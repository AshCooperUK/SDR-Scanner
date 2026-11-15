from contextlib import nullcontext
from email.message import Message
from http import HTTPStatus
from io import BytesIO
from ipaddress import IPv4Address
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from access_network import LocalNetworks
from access_pin import AccessPin, PIN_SETUP_PATTERN, PIN_SETUP_SECONDS
from app import PanelHandler, ScannerRuntime, valid_action
from radio_pool import RadioPool
from receiver import WATERFALL_BINS


class LocalNameTests(unittest.TestCase):
    def test_lan_name_comes_from_the_destination_and_requires_forward_match(self):
        networks = LocalNetworks()
        destination = IPv4Address("192.168.42.127")
        with patch("access_network._command_output", side_effect=[
            "192.168.42.127 scanner.home.arpa\n",
            "192.168.42.127 STREAM scanner.home.arpa\n",
            "192.168.42.127 scanner.home.arpa\n",
            "192.168.42.90 STREAM scanner.home.arpa\n",
        ]) as command, patch("access_network.monotonic", return_value=24_000) as now:
            self.assertTrue(networks.is_local_name("scanner.home.arpa", destination))
            self.assertTrue(networks.is_local_name("scanner.home.arpa.", destination))
            self.assertFalse(networks.is_local_name("unrelated.example.com", destination))
            self.assertEqual(command.call_count, 2)
            command.assert_called_with("getent", "--", "ahostsv4", "scanner.home.arpa")
            now.return_value += 61
            self.assertFalse(networks.is_local_name("scanner.home.arpa", destination))

    def test_docker_magicdns_uses_the_known_peer_on_the_exact_tunnel(self):
        networks = LocalNetworks()
        destination = IPv4Address("100.75.4.18")
        status = {
            "BackendState": "Running",
            "Self": {"TailscaleIPs": [str(destination)], "DNSName": "scanner.example.ts.net."},
            "Peer": {"client": {"TailscaleIPs": ["100.75.4.19"]}},
        }
        with patch("access_network._command_json", side_effect=[None, status]) as command, patch(
            "access_network._ip_json", side_effect=[
                [{"ifname": "tailscale0", "flags": ["UP", "POINTOPOINT", "NOARP"],
                  "addr_info": [{"local": str(destination), "prefixlen": 32}]}],
                [{"dev": "tailscale0", "dst": "100.75.4.19"},
                 {"dev": "tailscale0", "dst": "100.75.4.20"}],
            ],
        ):
            self.assertTrue(networks.is_local(IPv4Address("100.75.4.19"), destination))
            self.assertFalse(networks.is_local(IPv4Address("100.75.4.20"), destination))
            self.assertTrue(networks.is_local_name("scanner.example.ts.net", destination))
            self.assertTrue(networks.is_local_name("scanner", destination))
            self.assertFalse(networks.is_local_name("scanner.example.ts.net", IPv4Address("100.75.4.21")))
            command.assert_called_with("docker", "exec", "tailscale", "tailscale", "status", "--json")


class AccessPolicyTests(unittest.TestCase):
    def request(self, host, *, peer="192.168.42.26", destination="192.168.42.18",
                remote_url=None, headers=()):
        handler = PanelHandler.__new__(PanelHandler)
        handler.headers = Message()
        handler.headers["Host"] = host
        for key, value in headers:
            handler.headers[key] = value
        handler.client_address = (peer, 32000)
        handler.connection = Mock()
        handler.connection.getsockname.return_value = (destination, 8766)
        handler.send_error = Mock()
        networks = Mock(spec=LocalNetworks)
        networks.is_local.return_value = peer == "192.168.42.26"
        networks.is_local_name.side_effect = lambda name, _: name == "scanner.home.arpa"
        handler.server = SimpleNamespace(
            server_port=8766,
            local_networks=networks,
            access_pin=SimpleNamespace(
                snapshot=lambda: {"remote_url": remote_url, "revision": "test"},
                guard=nullcontext,
                remote_url=remote_url,
                configured=True,
                authorized=lambda *_: False,
            ),
        )
        return handler.accept_request(), handler

    def program_pin(self, handler, pin, token, body):
        handler.server.access_pin = pin
        handler.json_response = Mock()
        payload = json.dumps(body).encode()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(payload))
        handler.rfile = BytesIO(payload)
        handler.program_or_unlock_pin(token)
        reply, *status = handler.json_response.call_args.args
        return reply, status[0] if status else HTTPStatus.OK

    def test_local_lan_hostname_and_numeric_connections(self):
        for host in ("scanner.home.arpa:8766", "192.168.42.18:8766"):
            with self.subTest(host=host):
                allowed, handler = self.request(host, remote_url="")
                self.assertTrue(allowed)
                self.assertFalse(handler.needs_pin(None))
                self.assertFalse(handler.pin_admin)
                self.assertTrue(handler.local_pin_setup_allowed())

    def test_remote_and_forwarded_connections_still_need_a_pin(self):
        for host, options in (
            ("scanner.example.com:8766", {}),
            ("scanner.home.arpa:8766", {"peer": "203.0.113.15"}),
            ("scanner.home.arpa:8766", {"headers": [("X-Forwarded-Proto", "https")]}),
            ("scanner.home.arpa:8766", {"remote_url": "http://scanner.home.arpa:8766"}),
        ):
            with self.subTest(host=host, options=options):
                allowed, handler = self.request(host, **options)
                self.assertTrue(allowed)
                self.assertFalse(handler.direct_request)
                self.assertTrue(handler.needs_pin(None))
                self.assertFalse(handler.pin_admin)
                self.assertFalse(handler.local_pin_setup_allowed())

    def test_loopback_keeps_numeric_admin_access(self):
        allowed, handler = self.request("127.0.0.1:8766", peer="127.0.0.1", destination="127.0.0.1")
        self.assertTrue(allowed)
        self.assertTrue(handler.pin_admin)
        self.assertFalse(handler.local_pin_setup_allowed())
        allowed, handler = self.request("scanner.home.arpa:8766", peer="127.0.0.1", destination="127.0.0.1")
        self.assertTrue(allowed)
        self.assertFalse(handler.direct_request)
        self.assertFalse(handler.pin_admin)

    def test_setup_code_issuance_requires_loopback_and_same_origin(self):
        with TemporaryDirectory() as folder:
            pin = AccessPin(Path(folder) / "access.json")
            for host, peer, destination, headers in (
                ("127.0.0.1:8766", "127.0.0.1", "127.0.0.1", []),
                ("127.0.0.1:8766", "127.0.0.1", "127.0.0.1",
                 [("Origin", "http://127.0.0.1:8766"), ("X-Forwarded-Proto", "http")]),
                ("192.168.42.18:8766", "192.168.42.26", "192.168.42.18",
                 [("Origin", "http://192.168.42.18:8766")]),
            ):
                with self.subTest(host=host, headers=headers):
                    allowed, handler = self.request(host, peer=peer, destination=destination, headers=headers)
                    self.assertTrue(allowed)
                    handler.server.access_pin = pin
                    handler.json_response = Mock()
                    handler.issue_pin_setup_code()
                    handler.send_error.assert_called_once_with(HTTPStatus.FORBIDDEN)
                    handler.json_response.assert_not_called()
            allowed, owner = self.request("127.0.0.1:8766", peer="127.0.0.1", destination="127.0.0.1",
                                          headers=[("Origin", "http://127.0.0.1:8766")])
            self.assertTrue(allowed)
            owner.server.access_pin = pin
            owner.json_response = Mock()
            owner.issue_pin_setup_code()
            reply, = owner.json_response.call_args.args
            self.assertTrue(PIN_SETUP_PATTERN.fullmatch(reply["setup_code"]))
            self.assertEqual(reply["expires_in"], PIN_SETUP_SECONDS)
            self.assertFalse(pin.configured)

    def test_loopback_owner_can_program_pin_without_touching_the_radio(self):
        with TemporaryDirectory() as folder:
            pin = AccessPin(Path(folder) / "access.json")
            token = "1" * 32
            old_grant = None
            for power, is_scanning, digits in ((False, False, "123456"),
                                               (True, False, "234567"),
                                               (True, True, "345678")):
                with self.subTest(power=power, is_scanning=is_scanning):
                    allowed, handler = self.request("127.0.0.1:8766", peer="127.0.0.1", destination="127.0.0.1",
                                                    headers=[("Origin", "http://127.0.0.1:8766")])
                    self.assertTrue(allowed)
                    runtime = Mock()
                    runtime.state.return_value = {"power": power, "is_scanning": is_scanning, "frequency": 128_125_000}
                    handler.radio = Mock(return_value=(runtime, 1 if power else None, False))
                    before = runtime.state.return_value.copy()
                    reply, status = self.program_pin(handler, pin, token, {"new_pin": digits})
                    self.assertEqual(status, HTTPStatus.OK)
                    self.assertTrue(reply["pin_configured"])
                    handler.radio.assert_not_called()
                    self.assertEqual(runtime.mock_calls, [])
                    self.assertEqual(runtime.state.return_value, before)
                    if old_grant:
                        self.assertFalse(pin.authorized(old_grant, token))
                    old_grant, _ = pin.unlock(digits, IPv4Address("203.0.113.15"), token)
                    self.assertTrue(pin.authorized(old_grant, token))

    def test_setup_code_requires_trusted_lan_and_is_single_use(self):
        with TemporaryDirectory() as folder:
            pin = AccessPin(Path(folder) / "access.json")
            code = pin.issue_setup_code()
            token = "1" * 32
            for peer, body, power, expected in (
                ("192.168.42.26", {"new_pin": "123456"}, False, HTTPStatus.FORBIDDEN),
                ("203.0.113.15", {"new_pin": "123456", "setup_code": code}, False, HTTPStatus.FORBIDDEN),
                ("192.168.42.26", {"new_pin": "123456", "setup_code": "0" * 32}, False, HTTPStatus.FORBIDDEN),
                ("192.168.42.26", {"new_pin": "123456", "setup_code": code}, True, HTTPStatus.OK),
                ("192.168.42.26", {"new_pin": "654321", "setup_code": code}, False, HTTPStatus.FORBIDDEN),
            ):
                with self.subTest(peer=peer, power=power, expected=expected):
                    allowed, handler = self.request("192.168.42.18:8766", peer=peer,
                                                    headers=[("Origin", "http://192.168.42.18:8766")])
                    self.assertTrue(allowed)
                    handler.radio = Mock(return_value=(SimpleNamespace(state=lambda: {"power": power}), 1, False))
                    reply, status = self.program_pin(handler, pin, token, body)
                    self.assertEqual(status, expected)
                    self.assertEqual(reply["can_program_pin_with_code"], peer == "192.168.42.26")
                    handler.radio.assert_not_called()
            self.assertTrue(pin.configured)

    def test_expired_or_superseded_setup_code_cannot_change_pin(self):
        with TemporaryDirectory() as folder, patch("access_pin.time.monotonic", return_value=3600) as now:
            pin = AccessPin(Path(folder) / "access.json")
            superseded = pin.issue_setup_code()
            code = pin.issue_setup_code()
            self.assertFalse(pin.set_pin_with_setup_code("123456", superseded))
            now.return_value += PIN_SETUP_SECONDS
            self.assertFalse(pin.set_pin_with_setup_code("123456", code))
            self.assertFalse(pin.configured)

    def test_root_and_lcd_serve_only_the_current_panel(self):
        allowed, handler = self.request("192.168.42.18:8766")
        self.assertTrue(allowed)
        handler.response = Mock()
        for path in ("/", "/lcd"):
            with self.subTest(path=path):
                handler.path = path
                handler.response.reset_mock()
                handler.do_GET()
                handler.response.assert_called_once()
                media_type, body = handler.response.call_args.args
                self.assertEqual(media_type, "text/html")
                self.assertIn(b'id="sdr-v10-panel"', body)
                self.assertIn(b'src="/v10.js"', body)
                handler.send_error.assert_not_called()
        retired_paths = (
            "/legacy", "/index.html", "/panel.css", "/panel.js", "/panel.png",
            "/dmr-panel.css", "/dmr-panel.js", "/field-background.webp",
            "/waterfall.js", "/workbench.webp",
        )
        with TemporaryDirectory() as folder, patch("app.ROOT", Path(folder)):
            web = Path(folder) / "web"
            web.mkdir()
            for path in retired_paths:
                with self.subTest(path=path):
                    (web / path.removeprefix("/")).touch()
                    handler.path = path
                    handler.response.reset_mock()
                    handler.send_error.reset_mock()
                    handler.do_GET()
                    handler.send_error.assert_called_once_with(HTTPStatus.NOT_FOUND)
                    handler.response.assert_not_called()

    def test_current_panel_actions_are_accepted(self):
        for action in (
            {"knob": "volume", "value": 0.5, "panel": "v10"},
            {"knob": "volume", "value": 0},
            {"knob": "squelch", "value": 0.25},
            {"v10": "tune", "hz": 98_400_000, "mode": "WFM"},
            {"setting": "tone"},
            {"record": "toggle"},
            {"record": "select", "path": "/var/lib/scanner/recordings"},
            {"discovery": "toggle"},
            {"dmr": "delete_sms", "id": 1, "time_ms": 1000},
        ):
            with self.subTest(action=action):
                self.assertTrue(valid_action(action))

    def test_retired_panel_actions_cannot_reach_the_receiver(self):
        for action in (
            {"knob": "volume", "value": 0.5},
            {"knob": "volume", "value": 0.5, "panel": "legacy"},
            {"setting": "dab"},
            {"setting": "dab", "gesture": "long"},
            {"tune": "frequency", "mhz": "98.4"},
            {"favorite": 1, "gesture": "tap"},
            {"key": "scan", "gesture": "tap"},
            {"dmr": "toggle", "enabled": True},
        ):
            with self.subTest(action=action):
                allowed, handler = self.request("192.168.42.18:8766")
                self.assertTrue(allowed)
                handler.path = "/api/action"
                payload = json.dumps(action).encode()
                handler.headers["Content-Type"] = "application/json"
                handler.headers["Content-Length"] = str(len(payload))
                handler.rfile = BytesIO(payload)
                handler.radio = Mock()
                handler.do_POST()
                handler.send_error.assert_called_once_with(HTTPStatus.BAD_REQUEST)
                handler.radio.assert_not_called()

    def test_remote_display_exposes_only_lcd_telemetry_without_a_lease(self):
        with TemporaryDirectory() as folder:
            pool = RadioPool(Path(folder) / "state.json", ScannerRuntime)
            self.addCleanup(pool.close)
            radio = pool.primary
            radio.act({"knob": "volume", "value": .5, "panel": "v10"})
            radio.act({"v10": "memory", "operation": "write", "group": 0, "slot": 1,
                       "hz": 446_006_250, "mode": "DMR", "name": "Private memory"})
            radio.act({"v10": "memory", "operation": "recall", "group": 0, "slot": 1})
            radio._dmr_messages.append({"message": "Private message", "radio_id": 1898})
            radio.receiver_status = "ready"
            target = (446_006_250, "DMR")
            radio.signal = (target, -65.1, -67.2, 0)
            frame = {"sequence": 1, "time_ms": 512, "center_hz": target[0], "span_hz": 1_000_000,
                     "levels": (-80,) * WATERFALL_BINS, "private": "Extra FFT metadata"}
            radio.waterfall = (target, frame)
            host = "scanner.example.com"
            allowed, handler = self.request(host, remote_url="https://" + host,
                                            headers=[("X-Forwarded-Proto", "https")])
            self.assertTrue(allowed)
            self.assertTrue(handler.needs_pin(None))
            handler.path = "/api/v10/display"
            handler.json_response = Mock()
            handler.radio = Mock()
            handler.server.radios = pool
            with patch.object(radio, "state", side_effect=AssertionError("private state requested")):
                handler.do_GET()
                display, = handler.json_response.call_args.args
                self.assertEqual((display["power"], display["tune_hz"], display["modulation"]), (True, *target))
                self.assertEqual(display["signal_dbfs"], -65.1)
                self.assertRegex(display["clock"], r"^\d{2}:\d{2}$")
                self.assertEqual(display["waterfall"], {key: value for key, value in frame.items() if key != "private"})
                self.assertEqual(set(display), {"power", "tune_hz", "modulation", "clock", "receiver", "muted",
                                               "audio_open", "signal_dbfs", "signal_meter_dbfs", "squelch_db",
                                               "scan_active", "search_active", "scan_direction", "waterfall", "v10"})
                self.assertEqual(set(display["v10"]), {"auto_mode", "frequency_mode", "scan_held", "scan_kind", "scan_paused",
                                                     "scan_zone", "scan_zones", "nearby_sweeping", "filter", "tune_step",
                                                     "custom_preset_names", "nearby_enabled", "nearby_receiving",
                                                     "nearby_held"})
                self.assertEqual(display["v10"]["scan_zones"], [])
                self.assertFalse(display["v10"]["scan_paused"])
                self.assertFalse(display["v10"]["nearby_enabled"])
                self.assertNotIn("Private", json.dumps(display))
                radio.waterfall = (target, {**frame, "levels": (-80,) * (WATERFALL_BINS + 1)})
                handler.do_GET()
                self.assertIsNone(handler.json_response.call_args.args[0]["waterfall"])
            for zone, name in (("CUSTOM1", "Air local"), ("CUSTOM2", "Private unselected range")):
                radio.act({"v10": "zone", "operation": "configure", "zone": zone, "name": name,
                           "start_hz": 128_125_000, "end_hz": 128_150_000})
            radio.act({"v10": "scan", "kind": "ZONE", "zones": ["PMR", "AIR", "CUSTOM1"], "enabled": True})
            radio.act({"v10": "nearby", "enabled": True})
            with patch.object(radio, "state", side_effect=AssertionError("private state requested")):
                handler.do_GET()
                display = handler.json_response.call_args.args[0]["v10"]
                self.assertEqual(display["scan_zones"], ["PMR", "AIR", "CUSTOM1"])
                self.assertFalse(display["scan_paused"])
                self.assertTrue(display["nearby_enabled"])
                self.assertTrue(display["nearby_sweeping"])
                self.assertFalse(display["nearby_receiving"])
                self.assertEqual(display["custom_preset_names"], {"CUSTOM1": "Air local"})
                self.assertNotIn("Private", json.dumps(display))
                radio._v10_advance(radio.v10.next_scan_at, True)
                handler.do_GET()
                self.assertTrue(handler.json_response.call_args.args[0]["v10"]["scan_paused"])
            handler.radio.assert_not_called()
            self.assertEqual(pool._leases, {})

    def test_remote_display_preserves_url_allowlist_and_other_route_protection(self):
        url = "https://scanner.example.com"
        for host, origin, allowed in (("unrelated.example.com", "https://unrelated.example.com", False),
                                      ("scanner.example.com", "https://unrelated.example.com", False),
                                      ("scanner.example.com", url, True)):
            with self.subTest(host=host, origin=origin):
                _, handler = self.request(host, remote_url=url,
                                          headers=[("Origin", origin), ("X-Forwarded-Proto", "https")])
                handler.path = "/api/v10/display"
                handler.server.radios = Mock()
                handler.radio = Mock()
                handler.json_response = Mock()
                handler.send_error.reset_mock()
                handler.do_GET()
                if not allowed:
                    handler.send_error.assert_called_once_with(HTTPStatus.FORBIDDEN)
                    handler.server.radios.lcd_display.assert_not_called()
                    continue
                handler.server.radios.lcd_display.assert_called_once_with()
                for method, path in ((handler.do_POST, "/api/v10/display"),
                                     (handler.do_POST, "/api/action"),
                                     (handler.do_GET, "/api/v10/display?session=" + "1" * 32),
                                     (handler.do_GET, "/api/state"), (handler.do_GET, "/audio"),
                                     (handler.do_GET, "/api/discovery"),
                                     (handler.do_GET, "/api/recordings/test.mp3"),
                                     (handler.do_GET, "/api/v10/backup")):
                    with self.subTest(path=path):
                        handler.path = path
                        method()
                        self.assertEqual(handler.json_response.call_args.args[1], HTTPStatus.UNAUTHORIZED)
                handler.radio.assert_not_called()

    def test_second_display_follows_first_powered_v10_tuner(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            pool = RadioPool(path / "state.json", ScannerRuntime)
            self.addCleanup(pool.close)
            secondary = ScannerRuntime(path / "secondary.json", receiver_index=1)
            pool._radios[1] = secondary
            self.assertFalse(pool.lcd_display()["power"])
            secondary.act({"knob": "volume", "value": .5, "panel": "v10"})
            secondary.act({"v10": "tune", "hz": 98_400_000, "mode": "WFM"})
            self.assertEqual(pool.lcd_display()["tune_hz"], 98_400_000)
            pool.primary.act({"knob": "volume", "value": .5, "panel": "v10"})
            pool.primary.act({"v10": "tune", "hz": 128_125_000, "mode": "AUTO"})
            primary = pool.lcd_display()
            self.assertEqual((primary["tune_hz"], primary["modulation"]), (128_125_000, "AM"))
            self.assertTrue(primary["v10"]["auto_mode"])
            pool.primary.act({"knob": "volume", "value": 0, "panel": "v10"})
            self.assertEqual(pool.lcd_display()["tune_hz"], 98_400_000)
            self.assertEqual(pool._leases, {})


if __name__ == "__main__":
    unittest.main()

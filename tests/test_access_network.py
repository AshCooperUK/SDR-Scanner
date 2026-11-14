from contextlib import nullcontext
from email.message import Message
from ipaddress import IPv4Address
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from access_network import LocalNetworks
from app import PanelHandler


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
                authorized=lambda *_: False,
            ),
        )
        return handler.accept_request(), handler

    def test_local_lan_hostname_and_numeric_connections(self):
        for host in ("scanner.home.arpa:8766", "192.168.42.18:8766"):
            with self.subTest(host=host):
                allowed, handler = self.request(host, remote_url="")
                self.assertTrue(allowed)
                self.assertFalse(handler.needs_pin(None))
                self.assertFalse(handler.pin_admin)

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

    def test_loopback_keeps_numeric_admin_access(self):
        allowed, handler = self.request("127.0.0.1:8766", peer="127.0.0.1", destination="127.0.0.1")
        self.assertTrue(allowed)
        self.assertTrue(handler.pin_admin)
        allowed, handler = self.request("scanner.home.arpa:8766", peer="127.0.0.1", destination="127.0.0.1")
        self.assertTrue(allowed)
        self.assertFalse(handler.direct_request)
        self.assertFalse(handler.pin_admin)


if __name__ == "__main__":
    unittest.main()

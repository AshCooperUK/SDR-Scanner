"""Shared browser setup persistence, conflict handling and request boundaries."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from email.message import Message
from http import HTTPStatus
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import PanelHandler
from browser_profiles import BrowserProfile, MOBILE_KEY_ACTIONS, ProfileConflict, validate_settings


def settings():
    result = {
        "lcd_backlight": 100, "mute_button_dim": True, "led_bright": 100, "lcd_type": "A",
        "waterfall_colours": "spectrum", "display_memory": True, "display_rds": True,
        "p25_rx_id_format": "decimal", "speech_language": "en-GB", "speech_speed": "fast",
        "speech_signal": True, "speech_level": 50, "beep_level": 50, "beep_confirmation": True,
        "speech_lock_switch": "speech", "screen_capture": False, "scope_max_hold": "off",
        "scope_averaging": 0, "scope_waterfall": True, "scope_waterfall_speed": "mid",
        "scope_reference": 0, "scope_sweep_speed": "mid", "meter_type": "dbfs", "snap_to_carrier": True,
        "volume": 32, "volume_muted": False, "audio_boost": 0, "powerVolume": .32,
        "panel_display": {"dialLock": False, "channelDial": False, "page": 1, "expanded": False,
                          "visible": True, "freeze": False, "fixed": False, "marker": False,
                          "span": 1_000_000, "edges": None, "scopePan": 0},
        "favorite_bank_a": 0, "favorite_bank_b": 1, "quick_menu_v3": ["scan-speed", "scan-pause"],
        "radio_bookmarks": [], "repeater_locator": "",
    }
    for prefix in [*("dial_" + letter for letter in "abcdefghij"), "main_dial"]:
        result.update({prefix + "_function": "unassigned", prefix + "_press": "auto", prefix + "_sensitivity": 1})
    result["dial_auto_ts"] = "high"
    return result


class BrowserProfileTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.profile.json"
        self.profile = BrowserProfile(self.path)

    def test_seed_round_trip_permissions_and_no_live_operations(self):
        self.assertEqual(self.profile.snapshot(), {"revision": 0, "settings": None})
        self.assertFalse(self.path.exists())
        first = settings()
        first["dial_b_function"] = "scope-pan"
        first["profile_tune"] = {"hz": 166_924_262, "mode": "FM", "frequency_mode": "memory", "group": 1, "slot": 9}
        first["profile_squelch"] = .41
        first["profile_history_logging"] = False
        result = self.profile.patch(0, first)
        first["lcd_type"] = "E"
        self.assertEqual(result["settings"]["lcd_type"], "A")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(BrowserProfile(self.path).snapshot(), result)
        self.assertEqual(result["settings"]["dial_b_function"], "scope-pan")

    def test_conflicting_patch_can_retry_without_losing_other_browser_changes(self):
        self.profile.patch(0, settings())
        self.profile.patch(1, {"lcd_type": "D"})
        with self.assertRaises(ProfileConflict):
            self.profile.patch(1, {"dial_g_function": "scan_speed"})
        result = self.profile.patch(2, {"dial_g_function": "scan_speed"})
        self.assertEqual(result["settings"]["lcd_type"], "D")
        self.assertEqual(result["settings"]["dial_g_function"], "scan_speed")
        self.assertEqual(result["revision"], 3)
        self.assertEqual(self.profile.patch(3, {"lcd_type": "D"}), result)

    def test_mobile_keys_are_optional_for_existing_profiles_and_round_trip_new_assignments(self):
        original = self.profile.patch(0, settings())
        reloaded = BrowserProfile(self.path)
        self.assertEqual(reloaded.snapshot(), original)
        self.assertNotIn("mobile_p1_action", original["settings"])
        self.assertNotIn("mobile_p2_action", original["settings"])
        updated = reloaded.patch(1, {"mobile_p1_action": "quick", "mobile_p2_action": "monitor"})
        self.assertEqual(updated["settings"]["mobile_p1_action"], "quick")
        self.assertEqual(updated["settings"]["mobile_p2_action"], "monitor")
        self.assertEqual(BrowserProfile(self.path).snapshot(), updated)
        removed = reloaded.patch(2, {"mobile_p1_action": None, "mobile_p2_action": None})
        self.assertEqual(removed["settings"], original["settings"])
        self.assertEqual(BrowserProfile(self.path).snapshot(), removed)

    def test_mobile_keys_accept_only_known_action_strings_without_partial_writes(self):
        for action in MOBILE_KEY_ACTIONS:
            with self.subTest(action=action):
                complete = settings()
                complete.update(mobile_p1_action=action, mobile_p2_action=action)
                validated = validate_settings(complete)
                self.assertEqual(validated["mobile_p1_action"], action)
                self.assertEqual(validated["mobile_p2_action"], action)
        self.profile.patch(0, settings())
        before = self.path.read_bytes()
        for value in ("power", "unknown", "QUICK", "", True, 1, 0.5, [], {}, ["quick"]):
            for key in ("mobile_p1_action", "mobile_p2_action"):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        self.profile.patch(1, {"volume": 40, key: value})
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertEqual(self.profile.snapshot()["revision"], 1)

    def test_incompatible_known_press_is_normalized_after_seed_and_patch_merge(self):
        complete = settings()
        complete.update(dial_b_function="scope-pan", dial_b_press="mute")
        first = self.profile.patch(0, complete)
        self.assertEqual(first["settings"]["dial_b_press"], "auto")
        second = self.profile.patch(1, {"dial_b_function": "volume", "dial_b_press": "mute"})
        self.assertEqual(second["settings"]["dial_b_press"], "mute")
        third = self.profile.patch(2, {"dial_b_function": "scope-pan"})
        self.assertEqual(third["settings"]["dial_b_press"], "auto")
        self.assertEqual(BrowserProfile(self.path).snapshot(), third)
        self.assertEqual(complete["dial_b_press"], "mute")

    def test_concurrent_revision_writers_cannot_overwrite_each_other(self):
        self.profile.patch(0, settings())

        def update(value):
            try:
                return self.profile.patch(1, {"lcd_backlight": value})["revision"]
            except ProfileConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, [40, 60]))
        self.assertCountEqual(results, [2, "conflict"])

    def test_optional_deletions_and_validation_fail_without_partial_writes(self):
        complete = settings()
        complete["scan_PROG"] = {"v10": "scan", "kind": "PROG", "enabled": True, "start_hz": 100_000,
                                 "end_hz": 200_000, "mode": "AM"}
        self.profile.patch(0, complete)
        self.assertNotIn("scan_PROG", self.profile.patch(1, {"scan_PROG": None})["settings"])
        before = self.path.read_bytes()
        bad = [
            {"volume": 30, "lcd_type": "unknown"}, {"volume": None}, {"dial_g_function": "power"},
            {"dial_a_sensitivity": True}, {"dial_a_sensitivity": 3}, {"dial_a_press": "unknown"},
            {"profile_squelch": float("nan")}, {"profile_history_logging": 1},
            {"profile_tune": {"hz": 1, "mode": "FM", "frequency_mode": "vfo", "group": 0, "slot": 0}},
            {"access": {"pin": "1234"}}, {"pin": "1234"}, {"session": "a" * 32}, {"power": True},
            {"quick_menu_v2": []}, {"quick_menu_v3": ["scan-speed", "scan-speed"]},
            {"radio_bookmarks": [{"name": "😀" * 33, "hz": 145_500_000, "mode": "FM"}]},
            {"radio_bookmarks": [{"name": "\ud800", "hz": 145_500_000, "mode": "FM"}]},
        ]
        for changes in bad:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.profile.patch(2, changes)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.profile.snapshot()["revision"], 2)
        with self.assertRaises(ValueError):
            self.profile.patch(True, {})
        with self.assertRaises(ValueError):
            BrowserProfile(self.path.parent / "empty.json").patch(0, {"volume": 50})

    def test_failed_save_keeps_previous_profile(self):
        self.profile.patch(0, settings())
        before = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.profile.patch(1, {"volume": 50})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.profile.snapshot()["settings"]["volume"], 32)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_rejects_unsafe_files_and_invalid_persisted_data(self):
        self.profile.patch(0, settings())
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            BrowserProfile(self.path)
        self.path.chmod(0o600)
        self.path.write_text('{"revision":1,"settings":{"volume":30}}')
        with self.assertRaises(ValueError):
            BrowserProfile(self.path)
        link = self.path.parent / "linked.json"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            BrowserProfile(link)


class ProfileRequestTests(unittest.TestCase):
    def handler(self, *, upload=False, authenticated=True, origin=True, marker=False):
        handler = PanelHandler.__new__(PanelHandler)
        handler.headers = Message()
        if marker:
            handler.headers["X-SDR-Profile"] = "1"
        handler.origin_checked = origin
        handler.direct_request = True
        handler.path = "/api/profile?session=" + "a" * 32
        handler.session_route = Mock(return_value=("/api/profile", "a" * 32, None))
        handler.accept_request = Mock(return_value=True)
        handler.check_pin = Mock(return_value=authenticated)
        handler.server = SimpleNamespace(browser_profile=Mock())
        handler.server.browser_profile.snapshot.return_value = {"revision": 0, "settings": None}
        handler.server.browser_profile.patch.return_value = {"revision": 1, "settings": settings()}
        handler.json_response = Mock()
        handler.send_error = Mock()
        handler.rfile = Mock()
        return handler

    def test_locked_browser_cannot_read_or_write_the_shared_setup(self):
        for method in ("do_GET", "do_POST"):
            handler = self.handler(authenticated=False)
            getattr(handler, method)()
            handler.server.browser_profile.snapshot.assert_not_called()
            handler.server.browser_profile.patch.assert_not_called()
            handler.rfile.read.assert_not_called()

    def test_read_requires_marker_or_checked_origin_and_session(self):
        handler = self.handler(origin=False)
        handler.do_GET()
        self.assertEqual(handler.json_response.call_args.args[1], HTTPStatus.FORBIDDEN)
        handler.server.browser_profile.snapshot.assert_not_called()
        handler.headers["X-SDR-Profile"] = "1"
        handler.do_GET()
        handler.server.browser_profile.snapshot.assert_called_once()
        handler.json_response.reset_mock()
        handler.browser_profile_request(None)
        self.assertEqual(handler.json_response.call_args.args[1], HTTPStatus.FORBIDDEN)

    def test_post_requires_origin_even_for_trusted_local_browser(self):
        handler = self.handler(origin=False, marker=True)
        handler.do_POST()
        handler.rfile.read.assert_not_called()
        handler.server.browser_profile.patch.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[1], HTTPStatus.FORBIDDEN)

    def test_valid_patch_and_conflict_return_current_revision(self):
        handler = self.handler()
        body = json.dumps({"revision": 1, "changes": {"volume": 40}}).encode()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = BytesIO(body)
        handler.do_POST()
        handler.server.browser_profile.patch.assert_called_once_with(1, {"volume": 40})
        handler.rfile = BytesIO(body)
        handler.server.browser_profile.patch.side_effect = ProfileConflict("changed")
        handler.do_POST()
        data, status = handler.json_response.call_args.args
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(data["revision"], 0)

    def test_oversized_or_malformed_request_is_rejected_before_write(self):
        handler = self.handler()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(128 * 1024 + 1)
        handler.do_POST()
        handler.rfile.read.assert_not_called()
        handler.server.browser_profile.patch.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[1], HTTPStatus.BAD_REQUEST)


if __name__ == "__main__":
    unittest.main()

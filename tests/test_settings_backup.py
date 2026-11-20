"""Complete settings round trips and owner-only credential backup boundaries."""

from copy import deepcopy
from email.message import Message
from http import HTTPStatus
import json
import queue
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from access_pin import AccessPin
from app import PanelHandler, ScannerRuntime
from settings_backup import scanner_document, server_backup, validate_server_backup


class SettingsBackupTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "scanner.json"
        self.runtime = ScannerRuntime(self.path)
        self.addCleanup(self.runtime.close)
        self.pin = AccessPin(self.path.with_suffix(".access.json"))

    def test_complete_round_trip_and_standby(self):
        runtime = self.runtime
        folder = self.path.parent / "my-recordings"
        folder.mkdir()
        runtime.scanner.recording_parent = folder
        runtime.scanner.clock_offset = 22
        runtime.scanner.rds_enabled = True
        runtime.scanner.permanent_frequencies = {145_500_000: None}
        runtime.scanner._save()
        self.assertEqual(scanner_document(runtime.scanner), json.loads(self.path.read_text()))
        runtime.v10.save(settings={**runtime.v10.settings, "noise_reduction": True},
                         bank_names={"1": "Local Repeaters"})
        runtime.dmr.act({"dmr": "save", "entry": {"hz": 438_500_000, "name": "DMR test"}})
        runtime.dmr._save(runtime.dmr.entries, {("hz", 438_500_000)})
        runtime.dmr.search = 430_000_000, 432_000_000, 6_250
        runtime.act({"knob": "volume", "value": .4, "panel": "v10"})
        runtime.act({"v10": "tune", "hz": 145_500_000, "mode": "FM"})
        runtime.scanner.squelch = .42
        runtime.discovery._logging["v10"] = False
        runtime.recording.enabled = True
        self.pin.set_pin("0042")
        with self.pin.program({"remote_url": "https://radio.example.com"}, self.pin.snapshot()["revision"]) as commit:
            commit()
        backup = server_backup(runtime, self.pin)
        self.assertNotIn("0042", json.dumps(backup))
        validate_server_backup(backup)
        runtime.v10.save(bank_names={"1": "Changed"})
        runtime.scanner.clock_offset = 1
        runtime.scanner.squelch = .8
        runtime.discovery._logging["v10"] = True
        self.pin.set_pin("5678")
        runtime.restore_settings(backup, self.pin)
        self.assertFalse(runtime.scanner.power)
        self.assertFalse(runtime.recording.enabled)
        self.assertIsNone(runtime.v10.scan)
        self.assertEqual(runtime.v10.vfo, (145_500_000, "FM"))
        self.assertEqual(runtime.scanner.squelch, .42)
        self.assertFalse(runtime.discovery.enabled)
        self.assertEqual(runtime.scanner.clock_offset, 22)
        self.assertEqual(runtime.scanner.permanent_frequencies, {145_500_000: None})
        self.assertTrue(runtime.v10.settings["noise_reduction"])
        self.assertEqual(runtime.v10.bank_names["1"], "Local Repeaters")
        self.assertEqual(runtime.dmr.search, (430_000_000, 432_000_000, 6_250))
        self.assertEqual(runtime.dmr.permanent_avoids, {("hz", 438_500_000)})
        self.assertEqual(runtime.recording.parent, folder)
        self.assertEqual(runtime.recording.recorder.destination(v10=True), folder)
        self.assertEqual(self.pin.backup(), backup["access"])
        reloaded = ScannerRuntime(self.path)
        self.addCleanup(reloaded.close)
        self.assertEqual(reloaded.v10.bank_names, {"1": "Local Repeaters"})
        self.assertEqual(reloaded.scanner.recording_parent, folder)
        self.assertEqual(reloaded.dmr.permanent_avoids, {("hz", 438_500_000)})

    def test_unconfigured_access_round_trip_survives_restart(self):
        backup = server_backup(self.runtime, self.pin)
        self.pin.set_pin("8")
        self.runtime.restore_settings(backup, self.pin)
        self.assertFalse(self.pin.path.exists())
        self.assertEqual(AccessPin(self.pin.path).backup(), {"remote_url": None, "credential": None})
        with self.pin.restore_backup({"remote_url": "", "credential": None}) as commit:
            commit()
        self.assertEqual(AccessPin(self.pin.path).backup(), {"remote_url": "", "credential": None})

    def test_malformed_and_partial_backups_make_no_changes(self):
        self.runtime.scanner._save()
        backup = server_backup(self.runtime, self.pin)
        before = self.path.read_bytes()
        candidates = []
        for section in ("receiver", "scanner", "dmr", "runtime", "access"):
            invalid = deepcopy(backup)
            invalid[section] = {}
            candidates.append(invalid)
        for invalid in candidates:
            with self.subTest(section=next(key for key in backup if backup[key] != invalid[key])):
                with self.assertRaises((ValueError, TypeError)):
                    self.runtime.restore_settings(invalid, self.pin)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.runtime.scanner.squelch, .5)
                self.assertFalse(self.pin.configured)
        invalid = deepcopy(backup)
        invalid["runtime"]["squelch"] = float("nan")
        with self.assertRaises(ValueError):
            validate_server_backup(invalid)

    def test_failed_file_commit_rolls_back_exact_previous_bytes(self):
        self.runtime.scanner._save()
        self.runtime.v10.save(bank_names={"2": "Before"})
        self.runtime.dmr._save({}, set())
        self.pin.set_pin("7")
        backup = server_backup(self.runtime, self.pin)
        backup["receiver"]["bank_names"] = {"2": "After"}
        before = {path: path.read_bytes() for path in (self.path, self.runtime.v10.path, self.runtime.dmr.path, self.pin.path)}
        replace = Path.replace
        failed = False

        def fail_once(source, target):
            nonlocal failed
            if Path(target) == self.runtime.dmr.path and not failed:
                failed = True
                raise OSError("simulated storage failure")
            return replace(source, target)

        with patch.object(Path, "replace", fail_once):
            with self.assertRaises(OSError):
                self.runtime.restore_settings(backup, self.pin)
        self.assertEqual(self.runtime.v10.bank_names, {"2": "Before"})
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_queued_timeout_cancels_restore_before_it_can_start(self):
        backup = server_backup(self.runtime, self.pin)
        reply = Mock()
        reply.get.side_effect = queue.Empty
        with patch("app.queue.SimpleQueue", return_value=reply), patch.object(self.runtime.recording, "_enqueue") as enqueue:
            with self.assertRaises(queue.Empty):
                self.runtime.restore_settings(backup, self.pin)
        task = enqueue.call_args.args[-1]
        self.assertTrue(task["cancelled"])
        self.assertFalse(task["started"])


class SettingsOwnerBoundaryTests(unittest.TestCase):
    def handler(self, *, local=True, origin=True, marker=False):
        handler = PanelHandler.__new__(PanelHandler)
        handler.headers = Message()
        if marker:
            handler.headers["X-SDR-Settings-Backup"] = "1"
        handler.pin_admin = local
        handler.local_pin_setup_allowed = Mock(return_value=False)
        handler.origin_checked = origin
        handler.server = SimpleNamespace(access_pin=Mock())
        handler.json_response = Mock()
        handler.send_error = Mock()
        handler.accept_request = Mock(return_value=True)
        handler.session_route = Mock(return_value=("/api/settings/backup", "a" * 32, None))
        handler.check_pin = Mock(return_value=True)
        handler.radio = Mock()
        handler.path = "/api/settings/backup?session=" + "a" * 32
        return handler

    def test_remote_even_unlocked_cannot_export_credentials(self):
        handler = self.handler(local=False)
        handler.do_GET()
        handler.radio.assert_not_called()
        handler.server.access_pin.backup.assert_not_called()
        self.assertEqual(handler.json_response.call_args.args[1], HTTPStatus.FORBIDDEN)

    def test_trusted_lan_cannot_export_or_restore_access_credentials(self):
        handler = self.handler(local=False)
        handler.local_pin_setup_allowed.return_value = True
        self.assertFalse(handler.settings_owner_access("a" * 32))
        self.assertFalse(handler.settings_owner_access("a" * 32, upload=True))
        handler.server.access_pin.backup.assert_not_called()

    def test_owner_requires_session_and_same_origin_read_or_header(self):
        handler = self.handler()
        self.assertFalse(handler.settings_owner_access(None))
        self.assertTrue(handler.settings_owner_access("a" * 32))
        handler.origin_checked = False
        self.assertFalse(handler.settings_owner_access("a" * 32))
        handler.headers["X-SDR-Settings-Backup"] = "1"
        self.assertTrue(handler.settings_owner_access("a" * 32))
        self.assertFalse(handler.settings_owner_access("a" * 32, upload=True))

    def test_remote_restore_rejected_before_body_or_credential_access(self):
        handler = self.handler(local=False)
        handler.rfile = Mock()
        handler.restore_settings_backup("a" * 32, None)
        handler.rfile.read.assert_not_called()
        handler.server.access_pin.restore_backup.assert_not_called()
        handler.radio.assert_not_called()


if __name__ == "__main__":
    unittest.main()

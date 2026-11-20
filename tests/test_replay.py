from contextlib import nullcontext
from datetime import datetime, timedelta
from email.message import Message
from http import HTTPStatus
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import quote
import os
import unittest
from unittest.mock import Mock, patch

from app import PanelHandler
from replay import RECORDINGS_SEARCH_URL, RECORDINGS_URL, latest_clip, open_clip, recording_filters, search_recordings


class RecordingReplayTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.older_root = self.root / "Recorded Audio"

    def write_clip(self, path, data, age):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (age, age))

    def read_clip(self, result, *older_roots):
        route = result["url"].removeprefix(RECORDINGS_URL)
        stream, size = open_clip(self.root, route, *older_roots)
        with stream:
            data = stream.read()
        self.assertEqual(len(data), size)
        return data

    def test_latest_resolves_new_and_legacy_clips_across_recording_roots(self):
        legacy = self.older_root / "443.00625mhz-141201-15112026.mp3"
        self.write_clip(legacy, b"legacy MP3", 1_700_000_002)
        new_name = "14:12:02 - 15112026 - 443.00625-2.mp3"
        self.write_clip(self.root / new_name, b"new V10 MP3", 1_700_000_001)
        old_result = latest_clip(self.root, self.older_root)
        self.assertEqual(old_result["label"], "443.00625 MHz · 141201-15112026.mp3")
        self.assertEqual(self.read_clip(old_result, self.older_root), b"legacy MP3")

        os.utime(self.root / new_name, (1_700_000_003, 1_700_000_003))
        new_result = latest_clip(self.root, self.older_root)
        self.assertTrue(new_result["url"].startswith(f"{RECORDINGS_URL}{quote(new_name, safe='')}/"))
        self.assertIn("%3A", new_result["url"])
        self.assertIn("%20", new_result["url"])
        self.assertEqual(new_result["label"], "443.00625 MHz · 14:12:02 - 15112026")
        self.assertEqual(self.read_clip(new_result, self.older_root), b"new V10 MP3")

    def test_fingerprint_selects_the_right_root_for_identical_clip_names(self):
        name = "14:12:03 - 15112026 - 443.00625.mp3"
        self.write_clip(self.root / name, b"primary MP3", 1_700_000_001)
        self.write_clip(self.older_root / name, b"other MP3", 1_700_000_002)
        result = latest_clip(self.root, self.older_root)
        self.assertEqual(self.read_clip(result, self.older_root), b"other MP3")
        self.write_clip(self.older_root / name, b"replacement MP3 file", 1_700_000_003)
        with self.assertRaises(FileNotFoundError):
            self.read_clip(result, self.older_root)

    def test_dated_v10_clips_play_from_both_the_root_and_the_saved_channel_folder(self):
        flat_name = "14:12:06 - 15.11.2026- 135.375000 MHz.mp3"
        self.write_clip(self.root / flat_name, b"VFO audio", 1_700_000_001)
        flat = latest_clip(self.root, self.older_root)
        self.assertEqual(flat["label"], "135.375000 MHz · 14:12:06 - 15.11.2026")
        self.assertEqual(self.read_clip(flat, self.older_root), b"VFO audio")

        folder = "CH2 - London Info"
        name = "14:12:07 - 15.11.2026- 135.375000 MHz-2.mp3"
        self.write_clip(self.root / folder / name, b"saved audio", 1_700_000_002)
        clip = latest_clip(self.root, self.older_root)
        self.assertTrue(clip["url"].startswith(f"{RECORDINGS_URL}{quote(folder, safe='')}/{quote(name, safe='')}/"))
        self.assertEqual(clip["label"], "CH2 - London Info · 135.375000 MHz · 14:12:07 - 15.11.2026")
        self.assertEqual(self.read_clip(clip, self.older_root), b"saved audio")

    def test_saved_channel_replay_accepts_only_supported_folders_and_plain_files(self):
        name = "14:12:06 - 15.11.2026- 135.375000 MHz.mp3"
        saved = self.root / "CH2 - London Info" / name
        self.write_clip(saved, b"named recording", 1_700_000_001)
        clip = latest_clip(self.root, self.older_root)
        self.write_clip(self.root / "My audio" / name, b"unrelated MP3", 1_700_000_002)
        self.write_clip(saved.with_name(name.replace("135.375000", "9999.000000")), b"out of range", 1_700_000_003)
        self.assertEqual(latest_clip(self.root, self.older_root), clip)

        saved.unlink()
        saved.symlink_to(self.root / "My audio" / name)
        self.assertIsNone(latest_clip(self.root, self.older_root))
        with self.assertRaises(OSError):
            self.read_clip(clip, self.older_root)

    def test_existing_frequency_and_dab_folder_routes_are_preserved(self):
        for name, folder, label in (("141203-15112026.mp3", "128.1250mhz", "128.1250 MHz"),
                                    ("141203-151126.mp3", "BBC Radio 4", "BBC Radio 4")):
            with self.subTest(folder=folder):
                self.write_clip(self.root / folder / name, b"legacy MP3", 1_700_000_002)
                clip = latest_clip(self.root)
                self.assertTrue(clip["url"].startswith(f"{RECORDINGS_URL}{quote(folder, safe='')}/{name}/"))
                self.assertEqual(clip["label"], f"{label} · {name}")
                self.assertEqual(self.read_clip(clip), b"legacy MP3")
                (self.root / folder / name).unlink()

    def test_v10_direct_folder_excludes_legacy_names_with_a_separate_older_root(self):
        older = self.older_root / "443.00625mhz-141201-15112026.mp3"
        self.write_clip(older, b"legacy recording", 1_700_000_001)
        for name in (older.name, "443.00625mhz/141202-15112026.mp3"):
            with self.subTest(name=name):
                local = self.root / name
                self.write_clip(local, b"unrelated recording", 1_700_000_002)
                standalone = latest_clip(self.root)
                self.assertEqual(self.read_clip(standalone), b"unrelated recording")
                with self.assertRaises(FileNotFoundError):
                    self.read_clip(standalone, self.older_root)
                selected = latest_clip(self.root, self.older_root)
                self.assertEqual(self.read_clip(selected, self.older_root), b"legacy recording")
                local.unlink()

    def test_latest_rejects_noncanonical_names_and_linked_recordings(self):
        name = "14:12:04 - 15112026 - 443.00625.mp3"
        self.write_clip(self.root / "14:12:04 - 15112026 - 443.006250.mp3", b"noncanonical", 1_700_000_003)
        self.write_clip(self.root / "14:12:04 - 15112026 - 9999.00625.mp3", b"out of range", 1_700_000_003)
        self.write_clip(self.root / name, b"valid MP3", 1_700_000_001)
        clip = latest_clip(self.root, self.older_root)
        self.assertEqual(self.read_clip(clip), b"valid MP3")

        other = self.root / "Other Folder"
        (self.root / name).unlink()
        self.write_clip(other / name, b"outside recording root", 1_700_000_002)
        (self.root / name).symlink_to(other / name)
        self.older_root.symlink_to(other, target_is_directory=True)
        self.assertIsNone(latest_clip(self.root, self.older_root))
        with self.assertRaises(OSError):
            self.read_clip(clip, self.older_root)
        route = f"%2E%2E%2F{quote(name, safe='')}/{'a' * 24}"
        with self.assertRaises(FileNotFoundError):
            open_clip(self.root, route, self.older_root)

    def search(self, **filters):
        return search_recordings(self.root, self.older_root, filters=recording_filters(filters))

    def test_search_filters_by_name_channel_frequency_and_date(self):
        name = "14:12:06 - 15.11.2026- 135.375000 MHz.mp3"
        for folder in ("CH2 - London Info", "CH20 - London Info", "CH3 - Bank 1", "CH4 - Bank 10"):
            self.write_clip(self.root / folder / name, folder.encode(), 1_700_000_001)
        self.write_clip(self.older_root / "135.3750mhz" / "141200-15112026.mp3", b"legacy", 1_700_000_002)
        self.write_clip(self.older_root / "BBC Radio 4" / "121200-161126.mp3", b"DAB", 1_700_000_002)

        result = self.search(query="CH2 london", date="15.11.2026", mhz="135.375.000 MHz")
        self.assertEqual(result["total"], 1)
        clip, = result["clips"]
        self.assertEqual(clip["folder"], "CH2 - London Info")
        self.assertEqual(clip["hz"], 135_375_000)
        self.assertEqual(clip["date"], "2026-11-15")
        self.assertEqual(self.read_clip(clip, self.older_root), b"CH2 - London Info")
        self.assertEqual(self.search(query="Bank 01")["total"], 1)
        self.assertEqual(self.search(date="2026-11-15", mhz="135.375")["total"], 5)
        self.assertEqual(self.search(mhz="135.375001")["total"], 0)
        dab, = self.search(query="BBC", date="2026-11-16")["clips"]
        self.assertIsNone(dab["hz"])
        self.assertEqual(self.read_clip(dab, self.older_root), b"DAB")

    def test_search_pages_sort_by_capture_date_and_exclude_symlinks(self):
        for second in range(12):
            name = f"14:12:{second:02} - 15.11.2026- 135.375000 MHz.mp3"
            self.write_clip(self.root / name, bytes([second]), 1_700_000_012 - second)
        outside = self.root / "Unrelated"
        self.write_clip(outside / name, b"outside", 1_700_000_003)
        (self.root / "CH2 - Linked").symlink_to(outside, target_is_directory=True)
        (self.root / "CH2 - Local").mkdir()
        (self.root / "CH2 - Local" / name).symlink_to(outside / name)
        os.link(outside / name, self.root / "CH2 - Local" / name.replace("14:12:", "14:13:"))

        first = self.search()
        self.assertEqual((first["page"], first["pages"], first["total"]), (1, 2, 12))
        self.assertEqual(len(first["clips"]), 10)
        self.assertEqual(self.read_clip(first["clips"][0]), b"\x0b")
        second = self.search(page=4)
        self.assertEqual(second["page"], 2)
        self.assertEqual([self.read_clip(clip) for clip in second["clips"]], [b"\x01", b"\x00"])

    def test_search_rejects_invalid_filters(self):
        for invalid in ({"query": ["London"]}, {"date": "31.02.2026"}, {"mhz": "MHZ"},
                        {"mhz": "1750.000001"}, {"page": True}, {"page": 0},
                        {"page": -1}, {"page": 1.5}, {"page": "1"}, {"page": 1 << 53},
                        {"path": "/tmp"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                recording_filters(invalid)

    def test_search_reaches_recordings_beyond_ten_thousand(self):
        total = 10_003
        start = datetime(2026, 11, 15)
        info = SimpleNamespace(st_dev=1, st_ino=1, st_ctime_ns=1, st_mtime_ns=1, st_size=1)

        def name_at(index):
            return f"{start + timedelta(seconds=index):%H:%M:%S - %d.%m.%Y}- 135.375000 MHz.mp3"

        def clips(*_):
            # Deliberately supply the oldest first: selection must sort the
            # entire matching archive, including clips past the former cap.
            for index in range(total):
                name = name_at(index)
                yield "", name, ("135.375000 MHz", name), info

        with patch("replay._finalized_clips", side_effect=clips):
            first = self.search()
            penultimate = self.search(page=1000)
            last = self.search(page=1001)
            beyond = self.search(page=(1 << 53) - 1)

        self.assertEqual((first["page"], first["pages"], first["total"]), (1, 1001, total))
        self.assertEqual(first["clips"][0]["name"], name_at(total - 1))
        self.assertEqual([clip["name"] for clip in penultimate["clips"]],
                         [name_at(index) for index in range(12, 2, -1)])
        self.assertEqual((last["page"], last["pages"], last["total"]), (1001, 1001, total))
        self.assertEqual([clip["name"] for clip in last["clips"]],
                         [name_at(index) for index in range(2, -1, -1)])
        self.assertEqual(beyond, last)

    def test_search_empty_archive_clamps_large_page_to_one(self):
        self.assertEqual(self.search(page=(1 << 53) - 1),
                         {"clips": [], "page": 1, "pages": 1, "total": 0})

    def test_search_route_obeys_pin_and_receiver_selection(self):
        name = "14:12:06 - 15.11.2026- 135.375000 MHz.mp3"
        self.write_clip(self.root / name, b"receiver clip", 1_700_000_001)
        body = json.dumps({"mhz": "135.375"}).encode()
        runtime = SimpleNamespace(lock=nullcontext(), recording=SimpleNamespace(
            playback_roots=lambda: (self.root, self.older_root),
        ))
        handler = PanelHandler.__new__(PanelHandler)
        handler.headers = Message()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(body))
        handler.server = SimpleNamespace()
        handler.rfile = BytesIO(body)
        handler.direct_request = False
        handler.origin_checked = True
        handler.accept_request = Mock(return_value=True)
        handler.session_route = Mock(return_value=(RECORDINGS_SEARCH_URL, "0" * 32, 1))
        handler.radio = Mock(return_value=(runtime, 1, False))
        handler.json_response = Mock()
        handler.check_pin = Mock(return_value=False)
        handler.do_POST()
        handler.radio.assert_not_called()
        handler.json_response.assert_not_called()
        self.assertEqual(handler.rfile.tell(), 0)

        handler.check_pin.return_value = True
        handler.do_POST()
        result, = handler.json_response.call_args.args
        self.assertEqual(self.read_clip(result["clips"][0], self.older_root), b"receiver clip")

        handler.rfile.seek(0)
        handler.radio.return_value = (runtime, 2, False)
        handler.do_POST()
        failure, status = handler.json_response.call_args.args
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(failure["error"], "Scanner receiver changed.")


if __name__ == "__main__":
    unittest.main()

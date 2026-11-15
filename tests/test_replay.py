from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote
import os
import unittest

from replay import RECORDINGS_URL, latest_clip, open_clip


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


if __name__ == "__main__":
    unittest.main()

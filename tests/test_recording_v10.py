from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import RecordingTarget, ScannerRuntime
from recording import MIN_CLIP_PCM_BYTES, Recorder, RecordingError, is_saved_channel_folder, saved_channel_folder


class TestEncoder:
    def encode(self, pcm):
        return b"encoded audio" if pcm else b""

    def finish(self):
        return b"end of audio"

    def close(self):
        pass


class V10RecordingTests(unittest.TestCase):
    def test_armed_v10_records_to_the_selected_folder_with_the_requested_name(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "Clips"
            selected.mkdir()
            path = root / "state.json"
            runtime = ScannerRuntime(path)
            self.addCleanup(runtime.close)
            frequency = 443_006_250
            tuned = RecordingTarget(frequency, "WFM", station_name="Test station")
            state = runtime.act({"knob": "volume", "value": 0.4, "panel": "v10"})
            self.assertFalse(state["record_enabled"])
            result = runtime.act({"record": "select", "path": str(selected)})
            self.assertTrue(result["record_selected"])
            self.assertEqual(result["record_folder"], str(selected))
            self.assertEqual(runtime.recording.playback_roots(), (selected, selected / "Recorded Audio"))

            date = datetime(2026, 11, 15, 14, 9, 15)
            pcm = b"\x01\x00" * (MIN_CLIP_PCM_BYTES // 2)
            with (patch("recording._Encoder", TestEncoder), patch.object(Recorder, "check_available"),
                  patch("recording.datetime") as clock):
                clock.now.return_value = date
                self.assertTrue(runtime.act({"record": "toggle"})["record_enabled"])
                with runtime.lock:
                    runtime.recording.receive(tuned, pcm[:MIN_CLIP_PCM_BYTES - 2])
                    runtime.recording.boundary()
                    runtime.recording.receive(tuned, pcm)
                self.assertFalse(runtime.act({"record": "toggle"})["record_enabled"])
                runtime.close()

            files = list(selected.iterdir())
            self.assertEqual([file.name for file in files], ["14:09:15 - 15.11.2026- 443.006250 MHz.mp3"])
            self.assertTrue(files[0].read_bytes().startswith(b"ID3"))
            restarted = ScannerRuntime(path)
            self.addCleanup(restarted.close)
            state = restarted.act({"knob": "volume", "value": 0.4, "panel": "v10"})
            self.assertFalse(state["record_enabled"])
            self.assertEqual(state["record_folder"], str(selected))

    def test_legacy_recording_keeps_its_directory_and_filename(self):
        with TemporaryDirectory() as directory, patch("recording._Encoder", TestEncoder):
            root = Path(directory)
            recorder = Recorder(root)
            date = datetime(2026, 11, 15, 14, 9, 15)
            clip = recorder.save_received(443_006_250, round(date.timestamp() * 1000), [b"\x01\x00"])
            self.assertEqual(clip, root / "Recorded Audio" / "443.00625mhz-140915-15112026.mp3")

    def test_named_memory_and_vfo_split_a_live_capture_on_the_same_frequency(self):
        with TemporaryDirectory() as directory, patch("recording._Encoder", TestEncoder), patch("recording.datetime") as clock:
            root = Path(directory)
            recorder = Recorder(root)
            clock.now.return_value = datetime(2026, 11, 15, 14, 9, 15)
            pcm = b"\x01\x00" * (MIN_CLIP_PCM_BYTES // 2)
            frequency = 135_375_000
            folder = saved_channel_folder(2, "London Info")
            filename = "14:09:15 - 15.11.2026- 135.375000 MHz.mp3"

            recorder.feed(frequency, pcm, v10=True, channel_folder=folder)
            self.assertEqual(recorder.active_path, root / "CH2 - London Info" / filename)
            recorder.feed(frequency, pcm, v10=True, station_name="Unsaved station name")
            self.assertTrue((root / folder / filename).read_bytes().startswith(b"ID3"))
            self.assertEqual(recorder.finish(), root / filename)

    def test_discovery_lookup_and_clear_scope_by_directory_and_frequency(self):
        with TemporaryDirectory() as directory, patch("recording._Encoder", TestEncoder):
            root = Path(directory)
            recorder = Recorder(root)
            date = datetime(2026, 11, 15, 14, 9, 15)
            time_ms = round(date.timestamp() * 1000)
            frequency = 135_375_000
            folder = saved_channel_folder(2, "London Info")
            pcm = [b"\x01\x00"]
            channel_clip = recorder.save_received(frequency, time_ms, pcm, v10=True, channel_folder=folder)
            concurrent_clip = recorder.save_received(frequency, time_ms, pcm, v10=True, channel_folder=folder)
            vfo_clip = recorder.save_received(frequency, time_ms, pcm, v10=True)
            previous_clip = recorder.save_received(frequency, time_ms, pcm, v10=True)
            previous_clip = previous_clip.rename(root / "14:09:15 - 15112026 - 135.3750.mp3")
            other_clip = recorder.save_received(135_400_000, time_ms, pcm, v10=True, channel_folder=folder)
            unrelated = root / folder / "14:09:16 - 15.11.2026- 135.375000 MHz.mp3"
            unrelated.write_bytes(b"unmanaged audio")

            self.assertEqual(concurrent_clip.name, "14:09:15 - 15.11.2026- 135.375000 MHz-2.mp3")
            self.assertTrue(recorder.has_saved_hit(concurrent_clip.name, frequency, v10=True, channel_folder=folder))
            self.assertFalse(recorder.has_saved_hit(concurrent_clip.name, frequency, v10=True))
            self.assertFalse(recorder.has_saved_hit(str(concurrent_clip), frequency, v10=True))
            self.assertEqual(recorder.clear_frequency(frequency, v10=True, channel_folder=folder), 2)
            self.assertFalse(channel_clip.exists())
            self.assertFalse(concurrent_clip.exists())
            self.assertTrue(other_clip.is_file())
            self.assertTrue(unrelated.is_file())
            self.assertTrue(vfo_clip.is_file())
            self.assertTrue(previous_clip.is_file())
            self.assertEqual(recorder.clear_frequency(frequency, v10=True), 2)

    def test_saved_channel_folders_cannot_escape_the_recording_directory(self):
        self.assertIsNone(saved_channel_folder(2, "  "))
        self.assertEqual(saved_channel_folder(2, ".. / London/Info:\\"), "CH2 - London Info")
        self.assertTrue(is_saved_channel_folder(saved_channel_folder(2, "é" * 200)))
        self.assertFalse(is_saved_channel_folder("CH2 - ../Outside"))
        with (TemporaryDirectory() as directory, TemporaryDirectory() as other_directory,
              patch("recording._Encoder", TestEncoder)):
            root = Path(directory)
            outside = Path(other_directory)
            folder = saved_channel_folder(2, "London Info")
            outside_clip = outside / "14:09:15 - 15.11.2026- 135.375000 MHz.mp3"
            outside_clip.write_bytes(b"do not replace")
            (root / folder).symlink_to(outside, target_is_directory=True)
            recorder = Recorder(root)
            with self.assertRaises(RecordingError):
                recorder.save_received(135_375_000, 0, [b"\x01\x00"], v10=True, channel_folder=folder)
            with self.assertRaises(RecordingError):
                recorder.clear_frequency(135_375_000, v10=True, channel_folder=folder)
            self.assertEqual(outside_clip.read_bytes(), b"do not replace")


if __name__ == "__main__":
    unittest.main()

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app import RecordingTarget, ScannerRuntime
from recording import MIN_CLIP_PCM_BYTES, Recorder


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
            self.assertEqual([file.name for file in files], ["14:09:15 - 15112026 - 443.00625.mp3"])
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


if __name__ == "__main__":
    unittest.main()

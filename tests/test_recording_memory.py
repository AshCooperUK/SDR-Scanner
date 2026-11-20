from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from app import ScannerRuntime
from receiver import Receiver


class RecordingMemoryTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = ScannerRuntime(Path(directory.name) / "state.json")
        self.addCleanup(self.runtime.close)
        self.runtime.act({"knob": "volume", "value": .5, "panel": "v10"})
        self.channel = (135_375_000, "AM")

    def recall(self, name, *, group=1, slot=2):
        self.runtime.act({"v10": "memory", "operation": "write", "group": group, "slot": slot,
                          "hz": self.channel[0], "mode": self.channel[1], "name": name})
        self.runtime.act({"v10": "memory", "operation": "recall", "group": group, "slot": slot})

    def test_unnamed_memories_use_distinct_folders_in_each_bank(self):
        self.recall("", group=0, slot=9)
        self.assertEqual(self.runtime._recording_target(self.channel).channel_folder, "CH9 - Bank 0")
        self.recall("", group=1, slot=9)
        self.assertEqual(self.runtime._recording_target(self.channel).channel_folder, "CH9 - Bank 1")

    def test_manual_tuning_to_saved_frequency_uses_the_recording_root(self):
        self.recall("London Info")
        self.runtime.act({"v10": "tune", "hz": self.channel[0], "mode": self.channel[1]})
        self.assertIsNone(self.runtime._recording_target(self.channel, station_name="RDS name").channel_folder)

    def test_receive_and_history_use_the_selected_memory_name(self):
        self.recall("London Info")
        receiver = Mock(spec=Receiver)
        receiver.tuning_offset_hz = None
        receiver.tuning_snr_db = 18
        receiver.waterfall = receiver.rds_status.return_value = receiver.tone_status.return_value = None
        receiver.level_live_audio.side_effect = lambda pcm: pcm
        pcm = bytes(640)

        def read():
            self.runtime.stopped.set()
            return -25.0, pcm

        receiver.read.side_effect = read
        with patch("app.Receiver", return_value=receiver), patch.object(self.runtime.recording, "receive") as record:
            self.runtime.run()
        tuned, received = record.call_args.args
        self.assertEqual(received, pcm)
        self.assertEqual(tuned.channel_folder, "CH2 - London Info")
        hit = self.runtime.discovery.hit(self.runtime.discovery.status()["hits"][0]["id"])
        self.assertEqual(hit.channel_folder, "CH2 - London Info")

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from app import ScannerRuntime
from receiver import NearbyPeak, Receiver
from v10 import NEARBY_COMPARE_SECONDS


class SquelchTests(unittest.TestCase):
    def runtime(self, mode="FM"):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        runtime = ScannerRuntime(Path(folder.name) / "state.json")
        self.addCleanup(runtime.close)
        runtime.act({"knob": "volume", "value": .5, "panel": "v10"})
        runtime.act({"v10": "tune", "hz": 446_006_250, "mode": mode})
        return runtime

    def receive(self, readings, *, squelch=.46, monitor=False, runtime=None):
        runtime = runtime or self.runtime()
        runtime.act({"knob": "squelch", "value": squelch})
        if monitor:
            runtime.act({"v10": "monitor", "enabled": True})
        receiver = Mock(spec=Receiver)
        receiver.tuning_offset_hz = None
        receiver.waterfall = None
        receiver.rds_status.return_value = None
        receiver.tone_status.return_value = None
        receiver.level_live_audio.side_effect = lambda pcm: pcm
        previous_gates = []
        frames = iter(readings)
        pcm = bytes(640)

        def read():
            previous_gates.append(runtime.scanner.audio_gate_open)
            level, receiver.tuning_snr_db = next(frames)
            snr = receiver.tuning_snr_db
            receiver.tuning_noise_dbfs = level - snr if snr is not None else None
            if len(previous_gates) == len(readings):
                runtime.stopped.set()
            return level, pcm

        receiver.read.side_effect = read
        with patch("app.Receiver", return_value=receiver), patch.object(runtime, "publish") as publish:
            runtime.run()
        return previous_gates[1:] + [runtime.scanner.audio_gate_open], publish.call_count

    def test_fm_noise_stays_closed_near_the_noise_floor(self):
        readings = [(-51.2, -0.2), (-51.2, 12.0), (-66.0, 12.0), (-51.2, 0.3)]
        self.assertEqual(self.receive(readings), ([False, True, False, False], 1))

    def test_low_manual_sql_passes_hiss_without_recording_it(self):
        runtime = self.runtime()
        runtime.act({"record": "toggle"})
        readings = [(-47.3, 1.1), (-42.1, 8.0), (-47.3, 1.1)]
        with patch.object(runtime.recording, "receive") as record:
            self.assertEqual(self.receive(readings, squelch=.24, runtime=runtime), ([True, True, True], 3))
        captured = [args[1] for args, _ in record.call_args_list]
        self.assertEqual([bool(pcm) for pcm in captured], [False, True, False])
        self.assertEqual(len(runtime.discovery.status()["hits"]), 1)

    def test_low_sql_does_not_make_fm_scan_stop_on_hiss(self):
        runtime = self.runtime()
        runtime.act({"v10": "memory", "operation": "write", "group": 1, "slot": 9,
                     "hz": 446_006_250, "mode": "FM", "name": ""})
        runtime.act({"v10": "scan", "kind": "ZONE", "enabled": True, "zones": ["FAV"]})
        self.assertEqual(self.receive([(-47.3, 1.1)], squelch=.24, runtime=runtime), ([False], 0))
        self.assertFalse(runtime.v10.paused_at)

    def test_low_sql_respects_a_saved_tone(self):
        runtime = self.runtime()
        runtime.scanner.v10_tone = {"kind": "CTCSS", "value": 123.0}
        self.assertEqual(self.receive([(-47.3, 1.1)], squelch=.24, runtime=runtime), ([False], 0))

    def test_monitor_and_fully_open_squelch_pass_noise(self):
        noise = [(-51.2, -0.2)]
        self.assertEqual(self.receive(noise, monitor=True), ([True], 1))
        self.assertEqual(self.receive(noise, squelch=0), ([True], 1))

    def test_unknown_fm_signal_quality_falls_back_to_channel_power(self):
        self.assertEqual(self.receive([(-51.2, None)]), ([True], 1))

    def test_squelch_adjustment_cancels_nearby_results_measured_at_old_threshold(self):
        runtime = self.runtime()
        panel = runtime.v10
        current = (446_006_250, "FM")
        start = 100
        panel.enable_nearby(current, start)
        pending, _, band, *_ = panel.next_nearby_probe(start, current)
        peak = NearbyPeak(446_018_750, -45, -60)
        panel.finish_nearby_probe(pending, peak, band.modulation, start, current=current, listening=False)
        live, *_ = panel.next_nearby_live_probe(start, current)

        runtime.act({"knob": "squelch", "value": .6})
        after = start + NEARBY_COMPARE_SECONDS
        self.assertIsNone(panel.take_nearby_candidate(after, current))
        self.assertIsNone(panel.finish_nearby_probe(live, peak, band.modulation, after, current=current,
                                                   listening=False, live=True))


if __name__ == "__main__":
    unittest.main()

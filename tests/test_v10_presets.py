from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import call, patch

import numpy as np

from app import ScannerRuntime, valid_action
from bandplan import AIR_STEP, factory_banks
from receiver import IQ_RATE, IQ_SAMPLES, Receiver, ReceiverError
from scanner import CHIRP_ZONE_LIMIT
from v10 import NEARBY_LIMIT, NEARBY_MIN_SECONDS, NEARBY_PMR, NEARBY_RESUME_SECONDS


class NearbyTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runtime = ScannerRuntime(Path(folder.name) / "state.json")
        self.addCleanup(self.runtime.close)
        self.panel = self.runtime.v10

    def observe(self, channel, start, *, name=None):
        self.panel.observe_nearby(channel, 1, start)
        self.panel.observe_nearby(channel, 1, start + .1, name=name)

    def arm(self, enabled=True):
        action = {"v10": "nearby", "enabled": enabled}
        self.assertTrue(valid_action(action))
        state = self.runtime.act(action)
        self.assertIsNone(state["v10_error"])
        return state

    def advance(self, now, receiving):
        with self.runtime.lock:
            self.runtime._v10_advance(now, receiving)

    def sweep(self, now, find):
        class Probe:
            def nearby_peak(self, *settings, **options):
                return find(*settings, **options)

        with patch("app.time.monotonic", return_value=now):
            return self.runtime._v10_sweep_nearby(Probe())

    def test_observations_need_stable_reception(self):
        channel = (102_400_000, "WFM")
        self.observe(channel, 0)
        self.assertEqual(self.panel.zone_entries("NEARBY"), [])
        self.runtime.act({"knob": "volume", "value": .5, "panel": "v10"})
        self.panel.observe_nearby(channel, 1, 1)
        self.panel.observe_nearby(channel, 1, 1 + NEARBY_MIN_SECONDS / 2)
        self.panel.observe_nearby(None, 1, 1.1)
        self.panel.observe_nearby(channel, 1, 1.12)
        self.panel.observe_nearby(channel, 2, 1.21)
        self.panel.observe_nearby(channel, 2, 2)
        self.assertEqual(self.panel.zone_entries("NEARBY"), [])
        self.panel.observe_nearby(channel, 2, 2.11, name="Heart")
        self.assertEqual(self.panel.zone_entries("NEARBY")[0]["name"], "Heart")

    def test_recall_history_is_read_only_and_bounded(self):
        self.runtime.act({"knob": "volume", "value": .5, "panel": "v10"})
        first = (102_400_000, "WFM")
        newest = (446_003_125, "dPMR")
        self.observe(first, 1, name="Heart")
        initial = self.panel.zone_entries("NEARBY")[0]
        self.observe(newest, 2)
        self.assertEqual(len(self.panel.zone_entries("NEARBY")), 2)
        recall = {"v10": "zone", "zone": "NEARBY", "operation": "recall", "index": initial["index"]}
        self.assertTrue(valid_action(recall))
        self.assertFalse(valid_action({**recall, "operation": "delete"}))
        self.assertFalse(valid_action({**recall, "operation": "write", "hz": first[0], "mode": first[1]}))
        state = self.runtime.act(recall)
        self.assertEqual((state["tune_hz"], state["modulation"]), first)
        state = self.runtime.act({"v10": "scan", "kind": "ZONE", "zone": "NEARBY", "enabled": True})
        self.assertIsNone(state["v10_error"])
        self.assertTrue(state["v10"]["nearby_enabled"])
        self.assertTrue(state["v10"]["nearby_sweeping"])
        self.assertIsNone(state["v10"]["scan_kind"])
        self.assertEqual((state["tune_hz"], state["modulation"]), first)
        for offset in range(NEARBY_LIMIT):
            self.observe((430_000_000 + offset * 12_500, "FM"), 3 + offset)
        self.assertEqual(len(self.panel.zone_entries("NEARBY")), NEARBY_LIMIT)
        self.assertIsNone(self.panel.zone_entry("NEARBY", initial["index"]))
        self.assertNotIn("NEARBY", self.panel.backup()["zones"])

    def test_nearby_interrupts_audible_fm_preset_and_resumes_the_same_station(self):
        radio = self.runtime
        radio.act({"knob": "volume", "value": .5, "panel": "v10"})
        foreground = radio.act({"v10": "scan", "kind": "ZONE", "zones": ["BCAST"], "enabled": True})
        station = foreground["tune_hz"]
        radio.scanner.tick(-25)
        self.assertTrue(radio.state()["audio_open"])
        state = self.arm()
        self.assertIsNone(state["v10_error"])
        self.assertTrue(state["v10"]["nearby_enabled"])
        self.assertTrue(state["v10"]["nearby_sweeping"])
        self.assertFalse(state["v10"]["nearby_receiving"])
        self.assertEqual(state["v10"]["scan_zones"], ["BCAST"])
        self.assertEqual((state["tune_hz"], state["modulation"]), (station, "WFM"))
        self.assertTrue(state["audio_open"])
        self.assertEqual(self.panel.zone_entries("NEARBY"), [])
        inspected = []

        def find_pmr(center, band, lower, upper, exact, blocked, squelch, **options):
            inspected.append(band.label)
            return 446_006_250 if exact and len(inspected) > 1 else None

        start = time.monotonic() + 1
        for step in range(5):
            when = start + 2 * step
            self.advance(when, True)
            self.sweep(when, find_pmr)
            if step < 4:
                self.assertEqual(radio.state()["tune_hz"], station)
                self.assertTrue(radio.state()["audio_open"])

        state = radio.state()
        self.assertEqual(inspected, ["PMR446", "VHF", "UHF", "AIR", "PMR446"])
        self.assertEqual((state["tune_hz"], state["modulation"]), (446_006_250, "FM"))
        self.assertFalse(state["v10"]["nearby_sweeping"])
        self.assertTrue(state["v10"]["nearby_receiving"])
        self.assertTrue(state["v10"]["auto_mode"])
        self.assertTrue(radio.lcd_display()["v10"]["auto_mode"])
        self.assertEqual(state["v10"]["scan_zones"], ["BCAST"])
        self.advance(start + 8.01, True)
        self.observe((446_006_250, "FM"), start + 8.01)
        self.assertEqual(self.panel.zone_entries("NEARBY")[0]["hz"], 446_006_250)
        self.advance(start + 10, False)
        self.advance(start + 10 + NEARBY_RESUME_SECONDS + .01, False)
        state = radio.state()
        self.assertEqual((state["tune_hz"], state["modulation"]), (station, "WFM"))
        self.assertEqual(state["v10"]["scan_zone"], "BCAST")
        self.assertTrue(state["v10"]["nearby_sweeping"])
        self.assertFalse(state["v10"]["nearby_receiving"])

    def test_confirmed_nearby_hit_waits_seven_seconds_after_squelch_closes(self):
        radio = self.runtime
        radio.act({"knob": "volume", "value": .5, "panel": "v10"})
        radio.act({"v10": "setting", "key": "resume_time", "value": 0})
        radio.act({"v10": "tune", "hz": 128_125_000, "mode": "AM"})
        self.arm()
        start = time.monotonic() + 1
        self.sweep(start, lambda *args, **options: 446_006_250)
        self.advance(start + .01, True)
        self.advance(start + 30, True)
        self.assertTrue(radio.state()["v10"]["nearby_receiving"])
        self.advance(start + 31, False)
        self.advance(start + 37.9, False)
        self.assertEqual(radio.state()["tune_hz"], 446_006_250)
        self.advance(start + 37.95, True)  # A reply starts the hang time again.
        self.advance(start + 38, False)
        self.advance(start + 44.9, False)
        self.assertEqual(radio.state()["tune_hz"], 446_006_250)
        self.advance(start + 45.01, False)
        state = radio.state()
        self.assertIsNone(state["v10"]["scan_kind"])
        self.assertTrue(state["v10"]["nearby_enabled"])
        self.assertEqual((state["tune_hz"], state["modulation"]), (128_125_000, "AM"))
        self.assertFalse(state["v10"]["auto_mode"])
        self.assertFalse(radio.lcd_display()["v10"]["auto_mode"])

    def test_false_or_locked_hit_does_not_interrupt_foreground_for_the_full_hang_time(self):
        radio = self.runtime
        radio.act({"knob": "volume", "value": .5, "panel": "v10"})
        pmr = 446_006_250
        radio.act({"v10": "tune", "hz": pmr, "mode": "FM"})
        radio.act({"v10": "lockout", "operation": "toggle"})
        radio.act({"v10": "tune", "hz": 98_400_000, "mode": "WFM"})
        self.arm()
        start = time.monotonic() + 1

        def locked_hit(center, band, lower, upper, exact, blocked, squelch, **options):
            self.assertIn(pmr, blocked)
            return pmr  # A late lockout must also be checked on acceptance.

        self.sweep(start, locked_hit)
        self.assertEqual(radio.state()["tune_hz"], 98_400_000)
        self.assertFalse(radio.state()["v10"]["nearby_receiving"])
        for index in range(4):
            self.sweep(start + 1 + index, lambda center, band, *args, **options:
                       pmr + 12_500 if band.label == "PMR446" else None)
        self.assertTrue(radio.state()["v10"]["nearby_receiving"])
        self.advance(start + 4.01, False)
        self.advance(start + 5, False)
        self.assertFalse(radio.state()["v10"]["nearby_receiving"])
        self.assertEqual(radio.state()["tune_hz"], 98_400_000)

    def test_holding_a_nearby_hit_can_pause_on_it_and_then_restore_manual_tuning(self):
        radio = self.runtime
        radio.act({"knob": "volume", "value": .5, "panel": "v10"})
        radio.act({"v10": "tune", "hz": 98_400_000, "mode": "WFM"})
        self.arm()
        start = time.monotonic() + 1
        self.sweep(start, lambda *args, **options: 446_006_250)
        self.advance(start + .01, True)
        with patch("app.time.monotonic", return_value=start + .02):
            state = radio.act({"v10": "scan_control", "operation": "hold"})
        self.assertIsNone(state["v10_error"])
        self.assertTrue(state["v10"]["nearby_held"])
        self.advance(start + 20, False)
        self.advance(start + 30, False)
        self.assertEqual(radio.state()["tune_hz"], 446_006_250)
        with patch("app.time.monotonic", return_value=start + 31):
            state = radio.act({"v10": "scan_control", "operation": "resume"})
        self.assertIsNone(state["v10_error"])
        self.assertEqual((state["tune_hz"], state["modulation"]), (98_400_000, "WFM"))
        self.assertTrue(state["v10"]["nearby_enabled"])
        self.assertFalse(state["v10"]["nearby_held"])

    def test_nearby_keeps_aviation_am_and_allows_signal_detection_elsewhere(self):
        radio = self.runtime
        radio.act({"knob": "volume", "value": .5, "panel": "v10"})
        radio.act({"v10": "tune", "hz": 98_400_000, "mode": "WFM"})
        self.arm()
        start = time.monotonic() + 1
        for index in range(4):
            self.sweep(start + index * 2, lambda center, band, *args, **options:
                       118_125_000 if band.label == "AIR" else None)
        self.assertEqual((radio.state()["tune_hz"], radio.state()["modulation"]), (118_125_000, "AM"))
        self.assertFalse(self.panel.nearby_auto_detect(118_125_000))
        self.assertEqual(self.arm(False)["tune_hz"], 98_400_000)
        self.arm()
        for index in range(2):
            self.sweep(start + 10 + index * 2, lambda center, band, *args, **options:
                       144_500_000 if band.label == "VHF" else None)
        self.assertTrue(self.panel.nearby_auto_detect(144_500_000))
        self.assertFalse(self.panel.nearby_auto_detect(144_512_500))
        with radio.lock:
            self.panel.classify_nearby("AM")
        self.assertFalse(self.arm(False)["v10"]["auto_mode"])

    def test_inspection_during_an_interrupt_resumes_underlying_preset_scan(self):
        radio = self.runtime
        radio.act({"knob": "volume", "value": .5, "panel": "v10"})
        original = radio.act({"v10": "scan", "kind": "ZONE", "zones": ["AIR"], "enabled": True})
        self.arm()
        self.sweep(time.monotonic() + 1, lambda *args, **options: 446_006_250)
        self.assertTrue(radio.state()["v10"]["nearby_receiving"])
        state = radio.act({"v10": "scan_control", "operation": "inspect", "hz": 128_125_000, "mode": "AM"})
        self.assertTrue(state["v10"]["scan_held"])
        self.assertTrue(state["v10"]["nearby_enabled"])
        self.assertFalse(state["v10"]["nearby_receiving"])
        self.assertEqual(state["tune_hz"], 128_125_000)
        state = radio.act({"v10": "scan_control", "operation": "resume"})
        self.assertEqual(state["tune_hz"], original["tune_hz"])
        self.assertFalse(state["v10"]["auto_mode"])

    def test_controls_reject_a_probe_that_started_before_the_action(self):
        radio = self.runtime
        radio.act({"knob": "volume", "value": .5, "panel": "v10"})
        radio.act({"v10": "tune", "hz": 98_400_000, "mode": "WFM"})
        self.arm()
        start = time.monotonic() + 1

        controls = []

        def retune(*settings, **options):
            controls.append("retune")
            radio.act({"v10": "tune", "hz": 145_500_000, "mode": "FM"})
            return 446_006_250

        self.sweep(start, retune)
        self.assertTrue(radio.state()["v10"]["nearby_enabled"])
        self.assertFalse(radio.state()["v10"]["nearby_receiving"])
        self.assertEqual(radio.state()["tune_hz"], 145_500_000)

        def disarm(*settings, **options):
            controls.append("disarm")
            self.arm(False)
            return 446_006_250

        self.sweep(start + 2, disarm)
        self.assertFalse(radio.state()["v10"]["nearby_enabled"])
        self.assertFalse(radio.state()["v10"]["nearby_receiving"])
        self.assertEqual(radio.state()["tune_hz"], 145_500_000)
        self.assertEqual(controls, ["retune", "disarm"])

    def test_fft_detects_the_exact_pmr_channel_and_obeys_squelch(self):
        band = NEARBY_PMR
        receiver = Receiver()
        center = (band.lower + band.upper) // 2 + 180_000
        time = np.arange(IQ_SAMPLES) / IQ_RATE
        iq = .08 * np.exp(2j * np.pi * (band.lower - center + 400) * time)
        exact = tuple(range(band.lower, band.upper + 1, band.step))
        def seek(squelch):
            return receiver._nearby_channel(iq, center, band, band.lower, band.upper, exact, set(), squelch)
        self.assertEqual(seek(-48), 446_006_250)
        self.assertIsNone(seek(-20))

    def test_primary_sdr_restores_the_listener_after_an_out_of_band_probe(self):
        receiver = Receiver()
        listener_center = 98_640_000
        listener_channel = (98_400_000, "WFM")
        receiver._center = listener_center
        receiver._tuning = listener_channel
        receiver._gain = 280
        band = NEARBY_PMR
        probe_center = (band.lower + band.upper) // 2 + 180_000
        arguments = (probe_center, band, band.lower, band.upper, (band.lower,), set(), -48)
        with patch.object(receiver, "_set_center") as tune, patch.object(receiver, "_read_iq") as read:
            read.return_value = (1, bytes(2 * IQ_SAMPLES))
            with patch.object(receiver, "_nearby_channel", return_value=None):
                self.assertIsNone(receiver.nearby_peak(*arguments, preserve=True))
            self.assertEqual(tune.call_args_list, [call(probe_center, False),
                                                   call(listener_center, False, restore_gain=280)])
            tune.reset_mock()
            read.side_effect = ReceiverError("Probe failed")
            with self.assertRaises(ReceiverError):
                receiver.nearby_peak(*arguments, preserve=True)
            self.assertEqual(tune.call_args_list, [call(probe_center, False),
                                                   call(listener_center, False, restore_gain=280)])
        self.assertEqual(receiver._tuning, listener_channel)

    def test_primary_sdr_reuses_recent_iq_when_the_nearby_band_is_in_view(self):
        receiver = Receiver()
        band = NEARBY_PMR
        receiver._center = band.lower + 300_000
        receiver._tuning = (band.lower + 20_000, "FM")
        offsets = np.arange(IQ_SAMPLES) / IQ_RATE
        receiver._watch_iq = .08 * np.exp(2j * np.pi * (band.lower - receiver._center) * offsets)
        with patch.object(receiver, "_set_center") as tune, patch.object(receiver, "_read_iq") as read:
            hit = receiver.nearby_peak(band.lower + 400_000, band, band.lower, band.upper,
                                       (band.lower,), set(), -48, preserve=True)
            self.assertEqual(hit, band.lower)
            tune.assert_not_called()
            read.assert_not_called()


class PresetCoverageTests(unittest.TestCase):
    def test_exact_ofcom_carriers_and_supplement_ids(self):
        factory = factory_banks(AIR_STEP)
        for bank, frequencies in {
            "BUS": (456_340_625, 461_840_625),
            "MRN": (162_050_000, 162_950_000, 163_000_000),
            "EVT": (470_018_750, 470_993_750, 474_018_750, 474_993_750),
        }.items():
            self.assertTrue(set(frequencies) <= {channel.hz for channel in factory[bank]})
        self.assertNotIn(156_525_000, {channel.hz for channel in factory["MRN"]})

        with TemporaryDirectory() as folder:
            runtime = ScannerRuntime(Path(folder) / "state.json")
            self.addCleanup(runtime.close)
            panel = runtime.v10
            self.assertEqual(panel.zone_entry("PMR", 0)["mode"], "FM")
            self.assertEqual(panel.zone_entry("CB", 0)["mode"], "AM")
            self.assertEqual(panel.zone_entry("PMR", CHIRP_ZONE_LIMIT)["mode"], "DMR")
            self.assertEqual(panel.zone_entry("PMR", CHIRP_ZONE_LIMIT + 16)["hz"], 446_003_125)
            self.assertEqual(panel.zone_entry("PMR", CHIRP_ZONE_LIMIT + 47)["hz"], 446_196_875)
            self.assertEqual({entry["mode"] for entry in panel.zone_entries("CB")} - {"AM", "FM"}, {"USB", "LSB"})


class BroadcastPresetTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runtime = ScannerRuntime(Path(folder.name) / "state.json")
        self.addCleanup(self.runtime.close)
        self.runtime.act({"knob": "volume", "value": .5, "panel": "v10"})
        self.panel = self.runtime.v10

    def test_fm_scan_follows_full_band_order_without_reassigning_preset_ids(self):
        panel = self.panel
        original = panel.zone_entries("BCAST")
        self.assertEqual([entry["hz"] for entry in original],
                         [channel.hz for channel in factory_banks(AIR_STEP)["BCAST"]])
        grid = list(range(87_500_000, 108_000_000, 100_000))
        action = {"kind": "ZONE", "zones": ["BCAST"]}
        with self.runtime.lock:
            for direction, starting_hz, expected_first in [
                (1, 107_600_000, 107_700_000),
                (-1, 87_700_000, 87_600_000),
                (1, 107_900_000, 87_500_000),
                (-1, 87_500_000, 107_900_000),
                (1, 107_650_000, 107_700_000),
                (-1, 107_650_000, 107_600_000),
                (1, 446_006_250, 87_500_000),
                (-1, 446_006_250, 107_900_000),
            ]:
                with self.subTest(direction=direction, starting_hz=starting_hz):
                    panel.stop_scan()
                    panel.scan_direction = direction
                    receiver_now = 100.0
                    first = panel.start_scan(action, (starting_hz, "WFM"), receiver_now)
                    self.assertEqual(first["hz"], expected_first)
                    ordered = grid if direction > 0 else grid[::-1]
                    offset = ordered.index(expected_first)
                    seen = [first["hz"]]
                    for index in range(1, len(grid)):
                        seen.append(panel.next_scan(receiver_now + index)["hz"])
                    self.assertEqual(seen, ordered[offset:] + ordered[:offset])
        self.assertEqual(panel.zone_entries("BCAST"), original)

    def test_broadcast_modes_settle_at_max_speed_and_hold_centred_carriers(self):
        for slot, hz in enumerate((101_800_000, 103_700_000)):
            self.runtime.act({"v10": "memory", "operation": "write", "group": 0,
                              "slot": slot, "hz": hz, "mode": "WFM"})
        panel = self.panel
        with self.runtime.lock:
            panel.set_value("scan_speed", 30, "WFM")
            for zone in ("BCAST", "FAV"):
                with self.subTest(zone=zone):
                    panel.stop_scan()
                    now = 100.0
                    entry = panel.start_scan({"kind": "ZONE", "zones": [zone]}, (101_800_000, "WFM"), now)
                    self.assertEqual(entry["mode"], "WFM")
                    point = panel.scan["current_point"]
                    self.assertIsNone(panel.scan_tick(now, False))
                    self.assertIsNone(panel.scan_tick(now + .07, False))
                    self.assertEqual(panel.scan["current_point"], point)
                    self.assertIsNotNone(panel.scan_tick(now + .13, False))
                    self.assertNotEqual(panel.scan["current_point"], point)
                    point = panel.scan["current_point"]
                    self.assertIsNone(panel.scan_tick(now + .14, False))
                    self.assertIsNone(panel.scan_tick(now + .22, True))
                    self.assertIsNone(panel.scan_tick(now + 60, True))
                    self.assertEqual(panel.scan["current_point"], point)
                    self.assertFalse(panel.closed_at)

    def test_wfm_scan_qualification_uses_measured_offset_and_signal_excess(self):
        from app import wfm_scan_channel

        self.assertTrue(wfm_scan_channel(None, None))
        self.assertTrue(wfm_scan_channel(None, 4.9))
        self.assertFalse(wfm_scan_channel(None, 5.0))
        for offset in (-39_999.0, 0.0, 39_999.0):
            self.assertTrue(wfm_scan_channel(offset, 15.0))
        for offset in (-100_000.0, -40_000.0, 40_000.0, 100_000.0):
            self.assertFalse(wfm_scan_channel(offset, 15.0))

    def test_synthetic_wfm_signal_requires_offset_and_rejects_adjacent_station(self):
        from app import usable_signal_level, wfm_scan_channel

        receiver = Receiver()
        receiver._center = 107_600_000
        receiver._tuning = (107_900_000, "WFM")
        receiver._prepared = (*receiver._tuning, receiver._center)
        for carrier, is_centred in ((107_900_000, True), (107_800_000, False)):
            with self.subTest(carrier=carrier):
                receiver._reset_tuning_measurement()
                receiver._decoder.reset(300_000, "WFM")
                random = np.random.default_rng(341)
                qualified = []
                raw_levels = []
                for block in range(10):
                    samples = np.arange(block * IQ_SAMPLES, (block + 1) * IQ_SAMPLES) / IQ_RATE
                    phase = 2 * np.pi * (carrier - receiver._center) * samples - 35 * np.cos(2 * np.pi * 1000 * samples)
                    noise = .003 * (random.normal(size=IQ_SAMPLES) + 1j * random.normal(size=IQ_SAMPLES))
                    receiver._watch_iq = (.1 * np.exp(1j * phase) + noise).astype(np.complex64)
                    level, _ = receiver._decoder.decode(receiver._watch_iq)
                    receiver._measure_tuning(level)
                    offset, snr = receiver.tuning_offset_hz, receiver.tuning_snr_db
                    power = usable_signal_level(level, "WFM", offset, snr)
                    raw_levels.append(level)
                    qualified.append(wfm_scan_channel(offset, snr) and power >= -44)
                self.assertTrue(all(level > -44 for level in raw_levels))
                self.assertEqual(qualified[:5], [False] * 5)
                self.assertEqual(qualified[5:], [is_centred] * 5)


class FavoritePresetTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runtime = ScannerRuntime(Path(folder.name) / "state.json")
        self.addCleanup(self.runtime.close)
        self.runtime.act({"knob": "volume", "value": .5, "panel": "v10"})
        self.panel = self.runtime.v10

    def act(self, action, **values):
        request = {"v10": action, **values}
        self.assertTrue(valid_action(request), request)
        state = self.runtime.act(request)
        self.assertIsNone(state["v10_error"])
        return state

    def save(self, group, slot, hz, mode="FM"):
        return self.act("memory", operation="write", group=group, slot=slot, hz=hz, mode=mode)

    def start(self):
        return self.act("scan", kind="ZONE", zones=["FAV"], enabled=True)

    def open_squelch(self):
        with self.runtime.lock:
            self.runtime.scanner.tick(self.runtime.scanner.squelch_db + 10)
            self.runtime._v10_advance(time.monotonic(), True)
        self.assertTrue(self.runtime.state()["audio_open"])

    def test_saved_favourite_joins_active_scan_without_interrupting_playback(self):
        self.save(0, 0, 446_006_250)
        self.save(1, 99, 430_400_000, "DMR")
        self.save(100, 0, 162_550_000)
        self.start()
        self.open_squelch()
        revision = self.runtime.scanner.tune_revision

        state = self.save(0, 70, 145_500_000)
        self.assertTrue(state["audio_open"])
        self.assertEqual(state["tune_hz"], 446_006_250)
        self.assertEqual((state["v10"]["group"], state["v10"]["slot"]), (0, 0))
        self.assertEqual(self.runtime.scanner.tune_revision, revision)
        self.assertEqual(self.panel.scan["points"], [("FAV", 0), ("FAV", 70), ("FAV", 199)])

        seen = [self.act("step", direction=1) for _ in range(3)]
        self.assertEqual([(state["tune_hz"], state["modulation"]) for state in seen],
                         [(145_500_000, "FM"), (430_400_000, "DMR"), (446_006_250, "FM")])
        self.assertEqual((seen[1]["v10"]["group"], seen[1]["v10"]["slot"]), (1, 99))

    def test_skipping_current_favourite_advances_even_with_open_squelch(self):
        self.save(0, 0, 446_006_250)
        self.save(1, 4, 145_500_000)
        self.start()
        self.open_squelch()

        state = self.act("memory", operation="skip", group=0, slot=0, skip=True)
        self.assertEqual(state["tune_hz"], 145_500_000)
        self.assertEqual((state["v10"]["group"], state["v10"]["slot"]), (1, 4))
        self.assertEqual(state["v10"]["scan_zones"], ["FAV"])
        self.assertFalse(state["audio_open"])
        self.assertEqual(self.act("step", direction=1)["tune_hz"], 145_500_000)

    def test_deleting_held_favourite_changes_destination_when_scan_resumes(self):
        self.save(0, 0, 446_006_250)
        self.save(1, 4, 145_500_000)
        self.start()
        state = self.act("scan_control", operation="hold")
        self.assertTrue(state["v10"]["scan_held"])

        state = self.act("memory", operation="delete", group=0, slot=0)
        self.assertEqual(state["tune_hz"], 446_006_250)
        self.assertTrue(state["v10"]["scan_held"])
        state = self.act("scan_control", operation="resume")
        self.assertEqual(state["tune_hz"], 145_500_000)
        self.assertFalse(state["v10"]["scan_held"])

        state = self.act("memory", operation="delete", group=1, slot=4)
        self.assertIsNone(state["v10"]["scan_kind"])
        self.assertEqual(self.panel.zone_entries("FAV"), [])

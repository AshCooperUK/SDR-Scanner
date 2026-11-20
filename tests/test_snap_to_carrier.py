"""Snap to Carrier uses the spectrum row the operator selected."""

from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import Mock

import numpy as np

from app import ScannerRuntime, valid_action
from receiver import IQ_RATE, PROBE_FFT, Receiver


CARRIER_HZ = 446_006_250
CENTER_HZ = CARRIER_HZ + 300_000


class SnapToCarrierTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runtime = ScannerRuntime(Path(folder.name) / "state.json")
        self.addCleanup(self.runtime.close)
        self.runtime.act({"knob": "volume", "value": .5, "panel": "v10"})
        self.runtime.receiver_status = "ready"
        self.receiver = Receiver()
        self.receiver._center = CENTER_HZ
        self.receiver._prepared = CARRIER_HZ, "FM", CENTER_HZ
        self.samples = np.arange(3 * PROBE_FFT) / IQ_RATE
        random = np.random.default_rng(573)
        self.noise = .012 * (random.normal(size=self.samples.size) + 1j * random.normal(size=self.samples.size))

    def signal(self, hz, level=.25, deviation_hz=0):
        phase = 2 * np.pi * (hz - CENTER_HZ) * self.samples
        phase += deviation_hz / 1_000 * np.sin(2 * np.pi * 1_000 * self.samples)
        return level * np.exp(1j * phase)

    def capture(self, samples, *, age=0):
        power = self.receiver._watch_spectrum(samples + self.noise)
        return self.capture_spectrum(power, age=age)

    def capture_spectrum(self, power, *, age=0):
        self.receiver._update_waterfall(power, monotonic() - age)
        hardware = Mock(spec=Receiver, scope_spectrum=self.receiver.scope_spectrum, tuner_gain=None)
        self.runtime._read_receiver(hardware)
        frame = self.receiver.waterfall
        return {"sequence": frame["sequence"], "time_ms": frame["time_ms"], "radius_hz": 5_625}

    def tune(self, hz, **fields):
        request = {"v10": "tune", "hz": hz, "mode": "FM", **fields}
        self.assertTrue(valid_action(request))
        return self.runtime.act(request)["tune_hz"]

    def test_recovers_modulated_narrow_fm_center_below_display_bin_width(self):
        snap = self.capture(self.signal(CARRIER_HZ, deviation_hz=2_500))
        self.assertAlmostEqual(self.tune(CARRIER_HZ - 3_500, snap=snap), CARRIER_HZ, delta=150)

    def test_interpolates_am_carrier_and_keeps_precise_ssb_tuning(self):
        hz = CARRIER_HZ + 131
        amplitude = 1.0 + .8 * np.sin(2 * np.pi * 3_000 * self.samples)
        snap = self.capture(self.signal(hz) * amplitude)
        self.assertAlmostEqual(self.tune(hz + 1_500, mode="AM", snap=snap), hz, delta=30)
        self.assertEqual(self.tune(hz + 1_500, mode="USB", snap=snap), hz + 1_500)

    def test_locates_the_center_of_a_wide_fm_channel(self):
        relative = self.receiver._probe_offsets + CENTER_HZ - CARRIER_HZ
        power = np.full(PROBE_FFT, 1e-10)
        power[np.abs(relative) < 84_000] = 1e-7
        snap = self.capture_spectrum(power)
        self.assertAlmostEqual(self.tune(CARRIER_HZ + 3_000, mode="WFM", snap=snap), CARRIER_HZ, delta=300)

    def test_snaps_to_nearest_carrier_and_rejects_ambiguous_midpoint(self):
        other_hz = CARRIER_HZ + 6_250
        snap = self.capture(self.signal(CARRIER_HZ, .45) + self.signal(other_hz, .12))
        self.assertAlmostEqual(self.tune(other_hz - 1_500, snap=snap), other_hz, delta=50)
        midpoint = (CARRIER_HZ + other_hz) // 2
        self.assertEqual(self.tune(midpoint, snap=snap), midpoint)

    def test_noise_dc_and_unmatched_or_stale_rows_keep_exact_frequency(self):
        snap = self.capture(self.signal(CENTER_HZ, .8))
        self.assertEqual(self.tune(CENTER_HZ + 500, snap=snap), CENTER_HZ + 500)
        self.assertEqual(self.tune(CARRIER_HZ + 500, snap=snap), CARRIER_HZ + 500)

        clicked = CARRIER_HZ + 3_000
        snap = self.capture(self.signal(CARRIER_HZ), age=4)
        self.assertEqual(self.tune(clicked, snap=snap), clicked)
        snap = self.capture(self.signal(CARRIER_HZ))
        self.assertEqual(self.tune(clicked, snap={**snap, "sequence": 500}), clicked)
        self.assertEqual(self.tune(clicked), clicked)

    def test_preset_inspection_snaps_without_ending_scan(self):
        self.runtime.act({"v10": "scan", "kind": "ZONE", "enabled": True, "zones": ["PMR"]})
        snap = self.capture(self.signal(CARRIER_HZ))
        request = {"v10": "scan_control", "operation": "inspect", "hz": CARRIER_HZ - 3_000, "snap": snap}
        self.assertTrue(valid_action(request))
        state = self.runtime.act(request)
        self.assertEqual(state["v10"]["scan_kind"], "ZONE")
        self.assertTrue(state["v10"]["scan_held"])
        self.assertAlmostEqual(state["tune_hz"], CARRIER_HZ, delta=25)

    def test_malformed_snap_is_rejected(self):
        snap = self.capture(self.signal(CARRIER_HZ))
        request = {"v10": "tune", "hz": CARRIER_HZ, "mode": "FM"}
        for invalid in (None, {**snap, "radius_hz": 25_000}, {**snap, "sequence": True}, {**snap, "center_hz": 0}):
            self.assertFalse(valid_action({**request, "snap": invalid}))

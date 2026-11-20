"""AUTO digital reception requires a clear native call and remains temporary."""

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

from app import AUTO_DIGITAL_DROP_SECONDS, ScannerRuntime
from digital_modes import AutoDigitalDetector, _verified_auto_mode


CALL = dict(slot=0, state=2, kind=2, src_id=1001, tg_id=9, enc=False, algid=0)
DMR = {"protocol": "DMR", "synctype": 12}
P25 = {"protocol": "P25p1", "synctype": 0}
NXDN = {"protocol": "NXDN", "synctype": 28}


class TestAutoDigitalMetadata(unittest.TestCase):
    def verify(self, status, *, nxdn48=False, **call):
        slots = [{**CALL, "protocol_label": status["protocol"], **call}]
        return _verified_auto_mode(status, {"slots": slots}, nxdn48=nxdn48)

    def test_dmr_requires_more_than_sync(self):
        self.assertEqual(self.verify(DMR), "DMR")
        self.assertIsNone(_verified_auto_mode(DMR, {}))
        self.assertIsNone(self.verify(DMR, enc=True))
        self.assertIsNone(self.verify(DMR, src_id=0))
        self.assertIsNone(self.verify({**DMR, "synctype": -1}))

    def test_p25_requires_phase_one_and_a_verified_nac(self):
        nac = dict(receive_known=True, receive_kind=3, receive_value=0x293)
        self.assertEqual(self.verify(P25, **nac), "P25")
        self.assertIsNone(self.verify(P25))
        self.assertIsNone(self.verify({"protocol": "P25p2", "synctype": 35}, **nac))

    def test_nxdn_needs_its_explicit_48_variant_confirmation(self):
        ran = dict(receive_known=True, receive_kind=2, receive_value=1)
        self.assertEqual(self.verify(NXDN, **ran), "NXDN")
        self.assertEqual(self.verify(NXDN, nxdn48=True, **ran), "NXDN-VN")
        self.assertIsNone(self.verify(NXDN, nxdn48=True))
        self.assertIsNone(self.verify(DMR, nxdn48=True))

    def test_different_tune_drops_late_result_from_old_native_probe(self):
        received = Event()
        release = Event()
        received_new = Event()

        class Probe:
            def __init__(self, executable, *, nxdn48=False):
                pass

            def process(self, pcm):
                if pcm == b"old":
                    received.set()
                    release.wait(timeout=1)
                    return "DMR"
                received_new.set()
                return None

            def close(self):
                pass

        detector = AutoDigitalDetector()
        self.addCleanup(detector.close)
        with patch("digital_modes.decoder_path", return_value="native"), patch("digital_modes._AutoDigitalProbe", Probe):
            self.assertIsNone(detector.observe((1, "FM"), b"old"))
            self.assertTrue(received.wait(timeout=1))
            self.assertIsNone(detector.observe((2, "FM"), b"new"))
            release.set()
            self.assertTrue(received_new.wait(timeout=1))
            self.assertIsNone(detector.observe((2, "FM"), b""))
            detector.reset()


class TestAutoDigitalRuntime(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.radio = ScannerRuntime(Path(folder.name) / "state.json")
        self.addCleanup(self.radio.close)
        self.radio.act({"knob": "volume", "value": .5, "panel": "v10"})
        self.radio.receiver_status = "ready"
        self.hz = 446_006_250
        self.tune(self.hz, "AUTO")

    def tune(self, hz, mode):
        return self.radio.act({"v10": "tune", "hz": hz, "mode": mode})

    def expired(self, wanted, now, **options):
        return self.radio._v10_auto_voice_expired(wanted, self.radio.dmr.revision, self.radio._dab_epoch, now, **options)

    def test_digital_reception_and_drop_leave_auto_selected(self):
        now = monotonic()
        self.assertTrue(self.radio._v10_select_auto_voice(self.hz, "FM", "DMR", now))
        state = self.radio.state()
        self.assertEqual((state["tune_hz"], state["modulation"], state["v10"]["auto_mode"]), (self.hz, "DMR", True))
        self.assertEqual(self.radio.v10.vfo, (self.hz, "AUTO"))
        self.assertFalse(self.expired((self.hz, "DMR"), now + .4, active=True))
        self.assertFalse(self.expired((self.hz, "DMR"), now + AUTO_DIGITAL_DROP_SECONDS))
        self.assertTrue(self.expired((self.hz, "DMR"), now + AUTO_DIGITAL_DROP_SECONDS + .5))
        state = self.radio.state()
        self.assertEqual((state["tune_hz"], state["modulation"], state["v10"]["auto_mode"]), (self.hz, "FM", True))

    def test_explicit_modes_broadcast_fm_and_airband_keep_their_modulation(self):
        now = monotonic()
        for hz, selection, modulation in ((self.hz, "FM", "FM"), (102_400_000, "AUTO", "WFM"),
                                            (128_125_000, "AUTO", "AM")):
            self.tune(hz, selection)
            self.assertFalse(self.radio._v10_select_auto_voice(hz, modulation, "DMR", now))
            self.assertEqual(self.radio.state()["modulation"], modulation)

    def test_decoder_failure_restores_analogue_and_backs_off(self):
        now = monotonic()
        self.assertTrue(self.radio._v10_select_auto_voice(self.hz, "FM", "P25", now))
        self.assertTrue(self.expired((self.hz, "P25"), now + .05, failed=True))
        self.assertEqual(self.radio.state()["modulation"], "FM")
        self.assertFalse(self.radio._v10_can_detect_digital(self.hz, "FM", now + 1))

    def test_simple_scan_keeps_direction_and_active_call_candidate(self):
        now = monotonic()
        result = self.radio.act({"v10": "scan", "kind": "SIMPLE", "enabled": True, "direction": -1})
        self.assertEqual(result["v10"]["scan_direction"], -1)
        self.assertTrue(self.radio._v10_select_auto_voice(self.hz, "FM", "DMR", now))
        self.radio._v10_advance(now + .1, False, digital_carrier=True)
        self.assertEqual(self.radio.state()["tune_hz"], self.hz)
        self.assertEqual(self.radio.state()["v10"]["scan_kind"], "SIMPLE")

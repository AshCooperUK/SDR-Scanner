from collections import deque
import unittest

import numpy as np

from digital_modes import DIGITAL_MODES, DigitalDecoder, _DIGITAL, _metadata


P25 = {"slot": 0, "state": 2, "kind": 2, "protocol_label": "P25p1", "src_id": 53, "tg_id": 83,
       "src_text": "53", "tg_text": "83", "receive_kind": 3, "receive_value": 0x293,
       "receive_known": True, "enc": False, "algid": 0x80, "elapsed_ms": 1680}
P25_STATUS = {"protocol": "P25p1", "synctype": 0}


class DigitalModeTests(unittest.TestCase):
    def read_p25(self, row=P25, code=None, status=P25_STATUS):
        metadata, unused_elapsed = _metadata("P25", _DIGITAL["P25"], status, {"slots": [row]}, code)
        return metadata

    def test_bound_receive_codes_require_verified_protocol_metadata(self):
        self.assertTrue(self.read_p25(code=0x293)["voice"])
        self.assertFalse(self.read_p25(code=0x294)["voice"])
        self.assertFalse(self.read_p25(dict(P25, receive_known=False), code=0x293)["voice"])
        self.assertFalse(self.read_p25(dict(P25, receive_kind=2), code=0x293)["voice"])
        self.assertFalse(self.read_p25(status={"protocol": "P25p2", "synctype": 35})["sync"])

    def test_identity_text_is_preserved_for_protocols_without_numeric_ids(self):
        dstar = {"slot": 0, "state": 2, "kind": 1, "protocol_label": "D-STAR", "src_text": "M7EXM",
                 "tg_text": "CQCQCQ", "enc": False, "algid": 0, "elapsed_ms": 1337}
        state, unused_elapsed = _metadata("D-STAR", _DIGITAL["D-STAR"], {"protocol": "DSTAR", "synctype": 6},
                                          {"slots": [dstar]}, None)
        self.assertEqual((state["source"], state["destination"], state["unit_id"]), ("M7EXM", "CQCQCQ", None))
        self.assertIs(state["encrypted"], False)

    def test_unsupported_modes_and_codes_fail_before_decoder_startup(self):
        self.assertNotIn("NXDN-N", DIGITAL_MODES)
        self.assertNotIn("DCR", DIGITAL_MODES)
        for mode, code in (("DCR", None), ("P25", 0xFFF), ("NXDN-VN", 64), ("dPMR", 1)):
            with self.subTest(mode=mode, code=code), self.assertRaises(ValueError):
                DigitalDecoder(mode, code=code)

    def test_audio_with_unknown_or_protected_crypto_never_leaves_the_bridge(self):
        decoder = DigitalDecoder.__new__(DigitalDecoder)
        decoder._output = bytearray(b"\xab\xcd" * 160)
        decoder._audio_identity = None
        decoder._elapsed = None
        decoder._pending_audio = deque()
        decoder._resample_state = np.zeros(48)
        clear = self.read_p25()
        self.assertEqual(decoder._play_clear_audio(clear, 1680, 1.0), b"")
        decoder._output.extend(b"\xab\xcd" * 160)
        self.assertEqual(decoder._play_clear_audio(clear, 1700, 1.1), b"")
        self.assertTrue(decoder._pending_audio)
        for row in (dict(P25, enc=True), dict(P25, enc=None), dict(P25, algid=0x84)):
            decoder._output.extend(b"\xab\xcd" * 160)
            self.assertEqual(decoder._play_clear_audio(self.read_p25(row), 1720, 1.4), b"")
            self.assertFalse(decoder._pending_audio)
            self.assertFalse(decoder._output)


if __name__ == "__main__":
    unittest.main()

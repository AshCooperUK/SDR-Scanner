from collections import deque
import unittest
from unittest.mock import patch

import numpy as np

from tier3 import DmrDecoderError, Tier3Decoder, _clear_group


CONTROL_HZ = 430_012_500
TRAFFIC_HZ = 430_037_500
CALL = {"state": 2, "protocol_label": "DMR", "kind": 2, "slot": 0, "src_id": 1001,
        "tg_id": 100, "enc": False, "algid": 0, "frequency_hz": TRAFFIC_HZ,
        "receive_known": True, "receive_kind": 1, "receive_value": 2, "elapsed_ms": 1000}


class Tier3BridgeTests(unittest.TestCase):
    def decoder(self):
        decoder = Tier3Decoder.__new__(Tier3Decoder)
        decoder._frequencies = {CONTROL_HZ, TRAFFIC_HZ}
        decoder._tune = None
        decoder._tune_taken = False
        decoder._hz = TRAFFIC_HZ
        decoder._input = bytearray()
        decoder._audio = bytearray()
        decoder._audio_alignment = 0
        decoder._pending_audio = deque()
        decoder._clear_call = None
        decoder._clear_elapsed = None
        decoder._resample_state = np.zeros(48)
        decoder._dc_state = np.zeros(1)
        decoder._decoder_status = {"trunk_tuned": True, "vc_freq_hz": TRAFFIC_HZ, "synctype": 12}
        decoder._calls = (CALL,)
        decoder._blocked = frozenset()
        decoder._hold = decoder._allowed = None
        decoder._rig = None
        decoder._rig_reply = bytearray()
        return decoder

    def test_tune_success_waits_for_the_receiver(self):
        decoder = self.decoder()
        self.assertEqual(decoder._rig_command(b"F 438800000"), b"RPRT -11\n")
        decoder._pending_audio.append((0, np.zeros(160)))
        self.assertEqual(decoder._rig_command(f"F {CONTROL_HZ}".encode()), b"")
        self.assertFalse(decoder._pending_audio)
        self.assertEqual(decoder._rig_command(b"f"), f"{TRAFFIC_HZ}\n".encode())
        self.assertFalse(decoder.ack_tune(CONTROL_HZ))
        decoder._tune_taken = True
        with patch.object(decoder, "_read_updates"), patch.object(decoder, "_read_audio"):
            self.assertTrue(decoder.ack_tune(CONTROL_HZ))
        self.assertEqual(decoder._rig_command(b"f"), f"{CONTROL_HZ}\n".encode())
        self.assertEqual(decoder._rig_reply, b"RPRT 0\n")
        self.assertIsNone(decoder._current_call()[1])

    def test_group_audio_needs_explicit_clear_metadata(self):
        self.assertIsNone(_clear_group(CALL, CONTROL_HZ))
        self.assertIsNone(_clear_group(dict(CALL, kind=1), TRAFFIC_HZ))
        for hints in ({"enc": True}, {"algid": 1}):
            self.assertIs(_clear_group(dict(CALL, **hints), TRAFFIC_HZ)[0], False)
        for hints in ({"enc": None}, {"algid": None}):
            self.assertIsNone(_clear_group(dict(CALL, **hints), TRAFFIC_HZ)[0])
        self.assertTrue(_clear_group(dict(CALL, algid=0x80), TRAFFIC_HZ)[0])

    def test_buffered_metadata_changes_discard_previous_audio(self):
        decoder = self.decoder()
        decoder._clear_call = (1, 100, 1001, 2)
        decoder._pending_audio.append((0, np.zeros(160)))
        decoder._api = None
        decoder._api_input = bytearray()
        decoder._audio.extend(b"\x00" * 7)
        with patch("tier3._read_all", side_effect=lambda _peer, pending, _limit: pending.extend(
                b'{"type":"call","slots":[]}\n')):
            decoder._read_updates()
        self.assertFalse(decoder._pending_audio)
        self.assertFalse(decoder._audio)
        self.assertEqual(decoder._audio_alignment, 1)

    def test_malformed_slot_metadata_fails_closed(self):
        decoder = self.decoder()
        decoder._api = None
        decoder._api_input = bytearray()
        with patch("tier3._read_all", side_effect=lambda _peer, pending, _limit: pending.extend(
                b'{"type":"call","slots":[null]}\n')):
            with self.assertRaises(DmrDecoderError):
                decoder._read_updates()


if __name__ == "__main__":
    unittest.main()

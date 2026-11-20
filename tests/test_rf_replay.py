import unittest
import wave
from email.message import Message
from http import HTTPStatus
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from app import PanelHandler
from receiver import IQ_BLOCK_BYTES, IQ_RATE, IQ_SAMPLES, SAMPLE_RATE
from rf_replay import MAX_BLOCKS, RF_REPLAY_URL, RFReplay


CENTER = 166_900_000
TUNING = (166_765_000, "AM")
SILENCE = bytes(IQ_BLOCK_BYTES)


class RFReplayTests(unittest.TestCase):
    def setUp(self):
        self.replay = RFReplay()

    def feed(self, sequence, *, generation=1, center=CENTER, gain=280, tuning=TUNING, raw=SILENCE):
        self.replay.feed(raw, generation, sequence, center, False, gain, tuning)

    def test_ring_bounds_and_rf_discontinuities(self):
        for sequence in range(MAX_BLOCKS + 10):
            self.feed(sequence)
        self.assertEqual(self.replay.status()["duration_ms"], 10_000)

        # A digital-only tuning change preserves still-contiguous RF data.
        last = MAX_BLOCKS + 10
        self.feed(last, tuning=(CENTER + 35_000, "FM"))
        status = self.replay.status()
        self.assertEqual(status["duration_ms"], 10_000)
        self.assertEqual((status["tune_hz"], status["mode"]), (CENTER + 35_000, "FM"))

        self.feed(last + 2)
        self.assertEqual(self.replay.status()["duration_ms"], 13)
        self.feed(last + 3)
        self.assertEqual(self.replay.status()["duration_ms"], 27)
        self.feed(last + 4, gain=290)
        self.assertEqual(self.replay.status()["duration_ms"], 13)
        self.feed(last + 5, center=CENTER + 50_000, gain=290)
        self.assertEqual(self.replay.status()["duration_ms"], 13)
        self.feed(last + 6, generation=2, center=CENTER + 50_000, gain=290)
        self.assertEqual(self.replay.status()["duration_ms"], 13)

    def test_snapshots_are_session_bound_and_expire(self):
        with patch("rf_replay.monotonic", return_value=50.0) as clock:
            self.feed(1)
            capture = self.replay.capture("browser-a")
            self.assertIsNone(self.replay.get("browser-b", capture["id"]))
            self.replay.clear_live()
            self.assertEqual(self.replay.status()["duration_ms"], 0)
            self.assertEqual(self.replay.get("browser-a", capture["id"]).metadata, capture)
            clock.return_value = 232.0
            self.assertIsNone(self.replay.get("browser-a", capture["id"]))

    def test_replay_can_select_two_carriers_from_the_same_raw_capture(self):
        span = 12 * IQ_SAMPLES
        times = np.arange(span) / IQ_RATE
        iq = sum((32 + 16 * np.cos(2 * np.pi * tone * times)) * np.exp(2j * np.pi * offset * times)
                 for tone, offset in ((1000, -135_000), (2000, 205_000)))
        raw = (np.column_stack((iq.real, iq.imag)) + 127.5).astype(np.uint8).tobytes()
        for i in range(12):
            self.feed(i, raw=raw[i * IQ_BLOCK_BYTES:(i + 1) * IQ_BLOCK_BYTES])
        capture = self.replay.get("browser-a", self.replay.capture("browser-a")["id"])
        self.assertFalse(capture.accepts(CENTER + 1_000_000, "AM"))
        self.assertFalse(capture.accepts(CENTER - 135_000, "DMR"))
        for offset, tone in ((-135_000, 1000), (205_000, 2000)):
            hz = CENTER + offset
            result = self.replay.render(capture, hz, "AM")
            self.assertEqual(len(result), capture.wave_size)
            self.assertIs(self.replay.render(capture, hz, "AM"), result)
            with wave.open(BytesIO(result), "rb") as audio:
                self.assertEqual((audio.getnchannels(), audio.getframerate()), (1, SAMPLE_RATE))
                pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")[480:]
            spectrum = abs(np.fft.rfft(pcm))
            peak = np.argmax(spectrum) * SAMPLE_RATE / len(pcm)
            self.assertAlmostEqual(peak, tone, delta=10)

    def test_handler_range_and_pin_checks(self):
        handler = PanelHandler.__new__(PanelHandler)
        handler.accept_request = Mock(return_value=True)
        handler.check_pin = Mock(return_value=False)
        handler.radio = Mock(return_value=(SimpleNamespace(rf_replay=self.replay), 1, False))
        handler.json_response = Mock()
        handler.response = Mock()
        handler.headers = Message()
        handler.server = SimpleNamespace()
        handler.direct_request = True
        self.feed(0)
        handler.path = f"{RF_REPLAY_URL}/capture"
        handler.do_POST()
        handler.radio.assert_not_called()

        handler.check_pin.return_value = True
        handler.do_POST()
        capture, = handler.json_response.call_args.args
        handler.path = f"{RF_REPLAY_URL}/{capture['id']}/AM/{TUNING[0]}"
        handler.command = "HEAD"
        handler.headers["Range"] = "bytes=0-1"
        with patch.object(self.replay, "render", side_effect=AssertionError("HEAD must skip demodulation")):
            handler.do_HEAD()
        self.assertEqual(handler.response.call_args.args, ("audio/wav", b"", HTTPStatus.PARTIAL_CONTENT))
        self.assertEqual(handler.response.call_args.kwargs["length"], 2)
        handler.command = "GET"
        handler.do_GET()
        self.assertEqual(handler.response.call_args.args, ("audio/wav", b"RI", HTTPStatus.PARTIAL_CONTENT))


if __name__ == "__main__":
    unittest.main()

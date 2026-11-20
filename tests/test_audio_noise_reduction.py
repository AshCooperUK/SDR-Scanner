import unittest
from unittest.mock import patch

import numpy as np
from scipy import signal

from receiver import SAMPLE_RATE, _ChannelDecoder, _NoiseReduction, _SpeechNoiseReduction, _speexdsp


class AudioNoiseReductionTests(unittest.TestCase):
    def test_disable_zero_level_and_digital_discriminator_bypass(self):
        decoder = _ChannelDecoder()
        for mode, enabled, level in [("FM", False, 7), ("FM", True, 0), ("DMR", True, 7)]:
            with self.subTest(mode=mode, enabled=enabled, level=level):
                decoder.settings = {"noise_reduction": enabled, "nr_level": level}
                decoder._audio_processing(mode)
                self.assertIsNone(decoder.noise_reduction)

    def test_fft_filter_covers_other_modes_and_missing_library(self):
        decoder = _ChannelDecoder()
        decoder.settings = {"noise_reduction": True}
        with patch("receiver._speexdsp", return_value=None) as library:
            decoder._audio_processing("WFM")
            library.assert_not_called()
            self.assertIsInstance(decoder.noise_reduction, _NoiseReduction)
            decoder._audio_processing("AM")
            library.assert_called_once()
            self.assertIsInstance(decoder.noise_reduction, _NoiseReduction)

    @unittest.skipUnless(_speexdsp(), "SpeexDSP library is required")
    def test_standard_level_reduces_hiss_and_retains_a_voice_onset(self):
        decoder = _ChannelDecoder()
        decoder.settings = {"noise_reduction": True, "nr_level": 7}
        decoder._audio_processing("FM")
        reducer = decoder.noise_reduction
        self.assertIsInstance(reducer, _SpeechNoiseReduction)

        size = 320
        seconds = np.arange(3 * SAMPLE_RATE) / SAMPLE_RATE
        hiss = np.random.default_rng(7).normal(0, .039, size=len(seconds))
        narrowband = signal.butter(4, [320, 3000], fs=SAMPLE_RATE, btype="bandpass", output="sos")
        hiss = signal.sosfilt(narrowband, hiss)
        voice = (0.11 * np.sin(2 * np.pi * 680 * seconds) + 0.07 * np.sin(2 * np.pi * 1360 * seconds))
        audio = hiss + voice * (seconds >= 2)

        padded = np.pad(audio, (0, size))
        processed = np.concatenate([reducer.process(frame) for frame in padded.reshape(-1, size)])
        aligned = processed[size:]
        self.assertEqual(len(aligned), len(audio))
        quiet = slice(SAMPLE_RATE, 2 * SAMPLE_RATE)
        onset = slice(2 * SAMPLE_RATE, int(2.1 * SAMPLE_RATE))
        self.assertLess(np.linalg.norm(aligned[quiet]), .56 * np.linalg.norm(hiss[quiet]))
        self.assertGreater(np.dot(aligned[onset], voice[onset]), .80 * np.dot(voice[onset], voice[onset]))

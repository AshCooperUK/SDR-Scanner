"""Find subaudible CTCSS tones and DCS words in narrow FM discriminator audio."""

from collections import Counter

import numpy as np
from scipy import signal


# ETSI TS 103 236, sections 4.1–4.2, plus codes common in handheld radios.
CTCSS_HZ = np.array([
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9,
    171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5,
    203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
])
DCS_CODES = (
    "023 025 026 031 032 036 043 047 051 053 054 065 071 072 073 074 "
    "114 115 116 122 125 131 132 134 143 145 152 155 156 162 165 172 174 "
    "205 212 223 225 226 243 244 245 246 251 252 255 261 263 265 266 271 274 "
    "306 311 315 325 331 332 343 346 351 356 364 365 371 "
    "411 412 413 423 431 432 445 446 452 454 455 462 464 465 466 "
    "503 506 516 523 526 532 546 565 606 612 624 627 631 632 654 662 664 "
    "703 712 723 731 732 734 743 754"
).split()

ANALYSIS_RATE = 2_400
WINDOW = ANALYSIS_RATE * 2 // 5
HOP = ANALYSIS_RATE // 10
WINDOW_WEIGHTS = np.hanning(WINDOW)
FFT_SIZE = 8192
MIN_SIGNAL_RMS = .012
DCS_BIT_RATE = 134.4
DCS_BITS = 23
DCS_MASK = (1 << DCS_BITS) - 1
DCS_WEIGHTS = 1 << np.arange(DCS_BITS)


def normalize_tone_filter(value):
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if kind == "CTCSS" and set(value) == {"kind", "hz"}:
        hz = value["hz"]
        if type(hz) in (int, float) and hz in CTCSS_HZ:
            return {"kind": kind, "hz": float(hz)}
    if kind == "DCS" and set(value) == {"kind", "code", "polarity"}:
        code, polarity = value["code"], value["polarity"]
        if code in DCS_CODES and polarity in ("N", "I"):
            return {"kind": kind, "code": code, "polarity": polarity}
    return None


def tone_matches(saved, detected):
    if detected is None or saved["kind"] != detected["kind"]:
        return False
    if saved["kind"] == "CTCSS":
        return saved["hz"] == detected["hz"]
    code = saved["code"], saved["polarity"]
    return code in ((detected["code"], detected["polarity"]),
                    (detected.get("alias"), detected.get("alias_polarity")))


def _dcs_word(code):
    data = 0x800 | int(code, 8)
    remainder = data << 11
    for bit in range(22, 10, -1):
        if remainder & (1 << bit):
            remainder ^= 0xC75 << (bit - 11)
    return (remainder << 12) | data


def _dcs_rotations(word):
    return [(word >> shift | word << (DCS_BITS - shift)) & DCS_MASK
            for shift in range(DCS_BITS)]


def _dcs_lookup():
    # There is no sync marker. A cyclic shift of an inverted word can be a
    # different normal word. Report the lower code first and include its alias.
    aliases = {}
    for code in DCS_CODES:
        normal = _dcs_word(code)
        for word, polarity in ((normal, "N"), (normal ^ DCS_MASK, "I")):
            for rotation in _dcs_rotations(word):
                aliases.setdefault(rotation, []).append((code, polarity))
    return {word: tuple(sorted(matches)) for word, matches in aliases.items()}


def _dcs_samples():
    # Try bit centres across the specified rate tolerance and all clock phases.
    # Sample two complete words to reject speech and random bit patterns.
    rates = DCS_BIT_RATE * (1 + np.linspace(-.02, .02, 13))
    periods = ANALYSIS_RATE / rates[:, None, None]
    phases = np.arange(8)[None, :, None] / 8
    bits = np.arange(2 * DCS_BITS)[None, None, :]
    centres = (WINDOW - 1 - 2 * DCS_BITS * periods) / 2 + (bits + phases) * periods
    return np.rint(centres).astype(int).reshape(-1, 2 * DCS_BITS)


DCS_WORDS = _dcs_lookup()
DCS_SAMPLES = _dcs_samples()


def _find_dcs(samples):
    windows = samples[DCS_SAMPLES]
    low, high = np.percentile(windows, [20, 80], axis=1)
    bits = windows > ((low + high) / 2)[:, None]
    words = bits.reshape(-1, 2, DCS_BITS) @ DCS_WEIGHTS
    matches = Counter(DCS_WORDS[word] for word, repeat in words
                      if word == repeat and word in DCS_WORDS)
    if not matches:
        return None
    aliases = matches.most_common(1)[0][0]
    code, polarity = aliases[0]
    result = {"kind": "DCS", "code": code, "polarity": polarity}
    if len(aliases) > 1:
        result["alias"], result["alias_polarity"] = aliases[1]
    return result


def _find_ctcss(samples):
    weighted = samples * WINDOW_WEIGHTS
    power = np.abs(np.fft.rfft(weighted, FFT_SIZE)) ** 2
    low = round(40 * FFT_SIZE / ANALYSIS_RATE)
    high = round(295 * FFT_SIZE / ANALYSIS_RATE)
    peak = low + int(np.argmax(power[low:high + 1]))
    # A continuous tone must account for most of the subaudible energy.
    if 2 * power[peak] < .65 * WINDOW_WEIGHTS.sum() * np.dot(weighted, samples):
        return None
    left, middle, right = power[peak - 1:peak + 2]
    curvature = left - 2 * middle + right
    fraction = .5 * (left - right) / curvature if curvature else 0
    frequency = (peak + fraction) * ANALYSIS_RATE / FFT_SIZE
    closest = float(CTCSS_HZ[np.argmin(np.abs(CTCSS_HZ - frequency))])
    if abs(frequency - closest) <= closest * .0075:
        return {"kind": "CTCSS", "hz": closest}
    return None


class ToneSearch:
    """Report recognised tones while leaving the receiver's audio gate alone."""

    def __init__(self, sample_rate=48_000):
        self._decimation, remainder = divmod(sample_rate, ANALYSIS_RATE)
        if not self._decimation or remainder:
            raise ValueError("Tone search requires a sample rate divisible by 2400 Hz")
        self._filter = signal.butter(6, 285, fs=sample_rate, output="sos")
        self._filter_initial = signal.sosfilt_zi(self._filter)
        self.reset()

    def reset(self):
        self._filter_state = None
        self._phase = 0
        self._pending = np.empty(0)
        self._candidate = None
        self._misses = 0
        self.result = None

    def feed(self, samples):
        if not len(samples):
            return self.result
        if self._filter_state is None:
            self._filter_state = self._filter_initial * samples[0]
        filtered, self._filter_state = signal.sosfilt(self._filter, samples, zi=self._filter_state)
        self._pending = np.concatenate((self._pending, filtered[self._phase::self._decimation]))
        self._phase = (self._phase - len(samples)) % self._decimation
        while self._pending.size >= WINDOW:
            samples = self._pending[:WINDOW]
            self._pending = self._pending[HOP:]
            self._analyse(samples - samples.mean())
        return self.result

    def _analyse(self, samples):
        result = None
        if np.dot(samples, samples) >= WINDOW * MIN_SIGNAL_RMS ** 2:
            result = _find_dcs(samples) or _find_ctcss(samples)
        if result is not None and result == self._candidate:
            self.result = result
            self._misses = 0
        elif self.result is not None:
            self._misses += 1
            if self._misses >= 3:
                self.result = None
        self._candidate = result

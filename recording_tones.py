"""Identify short voice-channel captures containing only isolated repeater tones."""

import numpy as np
from scipy.fft import rfft


def _is_quiet(samples, step, limit):
    for start in range(0, len(samples), step):
        block = samples[start:start + step]
        if len(block) >= step // 2 and np.mean(block * block) > limit * limit:
            return False
    return True


def _steady_tones(tonal, frequency, rms, hop, frame_length, rate):
    edges = np.flatnonzero(np.diff(np.r_[False, tonal, False]))
    for first, end in zip(edges[::2], edges[1::2]):
        changes = np.flatnonzero(np.abs(np.diff(frequency[first:end])) > 8)
        boundaries = [first, *(first + changes + 1), end]
        for begin, stop in zip(boundaries, boundaries[1:]):
            start_sample = begin * hop
            end_sample = (stop - 1) * hop + frame_length
            duration = (end_sample - start_sample) / rate
            if not 0.045 <= duration <= 0.42:
                continue
            pitches = frequency[begin:stop]
            levels = rms[begin:stop]
            if np.ptp(pitches) <= 8 and levels.max() <= 1.8 * levels.min():
                yield start_sample, end_sample, np.median(levels)


def _only_tones(pcm, rate):
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    frame_length = round(rate * 0.024)
    hop = round(rate * 0.006)
    if len(samples) < frame_length:
        return False

    # A Hann window separates a single steady pitch from adjacent voice harmonics.
    frames = np.lib.stride_tricks.sliding_window_view(samples, frame_length)[::hop]
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    fft_size = 1 << (3 * frame_length - 1).bit_length()
    bin_width = rate / fft_size
    windowed = frames * np.hanning(frame_length).astype(np.float32)
    power = np.abs(rfft(windowed, n=fft_size, axis=1)) ** 2
    low, high = round(475 / bin_width), round(2_300 / bin_width)
    peaks = np.argmax(power[:, low:high + 1], axis=1) + low
    rows = np.arange(len(peaks))
    half_width = round(65 / bin_width)
    narrow = power[rows[:, None], peaks[:, None] + np.arange(-half_width, half_width + 1)].sum(axis=1)
    tonal = (narrow >= 0.94 * power.sum(axis=1)) & (rms >= 58)
    # Sub-bin peak estimates distinguish steady tones from drifting voice pitch.
    left, center, right = (power[rows, peaks + offset] for offset in (-1, 0, 1))
    curvature = np.maximum(2 * center - left - right, 1)
    frequency = (peaks + 0.5 * (right - left) / curvature) * bin_width
    tones = list(_steady_tones(tonal, frequency, rms, hop, frame_length, rate))
    if not tones or len(tones) > 5 or sum(end - start for start, end, _ in tones) > 1.4 * rate:
        return False

    # Reject the classification if any section outside the detected tones
    # contains appreciable audio. Allow brief edges at the squelch and tone transitions.
    quiet_limit = min(320, 0.16 * np.median([level for _, _, level in tones]))
    padding = round(rate * 0.006)
    cursor = round(rate * 0.010)
    for start, end, _ in tones:
        if not _is_quiet(samples[cursor:max(cursor, start - padding)], hop, quiet_limit):
            return False
        cursor = max(cursor, end + padding)
    return _is_quiet(samples[cursor:len(samples) - round(rate * 0.010)], hop, quiet_limit)


class ToneOnlyClip:
    """Analyze a short whole clip while the MP3 encoder receives the original PCM."""

    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self._limit = sample_rate * 8 * 2
        self._buffer = bytearray()

    def observe(self, pcm):
        if self._buffer is None:
            return
        if len(self._buffer) + len(pcm) > self._limit:
            self._buffer = None
        else:
            self._buffer.extend(pcm)

    def should_discard(self):
        return self._buffer is not None and _only_tones(self._buffer, self.sample_rate)

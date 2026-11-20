"""Receive AM, FM and single sideband from a local RTL-SDR.

Only the radio worker should use a Receiver instance. Level measurements are
channel power relative to ADC full scale. Audio is signed little-endian PCM.
"""

from __future__ import annotations

import ctypes as c
from collections import deque
from ctypes.util import find_library
from functools import lru_cache
from threading import Condition, Event, Thread
from time import monotonic
from typing import NamedTuple
from weakref import finalize

import numpy as np
from scipy import signal

from bandplan import BANDS, PMR
from fsk import RttyDecoder
from rds import RdsDecoder
from tone_search import ToneSearch


SAMPLE_RATE = 24_000
IQ_RATE = 2_400_000
CHANNEL_RATE = 2 * SAMPLE_RATE
WFM_CHANNEL_RATE = 10 * SAMPLE_RATE
IQ_SAMPLES = 32_000
SETTLE_SAMPLES = 15_360
TUNER_OFFSET = 300_000
DIRECT_LIMIT = 25_000_000
TUNER_LIMIT = 1_750_000_000
PROBE_FFT = 8192
PROBE_HALF_WIDTH = 900_000
PROBE_STRIDE = 1_600_000
NEARBY_MIN_EXCESS_DB = 10
NEARBY_DC_GUARD_HZ = 20_000
NEARBY_PMR_STEP = PMR[1] - PMR[0]
NEARBY_BROADCAST_FM = next(band for band in BANDS if band.modulation == "WFM")
NEARBY_MIN_PEAK_FRACTION = 10 ** (-18 / 10)
WATERFALL_BINS = 320
WATERFALL_SPAN_HZ = 2 * PROBE_HALF_WIDTH
WATERFALL_INTERVAL = 1 / 7
CARRIER_SNAP_MAX_HZ = 7_500
CARRIER_DC_GUARD_HZ = 1_500
SSB_MODES = frozenset(("USB", "LSB"))
CW_MODES = frozenset(("CW", "CW-R"))
FSK_MODES = frozenset(("FSK", "FSK-R"))
SAM_MODES = frozenset(("S-AM(D)", "S-AM(U)", "S-AM(L)"))
ANALOG_VOICE_MODES = SSB_MODES | SAM_MODES | {"AM", "FM"}
SSB_LOW_HZ = 300
SSB_HIGH_HZ = 3_000
SSB_GAIN_DECAY = np.exp(-IQ_SAMPLES / (IQ_RATE * .5))
WATCH_HALF_WIDTH = {"AM": 7_000, "FM": 9_000, "WFM": 120_000, "USB": 3_600, "LSB": 3_600, "DMR": 8_000}
METER_NOISE_BANDWIDTH = {"AM": 7_400, "FM": 10_600, "WFM": 200_000,
                         "USB": SSB_HIGH_HZ - SSB_LOW_HZ, "LSB": SSB_HIGH_HZ - SSB_LOW_HZ, "DMR": 15_000}
WATCH_HALF_WIDTH.update(dict.fromkeys(CW_MODES, 900))
WATCH_HALF_WIDTH.update(dict.fromkeys(FSK_MODES, 2_400))
WATCH_HALF_WIDTH.update(dict.fromkeys(SAM_MODES, 7_000))
METER_NOISE_BANDWIDTH.update(dict.fromkeys(CW_MODES, 500))
METER_NOISE_BANDWIDTH.update(dict.fromkeys(FSK_MODES, 1_000))
METER_NOISE_BANDWIDTH.update(dict.fromkeys(SAM_MODES, 7_400))
METER_SPECTRUM_INTERVAL = 4
METER_MIN_EXCESS_DB = 5
AUTO_MIN_SNR_DB = 20
AUTO_VOTES = 8
NFM_DEEMPHASIS_SECONDS = 200e-6
WFM_DEEMPHASIS_SECONDS = 50e-6
NFM_AUDIO_MIN_SNR_DB = 16
NFM_AUDIO_MAX_OFFSET_HZ = 2_700
NFM_AUDIO_MIN_RMS = .012
NFM_AUDIO_TARGET_RMS = .12
NFM_AUDIO_MAX_GAIN = 2.5
NFM_AUDIO_GAIN_RISE = 1 - np.exp(-IQ_SAMPLES / (IQ_RATE * .12))
NFM_AUDIO_ENVELOPE_DECAY = np.exp(-IQ_SAMPLES / (IQ_RATE * .35))
TUNER_GAIN_TARGET = 280  # Librtlsdr expresses gain in tenths of a dB.
GAIN_RECOVERY_BLOCKS = IQ_RATE // IQ_SAMPLES
IQ_BLOCK_BYTES = 2 * IQ_SAMPLES
USB_TRANSFER_BYTES = 16_384
USB_TRANSFER_COUNT = 8
MAX_PENDING_BLOCKS = 6
IQ_CALLBACK = c.CFUNCTYPE(None, c.POINTER(c.c_ubyte), c.c_uint32, c.c_void_p)


class ReceiverError(RuntimeError):
    """The local radio could not complete a hardware operation."""


class NearbyPeak(NamedTuple):
    """Channel power at the ADC and adjusted for tuner gain."""

    hz: int
    level_dbfs: float
    strength_db: float


class ScopeSpectrum(NamedTuple):
    """Full-resolution FFT behind a specific row of the live scope."""

    sequence: int
    time_ms: int
    center_hz: int
    low_hz: int
    high_hz: int
    power: np.ndarray

    def carrier_near(self, hz, radius_hz, mode):
        if not self.low_hz <= hz < self.high_hz or mode in SSB_MODES | FSK_MODES:
            return None
        radius_hz = min(radius_hz, CARRIER_SNAP_MAX_HZ)
        bin_hz = IQ_RATE / PROBE_FFT
        wide = mode == "WFM"
        narrow_carrier = mode in SAM_MODES | CW_MODES | {"AM"}
        width = 27 if wide else 1 if narrow_carrier else 5
        margin = 250_000 if wide else 25_000
        lower = max(self.low_hz, hz - margin)
        upper = min(self.high_hz, hz + margin)
        first = PROBE_FFT // 2 + int(np.ceil((lower - self.center_hz) / bin_hz))
        stop = PROBE_FFT // 2 + int(np.ceil((upper - self.center_hz) / bin_hz))
        raw = self.power[first:stop]
        if raw.size < max(16, width + 2):
            return None
        floor = max(float(np.percentile(raw, 30 if wide else 50)), 1e-15)
        power = raw.copy()
        dc = PROBE_FFT // 2 - first
        guard = int(np.ceil(CARRIER_DC_GUARD_HZ / bin_hz))
        power[max(0, dc - guard):max(0, dc + guard + 1)] = floor
        smoothed = np.convolve(power, np.ones(width) / width, mode="same")
        occupied = smoothed > 4 * floor
        edges = np.flatnonzero(np.diff(np.r_[False, occupied, False]))
        candidates = []
        for start, end in zip(edges[::2], edges[1::2]):
            if start == 0 or end == len(smoothed) or smoothed[start:end].max() < 10 * floor:
                continue
            bandwidth = (end - start) * bin_hz
            peak = start + np.argmax(power[start:end])
            if wide:
                # Several nearby modulation peaks may belong to one FM station.
                # Estimate its center from both shoulders of an occupied channel.
                if not 70_000 <= bandwidth <= 240_000:
                    continue
                location = (start + end - 1) / 2
            elif narrow_carrier or bandwidth <= 2_000:
                location = _interpolated_fft_bin(power, peak)
            elif bandwidth <= 20_000:
                weights = smoothed[start:end] - floor
                location = np.dot(np.arange(start, end), weights) / weights.sum()
            else:
                continue
            frequency = round(self.center_hz + (first + location - PROBE_FFT // 2) * bin_hz)
            if (self.low_hz <= frequency < self.high_hz and abs(frequency - hz) <= radius_hz
                    and abs(frequency - self.center_hz) > CARRIER_DC_GUARD_HZ):
                candidates.append(frequency)
        candidates.sort(key=lambda frequency: abs(frequency - hz))
        if not candidates or (len(candidates) > 1
                              and abs(candidates[1] - hz) - abs(candidates[0] - hz) <= bin_hz):
            return None
        return candidates[0]


def _interpolated_fft_bin(power, peak):
    left, middle, right = np.log(np.maximum(power[peak - 1:peak + 2], 1e-15))
    curvature = left - 2 * middle + right
    offset = np.clip(0.5 * (left - right) / curvature, -0.5, 0.5) if curvature < 0 else 0
    return peak + offset


def _rtl_library():
    try:
        lib = c.CDLL(find_library("rtlsdr") or "librtlsdr.so.0")
    except OSError as exc:
        raise ReceiverError("librtlsdr is required to open the SDR") from exc

    device = c.c_void_p
    signatures = {
        "get_device_count": (c.c_uint32,),
        "open": (c.c_int, c.POINTER(device), c.c_uint32),
        "close": (c.c_int, device),
        "set_sample_rate": (c.c_int, device, c.c_uint32),
        "set_direct_sampling": (c.c_int, device, c.c_int),
        "set_center_freq": (c.c_int, device, c.c_uint32),
        "set_agc_mode": (c.c_int, device, c.c_int),
        "set_tuner_gain_mode": (c.c_int, device, c.c_int),
        "get_tuner_gains": (c.c_int, device, c.POINTER(c.c_int)),
        "set_tuner_gain": (c.c_int, device, c.c_int),
        "reset_buffer": (c.c_int, device),
        "read_async": (c.c_int, device, IQ_CALLBACK, c.c_void_p, c.c_uint32, c.c_uint32),
        "cancel_async": (c.c_int, device),
    }
    for name, (restype, *argtypes) in signatures.items():
        function = getattr(lib, "rtlsdr_" + name)
        function.restype = restype
        function.argtypes = argtypes
    return lib


def _decode_iq(buffer):
    interleaved = np.frombuffer(buffer, dtype=np.uint8).astype(np.float32)
    interleaved -= 127.5
    interleaved *= 1.0 / 127.5
    return interleaved.view(np.complex64)


def _center_for_frequency(hz, direct):
    if hz + TUNER_OFFSET > TUNER_LIMIT or (direct and hz + TUNER_OFFSET >= DIRECT_LIMIT):
        return hz - TUNER_OFFSET
    return hz + TUNER_OFFSET


def _dc_guard(mode):
    return 120_000 if mode == "WFM" else 20_000


class _IqStream:
    """Keep USB reads in flight while the receiver processes earlier blocks."""

    def __init__(self, library, device):
        self._library = library
        self._device = device
        self._changed = Condition()
        self._stopping = Event()
        self._finished = False
        self._close_when_done = False
        self._error = None
        self._settle_bytes = 2 * SETTLE_SAMPLES
        self._pending = bytearray()
        self._blocks = deque(maxlen=MAX_PENDING_BLOCKS)
        self._sequence = 0
        self._callback = IQ_CALLBACK(self._receive)
        self._thread = Thread(target=self._run, name="RTL-SDR USB", daemon=True)
        self._thread.start()

    def _run(self):
        try:
            result = self._library.rtlsdr_read_async(
                self._device, self._callback, None, USB_TRANSFER_COUNT, USB_TRANSFER_BYTES
            )
            if not self._stopping.is_set():
                self._error = f"SDR IQ stream ended ({result})"
        except Exception as exc:
            self._error = f"SDR IQ stream failed: {exc}"
        finally:
            with self._changed:
                self._finished = True
                close_device = self._close_when_done
                self._changed.notify_all()
            if close_device:
                self._library.rtlsdr_close(self._device)

    def _receive(self, buffer, size, _context):
        if self._stopping.is_set():
            # Stop may have been requested just before read_async entered C.
            self._library.rtlsdr_cancel_async(self._device)
            return
        if size % 2:
            self._error = "SDR returned an incomplete IQ sample"
            self._stopping.set()
            self._library.rtlsdr_cancel_async(self._device)
            return
        block = c.string_at(buffer, size)
        if self._settle_bytes:
            discard = min(self._settle_bytes, size)
            self._settle_bytes -= discard
            block = block[discard:]
        self._pending.extend(block)
        if len(self._pending) >= IQ_BLOCK_BYTES:
            data = bytes(self._pending[:IQ_BLOCK_BYTES])
            del self._pending[:IQ_BLOCK_BYTES]
            with self._changed:
                self._sequence += 1
                self._blocks.append((self._sequence, data))
                self._changed.notify()

    def read(self):
        with self._changed:
            if not self._changed.wait_for(lambda: self._blocks or self._finished, timeout=1.0):
                raise ReceiverError("SDR timed out waiting for IQ samples")
            if self._finished:
                raise ReceiverError(self._error or "SDR IQ stream stopped")
            return self._blocks.popleft()

    def discard_pending(self):
        with self._changed:
            self._blocks.clear()

    def stop(self):
        self._stopping.set()
        deadline = monotonic() + 1.0
        while self._thread.is_alive() and monotonic() < deadline:
            self._library.rtlsdr_cancel_async(self._device)
            self._thread.join(0.02)
        return not self._thread.is_alive()

    def close_device_after_stop(self):
        # libusb may take time to unwind after the device disappears. The C
        # handle must stay alive until the asynchronous read really returns.
        with self._changed:
            if not self._finished:
                self._close_when_done = True
                return
        self._library.rtlsdr_close(self._device)


class _NoiseReduction:
    def __init__(self, size, depth):
        self.previous = self.tail = np.zeros(size)
        self.window = np.sin((np.arange(size * 2) + .5) * np.pi / (size * 2))
        self.floor = None
        self.depth = depth / 15

    def process(self, block):
        spectrum = np.fft.rfft(np.concatenate((self.previous, block)) * self.window)
        power = np.abs(spectrum) ** 2
        if self.floor is None:
            self.floor = np.full_like(power, np.median(power))
        else:
            # Let quieter bins update quickly and louder bins slowly so
            # bursts of speech don't train the noise estimate.
            self.floor += np.where(power < self.floor, .22, .003) * (power - self.floor)
        gain = np.clip(1 - 1.5 * self.depth * self.floor / np.maximum(power, 1e-12), .16, 1.)
        filtered = np.fft.irfft(spectrum * gain, n=len(self.window)) * self.window
        size = len(block)
        output = self.tail + filtered[:size]
        self.previous, self.tail = block, filtered[size:]
        return output


@lru_cache(maxsize=1)
def _speexdsp():
    try:
        library = c.CDLL(find_library("speexdsp") or "libspeexdsp.so.1")
        signatures = {
            "speex_preprocess_state_init": (c.c_void_p, (c.c_int, c.c_int)),
            "speex_preprocess_state_destroy": (None, (c.c_void_p,)),
            "speex_preprocess_ctl": (c.c_int, (c.c_void_p, c.c_int, c.c_void_p)),
            "speex_preprocess_run": (c.c_int, (c.c_void_p, c.POINTER(c.c_int16))),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(library, name)
            function.restype = result
            function.argtypes = arguments
    except (OSError, AttributeError):
        return None
    return library


class _SpeechNoiseReduction:
    def __init__(self, library, size, depth):
        self.library = library
        self.handle = library.speex_preprocess_state_init(size, SAMPLE_RATE)
        if not self.handle:
            raise ReceiverError("Could not start the audio noise reducer")
        # Each tune gets a fresh noise estimate. Free the old native state
        # when the decoder replaces this block processor.
        close = finalize(self, library.speex_preprocess_state_destroy, self.handle)
        close.atexit = False
        noise_suppress = 18  # SPEEX_PREPROCESS_SET_NOISE_SUPPRESS, in negative dB.
        suppression = c.c_int(-round(25 * depth / 15))
        if library.speex_preprocess_ctl(self.handle, noise_suppress, c.byref(suppression)) != 0:
            close()
            raise ReceiverError("Could not configure the audio noise reducer")

    def process(self, block):
        # Reserve 6 dB of PCM headroom for the decoder's floating-point audio.
        pcm = np.rint(np.clip(block * 16384, -32768, 32767)).astype(np.int16)
        self.library.speex_preprocess_run(self.handle, pcm.ctypes.data_as(c.POINTER(c.c_int16)))
        return pcm / 16384


def _audio_noise_reducer(mode, level):
    if not level or mode == "DMR":
        return None
    size = IQ_SAMPLES * SAMPLE_RATE // IQ_RATE
    if mode in ANALOG_VOICE_MODES and (library := _speexdsp()) is not None:
        try:
            return _SpeechNoiseReduction(library, size, level)
        except ReceiverError:
            pass
    return _NoiseReduction(size, level)


def _analog_mode_evidence(channel):
    """Classify a strong narrowband channel when its IQ is conclusive."""
    envelope = np.abs(channel)
    amplitude = float(np.mean(envelope))
    if amplitude < 1e-5:
        return None
    products = channel[1:] * np.conjugate(channel[:-1])
    scale = CHANNEL_RATE / (2 * np.pi)
    carrier = float(np.angle(np.sum(products)) * scale)
    if abs(carrier) > 1200:
        return None
    phase_spread = float(np.std(np.angle(products) * scale))
    depth = float(np.std(envelope)) / amplitude
    if phase_spread >= 500 and depth < .10:
        return "FM"
    if phase_spread > 350 or depth < .10 or np.std(np.diff(envelope)) < .006 * amplitude:
        return None
    # AM retains a coherent carrier. Reject energy from fading or sideband
    # modulation when it does not have a dominant carrier near the centre.
    points = np.arange(channel.size)
    coherent = channel * np.exp((-2j * np.pi * carrier / CHANNEL_RATE) * points)
    fraction = abs(np.mean(coherent)) ** 2 / np.mean(envelope ** 2)
    return "AM" if fraction >= .55 else None


def _shelf(frequency, gain_db, high):
    gain = 10 ** (gain_db / 40)
    omega = 2 * np.pi * frequency / SAMPLE_RATE
    cosine = np.cos(omega)
    slope = np.sqrt(2 * gain) * np.sin(omega)
    if high:
        numer = [gain * ((gain + 1) + (gain - 1) * cosine + slope),
                 -2 * gain * ((gain - 1) + (gain + 1) * cosine),
                 gain * ((gain + 1) + (gain - 1) * cosine - slope)]
        denom = [(gain + 1) - (gain - 1) * cosine + slope,
                 2 * ((gain - 1) - (gain + 1) * cosine),
                 (gain + 1) - (gain - 1) * cosine - slope]
    else:
        numer = [gain * ((gain + 1) - (gain - 1) * cosine + slope),
                 2 * gain * ((gain - 1) - (gain + 1) * cosine),
                 gain * ((gain + 1) - (gain - 1) * cosine - slope)]
        denom = [(gain + 1) + (gain - 1) * cosine + slope,
                 -2 * ((gain - 1) + (gain + 1) * cosine),
                 (gain + 1) + (gain - 1) * cosine - slope]
    return signal.tf2sos(numer, denom)


class _ChannelDecoder:
    def __init__(self):
        self.rds = None
        self.tone_search = None
        narrow_decimation = signal.firwin(
            1101, 15_000, fs=IQ_RATE, window=("kaiser", 8.0)
        ).astype(np.float32)
        self.decimation_filters = {
            "AM": narrow_decimation,
            "FM": narrow_decimation,
            "USB": narrow_decimation,
            "LSB": narrow_decimation,
            "DMR": narrow_decimation,
            "WFM": signal.firwin(641, 104_000, fs=IQ_RATE, window=("kaiser", 8.0)).astype(np.float32),
        }
        self.decimation_filters.update(dict.fromkeys(CW_MODES | FSK_MODES | SAM_MODES, narrow_decimation))
        # Move a low-pass FIR away from zero to isolate one sideband in
        # complex baseband, before taking the real component for audio.
        ssb_taps = 641
        ssb_half_width = (SSB_HIGH_HZ - SSB_LOW_HZ) / 2
        ssb_midpoint = (SSB_HIGH_HZ + SSB_LOW_HZ) / 2
        ssb_indexes = np.arange(ssb_taps) - ssb_taps // 2
        usb = (signal.firwin(ssb_taps, ssb_half_width, fs=CHANNEL_RATE, window=("kaiser", 8.0))
               * np.exp(2j * np.pi * ssb_midpoint * ssb_indexes / CHANNEL_RATE)).astype(np.complex64)
        self.filters = {
            "AM": signal.firwin(121, 3700, fs=CHANNEL_RATE).astype(np.float32),
            "FM": signal.firwin(121, 5300, fs=CHANNEL_RATE).astype(np.float32),
            "DMR": signal.firwin(121, 7500, fs=CHANNEL_RATE).astype(np.float32),
            "USB": usb,
            "LSB": np.conjugate(usb),
            "WFM": np.ones(1, dtype=np.float32),  # The wide decimator already selects the station.
        }
        self.filters.update(dict.fromkeys(SAM_MODES, self.filters["AM"]))
        self.filters.update(dict.fromkeys(CW_MODES, self._lowpass(769, 250)))
        self.filters.update(dict.fromkeys(FSK_MODES, self._lowpass(513, 550)))
        self.sam_sidebands = {"S-AM(U)": self._sideband(0, 3500, 1),
                              "S-AM(L)": self._sideband(0, 3500, -1)}
        self.carrier_filter = signal.butter(2, 180, fs=CHANNEL_RATE, output="sos")
        self.bandwidth_hz = None
        self.cw_pitch = 600
        self.agc = "mid"
        self.settings = {}
        # Narrow FM voice usually carries boosted treble and may carry a
        # subaudible tone. Filter both after the discriminator.
        deemphasis = np.exp(-1 / (CHANNEL_RATE * NFM_DEEMPHASIS_SECONDS))
        wide_deemphasis = np.exp(-1 / (WFM_CHANNEL_RATE * WFM_DEEMPHASIS_SECONDS))
        self.audio_filters = {
            "AM": signal.butter(3, [250, 3400], fs=CHANNEL_RATE, btype="bandpass", output="sos"),
            "FM": np.vstack((
                signal.butter(5, [320, 3000], fs=CHANNEL_RATE, btype="bandpass", output="sos"),
                [1 - deemphasis, 0, 0, 1, -deemphasis, 0],
            )),
            "WFM": np.vstack((
                signal.butter(2, 40, fs=WFM_CHANNEL_RATE, btype="highpass", output="sos"),
                # Extract mono and reject the stereo pilot before resampling to 24 kHz.
                signal.butter(10, 10_000, fs=WFM_CHANNEL_RATE, output="sos"),
                [1 - wide_deemphasis, 0, 0, 1, -wide_deemphasis, 0],
            )),
        }
        self.audio_filters.update(dict.fromkeys(SAM_MODES, self.audio_filters["AM"]))
        narrow_audio = signal.butter(3, [150, 2800], fs=CHANNEL_RATE, btype="bandpass", output="sos")
        self.audio_filters.update(dict.fromkeys(CW_MODES | FSK_MODES, narrow_audio))
        self.sample_indexes = np.arange(IQ_SAMPLES, dtype=np.float64)
        self.reset(0, "FM")

    @staticmethod
    def _lowpass(taps, cutoff, rate=CHANNEL_RATE):
        return signal.firwin(taps, cutoff, fs=rate, window=("kaiser", 8.0)).astype(np.float32)

    @classmethod
    def _sideband(cls, lower, upper, direction):
        indexes = np.arange(641) - 320
        midpoint = direction * (lower + upper) / 2
        return (cls._lowpass(641, (upper - lower) / 2)
                * np.exp(2j * np.pi * midpoint * indexes / CHANNEL_RATE)).astype(np.complex64)

    def _filter(self, mode, rate):
        width = self.bandwidth_hz
        first, second = self.settings.get("pbt1", 0), self.settings.get("pbt2", 0)
        soft = self.settings.get("if_shape") == "soft"
        if mode == "DMR" or width is None and not (first or second or soft):
            return self.filters[mode]
        if mode in SSB_MODES:
            lower = 75
            upper = lower + min(width or 2700, 8400)
            if mode == "LSB":
                lower, upper = -upper, -lower
        else:
            width = width or METER_NOISE_BANDWIDTH[mode]
            lower, upper = -width / 2, width / 2
        if mode not in ("FM", "WFM"):
            # Each PBT control shifts a complete passband; its intersection
            # with the other passband determines the two remaining edges.
            lower += max(first, second)
            upper += min(first, second)
        center = (lower + upper) / 2
        center = float(np.clip(center, -.31 * rate, .31 * rate))
        half = min(max(25., (upper - lower) / 2), .48 * rate - abs(center))
        taps = 129 if soft else 769 if upper - lower < 2000 else 257
        result = self._lowpass(taps, half, rate)
        if center:
            indexes = np.arange(taps) - taps // 2
            return (result * np.exp(2j * np.pi * center * indexes / rate)).astype(np.complex64)
        return result

    def _audio_processing(self, mode):
        settings = self.settings
        filters = []
        for key, band in (("tone_hpf", "highpass"), ("tone_lpf", "lowpass")):
            if cutoff := settings.get(key, 0):
                filters.append(signal.butter(2, cutoff, fs=SAMPLE_RATE, btype=band, output="sos"))
        if bass := settings.get("tone_bass"):
            filters.append(_shelf(400, bass, high=False))
        if treble := settings.get("tone_treble"):
            filters.append(_shelf(2400, treble, high=True))
        if notch := settings.get("notch", 0):
            filters.append(signal.tf2sos(*signal.iirnotch(notch, 25, fs=SAMPLE_RATE)))
        if settings.get("audio_peak") and mode in CW_MODES:
            filters.append(signal.tf2sos(*signal.iirpeak(self.cw_pitch, 5, fs=SAMPLE_RATE)))
        if settings.get("fsk_tpf") and mode in FSK_MODES:
            pitch, shift = settings.get("fsk_pitch", 1615), settings.get("fsk_shift", 170)
            filters.append(signal.butter(3, [pitch - shift / 2 - 45, pitch + shift / 2 + 45],
                                         fs=SAMPLE_RATE, btype="bandpass", output="sos"))
        self.post_filter = np.concatenate(filters) if filters else None
        self.post_history = np.zeros((len(self.post_filter), 2)) if filters else None
        level = settings.get("nr_level", 7) if settings.get("noise_reduction") else 0
        self.noise_reduction = _audio_noise_reducer(mode, level)

    def reset(self, offset_hz, mode, keep_rds=False):
        if self.rds is not None:
            self.rds.reset(keep_metadata=keep_rds)
        if self.tone_search is not None:
            self.tone_search.reset()
        channel_rate = WFM_CHANNEL_RATE if mode == "WFM" else CHANNEL_RATE
        self.channel_decimation = IQ_RATE // channel_rate
        self.audio_decimation = channel_rate // SAMPLE_RATE
        self.level_start = channel_rate // 400
        self.deviation = 75_000 if mode == "WFM" else 2500
        self.discriminator_scale = channel_rate / (2 * np.pi * self.deviation)
        self.decimation_filter = self.decimation_filters[mode]
        history_blocks = (len(self.decimation_filter) - 2) // self.channel_decimation + 1
        self.history_size = history_blocks * self.channel_decimation
        if mode in FSK_MODES and self.settings.get("fsk_center") == "mark":
            offset_hz -= (.5 if mode == "FSK" else -.5) * self.settings.get("fsk_shift", 170)
        self.mixer = np.exp((-2j * np.pi * offset_hz / IQ_RATE) * self.sample_indexes).astype(np.complex64)
        self.mixer_phase = 1 + 0j
        self.phase_step = np.exp((-2j * np.pi * offset_hz * IQ_SAMPLES) / IQ_RATE)
        self.iq_history = np.zeros(self.history_size, dtype=np.complex64)
        self.channel_filter = self._filter(mode, channel_rate)
        self.channel_history = np.zeros(len(self.channel_filter) - 1, dtype=np.complex64)
        self.carrier_history = np.zeros((len(self.carrier_filter), 2), dtype=np.complex128)
        taps = self.sam_sidebands.get(mode, ())
        self.sideband_history = np.zeros(max(0, len(taps) - 1), dtype=np.complex64)
        self.bfo_sample = 0
        self.audio_filter = self.audio_filters.get(mode)
        if mode == "FM" and self.settings.get("fm_deemphasis") is False:
            self.audio_filter = self.audio_filter[:-1]
        self._audio_processing(mode)
        self.fsk = None
        if mode in FSK_MODES:
            self.fsk = RttyDecoder(channel_rate, baud=45.45 if self.settings.get("fsk_baud", 45) == 45 else 50,
                                   reverse=mode == "FSK-R", usos=self.settings.get("fsk_usos", True),
                                   shift=self.settings.get("fsk_shift", 170), newline=self.settings.get("fsk_newline", "any"))
        self.audio_history = np.zeros((len(self.audio_filter), 2)) if self.audio_filter is not None else None
        self.mode = mode
        self.previous_iq = 0j
        self.narrow_iq = None
        self.am_carrier = None
        self.ssb_peak = 0.0
        self.block_offset_hz = None

    def decode(self, iq):
        translated = iq * self.mixer * np.complex64(self.mixer_phase)
        self.mixer_phase *= self.phase_step
        if self.settings.get("noise_blanker") and self.mode not in ("FM", "WFM", "DMR"):
            level = np.mean(np.abs(translated))
            threshold = max(.025, level * (12 - .09 * self.settings.get("nb_level", 45)))
            spikes = np.abs(translated) > threshold
            if np.any(spikes):
                mask = spikes | np.r_[False, spikes[:-1]] | np.r_[spikes[1:], False]
                translated[mask] = 0

        samples = np.concatenate((self.iq_history, translated))
        filtered = signal.upfirdn(self.decimation_filter, samples, down=self.channel_decimation)
        start = self.history_size // self.channel_decimation
        channel = filtered[start:start + IQ_SAMPLES // self.channel_decimation]
        self.narrow_iq = channel if self.mode in ("AM", "FM") else None
        self.iq_history[:] = samples[-self.history_size:]
        channel, self.channel_history = signal.lfilter(
            self.channel_filter, np.array([1], dtype=np.float32), channel, zi=self.channel_history
        )

        # Exclude the head of the block when estimating signal level so the
        # channel filter's short transient cannot hide a hit after a retune.
        power = float(np.mean(np.abs(channel[self.level_start:]) ** 2))
        level = float(10 * np.log10(max(power, 1e-15)))
        if self.fsk is not None:
            self.fsk.feed(channel)
        if self.mode in SSB_MODES or self.mode in CW_MODES or self.mode in FSK_MODES:
            if self.mode in SSB_MODES:
                audio = channel.real
            else:
                pitch = self.cw_pitch if self.mode in CW_MODES else self.settings.get("fsk_pitch", 1615)
                side = -1 if self.mode in ("CW-R", "FSK-R") else 1
                indexes = np.arange(len(channel)) + self.bfo_sample
                audio = (channel * np.exp(2j * np.pi * side * pitch * indexes / CHANNEL_RATE)).real
                self.bfo_sample = (self.bfo_sample + len(channel)) % CHANNEL_RATE
            peak = float(np.max(np.abs(audio)))
            decay = {"fast": .90, "mid": SSB_GAIN_DECAY, "slow": .995}.get(self.agc)
            if decay is not None:
                self.ssb_peak = max(peak, self.ssb_peak * decay)
                audio = audio * (.8 / max(self.ssb_peak, .8 / 64))
        elif self.mode == "AM" or self.mode in SAM_MODES:
            if self.mode == "AM":
                audio = np.abs(channel)
            else:
                carrier, self.carrier_history = signal.sosfilt(self.carrier_filter, channel, zi=self.carrier_history)
                if np.mean(np.abs(carrier[self.level_start:])) < .0002:
                    audio = np.abs(channel)
                else:
                    coherent = channel * np.conj(carrier) / np.maximum(np.abs(carrier), .0002)
                    if self.mode in self.sam_sidebands:
                        coherent, self.sideband_history = signal.lfilter(
                            self.sam_sidebands[self.mode], [1], coherent, zi=self.sideband_history)
                    audio = coherent.real
            carrier = float(np.mean(np.abs(channel[self.level_start:])))
            if self.am_carrier is None:
                self.am_carrier = carrier
                self.audio_history = signal.sosfilt_zi(self.audio_filter) * (float(audio[-1]) / max(carrier, .005))
            elif self.agc != "off":
                decay = {"fast": .6, "mid": .9, "slow": .98}[self.agc]
                self.am_carrier = decay * self.am_carrier + (1 - decay) * carrier
            audio /= max(self.am_carrier if self.agc != "off" else 1.0, .005)
        else:
            previous = np.empty_like(channel)
            previous[0] = self.previous_iq
            previous[1:] = channel[:-1]
            self.previous_iq = channel[-1]
            audio = np.angle(channel * np.conjugate(previous)) * self.discriminator_scale
            # The carrier offset is the DC part of the discriminator output,
            # before the audio filter removes it.
            self.block_offset_hz = float(np.mean(audio[1:], dtype=np.float64)) * self.deviation

        if self.mode == "DMR":
            # The decoder needs 48 kHz discriminator samples with the 4FSK
            # symbols intact. Voice filtering and audio resampling come later.
            pcm = np.clip(audio * (44_000 / self.discriminator_scale), -32767, 32767)
            return level, pcm.astype("<i2").tobytes()
        if self.mode == "WFM" and self.rds is not None:
            self.rds.feed(audio)
        if self.mode == "FM" and self.tone_search is not None:
            self.tone_search.feed(audio)
        if self.audio_filter is not None:
            audio, self.audio_history = signal.sosfilt(self.audio_filter, audio, zi=self.audio_history)
        # The fractional raw IQ block length consumes exactly 320 AF
        # samples for both wide and narrow processing rates.
        audio = audio[::self.audio_decimation]
        if self.post_filter is not None:
            audio, self.post_history = signal.sosfilt(self.post_filter, audio, zi=self.post_history)
        if self.noise_reduction is not None:
            audio = self.noise_reduction.process(audio)
        pcm = (np.clip(audio * 0.65, -1.0, 1.0) * 32767).astype("<i2")
        return level, pcm.tobytes()


class Receiver:
    """An RTL-SDR receiver with continuous capture and bounded IQ/audio blocks."""

    def __init__(self, index=0):
        self.index = index
        self._library = None
        self._device = None
        self._center = None
        self._direct = False
        self._tuning = None
        self._prepared = None
        self._gains = ()
        self._gain = None
        self._nominal_gain = None
        self._headroom_blocks = 0
        self._gain_percent = None
        self._gain_tenths_db = None
        self._decoder = _ChannelDecoder()
        self._auto_revision = None
        self._auto_filter_history = np.zeros(len(self._decoder.filters["FM"]) - 1, dtype=np.complex64)
        self._auto_votes = deque(maxlen=12)
        self._stream = None
        self.iq_listener = None
        self._iq_stream_generation = 0
        self._last_sequence = None
        self._probe_ranges = ()
        self._probe_centers = ()
        self._probe_index = 0
        self._probe_pass = 0
        self._probe_window = np.hanning(PROBE_FFT)
        self._probe_offsets = np.fft.fftshift(np.fft.fftfreq(PROBE_FFT, 1 / IQ_RATE))
        self._waterfall = None
        self._waterfall_at = 0.0
        self._waterfall_sequence = 0
        self._watch_iq = None
        self._watch_responses = {}
        self._meter_noise_bins = ((np.abs(self._probe_offsets) >= 20_000)
                                  & (np.abs(self._probe_offsets) <= 800_000))
        self._meter_offsets = deque(maxlen=15)
        self._reset_tuning_measurement()

    def _call(self, name, *args):
        if not self._device:
            raise ReceiverError("The SDR is closed")
        result = getattr(self._library, "rtlsdr_" + name)(self._device, *args)
        if result != 0:
            raise ReceiverError(f"SDR {name.replace('_', ' ')} failed ({result})")

    def _set_gain(self):
        count = self._library.rtlsdr_get_tuner_gains(self._device, None)
        if count <= 0:
            self._gains = ()
            self._call("set_tuner_gain_mode", 0)
            return
        values = (c.c_int * count)()
        if self._library.rtlsdr_get_tuner_gains(self._device, values) != count:
            raise ReceiverError("Could not read the SDR's tuner gain settings")
        self._call("set_tuner_gain_mode", 1)
        self._gains = tuple(sorted(set(values)))
        self._select_nominal_gain()
        self._apply_gain(self._nominal_gain)

    def _select_nominal_gain(self):
        target = self._gain_tenths_db
        if target is None and self._gain_percent is not None:
            target = self._gains[0] + (self._gains[-1] - self._gains[0]) * self._gain_percent / 100
        if target is None:
            target = TUNER_GAIN_TARGET
        self._nominal_gain = min(self._gains, key=lambda gain: abs(gain - target))

    @property
    def tuner_gain(self):
        """Steps and selected/actual gain in tenths of a dB, if adjustable."""
        if self._direct or not self._gains:
            return None
        return {"steps": self._gains, "target": self._nominal_gain, "actual": self._gain}

    def configure(self, settings):
        """Apply panel changes in the same thread that owns the RTL device."""
        rf_gain = settings.get("rf_gain")
        gain_tenths_db = settings.get("rf_gain_tenths_db")
        if (self._gain_percent, self._gain_tenths_db) != (rf_gain, gain_tenths_db):
            self._gain_percent, self._gain_tenths_db = rf_gain, gain_tenths_db
            if self._gains and not self._direct:
                self._select_nominal_gain()
                if self._nominal_gain != self._gain:
                    self._apply_gain(self._nominal_gain)
        decoder = self._decoder
        keys = ("bandwidth_hz", "agc", "cw_pitch", "pbt1", "pbt2", "if_shape", "fsk_baud", "fsk_shift",
                "fsk_pitch", "fsk_usos", "fsk_newline", "fsk_center", "fsk_tpf", "tone_hpf", "tone_lpf",
                "tone_bass", "tone_treble", "fm_deemphasis", "notch", "audio_peak", "noise_reduction",
                "nr_level", "noise_blanker", "nb_level")
        selected = {key: settings[key] for key in keys if key in settings}
        if selected != decoder.settings:
            decoder.settings = selected
            decoder.bandwidth_hz = selected.get("bandwidth_hz")
            decoder.agc = selected.get("agc", "mid")
            decoder.cw_pitch = selected.get("cw_pitch", 600)
            self._prepared = None

    def process_decoded_audio(self, pcm):
        """Apply the selected AF tone controls after the digital vocoder."""
        decoder = self._decoder
        if decoder.post_filter is None:
            return pcm
        audio = np.frombuffer(pcm, "<i2").astype(np.float64)
        audio, decoder.post_history = signal.sosfilt(decoder.post_filter, audio, zi=decoder.post_history)
        return np.clip(audio, -32768, 32767).astype("<i2").tobytes()

    @property
    def fsk_text(self):
        decoder = self._decoder.fsk
        return decoder.snapshot() if decoder else None

    def _apply_gain(self, gain):
        self._call("set_tuner_gain", gain)
        self._gain = gain
        self._headroom_blocks = 0
        self._watch_iq = None
        self._tuning_noise_dbfs = None
        self._meter_blocks_until_spectrum = 0

    def _gain_for_block(self, raw):
        if self._direct or not self._gains:
            return self._gain
        samples = np.frombuffer(raw, dtype=np.uint8)
        clipped_gain = self._gain_after_clipping(samples)
        if clipped_gain is not None:
            self._headroom_blocks = 0
            return clipped_gain

        if self._gain < self._nominal_gain and samples.min() >= 96 and samples.max() <= 159:
            self._headroom_blocks += 1
            if self._headroom_blocks >= GAIN_RECOVERY_BLOCKS:
                ceiling = min(self._nominal_gain, self._gain + 60)
                return max((gain for gain in self._gains if self._gain < gain <= ceiling),
                           default=min(gain for gain in self._gains if gain > self._gain))
        else:
            self._headroom_blocks = 0
        return self._gain

    def _gain_after_clipping(self, samples):
        clipped = np.count_nonzero((samples <= 1) | (samples >= 254))
        if clipped <= samples.size // 200:
            return None
        # Leave enough headroom for a close transmitter using supported gain steps.
        return max((gain for gain in self._gains if gain <= self._gain - 60), default=self._gains[0])

    def device_count(self):
        """Return the number of RTL-SDR devices currently enumerated by libusb."""
        if self._library is None:
            self._library = _rtl_library()
        return self._library.rtlsdr_get_device_count()

    def open(self):
        if self._device:
            return
        if self.index >= self.device_count():
            raise ReceiverError("No RTL-SDR device is available")
        device = c.c_void_p()
        result = self._library.rtlsdr_open(c.byref(device), self.index)
        if result != 0:
            reason = {-3: "USB permission denied", -6: "USB device is busy"}.get(result, "USB open failed")
            raise ReceiverError(f"Cannot open RTL-SDR: {reason} ({result})")
        self._device = device
        try:
            self._call("set_sample_rate", IQ_RATE)
            self._call("set_agc_mode", 0)
            self._set_gain()
        except ReceiverError:
            self.close()
            raise

    def close(self):
        if self._device:
            if self._stream is not None and not self._stream.stop():
                self._stream.close_device_after_stop()
            else:
                self._library.rtlsdr_close(self._device)
        self._device = None
        self._stream = None
        self._last_sequence = None
        self._center = None
        self._direct = False
        self._tuning = None
        self._prepared = None
        self._watch_iq = None
        self._reset_tuning_measurement()
        self._gains = ()
        self._gain = None
        self._nominal_gain = None
        if self._decoder.rds is not None:
            self._decoder.rds.reset()

    def set_rds_enabled(self, enabled):
        if not enabled:
            self._decoder.rds = None
        elif self._decoder.rds is None:
            self._decoder.rds = RdsDecoder(WFM_CHANNEL_RATE)

    def rds_status(self):
        if self._prepared and self._prepared[1] == "WFM" and self._decoder.rds is not None:
            return self._decoder.rds.status()
        return None

    def set_tone_search(self, enabled):
        if enabled and self._decoder.tone_search is None:
            self._decoder.tone_search = ToneSearch(CHANNEL_RATE)
        elif not enabled:
            self._decoder.tone_search = None

    @property
    def tone_search_active(self):
        return self._decoder.tone_search is not None

    def tone_status(self):
        decoder = self._decoder.tone_search
        return decoder.result if decoder is not None else None

    def reset_tone_search(self):
        if self._decoder.tone_search is not None:
            self._decoder.tone_search.reset()

    @property
    def tuning_offset_hz(self):
        """Estimated signed carrier offset from the selected frequency, when reliable."""
        return self._tuning_offset_hz if self._prepared else None

    @property
    def tuning_snr_db(self):
        """Selected-channel power above the estimated receiver noise floor."""
        return self._tuning_snr_db if self._prepared else None

    @property
    def tuning_noise_dbfs(self):
        """Estimated in-channel noise power, measured with the SNR."""
        return self._tuning_noise_dbfs if self._prepared else None

    @property
    def waterfall(self):
        """Return the most recent display row measured on the audio tuning."""
        return self._waterfall if self._prepared else None

    @property
    def scope_spectrum(self):
        """Full-resolution data for the most recent live scope frame."""
        return self._scope_spectrum if self._prepared else None

    def _reset_tuning_measurement(self):
        self._tuning_offset_hz = None
        self._tuning_snr_db = None
        self._tuning_noise_dbfs = None
        self._nfm_audio_envelope = 0.0
        self._nfm_audio_gain = 1.0
        self._meter_blocks_until_spectrum = 0
        self._meter_offsets.clear()
        self._waterfall = None
        self._scope_spectrum = None
        self._waterfall_at = 0.0
        self._auto_revision = None
        self._auto_discriminator_revision = None

    def _stop_stream(self):
        if self._stream is not None and not self._stream.stop():
            raise ReceiverError("SDR IQ stream did not stop in time")
        self._stream = None
        self._last_sequence = None

    def _start_stream(self):
        self._call("reset_buffer")
        self._stream = _IqStream(self._library, self._device)
        self._iq_stream_generation += 1

    def _read_iq(self):
        if self._stream is None:
            raise ReceiverError("Tune the SDR before reading IQ")
        return self._stream.read()

    def _set_center(self, center, direct, *, restore_gain=None):
        gain_unchanged = direct or restore_gain not in self._gains or restore_gain == self._gain
        if self._stream is not None and (center, direct) == (self._center, self._direct) and gain_unchanged:
            return
        self._watch_iq = None
        self._stop_stream()
        if direct != self._direct:
            self._call("set_direct_sampling", 2 if direct else 0)
            self._direct = direct
            if not direct:
                self._set_gain()
        if not direct and restore_gain in self._gains and restore_gain != self._gain:
            self._apply_gain(restore_gain)
        self._call("set_center_freq", center)
        self._start_stream()
        self._center = center
        self._prepared = None

    def tune(self, hz, mode):
        hz = int(hz)
        if mode not in WATCH_HALF_WIDTH or not 100_000 <= hz <= TUNER_LIMIT:
            raise ReceiverError("Frequency or modulation is outside the SDR receiver configuration")
        if self._tuning != (hz, mode):
            self._reset_tuning_measurement()
        direct = hz < DIRECT_LIMIT
        if (self._center is None or self._direct != direct
                or not _dc_guard(mode) <= abs(hz - self._center) <= 800_000):
            # Moving the carrier away from tuner DC avoids false squelch hits.
            self._set_center(_center_for_frequency(hz, direct), direct)
        if self._prepared != (hz, mode, self._center):
            self._watch_iq = None
            self._decoder.reset(hz - self._center, mode, keep_rds=self._tuning == (hz, mode))
            self._prepared = hz, mode, self._center
            self._last_sequence = None
            self._stream.discard_pending()
        self._tuning = hz, mode

    def read(self):
        """Return channel dBFS and PCM, at 48 kHz for DMR or 24 kHz for voice."""
        if self._tuning is None:
            raise ReceiverError("Tune the SDR before reading audio")
        if self._prepared is None:
            self.tune(*self._tuning)
        sequence, raw = self._read_iq()
        gain = self._gain_for_block(raw)
        gain_changed = gain != self._gain
        if gain_changed:
            self._stop_stream()
            self._apply_gain(gain)
            self._start_stream()
            sequence, raw = self._read_iq()
        if gain_changed or (self._last_sequence is not None and sequence != self._last_sequence + 1):
            # A new stream or missing input invalidates the DSP history.
            hz, mode = self._tuning
            self._decoder.reset(hz - self._center, mode, keep_rds=True)
            self._auto_revision = None
            self._auto_discriminator_revision = None
        self._last_sequence = sequence
        if self.iq_listener is not None:
            self.iq_listener(raw, self._iq_stream_generation, sequence, self._center, self._direct,
                             self._gain, self._tuning)
        self._watch_iq = _decode_iq(raw)
        level, pcm = self._decoder.decode(self._watch_iq)
        self._measure_tuning(level)
        return level, pcm

    def auto_mode_hint(self, tune_revision):
        """Return an analogue mode only after repeated strong IQ evidence."""
        if tune_revision != self._auto_revision:
            self._auto_revision = tune_revision
            self._auto_votes.clear()
            self._auto_filter_history.fill(0)
        if self._decoder.narrow_iq is None:
            return None
        # Both modes use the same analysis bandwidth, independent of the
        # current listening filter and its user-selected settings.
        channel, self._auto_filter_history = signal.lfilter(
            self._decoder.filters["FM"], np.array([1], dtype=np.float32), self._decoder.narrow_iq,
            zi=self._auto_filter_history,
        )
        evidence = None
        if self._tuning_snr_db is not None and self._tuning_snr_db >= AUTO_MIN_SNR_DB:
            evidence = _analog_mode_evidence(channel)
        self._auto_votes.append(evidence)
        return evidence if evidence and self._auto_votes.count(evidence) >= AUTO_VOTES else None

    def auto_discriminator(self, tune_revision):
        """Produce 48 kHz discriminator PCM before analogue voice processing."""
        raw = self._decoder.narrow_iq
        if raw is None:
            return b""
        taps = self._decoder.filters["DMR"]
        sequence = self._last_sequence
        if (tune_revision != self._auto_discriminator_revision
                or sequence is not None and self._auto_discriminator_sequence != sequence - 1):
            self._auto_discriminator_revision = tune_revision
            self._auto_discriminator_history = np.zeros(len(taps) - 1, dtype=np.complex64)
            self._auto_discriminator_previous = 0j
        self._auto_discriminator_sequence = sequence
        channel, self._auto_discriminator_history = signal.lfilter(
            taps, np.array([1], dtype=np.float32), raw, zi=self._auto_discriminator_history,
        )
        previous = np.empty_like(channel)
        previous[0] = self._auto_discriminator_previous
        previous[1:] = channel[:-1]
        self._auto_discriminator_previous = channel[-1]
        pcm = np.angle(channel * np.conjugate(previous)) * 44_000
        return np.clip(pcm, -32767, 32767).astype("<i2").tobytes()

    def level_live_audio(self, pcm):
        """Raise quiet narrow-FM audio for live listening."""
        offset = self._tuning_offset_hz
        snr = self._tuning_snr_db
        if (self._tuning[1] != "FM" or offset is None or abs(offset) > NFM_AUDIO_MAX_OFFSET_HZ
                or snr is None or snr < NFM_AUDIO_MIN_SNR_DB):
            self._nfm_audio_envelope = 0.0
            self._nfm_audio_gain = 1.0
            return pcm

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        rms = float(np.sqrt(np.mean(samples * samples)))
        # A slow envelope release holds through syllable gaps. Below the floor
        # leave the audio untouched so an idle carrier does not bring up hiss.
        self._nfm_audio_envelope = max(rms, self._nfm_audio_envelope * NFM_AUDIO_ENVELOPE_DECAY)
        if rms < NFM_AUDIO_MIN_RMS:
            return pcm

        peak = float(np.max(np.abs(samples)))
        ceiling = max(1.0, .95 / peak)
        gain = max(1.0, min(NFM_AUDIO_TARGET_RMS / self._nfm_audio_envelope, NFM_AUDIO_MAX_GAIN, ceiling))
        start_gain = min(self._nfm_audio_gain, gain)
        self._nfm_audio_gain = start_gain + (gain - start_gain) * NFM_AUDIO_GAIN_RISE
        if self._nfm_audio_gain == 1:
            return pcm
        ramp = np.linspace(start_gain, self._nfm_audio_gain, samples.size, dtype=np.float32)
        return (samples * ramp * 32768).astype("<i2").tobytes()

    def _measure_tuning(self, level):
        hz, mode = self._tuning
        offset = hz - self._center
        if self._meter_blocks_until_spectrum == 0:
            spectrum = self._watch_spectrum(self._watch_iq)
            now = monotonic()
            if now >= self._waterfall_at:
                self._update_waterfall(spectrum, now)
            relative = self._probe_offsets - offset
            noise_bins = self._meter_noise_bins & (np.abs(relative) > WATCH_HALF_WIDTH[mode])
            noise = np.median(spectrum[noise_bins]) * (METER_NOISE_BANDWIDTH[mode] * PROBE_FFT / IQ_RATE)
            self._tuning_noise_dbfs = float(10 * np.log10(max(noise, 1e-15)))
            self._tuning_snr_db = float(level - self._tuning_noise_dbfs)
            self._meter_blocks_until_spectrum = METER_SPECTRUM_INTERVAL
            if mode == "AM":
                self._tuning_offset_hz = self._am_offset(spectrum, relative)
        self._meter_blocks_until_spectrum -= 1

        if self._tuning_snr_db < METER_MIN_EXCESS_DB:
            self._meter_offsets.clear()
            self._tuning_offset_hz = None
        elif mode in ("FM", "WFM", "DMR"):
            self._meter_offsets.append(self._decoder.block_offset_hz)
            if len(self._meter_offsets) >= (6 if mode == "WFM" else 4):
                self._tuning_offset_hz = sum(self._meter_offsets) / len(self._meter_offsets)

    def _update_waterfall(self, spectrum, now):
        # Send the usable IQ window so the display can pan around the receive frequency.
        low_limit, high_limit = (100_000, DIRECT_LIMIT) if self._direct else (DIRECT_LIMIT, TUNER_LIMIT)
        low = max(low_limit, self._center - PROBE_HALF_WIDTH)
        high = min(high_limit, self._center + PROBE_HALF_WIDTH)
        span = high - low
        edges = np.linspace(low - self._center, high - self._center, WATERFALL_BINS + 1)
        boundaries = np.searchsorted(self._probe_offsets, edges)
        first, last = boundaries[0], boundaries[-1]
        visible_power = spectrum[first:last]
        # The tuner can leave a strong DC spike at its LO, which is offset
        # from the selected frequency. Do not show it as a received carrier.
        dc = PROBE_FFT // 2 - first
        if -3 <= dc < len(visible_power) + 3:
            visible_power = visible_power.copy()
            dc_left = spectrum[PROBE_FFT // 2 - 5]
            dc_right = spectrum[PROBE_FFT // 2 + 5]
            visible_power[max(0, dc - 3):dc + 4] = (dc_left + dc_right) / 2
        grouped = np.maximum.reduceat(visible_power, boundaries[:-1] - first)
        levels = np.rint(10 * np.log10(np.clip(grouped, 1e-12, 1))).astype(int)
        self._waterfall_sequence += 1
        self._waterfall = {"sequence": self._waterfall_sequence, "center_hz": low + span // 2,
                           "span_hz": span, "levels": tuple(levels.tolist()), "time_ms": round(now * 1000)}
        self._scope_spectrum = ScopeSpectrum(self._waterfall_sequence, self._waterfall["time_ms"],
                                             self._center, low, high, spectrum)
        self._waterfall_at = now + WATERFALL_INTERVAL

    @staticmethod
    def _am_offset(spectrum, relative):
        # Broadcast and voice AM retain a narrow carrier, even when their
        # sidebands are asymmetric. Interpolate the Hann-windowed peak.
        start, stop = np.searchsorted(relative, [-4_500, 4_500])
        peak = start + np.argmax(spectrum[start:stop])
        window = spectrum[peak - 1:peak + 2]
        if window.sum() < 0.25 * spectrum[start:stop].sum():
            return None
        return float(relative[0] + _interpolated_fft_bin(spectrum, peak) * IQ_RATE / PROBE_FFT)

    def _watch_spectrum(self, iq):
        windows = iq[:3 * PROBE_FFT].reshape(3, PROBE_FFT)
        spectrum = np.fft.fftshift(np.fft.fft(windows * self._probe_window, axis=1), axes=1)
        window_energy = PROBE_FFT * np.sum(self._probe_window ** 2)
        return np.mean(np.abs(spectrum) ** 2, axis=0) / window_energy

    def _watch_level(self, power, offset, mode):
        half_width = WATCH_HALF_WIDTH[mode]
        sideband = mode in SSB_MODES
        response = self._watch_responses.get(mode)
        if response is None:
            if sideband:
                frequencies = np.linspace(-half_width, half_width, 145)
                _, weights = signal.freqz(self._decoder.decimation_filters[mode], worN=frequencies, fs=IQ_RATE)
            else:
                frequencies, weights = signal.freqz(self._decoder.decimation_filters[mode],
                                                   worN=PROBE_FFT, fs=IQ_RATE)
                stop = np.searchsorted(frequencies, half_width, side="right") + 1
                frequencies, weights = frequencies[:stop], weights[:stop]
            if mode != "WFM":
                _, channel_weights = signal.freqz(self._decoder.filters[mode], worN=frequencies,
                                                  fs=CHANNEL_RATE)
                weights *= channel_weights
            response = frequencies, np.abs(weights) ** 2
            self._watch_responses[mode] = response

        left, right = np.searchsorted(self._probe_offsets, [offset - half_width, offset + half_width])
        relative = self._probe_offsets[left:right] - offset
        weights = np.interp(relative if sideband else np.abs(relative), *response)
        channel_power = np.dot(power[left:right], weights)
        return float(10 * np.log10(max(channel_power, 1e-15)))

    def watch_levels(self, targets):
        """Measure covered channels from the latest primary audio block without retuning."""
        if self._watch_iq is None:
            return {}
        covered = []
        for hz, mode in targets:
            offset = abs(hz - self._center)
            if (mode in WATCH_HALF_WIDTH and 100_000 <= hz <= TUNER_LIMIT
                    and (hz < DIRECT_LIMIT) == self._direct and _dc_guard(mode) <= offset <= 800_000
                    and offset + WATCH_HALF_WIDTH[mode] <= PROBE_HALF_WIDTH):
                covered.append((hz, mode))
        if not covered:
            return {}
        spectrum = self._watch_spectrum(self._watch_iq)
        return {(hz, mode): self._watch_level(spectrum, hz - self._center, mode)
                for hz, mode in covered}

    def watch_probe(self, target, *, restore=True):
        """Check one channel, optionally leaving the capture window on it."""
        hz, mode = target
        if mode not in WATCH_HALF_WIDTH or not 100_000 <= hz <= TUNER_LIMIT:
            return None
        direct = hz < DIRECT_LIMIT
        center = _center_for_frequency(hz, direct)
        previous = self._center, self._direct
        previous_gain = self._gain
        if not restore:
            self._tuning = self._prepared = self._watch_iq = None
        try:
            self._set_center(center, direct)
            _, raw = self._read_iq()  # Stream startup has already discarded settling samples.
            spectrum = self._watch_spectrum(_decode_iq(raw))
            return self._watch_level(spectrum, hz - center, mode)
        finally:
            if restore and self._tuning is not None and previous[0] is not None:
                self._set_center(*previous, restore_gain=previous_gain)

    def nearby_live_peak(self, band, lower, upper, exact, blocked, squelch):
        """Check the part of a Nearby window already captured by the audio tuner."""
        if self._watch_iq is None or self._center is None:
            return None
        margin = PROBE_HALF_WIDTH - WATCH_HALF_WIDTH[band.modulation]
        lower = max(lower, self._center - margin)
        upper = min(upper, self._center + margin)
        if self._direct:
            upper = min(upper, DIRECT_LIMIT - 1)
        else:
            lower = max(lower, DIRECT_LIMIT)
        if lower > upper:
            return None
        return self._nearby_channel(self._watch_iq, self._center, band, lower, upper, exact, blocked, squelch)

    def nearby_peak(self, center, band, lower, upper, exact, blocked, squelch, *, preserve=False):
        """Find the strongest eligible channel, optionally preserving the listening channel."""
        half_width = WATCH_HALF_WIDTH[band.modulation]
        direct = center < DIRECT_LIMIT
        previous = self._center, self._direct
        if self._center is not None and self._direct == direct:
            covers_window = max(abs(lower - self._center), abs(upper - self._center)) + half_width <= PROBE_HALF_WIDTH
            if exact:
                dc_clear = all(abs(hz - self._center) >= NEARBY_DC_GUARD_HZ for hz in exact if hz not in blocked)
            else:
                # Keep the sweep's alternating centers when the current LO hides part of the tile.
                dc_clear = not lower - NEARBY_DC_GUARD_HZ < self._center < upper + NEARBY_DC_GUARD_HZ
            if covers_window and dc_clear:
                center = self._center
        if preserve and (center, direct) == previous and self._watch_iq is not None:
            return self._nearby_channel(self._watch_iq, center, band, lower, upper, exact, blocked, squelch)

        restore = preserve and self._tuning is not None and previous[0] is not None
        previous_gain = self._gain
        try:
            if not preserve and self._nominal_gain is not None:
                # A gain reduction for one window should not desensitize the next window.
                self._set_center(center, direct, restore_gain=self._nominal_gain)
            else:
                self._set_center(center, direct)
            _, raw = self._read_iq()
            if not direct and self._gains:
                clipped_gain = self._gain_after_clipping(np.frombuffer(raw, dtype=np.uint8))
                if clipped_gain is not None and clipped_gain < self._gain:
                    self._stop_stream()
                    self._apply_gain(clipped_gain)
                    self._start_stream()
                    _, raw = self._read_iq()
            return self._nearby_channel(_decode_iq(raw), center, band, lower, upper, exact, blocked, squelch)
        finally:
            if restore and ((center, direct) != previous or self._gain != previous_gain):
                self._set_center(*previous, restore_gain=previous_gain)
            elif not preserve:
                self._tuning = self._prepared = self._watch_iq = None

    def _nearby_channel(self, iq, center, band, lower, upper, exact, blocked, squelch):
        mode = band.modulation
        half_width = WATCH_HALF_WIDTH[mode]
        power = self._watch_spectrum(iq)
        frequencies = self._probe_offsets + center
        usable = ((np.abs(self._probe_offsets) >= NEARBY_DC_GUARD_HZ)
                  & (np.abs(self._probe_offsets) <= PROBE_HALF_WIDTH))
        floor = np.median(power[usable]) * (METER_NOISE_BANDWIDTH[mode] * PROBE_FFT / IQ_RATE)
        threshold = max(squelch, 10 * np.log10(max(floor, 1e-15)) + NEARBY_MIN_EXCESS_DB)
        if exact:
            candidates = dict.fromkeys(exact)
        else:
            in_window = usable & (frequencies >= lower) & (frequencies <= upper)
            peaks = signal.find_peaks(power)[0]
            candidates = {}
            for index in peaks[in_window[peaks]]:
                hz = self._nearby_channel_hz(round(float(frequencies[index])), band)
                candidates[hz] = max(candidates.get(hz, 0), power[index])
        best_hz, best_level = None, threshold
        min_aligned_peak = NEARBY_MIN_PEAK_FRACTION * 10 ** (threshold / 10)
        for hz, aligned_peak in candidates.items():
            offset = abs(hz - center)
            if (hz in blocked or not lower <= hz <= upper
                    or NEARBY_BROADCAST_FM.lower <= hz <= NEARBY_BROADCAST_FM.upper
                    or not NEARBY_DC_GUARD_HZ <= offset <= PROBE_HALF_WIDTH - half_width
                    or aligned_peak is not None and aligned_peak < min_aligned_peak):
                continue
            level = self._watch_level(power, hz - center, mode)
            if level < best_level:
                continue
            # Reject a weak spectral sidelobe whose audio filter mostly sees the adjacent carrier.
            candidate_min = NEARBY_MIN_PEAK_FRACTION * 10 ** (level / 10)
            if aligned_peak is not None and aligned_peak < candidate_min:
                continue
            best_hz, best_level, min_aligned_peak = hz, level, candidate_min
        if best_hz is None:
            return None
        gain_db = (self._gain or 0) / 10 if center >= DIRECT_LIMIT else 0
        return NearbyPeak(best_hz, best_level, best_level - gain_db)

    @staticmethod
    def _nearby_channel_hz(hz, band):
        # The broad UHF grid includes interstitial frequencies. Score PMR446
        # on its own channel centers so voice sidebands cannot win as another channel.
        slot = (hz - PMR[0] + NEARBY_PMR_STEP // 2) // NEARBY_PMR_STEP
        return PMR[slot] if 0 <= slot < len(PMR) else band.nearest(hz, band.step)

    def _probe_peak(self, iq, center, low, high):
        windows = iq[:3 * PROBE_FFT].reshape(3, PROBE_FFT)
        spectrum = np.fft.fftshift(np.fft.fft(windows * self._probe_window, axis=1), axes=1)
        power = np.mean(np.abs(spectrum / self._probe_window.sum()) ** 2, axis=0)
        frequencies = center + self._probe_offsets
        usable = ((np.abs(self._probe_offsets) >= 18_000) &
                  (np.abs(self._probe_offsets) <= PROBE_HALF_WIDTH) &
                  (frequencies >= low) & (frequencies <= high))
        if not np.any(usable):
            return None
        peak = int(np.argmax(np.where(usable, power, -np.inf)))
        noise = float(np.median(power[usable]))
        nearby = power[max(0, peak - 10):peak + 11]
        # A high peak over the local floor and a minimum integrated carrier
        # level keep this detector focused on strong nearby activity.
        if power[peak] < 100 * noise or nearby.sum() < 10 ** (-43 / 10):
            return None
        return round(float(frequencies[peak]))

    def probe(self, ranges, windows=1):
        """Check successive Close Call windows and return the first strong RF peak.

        Ranges are inclusive (lower Hz, upper Hz) pairs. Each check moves a
        single tuner, so subsequent read() calls resume the audio channel.
        """
        ranges = tuple((int(low), int(high)) for low, high in ranges)
        if ranges != self._probe_ranges:
            self._probe_ranges = ranges
            self._probe_centers = tuple(
                (left + min(PROBE_STRIDE, high - left) // 2, low, high)
                for low, high in ranges
                for left in range(low, high + 1, PROBE_STRIDE)
            )
            self._probe_index = 0
            self._probe_pass = 0

        for _ in range(min(windows, len(self._probe_centers))):
            center, low, high = self._probe_centers[self._probe_index]
            if self._probe_pass % 2:
                center += 60_000  # Cover the DC notch from the preceding pass.
            self._probe_index += 1
            if self._probe_index == len(self._probe_centers):
                self._probe_index = 0
                self._probe_pass += 1
            self._set_center(center, center < DIRECT_LIMIT)
            _, raw = self._read_iq()
            iq = _decode_iq(raw)
            self._prepared = None
            peak = self._probe_peak(iq, center, low, high)
            if peak is not None:
                return peak
        return None

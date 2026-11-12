"""Receive AM or narrowband FM from a local RTL-SDR.

Only the radio worker should use a Receiver instance. Level measurements are
channel power relative to ADC full scale. Audio is signed little-endian PCM.
"""

from __future__ import annotations

import ctypes as c
from collections import deque
from ctypes.util import find_library
from threading import Condition, Event, Thread
from time import monotonic

import numpy as np
from scipy import signal


SAMPLE_RATE = 24_000
IQ_RATE = 2_400_000
CHANNEL_RATE = 2 * SAMPLE_RATE
CHANNEL_DECIMATION = IQ_RATE // CHANNEL_RATE
IQ_SAMPLES = 32_000
SETTLE_SAMPLES = 15_360
TUNER_OFFSET = 300_000
DIRECT_LIMIT = 25_000_000
TUNER_LIMIT = 1_750_000_000
PROBE_FFT = 8192
PROBE_HALF_WIDTH = 900_000
PROBE_STRIDE = 1_600_000
NFM_DEEMPHASIS_SECONDS = 200e-6
TUNER_GAIN_TARGET = 280  # Librtlsdr expresses gain in tenths of a dB.
GAIN_RECOVERY_BLOCKS = IQ_RATE // IQ_SAMPLES
IQ_BLOCK_BYTES = 2 * IQ_SAMPLES
USB_TRANSFER_BYTES = 16_384
USB_TRANSFER_COUNT = 8
MAX_PENDING_BLOCKS = 6
IQ_CALLBACK = c.CFUNCTYPE(None, c.POINTER(c.c_ubyte), c.c_uint32, c.c_void_p)


class ReceiverError(RuntimeError):
    """The local radio could not complete a hardware operation."""


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


class _ChannelDecoder:
    def __init__(self):
        self.decimation_filter = signal.firwin(
            1101, 15_000, fs=IQ_RATE, window=("kaiser", 8.0)
        ).astype(np.float32)
        self.filters = {
            "AM": signal.firwin(121, 3700, fs=CHANNEL_RATE).astype(np.float32),
            "FM": signal.firwin(121, 5300, fs=CHANNEL_RATE).astype(np.float32),
        }
        # Narrow FM voice usually carries boosted treble and may carry a
        # subaudible tone. Filter both after the discriminator.
        deemphasis = np.exp(-1 / (CHANNEL_RATE * NFM_DEEMPHASIS_SECONDS))
        self.audio_filters = {
            "AM": signal.butter(3, [250, 3400], fs=CHANNEL_RATE, btype="bandpass", output="sos"),
            "FM": np.vstack((
                signal.butter(5, [320, 3000], fs=CHANNEL_RATE, btype="bandpass", output="sos"),
                [1 - deemphasis, 0, 0, 1, -deemphasis, 0],
            )),
        }
        self.sample_indexes = np.arange(IQ_SAMPLES, dtype=np.float64)
        history_blocks = (len(self.decimation_filter) - 2) // CHANNEL_DECIMATION + 1
        self.history_size = history_blocks * CHANNEL_DECIMATION
        self.reset(0, "FM")

    def reset(self, offset_hz, mode):
        self.mixer = np.exp((-2j * np.pi * offset_hz / IQ_RATE) * self.sample_indexes).astype(np.complex64)
        self.mixer_phase = 1 + 0j
        self.phase_step = np.exp((-2j * np.pi * offset_hz * IQ_SAMPLES) / IQ_RATE)
        self.iq_history = np.zeros(self.history_size, dtype=np.complex64)
        self.channel_history = np.zeros(len(self.filters[mode]) - 1, dtype=np.complex64)
        self.audio_filter = self.audio_filters[mode]
        self.audio_history = np.zeros((len(self.audio_filter), 2))
        self.mode = mode
        self.previous_iq = 0j
        self.am_carrier = None

    def decode(self, iq):
        translated = iq * self.mixer * np.complex64(self.mixer_phase)
        self.mixer_phase *= self.phase_step

        samples = np.concatenate((self.iq_history, translated))
        filtered = signal.upfirdn(self.decimation_filter, samples, down=CHANNEL_DECIMATION)
        start = self.history_size // CHANNEL_DECIMATION
        channel = filtered[start:start + IQ_SAMPLES // CHANNEL_DECIMATION]
        self.iq_history[:] = samples[-self.history_size:]
        channel, self.channel_history = signal.lfilter(
            self.filters[self.mode], np.array([1], dtype=np.float32), channel, zi=self.channel_history
        )

        # Exclude the head of the block when estimating signal level so the
        # channel filter's short transient cannot hide a hit after a retune.
        power = float(np.mean(np.abs(channel[120:]) ** 2))
        level = float(10 * np.log10(max(power, 1e-15)))
        if self.mode == "AM":
            audio = np.abs(channel)
            carrier = float(np.mean(audio[120:]))
            if self.am_carrier is None:
                self.am_carrier = carrier
                self.audio_history = signal.sosfilt_zi(self.audio_filter) * (carrier / max(carrier, 0.005))
            else:
                self.am_carrier = 0.9 * self.am_carrier + 0.1 * carrier
            audio /= max(self.am_carrier, 0.005)
        else:
            previous = np.empty_like(channel)
            previous[0] = self.previous_iq
            previous[1:] = channel[:-1]
            self.previous_iq = channel[-1]
            audio = np.angle(channel * np.conjugate(previous)) * (CHANNEL_RATE / (2 * np.pi * 2500))

        audio, self.audio_history = signal.sosfilt(self.audio_filter, audio, zi=self.audio_history)
        pcm = (np.clip(audio[::2] * 0.65, -1.0, 1.0) * 32767).astype("<i2")
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
        self._decoder = _ChannelDecoder()
        self._stream = None
        self._last_sequence = None
        self._probe_ranges = ()
        self._probe_centers = ()
        self._probe_index = 0
        self._probe_pass = 0
        self._probe_window = np.hanning(PROBE_FFT)
        self._probe_offsets = np.fft.fftshift(np.fft.fftfreq(PROBE_FFT, 1 / IQ_RATE))

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
        self._nominal_gain = min(self._gains, key=lambda gain: abs(gain - TUNER_GAIN_TARGET))
        self._apply_gain(self._nominal_gain)

    def _apply_gain(self, gain):
        self._call("set_tuner_gain", gain)
        self._gain = gain
        self._headroom_blocks = 0

    def _gain_for_block(self, raw):
        if self._direct or not self._gains:
            return self._gain
        samples = np.frombuffer(raw, dtype=np.uint8)
        clipped = np.count_nonzero((samples <= 1) | (samples >= 254))
        if clipped > samples.size // 200:
            self._headroom_blocks = 0
            # Leave enough headroom for a close transmitter. Move through
            # the tuner gain table instead of assuming a continuous gain.
            return max((gain for gain in self._gains if gain <= self._gain - 60),
                       default=self._gains[0])

        if self._gain < self._nominal_gain and samples.min() >= 96 and samples.max() <= 159:
            self._headroom_blocks += 1
            if self._headroom_blocks >= GAIN_RECOVERY_BLOCKS:
                ceiling = min(self._nominal_gain, self._gain + 60)
                return max((gain for gain in self._gains if self._gain < gain <= ceiling),
                           default=min(gain for gain in self._gains if gain > self._gain))
        else:
            self._headroom_blocks = 0
        return self._gain

    def open(self):
        if self._device:
            return
        if self._library is None:
            self._library = _rtl_library()
        if self.index >= self._library.rtlsdr_get_device_count():
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
        self._gains = ()
        self._gain = None
        self._nominal_gain = None

    def _stop_stream(self):
        if self._stream is not None and not self._stream.stop():
            raise ReceiverError("SDR IQ stream did not stop in time")
        self._stream = None
        self._last_sequence = None

    def _start_stream(self):
        self._call("reset_buffer")
        self._stream = _IqStream(self._library, self._device)

    def _read_iq(self):
        if self._stream is None:
            raise ReceiverError("Tune the SDR before reading IQ")
        return self._stream.read()

    def _set_center(self, center, direct):
        if (center, direct) == (self._center, self._direct):
            return
        self._stop_stream()
        if direct != self._direct:
            self._call("set_direct_sampling", 2 if direct else 0)
            if not direct:
                self._set_gain()
        self._call("set_center_freq", center)
        self._start_stream()
        self._center = center
        self._direct = direct
        self._prepared = None

    def tune(self, hz, mode):
        hz = int(hz)
        if mode not in ("AM", "FM") or not 100_000 <= hz <= TUNER_LIMIT:
            raise ReceiverError("Frequency or modulation is outside the SDR receiver configuration")
        direct = hz < DIRECT_LIMIT
        if self._center is None or self._direct != direct or not 20_000 <= abs(hz - self._center) <= 800_000:
            # Moving the carrier away from tuner DC avoids false squelch hits.
            center = hz + TUNER_OFFSET if hz + TUNER_OFFSET <= TUNER_LIMIT else hz - TUNER_OFFSET
            if direct and center >= DIRECT_LIMIT:
                center = hz - TUNER_OFFSET
            self._set_center(center, direct)
        if self._prepared != (hz, mode, self._center):
            self._decoder.reset(hz - self._center, mode)
            self._prepared = hz, mode, self._center
            self._last_sequence = None
            self._stream.discard_pending()
        self._tuning = hz, mode

    def read(self):
        """Return (channel dBFS, a 24 kHz mono PCM block)."""
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
            self._decoder.reset(hz - self._center, mode)
        self._last_sequence = sequence
        return self._decoder.decode(_decode_iq(raw))

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

"""A receive-only DSD-neo bridge for the IC-R8600 digital voice modes."""

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Thread
import time

import numpy as np
from scipy import signal

from dmr_decoder import (DmrDecoderError, _SlotDecoder, _RESAMPLE_FILTER, _DISCRIMINATOR_DC_POLE,
                         _metadata as _dmr_metadata, decoder_path)


@dataclass(frozen=True)
class _Mode:
    preset: str
    status_name: str
    call_name: str
    sync_types: tuple[int, ...]
    code_name: str | None = None
    code_id: int = 6
    code_min: int = 0
    code_max: int = 0


_DIGITAL = {
    "P25": _Mode("-f1", "P25p1", "P25p1", (0, 1), "NAC", 3, 1, 0xFFE),
    "NXDN-VN": _Mode("-fi", "NXDN", "NXDN", (28, 29), "RAN", 2, 0, 63),
    "D-STAR": _Mode("-fd", "DSTAR", "D-STAR", (6, 7, 18, 19)),
    "dPMR": _Mode("-fm", "dPMR", "dPMR", tuple(range(20, 28))),
}

DIGITAL_MODES = frozenset(_DIGITAL)
_CONFIRM_SECONDS = 0.25
_PCM_FRAME_BYTES = 320  # 20 ms of mono 8 kHz PCM16.
AUTO_VOICE_MODES = ("DMR", "P25", "NXDN-VN")
_AUTO_PROBE_SECONDS = 2.25
_NXDN_CONFIRM_SECONDS = 1.5
_AUTO_BUFFER_BLOCKS = 32
_AUTO_CONFIRM_PREROLL_BLOCKS = 10


def _bounded_code(value, lower, upper):
    return value if type(value) is int and lower <= value <= upper else None


def _label(value):
    if isinstance(value, str):
        value = value.strip()
        if value and len(value) <= 64 and value.isprintable():
            return value
    return None


def _metadata(mode, setting, status, calls, code):
    synced = status.get("protocol") == setting.status_name and status.get("synctype") in setting.sync_types
    result = dict(mode=mode, sync=synced, voice=False, encrypted=None, receive_kind=setting.code_name,
                  receive_code=None, slot=None, color_code=None, unit_id=None, talkgroup=None,
                  source=None, destination=None, errors=None)
    if not synced:
        return result, None
    for call in calls.get("slots", ()):
        if (call.get("slot") != 0 or call.get("state") != 2 or call.get("kind") not in (1, 2, 3)
                or call.get("protocol_label") != setting.call_name):
            continue
        known = call.get("receive_known") is True and call.get("receive_kind") == setting.code_id
        received_code = _bounded_code(call.get("receive_value"), setting.code_min, setting.code_max) if known else None
        algorithm = call.get("algid")
        explicit_alg = type(algorithm) is int
        encrypted = call.get("enc") is True or (explicit_alg and algorithm not in (0, 0x80))
        clear = call.get("enc") is False and explicit_alg and algorithm in (0, 0x80)
        source = _label(call.get("src_text"))
        destination = _label(call.get("tg_text"))
        unit_id = _bounded_code(call.get("src_id"), 1, 0xFFFFFF)
        talkgroup = _bounded_code(call.get("tg_id"), 1, 0xFFFFFF) if call.get("kind") == 2 else None
        if not (unit_id or source or destination):
            clear = False
        result.update(voice=code is None or received_code == code,
                      encrypted=True if encrypted else False if clear else None, receive_code=received_code,
                      unit_id=unit_id, talkgroup=talkgroup, source=source, destination=destination)
        return result, call.get("elapsed_ms")
    return result, None


def _audio_identity(metadata):
    if metadata["voice"] and metadata["encrypted"] is False:
        return tuple(metadata[key] for key in ("source", "destination", "unit_id", "talkgroup", "receive_code"))
    return None


def _verified_auto_mode(status, calls, *, nxdn48=False):
    """Promote only protocols with validated receive data and a clear voice identity."""
    protocol = status.get("protocol")
    if protocol == "DMR" and not nxdn48:
        mode = "DMR"
        result = _dmr_metadata(status, calls, {})
    else:
        mode = "NXDN-VN" if protocol == "NXDN" else "P25"
        if nxdn48 and mode != "NXDN-VN":
            return None
        result, _ = _metadata(mode, _DIGITAL[mode], status, calls, None)
        if result["receive_code"] is None:
            return None
    if (result["sync"] and result["voice"] and result["encrypted"] is False
            and _bounded_code(result["unit_id"], 1, 0xFFFFFF) is not None):
        # Native AUTO uses the same NXDN status code for both symbol rates.
        return "NXDN" if mode == "NXDN-VN" and not nxdn48 else mode
    return None


class _AutoDigitalProbe(_SlotDecoder):
    """Use the native automatic decoder's telemetry without forwarding its audio."""

    def __init__(self, executable, *, nxdn48=False):
        self._dc = np.zeros(1)
        self._nxdn48 = nxdn48
        super().__init__(executable, preset="-fi" if nxdn48 else "-fa")

    def process(self, discriminator):
        samples = np.frombuffer(discriminator, dtype="<i2")
        filtered, self._dc = signal.lfilter([1, -1], [1, -_DISCRIMINATOR_DC_POLE], samples, zi=self._dc)
        return super().process(np.clip(filtered, -32768, 32767).astype("<i2").tobytes())

    def _read_message(self, row):
        pass

    def _read_result(self):
        self._output.clear()
        return _verified_auto_mode(self._status, self._calls, nxdn48=self._nxdn48)


class AutoDigitalDetector:
    """Acquisition off the live audio thread, with at most one probe per carrier."""

    def __init__(self):
        self._condition = Condition()
        self._blocks = deque(maxlen=_AUTO_BUFFER_BLOCKS)
        self._key = None
        self._generation = 0
        self._finished = False
        self._result = None
        self._closed = False
        self._worker = None

    def _select(self, key):
        if self._key != key:
            self._key = key
            self._generation += 1
            self._blocks.clear()
            self._finished = False
            self._result = None
            self._condition.notify_all()

    def observe(self, key, pcm):
        with self._condition:
            if self._closed:
                return None
            self._select(key)
            if not self._finished and pcm:
                self._blocks.append(pcm)
                self._condition.notify_all()
            if self._worker is None and pcm:
                self._worker = Thread(target=self._run, name="scanner-auto-digital", daemon=True)
                self._worker.start()
            return self._result

    def reset(self):
        with self._condition:
            self._select(None)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=3)

    def _run(self):
        generation = 0
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._generation != generation)
                if self._closed:
                    return
                generation, key = self._generation, self._key
            if key is None:
                continue
            result = self._probe(generation)
            with self._condition:
                if generation == self._generation:
                    self._result = result
                    self._finished = True
                    self._blocks.clear()

    def _probe(self, generation):
        if (executable := decoder_path()) is None:
            return None
        recent = deque(maxlen=_AUTO_CONFIRM_PREROLL_BLOCKS)
        mode = self._listen(generation, executable, recent)
        if mode == "NXDN":
            return self._listen(generation, executable, recent, nxdn48=True)
        return mode

    def _listen(self, generation, executable, recent, *, nxdn48=False):
        deadline = time.monotonic() + (_NXDN_CONFIRM_SECONDS if nxdn48 else _AUTO_PROBE_SECONDS)
        buffered = deque(recent) if nxdn48 else deque()
        probe = None
        try:
            probe = _AutoDigitalProbe(executable, nxdn48=nxdn48)
            while (remaining := deadline - time.monotonic()) > 0:
                with self._condition:
                    self._condition.wait_for(lambda: buffered or self._blocks
                                             or generation != self._generation or self._closed, timeout=remaining)
                    if self._closed or generation != self._generation or not (buffered or self._blocks):
                        return None
                    pcm = (buffered or self._blocks).popleft()
                recent.append(pcm)
                if mode := probe.process(pcm):
                    return mode
        except (DmrDecoderError, OSError):
            # Optional decoding has no effect on the analogue receive path.
            return None
        finally:
            if probe is not None:
                probe.close()
        return None


class DigitalDecoder(_SlotDecoder):
    """Decode the selected conventional mode from 48 kHz PCM16 discriminator samples."""

    def __init__(self, mode, *, code=None, executable=None):
        setting = _DIGITAL.get(mode)
        if setting is None:
            raise ValueError("Select a supported digital receive mode.")
        if code is not None and (setting.code_name is None
                                 or _bounded_code(code, setting.code_min, setting.code_max) is None):
            raise ValueError(f"Enter a valid receive code for {mode}.")
        binary = executable or decoder_path()
        if binary is None:
            raise DmrDecoderError("Install DSD-neo 2.12.1 to receive digital voice.")
        self.mode = mode
        self._setting = setting
        self._code = code
        self._dc = np.zeros(1)
        self._pending_audio = deque()
        self._elapsed = None
        # The narrow dPMR discriminator has already passed the receiver's
        # channel filter. Applying the native input filter again smears its CCH.
        flags = ("-l",) if mode == "dPMR" else ()
        if code is not None:
            value = f"0x{code:03x}" if setting.code_name == "NAC" else str(code)
            flags += ("--" + setting.code_name.lower() + "-allow", value)
        super().__init__(str(Path(binary).resolve()), preset=setting.preset, receive_flags=flags)

    def process(self, discriminator):
        if discriminator:
            samples = np.frombuffer(discriminator, dtype="<i2")
            filtered, self._dc = signal.lfilter([1, -1], [1, -_DISCRIMINATOR_DC_POLE], samples, zi=self._dc)
            if self.mode == "dPMR":
                filtered *= .4
            discriminator = np.clip(filtered, -32768, 32767).astype("<i2").tobytes()
        return super().process(discriminator)

    def _read_message(self, row):
        # The inherited transport also receives native event rows.
        # Conventional DMR is the scanner's separate text-message path.
        pass

    def _changed_call(self, identity, elapsed):
        return (identity != self._audio_identity or
                type(elapsed) is int and type(self._elapsed) is int and elapsed < self._elapsed)

    def _reset_audio(self, identity, elapsed):
        self._audio_identity, self._elapsed = identity, elapsed
        self._pending_audio.clear()
        complete = len(self._output) // _PCM_FRAME_BYTES * _PCM_FRAME_BYTES
        del self._output[:complete]
        self._resample_state.fill(0)

    def _status_updated(self):
        if self._audio_identity is not None:
            metadata, elapsed = _metadata(self.mode, self._setting, self._status, self._calls, self._code)
            identity = _audio_identity(metadata)
            if self._changed_call(identity, elapsed):
                # A brief change can otherwise be masked by the next buffered update.
                self._reset_audio(None, None)

    def _read_result(self):
        metadata, elapsed = _metadata(self.mode, self._setting, self._status, self._calls, self._code)
        pcm = self._play_clear_audio(metadata, elapsed, time.monotonic())
        return metadata, pcm

    def _play_clear_audio(self, metadata, elapsed, now):
        identity = _audio_identity(metadata)
        if identity is None or self._changed_call(identity, elapsed):
            self._reset_audio(identity, elapsed)
            return b""
        self._elapsed = elapsed
        length = len(self._output) // _PCM_FRAME_BYTES * _PCM_FRAME_BYTES
        samples = self._output[:length]
        del self._output[:length]
        if samples:
            self._pending_audio.append((now, samples))
        ready = bytearray()
        while self._pending_audio and self._pending_audio[0][0] <= now - _CONFIRM_SECONDS:
            ready.extend(self._pending_audio.popleft()[1])
        if not ready:
            return b""
        mono = np.frombuffer(ready, dtype="<i2")
        upsampled = np.zeros(3 * len(mono))
        upsampled[::3] = mono
        pcm, self._resample_state = signal.lfilter(_RESAMPLE_FILTER, [1.0], upsampled, zi=self._resample_state)
        return np.clip(np.rint(pcm), -32768, 32767).astype("<i2").tobytes()

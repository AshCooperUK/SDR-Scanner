"""Bridge the RTL-SDR discriminator to a local DSD-neo conventional DMR decoder."""

from collections import deque
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from unicodedata import category

import numpy as np
from scipy import signal


_DMR_SYNC = frozenset((10, 11, 12, 13, 32, 33, 34))
_DMR_MOBILE_VOICE = 32
_MAX_MESSAGE_TEXT = 512
_MAX_PENDING_MESSAGES = 24
_MESSAGE_DUPLICATE_SECONDS = 0.75
_TMS_NOTICE = re.compile(r"(?:^|; )(?:ETSI TMS|VTX TMS|TMS) SRC: \d+; DST: \d+;")
_PCM_INPUT_LIMIT = 48_000  # Half a second of 48 kHz PCM16.
_TELEMETRY_LIMIT = 262_144
_TELEMETRY_TIMEOUT = 0.7
_OUTPUT_FRAME_BYTES = 4 * 160  # 20 ms of stereo PCM16 at 8 kHz.
_RESAMPLE_FILTER = 3 * signal.firwin(49, 1 / 3)
# Let a short data burst keep its symbol balance during acquisition.
_DISCRIMINATOR_DC_POLE = np.exp(-1 / (48_000 * 0.04))


class DmrDecoderError(RuntimeError):
    pass


def decoder_path():
    override = os.environ.get("SDR_SCANNER_DMR_DECODER")
    if override:
        executable = shutil.which(os.path.expanduser(override))
    else:
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        candidate = data_home / "sdr-scanner/bin/dsd-neo"
        executable = shutil.which(str(candidate)) or shutil.which("dsd-neo")
    return str(Path(executable).resolve()) if executable else None


def decoder_available():
    return decoder_path() is not None


def _metadata(status, calls, wanted, *, direct_only=False):
    synctype = status.get("synctype")
    mobile = synctype == _DMR_MOBILE_VOICE
    synced = status.get("protocol") == "DMR" and synctype in _DMR_SYNC
    result = dict(sync=synced, voice=False, encrypted=None, color_code=None, slot=None,
                  talkgroup=None, unit_id=None, errors=None)
    if not synced or (direct_only and not mobile):
        return result

    for call in calls.get("slots", []):
        raw_slot = call.get("slot")
        if raw_slot not in (0, 1) or call.get("protocol_label") != "DMR" or call.get("state") != 2:
            continue
        raw_cc = call.get("receive_value")
        cc = raw_cc if (call.get("receive_kind") == 1 and call.get("receive_known") is True
                        and type(raw_cc) is int and 0 <= raw_cc <= 15) else None
        kind = call.get("kind")
        enc = call.get("enc")
        algorithm = call.get("algid")
        clear_algorithm = type(algorithm) is int and algorithm in (0, 0x80)
        encrypted = enc is True or (type(algorithm) is int and not clear_algorithm)
        # A link-control identity ties streamed audio to an actual voice call.
        confirmed_clear = enc is False and clear_algorithm and kind in (2, 3)
        # Mobile and direct-mode calls use DSD-neo's internal lane 0. The
        # separately filtered worker identifies the over-the-air slot, if any.
        slot = wanted.get("slot") if mobile and raw_slot == 0 else raw_slot + 1
        entry = dict(sync=True, voice=kind in (1, 2, 3),
                     encrypted=True if encrypted else False if confirmed_clear else None,
                     color_code=cc, slot=slot,
                     talkgroup=(call.get("tg_id") or None) if kind == 2 else None,
                     unit_id=call.get("src_id") or None, errors=None)
        if all(value is None or entry[key] == value for key, value in wanted.items()):
            return entry
    return result


def _message(row):
    """Extract verified DMR text from DSD-neo's explicit data event rows."""
    if not isinstance(row, dict) or row.get("systype") not in _DMR_SYNC:
        return None
    if (row.get("category") != 3 or row.get("subtype") != 127 or row.get("enc") != 0
            or row.get("enc_alg") != 0 or row.get("crc_invalid") != 0 or row.get("crc_ras") != 0):
        return None
    source = row.get("source_id")
    target = row.get("target_id")
    if not all(type(value) is int and 1 <= value <= 0xFFFFFF for value in (source, target)):
        return None
    notice = row.get("event")
    body = row.get("text")
    if not isinstance(notice, str) or not isinstance(body, str):
        return None
    # The pinned API includes a timestamp before the decoder's event description.
    notice = notice[20:] if len(notice) > 19 and notice[4:5] == "-" and notice[19] == " " else notice
    description = notice.split("] ", 1)[1] if notice.startswith("[") and "] " in notice else notice
    if description.startswith("Short Data SRC: "):
        supported = "; declared " in description and "; decoded " in description and "; Text: " in description
    elif description.startswith("UDT SRC: "):
        supported = any(marker in description for marker in
                        ("; ISO7 Text;", "; ISO8 Text;", "; UTF16 Text;", "; Mixed Add/Text;"))
    else:
        supported = (description.startswith(("TMS SRC: ", "ETSI TMS SRC: ", "VTX TMS SRC: ", "IP ID: "))
                     and _TMS_NOTICE.search(description) is not None
                     and not any(description.rstrip().endswith(marker)
                                 for marker in ("Acknowledgment;", "Truncated;", "No Text;")))
    if not supported:
        return None
    text = "".join(char for char in body[:_MAX_MESSAGE_TEXT]
                   if char == "\n" or category(char) not in ("Cc", "Cf", "Cs")).strip()
    if not text:
        return None
    slot = row.get("receive_slot")
    color_code = row.get("receive_value")
    if (row.get("receive_kind") != 1 or row.get("receive_known") is not True
            or type(color_code) is not int or not 0 <= color_code <= 15):
        color_code = None
    return {"source_id": source, "target_id": target, "text": text,
            "slot": slot if type(slot) is int and slot in (1, 2) else None, "color_code": color_code}


class _SlotDecoder:
    """Own a DSD-neo process for one receive slot or unnumbered direct calls."""

    def __init__(self, executable, slot, color_code, talkgroup):
        self._process = None
        self._socket = None
        self._directory = None
        self._discriminator = bytearray()
        self._output = bytearray()
        self._telemetry = bytearray()
        self._last_telemetry = time.monotonic()
        self._status = {}
        self._pending_calls = {}
        self._calls = {}
        self._wanted = dict(slot=slot, color_code=color_code, talkgroup=talkgroup)
        self._direct_only = slot is None
        self._audio_identity = None
        self._resample_state = np.zeros(len(_RESAMPLE_FILTER) - 1)
        self._messages = deque(maxlen=_MAX_PENDING_MESSAGES)
        self._message_rows = deque(maxlen=4 * _MAX_PENDING_MESSAGES)

        try:
            self._directory = tempfile.TemporaryDirectory(prefix="sdr-scanner-dmr-")
            token = secrets.token_urlsafe(32)
            token_file = Path(self._directory.name) / "token"
            with os.fdopen(os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
                stream.write(token)

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]

            command = [executable, "--no-config", "--no-decryption-keys", "-fs", "-i", "-", "-o", "-", "-s", "48000",
                       "--frontend", "none", "--api", str(port), "--api-bind", "127.0.0.1",
                       "--api-token-file", str(token_file), "-V", "3" if slot == 2 else "1"]
            if slot is not None:
                command.extend(("--slot-allow", str(slot)))
            if color_code is not None:
                command.extend(("--dcc-allow", str(color_code)))
            if talkgroup is not None:
                command.extend(("-I", str(talkgroup)))

            environment = {key: value for key, value in os.environ.items() if not key.startswith("DSD_NEO_")}
            self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, bufsize=0, cwd=self._directory.name,
                                             env=environment)
            self._connect(port, token)
            os.set_blocking(self._process.stdin.fileno(), False)
            os.set_blocking(self._process.stdout.fileno(), False)
        except (OSError, ValueError, DmrDecoderError) as error:
            self.close()
            if isinstance(error, DmrDecoderError):
                raise
            raise DmrDecoderError("Could not start the local DSD-neo decoder.") from error

    def _connect(self, port, token):
        deadline = time.monotonic() + 1.2
        while self._process.poll() is None and time.monotonic() < deadline:
            try:
                self._socket = socket.create_connection(("127.0.0.1", port), timeout=0.15)
                break
            except ConnectionRefusedError:
                time.sleep(0.015)
        if self._socket is None:
            raise DmrDecoderError("DSD-neo did not start its local telemetry feed.")

        self._socket.settimeout(0.7)
        with self._socket.makefile("rb", buffering=0) as incoming:
            greeting = json.loads(incoming.readline(_TELEMETRY_LIMIT))
            if greeting.get("type") != "welcome" or greeting.get("auth_required") is not True:
                raise DmrDecoderError("DSD-neo did not offer authenticated telemetry.")
            commands = ({"cmd": "auth", "token": token},
                        {"cmd": "subscribe", "topics": ["call", "status", "event"]})
            for command in commands:
                self._socket.sendall((json.dumps(command) + "\n").encode())
                reply = json.loads(incoming.readline(_TELEMETRY_LIMIT))
                if reply.get("ok") is not True:
                    raise DmrDecoderError("DSD-neo rejected local telemetry setup.")
        self._socket.setblocking(False)

    def _read_updates(self):
        while True:
            try:
                data = self._socket.recv(16384)
            except BlockingIOError:
                return
            if not data:
                raise DmrDecoderError("DSD-neo stopped sending telemetry.")
            self._telemetry.extend(data)
            if len(self._telemetry) > _TELEMETRY_LIMIT:
                raise DmrDecoderError("DSD-neo sent an oversized telemetry message.")
            lines = self._telemetry.split(b"\n")
            self._telemetry = lines.pop()
            for line in lines:
                message = json.loads(line)
                if message.get("type") == "call":
                    self._pending_calls = message
                elif message.get("type") == "status":
                    self._calls = self._pending_calls
                    self._status = message
                    self._last_telemetry = time.monotonic()
                elif message.get("type") == "event":
                    self._read_message(message.get("row"))

    def _read_message(self, row):
        message = _message(row)
        if message is None:
            return
        # A saved talkgroup filters voice. SMS recipient IDs are independent.
        if any(self._wanted[key] not in (None, message[key]) for key in ("slot", "color_code")):
            return
        identity = (row.get("ring"), row.get("slot"), row.get("push"))
        if identity not in self._message_rows:
            self._message_rows.append(identity)
            self._messages.append(message)

    def take_messages(self):
        pending = tuple(self._messages)
        self._messages.clear()
        return pending

    def _read_audio(self):
        while len(self._output) < _PCM_INPUT_LIMIT:
            try:
                block = os.read(self._process.stdout.fileno(), 8192)
            except BlockingIOError:
                break
            if not block:
                raise DmrDecoderError("DSD-neo closed its audio stream.")
            self._output.extend(block)

    def _decode_audio(self, metadata):
        end = len(self._output) // _OUTPUT_FRAME_BYTES * _OUTPUT_FRAME_BYTES
        frames = np.frombuffer(self._output[:end], dtype="<i2").reshape(-1, 160, 2)
        del self._output[:end]
        identity = (metadata["slot"], metadata["talkgroup"], metadata["unit_id"], metadata["color_code"])
        if (metadata["encrypted"] is not False or not metadata["voice"] or identity[2] is None
                or identity != self._audio_identity):
            self._audio_identity = identity if metadata["encrypted"] is False and metadata["voice"] else None
            self._resample_state.fill(0)
            return b""
        if not len(frames):
            return b""
        mobile = self._status.get("synctype") == _DMR_MOBILE_VOICE
        mono = frames[:, :, 0 if mobile else metadata["slot"] - 1].reshape(-1)
        if not len(mono) or not np.any(mono):
            return b""
        upsampled = np.zeros(3 * len(mono))
        upsampled[::3] = mono
        pcm, self._resample_state = signal.lfilter(_RESAMPLE_FILTER, [1.0], upsampled, zi=self._resample_state)
        return np.clip(np.rint(pcm), -32768, 32767).astype("<i2").tobytes()

    def process(self, samples):
        """Write 48 kHz mono PCM and return current call state and 24 kHz mono voice."""
        if self._process is None or self._process.poll() is not None:
            raise DmrDecoderError("The local DSD-neo decoder stopped.")

        try:
            self._discriminator.extend(samples)
            if self._discriminator:
                try:
                    sent = os.write(self._process.stdin.fileno(), self._discriminator)
                    del self._discriminator[:sent]
                except BlockingIOError:
                    pass
            if len(self._discriminator) > _PCM_INPUT_LIMIT:
                raise DmrDecoderError("DSD-neo cannot keep up with the SDR input.")

            self._read_audio()
            self._read_updates()
            if time.monotonic() - self._last_telemetry > _TELEMETRY_TIMEOUT:
                raise DmrDecoderError("DSD-neo telemetry timed out.")
            metadata = _metadata(self._status, self._calls, self._wanted, direct_only=self._direct_only)
            return metadata, self._decode_audio(metadata)
        except (OSError, ValueError) as error:
            raise DmrDecoderError("Could not read from the local DSD-neo decoder.") from error

    def close(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._process is not None:
            self._process.stdin.close()
            self._process.stdout.close()
            try:
                self._process.wait(timeout=0.03)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=0.12)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
            self._process = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


class DmrDecoder:
    """Decode one clear conventional DMR call at a time."""

    def __init__(self, *, slot=None, color_code=None, talkgroup=None):
        executable = decoder_path()
        if not executable:
            raise DmrDecoderError("Install DSD-neo 2.12.1 to receive DMR audio.")
        self._slots = []
        self._active_slot = None
        self._discriminator_dc_state = np.zeros(1)
        self._recent_messages = deque(maxlen=4 * _MAX_PENDING_MESSAGES)
        try:
            # Decode separately to keep the native mix and call metadata tied to
            # one slot. Mobile sync without a proven slot needs an unfiltered path.
            for number in (slot,) if slot is not None else (1, 2, None):
                self._slots.append(_SlotDecoder(executable, number, color_code, talkgroup))
        except DmrDecoderError:
            self.close()
            raise

    def process(self, samples):
        """Write 48 kHz mono PCM and return current call state and 24 kHz mono voice."""
        if not self._slots:
            raise DmrDecoderError("The local DSD-neo decoder stopped.")
        if samples:
            # Remove discriminator DC from small transmitter or tuner offsets without attenuating the 4FSK symbols.
            values = np.frombuffer(samples, dtype="<i2")
            filtered, self._discriminator_dc_state = signal.lfilter(
                [1, -1], [1, -_DISCRIMINATOR_DC_POLE], values, zi=self._discriminator_dc_state
            )
            samples = np.clip(filtered, -32768, 32767).astype("<i2").tobytes()
        readings = [decoder.process(samples) for decoder in self._slots]
        clear = [reading for reading in readings if reading[0]["voice"] and reading[0]["encrypted"] is False]
        clear = [reading for reading in clear if reading[0]["slot"] is not None] or clear
        selected = next((reading for reading in clear if reading[0]["slot"] == self._active_slot), None)
        if selected is None:
            selected = next(iter(clear), None)
        self._active_slot = selected[0]["slot"] if selected is not None else None
        if selected is not None:
            return selected
        return max(readings, key=lambda reading: (reading[0]["sync"], reading[0]["voice"]))

    def take_messages(self):
        """Drain decoded SMS, coalescing duplicate reports by parallel workers."""
        now = time.monotonic()
        cutoff = now - _MESSAGE_DUPLICATE_SECONDS
        while self._recent_messages and self._recent_messages[0][0] < cutoff:
            self._recent_messages.popleft()
        pending = []
        for index, decoder in enumerate(self._slots):
            for message in decoder.take_messages():
                body = message["source_id"], message["target_id"], message["text"]
                slot, cc = message["slot"], message["color_code"]
                if any(worker != index and prior_body == body
                       and (slot is None or prior_slot in (None, slot))
                       and (cc is None or prior_cc in (None, cc))
                       for _, worker, prior_body, prior_slot, prior_cc in self._recent_messages):
                    continue
                self._recent_messages.append((now, index, body, slot, cc))
                pending.append(message)
        return tuple(pending)

    def close(self):
        for decoder in self._slots:
            decoder.close()
        self._slots.clear()

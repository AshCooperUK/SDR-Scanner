"""Receive-only DMR Tier III bridge for the pinned DSD-neo desktop decoder.

The scanner runtime owns the receiver and acknowledges decoder tune requests.
Only the discriminator, tuner control, and authenticated telemetry use loopback.
"""

from collections import deque
from collections.abc import Mapping
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time

import numpy as np
from scipy import signal

from dmr_decoder import DmrDecoderError, decoder_path


_MIN_HZ = 100_000
_MAX_HZ = 1_750_000_000
_DMR_SYNC = frozenset((10, 11, 12, 13, 32, 33, 34))
_TELEMETRY_TIMEOUT = 2.0
_TUNE_TIMEOUT = 1.5
_INPUT_LIMIT = 12_000  # 125 ms of 48 kHz mono PCM16.
_TELEMETRY_LIMIT = 262_144
_AUDIO_FRAME_BYTES = 4  # Two 8 kHz PCM16 slot lanes.
_DEFAULT_PASSBAND_HZ = 15_000
_FILTER = 3 * signal.firwin(49, 1 / 3)
_DC_POLE = np.exp(-1 / (48_000 * 0.04))
_AUDIO_CONFIRM_DELAY = 0.30  # Allow the decoder's coalesced call updates to arrive.


def _within(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def _listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.setblocking(False)
        return listener
    except OSError:
        listener.close()
        raise


def _accept(listener):
    try:
        peer, _ = listener.accept()
    except BlockingIOError:
        return None
    peer.setblocking(False)
    peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return peer


def _send_pending(peer, pending):
    if peer is not None and pending:
        try:
            sent = peer.send(pending)
        except BlockingIOError:
            return
        del pending[:sent]


def _read_all(peer, pending, limit):
    while len(pending) < limit:
        try:
            block = peer.recv(min(16384, limit - len(pending)))
        except BlockingIOError:
            return
        if not block:
            raise DmrDecoderError("The local DSD-neo connection closed.")
        pending.extend(block)
    raise DmrDecoderError("DSD-neo sent an oversized message.")


def _json_record(line):
    message = json.loads(line)
    if not isinstance(message, dict):
        raise DmrDecoderError("DSD-neo sent invalid Tier III telemetry.")
    return message


def _clear_group(row, hz):
    if (row.get("state") != 2 or row.get("protocol_label") != "DMR" or row.get("kind") != 2
            or type(row.get("slot")) is not int or row["slot"] not in (0, 1)
            or row.get("frequency_hz") != hz or not _within(row.get("src_id"), 1, 0xFFFFFF)
            or not _within(row.get("tg_id"), 1, 0xFFFFFF)):
        return None
    # Algorithm 0 (no override) and 0x80 (clear) are the public decoder values.
    algorithm = row.get("algid")
    encrypted_flag = row.get("enc")
    clear_algorithm = type(algorithm) is int and algorithm in (0, 0x80)
    if encrypted_flag is True or (type(algorithm) is int and not clear_algorithm):
        clear = False
    else:
        clear = True if encrypted_flag is False and clear_algorithm else None
    return clear, (row["slot"] + 1, row["tg_id"], row["src_id"])


class Tier3Decoder:
    """Follow one configured DMR Tier III site using a persistent decoder."""

    def __init__(self, control_hz, channel_map, *, talkgroups=None, hold=None, executable=None,
                 blocked=(), allow_list=False):
        if (not _within(control_hz, _MIN_HZ, _MAX_HZ) or not isinstance(channel_map, Mapping)
                or control_hz not in channel_map.values() or not all(
                    _within(channel, 1, 4094) and _within(hz, _MIN_HZ, _MAX_HZ)
                    for channel, hz in channel_map.items())):
            raise ValueError("Enter a Tier III channel map containing the primary control frequency.")
        groups = {} if talkgroups is None else dict(talkgroups)
        blacklist = frozenset(blocked)
        if (not all(_within(tg, 1, 0xFFFFFF) and isinstance(name, str) for tg, name in groups.items())
                or not all(_within(tg, 1, 0xFFFFFF) for tg in blacklist)
                or (hold is not None and (not _within(hold, 1, 0xFFFFFF) or hold in blacklist))):
            raise ValueError("Enter valid Tier III talkgroup IDs and names.")
        binary = executable or decoder_path()
        if not binary:
            raise DmrDecoderError("Install DSD-neo 2.12.1 to follow DMR Tier III.")
        self._frequencies = frozenset(channel_map.values())
        self._hold = hold
        self._allowed = frozenset(groups) if allow_list else None
        self._blocked = blacklist
        self._hz = control_hz
        self._directory = None
        self._process = None
        self._pcm_listener = self._rig_listener = None
        self._api = self._pcm = self._rig = None
        self._tune = None
        self._tune_taken = False
        self._tune_started = 0.0
        self._rig_input = bytearray()
        self._rig_reply = bytearray()
        self._input = bytearray()
        self._api_input = bytearray()
        self._audio = bytearray()
        self._audio_alignment = 0
        self._last_telemetry = time.monotonic()
        self._decoder_status = {}
        self._calls = ()
        self._site = {}
        self._clear_call = None
        self._clear_elapsed = None
        self._pending_audio = deque()
        self._dc_state = np.zeros(1)
        self._resample_state = np.zeros(len(_FILTER) - 1)
        try:
            self._directory = tempfile.TemporaryDirectory(prefix="sdr-scanner-tier3-")
            folder = Path(self._directory.name)
            channel_path = folder / "channels.csv"
            # DSD-neo tries the map in file order when hunting for a control channel.
            ordered = sorted(channel_map.items(), key=lambda row: row[1] != control_hz)
            channel_path.write_text("channel_number,frequency_hz\n" + "".join(
                f"{channel},{hz}\n" for channel, hz in ordered), encoding="ascii")
            group_path = folder / "groups.csv"
            group_rows = []
            for group_id in sorted(groups.keys() | blacklist | ({hold} if hold is not None else set())):
                # DSD-neo's upstream reader does not unescape quoted CSV names.
                alias = "".join(char for char in groups.get(group_id, "") if char.isprintable() and char != ",")[:48]
                group_rows.append(f"{group_id},{'B' if group_id in blacklist else 'A'},{alias}\n")
            group_path.write_text("id,mode,name\n" + "".join(group_rows), encoding="utf-8")
            token = secrets.token_urlsafe(32)
            token_path = folder / "token"
            with os.fdopen(os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as contents:
                contents.write(token + "\n")
            self._pcm_listener = _listener()
            self._rig_listener = _listener()
            with _listener() as reservation:
                api_port = reservation.getsockname()[1]
            arguments = [str(Path(binary).resolve()), "--no-config", "--no-decryption-keys", "--enc-lockout",
                         "-fs", "-T", "-p", "-i", f"tcp:127.0.0.1:{self._pcm_listener.getsockname()[1]}",
                         "-U", str(self._rig_listener.getsockname()[1]), "-s", "48000", "-o", "-",
                         "-C", str(channel_path), "-G", str(group_path), "--frontend", "none",
                         "--api", str(api_port), "--api-bind", "127.0.0.1", "--api-token-file", str(token_path)]
            if hold is not None:
                arguments.extend(("-I", str(hold)))
            if allow_list:
                arguments.append("-W")
            env = {name: value for name, value in os.environ.items() if not name.startswith("DSD_NEO_")}
            self._process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, bufsize=0, cwd=folder, env=env)
            os.set_blocking(self._process.stdout.fileno(), False)
            self._connect(api_port, token)
        except (OSError, ValueError, DmrDecoderError) as error:
            self.close()
            if isinstance(error, DmrDecoderError):
                raise
            raise DmrDecoderError("Could not start the local DSD-neo Tier III decoder.") from error

    def _connect(self, port, token):
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and self._process.poll() is None:
            self._service_rig()
            self._connect_input()
            try:
                self._api = socket.create_connection(("127.0.0.1", port), timeout=0.15)
                break
            except ConnectionRefusedError:
                time.sleep(0.015)
        if self._api is None:
            raise DmrDecoderError("DSD-neo Tier III telemetry did not start.")
        self._api.settimeout(0.7)
        with self._api.makefile("rb", buffering=0) as lines:
            welcome = _json_record(lines.readline(_TELEMETRY_LIMIT))
            if welcome.get("type") != "welcome" or welcome.get("auth_required") is not True:
                raise DmrDecoderError("DSD-neo did not offer authenticated Tier III telemetry.")
            for request in ({"cmd": "auth", "token": token},
                            {"cmd": "subscribe", "topics": ["call", "status", "event"]}):
                self._api.sendall((json.dumps(request) + "\n").encode())
                reply = _json_record(lines.readline(_TELEMETRY_LIMIT))
                if reply.get("ok") is not True:
                    raise DmrDecoderError("DSD-neo rejected Tier III telemetry setup.")
        self._api.setblocking(False)
        self._last_telemetry = time.monotonic()

    def _connect_input(self):
        if self._pcm is None:
            self._pcm = _accept(self._pcm_listener)
            if self._pcm is not None:
                self._pcm.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _INPUT_LIMIT)
                self._pcm_listener.close()
                self._pcm_listener = None

    def _rig_command(self, line):
        parts = line.split()
        if parts == [b"f"]:
            return f"{self._hz}\n".encode()
        if parts == [b"m"]:
            return f"FM\n{_DEFAULT_PASSBAND_HZ}\n".encode()
        if parts in ([b"M", b"FM", b"0"], [b"M", b"FM", str(_DEFAULT_PASSBAND_HZ).encode()]):
            return b"RPRT 0\n"
        if len(parts) != 2 or parts[0] != b"F" or not parts[1].isdigit():
            return b"RPRT -11\n"
        hz = int(parts[1])
        if hz not in self._frequencies or self._tune is not None:
            return b"RPRT -11\n"
        if hz == self._hz:
            return b"RPRT 0\n"
        self._tune = hz
        self._tune_taken = False
        self._tune_started = time.monotonic()
        self._input.clear()
        self._discard_audio()
        return b""

    def _service_rig(self):
        if self._rig is None:
            self._rig = _accept(self._rig_listener)
            if self._rig is None:
                return
            self._rig_listener.close()
            self._rig_listener = None
        _read_all(self._rig, self._rig_input, 4096)
        lines = self._rig_input.split(b"\n")
        self._rig_input = lines.pop()
        for line in lines:
            self._rig_reply.extend(self._rig_command(line))
        if len(self._rig_reply) > 4096:
            raise DmrDecoderError("DSD-neo tune replies backed up.")
        _send_pending(self._rig, self._rig_reply)
        if self._tune is not None and time.monotonic() - self._tune_started > _TUNE_TIMEOUT:
            raise DmrDecoderError("The SDR did not acknowledge the Tier III retune in time.")

    def take_tune(self):
        """Return a pending verified frequency once for the receiver owner's loop."""
        if self._process is None or self._process.poll() is not None:
            raise DmrDecoderError("The local DSD-neo Tier III decoder stopped.")
        try:
            self._service_rig()
        except OSError as error:
            raise DmrDecoderError("Could not read the Tier III tune request.") from error
        if self._tune is None or self._tune_taken:
            return None
        self._tune_taken = True
        return self._tune

    def ack_tune(self, hz, ok=True):
        """Acknowledge only after the receiver committed this specific tune."""
        if not self._tune_taken or self._tune != hz:
            return False
        try:
            self._read_updates()
            self._read_audio()
            self._discard_audio()
            self._calls = ()
            self._decoder_status = {}
            self._input.clear()
            self._dc_state.fill(0)
            if ok:
                self._hz = hz
            self._tune = None
            self._tune_taken = False
            self._rig_reply.extend(b"RPRT 0\n" if ok else b"RPRT -11\n")
            _send_pending(self._rig, self._rig_reply)
            return True
        except (ValueError, OSError) as error:
            raise DmrDecoderError("Could not acknowledge the Tier III retune.") from error

    def _read_updates(self):
        _read_all(self._api, self._api_input, _TELEMETRY_LIMIT)
        lines = self._api_input.split(b"\n")
        self._api_input = lines.pop()
        for line in lines:
            message = _json_record(line)
            kind = message.get("type")
            if kind == "status":
                self._decoder_status = message
                self._last_telemetry = time.monotonic()
            elif kind == "call":
                slots = message.get("slots")
                if not isinstance(slots, list) or not all(isinstance(slot, dict) for slot in slots):
                    raise DmrDecoderError("DSD-neo sent invalid Tier III slot telemetry.")
                self._calls = slots
            elif kind == "event":
                row = message.get("row")
                site = row.get("site") if isinstance(row, dict) else None
                if (isinstance(site, dict) and site.get("dmr_trunk") == 1
                        and row.get("crc_invalid") == 0 and row.get("crc_ras") == 0):
                    self._site = {key: value for key in ("dmr_net", "dmr_site", "dmr_t3_syscode", "dmr_t3_model")
                                  if _within(value := site.get(key), 0, 0xFFFF)}
            if kind in ("status", "call") and self._clear_call is not None:
                # Catch a call change even if an entire transition was buffered between polls.
                clear, identity, elapsed = self._current_call()
                if (clear is not True or identity != self._clear_call or
                        (type(elapsed) is int and type(self._clear_elapsed) is int and elapsed < self._clear_elapsed)):
                    self._discard_audio()

    def _read_audio(self):
        while len(self._audio) <= _INPUT_LIMIT:
            try:
                block = os.read(self._process.stdout.fileno(), 8192)
            except BlockingIOError:
                return
            if not block:
                raise DmrDecoderError("DSD-neo closed its Tier III audio stream.")
            skip = min(self._audio_alignment, len(block))
            self._audio_alignment -= skip
            self._audio.extend(block[skip:])
        self._discard_audio()

    def _discard_audio(self):
        self._audio_alignment = (-len(self._audio)) % _AUDIO_FRAME_BYTES if self._audio else self._audio_alignment
        self._audio.clear()
        self._pending_audio.clear()
        self._clear_call = self._clear_elapsed = None
        self._resample_state.fill(0)

    def _current_call(self):
        reported_hz = self._decoder_status.get("vc_freq_hz")
        if (self._tune is not None or self._decoder_status.get("trunk_tuned") is not True
                or reported_hz != self._hz or not _within(sync := self._decoder_status.get("synctype"), 0, 100)
                or sync not in _DMR_SYNC):
            return None, None, None
        lead = self._decoder_status.get("lead_slot")
        for row in sorted(self._calls, key=lambda item: item.get("slot") != lead):
            result = _clear_group(row, self._hz)
            if result is None:
                continue
            clear, identity = result
            group = identity[1]
            if (group not in self._blocked and (self._hold is None or group == self._hold)
                    and (self._allowed is None or group in self._allowed)):
                color_code = row.get("receive_value")
                if (row.get("receive_known") is not True or row.get("receive_kind") != 1
                        or not _within(color_code, 0, 15)):
                    color_code = None
                return clear, (*identity, color_code), row.get("elapsed_ms")
        return None, None, None

    def _decode_audio(self, clear, identity, elapsed, now):
        changed_call = (identity != self._clear_call or (
            type(elapsed) is int and type(self._clear_elapsed) is int and elapsed < self._clear_elapsed))
        if clear is not True or changed_call:
            self._discard_audio()
            if clear is True:
                self._clear_call, self._clear_elapsed = identity, elapsed
            return b""
        self._clear_elapsed = elapsed
        complete = len(self._audio) // _AUDIO_FRAME_BYTES * _AUDIO_FRAME_BYTES
        if complete:
            frames = np.frombuffer(self._audio[:complete], dtype="<i2").reshape(-1, 2)
            del self._audio[:complete]
            lane = frames[:, identity[0] - 1].copy()
            self._pending_audio.append((now, lane))
        ready = []
        while self._pending_audio and self._pending_audio[0][0] <= now - _AUDIO_CONFIRM_DELAY:
            ready.append(self._pending_audio.popleft()[1])
        if not ready:
            return b""
        mono = np.concatenate(ready)
        upsampled = np.zeros(3 * len(mono))
        upsampled[::3] = mono
        pcm, self._resample_state = signal.lfilter(_FILTER, [1.0], upsampled, zi=self._resample_state)
        return np.clip(np.rint(pcm), -32768, 32767).astype("<i2").tobytes()

    def process(self, discriminator):
        """Supply unsquelched 48 kHz PCM and read status plus clear 24 kHz voice."""
        if self._process is None or self._process.poll() is not None:
            raise DmrDecoderError("The local DSD-neo Tier III decoder stopped.")
        try:
            self._service_rig()
            self._connect_input()
            if self._tune is None and discriminator:
                samples = np.frombuffer(discriminator, dtype="<i2")
                filtered, self._dc_state = signal.lfilter([1, -1], [1, -_DC_POLE], samples, zi=self._dc_state)
                self._input.extend(np.clip(filtered, -32768, 32767).astype("<i2").tobytes())
            _send_pending(self._pcm, self._input)
            if len(self._input) > _INPUT_LIMIT:
                raise DmrDecoderError("DSD-neo Tier III cannot keep up with the SDR input.")
            self._read_audio()
            self._read_updates()
            now = time.monotonic()
            if now - self._last_telemetry > _TELEMETRY_TIMEOUT:
                raise DmrDecoderError("DSD-neo Tier III telemetry timed out.")
            clear, identity, elapsed = self._current_call()
            pcm = self._decode_audio(clear, identity, elapsed, now)
            native = self._decoder_status
            control_hz = native.get("cc_freq_hz") or None
            voice_hz = native.get("vc_freq_hz") or None
            trunk_tuned = native.get("trunk_tuned") is True
            synchronized = (self._tune is None and native.get("protocol") == "DMR"
                            and _within(sync := native.get("synctype"), 0, 100) and sync in _DMR_SYNC)
            if self._tune is not None:
                phase = "tuning"
            elif identity is not None and clear is not None:
                phase = "voice" if clear else "encrypted"
            elif trunk_tuned:
                phase = "following"
            elif synchronized and control_hz == self._hz:
                phase = "control"
            else:
                phase = "acquiring"
            slot, talkgroup, unit_id, color_code = identity or (None, None, None, None)
            return {"sync": synchronized, "voice": identity is not None, "encrypted": None if clear is None else not clear,
                    "slot": slot, "talkgroup": talkgroup, "unit_id": unit_id, "color_code": color_code,
                    "errors": None, "phase": phase, "cc_freq_hz": control_hz, "vc_freq_hz": voice_hz,
                    "trunk_tuned": trunk_tuned, "site": dict(self._site)}, pcm
        except (OSError, ValueError) as error:
            raise DmrDecoderError("Could not read from the DSD-neo Tier III decoder.") from error

    def take_messages(self):
        """Standard group-voice following does not switch to data grants."""
        return ()

    def close(self):
        for name in ("_api", "_pcm", "_rig", "_pcm_listener", "_rig_listener"):
            endpoint = getattr(self, name)
            if endpoint is not None:
                endpoint.close()
                setattr(self, name, None)
        if self._process is not None:
            self._process.stdout.close()
            try:
                self._process.wait(timeout=0.04)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
            self._process = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None

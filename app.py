"""Serve the local scanner panel and bridge it to the USB receiver."""

import argparse
import json
import logging
import math
import mimetypes
import os
import queue
import re
import signal
import socket
import stat
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from folder_picker import browse_folders, select_folder
from receiver import SAMPLE_RATE, Receiver, ReceiverError
from recording import Recorder, RecordingError
from replay import RECORDINGS_URL, byte_range, latest_clip, open_clip
from scanner import (
    FAVORITE_COUNT, FINE_STEP_HZ, KEY_ALIASES, KEYS, MANUAL_MAX_HZ, MANUAL_MIN_HZ,
    Scanner, normalize_favorite_name,
)


ROOT = Path(__file__).resolve().parent
ASSETS = frozenset({"index.html", "panel.css", "panel.js", "panel.png", "field-background.webp",
                    "pcm-buffer.js", "pcm-worklet.js", "ashcooperuk-avatar.png"})
SILENCE = bytes(960)
FREQUENCY_ENTRY = re.compile(r"[0-9]{1,4}(?:\.[0-9]{0,6})?")
WHEEL_STEPS_HZ = frozenset(10 ** position for position in range(10))
WATCH_INTERVAL = 0.12
WATCH_LISTENING_INTERVAL = 0.85
WATCH_IDLE_INTERVAL = 0.30
WATCH_ACTIVITY_SECONDS = 1.4
WATCH_RDS_GRACE = 4.2
WATCH_RECEIVER_RETRY = 2.0
WATCH_SECONDARY_START_GRACE = 0.5
WFM_NOISE_MARGIN_DB = 1.5
TUNING_LIMITS_HZ = {"AM": (350, 3_000), "FM": (900, 4_200), "WFM": (8_000, 40_000)}
LOG = logging.getLogger("scanner")


def target(state):
    hz = state["tune_hz"]
    return (hz, state["modulation"]) if hz is not None else None


PRIVATE_NETWORKS = tuple(IPv4Network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
))
REMOTE_CONFIG_LIMIT = 16_384


class RemoteAccess:
    """Opt-in HTTP access for configured private networks."""

    def __init__(self, clients, destinations):
        self.clients = clients
        self.destinations = destinations

    @staticmethod
    def _networks(values, setting):
        if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
            raise ValueError(f"{setting} must be a nonempty list of private IPv4 addresses or CIDRs.")
        try:
            networks = tuple(IPv4Network(value) for value in values)
        except ValueError:
            raise ValueError(f"{setting} contains an invalid IPv4 address or CIDR.") from None
        if not all(any(network.subnet_of(private) for private in PRIVATE_NETWORKS) for network in networks):
            raise ValueError(f"{setting} must stay within a private LAN or Tailscale network.")
        return networks

    @classmethod
    def load(cls, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError("The remote config must be a regular file owned by this user with mode 0600.")
            payload = stream.read(REMOTE_CONFIG_LIMIT + 1)
        if len(payload) > REMOTE_CONFIG_LIMIT:
            raise ValueError("The remote config exceeds 16 KiB.")
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, RecursionError):
            raise ValueError("The remote config must contain UTF-8 JSON.") from None
        if not isinstance(data, dict) or set(data) != {"allowed_cidrs", "allowed_addresses"}:
            raise ValueError("The remote config requires allowed_cidrs and allowed_addresses.")
        clients = cls._networks(data["allowed_cidrs"], "allowed_cidrs")
        destinations = cls._networks(data["allowed_addresses"], "allowed_addresses")
        return cls(clients, destinations)

    def allows(self, peer, destination):
        return (any(peer in network for network in self.clients)
                and any(destination in network for network in self.destinations))


def usable_signal_level(level, modulation, offset, rf_excess):
    if modulation == "WFM" and rf_excess is not None:
        excess = rf_excess - WFM_NOISE_MARGIN_DB
        if excess <= 0:
            return -150.0
        # Estimate wanted power by removing the measured noise contribution.
        signal_fraction = -math.expm1(-excess * math.log(10) / 10)
        level += 10 * math.log10(signal_fraction)
    if offset is not None:
        tolerance, limit = TUNING_LIMITS_HZ[modulation]
        detuned = (abs(offset) - tolerance) / (limit - tolerance)
        level -= 45 * min(1.0, max(0.0, detuned))
    return level


class RecordingRuntime:
    """Keep MP3 encoding and file operations off the receiver thread."""

    def __init__(self, scanner, lock, stopped):
        self.scanner = scanner
        self.lock = lock
        self.stopped = stopped
        self.parent = scanner.recording_parent or scanner.state_path.parent.resolve() / "recordings"
        self.recorder = Recorder(self.parent)
        self.folder = str(self.recorder.root)
        self.enabled = False
        self.choosing = False
        self.message = ""
        self._epoch = 0
        self._capture = None
        self._writing = False
        self._suppressed = None
        self._queue = queue.SimpleQueue()
        self._pcm_slots = threading.BoundedSemaphore(150)
        self._worker = threading.Thread(target=self._run, name="MP3 recorder", daemon=True)

    def state(self):
        return {
            "record_enabled": self.enabled,
            "record_active": self.enabled and self._capture is not None and self._writing,
            "record_folder": self.folder,
            "record_message": self.message,
            "record_choosing": self.choosing,
        }

    def _enqueue(self, command, *args):
        if self._worker.ident is None:
            self._worker.start()
        self._queue.put((command, args))

    def _finish(self):
        if self._capture is not None:
            self._capture = None
            self._writing = False
            self._enqueue("finish", self._epoch)

    def boundary(self):
        self._suppressed = None
        self._finish()

    def receive(self, tuned, pcm):
        if self.stopped.is_set():
            return
        if not pcm or tuned != self._suppressed:
            self._suppressed = None
        if not self.enabled or not pcm or tuned is None or self._suppressed is not None:
            self._finish()
            return
        if self._capture != tuned:
            self._finish()
            self._capture = tuned
        if self._pcm_slots.acquire(blocking=False):
            self._enqueue("feed", self._epoch, tuned, pcm)
        else:
            self.enabled = False
            self._finish()
            self.message = "Recording stopped because disk writes fell behind."

    def act(self, action, tuned):
        if self.stopped.is_set():
            return
        command = action["record"]
        if command == "toggle":
            self.enabled = not self.enabled
            if self.enabled:
                self._epoch += 1
                self._suppressed = None
                self._enqueue("check", self._epoch)
                self.message = "Recording armed."
            else:
                self._finish()
                self.message = "Recording stopped."
        elif self.choosing:
            self.message = "Finish choosing a folder before clearing recordings."
        elif tuned is None:
            self.message = "Tune to the frequency you want to clear."
        elif action["hz"] != tuned[0]:
            self.message = "Frequency changed. Hold RECORD again to clear the tuned frequency."
        else:
            self._finish()
            if self.enabled and self.scanner.audio_gate_open:
                self._suppressed = tuned
            self._enqueue("delete", tuned[0])
            self.message = "Clearing recordings for the tuned frequency."

    def folder_action(self, action):
        if action["record"] == "browse":
            with self.lock:
                folder = Path(action.get("path", self.parent))
            try:
                if "path" not in action:
                    while not folder.is_dir() and folder != folder.parent:
                        folder = folder.parent
                return {"record_browser": browse_folders(folder)}
            except (OSError, ValueError) as error:
                return {"record_browser": {"error": str(error)}}

        try:
            selected = select_folder(Path(action["path"]))
            with self.lock:
                if self.stopped.is_set():
                    raise ValueError("The scanner is stopping.")
                if self.choosing:
                    raise ValueError("A recording folder is already being saved.")
                self.choosing = True
                self._finish()
                reply = queue.SimpleQueue()
                self._enqueue("folder", selected, reply)
            error = reply.get()
            if error is not None:
                raise ValueError(error)
            return {"record_selected": True}
        except (OSError, ValueError) as error:
            with self.lock:
                self.message = str(error)
            return {"record_selected": False, "record_browser": {"error": str(error)}}

    def _set_parent(self, parent, reply):
        failure = None
        try:
            if self.stopped.is_set():
                raise ValueError("The scanner is stopping.")
            replacement = Recorder(parent)
            replacement.set_parent(parent)
            self.recorder.finish()
            with self.lock:
                if self.stopped.is_set():
                    raise ValueError("The scanner is stopping.")
                self.scanner.set_recording_parent(parent)
                self.recorder = replacement
                self.parent = parent
                self.folder = str(self.recorder.root)
                self.message = "Recording folder saved."
        except (RecordingError, OSError, ValueError) as error:
            failure = str(error)
        finally:
            with self.lock:
                self.choosing = False
                if failure is not None:
                    self.message = failure
            reply.put(failure)

    def _run(self):
        failed_epoch = None
        while True:
            command, args = self._queue.get()
            epoch = args[0] if command in ("check", "feed", "finish") else None
            try:
                if command == "stop":
                    self.recorder.finish()
                    return
                if command == "check":
                    self.recorder.check_available()
                elif command == "feed":
                    self._pcm_slots.release()
                    if epoch == failed_epoch:
                        continue
                    _, (hz, modulation), pcm = args
                    self.recorder.feed(hz, pcm, skip_tone_only=(modulation in ("AM", "FM")))
                    with self.lock:
                        if epoch == self._epoch and self._capture and self._capture[0] == hz:
                            self._writing = True
                elif command == "finish":
                    self.recorder.finish()
                elif command == "folder":
                    self._set_parent(*args)
                elif command == "delete":
                    hz = args[0]
                    count = self.recorder.clear_frequency(hz)
                    with self.lock:
                        files = "recording" if count == 1 else "recordings"
                        self.message = f"Cleared {count} {files} at {hz / 1_000_000:.6f} MHz."
            except (RecordingError, OSError) as error:
                LOG.warning("Recording %s failed: %s", command, error)
                with self.lock:
                    self.message = str(error)
                    if epoch is not None:
                        failed_epoch = epoch
                        if epoch == self._epoch:
                            self.enabled = False
                            self._capture = None
                            self._writing = False
                if command == "stop":
                    return

    def stop(self):
        self.enabled = False
        self._capture = None
        self._writing = False
        if self._worker.ident is not None:
            self._queue.put(("stop", ()))

    def join(self):
        if self._worker.ident is not None:
            self._worker.join(timeout=3)


class ScannerRuntime:
    def __init__(self, state_path):
        self.scanner = Scanner(state_path)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.recording = RecordingRuntime(self.scanner, self.lock, self.stopped)
        self.clients = set()
        self.receiver_status = "standby"
        self.signal = None
        self.rds = None
        self.rds_target = None
        self.watch_enabled = False
        self._watch_activity = {}
        self._watch_epoch = 0
        self._watch_cursor = 1
        self._next_watch = 0.0
        self._next_watch_probe = 0.0
        self._watch_secondary_ready = False
        self._watch_wake = threading.Event()
        self._watch_worker = threading.Thread(target=self._run_watch_receiver, name="WATCH receiver", daemon=True)
        self.worker = threading.Thread(target=self.run, name="SDR receiver", daemon=True)

    def start(self):
        self.worker.start()
        self._watch_worker.start()

    def _rds_for(self, state):
        if (self.rds and state["power"] and state["modulation"] == "WFM"
                and state["rds_enabled"] and self.receiver_status == "ready"
                and target(state) == self.rds_target):
            return self.rds
        return None

    def state(self):
        with self.lock:
            state = self.scanner.snapshot()
            state["receiver"] = self.receiver_status
            current_signal = (self.signal if self.signal and self.receiver_status == "ready"
                              and self.signal[0] == target(state) else None)
            state["signal_dbfs"] = current_signal[1] if current_signal else None
            state["signal_meter_dbfs"] = current_signal[2] if current_signal else None
            state["tuning_offset_hz"] = current_signal[3] if current_signal else None
            state.update(self.recording.state())
            rds = self._rds_for(state)
            state["rds"] = dict(rds) if rds else None
            state["watch_enabled"] = self.watch_enabled
            state["watch_active"] = []
            state["watch_receiver"] = None
            if self.watch_enabled and state["power"] and self.receiver_status == "ready":
                state["watch_receiver"] = "secondary" if self._watch_secondary_ready else "primary"
                now, targets = time.monotonic(), self._watch_targets()
                state["watch_active"] = [slot for slot, (signal, expires) in sorted(self._watch_activity.items())
                                         if expires > now and targets.get(slot) == signal]
            if self.receiver_status == "unavailable" and state["power"]:
                state["audio_open"] = False
                if state.get("audio_expected", state["tune_hz"] is not None):
                    state["display"] = "Err"
            return state

    def act(self, action):
        if action.get("record") in ("browse", "select"):
            result = self.recording.folder_action(action)
            return {**self.state(), **result}
        with self.lock:
            before = self.scanner.snapshot()
            watch_changed = action.get("setting") == "watch"
            if "record" in action:
                self.recording.act(action, target(before))
            elif "key" in action:
                self.scanner.key(action["key"], action.get("gesture", "tap"))
            elif "favorite" in action:
                if "name" in action:
                    self.scanner.rename_favorite(action["favorite"], action["name"])
                else:
                    gesture = action.get("gesture", "tap")
                    rds = self._rds_for(before) if gesture == "long" else None
                    self.scanner.favorite(action["favorite"], gesture, rds_ps=rds.get("ps") if rds else None)
            elif "setting" in action:
                if watch_changed:
                    self.watch_enabled = not self.watch_enabled
                    self._watch_cursor = 1
                    self._next_watch_probe = 0.0
                else:
                    self.scanner.toggle_rds()
            elif action.get("tune") == "step":
                self.scanner.tune_step(action["direction"], action.get("step_hz", FINE_STEP_HZ))
            elif action.get("tune") == "frequency":
                whole, _, fraction = action["mhz"].partition(".")
                hz = int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))
                self.scanner.tune_frequency(hz)
            else:
                self.scanner.knob(action["knob"], action["value"], action.get("held", ()))
            after = self.scanner.snapshot()
            if self.watch_enabled and (watch_changed or not before["power"] and after["power"]):
                self._next_watch_probe = time.monotonic() + WATCH_SECONDARY_START_GRACE
            retuned = target(before) != target(after)
            if retuned:
                self.signal = None
            if retuned or before["rds_enabled"] != after["rds_enabled"]:
                self.rds = None
                self.rds_target = None
            if (watch_changed or (retuned and not self._watch_secondary_ready) or before["power"] != after["power"]
                    or before["squelch"] != after["squelch"]):
                self._clear_watch()
            if retuned or (before["audio_open"] and not after["audio_open"]):
                self.discard_audio()
            if retuned or not self.scanner.audio_gate_open:
                self.recording.boundary()
            self.wake.set()
            self._watch_wake.set()
            return self.state()

    def _clear_watch(self):
        self._watch_epoch += 1
        self._watch_activity.clear()
        self._next_watch = 0.0

    def _watch_targets(self):
        channels = self.scanner.memories["FAV"][:FAVORITE_COUNT]
        return {slot: (channel.hz, channel.modulation) for slot, channel in enumerate(channels, 1)
                if channel and not self.scanner._lock_kind(channel)}

    def _watch_current(self, epoch, primary):
        return (self.watch_enabled and not self._watch_secondary_ready
                and epoch == self._watch_epoch and self.scanner.power
                and not self.scanner.menu and not self.scanner.preview_until
                and target(self.scanner.snapshot()) == primary)

    def _watch(self, receiver, primary, sample=None, *, allow_probe=True, audible=False, rds_ready=False):
        """Measure nearby slots from the last audio block and visit one distant slot if due."""
        with self.lock:
            now, epoch = time.monotonic(), self._watch_epoch
            if now < self._next_watch or not self._watch_current(epoch, primary):
                return
            self._next_watch = now + WATCH_INTERVAL
            targets = self._watch_targets()
        if not targets:
            return

        levels = receiver.watch_levels(set(targets.values())) if sample else {}
        if sample:
            sampled_target, level = sample
            levels[sampled_target] = level
        with self.lock:
            if not self._watch_current(epoch, primary):
                return
            distant = [slot for slot, signal in self._watch_targets().items()
                       if targets.get(slot) == signal and signal not in levels]
            rds_acquiring = primary and primary[1] == "WFM" and self.scanner.rds_enabled and not rds_ready
            probe = (allow_probe and not rds_acquiring and self.scanner.close_mode == "off"
                     and distant and now >= self._next_watch_probe)
            if probe:
                slot = min(distant, key=lambda value: (value - self._watch_cursor) % FAVORITE_COUNT)
                signal = targets[slot]
                self._watch_cursor = slot % FAVORITE_COUNT + 1
                self._next_watch_probe = now + (WATCH_LISTENING_INTERVAL if audible else WATCH_IDLE_INTERVAL)

        if probe:
            start = time.monotonic()
            level = receiver.watch_probe(signal)
            elapsed = time.monotonic() - start
            if level is not None:
                levels[signal] = level
            if audible:
                self._pad_watch_gap(primary, elapsed)

        with self.lock:
            if not self._watch_current(epoch, primary):
                return
            self._remember_watch_levels(targets, levels)

    def _remember_watch_levels(self, targets, levels):
        expires = time.monotonic() + WATCH_ACTIVITY_SECONDS
        threshold = self.scanner.squelch_db
        for slot, signal in self._watch_targets().items():
            level = levels.get(signal)
            if targets.get(slot) == signal and level is not None and math.isfinite(level) and level >= threshold:
                self._watch_activity[slot] = signal, expires

    def _pad_watch_gap(self, primary, elapsed):
        # Keep the browser's jitter buffer moving through a brief retune.
        # These samples are silence because the main station was not received.
        with self.lock:
            state = self.scanner.snapshot()
            if state["audio_open"] and target(state) == primary:
                samples = round(min(elapsed, 0.14) * SAMPLE_RATE)
                if samples:
                    self.publish(bytes(2 * samples), drop_oldest=False)

    def _set_watch_secondary(self, ready):
        if ready != self._watch_secondary_ready:
            self._watch_secondary_ready = ready
            self._clear_watch()
            self._next_watch_probe = 0.0

    def _secondary_watch_requested(self):
        return (self.watch_enabled and self.scanner.power and self.receiver_status == "ready"
                and not self.stopped.is_set())

    def _wait_watch_receiver(self, seconds):
        self._watch_wake.wait(seconds)
        self._watch_wake.clear()

    @staticmethod
    def _open_watch_receiver(receiver):
        for index in range(1, receiver.device_count()):
            receiver.index = index
            try:
                receiver.open()
                return True
            except ReceiverError as exc:
                LOG.debug("WATCH could not open SDR index %d: %s", index, exc)
        return False

    def _run_watch_receiver(self):
        """Use an available extra dongle without waiting on it in the audio worker."""
        receiver = Receiver(index=1)
        connected = False
        cursor = 1
        retry_at = next_check = 0.0
        try:
            while not self.stopped.is_set():
                with self.lock:
                    requested = self._secondary_watch_requested()
                    targets = self._watch_targets() if requested else {}
                    epoch = self._watch_epoch
                    paused = self.scanner.menu or self.scanner.preview_until

                if not targets:
                    if connected:
                        with self.lock:
                            self._set_watch_secondary(False)
                        receiver.close()
                        connected = False
                    retry_at = next_check = 0.0
                    cursor = 1
                    self._wait_watch_receiver(0.25)
                    continue

                now = time.monotonic()
                if paused or now < max(retry_at, next_check):
                    self._wait_watch_receiver(0.25 if paused else max(retry_at, next_check) - now)
                    continue

                try:
                    if not connected:
                        connected = self._open_watch_receiver(receiver)
                        if not connected:
                            retry_at = time.monotonic() + WATCH_RECEIVER_RETRY
                        continue

                    slot = min(targets, key=lambda value: (value - cursor) % FAVORITE_COUNT)
                    signal = targets[slot]
                    cursor = slot % FAVORITE_COUNT + 1
                    next_check = now + WATCH_INTERVAL
                    level = receiver.watch_probe(signal)
                    with self.lock:
                        if (level is not None and epoch == self._watch_epoch and self._secondary_watch_requested()
                                and not (self.scanner.menu or self.scanner.preview_until)):
                            self._set_watch_secondary(True)
                            self._remember_watch_levels(targets, {signal: level})
                except ReceiverError as exc:
                    LOG.debug("WATCH SDR unavailable: %s", exc)
                    with self.lock:
                        self._set_watch_secondary(False)
                    receiver.close()
                    connected = False
                    retry_at = time.monotonic() + WATCH_RECEIVER_RETRY
        finally:
            with self.lock:
                self._set_watch_secondary(False)
            receiver.close()

    def subscribe(self):
        # Absorb brief delivery jitter without letting a slow browser delay the tuner.
        client = queue.Queue(maxsize=6)
        with self.lock:
            self.clients.add(client)
        return client

    def unsubscribe(self, client):
        with self.lock:
            self.clients.discard(client)

    def discard_audio(self):
        for client in self.clients:
            while True:
                try:
                    client.get_nowait()
                except queue.Empty:
                    break

    def publish(self, pcm, *, drop_oldest=True):
        for client in self.clients:
            try:
                client.put_nowait(pcm)
            except queue.Full:
                if not drop_oldest:
                    continue
                try:
                    client.get_nowait()
                except queue.Empty:
                    pass
                client.put_nowait(pcm)

    def wait(self, seconds):
        self.wake.wait(seconds)
        self.wake.clear()

    def run(self):
        receiver = Receiver()
        connected = False
        tuned = None
        rds_since = None
        retry_at = 0.0
        try:
            while not self.stopped.is_set():
                with self.lock:
                    self.scanner.tick()
                    state = self.scanner.snapshot()
                    probe = self.scanner.take_close_probe() if connected else None
                    if not self.scanner.audio_gate_open:
                        self.recording.boundary()

                if not state["power"]:
                    if connected:
                        receiver.close()
                    connected = False
                    tuned = None
                    retry_at = 0.0
                    with self.lock:
                        self.receiver_status = "standby"
                        self.signal = None
                        self.rds = None
                        self.rds_target = None
                    self.wait(0.05)
                    continue

                if time.monotonic() < retry_at:
                    self.wait(0.10)
                    continue

                try:
                    if not connected:
                        receiver.open()
                        connected = True
                        with self.lock:
                            self.receiver_status = "ready"
                            self._watch_wake.set()
                        LOG.info("USB SDR opened")

                    if probe:
                        hit = receiver.probe(probe, windows=3)
                        if hit is not None:
                            with self.lock:
                                self.scanner.close_hit(hit)
                        continue

                    wanted = target(state)
                    if wanted is None:
                        if self.watch_enabled and not self._watch_secondary_ready:
                            self._watch(receiver, None)
                        self.wait(0.02)
                        continue
                    if wanted != tuned:
                        receiver.tune(*wanted)
                        tuned = wanted
                        rds_since = None

                    decode_rds = state["rds_enabled"] and wanted[1] == "WFM"
                    receiver.set_rds_enabled(decode_rds)
                    if not decode_rds:
                        rds_since = None
                    elif rds_since is None:
                        rds_since = time.monotonic()
                    level, pcm = receiver.read()
                    offset = getattr(receiver, "tuning_offset_hz", None)
                    rf_excess = getattr(receiver, "tuning_snr_db", None)
                    usable_level = usable_signal_level(level, wanted[1], offset, rf_excess)
                    rds = receiver.rds_status() if decode_rds else None
                    with self.lock:
                        current = self.scanner.snapshot()
                        if not current["power"] or target(current) != wanted:
                            continue
                        self.scanner.tick(usable_level if wanted[1] == "WFM" else level)
                        was_open = current["audio_open"]
                        current = self.scanner.snapshot()
                        self.rds = rds if current["rds_enabled"] and target(current) == wanted else None
                        self.rds_target = wanted if self.rds else None
                        primary = target(current)
                        self.signal = ((wanted, level, usable_level, offset)
                                       if primary == wanted and math.isfinite(level) else None)
                        audible = primary == wanted and current["audio_open"] and bool(pcm)
                        capture = (pcm if primary == wanted and self.scanner.audio_gate_open
                                   and math.isfinite(level) else None)
                        self.recording.receive(primary, capture)
                        if audible:
                            self.publish(pcm)
                        elif was_open:
                            self.discard_audio()
                    if self.watch_enabled and not self._watch_secondary_ready:
                        rds_ready = rds_since is not None and time.monotonic() - rds_since >= WATCH_RDS_GRACE
                        self._watch(receiver, primary, (wanted, level),
                                    allow_probe=primary == wanted, audible=audible, rds_ready=rds_ready)
                except ReceiverError as exc:
                    LOG.warning("USB SDR unavailable: %s", exc)
                    receiver.close()
                    connected = False
                    tuned = None
                    retry_at = time.monotonic() + 2
                    with self.lock:
                        self.receiver_status = "unavailable"
                        self._watch_wake.set()
                        self.signal = None
                        self.rds = None
                        self.rds_target = None
                        self._clear_watch()
                        self.recording.boundary()
        finally:
            receiver.close()

    def close(self):
        with self.lock:
            self.stopped.set()
            self.wake.set()
            self._watch_wake.set()
            self.recording.stop()
        if self.worker.ident is not None:
            self.worker.join(timeout=3)
        if self._watch_worker.ident is not None:
            self._watch_worker.join(timeout=3)
        self.recording.join()


def valid_action(action):
    if not isinstance(action, dict):
        return False
    if "record" in action:
        command = action["record"]
        if command == "toggle":
            return set(action) == {"record"}
        if command in ("browse", "select"):
            if command == "browse" and set(action) == {"record"}:
                return True
            path = action.get("path")
            return (set(action) == {"record", "path"} and isinstance(path, str)
                    and 0 < len(path) <= 1024 and "\0" not in path and Path(path).is_absolute())
        hz = action.get("hz")
        return (command == "delete" and set(action) == {"record", "hz"} and type(hz) is int
                and MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ)
    if "name" in action and set(action) != {"favorite", "name"}:
        return False
    if "key" in action:
        key = action["key"]
        return (isinstance(key, str) and (key in KEYS or key in KEY_ALIASES)
                and action.get("gesture", "tap") in ("tap", "long", "double"))
    if "favorite" in action:
        number = action["favorite"]
        if type(number) is not int or not 1 <= number <= FAVORITE_COUNT:
            return False
        if "name" in action:
            return normalize_favorite_name(action["name"]) is not None
        return action.get("gesture", "tap") in ("tap", "long")
    if "setting" in action:
        return action["setting"] in ("rds", "watch")
    if "tune" in action:
        if action["tune"] == "step":
            direction = action.get("direction")
            step_hz = action.get("step_hz", FINE_STEP_HZ)
            return (type(direction) is int and direction in (-1, 1)
                    and type(step_hz) is int and step_hz in WHEEL_STEPS_HZ)
        if action["tune"] == "frequency":
            mhz = action.get("mhz")
            return isinstance(mhz, str) and FREQUENCY_ENTRY.fullmatch(mhz) is not None
        return False
    knob = action.get("knob")
    value = action.get("value")
    held = action.get("held", [])
    return (
        knob in ("volume", "squelch")
        and type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)
        and isinstance(held, list) and len(held) <= len(KEYS)
        and all(isinstance(key, str) and (key in KEYS or key in KEY_ALIASES) for key in held)
    )


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string, *args):
        LOG.debug(format_string, *args)

    def local_request(self):
        port = self.server.server_port
        # The TCP endpoints are authoritative; forwarded headers cannot grant access.
        peer = IPv4Address(self.client_address[0])
        destination = IPv4Address(self.connection.getsockname()[0])
        loopback = peer.is_loopback and destination.is_loopback
        hosts = {f"{destination}:{port}"}
        if loopback:
            hosts.add(f"localhost:{port}")
        requested_hosts = self.headers.get_all("Host", ())
        host = requested_hosts[0].lower() if len(requested_hosts) == 1 else None
        origins = self.headers.get_all("Origin", ())
        if host not in hosts or len(origins) > 1 or (origins and origins[0] != f"http://{host}"):
            self.send_error(HTTPStatus.FORBIDDEN)
            return False
        if loopback:
            return True

        access = getattr(self.server, "remote_access", None)
        if access is not None and access.allows(peer, destination):
            return True
        self.send_error(HTTPStatus.FORBIDDEN)
        return False

    def response(self, content_type, body):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_response(self, result):
        self.response("application/json", json.dumps(result).encode("utf-8"))

    def do_GET(self):
        if not self.local_request():
            return
        if self.path == "/api/state":
            self.json_response(self.server.runtime.state())
            return
        if self.path == "/audio":
            self.stream_audio()
            return
        if self.path.startswith(RECORDINGS_URL):
            self.replay_recording()
            return
        name = "index.html" if self.path == "/" else self.path.removeprefix("/")
        if name not in ASSETS or not (ROOT / "web" / name).is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.response(mimetypes.guess_type(name)[0] or "application/octet-stream",
                      (ROOT / "web" / name).read_bytes())

    def do_HEAD(self):
        if not self.local_request():
            return
        if self.path.startswith(RECORDINGS_URL):
            self.replay_recording()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def replay_recording(self):
        runtime = self.server.runtime
        with runtime.lock:
            root = Path(runtime.recording.folder)
        route = self.path.removeprefix(RECORDINGS_URL)
        if route == "last":
            try:
                clip = latest_clip(root)
            except OSError as error:
                LOG.warning("Could not list recordings for playback: %s", error)
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Could not read the recording folder")
                return
            if clip is None:
                self.send_error(HTTPStatus.NOT_FOUND, "No completed recordings")
            else:
                self.json_response(clip)
            return

        try:
            stream, size = open_clip(root, route)
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "Recording unavailable")
            return

        with stream:
            requested = self.headers.get("Range")
            try:
                start, end = (0, size - 1) if requested is None else byte_range(requested, size)
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(HTTPStatus.OK if requested is None else HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if requested is not None:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                stream.seek(start)
                remaining = length
                while remaining:
                    block = stream.read(min(remaining, 65_536))
                    if not block:
                        self.close_connection = True
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
            except OSError:
                self.close_connection = True

    def do_POST(self):
        if not self.local_request():
            self.close_connection = True
            return
        if self.path != "/api/action":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048:
                raise ValueError("Invalid action length")
            action = json.loads(self.rfile.read(length))
            if not valid_action(action):
                raise ValueError("Invalid panel action")
        except (ValueError, UnicodeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        self.json_response(self.server.runtime.act(action))

    def stream_audio(self):
        runtime = self.server.runtime
        client = runtime.subscribe()
        self.close_connection = True
        try:
            self.connection.settimeout(2)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            while not runtime.stopped.is_set():
                try:
                    pcm = client.get(timeout=0.35)
                except queue.Empty:
                    pcm = SILENCE
                self.wfile.write(pcm)
        except OSError:
            pass
        finally:
            runtime.unsubscribe(client)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--state", type=Path, default=ROOT / ".scanner-state.json")
    parser.add_argument("--remote-config", type=Path, help="Owner-only JSON for private-network access")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    remote_access = None
    if args.remote_config is not None:
        try:
            remote_access = RemoteAccess.load(args.remote_config)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    bind = "0.0.0.0" if remote_access else "127.0.0.1"
    with ThreadingHTTPServer((bind, args.port), PanelHandler) as server:
        runtime = ScannerRuntime(args.state)
        server.runtime = runtime
        server.remote_access = remote_access
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(
            target=server.shutdown, daemon=True).start())
        runtime.start()
        LOG.info("Panel available at http://127.0.0.1:%d/", server.server_port)
        if remote_access:
            LOG.info("Private-network access is enabled for the configured IPv4 ranges.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            runtime.close()


if __name__ == "__main__":
    main()

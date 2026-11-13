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
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from bandplan import BANK_KEYS
from dab import BAND_III, SERVICE_ID, DabError, DabReceiver
from folder_picker import browse_folders, select_folder
from radio_pool import RadioPool
from receiver import SAMPLE_RATE, Receiver, ReceiverError
from recording import Recorder, RecordingError
from replay import RECORDINGS_URL, byte_range, latest_clip, open_clip
from scanner import (
    FAVORITE_COUNT, FINE_STEP_HZ, KEY_ALIASES, KEYS, MANUAL_MAX_HZ, MANUAL_MIN_HZ,
    Scanner, format_frequency, normalize_favorite_name,
)


ROOT = Path(__file__).resolve().parent
ASSETS = frozenset({"index.html", "panel.css", "panel.js", "panel.png", "field-background.webp", "workbench.webp",
                    "waterfall.js", "pcm-buffer.js", "pcm-worklet.js", "ashcooperuk-avatar.png"})
SILENCE = bytes(960)
FREQUENCY_ENTRY = re.compile(r"[0-9]{1,4}(?:\.[0-9]{0,6})?")
SESSION_QUERY = re.compile(r"session=([0-9a-f]{32})(?:&radio=([1-9][0-9]{0,5}))?")
WHEEL_STEPS_HZ = frozenset(10 ** position for position in range(10))
WATCH_INTERVAL = 0.12
WATCH_LISTENING_INTERVAL = 0.85
WATCH_IDLE_INTERVAL = 0.30
WATCH_ACTIVITY_SECONDS = 1.4
WATCH_ONLY_DWELL = 0.5
WATCH_ONLY_STALE_SECONDS = 2 * FAVORITE_COUNT * WATCH_ONLY_DWELL
WATCH_RDS_GRACE = 4.2
WATCH_RECEIVER_RETRY = 2.0
WATCH_SECONDARY_START_GRACE = 0.5
WATCH_EXIT_KEYS = {*BANK_KEYS, "band", "close", "down", "scan", "search", "up"}
DAB_ARROWS = {"up", "down"}
DAB_EXIT_KEYS = (set(BANK_KEYS) - {"fav"}) | {"band", "close", "watch"}
DAB_FREQUENCIES = dict(BAND_III)
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


@dataclass(frozen=True)
class RecordingTarget:
    hz: int
    modulation: str
    sid: str | None = None
    station_name: str | None = None

    @property
    def dab_identity(self):
        if self.sid is not None:
            return {"sid": self.sid, "name": self.station_name}
        return None


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

    def act(self, action, tuned, *, audio_open):
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
        elif action["hz"] != tuned.hz:
            self.message = "Frequency changed. Hold RECORD again to clear the tuned frequency."
        elif action.get("dab") != tuned.dab_identity:
            self.message = "Tuning changed. Hold RECORD again to clear the current recordings."
        else:
            self._finish()
            if self.enabled and audio_open:
                self._suppressed = tuned
            self._enqueue("delete", tuned)
            self.message = "Clearing recordings for the current station or frequency."

    def folder_action(self, action, *, parked=False):
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
                if parked and self.scanner.power:
                    return None
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
                if parked and self.scanner.power:
                    return None
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
                    _, tuned, pcm = args
                    self.recorder.feed(tuned.hz, pcm, skip_tone_only=tuned.modulation in ("AM", "FM"),
                                       station_name=tuned.station_name, dab_sid=tuned.sid)
                    with self.lock:
                        if epoch == self._epoch and self._capture == tuned:
                            self._writing = True
                elif command == "finish":
                    self.recorder.finish()
                elif command == "folder":
                    self._set_parent(*args)
                elif command == "delete":
                    tuned = args[0]
                    count = self.recorder.clear_frequency(tuned.hz, station_name=tuned.station_name, dab_sid=tuned.sid)
                    with self.lock:
                        files = "recording" if count == 1 else "recordings"
                        label = tuned.station_name or f"{tuned.hz / 1_000_000:.6f} MHz"
                        self.message = f"Cleared {count} {files} for {label}."
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


@dataclass
class WatchDwell:
    slot: int
    signal: tuple[int, str]
    until: float
    sampled: bool = False
    hit: bool = False


class ScannerRuntime:
    def __init__(self, state_path, *, receiver_index=0, watch_available=None):
        self.scanner = Scanner(state_path)
        self.receiver_index = receiver_index
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.recording = RecordingRuntime(self.scanner, self.lock, self.stopped)
        self.clients = set()
        self.receiver_status = "standby"
        self._receiver_reopen = False
        self.signal = None
        self.waterfall = None
        self.rds = None
        self.rds_target = None
        self.dab_enabled = False
        self._dab_status = {}
        self._dab_commands = deque(maxlen=12)
        self._dab_recall = None
        self._dab_favorite_slot = None
        self._dab_browse_favorites = False
        self._dab_epoch = 0
        self._dab_mode_generation = 0
        self._dab_save_retry_at = 0.0
        self.watch_enabled = False
        self.watch_only = False
        self._watch_mode_epoch = 0
        self._watch_dwell = None
        self._watch_activity = {}
        self._watch_epoch = 0
        self._watch_cursor = 1
        self._next_watch = 0.0
        self._next_watch_probe = 0.0
        self._watch_secondary_ready = False
        self._watch_available = watch_available
        self._watch_index = None
        self._watch_wake = threading.Event()
        self._watch_worker = threading.Thread(target=self._run_watch_receiver, name="WATCH receiver", daemon=True)
        self.worker = threading.Thread(target=self.run, name="SDR receiver", daemon=True)

    def start(self):
        self.worker.start()
        self._watch_worker.start()

    def reopen_receiver(self):
        """Ask the tuner worker to reopen its index after USB enumeration changes."""
        with self.lock:
            self._receiver_reopen = True
            self.wake.set()

    def _rds_for(self, state):
        if (self.rds and state["power"] and state["modulation"] == "WFM"
                and state["rds_enabled"] and self.receiver_status == "ready"
                and target(state) == self.rds_target):
            return self.rds
        return None

    def _dab_station(self):
        status = self._dab_status
        if status.get("sid") and not (status.get("scanning") or status.get("error")):
            return status["block"], status["sid"]
        return None

    def _dab_favorite_slots(self, station):
        return [slot for slot, entry in enumerate(self.scanner.dab_favorites, 1)
                if entry and (entry["block"], entry["sid"]) == station]

    def dab_handoff_station(self):
        with self.lock:
            if not (self.scanner.power and self.dab_enabled):
                return None
            status = self._dab_status
            if status.get("block") and status.get("target_sid"):
                return status["block"], status["target_sid"]
            return self._dab_station() or (self._dab_recall[1:] if self._dab_recall
                                           else self.scanner.dab_last_station)

    def resume_dab(self, station, volume):
        with self.lock:
            if self.act({"knob": "volume", "value": volume}) is None:
                return
            if not self.dab_enabled:
                self._set_dab(True)
            if station:
                self._recall_dab_favorite(None, {"block": station[0], "sid": station[1]})
            self.wake.set()
            self._watch_wake.set()

    def _dab_favorite_slot_for(self, matches):
        return self._dab_favorite_slot if self._dab_favorite_slot in matches else next(iter(matches), None)

    def _dab_favorite_name(self, slot):
        names = self.scanner.favorite_names
        return names[slot - 1] if slot and slot <= len(names) else ""

    def _dab_recording_target(self):
        station = self._dab_station()
        if not station:
            return None
        name = None
        if self.scanner.rds_enabled:
            favorite = self._dab_favorite_slot_for(self._dab_favorite_slots(station))
            name = self._dab_favorite_name(favorite) or self._dab_status.get("service") or None
        return RecordingTarget(self._dab_status["hz"], "DAB", station[1], name)

    def _show_dab(self, state):
        status = self._dab_status
        recall = (("recall", status["block"], status["recall_sid"]) if status.get("recall_sid")
                  else self._dab_recall if status.get("recalling") or not status else None)
        hz = DAB_FREQUENCIES[recall[1]] if recall else status.get("hz")
        error = status.get("error")
        scanning = status.get("scanning", False) and not (recall or status.get("recalling") or error)
        loading = bool(recall or status.get("audio_pending")) and not (error or scanning or status.get("audio_open"))
        if error:
            display = "Err"
        elif scanning:
            display = "SCAN"
        elif loading:
            display = "LOADING"
        else:
            display = format_frequency(hz) if hz else "no Ch"
        service, text = status.get("service", ""), status.get("text", "")
        station = self._dab_station()
        selected = station or (recall[1:] if recall else None)
        matches = self._dab_favorite_slots(selected)
        favorite = self._dab_favorite_slot_for(matches)
        metadata = {"ps": service, "text": text} if not loading and state["rds_enabled"] and (service or text) else None
        recording_target = self._dab_recording_target()
        programming = self.scanner.dab_programming
        if programming or (state["display"] in ("Pg.L", "OF Pg.L") and self.scanner.notice_until > time.monotonic()):
            display, channel = state["display"], state["channel"]
        else:
            channel = None if loading else favorite
        show_favorites = favorite or self._dab_browse_favorites or self.scanner.dab_program_slot is not None
        state.update(display=display, channel=channel, upper=["FAV", "DAB"] if show_favorites else ["DAB"],
                     lower=["HOLD", "CH"] if programming else ["SCAN"] if scanning else [],
                     blink_bank=None, blink=False, close_blink=False, scan_scroll=scanning and not programming,
                     scan_active=bool(scanning and hz and not programming), search_active=False, search_full=False,
                     tune_hz=hz, modulation="DAB", active_favorite=favorite if station else None,
                     dab_active_favorites=matches if station else [],
                     can_tune=False,
                     close_probe=None, audio_open=bool(status.get("audio_open") and not state["muted"]),
                     rds=metadata, dab_error=error, dab_rescanning=bool(status.get("rescanning")),
                     dab_tuning=loading,
                     dab_target_sid=status.get("target_sid") or (recall[2] if recall else None),
                     dab_browse_favorites=self._dab_browse_favorites, dab_programming=programming,
                     dab_recording_identity=recording_target.dab_identity if recording_target else None)

    def state(self, *, parked=False, parked_backlight=False):
        with self.lock:
            state = self.scanner.snapshot(parked=parked, parked_backlight=parked_backlight)
            dab_enabled = self.dab_enabled and state["power"]
            watch_only = self.watch_only and state["power"] and not dab_enabled
            if watch_only:
                state.update(display="-------", channel=None, upper=[], lower=[], blink_bank=None,
                             blink=False, close_blink=False, scan_scroll=False, scan_active=False,
                             search_active=False, search_full=False, tune_hz=None, modulation="FM",
                             active_favorite=None, close_probe=None, audio_open=False)
            state["receiver"] = "standby" if parked else self.receiver_status
            current_signal = (self.signal if self.signal and state["receiver"] == "ready"
                              and self.signal[0] == target(state) else None)
            state["signal_dbfs"] = current_signal[1] if current_signal else None
            state["signal_meter_dbfs"] = current_signal[2] if current_signal else None
            state["tuning_offset_hz"] = current_signal[3] if current_signal else None
            state.update(self.recording.state())
            if parked:
                state.update(record_enabled=False, record_active=False, record_choosing=False)
            rds = self._rds_for(state)
            state["rds"] = dict(rds) if rds else None
            state["watch_enabled"] = self.watch_enabled and not parked and not dab_enabled
            state["watch_only"] = watch_only
            state["watch_channel"] = None
            state["watch_active"] = []
            state["watch_receiver"] = None
            if state["watch_enabled"] and state["power"] and self.receiver_status == "ready":
                state["watch_receiver"] = "secondary" if self._watch_secondary_ready and not watch_only else "primary"
                now, targets = time.monotonic(), self._watch_targets()
                dwell = self._watch_dwell
                if watch_only and dwell is not None and targets.get(dwell.slot) == dwell.signal:
                    state["watch_channel"] = {"slot": dwell.slot, "hz": dwell.signal[0]}
                state["watch_active"] = [slot for slot, (signal, expires) in sorted(self._watch_activity.items())
                                         if expires > now and targets.get(slot) == signal]
            state["dab_enabled"] = dab_enabled
            if dab_enabled:
                self._show_dab(state)
            elif self.receiver_status == "unavailable" and state["power"]:
                state["audio_open"] = False
                if watch_only or state.get("audio_expected", state["tune_hz"] is not None):
                    state["display"] = "Err"
            capture = self.waterfall
            state["waterfall"] = (capture[1] if capture and state["power"] and state["receiver"] == "ready"
                                  and not (state["scan_active"] or state["search_active"] or state.get("dab_rescanning"))
                                  and capture[0] == target(state) else None)
            return state

    def act(self, action, *, parked=False, parked_backlight=False):
        if action.get("record") in ("browse", "select"):
            result = self.recording.folder_action(action, parked=parked)
            if result is None:
                return None
            return {**self.state(parked=parked, parked_backlight=parked_backlight), **result}
        with self.lock:
            if self.stopped.is_set() or (parked and self.scanner.power):
                return None
            before = self.scanner.snapshot()
            was_watch_only = self.watch_only
            was_dab = self.dab_enabled
            gesture = action.get("gesture", "tap")
            key = action.get("key")
            key = KEY_ALIASES.get(key, key)
            favorite = action.get("favorite")
            recall = favorite is not None and "name" not in action and gesture == "tap"
            if self.dab_enabled and (key in DAB_EXIT_KEYS
                    or action.get("setting") == "watch" or "tune" in action
                    or recall and not self.scanner.dab_programming and self.scanner.memories["FAV"][favorite - 1]):
                self._set_dab(False)
            changes_lockout = key == "lock" or (favorite is not None and gesture == "lock")
            previous_targets = self._watch_targets() if changes_lockout else None
            watch_key = key == "watch"
            watch_changed = watch_key or action.get("setting") == "watch"
            if action.get("setting") == "dab":
                if before["power"] and (not self.scanner.menu or self.scanner.dab_programming):
                    if gesture == "long":
                        self._start_dab_rescan()
                    else:
                        self._set_dab(not self.dab_enabled)
            elif self.dab_enabled and key and key != "mute":
                self._dab_key(key, gesture)
            elif watch_changed:
                if self.watch_only:
                    self._set_watch_only(False)
                elif (watch_key and before["power"] and not self.scanner.menu
                      and (gesture == "long" or target(before) is None)):
                    self._set_watch_only(True)
                elif gesture == "tap":
                    self.watch_enabled = not self.watch_enabled
                self._watch_cursor = 1
                self._next_watch_probe = 0.0
            elif "record" in action:
                if self.dab_enabled:
                    tuned = self._dab_recording_target()
                    self.recording.act(action, tuned, audio_open=self._dab_status.get("audio_open"))
                else:
                    tuned = None if self.watch_only else target(before)
                    self.recording.act(action, RecordingTarget(*tuned) if tuned else None,
                                       audio_open=self.scanner.audio_gate_open)
            elif "key" in action:
                key = action["key"]
                if KEY_ALIASES.get(key, key) in WATCH_EXIT_KEYS:
                    self._set_watch_only(False)
                if self.watch_only and key == "lock" and gesture == "long":
                    self.scanner.clear_temporary_lockouts()
                elif not self.watch_only or key == "mute":
                    self.scanner.key(key, gesture)
            elif "favorite" in action:
                if "name" in action:
                    self.scanner.rename_favorite(favorite, action["name"])
                elif recall and before["power"] and not self.scanner.menu and (
                        entry := self.scanner.get_dab_favorite(favorite)):
                    self._recall_dab_favorite(favorite, entry)
                elif self.dab_enabled and gesture == "long":
                    if before["power"] and not self.scanner.menu and (station := self._dab_station()):
                        saved = self.scanner.save_dab_favorite(
                            favorite, block=station[0], sid=station[1], hz=self._dab_status["hz"],
                            name=self._dab_status.get("service", ""),
                        )
                        if saved:
                            self._dab_favorite_slot = favorite
                else:
                    if self.watch_only and recall and self.scanner.memories["FAV"][favorite - 1]:
                        self._set_watch_only(False)
                    if not (self.watch_only or self.dab_enabled) or gesture == "lock":
                        rds = self._rds_for(before) if gesture == "long" else None
                        self.scanner.favorite(favorite, gesture, rds_ps=rds.get("ps") if rds else None)
            elif "setting" in action:
                self.scanner.toggle_rds()
            elif action.get("tune") == "step":
                self._set_watch_only(False)
                self.scanner.tune_step(action["direction"], action.get("step_hz", FINE_STEP_HZ))
            elif action.get("tune") == "frequency":
                self._set_watch_only(False)
                whole, _, fraction = action["mhz"].partition(".")
                hz = int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))
                self.scanner.tune_frequency(hz)
            else:
                self.scanner.knob(action["knob"], action["value"], action.get("held", ()))
                if before["power"] and not self.scanner.power:
                    self.scanner.cancel_dab_program()
                    self._dab_commands.clear()
                    self._dab_status = {}
                    if self.dab_enabled:
                        last = self.scanner.dab_last_station
                        self._dab_recall = ("recall", *last) if last else None
                    self._dab_mode_generation += 1
                    self._dab_epoch += 1
            if previous_targets is not None:
                self._watch_lockouts_changed(previous_targets)
            after = self.scanner.snapshot()
            if self.watch_enabled and not (self.watch_only or self.dab_enabled) and (
                    watch_changed or not before["power"] and after["power"]):
                self._next_watch_probe = time.monotonic() + WATCH_SECONDARY_START_GRACE
            watch_mode_changed = was_watch_only != self.watch_only
            dab_changed = was_dab != self.dab_enabled
            retuned = watch_mode_changed or dab_changed or target(before) != target(after)
            if retuned:
                self.signal = None
                self.waterfall = None
            if retuned or before["rds_enabled"] != after["rds_enabled"]:
                self.rds = None
                self.rds_target = None
            if (watch_changed or watch_mode_changed or dab_changed or (retuned and not self._watch_secondary_ready)
                    or before["power"] != after["power"]
                    or before["squelch"] != after["squelch"]):
                self._clear_watch()
            if retuned or (before["audio_open"] and not after["audio_open"]):
                self.discard_audio()
            receiving = self._dab_status.get("audio_open") if self.dab_enabled else self.scanner.audio_gate_open
            if retuned or not after["power"] or not receiving:
                self.recording.boundary()
            self.wake.set()
            self._watch_wake.set()
            return self.state(parked=parked, parked_backlight=parked_backlight)

    def _set_dab(self, enabled):
        self.scanner.cancel_dab_program()
        self.dab_enabled = enabled
        self._dab_epoch += 1
        self._dab_mode_generation += 1
        self._dab_commands.clear()
        self._dab_status = {}
        last_station = self.scanner.dab_last_station
        self._dab_recall = ("recall", *last_station) if enabled and last_station else None
        if self._dab_recall:
            self._queue_dab(self._dab_recall)
        self._dab_favorite_slot = None
        self._dab_browse_favorites = False
        self.scanner._clear_signal()
        self.scanner.probe_request = None

    def _queue_dab(self, command):
        self._dab_commands.append(command)
        self._dab_epoch += 1
        self.waterfall = None
        if isinstance(command, tuple) and command[0] == "recall":
            block = command[1]
            self._dab_status = {"hz": DAB_FREQUENCIES[block], "block": block, "scanning": True,
                                "recalling": True, "recall_sid": command[2]}
        elif command in DAB_ARROWS:
            status = self._dab_status
            scanning = status.get("scanning", False) and not status.get("recalling")
            self._dab_status = {"hz": status.get("hz"), "scanning": scanning,
                                "rescanning": status.get("rescanning", False),
                                "audio_pending": not scanning and bool(self.scanner.dab_station_ids)}
        else:
            self._dab_status = {"scanning": True, "rescanning": True}
        self.discard_audio()
        self.recording.boundary()

    def _remember_dab(self, station, blocks, service_counts, station_ids):
        now = time.monotonic()
        if now < self._dab_save_retry_at:
            return
        previous = self.scanner.dab_last_station
        if not station and previous and previous[0] not in blocks:
            previous = None
        station = station or previous
        if (station, blocks, service_counts, station_ids) == (
                self.scanner.dab_last_station, self.scanner.dab_known_blocks,
                self.scanner.dab_service_counts, self.scanner.dab_station_ids):
            return
        try:
            self.scanner.remember_dab(station, blocks, service_counts, station_ids)
        except OSError as error:
            self._dab_save_retry_at = now + 5.0
            LOG.warning("Could not remember DAB reception: %s", error)

    def _start_dab_rescan(self):
        if not self.dab_enabled:
            self._set_dab(True)
        self.scanner.cancel_dab_program()
        self._dab_commands.clear()
        self._dab_recall = self._dab_favorite_slot = None
        self._dab_browse_favorites = False
        last = self.scanner.dab_last_station
        self._queue_dab(("rescan", *last) if last else "rescan")

    def _recall_dab_favorite(self, slot, entry):
        self._set_watch_only(False)
        if not self.dab_enabled:
            self._set_dab(True)
        station = entry["block"], entry["sid"]
        command = ("recall", *station)
        pending = self._dab_status.get("recalling") or not self._dab_status
        same_favorite = self._dab_favorite_slot == slot
        self._dab_favorite_slot = slot
        if station != self._dab_station() and not (same_favorite and pending and command == self._dab_recall):
            self._dab_recall = command
            self._dab_commands.clear()
            self._queue_dab(self._dab_recall)

    def _step_dab_favorite(self, direction):
        entries = [(slot, entry) for slot, entry in enumerate(self.scanner.dab_favorites, 1) if entry]
        if not entries:
            return
        slots = [slot for slot, _ in entries]
        station = self._dab_station()
        current = self._dab_favorite_slot or next((slot for slot, entry in entries
                                                  if (entry["block"], entry["sid"]) == station), None)
        if current in slots:
            index = (slots.index(current) + direction) % len(slots)
        else:
            index = 0 if direction == 1 else -1
        self._recall_dab_favorite(*entries[index])

    def _dab_key(self, key, gesture):
        if not self.scanner.power:
            return
        if self.scanner.dab_programming:
            if slot := self.scanner.dab_program_key(key, gesture):
                self._dab_favorite_slot = slot
        elif key == "prog":
            if gesture == "long":
                self.scanner.key(key, gesture)
            elif station := self._dab_station():
                self.scanner.begin_dab_program(block=station[0], sid=station[1],
                                               hz=self._dab_status["hz"],
                                               name=self._dab_status.get("service", ""))
        elif key == "fav":
            self._dab_browse_favorites = not self._dab_browse_favorites
            if self._dab_browse_favorites and not self._dab_favorite_slot:
                current = self.state()["active_favorite"]
                if current:
                    self._dab_favorite_slot = current
                else:
                    self._step_dab_favorite(1)
        elif self._dab_browse_favorites and key in DAB_ARROWS:
            self._step_dab_favorite(1 if key == "up" else -1)
        elif key in DAB_ARROWS:
            self._dab_browse_favorites = False
            self._dab_recall = self._dab_favorite_slot = None
            self._queue_dab(key)

    def _set_watch_only(self, enabled):
        if self.watch_only != enabled:
            self.watch_only = self.watch_enabled = enabled
            self._watch_mode_epoch += 1
            self._watch_cursor = 1
            self._next_watch_probe = 0.0
            self.scanner._clear_signal()
            self.scanner.probe_request = None

    def _clear_watch(self):
        self._watch_epoch += 1
        self._watch_activity.clear()
        self._watch_dwell = None
        self._next_watch = 0.0

    def _watch_targets(self):
        channels = self.scanner.memories["FAV"][:FAVORITE_COUNT]
        return {slot: (channel.hz, channel.modulation) for slot, channel in enumerate(channels, 1)
                if channel and not self.scanner._lock_kind(channel)}

    def _watch_lockouts_changed(self, previous):
        targets = self._watch_targets()
        if targets == previous:
            return
        self._watch_epoch += 1
        self._watch_activity = {slot: entry for slot, entry in self._watch_activity.items()
                                if targets.get(slot) == entry[0]}
        dwell = self._watch_dwell
        if dwell is not None and targets.get(dwell.slot) != dwell.signal:
            self._watch_dwell = None

    def _watch_only_step(self, receiver):
        """Sample the selected favourite several times before advancing."""
        with self.lock:
            if not self.watch_only or not self.scanner.power or self._receiver_reopen or self.stopped.is_set():
                return
            now, epoch = time.monotonic(), self._watch_epoch
            targets = self._watch_targets()
            dwell = self._watch_dwell
            if dwell is not None:
                same_channel = targets.get(dwell.slot) == dwell.signal
                if not same_channel or now >= dwell.until:
                    if not same_channel or (dwell.sampled and not dwell.hit):
                        self._watch_activity.pop(dwell.slot, None)
                    self._watch_dwell = dwell = None
            if not targets:
                return
            if dwell is None:
                slot = min(targets, key=lambda value: (value - self._watch_cursor) % FAVORITE_COUNT)
                self._watch_dwell = dwell = WatchDwell(slot, targets[slot], now + WATCH_ONLY_DWELL)
                self._watch_cursor = slot % FAVORITE_COUNT + 1
                self._next_watch = now
            if now < self._next_watch:
                return
            self._next_watch = now + WATCH_INTERVAL

        level = receiver.watch_probe(dwell.signal, restore=False)
        with self.lock:
            if (self._watch_epoch != epoch or self._watch_dwell is not dwell or self._receiver_reopen
                    or self.stopped.is_set() or level is None or not math.isfinite(level)):
                return
            dwell.sampled = True
            if level >= self.scanner.squelch_db:
                dwell.hit = True
                self._watch_activity[dwell.slot] = (dwell.signal, time.monotonic() + WATCH_ONLY_STALE_SECONDS)

    def _watch_current(self, epoch, primary):
        return (self.watch_enabled and not self.watch_only and not self._watch_secondary_ready
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
            if not self.watch_only:
                self._clear_watch()
                self._next_watch_probe = 0.0

    def _secondary_watch_requested(self):
        return (self.watch_enabled and not (self.watch_only or self.dab_enabled)
                and self.scanner.power and self.receiver_status == "ready"
                and not self.stopped.is_set())

    def _wait_watch_receiver(self, seconds):
        self._watch_wake.wait(seconds)
        self._watch_wake.clear()

    @property
    def watch_index(self):
        """Return the SDR index WATCH is opening or using."""
        with self.lock:
            return self._watch_index

    def _watch_index_allowed(self, index):
        # The session pool can call into this runtime while checking reservations.
        return self._watch_available is None or self._watch_available(index)

    def _open_watch_receiver(self, receiver):
        for index in range(receiver.device_count()):
            if index == self.receiver_index:
                continue
            with self.lock:
                self._watch_index = index
            if not self._watch_index_allowed(index):
                continue
            receiver.index = index
            try:
                receiver.open()
                return True
            except ReceiverError as exc:
                LOG.debug("WATCH could not open SDR index %d: %s", index, exc)
        with self.lock:
            self._watch_index = None
        return False

    def _close_watch_receiver(self, receiver):
        with self.lock:
            self._set_watch_secondary(False)
        try:
            receiver.close()
        finally:
            with self.lock:
                self._watch_index = None

    def _run_watch_receiver(self):
        """Use an available extra dongle without waiting on it in the audio worker."""
        receiver = Receiver(index=0 if self.receiver_index else 1)
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

                idle = not targets or paused
                if connected and (idle or not self._watch_index_allowed(receiver.index)):
                    self._close_watch_receiver(receiver)
                    connected = False
                if idle:
                    retry_at = next_check = 0.0
                    if not targets:
                        cursor = 1
                    self._wait_watch_receiver(0.25)
                    continue

                now = time.monotonic()
                if now < max(retry_at, next_check):
                    self._wait_watch_receiver(max(retry_at, next_check) - now)
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
                    self._close_watch_receiver(receiver)
                    connected = False
                    retry_at = time.monotonic() + WATCH_RECEIVER_RETRY
        finally:
            self._close_watch_receiver(receiver)

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
        receiver = Receiver(index=self.receiver_index) if self.receiver_index else Receiver()
        dab = None
        dab_generation = None
        dab_resume = None
        dab_retry_at = 0.0
        connected = False
        tuned = None
        rds_since = None
        retry_at = 0.0
        try:
            while not self.stopped.is_set():
                with self.lock:
                    reopen = self._receiver_reopen
                    mode_generation = self._dab_mode_generation
                    if reopen:
                        self._receiver_reopen = False
                        self.receiver_status = "standby"
                        self.scanner._clear_signal()
                        self.signal = self.rds = self.rds_target = None
                        self.waterfall = None
                        self._dab_status = {}
                        self.discard_audio()
                        self._clear_watch()
                        self._watch_wake.set()
                        self.recording.boundary()
                    else:
                        dab_enabled = self.dab_enabled and self.scanner.power
                        dab_epoch = self._dab_epoch
                        watch_only = self.watch_only and self.scanner.power and not dab_enabled
                        watch_mode_epoch = self._watch_mode_epoch
                        if not (watch_only or dab_enabled):
                            self.scanner.tick()
                        state = self.scanner.snapshot()
                        probe = self.scanner.take_close_probe() if connected and not (watch_only or dab_enabled) else None
                        if not dab_enabled and (watch_only or not self.scanner.audio_gate_open):
                            self.recording.boundary()

                if mode_generation != dab_generation:
                    if dab is not None:
                        dab.close()
                        dab = None
                    dab_generation = mode_generation
                    dab_resume = None
                    dab_retry_at = 0.0

                if reopen:
                    if dab is not None:
                        dab_resume = dab.recovery_command() or dab_resume
                        dab.close()
                        dab = None
                    dab_retry_at = 0.0
                    if connected:
                        receiver.close()
                    connected = False
                    tuned = None
                    rds_since = None
                    retry_at = 0.0
                    continue

                if not dab_enabled:
                    if dab is not None:
                        dab.close()
                        dab = None
                    dab_retry_at = 0.0
                if not state["power"]:
                    if connected:
                        receiver.close()
                    connected = False
                    tuned = None
                    retry_at = 0.0
                    with self.lock:
                        self.receiver_status = "standby"
                        self.signal = None
                        self.waterfall = None
                        self.rds = None
                        self.rds_target = None
                        self._dab_status = {}
                    self.wait(0.05)
                    continue

                if dab_enabled:
                    if connected:
                        receiver.close()
                        connected = False
                    tuned = rds_since = None
                    if time.monotonic() < dab_retry_at:
                        self.wait(0.10)
                        continue
                    with self.lock:
                        if dab_epoch != self._dab_epoch or not self.scanner.power:
                            continue
                        resume = dab_resume or self._dab_recall
                        command = self._dab_commands.popleft() if self._dab_commands else (
                            resume if dab is None else None)
                        known_blocks = self.scanner.dab_known_blocks
                        service_counts = self.scanner.dab_service_counts
                        station_ids = self.scanner.dab_station_ids
                    try:
                        if dab is None:
                            anchor = resume[1:] if isinstance(resume, tuple) else None
                            dab = DabReceiver(self.receiver_index, known_blocks=known_blocks,
                                              service_counts=service_counts, station_ids=station_ids,
                                              browse_anchor=anchor)
                        status, pcm = dab.step(command)
                    except DabError as error:
                        LOG.warning("DAB receiver unavailable: %s", error)
                        dab_resume = (dab.recovery_command() if dab is not None else None) or command or resume
                        if dab is not None:
                            dab.close()
                            dab = None
                        with self.lock:
                            if dab_epoch == self._dab_epoch:
                                self._dab_status = {"error": str(error)}
                                if isinstance(dab_resume, tuple) and dab_resume[0] == "recall":
                                    _, block, sid = dab_resume
                                    self._dab_status.update(block=block, hz=DAB_FREQUENCIES[block], target_sid=sid)
                                self.receiver_status = "unavailable"
                                self.waterfall = None
                                self.discard_audio()
                                self.recording.boundary()
                        dab_retry_at = time.monotonic() + 3.0
                    else:
                        dab_resume = None
                        with self.lock:
                            if (dab_epoch == self._dab_epoch and self.scanner.power
                                    and not self._receiver_reopen and not self._dab_commands):
                                self._dab_status = status
                                frame = dab.waterfall
                                self.waterfall = ((status["hz"], "DAB"), frame) if frame else None
                                station = self._dab_station() if status.get("audio_open") else None
                                if station:
                                    self._dab_recall = ("recall", *station)
                                elif status.get("target_sid"):
                                    self._dab_recall = ("recall", status["block"], status["target_sid"])
                                if not status.get("rescanning"):
                                    self._remember_dab(station, status.get("known_blocks", known_blocks),
                                                       status.get("service_counts", service_counts),
                                                       status.get("station_ids", station_ids))
                                self.receiver_status = "ready"
                                if pcm and status.get("audio_open"):
                                    if self.recording.enabled:
                                        self.recording.receive(self._dab_recording_target(), pcm)
                                    if not self.scanner.muted:
                                        self.publish(pcm)
                                elif not status.get("audio_open"):
                                    self.recording.boundary()
                    self.wait(0.01)
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
                        LOG.info("USB SDR %d opened", self.receiver_index + 1)

                    if probe:
                        hit = receiver.probe(probe, windows=3)
                        if hit is not None:
                            with self.lock:
                                if (not self._receiver_reopen and watch_mode_epoch == self._watch_mode_epoch
                                        and dab_epoch == self._dab_epoch):
                                    self.scanner.close_hit(hit)
                        continue

                    if watch_only:
                        tuned = rds_since = None
                        self._watch_only_step(receiver)
                        self.wait(0.02)
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
                        if (self._receiver_reopen or watch_mode_epoch != self._watch_mode_epoch
                                or dab_epoch != self._dab_epoch
                                or not current["power"] or target(current) != wanted):
                            continue
                        self.scanner.tick(usable_level if wanted[1] == "WFM" else level)
                        was_open = current["audio_open"]
                        current = self.scanner.snapshot()
                        self.rds = rds if current["rds_enabled"] and target(current) == wanted else None
                        self.rds_target = wanted if self.rds else None
                        primary = target(current)
                        self.signal = ((wanted, level, usable_level, offset)
                                       if primary == wanted and math.isfinite(level) else None)
                        frame = receiver.waterfall
                        self.waterfall = (wanted, frame) if primary == wanted and frame else None
                        audible = primary == wanted and current["audio_open"] and bool(pcm)
                        capture = (pcm if primary == wanted and self.scanner.audio_gate_open
                                   and math.isfinite(level) else None)
                        self.recording.receive(RecordingTarget(*primary) if capture else None, capture)
                        if audible:
                            self.publish(receiver.level_live_audio(pcm))
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
                        self.waterfall = None
                        self.rds = None
                        self.rds_target = None
                        self._clear_watch()
                        self.recording.boundary()
        finally:
            if dab is not None:
                dab.close()
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
        if (command != "delete" or type(hz) is not int or not MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ
                or set(action) not in ({"record", "hz"}, {"record", "hz", "dab"})):
            return False
        if "dab" not in action:
            return True
        dab = action["dab"]
        return (isinstance(dab, dict) and set(dab) == {"sid", "name"}
                and isinstance(dab["sid"], str) and SERVICE_ID.fullmatch(dab["sid"]) is not None
                and (dab["name"] is None or normalize_favorite_name(dab["name"]) is not None))
    if "name" in action and set(action) != {"favorite", "name"}:
        return False
    if "key" in action:
        key = action["key"]
        if key == "watch":
            return action.get("gesture", "tap") in ("tap", "long")
        return (isinstance(key, str) and (key in KEYS or key in KEY_ALIASES)
                and action.get("gesture", "tap") in ("tap", "long", "double"))
    if "favorite" in action:
        number = action["favorite"]
        if type(number) is not int or not 1 <= number <= FAVORITE_COUNT:
            return False
        if "name" in action:
            return normalize_favorite_name(action["name"]) is not None
        return action.get("gesture", "tap") in ("tap", "long", "lock")
    if "setting" in action:
        setting = action["setting"]
        return setting in ("rds", "watch", "dab") and (
            "gesture" not in action or (setting == "dab" and action["gesture"] in ("tap", "long")))
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

    def response(self, content_type, body, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_response(self, result, status=HTTPStatus.OK):
        self.response("application/json", json.dumps(result).encode("utf-8"), status)

    def session_route(self):
        path, separator, query = self.path.partition("?")
        if not separator:
            return path, None, None
        match = SESSION_QUERY.fullmatch(query)
        if match is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid scanner session")
            return None
        token, radio = match.groups()
        return path, token, int(radio) if radio else None

    def radio(self, token, *, touch=True):
        pool = getattr(self.server, "radios", None)
        if pool is None:
            return self.server.runtime, 1, False
        return pool.resolve(token, touch=touch)

    def radio_state(self, state, radio_id, shared, token):
        pool = getattr(self.server, "radios", None)
        if token is not None and pool is not None and state["power"] and pool.muted(token):
            state = {**state, "muted": True, "audio_open": False}
        return {**state, "radio_id": radio_id, "radio_shared": shared}

    def panel_state(self, runtime, radio_id, shared, token):
        if radio_id is None:
            state = runtime.state(parked=True, parked_backlight=self.server.radios.parked_backlight(token))
        else:
            state = runtime.state()
        return self.radio_state(state, radio_id, shared, token)

    def do_GET(self):
        if not self.local_request():
            return
        route = self.session_route()
        if route is None:
            return
        path, token, expected_radio = route
        if path == "/api/state":
            runtime, radio_id, shared = self.radio(token)
            self.json_response(self.panel_state(runtime, radio_id, shared, token))
            return
        if path == "/audio":
            runtime, radio_id, _ = self.radio(token)
            if radio_id is None:
                self.send_error(HTTPStatus.CONFLICT, "Scanner panel is in standby")
                return
            self.stream_audio(runtime, token)
            return
        if path.startswith(RECORDINGS_URL):
            self.replay_recording(path, token, expected_radio)
            return
        name = "index.html" if path == "/" else path.removeprefix("/")
        if name not in ASSETS or not (ROOT / "web" / name).is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.response(mimetypes.guess_type(name)[0] or "application/octet-stream",
                      (ROOT / "web" / name).read_bytes())

    def do_HEAD(self):
        if not self.local_request():
            return
        route = self.session_route()
        if route is None:
            return
        path, token, expected_radio = route
        if path.startswith(RECORDINGS_URL):
            self.replay_recording(path, token, expected_radio)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def replay_recording(self, path, token, expected_radio):
        runtime, radio_id, _ = self.radio(token)
        if expected_radio is not None and expected_radio != radio_id:
            self.send_error(HTTPStatus.CONFLICT, "Scanner receiver changed")
            return
        with runtime.lock:
            root = Path(runtime.recording.folder)
        route = path.removeprefix(RECORDINGS_URL)
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
        route = self.session_route()
        if route is None:
            self.close_connection = True
            return
        path, token, expected_radio = route
        pool = getattr(self.server, "radios", None)
        if path == "/api/session/leave" and token is not None:
            if self.headers.get("Content-Length", "0") != "0" or "Transfer-Encoding" in self.headers:
                self.close_connection = True
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if pool is not None:
                pool.leave(token)
            self.json_response({})
            return
        if path != "/api/action":
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
        if pool is not None and token is not None and action.get("key") == "mute":
            runtime, radio_id, shared = pool.mute(token, action.get("gesture", "tap"))
            self.json_response(self.panel_state(runtime, radio_id, shared, token))
            return
        if pool is not None and token is not None and action.get("knob") == "volume":
            runtime, radio_id, shared, accepted = pool.volume(token, expected_radio, action)
            status = HTTPStatus.OK if accepted else HTTPStatus.CONFLICT
            self.json_response(self.panel_state(runtime, radio_id, shared, token), status)
            return
        runtime, radio_id, shared = self.radio(token)
        if expected_radio is None or expected_radio == radio_id:
            if radio_id is None:
                result = runtime.act(action, parked=True, parked_backlight=pool.parked_backlight(token))
            else:
                result = runtime.act(action)
            if result is not None:
                self.json_response(self.radio_state(result, radio_id, shared, token))
                return
            runtime, radio_id, shared = self.radio(token, touch=False)
        self.json_response(self.panel_state(runtime, radio_id, shared, token), HTTPStatus.CONFLICT)

    def stream_audio(self, runtime, token):
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
            check_at = time.monotonic() + 0.35
            while not runtime.stopped.is_set():
                if token is not None and time.monotonic() >= check_at:
                    assigned, radio_id, _ = self.radio(token, touch=False)
                    if assigned is not runtime or radio_id is None:
                        break
                    check_at = time.monotonic() + 0.35
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
        radios = RadioPool(args.state, ScannerRuntime)
        server.radios = radios
        server.remote_access = remote_access
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(
            target=server.shutdown, daemon=True).start())
        radios.start()
        LOG.info("Panel available at http://127.0.0.1:%d/", server.server_port)
        if remote_access:
            LOG.info("Private-network access is enabled for the configured IPv4 ranges.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            radios.close()


if __name__ == "__main__":
    main()

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
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network, IPv6Address
from pathlib import Path

from access_network import LocalNetworks
from access_pin import ACCESS_SECONDS, PIN_PATTERN, REMOTE_AUTHORITY, AccessConflict, AccessPin
from bandplan import BANK_KEYS
from dab import BAND_III, SERVICE_ID, DabError, DabReceiver
from discovery import DISCOVERY_URL, MAX_HITS, Discovery, wave_audio
from dmr import DmrState, valid_dmr_action
from dmr_decoder import DmrDecoder, DmrDecoderError
from folder_picker import browse_folders, select_folder
from radio_pool import RadioPool
from receiver import SAMPLE_RATE, Receiver, ReceiverError
from recording import MIN_CLIP_PCM_BYTES, Recorder, RecordingError
from replay import RECORDINGS_URL, byte_range, latest_clip, open_clip
from scanner import (
    FAVORITE_COUNT, FINE_STEP_HZ, KEY_ALIASES, KEYS, MANUAL_MAX_HZ, MANUAL_MIN_HZ,
    ChirpConflict, Scanner, format_frequency, normalize_favorite_name,
)
from tone_search import normalize_tone_filter


ROOT = Path(__file__).resolve().parent
ASSETS = frozenset({"index.html", "panel.css", "panel.js", "dmr-panel.css", "dmr-panel.js", "panel.png", "field-background.webp", "workbench.webp",
                    "waterfall.js", "pcm-buffer.js", "pcm-worklet.js", "ashcooperuk-avatar.png"})
SILENCE = bytes(960)
FREQUENCY_ENTRY = re.compile(r"[0-9]{1,4}(?:\.[0-9]{0,6})?")
SESSION_QUERY = re.compile(r"session=([0-9a-f]{32})(?:&radio=([1-9][0-9]{0,5}))?")
CHIRP_ROUTE = "/api/chirp"
CHIRP_LIMIT = 8 * 1024 * 1024
PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Port",
                 "X-Forwarded-Proto", "X-Real-IP", "CF-Connecting-IP", "CF-Visitor", "CF-Ray")
WHEEL_STEPS_HZ = frozenset(10 ** position for position in range(10))
WATCH_INTERVAL = 0.12
WATCH_LISTENING_INTERVAL = 0.85
WATCH_IDLE_INTERVAL = 0.30
WATCH_ACTIVITY_SECONDS = 1.4
WATCH_ONLY_DWELL = 0.5
WATCH_ONLY_STALE_SECONDS = 2 * FAVORITE_COUNT * WATCH_ONLY_DWELL
WATCH_RDS_GRACE = 4.2
WATCH_RECEIVER_RETRY = 2.0
DMR_SQUELCH_HANG = 0.09  # Bridge the gaps between TDMA bursts.
DMR_SMS_HISTORY = 24
WATCH_SECONDARY_START_GRACE = 0.5
WATCH_EXIT_KEYS = {*BANK_KEYS, "band", "close", "down", "scan", "search", "sideband", "up"}
DAB_ARROWS = {"up", "down"}
DAB_EXIT_KEYS = (set(BANK_KEYS) - {"fav"}) | {"band", "close", "sideband", "watch"}
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


def request_host_parts(headers):
    hosts = headers.get_all("Host", ())
    if len(hosts) != 1 or len(hosts[0]) > 260 or not hosts[0].isascii():
        return None
    match = REMOTE_AUTHORITY.fullmatch(hosts[0].lower())
    if match is None:
        return None
    host, port = match.groups()
    if (port and int(port) > 65535) or len(host) > 254:
        return None
    if host.startswith("["):
        try:
            host = f"[{IPv6Address(host[1:-1])}]"
        except ValueError:
            return None
    return host, port


def request_origin_for(host, port, scheme):
    default_port = "443" if scheme == "https" else "80"
    suffix = f":{port}" if port and port != default_port else ""
    return f"{scheme}://{host}{suffix}"


def usable_signal_level(level, modulation, offset, rf_excess):
    if modulation == "WFM" and rf_excess is not None:
        excess = rf_excess - WFM_NOISE_MARGIN_DB
        if excess <= 0:
            return -150.0
        # Estimate wanted power by removing the measured noise contribution.
        signal_fraction = -math.expm1(-excess * math.log(10) / 10)
        level += 10 * math.log10(signal_fraction)
    if offset is not None and modulation in TUNING_LIMITS_HZ:
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
        self._save_slots = threading.BoundedSemaphore(4)
        self._saved_hits = {}
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

    def queue_hit_save(self, hit, chunks):
        # Called under the scanner lock so shutdown and folder selection keep
        # their order relative to this save. The caller waits outside the lock.
        if self.stopped.is_set() or not self._save_slots.acquire(blocking=False):
            return None
        reply = queue.SimpleQueue()
        tuned = RecordingTarget(hit.hz, hit.mode, hit.dab_sid, hit.station_name)
        self._enqueue("save_hit", hit.id, tuned, hit.time_ms, chunks, reply)
        return reply

    def _save_hit(self, hit_id, tuned, time_ms, chunks, reply):
        key = self.recorder.root, hit_id
        try:
            filename = self._saved_hits.get(key)
            if filename is None or not self.recorder.has_saved_hit(filename, tuned.hz, dab_sid=tuned.sid):
                path = self.recorder.save_received(tuned.hz, time_ms, chunks,
                                                   station_name=tuned.station_name, dab_sid=tuned.sid)
                if path is None:
                    raise RecordingError("Could not save the Discovery clip.")
                filename = path.name
                self._saved_hits[key] = filename
                if len(self._saved_hits) > 4 * MAX_HITS:
                    self._saved_hits.pop(next(iter(self._saved_hits)))
            reply.put(({"filename": filename}, HTTPStatus.OK))
        except (RecordingError, OSError) as error:
            LOG.warning("Discovery recording failed: %s", error)
            reply.put(({"error": "Could not save the Discovery clip to the recording folder."},
                       HTTPStatus.SERVICE_UNAVAILABLE))
        finally:
            self._save_slots.release()

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
                elif command == "save_hit":
                    self._save_hit(*args)
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
        self.dmr = DmrState(self.scanner.state_path.with_suffix(".dmr.json"))
        self._dmr_error = None
        self._dmr_action_error = None
        self._dmr_messages = deque(maxlen=DMR_SMS_HISTORY)
        self._dmr_message_id = 0
        self._dmr_audio = False
        self._dmr_rf_until = 0.0
        self._dmr_started_from_standby = False
        self.receiver_index = receiver_index
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.recording = RecordingRuntime(self.scanner, self.lock, self.stopped)
        self.discovery = Discovery(SAMPLE_RATE)
        self.clients = set()
        self.receiver_status = "standby"
        self._receiver_reopen = False
        self.signal = None
        self.waterfall = None
        self.rds = None
        self.rds_target = None
        self.tone_enabled = False
        self.tone_result = None
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

    def chirp_download(self):
        with self.lock:
            return self.scanner.chirp_download()

    def chirp_upload(self, revision, changes, zones, settings, *, commit_access=None):
        with self.lock:
            if self.stopped.is_set():
                raise ChirpConflict("The scanner is shutting down. Download again after restarting it.")
            previous_watch = self._watch_targets()
            revision_before = self.scanner.tune_revision
            rds_before = self.scanner.rds_enabled
            image = self.scanner.chirp_upload(revision, changes, zones, settings, commit_access=commit_access)
            retuned = not self.dab_enabled and revision_before != self.scanner.tune_revision
            if retuned:
                self.signal = self.waterfall = self.tone_result = None
                self.discard_audio()
                self.recording.boundary()
                self.discovery.boundary()
            if retuned or rds_before != self.scanner.rds_enabled:
                self.rds = self.rds_target = None
            self._watch_lockouts_changed(previous_watch)
            self.wake.set()
            self._watch_wake.set()
            return image

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
            if self.dmr.enabled:
                self.act({"dmr": "toggle", "enabled": False})
            if self.act({"knob": "volume", "value": volume}, start_favorites=False) is None:
                return
            if not self.dab_enabled or self._dab_browse_favorites:
                self._set_dab(True)
            if station:
                self._recall_dab_favorite(None, {"block": station[0], "sid": station[1]})
            self.wake.set()
            self._watch_wake.set()

    def resume_dmr(self, hz, volume):
        with self.lock:
            self.act({"dmr": "toggle", "enabled": True})
            self.act({"knob": "volume", "value": volume})
            if hz is not None:
                self.act({"dmr": "tune", "hz": hz})

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
        if programming or state["favorite_edit"] == "delete" or (
                state["display"] in ("Pg.L", "OF Pg.L") and self.scanner.notice_until > time.monotonic()):
            display, channel = state["display"], state["channel"]
        else:
            channel = None if loading else favorite
        show_favorites = favorite or self._dab_browse_favorites or self.scanner.dab_program_slot is not None
        state.update(display=display, channel=channel, upper=["FAV", "DAB"] if show_favorites else ["DAB"],
                     lower=["HOLD", "CH"] if programming else ["SCAN"] if scanning
                     else ["HOLD"] if self._dab_browse_favorites else [],
                     blink_bank=None, blink=False, close_blink=False, scan_scroll=scanning and not programming,
                     scan_active=bool(scanning and hz and not programming), search_active=False, search_full=False,
                     band_search=None, band_search_range=None,
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
            dmr_enabled = self.dmr.enabled and not (parked and self.scanner.power)
            dab_enabled = self.dab_enabled and state["power"] and not dmr_enabled
            watch_only = self.watch_only and state["power"] and not (dab_enabled or dmr_enabled)
            state["dmr_enabled"] = dmr_enabled
            state["dmr"] = None
            if dmr_enabled:
                dmr = self.dmr.snapshot()
                state["dmr"] = {**dmr, "action_error": self._dmr_action_error,
                                 "messages": [] if parked else list(self._dmr_messages),
                                 "error": self._dmr_action_error or self._dmr_error or dmr.get("error")}
                state.update(tune_hz=dmr["hz"] if state["power"] else None, modulation="DMR",
                             audio_open=state["power"] and self._dmr_audio and not state["muted"],
                             can_tune=False, close_probe=None, active_favorite=None,
                             scan_active=state["power"] and dmr["mode"] == "scan",
                             search_active=state["power"] and dmr["mode"] == "search")
            if watch_only:
                state.update(display="-------", channel=None, upper=[], lower=[], blink_bank=None,
                             blink=False, close_blink=False, scan_scroll=False, scan_active=False,
                             search_active=False, search_full=False, band_search=None, band_search_range=None,
                             tune_hz=None, modulation="FM",
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
            state["discovery_started"] = self.discovery.started and not parked
            state["discovery_enabled"] = self.discovery.enabled and not parked
            state["discovery_current"] = self.discovery.current if state["power"] and not parked else None
            state["replay_available"] = self.discovery.replay_available and not parked
            rds = self._rds_for(state) if not dmr_enabled else None
            state["rds"] = dict(rds) if rds else None
            state["watch_enabled"] = self.watch_enabled and not parked and not (dab_enabled or dmr_enabled)
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
            state["tone_enabled"] = self.tone_enabled and state["power"] and not (dab_enabled or watch_only or dmr_enabled)
            tone = self.tone_result
            if dab_enabled or watch_only or dmr_enabled:
                state.update(tone_filter=None, tone_matched=None)
            state["tone_searching"] = bool((state["tone_enabled"] or state["tone_filter"])
                                           and state["modulation"] == "FM" and state["tune_hz"] is not None
                                           and self.scanner.signal_open and not self.scanner.menu)
            state["channel_tone"] = (tone[1] if state["tone_searching"] and current_signal
                                     and tone and tone[0] == target(state) else None)
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

    def act(self, action, *, parked=False, parked_backlight=False, start_favorites=True):
        if action.get("record") in ("browse", "select"):
            result = self.recording.folder_action(action, parked=parked)
            if result is None:
                return None
            return {**self.state(parked=parked, parked_backlight=parked_backlight), **result}
        with self.lock:
            if self.stopped.is_set() or (parked and self.scanner.power):
                return None
            if "discovery" in action:
                if parked:
                    return None
                self.discovery.control(action["discovery"])
                return self.state()
            if "dmr" in action or self.dmr.enabled:
                return self._act_dmr(action, parked=parked, parked_backlight=parked_backlight)
            before = self.scanner.snapshot()
            before_tune_revision = self.scanner.tune_revision
            tone_filter_saved = None
            was_watch_only = self.watch_only
            was_dab = self.dab_enabled
            gesture = action.get("gesture", "tap")
            key = action.get("key")
            key = KEY_ALIASES.get(key, key)
            favorite = action.get("favorite")
            recall = favorite is not None and not ({"name", "tone"} & action.keys()) and gesture == "tap"
            edit_favorite = favorite is not None and gesture in ("select", "delete")
            if self.dab_enabled and (key in DAB_EXIT_KEYS
                    or (key == "scan" and gesture == "long" and before["power"] and not self.scanner.menu)
                    or action.get("setting") == "watch" or "tune" in action
                    or recall and not self.scanner.dab_programming
                    and self.scanner.memories["FAV"][favorite - 1]):
                self._set_dab(False)
            changes_lockout = key == "lock" or (favorite is not None and gesture in ("delete", "lock"))
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
                if KEY_ALIASES.get(key, key) in WATCH_EXIT_KEYS or (key, gesture) == ("hold", "long"):
                    self._set_watch_only(False)
                if self.watch_only and key == "lock" and gesture == "long":
                    self.scanner.clear_temporary_lockouts()
                elif not self.watch_only or key == "mute":
                    self.scanner.key(key, gesture, recall_favorite=self._recall_saved_favorite)
            elif "favorite" in action:
                if "tone" in action:
                    try:
                        tone_filter_saved = self.scanner.set_favorite_tone(favorite, action["tone"], action.get("hz"))
                    except OSError as error:
                        LOG.warning("Could not save favourite tone: %s", error)
                        tone_filter_saved = False
                elif "name" in action:
                    self.scanner.rename_favorite(favorite, action["name"])
                elif edit_favorite:
                    deleted = self.scanner.favorite(favorite, gesture)
                    if deleted and self._dab_favorite_slot == favorite:
                        self._dab_favorite_slot = None
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
                if action["setting"] == "tone":
                    if before["power"] and not self.dab_enabled and not self.watch_only:
                        self.tone_enabled = not self.tone_enabled
                        self.tone_result = None
                else:
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
                self.scanner.knob(action["knob"], action["value"], action.get("held", ()),
                                  start_favorites=self._start_favorites if start_favorites else None)
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
            retuned = (watch_mode_changed or dab_changed or target(before) != target(after)
                       or (before_tune_revision != self.scanner.tune_revision
                           and bool(before["tone_filter"] or after["tone_filter"])))
            if retuned:
                self.signal = None
                self.waterfall = None
            if retuned or not after["power"] or not self.scanner.audio_gate_open:
                self.tone_result = None
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
                self.discovery.boundary()
            self.wake.set()
            self._watch_wake.set()
            state = self.state(parked=parked, parked_backlight=parked_backlight)
            if tone_filter_saved is not None:
                state["tone_filter_saved"] = tone_filter_saved
            return state

    def _dmr_boundary(self):
        self._dmr_audio = False
        self._dmr_rf_until = 0.0
        self.signal = self.waterfall = None
        self.discard_audio()
        self.recording.boundary()
        self.discovery.boundary()

    def _act_dmr(self, action, *, parked, parked_backlight):
        was_enabled, revision = self.dmr.enabled, self.dmr.revision
        was_power = self.scanner.power
        if command := action.get("dmr"):
            if command != "toggle" and not (self.dmr.enabled and was_power and not parked):
                return self.state(parked=parked, parked_backlight=parked_backlight)
            if command == "delete_sms":
                identity = action["id"], action["time_ms"]
                self._dmr_messages = deque(
                    (message for message in self._dmr_messages if (message["id"], message["time_ms"]) != identity),
                    maxlen=DMR_SMS_HISTORY,
                )
                self._dmr_action_error = self.dmr.notice = None
                return self.state()
            try:
                self.dmr.act(action)
            except OSError:
                self._dmr_action_error = "Could not save DMR favourites."
            except ValueError as error:
                self._dmr_action_error = str(error)
            else:
                self._dmr_action_error = None
                if self.dmr.enabled != was_enabled:
                    self._dmr_error = None
        elif knob := action.get("knob"):
            self.scanner.knob(knob, action["value"], start_favorites=lambda: None)
            if not was_power and self.scanner.power:
                self._dmr_started_from_standby = True
            if was_power and not self.scanner.power:
                self.dmr.power_off()
                self._dab_epoch += 1
                self._dab_mode_generation += 1
                if self.dab_enabled:
                    self._dab_status = {}
        elif action.get("key") == "mute":
            self.scanner.key("mute", action.get("gesture", "tap"))
        elif action.get("record") == "toggle" and was_power:
            self.recording.act(action, None, audio_open=False)
        if self.dmr.enabled != was_enabled:
            self._dab_epoch += 1
            self._dab_mode_generation += 1
            self.scanner._clear_signal()
            self.rds = self.rds_target = self.tone_result = None
            self._clear_watch()
            if not self.dmr.enabled:
                if self.scanner.power and self._dmr_started_from_standby:
                    self._start_favorites()
                self._dmr_started_from_standby = False
        if self.dmr.enabled != was_enabled or self.dmr.revision != revision or self.scanner.power != was_power:
            self._dmr_boundary()
        self.wake.set()
        self._watch_wake.set()
        return self.state(parked=parked, parked_backlight=parked_backlight)

    def _set_dab(self, enabled):
        self.scanner.cancel_dab_program()
        if enabled:
            self.scanner.exit_selected_scan()
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
        self.discovery.boundary()

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
        self.discovery.boundary()

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
        self._dab_favorite_slot = slot
        if slot is not None:
            self._dab_browse_favorites = True
        if station != self._dab_station() and not (pending and command == self._dab_recall):
            self._dab_recall = command
            self._dab_commands.clear()
            self._queue_dab(self._dab_recall)

    def _recall_saved_favorite(self, slot):
        if entry := self.scanner.get_dab_favorite(slot):
            self._recall_dab_favorite(slot, entry)
        else:
            if self.dab_enabled:
                self._set_dab(False)
            self.scanner._hold_channel(self.scanner.memories["FAV"][slot - 1])

    def _start_favorites(self, slot=None):
        self._set_watch_only(False)
        slot = slot or self.scanner.next_favorite_slot(1, include_dab=True)
        if self.dab_enabled and not (slot and self.scanner.get_dab_favorite(slot)):
            self._set_dab(False)
        self.scanner.hold_favorites(slot, recall_favorite=self._recall_saved_favorite)

    def _step_saved_favorite(self, direction):
        current = self._dab_favorite_slot or self._dab_favorite_slot_for(
            self._dab_favorite_slots(self._dab_station()))
        if slot := self.scanner.next_favorite_slot(direction, current, include_dab=True):
            self._recall_saved_favorite(slot)

    def _dab_key(self, key, gesture):
        if not self.scanner.power:
            return
        if self.scanner.dab_programming:
            if slot := self.scanner.dab_program_key(key, gesture):
                self._dab_favorite_slot = slot
        elif key == "hold" and gesture == "long":
            slot = self._dab_favorite_slot or self._dab_favorite_slot_for(
                self._dab_favorite_slots(self._dab_station()))
            self._start_favorites(slot)
        elif key == "prog":
            if gesture == "long":
                self.scanner.key(key, gesture)
            elif station := self._dab_station():
                self.scanner.begin_dab_program(block=station[0], sid=station[1],
                                               hz=self._dab_status["hz"],
                                               name=self._dab_status.get("service", ""))
        elif key == "fav":
            if gesture == "long":
                self.scanner.key(key, gesture)
                return
            self.scanner.end_favorite_edit()
            self._dab_browse_favorites = not self._dab_browse_favorites
            if self._dab_browse_favorites and not self._dab_favorite_slot:
                current = self.state()["active_favorite"]
                if current:
                    self._dab_favorite_slot = current
                else:
                    self._step_saved_favorite(1)
        elif self._dab_browse_favorites and key in DAB_ARROWS:
            self._step_saved_favorite(1 if key == "up" else -1)
        elif key in DAB_ARROWS:
            self._dab_browse_favorites = False
            self._dab_recall = self._dab_favorite_slot = None
            self._queue_dab(key)

    def _set_watch_only(self, enabled):
        if self.watch_only != enabled:
            if enabled:
                self.scanner.exit_selected_scan()
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
        return (self.watch_enabled and not (self.watch_only or self.dab_enabled or self.dmr.enabled)
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

    def _receive_dmr(self, wanted, revision, epoch, level, status, pcm, messages=()):
        with self.lock:
            if (self._receiver_reopen or not (self.dmr.enabled and self.scanner.power)
                    or revision != self.dmr.revision or epoch != self._dab_epoch):
                return
            for message in messages:
                self._dmr_message_id += 1
                received = {**message, "id": self._dmr_message_id,
                            "hz": wanted[0], "time_ms": time.time_ns() // 1_000_000}
                self._dmr_messages.appendleft(received)
                self.discovery.receive_sms(received, revision=revision,
                                           signal_dbfs=level if math.isfinite(level) else None)
            self.signal = (wanted, level, level, None) if math.isfinite(level) else None
            now = time.monotonic()
            if level >= self.scanner.squelch_db:
                self._dmr_rf_until = now + DMR_SQUELCH_HANG
            gate = self.dmr.receive(status if now < self._dmr_rf_until else {"sync": False}, now)
            if not gate:
                if self._dmr_audio:
                    self._dmr_boundary()
                    self.signal = (wanted, level, level, None) if math.isfinite(level) else None
                return
            self._dmr_audio = True
            if pcm:
                entry = self.dmr.entry
                name = entry["name"] if entry else None
                tuned = RecordingTarget(wanted[0], "DMR", station_name=name)
                self.recording.receive(tuned, pcm)
                self.discovery.receive(pcm, channel=wanted, revision=revision, signal_dbfs=level,
                                       station_name=name, dmr=self.dmr.call)
                if not self.scanner.muted:
                    self.publish(pcm)

    def run(self):
        receiver = Receiver(index=self.receiver_index) if self.receiver_index else Receiver()
        dab = None
        dab_generation = None
        dab_resume = None
        dab_retry_at = 0.0
        connected = False
        tuned = None
        tone_revision = None
        rds_since = None
        retry_at = 0.0
        decoder = None
        decoder_target = None
        decoder_retry_at = 0.0
        try:
            while not self.stopped.is_set():
                with self.lock:
                    reopen = self._receiver_reopen
                    mode_generation = self._dab_mode_generation
                    dmr_enabled = self.dmr.enabled and self.scanner.power
                    if reopen:
                        self._receiver_reopen = False
                        self.receiver_status = "standby"
                        self.scanner._clear_signal()
                        self.signal = self.rds = self.rds_target = None
                        self.waterfall = None
                        self.tone_result = None
                        self._dab_status = {}
                        self.dmr.stop_signal()
                        self._dmr_audio = False
                        self._dmr_rf_until = 0.0
                        self.discard_audio()
                        self._clear_watch()
                        self._watch_wake.set()
                        self.recording.boundary()
                        self.discovery.boundary()
                    else:
                        dab_enabled = self.dab_enabled and self.scanner.power and not dmr_enabled
                        dab_epoch = self._dab_epoch
                        watch_only = self.watch_only and self.scanner.power and not (dab_enabled or dmr_enabled)
                        watch_mode_epoch = self._watch_mode_epoch
                        if dmr_enabled:
                            revision = self.dmr.revision
                            self.dmr.tick()
                            if self.dmr.revision != revision:
                                self._dmr_boundary()
                            dmr_revision = self.dmr.revision
                            dmr_hz = self.dmr.hz
                            dmr_entry = self.dmr.entry or {}
                        elif not (watch_only or dab_enabled):
                            self.scanner.tick()
                        state = self.scanner.snapshot()
                        tune_revision = self.scanner.tune_revision
                        decode_tone = (state["modulation"] == "FM" and self.scanner.signal_open
                                       and not self.scanner.menu
                                       and (self.tone_enabled or state["tone_filter"] or self.discovery.enabled))
                        probe = self.scanner.take_close_probe() if connected and not (watch_only or dab_enabled or dmr_enabled) else None
                        if not (dab_enabled or dmr_enabled) and (watch_only or not self.scanner.audio_gate_open):
                            self.recording.boundary()
                            self.discovery.boundary()

                if decoder is not None and (reopen or not dmr_enabled):
                    decoder.close()
                    decoder = None
                    decoder_target = None
                if not dmr_enabled:
                    decoder_retry_at = 0.0

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
                                self.discovery.boundary()
                        dab_retry_at = time.monotonic() + 3.0
                    else:
                        dab_resume = None
                        with self.lock:
                            if (dab_epoch == self._dab_epoch and self.scanner.power
                                    and not self._receiver_reopen and not self._dab_commands):
                                previous_station = self._dab_station()
                                self._dab_status = status
                                frame = dab.waterfall
                                self.waterfall = ((status["hz"], "DAB"), frame) if frame else None
                                station = self._dab_station() if status.get("audio_open") else None
                                if station != previous_station:
                                    self.discovery.boundary()
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
                                    tuned_dab = (self._dab_recording_target()
                                                 if self.discovery.enabled or self.recording.enabled else None)
                                    if tuned_dab is None:
                                        self.discovery.receive(pcm)
                                    else:
                                        self.discovery.receive(pcm, channel=(tuned_dab.hz, tuned_dab.modulation),
                                                               revision=dab_epoch, dab_sid=tuned_dab.sid,
                                                               station_name=tuned_dab.station_name)
                                    if self.recording.enabled:
                                        self.recording.receive(tuned_dab, pcm)
                                    if not self.scanner.muted:
                                        self.publish(pcm)
                                elif not status.get("audio_open"):
                                    self.recording.boundary()
                                    self.discovery.boundary()
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

                    if dmr_enabled:
                        if not dmr_hz or time.monotonic() < decoder_retry_at:
                            self.wait(0.05)
                            continue
                        wanted = (dmr_hz, "DMR")
                        identity = (wanted, dmr_revision, dab_epoch)
                        if decoder is not None and decoder_target != identity:
                            decoder.close()
                            decoder = None
                        try:
                            new_decoder = decoder is None
                            if decoder is None:
                                decoder = DmrDecoder(**{key: dmr_entry.get(key)
                                                        for key in ("slot", "color_code", "talkgroup")})
                                decoder_target = identity
                                with self.lock:
                                    self._dmr_error = None
                            if wanted != tuned:
                                receiver.set_rds_enabled(False)
                                if getattr(receiver, "tone_search_active", False):
                                    receiver.set_tone_search(False)
                                receiver.tune(*wanted)
                                tuned = wanted
                                rds_since = None
                            if new_decoder:
                                with self.lock:
                                    if dmr_revision == self.dmr.revision and dab_epoch == self._dab_epoch:
                                        self.dmr.begin_dwell()
                            level, discriminator = receiver.read()
                            status, pcm = decoder.process(discriminator)
                            self._receive_dmr(wanted, dmr_revision, dab_epoch, level, status, pcm,
                                              decoder.take_messages())
                        except DmrDecoderError as error:
                            if decoder is not None:
                                decoder.close()
                                decoder = None
                            with self.lock:
                                self._dmr_error = str(error)
                                self.dmr.stop_signal()
                                self._dmr_boundary()
                            decoder_retry_at = time.monotonic() + 2
                        continue

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
                    if decode_tone or getattr(receiver, "tone_search_active", False):
                        receiver.set_tone_search(decode_tone)
                    if decode_tone and tone_revision != tune_revision:
                        receiver.reset_tone_search()
                    tone_revision = tune_revision
                    if not decode_rds:
                        rds_since = None
                    elif rds_since is None:
                        rds_since = time.monotonic()
                    level, pcm = receiver.read()
                    offset = getattr(receiver, "tuning_offset_hz", None)
                    rf_excess = getattr(receiver, "tuning_snr_db", None)
                    usable_level = usable_signal_level(level, wanted[1], offset, rf_excess)
                    rds = receiver.rds_status() if decode_rds else None
                    tone = receiver.tone_status() if decode_tone else None
                    with self.lock:
                        current = self.scanner.snapshot()
                        if (self._receiver_reopen or self.dmr.enabled or watch_mode_epoch != self._watch_mode_epoch
                                or dab_epoch != self._dab_epoch
                                or not current["power"] or target(current) != wanted
                                or tune_revision != self.scanner.tune_revision):
                            continue
                        self.scanner.tick(usable_level if wanted[1] == "WFM" else level, tone)
                        was_open = current["audio_open"]
                        current = self.scanner.snapshot()
                        self.rds = rds if current["rds_enabled"] and target(current) == wanted else None
                        self.rds_target = wanted if self.rds else None
                        primary = target(current)
                        tone_carrier = bool(current["tone_filter"] and self.scanner.signal_open)
                        self.tone_result = ((wanted, tone) if tone and primary == wanted and self.scanner.signal_open
                                            and tune_revision == self.scanner.tune_revision
                                            and (self.tone_enabled or current["tone_filter"]) else None)
                        self.signal = ((wanted, level, usable_level, offset)
                                       if primary == wanted and math.isfinite(level) else None)
                        frame = receiver.waterfall
                        self.waterfall = (wanted, frame) if primary == wanted and frame else None
                        same_channel = primary == wanted and tune_revision == self.scanner.tune_revision
                        audible = same_channel and current["audio_open"] and bool(pcm)
                        capture = (pcm if same_channel and self.scanner.audio_gate_open
                                   and math.isfinite(level) else None)
                        self.recording.receive(RecordingTarget(*primary) if capture else None, capture)
                        self.discovery.receive(capture, channel=primary,
                                               revision=tune_revision, signal_dbfs=level, tone=tone)
                        if audible:
                            self.publish(receiver.level_live_audio(pcm))
                        elif was_open:
                            self.discard_audio()
                    if self.watch_enabled and not self._watch_secondary_ready:
                        rds_ready = rds_since is not None and time.monotonic() - rds_since >= WATCH_RDS_GRACE
                        self._watch(receiver, primary, (wanted, level),
                                    allow_probe=primary == wanted and not tone_carrier, audible=audible, rds_ready=rds_ready)
                except ReceiverError as exc:
                    LOG.warning("USB SDR unavailable: %s", exc)
                    if decoder is not None:
                        decoder.close()
                        decoder = None
                        decoder_target = None
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
                        self.tone_result = None
                        self._clear_watch()
                        self.dmr.stop_signal()
                        self._dmr_audio = False
                        self._dmr_rf_until = 0.0
                        self.recording.boundary()
                        self.discovery.boundary()
        finally:
            if decoder is not None:
                decoder.close()
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
    if "dmr" in action:
        if action["dmr"] == "delete_sms":
            return (action.keys() == {"dmr", "id", "time_ms"}
                    and all(type(action[key]) is int and 0 < action[key] < (1 << 53)
                            for key in ("id", "time_ms")))
        return valid_dmr_action(action)
    if "discovery" in action:
        return set(action) == {"discovery"} and action["discovery"] in ("open", "toggle")
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
    if "tone" in action:
        favorite, tone, hz = action.get("favorite"), action["tone"], action.get("hz")
        return (set(action) in ({"favorite", "tone"}, {"favorite", "tone", "hz"})
                and type(favorite) is int and 1 <= favorite <= FAVORITE_COUNT
                and ("hz" not in action or type(hz) is int and MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ)
                and (tone is None or normalize_tone_filter(tone) is not None))
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
        return action.get("gesture", "tap") in ("tap", "long", "lock", "select", "delete")
    if "setting" in action:
        setting = action["setting"]
        return setting in ("rds", "watch", "dab", "tone") and (
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

    def accept_request(self):
        port = self.server.server_port
        peer = IPv4Address(self.client_address[0])
        destination = IPv4Address(self.connection.getsockname()[0])
        loopback = peer.is_loopback and destination.is_loopback
        private_access = getattr(self.server, "remote_access", None)
        parts = request_host_parts(self.headers)
        if parts is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Host header")
            return False
        host, host_port = parts
        forwarded = any(header in self.headers for header in PROXY_HEADERS)
        http_origin = request_origin_for(host, host_port, "http")
        https_origin = request_origin_for(host, host_port, "https")
        numeric_destination = (int(host_port or "80") == port
                               and (host == str(destination) or loopback and host == "localhost"))
        origins = self.headers.get_all("Origin", ())
        origin = origins[0] if len(origins) == 1 else None
        direct = numeric_destination and not forwarded and origin != https_origin
        if len(origins) > 1 or origins and origin not in (http_origin, https_origin):
            self.send_error(HTTPStatus.FORBIDDEN)
            return False
        networks = getattr(self.server, "local_networks", None)
        local = direct and (loopback or peer == destination
                            or networks is not None and networks.is_local(peer, destination))
        allowlist_match = private_access is None or private_access.allows(peer, destination)
        self.origin_checked = origin is not None
        self.secure_request = origin == https_origin
        self.direct_request = direct and (local or allowlist_match)
        self.trusted_local = local
        self.request_peer = peer
        self.request_destination = destination
        self.pin_admin = self.direct_request and (loopback or peer == destination)
        protocols = self.headers.get_all("X-Forwarded-Proto", [])
        self.remote_origin = (https_origin if protocols == ["https"] else
                              http_origin if protocols in ([], ["http"]) else None)
        self.request_origin = origin
        pin = getattr(self.server, "access_pin", None)
        snapshot = pin.snapshot() if pin is not None else None
        self.access_revision = snapshot["revision"] if snapshot is not None else None
        if snapshot is not None and not self.remote_allowed(snapshot["remote_url"]):
            self.send_error(HTTPStatus.FORBIDDEN)
            return False
        return True

    def remote_allowed(self, remote_url):
        return (self.trusted_local or remote_url is None or bool(remote_url)
                and remote_url == self.remote_origin and self.request_origin in (None, remote_url))

    def response(self, content_type, body, status=HTTPStatus.OK, *, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_response(self, result, status=HTTPStatus.OK, *, headers=()):
        self.response("application/json", json.dumps(result).encode("utf-8"), status, headers=headers)

    @staticmethod
    def pin_cookie_name(token):
        return f"scanner_pin_{token}"

    def pin_capability(self, token):
        if token is not None:
            cookies = SimpleCookie()
            try:
                cookies.load("; ".join(self.headers.get_all("Cookie", ())))
            except CookieError:
                return None
            cookie = cookies.get(self.pin_cookie_name(token))
            return cookie.value if cookie else None

    def needs_pin(self, token):
        if self.trusted_local:
            return False
        pin = getattr(self.server, "access_pin", None)
        if pin is None:
            return True
        with pin.guard():
            return not (self.remote_allowed(pin.remote_url)
                        and pin.authorized(self.pin_capability(token), token))

    def access_state(self, token, *, unlocked=False):
        pin = getattr(self.server, "access_pin", None)
        return {"pin_required": not unlocked and self.needs_pin(token),
                "pin_configured": bool(pin and pin.configured),
                "can_program_pin": self.pin_admin and pin is not None}

    def check_pin(self, token):
        access = self.access_state(token)
        if access["pin_required"]:
            self.json_response(access, HTTPStatus.UNAUTHORIZED)
            return False
        return True

    def chirp_request(self, *, upload=False):
        self.close_connection = True
        if (self.path != CHIRP_ROUTE or not self.direct_request or not self.request_peer.is_loopback
                or not self.request_destination.is_loopback or self.origin_checked
                or self.headers.get_all("X-SDR-Scanner-Clone", []) != ["1"]
                or any(name.lower().startswith("sec-fetch-") for name in self.headers.keys())):
            self.json_response({"error": "CHIRP programming requires a direct local scanner connection."},
                               HTTPStatus.FORBIDDEN)
            return
        runtime, _, _ = self.radio(None, touch=False)
        pin = getattr(self.server, "access_pin", None)
        try:
            if upload:
                self.connection.settimeout(5)
                lengths = self.headers.get_all("Content-Length", [])
                if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0])
                        or not 0 < int(lengths[0]) <= CHIRP_LIMIT or "Transfer-Encoding" in self.headers
                        or self.headers.get_content_type() != "application/json"):
                    raise ValueError("Invalid CHIRP request length or content type.")
                action = json.loads(self.rfile.read(int(lengths[0])))
                if (not isinstance(action, dict) or type(action.get("version")) is not int
                        or action["version"] != 1 or not {"version", "revision"} <= action.keys()
                        or action.keys() - {"version", "revision", "changes", "zones", "settings", "access"}):
                    raise ValueError("Invalid CHIRP image version or fields.")
                programming = (action["revision"], action.get("changes", []),
                               action.get("zones", {}), action.get("settings", {}))
                if "access" in action:
                    if pin is None:
                        raise ValueError("Scanner remote access settings are unavailable.")
                    access = action["access"]
                    if (not isinstance(access, dict) or "revision" not in access
                            or access.keys() - {"revision", "new_pin", "remote_url"}):
                        raise ValueError("Invalid scanner remote access settings.")
                    revision = access.pop("revision")
                    with pin.program(access, revision) as commit:
                        image = runtime.chirp_upload(*programming, commit_access=commit)
                        image["access"] = pin.snapshot()
                else:
                    image = runtime.chirp_upload(*programming)
            else:
                image = runtime.chirp_download()
            if pin is not None and "access" not in image:
                image["access"] = pin.snapshot()
        except (ChirpConflict, AccessConflict) as error:
            self.json_response({"error": str(error)}, HTTPStatus.CONFLICT)
        except (ValueError, RecursionError) as error:
            self.json_response({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except TimeoutError:
            self.json_response({"error": "CHIRP transfer timed out."}, HTTPStatus.REQUEST_TIMEOUT)
        except OSError as error:
            LOG.warning("Could not complete CHIRP transfer: %s", error)
            self.json_response({"error": "Could not save scanner programming."}, HTTPStatus.SERVICE_UNAVAILABLE)
        else:
            self.json_response(image)

    def program_or_unlock_pin(self, token):
        self.close_connection = True
        if token is None or not self.origin_checked:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        lengths = self.headers.get_all("Content-Length", ())
        try:
            if (len(lengths) != 1 or not 0 < int(lengths[0]) <= 64 or "Transfer-Encoding" in self.headers
                    or self.headers.get_content_type() != "application/json"):
                raise ValueError
            action = json.loads(self.rfile.read(int(lengths[0])))
            if not isinstance(action, dict) or action.keys() not in ({"pin"}, {"new_pin"}):
                raise ValueError
            digits = next(iter(action.values()))
            if not isinstance(digits, str) or not PIN_PATTERN.fullmatch(digits):
                raise ValueError
        except (ValueError, RecursionError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Enter a six-digit PIN")
            return
        pin = getattr(self.server, "access_pin", None)
        if pin is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if "new_pin" in action:
            if not self.pin_admin:
                self.json_response(self.access_state(token), HTTPStatus.FORBIDDEN)
                return
            runtime, radio_id, _ = self.radio(token, touch=False)
            if radio_id is not None and runtime.state()["power"]:
                self.json_response(self.access_state(token), HTTPStatus.CONFLICT)
                return
            try:
                pin.set_pin(digits)
            except OSError as error:
                LOG.warning("Could not save scanner access PIN: %s", error)
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                return
            self.json_response(self.access_state(token))
            return
        capability, retry_after = pin.unlock(digits, self.request_peer, token, revision=self.access_revision)
        if capability is None:
            if retry_after:
                self.json_response(self.access_state(token), HTTPStatus.TOO_MANY_REQUESTS,
                                   headers=(("Retry-After", str(retry_after)),))
            else:
                self.json_response(self.access_state(token), HTTPStatus.FORBIDDEN)
            return
        cookie = SimpleCookie({self.pin_cookie_name(token): capability})
        morsel = cookie[self.pin_cookie_name(token)]
        morsel["path"] = "/"
        morsel["max-age"] = ACCESS_SECONDS
        morsel["httponly"] = True
        morsel["samesite"] = "Strict"
        morsel["secure"] = self.secure_request
        self.json_response(self.access_state(token, unlocked=True), headers=(("Set-Cookie", morsel.OutputString()),))

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
        if not self.accept_request():
            return
        route = self.session_route()
        if route is None:
            return
        path, token, expected_radio = route
        if path == CHIRP_ROUTE:
            self.chirp_request()
            return
        if path == "/api/access":
            self.json_response(self.access_state(token))
            return
        if (path.startswith(("/api/", RECORDINGS_URL)) or path == "/audio") and not self.check_pin(token):
            return
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
        if path == DISCOVERY_URL or path.startswith(f"{DISCOVERY_URL}/"):
            self.serve_discovery(path, token, expected_radio)
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
        if not self.accept_request():
            return
        route = self.session_route()
        if route is None:
            return
        path, token, expected_radio = route
        if (path.startswith(("/api/", RECORDINGS_URL)) or path == "/audio") and not self.check_pin(token):
            return
        if path == DISCOVERY_URL or path.startswith(f"{DISCOVERY_URL}/"):
            self.serve_discovery(path, token, expected_radio)
        elif path.startswith(RECORDINGS_URL):
            self.replay_recording(path, token, expected_radio)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def serve_discovery(self, path, token, expected_radio):
        runtime, radio_id, _ = self.radio(token)
        if radio_id is None:
            self.send_error(HTTPStatus.CONFLICT, "Scanner panel is in standby")
            return
        if expected_radio is not None and expected_radio != radio_id:
            self.send_error(HTTPStatus.CONFLICT, "Scanner receiver changed")
            return
        route = path.removeprefix(DISCOVERY_URL)
        if not route:
            with runtime.lock:
                status = runtime.discovery.status()
            self.json_response(status)
            return
        hit_id = None
        if route != "/replay":
            match = re.fullmatch(r"/hit/([1-9][0-9]{0,17})", route)
            if match is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            hit_id = int(match[1])
        with runtime.lock:
            chunks = runtime.discovery.audio(hit_id)
        if not chunks:
            self.send_error(HTTPStatus.NOT_FOUND, "Audio unavailable")
            return
        self.response("audio/wav", wave_audio(chunks, SAMPLE_RATE))

    def record_discovery_hit(self, hit_id, token, expected_radio):
        runtime, radio_id, _ = self.radio(token)
        with runtime.lock:
            if radio_id is None or not runtime.scanner.power:
                failure = ("Power on the scanner first.", HTTPStatus.CONFLICT)
            elif expected_radio is not None and expected_radio != radio_id:
                failure = ("Scanner receiver changed.", HTTPStatus.CONFLICT)
            else:
                hit = runtime.discovery.hit(hit_id)
                chunks = runtime.discovery.audio(hit_id) if hit is not None else ()
                if not chunks:
                    failure = ("This hit has rolled out of the replay buffer.", HTTPStatus.NOT_FOUND)
                elif sum(map(len, chunks)) < MIN_CLIP_PCM_BYTES:
                    failure = ("Saving a clip requires at least two seconds of buffered audio.",
                               HTTPStatus.UNPROCESSABLE_ENTITY)
                else:
                    reply = runtime.recording.queue_hit_save(hit, chunks)
                    failure = None if reply is not None else (
                        "The recorder is busy. Try again in a moment.", HTTPStatus.SERVICE_UNAVAILABLE)
        if failure is not None:
            message, status = failure
            self.json_response({"error": message}, status)
            return
        body, status = reply.get()
        self.json_response(body, status)

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
                check_at = time.monotonic() + 0.35
                while remaining:
                    if time.monotonic() >= check_at:
                        if self.needs_pin(token):
                            self.close_connection = True
                            break
                        check_at = time.monotonic() + 0.35
                    block = stream.read(min(remaining, 65_536))
                    if not block:
                        self.close_connection = True
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
            except OSError:
                self.close_connection = True

    def do_POST(self):
        if not self.accept_request():
            self.close_connection = True
            return
        route = self.session_route()
        if route is None:
            self.close_connection = True
            return
        path, token, expected_radio = route
        if path == CHIRP_ROUTE:
            self.chirp_request(upload=True)
            return
        if path == "/api/access":
            self.program_or_unlock_pin(token)
            return
        if not self.direct_request and not self.origin_checked:
            self.close_connection = True
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not self.check_pin(token):
            self.close_connection = True
            return
        pool = getattr(self.server, "radios", None)
        save_hit = re.fullmatch(DISCOVERY_URL + r"/hit/([1-9][0-9]{0,17})/record", path)
        if save_hit is not None:
            lengths = self.headers.get_all("Content-Length", ())
            if len(lengths) > 1 or any(value != "0" for value in lengths) or "Transfer-Encoding" in self.headers:
                self.close_connection = True
                self.json_response({"error": "Discovery recording requires an empty request."},
                                   HTTPStatus.BAD_REQUEST)
                return
            self.record_discovery_hit(int(save_hit[1]), token, expected_radio)
            return
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
                if time.monotonic() >= check_at:
                    assigned, radio_id, _ = self.radio(token, touch=False)
                    if assigned is not runtime or radio_id is None or self.needs_pin(token):
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
    parser.add_argument("--listen", action="store_true", help="Listen on all IPv4 interfaces for LAN or forwarding")
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
    try:
        access_pin = AccessPin(args.state.with_suffix(".access.json"))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    bind = "0.0.0.0" if args.listen or remote_access else "127.0.0.1"
    with ThreadingHTTPServer((bind, args.port), PanelHandler) as server:
        radios = RadioPool(args.state, ScannerRuntime)
        server.radios = radios
        server.remote_access = remote_access
        server.local_networks = LocalNetworks()
        server.access_pin = access_pin
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(
            target=server.shutdown, daemon=True).start())
        radios.start()
        LOG.info("Panel available at http://127.0.0.1:%d/", server.server_port)
        if bind != "127.0.0.1":
            LOG.info("The scanner is listening on all IPv4 interfaces. Non-local connections require the PIN.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            radios.close()


if __name__ == "__main__":
    main()

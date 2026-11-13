"""Receive Band III DAB and DAB+ through the scanner's assigned RTL-SDR."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
from threading import Thread
import time


LOOPBACK = "127.0.0.1"
# Check the national and nearby Norfolk multiplexes before sweeping Band III.
BAND_III = (
    ("12B", 225_648_000), ("10B", 211_648_000), ("11D", 222_064_000),
    ("11A", 216_928_000), ("9C", 206_352_000),
    ("5A", 174_928_000), ("5B", 176_640_000), ("5C", 178_352_000), ("5D", 180_064_000),
    ("6A", 181_936_000), ("6B", 183_648_000), ("6C", 185_360_000), ("6D", 187_072_000),
    ("7A", 188_928_000), ("7B", 190_640_000), ("7C", 192_352_000), ("7D", 194_064_000),
    ("8A", 195_936_000), ("8B", 197_648_000), ("8C", 199_360_000), ("8D", 201_072_000),
    ("9A", 202_928_000), ("9B", 204_640_000), ("9D", 208_064_000),
    ("10A", 209_936_000), ("10C", 213_360_000), ("10D", 215_072_000),
    ("11B", 218_640_000), ("11C", 220_352_000), ("12A", 223_936_000),
    ("12C", 227_360_000), ("12D", 229_072_000),
    ("13A", 230_784_000), ("13B", 232_496_000), ("13C", 234_208_000),
    ("13D", 235_776_000), ("13E", 237_488_000), ("13F", 239_200_000),
)
BLOCK_INDEX = {block: index for index, (block, _) in enumerate(BAND_III)}
SERVICE_ID = re.compile(r"0x[0-9a-f]{4,8}")
STREAM_PATH = re.compile(rf"/mp3/(?P<sid>{SERVICE_ID.pattern})")
STARTUP_TIMEOUT = 8.0
SCAN_DWELL = 4.0
ENSEMBLE_DWELL = 8.0
RECALL_DWELL = 8.0
POLL_INTERVAL = 0.35
RESCAN_SERVICE_SETTLE = 2.0
AUDIO_START_TIMEOUT = 12.0
AUDIO_RETRY_INTERVAL = 0.75
AUDIO_OPEN_HOLD = 0.45
MAX_MUX_BYTES = 1024 * 1024


class DabError(RuntimeError):
    """The DAB decoder could not start or continue receiving."""


@dataclass(frozen=True)
class Station:
    sid: str
    path: str
    name: str
    text: str


@dataclass(frozen=True)
class Multiplex:
    stations: tuple[Station, ...]
    detected: bool


def _local_ports():
    with socket.socket() as iq, socket.socket() as web:
        for sock in (iq, web):
            sock.bind((LOOPBACK, 0))
        return iq.getsockname()[1], web.getsockname()[1]


def _rtl_tcp_listening(port):
    # The decoder does not retry its first refused connection. Inspect Linux's
    # IPv4 listen table without connecting a probe to the single-client IQ feed.
    address = f"0100007F:{port:04X}"
    with open("/proc/net/tcp", encoding="ascii") as connections:
        next(connections, None)
        for connection in connections:
            _, local, _, state, _ = connection.split(None, 4)
            if local == address and state == "0A":
                return True
    return False


def _request(port, block):
    # Upstream joins its programme-handler thread during a retune.
    connection = HTTPConnection(LOOPBACK, port, timeout=5.0 if block else 0.4)
    try:
        if block:
            connection.request("POST", "/channel", block.encode("ascii"),
                               {"Content-Type": "text/plain"})
        else:
            connection.request("GET", "/mux.json")
        with connection.getresponse() as response:
            if response.status != 200:
                raise DabError(f"DAB decoder returned HTTP {response.status}.")
            if block:
                return None
            payload = response.read(MAX_MUX_BYTES + 1)
            if len(payload) > MAX_MUX_BYTES:
                raise DabError("DAB station list is too large.")
            return _multiplex(json.loads(payload))
    finally:
        connection.close()


def _clean_label(value, limit):
    if not isinstance(value, str):
        return ""
    return " ".join(value[:limit].replace("\x00", " ").split())


def _multiplex(mux):
    services = mux.get("services") if isinstance(mux, dict) else None
    ensemble = mux.get("ensemble") if isinstance(mux, dict) else None
    ensemble_id = ensemble.get("id") if isinstance(ensemble, dict) else None
    detected = (isinstance(ensemble_id, str) and ensemble_id != "0x0000"
                and bool(re.fullmatch(r"0x[0-9a-f]{4}", ensemble_id)))
    stations = {}
    for service in services if isinstance(services, list) else ():
        if not isinstance(service, dict):
            continue
        path = service.get("url_mp3")
        stream = STREAM_PATH.fullmatch(path) if isinstance(path, str) else None
        if stream is None:
            continue
        service_id = int(stream["sid"], 16)
        if not service_id:
            continue
        sid = f"0x{service_id:04x}"
        labels = service.get("label")
        labels = labels if isinstance(labels, dict) else {}
        name = next((label for key in ("label", "fig2label", "shortlabel")
                     if (label := _clean_label(labels.get(key), 64))), "")
        dls = service.get("dls")
        text = _clean_label(dls.get("label"), 256) if isinstance(dls, dict) else ""
        stations[sid] = Station(sid, f"/mp3/{sid}", name, text)
    return Multiplex(tuple(sorted(stations.values(), key=lambda station: station.sid)), detected)


def _stop(process):
    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    Thread(target=process.wait, name="dab-reap", daemon=True).start()
    finally:
        if process.stdout is not None:
            process.stdout.close()


class DabReceiver:
    def __init__(self, index, *, known_blocks=(), service_counts=None):
        self._index = index
        self._iq = self._decoder = self._audio = None
        self._http = self._pending = None
        self._iq_port = self._http_port = None
        self._gst = self._welle = None
        self._closed = False
        self._block_index = 0
        self._generation = 0
        self._retune = False
        self._decoder_block = None
        self._remaining = len(BAND_III)
        self._direction = 1
        self._recall_sid = None
        self._rescan_previous = None
        self._rescan_previous_found = False
        self._rescan_first = None
        self._rescan_counts = None
        self._received_service_ids = set()
        self._rescan_services_changed = None
        self._scan_until = None
        self._scan_started = None
        self._startup_until = None
        self._poll_at = 0.0
        self._station = None
        self._stations = ()
        self._known_blocks = {BLOCK_INDEX[block] for block in known_blocks
                              if isinstance(block, str) and block in BLOCK_INDEX}
        self._service_counts = {}
        if isinstance(service_counts, dict):
            self._service_counts = {block: count for block, count in service_counts.items()
                                    if block in BLOCK_INDEX and type(count) is int and count > 0}
            self._known_blocks.update(BLOCK_INDEX[block] for block in self._service_counts)
        self._scan_order = tuple(range(len(BAND_III)))
        self._audio_retry_at = 0.0
        self._audio_until = 0.0
        self._audio_open_until = 0.0
        self._partial_sample = b""
        self._error = None

    @property
    def _channel(self):
        return BAND_III[self._block_index][0]

    def _start_iq(self, now):
        welle = Path(os.environ.get("SDR_SCANNER_WELLE_CLI") or
                     Path.home() / ".local/share/sdr-scanner/bin/welle-cli").expanduser()
        rtl_tcp = shutil.which("rtl_tcp")
        self._gst = shutil.which("gst-launch-1.0")
        if not welle.is_file() or not os.access(welle, os.X_OK):
            raise DabError("The scanner's welle-cli DAB decoder is missing.")
        if not rtl_tcp or not self._gst:
            raise DabError("DAB requires rtl_tcp and gst-launch-1.0.")
        self._welle = str(welle.resolve())
        self._iq_port, self._http_port = _local_ports()
        self._iq = subprocess.Popen(
            [rtl_tcp, "-a", LOOPBACK, "-p", str(self._iq_port), "-d", str(self._index)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._startup_until = now + STARTUP_TIMEOUT

    def _start_decoder(self, now):
        self._decoder = subprocess.Popen(
            [self._welle, "-c", self._channel, "-T", "-F", f"rtl_tcp,{LOOPBACK}:{self._iq_port}",
             "-w", str(self._http_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._http = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dab-decoder")
        self._startup_until = now + STARTUP_TIMEOUT
        self._decoder_block = self._channel
        self._retune = False

    def _clear_audio(self):
        _stop(self._audio)
        self._audio = None
        self._audio_open_until = 0.0
        self._partial_sample = b""

    def _tune_block(self, index):
        self._clear_audio()
        self._station = None
        self._stations = ()
        self._scan_until = None
        self._scan_started = None
        self._received_service_ids.clear()
        self._rescan_services_changed = None
        self._poll_at = 0.0
        self._error = None
        self._generation += 1
        self._block_index = index
        self._retune = self._channel != self._decoder_block or bool(self._pending and self._pending[2])

    def _scan_blocks(self, direction, prefer_known):
        order = tuple((self._block_index + direction * offset) % len(BAND_III)
                      for offset in range(1, len(BAND_III) + 1))
        if not prefer_known:
            return order
        # Walk known multiplexes before probing frequencies we have never received.
        # Keep the current block as the final fallback.
        others = order[:-1]
        known = tuple(index for index in others if index in self._known_blocks)
        unknown = tuple(index for index in others if index not in self._known_blocks)
        return known + unknown + (self._block_index,)

    def _cancel_rescan(self):
        self._rescan_counts = self._rescan_previous = self._rescan_first = None
        self._rescan_previous_found = False

    def _start_rescan(self, previous):
        if previous is None and self._station:
            previous = self._block_index, self._station.sid
        elif previous is None and self._rescan_counts is not None:
            previous = self._rescan_previous
        self._rescan_previous = previous
        self._rescan_previous_found = False
        self._rescan_first = None
        self._rescan_counts = {}
        self._recall_sid = None
        self._direction = 1
        self._scan_order = tuple(range(len(BAND_III)))
        self._remaining = len(self._scan_order)
        self._tune_block(self._scan_order[0])

    def _finish_rescan(self, now):
        previous = self._rescan_previous if self._rescan_previous_found else self._rescan_first
        self._service_counts = self._rescan_counts
        self._known_blocks = {BLOCK_INDEX[block] for block in self._service_counts}
        self._cancel_rescan()
        if previous is None:
            self._error = "No DAB stations found."
            return
        self._recall(*previous, now)

    def _advance_block(self, now, *, direction=None, prefer_known=False):
        self._recall_sid = None
        if direction is not None:
            self._cancel_rescan()
            self._direction = direction
            self._scan_order = self._scan_blocks(direction, prefer_known)
            self._remaining = len(BAND_III)
        else:
            self._remaining -= 1
        if not self._remaining:
            if self._rescan_counts is not None:
                self._finish_rescan(now)
            else:
                self._error = "No DAB stations found."
            return
        self._tune_block(self._scan_order[-self._remaining])

    def _recall(self, index, sid, now):
        self._cancel_rescan()
        if self._block_index == index:
            if self._recall_sid == sid or (self._station and self._station.sid == sid and not self._error):
                return
            if self._decoder_block == self._channel and not self._retune and not (self._pending and self._pending[2]):
                station = next((item for item in self._stations if item.sid == sid), None)
                if station:
                    self._select(station, now)
                    return
        self._remaining = 0
        self._recall_sid = sid
        self._tune_block(index)

    def _select(self, station, now):
        self._clear_audio()
        self._station = station
        self._remaining = 0
        self._recall_sid = None
        self._scan_until = None
        self._error = None
        self._audio_retry_at = now
        self._audio_until = now + AUDIO_START_TIMEOUT

    def _start_scan_dwell(self, now):
        self._scan_started = now
        dwell = RECALL_DWELL if self._recall_sid else SCAN_DWELL
        if (not self._recall_sid and self._rescan_counts is None
                and self._block_index in self._known_blocks):
            dwell = ENSEMBLE_DWELL
        self._scan_until = now + dwell

    def _update_stations(self, multiplex, now):
        stations = multiplex.stations
        if self._rescan_counts is None and (multiplex.detected or stations):
            self._known_blocks.add(self._block_index)
        if self._remaining and multiplex.detected:
            self._scan_until = self._scan_started + ENSEMBLE_DWELL
        if not stations:
            return
        self._stations = stations
        previous_count = len(self._received_service_ids)
        self._received_service_ids.update(station.sid for station in stations)
        count = len(self._received_service_ids)
        if self._rescan_counts is not None:
            self._scan_until = self._scan_started + ENSEMBLE_DWELL
            if count > previous_count:
                self._rescan_services_changed = now
                self._rescan_counts[self._channel] = count
            if self._rescan_first is None:
                self._rescan_first = self._block_index, stations[0].sid
            waiting_for_previous = False
            if self._rescan_previous and self._rescan_previous[0] == self._block_index:
                self._rescan_previous_found = self._rescan_previous[1] in self._received_service_ids
                waiting_for_previous = not self._rescan_previous_found
            if not waiting_for_previous and now - self._rescan_services_changed >= RESCAN_SERVICE_SETTLE:
                self._advance_block(now)
            return

        self._service_counts[self._channel] = max(self._service_counts.get(self._channel, 0), count)
        if self._recall_sid:
            station = next((item for item in stations if item.sid == self._recall_sid), None)
            if station:
                self._select(station, now)
        elif self._remaining:
            self._select(stations[0 if self._direction > 0 else -1], now)
        elif self._station:
            for station in stations:
                if station.path == self._station.path:
                    self._station = station
                    break

    def _poll_decoder(self, now):
        if self._pending:
            future, generation, block = self._pending
            if not future.done():
                return
            self._pending = None
            if generation == self._generation:
                try:
                    multiplex = future.result()
                except (DabError, OSError, HTTPException, ValueError) as exc:
                    if block:
                        raise DabError(f"Could not tune DAB block {block}.") from exc
                else:
                    if block:
                        self._retune = False
                        self._decoder_block = block
                        self._start_scan_dwell(now)
                    else:
                        self._startup_until = None
                        if self._scan_until is None and (self._remaining or self._recall_sid):
                            self._start_scan_dwell(now)
                        self._update_stations(multiplex, now)

        if self._startup_until and now >= self._startup_until:
            raise DabError("The DAB decoder did not respond.")
        if self._scan_until and now >= self._scan_until:
            if self._recall_sid:
                self._recall_sid = None
                self._scan_until = None
                self._error = f"Saved DAB station is unavailable on block {self._channel}."
            elif self._remaining:
                self._advance_block(now)
        if now >= self._poll_at and (self._remaining or self._recall_sid or self._station):
            block = self._channel if self._retune else None
            self._pending = (self._http.submit(_request, self._http_port, block), self._generation, block)
            self._poll_at = now + POLL_INTERVAL

    def _step_station(self, direction, now):
        if self._station is None:
            if not self._remaining and not self._recall_sid:
                self._advance_block(now, direction=direction, prefer_known=True)
            return
        if self._error:
            self._select(self._station, now)
            return
        next_station = next((index + direction for index, station in enumerate(self._stations)
                             if station.sid == self._station.sid),
                            0 if direction > 0 else len(self._stations) - 1)
        if 0 <= next_station < len(self._stations):
            self._select(self._stations[next_station], now)
        elif direction < 0 and self._known_blocks == {self._block_index}:
            station = self._stations[-1]
            if station.sid != self._station.sid:
                self._select(station, now)
        else:
            self._advance_block(now, direction=direction, prefer_known=True)

    def _read_audio(self, now):
        if self._station is None:
            return b""
        if now >= self._audio_until:
            self._clear_audio()
            self._error = "DAB station audio is unavailable."
            self._audio_retry_at = now + AUDIO_RETRY_INTERVAL
            self._audio_until = self._audio_retry_at + AUDIO_START_TIMEOUT
            return b""
        if self._audio is None:
            if now < self._audio_retry_at:
                return b""
            location = f"http://{LOOPBACK}:{self._http_port}{self._station.path}"
            self._audio = subprocess.Popen(
                [self._gst, "-q", "souphttpsrc", f"location={location}", "timeout=5",
                 "automatic-redirect=false", "!",
                 "mpegaudioparse", "!", "mpg123audiodec", "!", "audioconvert", "!", "audioresample",
                 "!", "audio/x-raw,format=S16LE,channels=1,rate=24000", "!", "fdsink", "fd=1", "sync=true"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            os.set_blocking(self._audio.stdout.fileno(), False)
        try:
            pcm = os.read(self._audio.stdout.fileno(), 1920)
        except BlockingIOError:
            pcm = b""
        if not pcm:
            if self._audio.poll() is not None:
                self._clear_audio()
                self._audio_retry_at = now + AUDIO_RETRY_INTERVAL
            return b""
        self._error = None
        self._audio_open_until = now + AUDIO_OPEN_HOLD
        self._audio_until = now + AUDIO_START_TIMEOUT
        pcm = self._partial_sample + pcm
        complete = len(pcm) & ~1
        self._partial_sample = pcm[complete:]
        return pcm[:complete]

    def step(self, command=None):
        if self._closed:
            raise DabError("The DAB receiver is closed.")
        station_command = None
        if isinstance(command, tuple):
            if len(command) != 3 or command[0] not in ("recall", "rescan"):
                raise DabError("Invalid DAB station command.")
            command, block, sid = command
            if (not isinstance(block, str) or block not in BLOCK_INDEX
                    or not isinstance(sid, str) or not SERVICE_ID.fullmatch(sid)):
                raise DabError("Invalid DAB block or service ID.")
            station_command = BLOCK_INDEX[block], sid
        now = time.monotonic()
        try:
            if self._iq is None:
                self._start_iq(now)
            if self._iq.poll() is not None:
                raise DabError("Could not open the assigned RTL-SDR for DAB.")
            if self._decoder is not None and self._decoder.poll() is not None:
                raise DabError("The welle-cli DAB decoder stopped.")
            if command == "search":
                self._advance_block(now, direction=1)
            elif command == "rescan":
                self._start_rescan(station_command)
            elif command == "recall" and station_command:
                self._recall(*station_command, now)
            elif command in ("up", "down"):
                self._step_station(1 if command == "up" else -1, now)
            if self._decoder is None:
                if _rtl_tcp_listening(self._iq_port):
                    self._start_decoder(now)
                elif now >= self._startup_until:
                    raise DabError("The DAB IQ receiver did not start.")
            if self._decoder is not None:
                self._poll_decoder(now)
            pcm = self._read_audio(now)
        except (DabError, OSError) as exc:
            self.close()
            if isinstance(exc, DabError):
                raise
            raise DabError(f"Could not run the DAB receiver: {exc}") from exc

        block, hz = BAND_III[self._block_index]
        status = {"hz": hz, "block": block, "sid": self._station.sid if self._station else None,
                  "service": self._station.name if self._station else "",
                  "text": self._station.text if self._station else "",
                  "scanning": bool(self._remaining or self._recall_sid),
                  "recalling": self._recall_sid is not None,
                  "rescanning": self._rescan_counts is not None,
                  "known_blocks": tuple(BAND_III[index][0] for index in sorted(self._known_blocks)),
                  "service_counts": self._service_counts.copy(),
                  "audio_open": now < self._audio_open_until, "error": self._error}
        return status, pcm

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._clear_audio()
        _stop(self._decoder)
        _stop(self._iq)
        if self._http is not None:
            self._http.shutdown(wait=False, cancel_futures=True)

"""Serve the local scanner panel and bridge it to the USB receiver."""

import argparse
import json
import logging
import math
import mimetypes
import queue
import re
import signal
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from receiver import Receiver, ReceiverError
from scanner import Scanner


ROOT = Path(__file__).resolve().parent
ASSETS = frozenset({"index.html", "panel.css", "panel.js", "panel.png", "pcm-worklet.js"})
KEYS = frozenset({
    "hold", "up", "down", "private", "emg", "frn", "close", "air",
    "cb", "search", "lock", "band", "prog",
})
SILENCE = bytes(960)
FREQUENCY_ENTRY = re.compile(r"[0-9]{1,4}(?:\.[0-9]{0,6})?")
LOG = logging.getLogger("scanner")


def target(state):
    hz = state["tune_hz"]
    return (hz, state["modulation"]) if hz is not None else None


class ScannerRuntime:
    def __init__(self, state_path):
        self.scanner = Scanner(state_path)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.clients = set()
        self.receiver_status = "standby"
        self.worker = threading.Thread(target=self.run, name="SDR receiver", daemon=True)

    def state(self):
        with self.lock:
            state = self.scanner.snapshot()
            state["receiver"] = self.receiver_status
            if self.receiver_status == "unavailable" and state["power"]:
                state["audio_open"] = False
                if state.get("audio_expected", state["tune_hz"] is not None):
                    state["display"] = "Err"
            return state

    def act(self, action):
        with self.lock:
            before = self.scanner.snapshot()
            if "key" in action:
                self.scanner.key(action["key"], action.get("gesture", "tap"))
            elif action.get("tune") == "step":
                self.scanner.tune_step(action["direction"])
            elif action.get("tune") == "frequency":
                whole, _, fraction = action["mhz"].partition(".")
                hz = int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))
                self.scanner.tune_frequency(hz)
            else:
                self.scanner.knob(action["knob"], action["value"], action.get("held", ()))
            after = self.state()
            if target(before) != target(after) or (before["audio_open"] and not after["audio_open"]):
                self.discard_audio()
            self.wake.set()
            return after

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

    def publish(self, pcm):
        for client in self.clients:
            try:
                client.put_nowait(pcm)
            except queue.Full:
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
        retry_at = 0.0
        try:
            while not self.stopped.is_set():
                with self.lock:
                    self.scanner.tick()
                    state = self.scanner.snapshot()
                    probe = self.scanner.take_close_probe() if connected else None

                if not state["power"]:
                    if connected:
                        receiver.close()
                    connected = False
                    tuned = None
                    retry_at = 0.0
                    with self.lock:
                        self.receiver_status = "standby"
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
                        LOG.info("USB SDR opened")

                    if probe:
                        hit = receiver.probe(probe, windows=3)
                        if hit is not None:
                            with self.lock:
                                self.scanner.close_hit(hit)
                        continue

                    wanted = target(state)
                    if wanted is None:
                        self.wait(0.02)
                        continue
                    if wanted != tuned:
                        receiver.tune(*wanted)
                        tuned = wanted

                    level, pcm = receiver.read()
                    with self.lock:
                        current = self.scanner.snapshot()
                        if not current["power"] or target(current) != wanted:
                            continue
                        self.scanner.tick(level)
                        was_open = current["audio_open"]
                        current = self.scanner.snapshot()
                        if target(current) == wanted and current["audio_open"] and pcm:
                            self.publish(pcm)
                        elif was_open:
                            self.discard_audio()
                except ReceiverError as exc:
                    LOG.warning("USB SDR unavailable: %s", exc)
                    receiver.close()
                    connected = False
                    tuned = None
                    retry_at = time.monotonic() + 2
                    with self.lock:
                        self.receiver_status = "unavailable"
        finally:
            receiver.close()

    def close(self):
        self.stopped.set()
        self.wake.set()
        self.worker.join(timeout=3)


def valid_action(action):
    if not isinstance(action, dict):
        return False
    if "key" in action:
        key = action["key"]
        return (isinstance(key, str) and key in KEYS
                and action.get("gesture", "tap") in ("tap", "long", "double"))
    if "tune" in action:
        if action["tune"] == "step":
            return type(action.get("direction")) is int and action["direction"] in (-1, 1)
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
        and all(isinstance(key, str) and key in KEYS for key in held)
    )


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string, *args):
        LOG.debug(format_string, *args)

    def local_request(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        if host in hosts and origin in (None, f"http://{host}"):
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
        name = "index.html" if self.path == "/" else self.path.removeprefix("/")
        if name not in ASSETS or not (ROOT / "web" / name).is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.response(mimetypes.guess_type(name)[0] or "application/octet-stream",
                      (ROOT / "web" / name).read_bytes())

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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with ThreadingHTTPServer(("127.0.0.1", args.port), PanelHandler) as server:
        runtime = ScannerRuntime(args.state)
        server.runtime = runtime
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(
            target=server.shutdown, daemon=True).start())
        runtime.worker.start()
        LOG.info("Panel available at http://127.0.0.1:%d/", server.server_port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            runtime.close()


if __name__ == "__main__":
    main()

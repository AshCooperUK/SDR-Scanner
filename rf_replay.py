"""Keep recent contiguous IQ samples for receive-only offline replay."""

from collections import OrderedDict, deque
from dataclasses import dataclass
from secrets import token_hex
from threading import BoundedSemaphore, Lock
from time import monotonic

from discovery import wave_audio
from receiver import (DIRECT_LIMIT, IQ_BLOCK_BYTES, IQ_RATE, IQ_SAMPLES, PROBE_HALF_WIDTH, SAMPLE_RATE,
                      TUNER_LIMIT, _ChannelDecoder, _decode_iq)


RF_REPLAY_URL = "/api/rf-replay"
MODES = ("FM", "WFM", "AM", "S-AM(D)", "S-AM(U)", "S-AM(L)", "USB", "LSB", "CW", "CW-R")
MAX_SECONDS = 10
MAX_BLOCKS = MAX_SECONDS * IQ_RATE // IQ_SAMPLES
MAX_SNAPSHOTS = 2
SNAPSHOT_SECONDS = 180
LIVE_TIMEOUT = 1.0
WAV_HEADER_BYTES = 44
WAV_BLOCK_BYTES = 2 * SAMPLE_RATE * IQ_SAMPLES // IQ_RATE
RENDER_SLOT = BoundedSemaphore(1)


def _metadata(center, direct, tuning, count):
    lower, upper = (100_000, DIRECT_LIMIT - 1) if direct else (DIRECT_LIMIT, TUNER_LIMIT)
    hz, mode = tuning
    return {"duration_ms": round(count * IQ_SAMPLES * 1000 / IQ_RATE),
            "center_hz": center, "min_hz": max(lower, center - PROBE_HALF_WIDTH),
            "max_hz": min(upper, center + PROBE_HALF_WIDTH), "tune_hz": hz,
            "mode": mode if mode in MODES else "FM", "modes": MODES}


@dataclass(frozen=True)
class Capture:
    id: str
    expires_at: float
    center: int
    direct: bool
    tuning: tuple
    blocks: tuple

    @property
    def metadata(self):
        return {**_metadata(self.center, self.direct, self.tuning, len(self.blocks)), "id": self.id}

    @property
    def wave_size(self):
        return WAV_HEADER_BYTES + len(self.blocks) * WAV_BLOCK_BYTES

    def accepts(self, hz, mode):
        metadata = self.metadata
        return mode in MODES and metadata["min_hz"] <= hz <= metadata["max_hz"]

    def render(self, hz, mode):
        decoder = _ChannelDecoder()
        decoder.reset(hz - self.center, mode)
        return wave_audio((decoder.decode(_decode_iq(block))[1] for block in self.blocks), SAMPLE_RATE)


class RFReplay:
    """Use one live ring and at most two immutable, short-lived snapshots per receiver."""

    def __init__(self):
        self._lock = Lock()
        self._blocks = deque(maxlen=MAX_BLOCKS)
        self._snapshots = OrderedDict()
        self._source = None
        self._tuning = None
        self._sequence = None
        self._updated = 0.0
        self._next_expiry = float("inf")
        self._cached_wave = None

    def feed(self, raw, generation, sequence, center, direct, gain, tuning):
        if len(raw) != IQ_BLOCK_BYTES:
            self.clear_live()
            return
        now = monotonic()
        source = generation, center, direct, gain
        with self._lock:
            if (source != self._source or sequence != self._sequence + 1
                    or now - self._updated > LIVE_TIMEOUT):
                self._blocks.clear()
            self._source = source
            self._tuning = tuning
            self._sequence = sequence
            self._updated = now
            self._blocks.append(raw)
            self._expire(now)

    def clear_live(self):
        with self._lock:
            self._source = None
            self._blocks.clear()

    def _expire(self, now):
        if now < self._next_expiry:
            return
        while self._snapshots and next(iter(self._snapshots.values())).expires_at <= now:
            self._snapshots.popitem(last=False)
        self._next_expiry = next(iter(self._snapshots.values())).expires_at if self._snapshots else float("inf")

    def status(self):
        now = monotonic()
        with self._lock:
            self._expire(now)
            if self._blocks and now - self._updated <= LIVE_TIMEOUT:
                _, center, direct, _ = self._source
                return _metadata(center, direct, self._tuning, len(self._blocks))
        return {"duration_ms": 0, "center_hz": None, "min_hz": None, "max_hz": None,
                "tune_hz": None, "mode": None, "modes": MODES}

    def capture(self, session):
        now = monotonic()
        with self._lock:
            self._expire(now)
            if not self._blocks or now - self._updated > LIVE_TIMEOUT:
                return None
            _, center, direct, _ = self._source
            capture = Capture(token_hex(8), now + SNAPSHOT_SECONDS, center, direct, self._tuning, tuple(self._blocks))
            self._snapshots[session] = capture
            self._snapshots.move_to_end(session)
            if len(self._snapshots) > MAX_SNAPSHOTS:
                self._snapshots.popitem(last=False)
            self._next_expiry = next(iter(self._snapshots.values())).expires_at
            return capture.metadata

    def get(self, session, capture_id):
        with self._lock:
            self._expire(monotonic())
            capture = self._snapshots.get(session)
            return capture if capture is not None and capture.id == capture_id else None

    def render(self, capture, hz, mode):
        key = capture.id, hz, mode
        with self._lock:
            if self._cached_wave is not None and self._cached_wave[0] == key:
                return self._cached_wave[1]
        if not RENDER_SLOT.acquire(blocking=False):
            return None
        try:
            result = capture.render(hz, mode)
            with self._lock:
                self._cached_wave = key, result
            return result
        finally:
            RENDER_SLOT.release()

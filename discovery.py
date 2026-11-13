"""Keep recent received audio and reception hits in memory for instant replay."""

from collections import deque
from dataclasses import dataclass
from io import BytesIO
from time import time_ns
import wave

from recording import MIN_CLIP_SECONDS


DISCOVERY_URL = "/api/discovery"
REPLAY_SECONDS = 30
MAX_HITS = 100
SAMPLE_BYTES = 2


@dataclass
class DiscoveryHit:
    id: int
    time_ms: int
    hz: int
    mode: str
    revision: int
    start: int
    end: int
    signal_dbfs: float | None
    tone: dict | None
    dab_sid: str | None
    station_name: str | None


class Discovery:
    """Use a single PCM buffer for replay and the individual logged hits."""

    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self._capacity = sample_rate * SAMPLE_BYTES * REPLAY_SECONDS
        self._audio = deque()
        self._start = self._end = 0
        self._hits = deque(maxlen=MAX_HITS)
        self._next_id = 1
        self._active = None
        self.enabled = self.started = False

    @property
    def replay_available(self):
        return bool(self._audio)

    @property
    def current(self):
        hit = self._active
        return {"hz": hit.hz, "mode": hit.mode, "tone": hit.tone} if hit else None

    def control(self, command):
        if command == "open" and self.started:
            return
        self.enabled = not self.enabled if command == "toggle" else True
        self.started = True
        self.boundary()

    def boundary(self):
        self._active = None

    def receive(self, pcm, *, channel=None, revision=0, signal_dbfs=None, tone=None,
                dab_sid=None, station_name=None):
        if not pcm:
            self.boundary()
            return
        start = self._end
        self._end += len(pcm)
        self._audio.append(pcm)
        excess = self._end - self._start - self._capacity
        while excess > 0:
            chunk = self._audio.popleft()
            dropped = min(excess, len(chunk))
            if dropped < len(chunk):
                self._audio.appendleft(chunk[dropped:])
            self._start += dropped
            excess -= dropped

        if not (self.enabled and channel):
            self.boundary()
            return
        active = self._active
        if (active is None or (active.hz, active.mode) != channel
                or active.revision != revision or active.dab_sid != dab_sid):
            active = DiscoveryHit(self._next_id, time_ns() // 1_000_000, *channel, revision,
                                  start, self._end, signal_dbfs, tone, dab_sid, station_name)
            self._next_id += 1
            self._hits.appendleft(active)
            self._active = active
        else:
            active.end = self._end
            if tone is not None:
                active.tone = tone
            if signal_dbfs is not None:
                active.signal_dbfs = signal_dbfs if active.signal_dbfs is None else max(active.signal_dbfs, signal_dbfs)

    def status(self):
        bytes_per_second = self.sample_rate * SAMPLE_BYTES
        return {
            "started": self.started,
            "enabled": self.enabled,
            "replay_available": self.replay_available,
            "hits": [{
                "id": hit.id,
                "time_ms": hit.time_ms,
                "hz": hit.hz,
                "mode": hit.mode,
                "tone": hit.tone,
                "signal_dbfs": hit.signal_dbfs,
                "duration_ms": round((hit.end - hit.start) * 1000 / bytes_per_second),
                "playable": hit.end > self._start,
                "recordable": hit.end - max(hit.start, self._start) >= MIN_CLIP_SECONDS * bytes_per_second,
            } for hit in self._hits],
        }

    def hit(self, hit_id):
        return next((hit for hit in self._hits if hit.id == hit_id), None)

    def audio(self, hit_id=None):
        if hit_id is None:
            return tuple(self._audio)
        hit = self.hit(hit_id)
        if hit is None or hit.end <= self._start:
            return ()
        chunks = []
        position = self._start
        for chunk in self._audio:
            end = position + len(chunk)
            if end > hit.start:
                start_index = max(0, hit.start - position)
                end_index = hit.end - position
                chunks.append(chunk[start_index:end_index])
            if end >= hit.end:
                break
            position = end
        return tuple(chunks)


def wave_audio(chunks, sample_rate):
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, SAMPLE_BYTES, sample_rate, 0, "NONE", ""))
        stream.writeframes(b"".join(chunks))
    return output.getvalue()

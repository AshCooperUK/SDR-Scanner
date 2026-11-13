"""Assign browser sessions to independent scanner runtimes when USB permits."""

from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

from receiver import Receiver, ReceiverError


LEASE_SECONDS = 90.0
CHECK_SECONDS = 0.75
RECEIVER_GRACE = 2.5
PROBE_RETRY_SECONDS = 2.0
REORDER_RETRY_SECONDS = 6.0
STANDBY_LIGHT_SECONDS = 3.0


@dataclass
class Lease:
    seen: float
    index: int = 0
    parked_at: float | None = None
    volume: float | None = None
    muted: bool = False


class RadioPool:
    def __init__(self, state_path, runtime_factory, receiver_factory=Receiver):
        self._runtime_factory = runtime_factory
        self._receiver_factory = receiver_factory
        state_path = Path(state_path)
        self._extra_state = state_path.parent / ".scanner-radios"
        if state_path.name != ".scanner-state.json":
            self._extra_state /= state_path.name
        self._lock = Lock()
        self._refresh_lock = Lock()
        self._stop = Event()
        self._wake = Event()
        self._leases = {}
        self._pending = set()
        self._unavailable_since = {}
        self._probe_again = {}
        self._reorder_after = 0.0
        self._watch_excluded = frozenset({0})
        self.primary = runtime_factory(state_path, receiver_index=0, watch_available=self.watch_available)
        self._radios = {0: self.primary}
        self._discovery = None
        self._worker = Thread(target=self._run, name="SDR session allocation", daemon=True)

    def start(self):
        self.primary.start()
        self._worker.start()

    def watch_available(self, index):
        # The WATCH worker can call this without acquiring either pool lock.
        return index not in self._watch_excluded

    def _update_watch_exclusions(self):
        self._watch_excluded = frozenset(self._radios.keys() | self._pending)

    def _primary_listeners(self):
        return [token for token, lease in self._leases.items()
                if lease.index == 0 and lease.parked_at is None]

    def _waiting(self):
        return self._primary_listeners()[1:]

    def _expire(self, now):
        for token in [token for token, lease in self._leases.items() if now - lease.seen >= LEASE_SECONDS]:
            del self._leases[token]

    def _fallback_index(self):
        if self.primary.receiver_status == "unavailable":
            # An open dongle can keep running at its original logical slot
            # when the physical USB indices change after a disconnection.
            return next((index for index, radio in self._radios.items()
                         if radio.receiver_status == "ready"), 0)
        return 0

    def _assignment(self, token):
        lease = self._leases.get(token)
        if token is not None and (lease is None or lease.parked_at is not None):
            runtime = self._radios.get(lease.index, self.primary) if lease else self.primary
            return runtime, None, False
        fallback = self._fallback_index()
        index = (lease.index or fallback) if lease else 0
        shared = sum((item.index or fallback) == index for item in self._leases.values()
                     if item.parked_at is None) > 1
        return self._radios[index], index + 1, shared

    def _active_indices(self):
        fallback = self._fallback_index()
        indices = {lease.index for lease in self._leases.values() if lease.parked_at is None}
        if 0 in indices:
            indices.add(fallback)
        return indices

    def resolve(self, token, *, touch=True):
        with self._lock:
            if token is not None:
                lease = self._leases.get(token)
                now = monotonic()
                expired = lease is not None and now - lease.seen >= LEASE_SECONDS
                if not touch and expired:
                    return self._radios.get(lease.index, self.primary), None, False
                if touch and not self._stop.is_set():
                    if lease is None or expired:
                        self._expire(now)
                        self._leases[token] = Lease(now, parked_at=now)
                    else:
                        lease.seen = now
            return self._assignment(token)

    def parked_backlight(self, token):
        with self._lock:
            lease = self._leases.get(token)
            return (lease is not None and lease.parked_at is not None
                    and monotonic() - lease.parked_at < STANDBY_LIGHT_SECONDS)

    def muted(self, token):
        with self._lock:
            lease = self._leases.get(token)
            return lease is not None and lease.parked_at is None and lease.muted

    def mute(self, token, gesture):
        self.resolve(token)
        with self._lock:
            runtime, radio_id, shared = self._assignment(token)
            if gesture == "tap" and radio_id is not None and runtime.state()["power"]:
                lease = self._leases[token]
                lease.muted = not lease.muted
            return runtime, radio_id, shared

    def volume(self, token, expected_radio, action):
        self.resolve(token)
        with self._lock:
            self._expire(monotonic())
            runtime, radio_id, shared = self._assignment(token)
            if action["value"] and expected_radio is not None and expected_radio != radio_id:
                return runtime, radio_id, shared, False
            lease = self._leases.get(token)
            if lease is None:
                accepted = token is None and runtime.act(action) is not None
                return runtime, radio_id, shared, accepted

            if action["value"] == 0:
                if lease.parked_at is None:
                    previous = {lease.index, radio_id - 1}
                    lease.index = radio_id - 1
                    lease.parked_at = monotonic()
                    lease.volume = None
                    for index in previous - self._active_indices():
                        self._radios[index].act(action)
                    self._wake.set()
                return runtime, None, False, True

            if lease.parked_at is None:
                accepted = self._apply_volume(runtime, lease, action)
                return runtime, radio_id, shared, accepted

            lease.parked_at = None
            lease.index = 0
            self._leases[token] = self._leases.pop(token)
            try_spare = bool(self._waiting())
        if try_spare:
            self._refresh(wait=CHECK_SECONDS)
            self._wake.set()

        with self._lock:
            runtime, radio_id, shared = self._assignment(token)
            if self._leases.get(token) is not lease or lease.parked_at is not None:
                return runtime, radio_id, shared, False
            if shared and runtime.state()["power"]:
                lease.volume = action["value"]
                return runtime, radio_id, shared, True
            accepted = self._apply_volume(runtime, lease, action)
            return runtime, radio_id, shared, accepted

    @staticmethod
    def _apply_volume(runtime, lease, action):
        result = runtime.act(action)
        if result is None:
            return False
        lease.volume = action["value"] if result["power"] else None
        return True

    def leave(self, token):
        with self._lock:
            self._leases.pop(token, None)
        self._wake.set()

    def _device_count(self):
        try:
            if self._discovery is None:
                self._discovery = self._receiver_factory(0)
            return self._discovery.device_count()
        except ReceiverError:
            return None

    def _probe(self, index):
        receiver = self._receiver_factory(index)
        try:
            receiver.open()
            return True
        except ReceiverError:
            return False
        finally:
            receiver.close()

    def _retire_radios(self, count, now):
        with self._lock:
            active = self._active_indices()
            retired = []
            for index, runtime in list(self._radios.items())[1:]:
                status = runtime.receiver_status
                if status == "unavailable":
                    unavailable = now - self._unavailable_since.setdefault(index, now) >= RECEIVER_GRACE
                else:
                    self._unavailable_since.pop(index, None)
                    unavailable = False
                disappeared = count is not None and index >= count and status != "ready"
                if index in active and not unavailable and not disappeared:
                    continue
                moving = [lease for lease in self._leases.values()
                          if lease.index == index and lease.parked_at is None]
                if 0 not in active:
                    volume = next((lease.volume for lease in moving if lease.volume is not None), None)
                    if volume is not None and not self.primary.state()["power"]:
                        self._continue_listening(runtime, self.primary, volume)
                for lease in moving:
                    lease.index = 0
                del self._radios[index]
                self._unavailable_since.pop(index, None)
                self._pending.add(index)
                retired.append((index, runtime))
            self._update_watch_exclusions()
        for index, runtime in retired:
            runtime.close()
            with self._lock:
                self._pending.discard(index)
                self._update_watch_exclusions()

    def _assign_radios(self, count, now):
        with self._lock:
            watch_indices = {runtime.watch_index for runtime in self._radios.values()}
            candidates = [index for index in range(1, count)
                          if index not in self._radios and self._probe_again.get(index, 0) <= now]
        # Take a truly idle dongle first so WATCH can keep its current receiver.
        candidates.sort(key=lambda index: index in watch_indices)
        yielding = set()
        assigned = False
        try:
            for index in candidates:
                with self._lock:
                    if self._stop.is_set() or len(self._waiting()) <= len(yielding):
                        break
                    self._pending.add(index)
                    self._update_watch_exclusions()
                    watch_busy = any(runtime.watch_index == index for runtime in self._radios.values())
                if watch_busy:
                    yielding.add(index)
                    continue
                if self._probe(index):
                    self._probe_again.pop(index, None)
                    if self._start_radio(index):
                        assigned = True
                else:
                    self._probe_again[index] = now + PROBE_RETRY_SECONDS
                with self._lock:
                    self._pending.discard(index)
                    self._update_watch_exclusions()
        finally:
            with self._lock:
                self._pending = yielding if self._waiting() else set()
                self._update_watch_exclusions()
        return assigned

    def _start_radio(self, index):
        path = self._extra_state / f"radio-{index + 1}" / "state.json"
        runtime = self._runtime_factory(path, receiver_index=index, watch_available=self.watch_available)
        runtime.start()
        with self._lock:
            waiting = self._waiting()
            if waiting and not self._stop.is_set():
                lease = self._leases[waiting[0]]
                if lease.volume is not None:
                    source, _, _ = self._assignment(waiting[0])
                    self._continue_listening(source, runtime, lease.volume)
                self._radios[index] = runtime
                lease.index = index
                self._update_watch_exclusions()
                return True
        runtime.close()
        return False

    @staticmethod
    def _continue_listening(source, destination, volume):
        state = source.state()
        if not state["power"]:
            return
        if state.get("dab_enabled"):
            destination.resume_dab(source.dab_handoff_station(), volume)
            return
        destination.act({"knob": "volume", "value": volume})
        if "HOLD" in state.get("lower", ()) and state.get("tune_hz"):
            mhz, hz = divmod(state["tune_hz"], 1_000_000)
            destination.act({"tune": "frequency", "mhz": f"{mhz}.{hz:06d}"})

    def _repair_usb_order(self, count, now):
        if count < 2 or now < self._reorder_after or self._stop.is_set():
            return
        with self._lock:
            status = self.primary.receiver_status
            if status == "ready" and self._waiting() and not self._pending:
                candidates = [(0, self.primary)]
            elif status == "unavailable":
                candidates = [(index, runtime) for index, runtime in self._radios.items()
                              if index < count and runtime.receiver_status == "ready"]
            else:
                candidates = []
        if not candidates:
            return
        self._reorder_after = now + REORDER_RETRY_SECONDS
        for index, runtime in candidates:
            # If the configured index opens while the runtime's handle is live,
            # libusb now enumerates that handle's dongle at a different index.
            if runtime.receiver_status == "ready" and self._probe(index) and runtime.receiver_status == "ready":
                self._probe_again.clear()
                runtime.reopen_receiver()
                break

    def _refresh(self, *, wait=0):
        if self._stop.is_set() or not self._refresh_lock.acquire(timeout=wait):
            return
        try:
            now = monotonic()
            with self._lock:
                self._expire(now)
                needs_usb = len(self._radios) > 1 or bool(self._waiting()) or bool(self._pending)
            if needs_usb:
                count = self._device_count()
                self._retire_radios(count, now)
                if not self._assign_radios(count or 0, now):
                    self._repair_usb_order(count or 0, now)
        finally:
            self._refresh_lock.release()

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            self._refresh()
            self._wake.wait(CHECK_SECONDS)

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._worker.ident is not None:
            self._worker.join()
        with self._refresh_lock:
            with self._lock:
                radios = list(self._radios.values())
                self._radios = {0: self.primary}
                self._leases.clear()
            for runtime in radios:
                runtime.close()

"""Conventional DMR tuning, saved receive filters, and scan state."""

import json
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from unicodedata import category

from scanner import MANUAL_MAX_HZ, MANUAL_MIN_HZ


LOG = logging.getLogger(__name__)
MAX_ENTRIES = 1000
MAX_AVOIDS = 1000
MAX_FILE_BYTES = 1_024_000
MAX_LABEL = 64
MAX_DMR_ID = (1 << 24) - 1
SCAN_DWELL = 0.80
REPLY_DELAY = 0.70
CALL_TIMEOUT = 0.40
DEFAULT_SEARCH = (430_000_000, 440_000_000, 12_500)
SEARCH_STEPS = (6_250, 12_500, 25_000)
SCOPES = ("channel", "department", "system")
ENTRY_KEYS = {"id", "hz", "name", "system", "department", "color_code", "slot", "talkgroup"}
CALL_LIMITS = {"color_code": (0, 15), "slot": (1, 2), "talkgroup": (0, MAX_DMR_ID),
               "unit_id": (0, MAX_DMR_ID), "errors": (0, 65535)}
FILTERS = ("color_code", "slot", "talkgroup")


def _in_range(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def _is_hz(value):
    return _in_range(value, MANUAL_MIN_HZ, MANUAL_MAX_HZ)


def _is_entry_id(value):
    return _in_range(value, 1, (1 << 31) - 1)


def _is_label(value):
    return (isinstance(value, str) and len(value) <= MAX_LABEL
            and all(category(char) not in ("Cc", "Cs", "Zl", "Zp") for char in value))


def _is_entry(entry):
    return (isinstance(entry, dict) and not (entry.keys() - ENTRY_KEYS) and _is_hz(entry.get("hz"))
            and ("id" not in entry or _is_entry_id(entry["id"]))
            and all(_is_label(entry.get(key, "")) for key in ("name", "system", "department"))
            and all(entry.get(key) is None or _in_range(entry[key], *CALL_LIMITS[key]) for key in FILTERS))


def valid_dmr_action(action):
    """Validate a browser action without consulting the current radio state."""
    if not isinstance(action, dict):
        return False
    kind, keys = action.get("dmr"), action.keys()
    if kind == "toggle":
        return keys == {"dmr"} or keys == {"dmr", "enabled"} and type(action["enabled"]) is bool
    if kind == "tune":
        return keys == {"dmr", "hz"} and _is_hz(action["hz"])
    if kind == "step":
        return keys == {"dmr", "direction"} and type(action["direction"]) is int and action["direction"] in (-1, 1)
    if kind == "search":
        return keys == {"dmr"} or (keys == {"dmr", "lower", "upper", "step"}
            and _is_hz(action["lower"]) and _is_hz(action["upper"]) and action["lower"] <= action["upper"]
            and type(action["step"]) is int and action["step"] in SEARCH_STEPS)
    if kind == "hold":
        return keys == {"dmr", "scope"} and action["scope"] in SCOPES
    if kind == "avoid":
        return (keys in ({"dmr", "permanent"}, {"dmr", "permanent", "scope"})
                and type(action["permanent"]) is bool and action.get("scope", "channel") in SCOPES)
    if kind == "save":
        return keys == {"dmr", "entry"} and _is_entry(action["entry"])
    if kind in ("recall", "delete"):
        return keys == {"dmr", "id"} and _is_entry_id(action["id"])
    return kind in ("scan", "clear_avoids") and keys == {"dmr"}


def _entry_with_defaults(entry, number):
    hz = entry["hz"]
    return {"id": number, "hz": hz, "name": entry.get("name", "").strip() or f"{hz / 1e6:.6f} MHz",
            "system": entry.get("system", "").strip() or "DMR Favourites",
            "department": entry.get("department", "").strip() or "Channels",
            **{key: entry.get(key) for key in FILTERS}}


def _is_avoid(key):
    if not isinstance(key, (list, tuple)) or not 2 <= len(key) <= 3:
        return False
    scope, value, *rest = key
    if scope == "channel":
        return not rest and _is_entry_id(value)
    if scope == "hz":
        return not rest and _is_hz(value)
    if scope == "system":
        return not rest and bool(value) and _is_label(value)
    return (scope == "department" and bool(rest) and bool(value) and bool(rest[0])
            and _is_label(value) and _is_label(rest[0]))


class DmrState:
    """One receiver's DMR mode. The application owns its runtime lock."""

    def __init__(self, path):
        self.path = Path(path)
        self.entries, self.permanent_avoids = self._load()
        self.temporary_avoids = set()
        self.enabled = False
        self.hz = None
        self.revision = 0
        self.mode = "hold"
        self.direction = 1
        self.hold_scope = "channel"
        self.search = DEFAULT_SEARCH
        self.call = None
        self.error = None
        self.notice = None
        self._entry_id = None
        self._scope_key = None
        self._resume_mode = "scan"
        self._call_at = 0.0
        self._advance_at = 0.0

    @property
    def entry(self):
        return self.entries.get(self._entry_id)

    def _load(self):
        try:
            with self.path.open("rb") as stream:
                payload = stream.read(MAX_FILE_BYTES + 1)
            if len(payload) > MAX_FILE_BYTES:
                raise ValueError("DMR settings exceed size limit")
            data = json.loads(payload)
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError("Unknown DMR settings file format")
            entries, avoids = data.get("entries", []), data.get("avoids", [])
            if not isinstance(entries, list) or not isinstance(avoids, list):
                raise ValueError("Invalid DMR settings")
            if len(entries) > MAX_ENTRIES or len(avoids) > MAX_AVOIDS:
                raise ValueError("Too many DMR entries or avoids")
            results = {}
            for entry in entries:
                if not _is_entry(entry) or "id" not in entry or entry["id"] in results:
                    raise ValueError("Invalid or duplicate DMR entry")
                results[entry["id"]] = _entry_with_defaults(entry, entry["id"])
            if any(not _is_avoid(key) for key in avoids):
                raise ValueError("Invalid saved DMR avoid")
            return results, {tuple(key) for key in avoids}
        except FileNotFoundError:
            return {}, set()
        except (OSError, ValueError, RecursionError) as error:
            LOG.warning("Could not read DMR programming: %s", error)
            return {}, set()

    def _save(self, entries, avoids):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                    prefix=self.path.name + ".", delete=False) as stream:
                pending = Path(stream.name)
                json.dump({"version": 1, "entries": list(entries.values()), "avoids": sorted(avoids)},
                          stream, separators=(",", ":"), ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(self.path)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
        self.entries = entries
        self.permanent_avoids = avoids

    def report_error(self, message):
        self.error = str(message)[:240] if message else None
        if self.error:
            self.stop_signal()

    def stop_signal(self):
        self.call = None
        self._call_at = 0.0

    def begin_dwell(self):
        """Give a newly prepared decoder a full interval to acquire a call."""
        self._advance_at = monotonic() + SCAN_DWELL

    def power_off(self):
        self.temporary_avoids.clear()
        self.stop_signal()
        self.begin_dwell()

    def _set_target(self, hz, number=None, *, force=False, now=None):
        if now is None:
            now = monotonic()
        if force or (hz, number) != (self.hz, self._entry_id):
            self.hz, self._entry_id = hz, number
            self.revision += 1
            self.stop_signal()
        self._advance_at = now + SCAN_DWELL

    def _avoid_keys(self, hz, entry):
        if entry is None:
            return (("hz", hz),) if hz else ()
        return (("hz", hz), ("channel", entry["id"]), ("system", entry["system"]),
                ("department", entry["system"], entry["department"]))

    def _avoid_kind(self, hz, entry):
        keys = self._avoid_keys(hz, entry)
        if any(key in self.permanent_avoids for key in keys):
            return "permanent"
        if any(key in self.temporary_avoids for key in keys):
            return "temporary"

    def _scan_entries(self):
        for number, entry in sorted(self.entries.items()):
            if self._scope_key is not None and self._scope_key not in self._avoid_keys(entry["hz"], entry):
                continue
            if self._avoid_kind(entry["hz"], entry) is None:
                yield number

    def _next_entry(self, *, keep_current=False, preferred=None, now=None):
        numbers = list(self._scan_entries())
        if not numbers:
            self._set_target(None, now=now)
            return
        if self.direction < 0:
            numbers.reverse()
        current = self._entry_id
        if preferred in numbers:
            number = preferred
        elif keep_current and current in numbers:
            number = current
        else:
            number = next((number for number in numbers if current is None
                           or self.direction * (number - current) > 0), numbers[0])
        self._set_target(self.entries[number]["hz"], number, now=now)

    def _next_frequency(self, *, first=False, now=None):
        lower, upper, step = self.search
        last_index = (upper - lower) // step
        if first or self.hz is None or not lower <= self.hz <= upper:
            index = 0 if self.direction > 0 else last_index
        elif self.direction > 0:
            index = (self.hz - lower) // step + 1
        else:
            index = (self.hz - lower - 1) // step
        # At most one more candidate than the number of stored avoids is needed.
        attempts = min(last_index + 1, len(self.temporary_avoids) + len(self.permanent_avoids) + 1)
        for _ in range(attempts):
            index %= last_index + 1
            hz = lower + index * step
            if self._avoid_kind(hz, None) is None:
                self._set_target(hz, now=now)
                return
            index += self.direction
        self._set_target(None, now=now)

    def _set_mode(self, mode, scope=None, scope_key=None):
        if mode != "hold":
            self._resume_mode = mode
        self.mode, self.hold_scope, self._scope_key = mode, scope, scope_key

    @staticmethod
    def _group_key(scope, entry):
        key = (scope, entry["system"])
        return key if scope == "system" else (*key, entry["department"])

    def _hold(self, scope):
        if scope == "channel":
            if self.mode == "hold":
                if self._resume_mode == "search":
                    self._set_mode("search")
                    self._next_frequency()
                else:
                    key = self._scope_key
                    self._set_mode("scan", key[0] if key else None, key)
                    self._next_entry()
            else:
                self._set_mode("hold", scope, self._scope_key)
            return
        if self._scope_key and self._scope_key[0] == scope:
            self._set_mode("scan")
            self._next_entry(keep_current=True)
            return
        entry = self.entry
        if entry is None:
            groups = {}
            for candidate in self.entries.values():
                if candidate["hz"] == self.hz:
                    groups.setdefault(self._group_key(scope, candidate), candidate)
            if len(groups) != 1:
                self.notice = f"Select a channel in FAV to hold its {scope}."
                return
            entry = next(iter(groups.values()))
        self._set_mode("scan", scope, self._group_key(scope, entry))
        self._next_entry(preferred=entry["id"])

    def _step(self, direction):
        self.direction = direction
        if self.mode == "search":
            self._next_frequency()
        elif self.mode != "hold" or self.entry is not None or self.hz is None:
            self._next_entry()
        else:
            hz = self.hz + self.direction * self.search[2]
            if _is_hz(hz):
                self._set_target(hz)

    def _avoid(self, permanent, scope):
        entry = self.entry
        if scope == "channel":
            key = ("channel", entry["id"]) if entry else ("hz", self.hz)
        elif entry is None:
            return
        elif scope == "system":
            key = ("system", entry["system"])
        else:
            key = ("department", entry["system"], entry["department"])
        if not _is_avoid(key):
            return
        if permanent:
            if key not in self.permanent_avoids and len(self.permanent_avoids) >= MAX_AVOIDS:
                self.notice = "Permanent avoids are full"
                return
            self._save(self.entries, self.permanent_avoids | {key})
            self.temporary_avoids.discard(key)
        elif key not in self.permanent_avoids:
            if key not in self.temporary_avoids and len(self.temporary_avoids) >= MAX_AVOIDS:
                self.notice = "Temporary avoids are full"
                return
            self.temporary_avoids.add(key)
        if self.mode == "scan":
            self._next_entry()
        elif self.mode == "search":
            self._next_frequency()

    def _save_entry(self, entry):
        number = entry.get("id")
        if number is None:
            if len(self.entries) >= MAX_ENTRIES:
                self.notice = "DMR favourites are full"
                return
            number = next(index for index in range(1, MAX_ENTRIES + 1) if index not in self.entries)
        elif number not in self.entries:
            self.notice = "Favourite no longer exists"
            return
        saved = _entry_with_defaults(entry, number)
        old = self.entries.get(number)
        self._save(self.entries | {number: saved}, self.permanent_avoids)
        self._set_mode("hold", "channel")
        self._set_target(saved["hz"], number, force=old != saved)

    def _delete_entry(self, number):
        if number not in self.entries:
            self.notice = "Favourite no longer exists"
            return
        remaining = self.entries.copy()
        removed = remaining.pop(number)
        key = ("channel", number)
        self._save(remaining, self.permanent_avoids - {key})
        self.temporary_avoids.discard(key)
        if number == self._entry_id:
            if self.mode == "scan":
                self._next_entry()
            else:
                self._set_target(removed["hz"])

    def act(self, action):
        if not valid_dmr_action(action):
            raise ValueError("Invalid DMR control")
        self.notice = None
        kind = action["dmr"]
        if kind == "toggle":
            enabled = action.get("enabled", not self.enabled)
            if enabled != self.enabled:
                self.enabled = enabled
                self.revision += 1
                self.report_error(None)
                self.stop_signal()
                self.begin_dwell()
                if enabled and self.hz is None:
                    if self.mode == "search":
                        self._next_frequency(first=True)
                    else:
                        self._next_entry()
            return
        if not self.enabled:
            return
        if kind == "tune":
            self._set_mode("hold", "channel")
            self._set_target(action["hz"])
        elif kind == "step":
            self._step(action["direction"])
        elif kind == "scan":
            was_scanning = self.mode == "scan"
            key = self._scope_key
            self._set_mode("scan", key[0] if key else None, key)
            self._next_entry(keep_current=not was_scanning)
        elif kind == "search":
            if "lower" in action:
                self.search = action["lower"], action["upper"], action["step"]
            elif self.mode == "search":
                self._set_mode("hold", "channel")
                return
            self._set_mode("search")
            if ("lower" not in action and self.hz is not None
                    and self.search[0] <= self.hz <= self.search[1] and self._avoid_kind(self.hz, None) is None):
                self._set_target(self.hz)
            else:
                self._next_frequency(first="lower" in action)
        elif kind == "hold":
            self._hold(action["scope"])
        elif kind == "avoid":
            scope = action.get("scope", self.hold_scope or "channel")
            self._avoid(action["permanent"], scope)
        elif kind == "clear_avoids":
            self._save(self.entries, set())
            self.temporary_avoids.clear()
        elif kind == "save":
            self._save_entry(action["entry"])
        elif kind == "delete":
            self._delete_entry(action["id"])
        elif kind == "recall":
            if entry := self.entries.get(action["id"]):
                self._set_mode("hold", "channel")
                self._set_target(entry["hz"], entry["id"])
            else:
                self.notice = "Favourite no longer exists"

    def _expire_call(self, now):
        if self.call is not None and now - self._call_at >= CALL_TIMEOUT:
            self.stop_signal()

    def receive(self, metadata=None, now=None):
        """Update decoder status and report whether this call may open audio."""
        if now is None:
            now = monotonic()
        if not self.enabled or self.hz is None or self.error:
            return False
        if metadata is None:
            self._expire_call(now)
        elif metadata.get("sync", metadata.get("synced")) is not True:
            self.stop_signal()
        else:
            call = {key: value if _in_range(value, *limit) else None
                    for key, limit in CALL_LIMITS.items() if (value := metadata.get(key)) is not None}
            call.update(sync=True, voice=metadata.get("voice") is True, encrypted=None)
            if type(metadata.get("encrypted")) is bool:
                call["encrypted"] = metadata["encrypted"]
            entry = self.entry
            call["matched"] = entry is None or all(entry[key] is None or call.get(key) == entry[key] for key in FILTERS)
            self.call = call
            self._call_at = now
        open_audio = bool(self.call and self.call["matched"] and self.call["voice"] and self.call["encrypted"] is False)
        if open_audio:
            self._advance_at = max(self._advance_at, now + REPLY_DELAY)
        return open_audio

    def tick(self, now=None):
        if not self.enabled:
            return
        if now is None:
            now = monotonic()
        self._expire_call(now)
        if self.error or now < self._advance_at:
            return
        if self.mode == "scan":
            self._next_entry(now=now)
        elif self.mode == "search":
            self._next_frequency(now=now)

    def snapshot(self):
        avoid_kind = self._avoid_kind(self.hz, self.entry)
        lower, upper, step = self.search
        return {"enabled": self.enabled, "hz": self.hz, "mode": self.mode, "direction": self.direction,
                "hold_scope": self.hold_scope, "entry": self.entry.copy() if self.entry else None,
                "entries": [entry.copy() for _, entry in sorted(self.entries.items())],
                "call": self.call.copy() if self.call else None, "error": self.error, "notice": self.notice,
                "search": {"lower": lower, "upper": upper, "step": step},
                "avoided": avoid_kind is not None, "avoid_kind": avoid_kind}

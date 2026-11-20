"""Persist the shared browser layout without sessions, credentials or radio actions."""

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile
import threading


PROFILE_LIMIT = 128 * 1024
MAX_REVISION = 2 ** 53 - 1
MODES = {"AUTO", "FM", "NFM", "WFM", "AM", "S-AM(D)", "S-AM(U)", "S-AM(L)", "USB", "LSB",
         "CW", "CW-R", "FSK", "FSK-R", "DMR", "D-STAR", "P25", "dPMR", "DPMR", "NXDN-VN"}
SCAN_KINDS = {"PROG", "DELTA", "AUTO", "MEMO", "SEL", "MODE", "ZONE", "SIMPLE"}
ZONES = {"FAV", "CB", "PMR", "BUS", "HAM", "BCAST", "RAIL", "EVT", "UTIL", "AIR", "MRN", "NEARBY",
         *("CUSTOM" + str(index) for index in range(1, 13))}
QUICK_ITEMS = {"group", "meter", "record", "mirror", "tier-iii", "snap-carrier", "record-search", "rf-replay",
               "bookmarks", "mode", "tuning-step", "filter", "scan-start", "scan-settings", "scan-speed", "scan-pause",
               "priority", "skip", "memory", "scope", "volume", "boost", "squelch", "rf-gain", "history", "replay"}
MOBILE_KEY_ACTIONS = {"none", "quick", "monitor", "record", "mode", "frequency", "volume", "speech",
                      "lockout", "simple-scan", "bookmarks", "scan-speed", "memory", "group",
                      "tuning-lock", "scope", "recenter-scope"}
DIAL_FUNCTIONS = {"unassigned", "tuning", "fine-tuning", "coarse-tuning", "tuning-step", "memory-channel",
                  "preset-channel", "favorite-bank-a", "favorite-bank-b", "volume", "squelch", "rf_gain",
                  "rf_gain_tenths_db", "audio_boost", "tone_treble", "tone_bass", "beep_level", "scope-span",
                  "scope-pan", "scope_reference", "scan-direction", "scan_speed", "pause_time", "resume_time",
                  "priority_interval"}
DIAL_PRESS = {"auto", "frequency-entry", "tuning-step", "tuning-lock", "vfo-memory", "memory-channels",
              "memory-groups", "favorite-bank", "preset-settings", "adjust", "reset", "mute", "monitor",
              "audio-controls", "scope-span", "recenter-scope", "center-fixed", "full-capture", "scope-hold",
              "scope-marker", "scope-settings", "scan", "reverse-scan", "preset-hold", "scan-settings", "priority",
              "dial-settings", "quick", "function", "receive-adjustments", "none"}
COMMON_PRESS = {"auto", "dial-settings", "quick", "function", "receive-adjustments", "none"}
TUNING_PRESS = {"frequency-entry", "tuning-step", "tuning-lock", "vfo-memory"}
AUDIO_PRESS = {"adjust", "reset", "audio-controls"}
SCAN_PRESS = {"adjust", "scan-settings", "scan", "preset-hold"}
DIAL_PRESS_BY_FUNCTION = {
    "tuning": TUNING_PRESS, "fine-tuning": TUNING_PRESS, "coarse-tuning": TUNING_PRESS,
    "tuning-step": {"tuning-step", "frequency-entry", "tuning-lock"},
    "memory-channel": {"memory-channels", "memory-groups", "vfo-memory"},
    "preset-channel": {"preset-settings", "preset-hold", "scan"},
    "favorite-bank-a": {"favorite-bank", "memory-groups"}, "favorite-bank-b": {"favorite-bank", "memory-groups"},
    "volume": {"mute", "adjust", "audio-controls"}, "squelch": {"monitor", "adjust", "audio-controls"},
    "rf_gain": {"adjust", "audio-controls"}, "rf_gain_tenths_db": AUDIO_PRESS, "audio_boost": AUDIO_PRESS,
    "tone_treble": AUDIO_PRESS, "tone_bass": AUDIO_PRESS, "beep_level": AUDIO_PRESS,
    "scope-span": {"scope-span", "full-capture", "scope-hold", "recenter-scope"},
    "scope-pan": {"recenter-scope", "center-fixed", "scope-hold", "full-capture"},
    "scope_reference": {"adjust", "reset", "scope-marker", "scope-settings"},
    "scan-direction": {"reverse-scan", "scan", "preset-hold"}, "scan_speed": SCAN_PRESS,
    "pause_time": SCAN_PRESS, "resume_time": SCAN_PRESS, "priority_interval": {"adjust", "priority", "scan-settings"},
}
DIAL_PREFIXES = [*("dial_" + letter for letter in "abcdefghij"), "main_dial"]


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _text(value, limit):
    if not isinstance(value, str) or "\0" in value:
        return False
    try:
        # Match JavaScript's UTF-16 .length and reject invalid surrogate text.
        return len(value.encode("utf-16-le")) // 2 <= limit
    except UnicodeEncodeError:
        return False


def _hz(value):
    return _integer(value, 100_000, 1_750_000_000)


def _one_of(choices):
    # Avoid treating bools as integers or accepting unhashable JSON values.
    return lambda value: type(value) in (str, int, float) and not isinstance(value, bool) and value in choices


def _boolean(value):
    return type(value) is bool


def _panel(value):
    flags = {"dialLock", "channelDial", "expanded", "visible", "freeze", "fixed", "marker"}
    if not isinstance(value, dict) or value.keys() != flags | {"page", "span", "edges", "scopePan"}:
        return False
    edges = value["edges"]
    return (all(_boolean(value[key]) for key in flags) and _integer(value["page"], 1, 2)
            and (value["span"] == "full" or _number(value["span"], 1, 1_800_000))
            and _number(value["scopePan"], -1_750_000_000, 1_750_000_000)
            and (edges is None or isinstance(edges, list) and len(edges) == 2
                 and all(_hz(item) for item in edges) and edges[0] < edges[1])
            and (not value["fixed"] or edges is not None))


def _bookmarks(value):
    return (isinstance(value, list) and len(value) <= 200 and all(
        isinstance(entry, dict) and {"name", "hz", "mode"} <= entry.keys() <= {"name", "hz", "mode", "callsign"}
        and _text(entry["name"], 64) and bool(entry["name"].strip()) and _hz(entry["hz"])
        and _one_of(MODES)(entry["mode"]) and ("callsign" not in entry or _text(entry["callsign"], 64))
        for entry in value))


def _unique_list(value, allowed):
    return (isinstance(value, list) and len(value) <= len(allowed)
            and all(isinstance(item, str) and item in allowed for item in value)
            and len(set(value)) == len(value))


def _scan(value):
    if (not isinstance(value, dict) or value.get("v10") != "scan" or value.get("enabled") is not True
            or not _one_of(SCAN_KINDS)(value.get("kind"))):
        return False
    fields = {
        "v10": lambda item: item == "scan", "kind": _one_of(SCAN_KINDS), "enabled": lambda item: item is True,
        "start_hz": _hz, "end_hz": _hz, "center_hz": _hz,
        "span_hz": lambda item: _integer(item, 1, 1_750_000_000), "mode": _one_of(MODES - {"AUTO"}),
        "group": lambda item: _integer(item, 0, 100), "edge": lambda item: _integer(item, 0, 49),
        "select_tag": lambda item: _integer(item, 1, 9), "start_slot": lambda item: _integer(item, 0, 199),
        "end_slot": lambda item: _integer(item, 0, 199), "auto_clear": _boolean, "direction": _one_of({-1, 1}),
        "step": lambda item: _integer(item, 1, 1_000_000),
        "scope_span_hz": lambda item: _integer(item, 1, 1_800_000), "zone": _one_of(ZONES),
        "zones": lambda items: _unique_list(items, ZONES) and bool(items),
    }
    return (all(key in fields and fields[key](item) for key, item in value.items())
            and not ("start_hz" in value and "end_hz" in value and value["start_hz"] >= value["end_hz"])
            and not ("start_slot" in value and "end_slot" in value and value["start_slot"] > value["end_slot"])
            and (value["kind"] != "ZONE" or bool(value.get("zone") or value.get("zones"))))


def _tune(value):
    return (isinstance(value, dict) and value.keys() == {"hz", "mode", "frequency_mode", "group", "slot"}
            and _hz(value["hz"]) and _one_of(MODES)(value["mode"])
            and value["frequency_mode"] in ("vfo", "memory")
            and _integer(value["group"], 0, 100) and _integer(value["slot"], 0, 199))


VALIDATORS = {
    "lcd_backlight": lambda value: _number(value, 0, 100), "mute_button_dim": _boolean,
    "led_bright": lambda value: _number(value, 0, 100), "lcd_type": _one_of({"A", "B", "C", "D", "E"}),
    "waterfall_colours": _one_of({"spectrum", "blue", "amber", "greyscale"}), "display_memory": _boolean,
    "display_rds": _boolean, "p25_rx_id_format": _one_of({"decimal", "hex"}),
    "speech_language": _one_of({"en-GB", "ja-JP"}), "speech_speed": _one_of({"slow", "fast"}),
    "speech_signal": _boolean, "speech_level": lambda value: _number(value, 0, 100),
    "beep_level": lambda value: _number(value, 0, 100), "beep_confirmation": _boolean,
    "speech_lock_switch": _one_of({"speech", "lock"}), "screen_capture": _boolean,
    "scope_max_hold": _one_of({"off", "10s", "on"}), "scope_averaging": _one_of({0, 2, 3, 4}),
    "scope_waterfall": _boolean, "scope_waterfall_speed": _one_of({"slow", "mid", "fast"}),
    "scope_reference": lambda value: _number(value, -20, 20), "scope_sweep_speed": _one_of({"slow", "mid", "fast"}),
    "meter_type": _one_of({"relative", "dbfs"}), "snap_to_carrier": _boolean,
    "volume": lambda value: _number(value, 0, 100), "volume_muted": _boolean,
    "audio_boost": lambda value: _number(value, 0, 24), "powerVolume": lambda value: _number(value, 0.01, 1),
    "panel_display": _panel, "favorite_bank_a": lambda value: _integer(value, 0, 99),
    "favorite_bank_b": lambda value: _integer(value, 0, 99),
    "quick_menu_v3": lambda value: _unique_list(value, QUICK_ITEMS), "radio_bookmarks": _bookmarks,
    "repeater_locator": lambda value: isinstance(value, str) and (value == "" or bool(re.fullmatch(
        r"[A-R]{2}[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?", value, re.IGNORECASE))),
}
REQUIRED_KEYS = set(VALIDATORS)
for prefix in DIAL_PREFIXES:
    VALIDATORS[prefix + "_function"] = _one_of(DIAL_FUNCTIONS)
    VALIDATORS[prefix + "_press"] = _one_of(DIAL_PRESS)
    VALIDATORS[prefix + "_sensitivity"] = _one_of({0.25, 0.5, 1, 2, 4})
VALIDATORS["dial_auto_ts"] = _one_of({"off", "low", "high"})
REQUIRED_KEYS.update(set(VALIDATORS) - REQUIRED_KEYS)
for kind in ("PROG", "DELTA", "AUTO", "MEMO", "SEL", "MODE"):
    VALIDATORS["scan_" + kind] = lambda value, kind=kind: _scan(value) and value["kind"] == kind
VALIDATORS.update(last_scan=_scan, profile_tune=_tune, profile_squelch=lambda value: _number(value, 0, 1),
                  profile_history_logging=_boolean, mobile_p1_action=_one_of(MOBILE_KEY_ACTIONS),
                  mobile_p2_action=_one_of(MOBILE_KEY_ACTIONS))
OPTIONAL_KEYS = set(VALIDATORS) - REQUIRED_KEYS


def validate_settings(settings):
    if (not isinstance(settings, dict) or not REQUIRED_KEYS <= settings.keys() <= VALIDATORS.keys()
            or not all(VALIDATORS[key](value) for key, value in settings.items())):
        raise ValueError("Invalid shared radio settings.")
    result = deepcopy(settings)
    for prefix in DIAL_PREFIXES:
        choices = COMMON_PRESS | DIAL_PRESS_BY_FUNCTION.get(result[prefix + "_function"], set())
        if result[prefix + "_press"] not in choices:
            result[prefix + "_press"] = "auto"
    return result


class ProfileConflict(ValueError):
    """The shared setup changed while a browser was preparing its patch."""


class BrowserProfile:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._revision = 0
        self._settings = None
        self._load()

    def _load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError("The shared radio settings must be owned by the service user with mode 0600.")
            payload = stream.read(PROFILE_LIMIT + 1)
        if len(payload) > PROFILE_LIMIT:
            raise ValueError("The shared radio settings file is too large.")
        try:
            data = json.loads(payload)
            if (not isinstance(data, dict) or data.keys() != {"revision", "settings"}
                    or not _integer(data["revision"], 1, MAX_REVISION)):
                raise ValueError
            settings = validate_settings(data["settings"])
        except (ValueError, TypeError, RecursionError):
            raise ValueError("The shared radio settings file is invalid.") from None
        self._revision, self._settings = data["revision"], settings

    def snapshot(self):
        with self._lock:
            return {"revision": self._revision, "settings": deepcopy(self._settings)}

    def patch(self, revision, changes):
        if not _integer(revision, 0, MAX_REVISION) or not isinstance(changes, dict) or len(changes) > len(VALIDATORS):
            raise ValueError("Invalid shared radio settings patch.")
        for key, value in changes.items():
            if key not in VALIDATORS or (value is None and key not in OPTIONAL_KEYS) or (value is not None and not VALIDATORS[key](value)):
                raise ValueError("Invalid shared radio setting: " + str(key)[:64])
        with self._lock:
            if revision != self._revision:
                raise ProfileConflict("Shared radio settings changed; retry with the current revision.")
            settings = deepcopy(self._settings or {})
            for key, value in changes.items():
                if value is None:
                    settings.pop(key, None)
                else:
                    settings[key] = deepcopy(value)
            settings = validate_settings(settings)
            if settings == self._settings:
                return self.snapshot()
            if self._revision >= MAX_REVISION:
                raise ValueError("The shared radio settings revision limit was reached.")
            result = {"revision": self._revision + 1, "settings": settings}
            payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > PROFILE_LIMIT:
                raise ValueError("The shared radio settings exceed 128 KB.")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = None
            try:
                with NamedTemporaryFile("wb", dir=self.path.parent, prefix=self.path.name + ".", delete=False) as stream:
                    pending = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                pending.replace(self.path)
            finally:
                if pending is not None:
                    pending.unlink(missing_ok=True)
            self._revision, self._settings = result["revision"], settings
            return self.snapshot()

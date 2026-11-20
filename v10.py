"""Programming and scan state for the V10 receiver panel."""

import json
import logging
import os
import re
from bisect import bisect_left, bisect_right
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from uuid import uuid4

from bandplan import AIR_STEP, PMR, PRESET_SCAN_RANGES, V10_PRESETS, Band, band_search_mode
from scanner import CHIRP_ZONE_LIMIT, MANUAL_MAX_HZ, MANUAL_MIN_HZ, normalize_favorite_name
from tone_search import normalize_tone_filter

LOG = logging.getLogger("scanner.v10")
MODES = ("FM", "WFM", "AM", "S-AM(D)", "S-AM(U)", "S-AM(L)", "USB", "LSB",
         "CW", "CW-R", "FSK", "FSK-R", "DMR", "P25", "D-STAR", "NXDN-VN", "dPMR")
VFO_MODES = (*MODES, "AUTO")
VOICE_MODES = frozenset(("DMR", "P25", "D-STAR", "NXDN-VN", "dPMR"))
SCAN_KINDS = ("PROG", "DELTA", "AUTO", "MEMO", "SEL", "MODE", "ZONE", "SIMPLE")
DIGITAL_SCAN_ACQUISITION_SECONDS = 1.05
DIGITAL_SCAN_PROBE_SECONDS = .06  # One complete DMR frame, including both slots.
WFM_SCAN_ACQUISITION_SECONDS = .10  # Six IQ blocks for a carrier offset, plus settling time.
SCAN_SCOPE_BASE_HZ = 100_000
SCAN_SCOPE_MAX_HZ = 1_800_000
MAX_SCAN_SPEED = 30
PRESET_SCAN_BATCH = 8
ZONE_BANKS = {"CB": ("CBAM", "CBFM"), **{bank: (bank,) for bank in
              ("PMR", "BUS", "HAM", "BCAST", "RAIL", "EVT", "UTIL", "AIR", "MRN")}}
NEARBY_ZONE = "NEARBY"
CUSTOM_STEPS = (100, 500, 1_000, 2_500, 3_125, 5_000, 6_250, AIR_STEP, 10_000, 12_500, 25_000, 50_000, 100_000)
PRESET_RANGE_LIMIT = 64
NEARBY_LIMIT = 128
NEARBY_MIN_SECONDS = .08
NEARBY_MAX_GAP = .25
NEARBY_RESUME_SECONDS = 7.0
NEARBY_ACQUIRE_SECONDS = .55
NEARBY_COMPARE_SECONDS = .20
NEARBY_RETRY_SECONDS = 1.0
NEARBY_LIVE_INTERVAL = .07
NEARBY_IDLE_INTERVAL = .065
NEARBY_LISTENING_INTERVAL = .18
NEARBY_OFFSET_LIMITS = {"AM": 3_000, "FM": 4_200}
NEARBY_MAX_INDEX = (MANUAL_MAX_HZ + 1) * len(MODES) - 1
NEARBY_TILE_HZ = 1_600_000
NEARBY_SWEEPS = (
    Band("VHF", 144_000_000, 174_000_000),
    Band("UHF", 430_000_000, 470_000_000),
    Band("AIR", 117_975_000, 137_000_000, AIR_STEP, "AM"),
    Band("CB", 26_965_000, 27_405_000, 5_000, "AM"),
)
NEARBY_PMR = Band("PMR446", PMR[0], PMR[-1], 12_500)
CUSTOM_ZONES = frozenset(f"CUSTOM{number}" for number in range(1, 13))
ZONES = frozenset((*ZONE_BANKS, "FAV", NEARBY_ZONE, *CUSTOM_ZONES))
ZONE_LIMIT = 16_384
ZONE_MAX_INDEX = (1 << 31) - 1


def auto_band_mode(hz):
    """Default modes for UK broadcast bands and civil VHF voice."""
    if 87_500_000 <= hz < 108_000_000:
        return "WFM"
    if (117_975_000 <= hz < 137_000_000 or 148_500 <= hz < 283_500
            or 526_500 <= hz < 1_606_500):
        return "AM"
    return None


def next_band_frequency(band, previous, direction):
    """Step within one band, wrapping at its inclusive endpoints."""
    low, high, step = band.lower, band.upper, band.step
    upwards = direction > 0
    if previous is None or (not low <= previous < high if upwards else not low < previous <= high):
        return low if upwards else high
    offset = previous - low
    index = (3 * offset + 1) // 25_000 if step == AIR_STEP else offset // step
    if upwards:
        return min(high, band.frequency(index + 1, step))
    if band.frequency(index, step) >= previous:
        index -= 1
    return max(low, band.frequency(index, step))


@dataclass(frozen=True)
class PresetRangePlan:
    """A union of compact receive grids and exact listed frequencies."""

    bands: tuple[Band, ...]
    listed: tuple[int, ...] = ()
    excluded: frozenset[int] = frozenset()

    def next(self, previous, direction):
        if direction > 0:
            candidates = [next_band_frequency(band, previous, direction)
                          for band in self.bands if previous is None or previous < band.upper]
            index = bisect_right(self.listed, previous) if previous is not None else 0
            if index < len(self.listed):
                candidates.append(self.listed[index])
            return min(candidates) if candidates else min(band.lower for band in self.bands)

        candidates = [next_band_frequency(band, previous, direction)
                      for band in self.bands if previous is None or previous > band.lower]
        index = bisect_left(self.listed, previous) if previous is not None else len(self.listed)
        if index:
            candidates.append(self.listed[index - 1])
        return max(candidates) if candidates else max(band.upper for band in self.bands)

    def next_unlocked(self, previous, direction, blocked):
        blocked = blocked | self.excluded
        first = None
        for _ in range(len(blocked) + 1):
            previous = self.next(previous, direction)
            if previous not in blocked:
                return previous
            if previous == first:
                break
            if first is None:
                first = previous
        return None

    def entry(self, zone, frequency):
        if frequency is None or frequency in self.excluded:
            return None
        band = next((band for band in self.bands if band.lower <= frequency <= band.upper), None)
        if band is None:
            return None
        if band.modulation != "AUTO":
            return {"hz": frequency, "mode": band.modulation}
        mode = auto_band_mode(frequency)
        if mode is None and zone in ("HAM", "BUS"):
            mode = band_search_mode(zone, frequency)
        return {"hz": frequency, "mode": mode or "FM", "automatic": True}


def nearby_origin_window(origin):
    frequency, mode = origin
    band = next((band for band in NEARBY_SWEEPS if band.lower <= frequency <= band.upper), None)
    if band is None and (mode not in ("FM", "AM") or auto_band_mode(frequency) == "WFM"):
        return None
    lower = max(band.lower if band else MANUAL_MIN_HZ, frequency - NEARBY_TILE_HZ // 2)
    upper = min(band.upper if band else MANUAL_MAX_HZ, frequency + NEARBY_TILE_HZ // 2)
    if lower < 25_000_000 <= upper:
        lower, upper = (lower, 24_999_999) if frequency < 25_000_000 else (25_000_000, upper)
    return band or Band("Tuned", lower, upper, 6_250, mode), lower, upper, frequency


def nearby_windows(origin):
    """Interleave wide RF windows across voice allocations."""
    frequency, _ = origin
    local = nearby_origin_window(origin)
    windows = [local] if local else []
    for offset in range(0, max(band.upper - band.lower for band in NEARBY_SWEEPS), NEARBY_TILE_HZ):
        for band in NEARBY_SWEEPS:
            lower = band.lower + offset
            if lower < band.upper:
                windows.append((band, lower, min(lower + NEARBY_TILE_HZ, band.upper), frequency))
    return windows


@dataclass(frozen=True)
class NearbyProbe:
    window: tuple


@dataclass(frozen=True)
class PendingNearbyHit:
    window: tuple
    deadline: float
    strength_db: float
    entry: dict


SETTINGS = {
    "scan_resume": (True, (False, True)),
    "pause_time": (10, range(31)),
    "resume_time": (2, range(-1, 6)),
    "scan_speed": (8, range(1, MAX_SCAN_SPEED + 1)),
    "scan_follow_scope": (False, (False, True)),
    "ham_full_range": (False, (False, True)),
    "bus_full_range": (False, (False, True)),
    "priority_interval": (5, range(1, 16)),
    "skip_timer": (5, (5, 10, 15)),
    "skip_function": (True, (False, True)),
    "auto_memory_clear": ("ask", ("never", "ask", "always")),
    "scan_screen": (True, (False, True)),
    "scan_dial": ("direction", ("direction", "stop")),
    "tune_step": (12_500, range(1, 1_000_001)),
    "sleep_minutes": (0, range(0, 121, 5)),
    "dmr_squelch": ("any", ("any", "group", "individual")),
    "p25_nac": (None, range(1, 0xFFF)),
    "nxdn_ran": (None, range(64)),
    "rf_gain": (None, range(0, 101)),
    "rf_gain_tenths_db": (None, range(-1000, 1001)),
    "bandwidth_hz": (None, range(50, 241_001)),
    "agc": ("mid", ("off", "fast", "mid", "slow")),
    "audio_peak": (False, (False, True)),
    "noise_blanker": (False, (False, True)),
    "noise_reduction": (False, (False, True)),
    "nb_level": (45, range(0, 101)),
    "nr_level": (7, range(0, 16)),
    "notch": (0, range(0, 10_001)),
    "cw_pitch": (600, range(300, 901)),
    "if_shape": ("sharp", ("soft", "sharp")),
    "pbt1": (0, range(-2500, 2501)),
    "pbt2": (0, range(-2500, 2501)),
    "fsk_baud": (45, (45, 50)),
    "fsk_shift": (170, (170, 200, 425, 800, 850)),
    "fsk_pitch": (1615, (1275, 1500, 1615, 2125)),
    "fsk_usos": (True, (False, True)),
    "fsk_newline": ("any", ("any", "pair")),
    "fsk_center": ("center", ("center", "mark")),
    "fsk_tpf": (False, (False, True)),
    "tone_hpf": (0, range(0, 2001)),
    "tone_lpf": (0, range(0, 5001)),
    "tone_bass": (0, range(-15, 16)),
    "tone_treble": (0, range(-15, 16)),
    "fm_deemphasis": (True, (False, True)),
}
RF_PER_MODE = frozenset({"bandwidth_hz", "agc", "audio_peak", "noise_blanker", "noise_reduction",
                         "nb_level", "nr_level", "notch", "if_shape", "pbt1", "pbt2", "tone_hpf",
                         "tone_lpf", "tone_bass", "tone_treble", "fm_deemphasis"})
FILE_LIMIT = 5 * 1024 * 1024
MAX_ACTION_BYTES = FILE_LIMIT + 1024
AUTO_GROUP = 100
TIMER_DEFAULT = {"enabled": False, "on": None, "off": None, "group": 0, "slot": 0,
                 "weekday": None, "repeat": False}
CLOCK_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def hz(value):
    return integer(value, MANUAL_MIN_HZ, MANUAL_MAX_HZ)


def valid_scan_scope_span(value):
    return integer(value, 1, SCAN_SCOPE_MAX_HZ)


def position(value):
    return integer(value, 0, 99)


def memory_position(group, slot):
    return (position(group) and position(slot)) or (type(group) is int and group == AUTO_GROUP
                                                    and integer(slot, 0, 199))


def valid_bank_name(name):
    return normalize_favorite_name(name) is not None and len(name) <= 24


def valid_bank_names(names):
    return (isinstance(names, dict) and len(names) <= 100
            and all(isinstance(group, str) and re.fullmatch(r"0|[1-9][0-9]?", group)
                    and valid_bank_name(name) and name and name == name.strip()
                    for group, name in names.items()))


def select_tag(value):
    return type(value) is bool or integer(value, 0, 9)


def valid_setting(key, value):
    details = SETTINGS.get(key) if isinstance(key, str) else None
    if details is None:
        return False
    default, choices = details
    if default is None:
        return value is None or type(value) is int and value in choices
    return type(value) is type(default) and value in choices


def valid_member(action, key, check):
    return key not in action or check(action[key])


def valid_profile(profile):
    if not isinstance(profile, dict) or not profile.keys() <= {
        "control_hz", "channel_map", "talkgroups", "hold", "blocked", "allow_list"
    } or not hz(profile.get("control_hz")):
        return False
    channels = profile.get("channel_map")
    groups = profile.get("talkgroups", {})
    blocked = profile.get("blocked", [])
    return (isinstance(channels, dict) and 0 < len(channels) <= 4094
            and all(isinstance(key, str) and key.isascii() and key.isdecimal() and len(key) <= 4
                    and key == str(int(key)) and 1 <= int(key) <= 4094 and hz(value)
                    for key, value in channels.items())
            and profile["control_hz"] in channels.values()
            and isinstance(groups, dict) and len(groups) <= 4094
            and all(isinstance(key, str) and key.isascii() and key.isdecimal() and len(key) <= 8
                    and key == str(int(key)) and 1 <= int(key) <= 0xFFFFFF
                    and (label := normalize_favorite_name(value)) is not None
                    and "," not in label for key, value in groups.items())
            and isinstance(blocked, list) and len(blocked) <= 4094
            and valid_member(profile, "hold", lambda value: value is None or integer(value, 1, 0xFFFFFF) and value not in blocked)
            and all(integer(value, 1, 0xFFFFFF) for value in blocked)
            and valid_member(profile, "allow_list", lambda value: type(value) is bool))


def valid_memory(action):
    operation = action.get("operation")
    fields = {"v10", "operation", "group", "slot"}
    if not fields <= action.keys() or not memory_position(action["group"], action["slot"]):
        return False
    if operation == "write":
        return (action.keys() <= fields | {"hz", "mode", "name", "selected", "skip",
                                           "tone", "color_code", "timeslot", "talkgroup"}
                and hz(action.get("hz")) and action.get("mode") in MODES
                and valid_member(action, "name", lambda v: normalize_favorite_name(v) is not None)
                and valid_member(action, "selected", select_tag)
                and valid_member(action, "skip", lambda v: type(v) is bool)
                and valid_member(action, "tone", lambda v: v is None or normalize_tone_filter(v) is not None)
                and valid_member(action, "color_code", lambda v: v is None or integer(v, 0, 15))
                and valid_member(action, "timeslot", lambda v: v is None or integer(v, 1, 2))
                and valid_member(action, "talkgroup", lambda v: v is None or integer(v, 1, 0xFFFFFF)))
    if operation == "recall":
        return action.keys() == fields
    if operation == "delete":
        if action.keys() == fields:
            return True
        expected = action.get("expected")
        return (action.keys() == fields | {"expected"} and isinstance(expected, dict)
                and not expected.keys() & {"v10", "operation"}
                and expected.get("group") == action["group"] and expected.get("slot") == action["slot"]
                and valid_memory({**expected, "v10": "memory", "operation": "write"}))
    if operation == "select":
        return action.keys() == fields | {"selected"} and select_tag(action["selected"])
    if operation == "skip":
        return action.keys() == fields | {"skip"} and type(action["skip"]) is bool
    return (operation == "name" and action.keys() == fields | {"name"}
            and normalize_favorite_name(action["name"]) is not None)


def valid_edge(action):
    required = {"number", "start_hz", "end_hz", "mode"}
    return (required <= action.keys() <= required | {"v10", "step"}
            and integer(action["number"], 0, 49) and hz(action["start_hz"]) and hz(action["end_hz"])
            and action["start_hz"] < action["end_hz"] and action["mode"] in MODES
            and valid_member(action, "step", lambda v: integer(v, 1, 1_000_000)))


def valid_zone_entry(entry):
    return (isinstance(entry, dict) and entry.keys() == {"hz", "mode", "name"}
            and hz(entry["hz"]) and entry["mode"] in MODES
            and normalize_favorite_name(entry["name"]) is not None)


def valid_zone_overrides(zones):
    if not isinstance(zones, dict) or zones.keys() - ZONE_BANKS.keys():
        return False
    return all(isinstance(zone, dict) and zone.keys() == {"next_index", "changes"}
               and integer(zone["next_index"], ZONE_LIMIT, ZONE_MAX_INDEX + 1)
               and isinstance(zone["changes"], dict) and len(zone["changes"]) <= ZONE_LIMIT
               and all(isinstance(index, str) and re.fullmatch(r"0|[1-9][0-9]{0,9}", index)
                       and int(index) < zone["next_index"]
                       and (entry is None or valid_zone_entry(entry))
                       for index, entry in zone["changes"].items()) for zone in zones.values())


CUSTOM_RANGE_FIELDS = frozenset(("name", "start_hz", "end_hz"))
CUSTOM_NAME_LENGTH = 24


def valid_custom_range(preset):
    return (isinstance(preset, dict) and CUSTOM_RANGE_FIELDS <= preset.keys() <= CUSTOM_RANGE_FIELDS | {"step_hz"}
            and bool(normalize_favorite_name(preset["name"])) and len(preset["name"]) <= CUSTOM_NAME_LENGTH
            and hz(preset["start_hz"]) and hz(preset["end_hz"])
            and preset["start_hz"] < preset["end_hz"]
            and valid_member(preset, "step_hz", lambda value: type(value) is int and value in CUSTOM_STEPS))


def valid_custom_presets(presets):
    return (isinstance(presets, dict) and presets.keys() <= CUSTOM_ZONES
            and all(valid_custom_range(preset) for preset in presets.values()))


def valid_preset_ranges(ranges):
    return (isinstance(ranges, list) and 1 <= len(ranges) <= PRESET_RANGE_LIMIT
            and all(isinstance(preset, dict)
                    and CUSTOM_RANGE_FIELDS <= preset.keys() <= CUSTOM_RANGE_FIELDS | {"step_hz", "mode"}
                    and bool(normalize_favorite_name(preset["name"])) and len(preset["name"]) <= CUSTOM_NAME_LENGTH
                    and hz(preset["start_hz"]) and hz(preset["end_hz"]) and preset["start_hz"] < preset["end_hz"]
                    and valid_member(preset, "step_hz", lambda value: integer(value, 1, 1_000_000))
                    and valid_member(preset, "mode", lambda value: value in VFO_MODES)
                    for preset in ranges))


def valid_preset_range_overrides(presets):
    return (isinstance(presets, dict) and presets.keys() <= PRESET_SCAN_RANGES.keys()
            and all(valid_preset_ranges(ranges) for ranges in presets.values()))


def valid_zone_action(action):
    fields = {"v10", "zone", "operation"}
    zone, operation = action.get("zone"), action.get("operation")
    if not isinstance(zone, str) or zone not in ZONES:
        return False
    if zone in CUSTOM_ZONES:
        return (operation == "configure"
                and valid_custom_range({key: value for key, value in action.items() if key not in fields}))
    if zone == NEARBY_ZONE:
        return (operation == "recall" and action.keys() == fields | {"index"}
                and integer(action.get("index"), 0, NEARBY_MAX_INDEX))
    if zone in PRESET_SCAN_RANGES and operation in ("configure_ranges", "reset_ranges"):
        required = fields | {"ranges"} if operation == "configure_ranges" else fields
        return (required <= action.keys() <= required | {"expected_ranges"}
                and (operation == "reset_ranges" or valid_preset_ranges(action["ranges"]))
                and valid_member(action, "expected_ranges", valid_preset_ranges))
    max_index = 199 if zone == "FAV" else ZONE_MAX_INDEX
    if operation in ("recall", "delete"):
        return action.keys() == fields | {"index"} and integer(action.get("index"), 0, max_index)
    return (operation == "write" and fields | {"hz", "mode"} <= action.keys()
            <= fields | {"index", "hz", "mode", "name"}
            and valid_member(action, "index", lambda value: integer(value, 0, max_index))
            and valid_zone_entry({"hz": action.get("hz"), "mode": action.get("mode"), "name": action.get("name", "")}))


def valid_timer(timer):
    return (isinstance(timer, dict) and timer.keys() == TIMER_DEFAULT.keys()
            and type(timer["enabled"]) is bool and type(timer["repeat"]) is bool
            and all(v is None or isinstance(v, str) and CLOCK_TIME.fullmatch(v) for v in (timer["on"], timer["off"]))
            and (timer["weekday"] is None or integer(timer["weekday"], 0, 6))
            and memory_position(timer["group"], timer["slot"])
            and (timer["off"] is None or timer["on"] is not None and timer["off"] > timer["on"])
            and (not timer["enabled"] or timer["on"] is not None))


def valid_backup(data):
    fields = {"version", "settings", "by_mode", "memories", "edges", "profile", "timer"}
    optional = {"zones", "custom_presets", "preset_ranges", "bank_names"}
    if (not isinstance(data, dict) or not fields <= data.keys() <= fields | optional
            or type(data["version"]) is not int or data["version"] != 1
            or not valid_zone_overrides(data.get("zones", {}))
            or not valid_custom_presets(data.get("custom_presets", {}))
            or not valid_preset_range_overrides(data.get("preset_ranges", {}))
            or not valid_bank_names(data.get("bank_names", {}))):
        return False
    settings, by_mode, memories, edges, profile, timer = (
        data[key] for key in ("settings", "by_mode", "memories", "edges", "profile", "timer")
    )
    if (not isinstance(settings, dict) or not all(valid_setting(key, value) for key, value in settings.items())
            or not isinstance(by_mode, dict)
            or not all(mode in MODES and isinstance(values, dict)
                       and all(key in RF_PER_MODE and valid_setting(key, value) for key, value in values.items())
                       for mode, values in by_mode.items())
            or not isinstance(memories, list) or len(memories) > 2200
            or not isinstance(edges, list) or len(edges) > 50
            or profile is not None and not valid_profile(profile) or not valid_timer(timer)):
        return False
    locations = set()
    for entry in memories:
        if (not isinstance(entry, dict) or entry.keys() & {"v10", "operation"}
                or not valid_memory({"v10": "memory", "operation": "write", **entry})):
            return False
        location = (entry["group"], entry["slot"])
        if location in locations:
            return False
        locations.add(location)
    return (sum(group != AUTO_GROUP for group, _ in locations) <= 2000
            and (not timer["enabled"] or (timer["group"], timer["slot"]) in locations)
            and all(isinstance(edge, dict) and "v10" not in edge and valid_edge(edge) for edge in edges)
            and len({edge["number"] for edge in edges}) == len(edges))


def valid_scope_snap(snap):
    return (isinstance(snap, dict) and snap.keys() == {"sequence", "time_ms", "radius_hz"}
            and integer(snap["sequence"], 1, 2 ** 53 - 1)
            and integer(snap["time_ms"], 0, 2 ** 53 - 1)
            and integer(snap["radius_hz"], 1, 7_500))


def valid_action(action):
    command = action.get("v10")
    if command == "tune":
        fields = {"v10", "hz", "mode"}
        return (fields <= action.keys() <= fields | {"snap"} and hz(action["hz"]) and action["mode"] in VFO_MODES
                and valid_member(action, "snap", valid_scope_snap))
    if command == "nearby":
        return action.keys() == {"v10", "enabled"} and type(action["enabled"]) is bool
    if command == "step":
        return (action.keys() == {"v10", "direction"} and type(action["direction"]) is int
                and action["direction"] in (-1, 1))
    if command == "preset_step":
        return (action.keys() == {"v10", "steps"} and integer(action["steps"], -80, 80)
                and action["steps"] != 0)
    if command == "setting":
        return ({"v10", "key", "value"} <= action.keys() <= {"v10", "key", "value", "mode", "scope_span_hz"}
                and valid_setting(action["key"], action["value"])
                and ("mode" not in action or action["key"] in RF_PER_MODE and action["mode"] in MODES)
                and ("scope_span_hz" not in action or action["key"] == "scan_follow_scope"
                     and action["value"] is True and valid_scan_scope_span(action["scope_span_hz"])))
    if command == "memory":
        return valid_memory(action)
    if command == "bank_name":
        return action.keys() == {"v10", "group", "name"} and position(action["group"]) and valid_bank_name(action["name"])
    if command == "zone":
        return valid_zone_action(action)
    if command == "lockout":
        return (action.keys() == {"v10", "operation"}
                and action["operation"] in ("toggle", "clear-temporary"))
    if command == "edge":
        return valid_edge(action)
    if command == "scan_control":
        fields = {"v10", "operation"}
        operation = action.get("operation")
        if operation == "scope_span":
            return action.keys() == fields | {"scope_span_hz"} and valid_scan_scope_span(action["scope_span_hz"])
        if operation in ("hold", "resume"):
            return action.keys() == fields
        if operation == "inspect":
            return (fields | {"hz"} <= action.keys() <= fields | {"hz", "mode", "snap"} and hz(action["hz"])
                    and valid_member(action, "mode", lambda value: value in VFO_MODES)
                    and valid_member(action, "snap", valid_scope_snap))
        return (action.keys() <= fields | {"enabled"} and operation in ("fine", "jump", "recall")
                and valid_member(action, "enabled", lambda v: type(v) is bool))
    if command == "scan":
        if "scope_span_hz" in action and (action.get("enabled") is not True
                                          or not valid_scan_scope_span(action["scope_span_hz"])):
            return False
        if action.get("kind") == "SIMPLE":
            fields = {"v10", "enabled", "kind"}
            if action.get("enabled") is False:
                return action.keys() == fields
            return (fields <= action.keys() <= fields | {"direction", "step", "scope_span_hz"} and action.get("enabled") is True
                    and valid_member(action, "direction", lambda value: type(value) is int and value in (-1, 1))
                    and valid_member(action, "step", lambda value: integer(value, 1, 1_000_000)))
        if action.get("kind") == "ZONE":
            fields = {"v10", "enabled", "kind"}
            if action.get("enabled") is False:
                return action.keys() == fields
            if action.get("enabled") is not True:
                return False
            if "scope_span_hz" in action:
                fields.add("scope_span_hz")
            if action.keys() == fields | {"zone"}:
                return isinstance(action["zone"], str) and action["zone"] in ZONES
            zones = action.get("zones")
            return (action.keys() == fields | {"zones"} and isinstance(zones, list) and 0 < len(zones) <= len(ZONES)
                    and all(isinstance(zone, str) and zone in ZONES for zone in zones)
                    and len(set(zones)) == len(zones))
        return (action.keys() <= {"v10", "kind", "enabled", "start_hz", "end_hz", "center_hz", "span_hz", "mode",
                                  "group", "edge", "select_tag", "start_slot", "end_slot", "auto_clear", "scope_span_hz"}
                and action.get("kind") in SCAN_KINDS and type(action.get("enabled")) is bool
                and all(valid_member(action, key, hz) for key in ("start_hz", "end_hz", "center_hz"))
                and valid_member(action, "span_hz", lambda v: integer(v, 1, MANUAL_MAX_HZ))
                and valid_member(action, "mode", lambda v: v in MODES)
                and valid_member(action, "group", lambda v: integer(v, 0, AUTO_GROUP))
                and valid_member(action, "edge", lambda v: integer(v, 0, 49))
                and valid_member(action, "select_tag", lambda v: integer(v, 1, 9))
                and all(valid_member(action, key, lambda v: integer(v, 0, 199)) for key in ("start_slot", "end_slot"))
                and valid_member(action, "auto_clear", lambda v: type(v) is bool))
    if command == "frequency_mode":
        return action.keys() == {"v10", "value"} and action["value"] in ("vfo", "memory")
    if command == "priority":
        return (action.keys() <= {"v10", "enabled", "group", "slot"} and type(action.get("enabled")) is bool
                and valid_member(action, "group", lambda v: integer(v, 0, AUTO_GROUP))
                and valid_member(action, "slot", lambda v: integer(v, 0, 199)))
    if command == "trunk":
        return (action.keys() <= {"v10", "operation", "profile"} and action.get("operation") in ("save", "start", "stop")
                and valid_member(action, "profile", valid_profile)
                and ("profile" in action or action["operation"] != "save"))
    if command == "timer":
        if action.keys() == {"v10", "enabled"}:
            return type(action["enabled"]) is bool
        return valid_timer({key: value for key, value in action.items() if key != "v10"})
    if command == "monitor":
        return action.keys() == {"v10", "enabled"} and type(action["enabled"]) is bool
    if command == "skip":
        return action.keys() == {"v10"}
    if command == "restore":
        return action.keys() == {"v10", "backup"} and valid_backup(action["backup"])
    if command == "reset":
        return action.keys() == {"v10", "scope"} and action["scope"] in ("partial", "all")
    return False


class ReceiverPanel:
    """One radio's programming, with scan timing driven by received SDR blocks."""

    def __init__(self, path, legacy, dmr):
        self.path = Path(path)
        self.settings = {key: default for key, (default, _) in SETTINGS.items()}
        self.by_mode = {}
        self.memories = {}
        self.memory_revision = uuid4().hex
        self.edges = {}
        self.zone_defaults = {}
        for zone, banks in ZONE_BANKS.items():
            channels = (channel for bank in banks for channel in legacy.factory.get(bank, ()))
            defaults = {
                index: {"hz": channel.hz, "mode": channel.modulation,
                        "name": f"CH {channel.preset_number}" if channel.preset_number is not None else ""}
                for index, channel in enumerate(channels)
            }
            if not any(bank in legacy.zone_overrides for bank in banks):
                # Keep added factory entries above legacy IDs and below user IDs.
                defaults.update({
                    index: {"hz": channel.hz, "mode": channel.modulation, "name": f"CH {channel.preset_number}"}
                    for index, channel in enumerate(V10_PRESETS.get(zone, ()), CHIRP_ZONE_LIMIT)
                })
            self.zone_defaults[zone] = defaults
        self.zones = {}
        self.custom_presets = {}
        self.preset_ranges = {}
        self.bank_names = {}
        self._preset_plans = {}
        self.nearby = None
        self._nearby = {}
        self._nearby_candidate = None
        self.profile = None
        self.timer = TIMER_DEFAULT.copy()
        self.error = None
        self._load(legacy, dmr)
        self.active = False
        self.frequency_mode = "vfo"
        self.group, self.slot = next(iter(sorted(self.memories)), (0, 0))
        self.vfo = (98_400_000, "WFM")
        self.auto_tune = False
        self.scan = None
        self.preset_point = None
        self.scan_direction = 1
        self.scan_origin = None
        self.paused_at = self.closed_at = self.next_scan_at = 0.0
        self.skipped = {}
        self.temporary_locks = set()
        self.priority = None
        self.priority_due = 0.0
        self.priority_return = None
        self.priority_scan_point = None
        self.priority_check_end = 0.0
        self.trunk_enabled = False
        self.trunk_generation = 0
        self.trunk_frequency = None
        self.trunk_status = {}
        self.trunk_profile = None
        self.trunk_error = None
        self.trunk_audio = False
        self.active_memory = None
        self.voice_mode = "DMR"
        self.digital_status = None
        self.monitor = False
        self.sleep_until = 0.0
        self.timer_minute = None
        self.timer_power_off = None

    def _load(self, legacy, dmr):
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(FILE_LIMIT + 1)
            if len(raw) > FILE_LIMIT:
                raise ValueError("V10 state exceeds size limit")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Incompatible V10 receiver state")
            for key, value in data.get("settings", {}).items():
                if valid_setting(key, value):
                    self.settings[key] = value
            for mode, values in data.get("by_mode", {}).items():
                if mode in MODES and isinstance(values, dict):
                    self.by_mode[mode] = {key: value for key, value in values.items()
                                          if key in RF_PER_MODE and valid_setting(key, value)}
            for entry in data.get("memories", []):
                if isinstance(entry, dict) and valid_memory({"v10": "memory", "operation": "write", **entry}):
                    self.memories[entry["group"], entry["slot"]] = entry
            for entry in data.get("edges", []):
                if isinstance(entry, dict) and valid_edge(entry):
                    self.edges[entry["number"]] = entry
            if valid_profile(data.get("profile")):
                self.profile = data["profile"]
            if valid_timer(data.get("timer")):
                self.timer = data["timer"]
            if valid_zone_overrides(data.get("zones", {})):
                self.zones = data.get("zones", {})
            if valid_custom_presets(data.get("custom_presets", {})):
                self.custom_presets = data.get("custom_presets", {})
            if valid_preset_range_overrides(data.get("preset_ranges", {})):
                self.preset_ranges = data.get("preset_ranges", {})
            if valid_bank_names(data.get("bank_names", {})):
                self.bank_names = data.get("bank_names", {})
            return
        except FileNotFoundError:
            pass
        except (AttributeError, OSError, ValueError, RecursionError, TypeError) as error:
            LOG.warning("Could not load V10 receiver programming: %s", error)
            self.error = "Could not load saved V10 programming"
        for slot, channel in enumerate(legacy.memories["FAV"][:100]):
            if channel:
                name = legacy.favorite_names[slot]
                self.memories[0, slot] = {"group": 0, "slot": slot, "hz": channel.hz,
                                           "mode": channel.modulation, "name": name,
                                           "tone": legacy.favorite_tones.get(slot)}
        for slot, entry in enumerate(list(dmr.entries.values())[:100]):
            self.memories[1, slot] = {
                "group": 1, "slot": slot, "hz": entry["hz"], "mode": "DMR", "name": entry["name"],
                "color_code": entry.get("color_code"), "timeslot": entry.get("slot"),
                "talkgroup": entry.get("talkgroup"),
            }

    def save(self, *, settings=None, by_mode=None, memories=None, edges=None, profile=..., timer=None, zones=None,
             custom_presets=None, preset_ranges=None, bank_names=None):
        settings = self.settings if settings is None else settings
        by_mode = self.by_mode if by_mode is None else by_mode
        memories = self.memories if memories is None else memories
        edges = self.edges if edges is None else edges
        profile = self.profile if profile is ... else profile
        timer = self.timer if timer is None else timer
        zones = self.zones if zones is None else zones
        custom_presets = self.custom_presets if custom_presets is None else custom_presets
        preset_ranges = self.preset_ranges if preset_ranges is None else preset_ranges
        bank_names = self.bank_names if bank_names is None else bank_names
        pending = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                    prefix=self.path.name + ".", delete=False) as stream:
                pending = Path(stream.name)
                json.dump({"version": 1, "settings": settings, "by_mode": by_mode, "memories": list(memories.values()),
                           "edges": list(edges.values()), "profile": profile, "timer": timer, "zones": zones,
                           "custom_presets": custom_presets, "preset_ranges": preset_ranges, "bank_names": bank_names},
                          stream, separators=(",", ":"), ensure_ascii=False)
                stream.flush()
                if stream.buffer.tell() > FILE_LIMIT:
                    raise ValueError("V10 state exceeds the programming size limit")
                os.fsync(stream.fileno())
            pending.replace(self.path)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
        for zone in tuple(self._preset_plans):
            if (zones.get(zone) != self.zones.get(zone)
                    or preset_ranges.get(zone) != self.preset_ranges.get(zone)
                    or custom_presets.get(zone) != self.custom_presets.get(zone)):
                self._preset_plans.pop(zone)
        if memories is not self.memories:
            self.memory_revision = uuid4().hex
        self.settings, self.by_mode, self.memories, self.edges = settings, by_mode, memories, edges
        self.profile, self.timer, self.zones = profile, timer, zones
        self.custom_presets = custom_presets
        self.preset_ranges = preset_ranges
        self.bank_names = bank_names

    def backup(self):
        return deepcopy({"version": 1, "settings": self.settings, "by_mode": self.by_mode,
                         "memories": list(self.memories.values()), "edges": list(self.edges.values()),
                         "profile": self.profile, "timer": self.timer, "zones": self.zones,
                         "custom_presets": self.custom_presets, "preset_ranges": self.preset_ranges,
                         "bank_names": self.bank_names})

    def restore(self, data):
        settings = {key: default for key, (default, _) in SETTINGS.items()}
        settings.update(data["settings"])
        memories = {(entry["group"], entry["slot"]): entry for entry in data["memories"]}
        edges = {entry["number"]: entry for entry in data["edges"]}
        self.save(settings=settings, by_mode=data["by_mode"], memories=memories, edges=edges,
                  profile=data["profile"], timer=data["timer"], zones=data.get("zones", {}),
                  custom_presets=data.get("custom_presets", {}), preset_ranges=data.get("preset_ranges", {}),
                  bank_names=data.get("bank_names", {}))
        self.power_off()
        self.timer_power_off = None
        self.group, self.slot = next(iter(sorted(self.memories)), (0, 0))

    def current_settings(self, mode):
        return {**self.settings, **self.by_mode.get(mode, {})}

    def set_bank_name(self, group, name):
        key = str(group)
        name = normalize_favorite_name(name)
        if name == self.bank_names.get(key, ""):
            return
        names = self.bank_names.copy()
        if name:
            names[key] = name
        else:
            names.pop(key, None)
        self.save(bank_names=names)

    def set_value(self, key, value, mode):
        if key == "rf_gain" and self.settings["rf_gain_tenths_db"] is not None:
            self.save(settings={**self.settings, key: value, "rf_gain_tenths_db": None})
            return
        if value == self.current_settings(mode)[key]:
            return
        if key in RF_PER_MODE:
            self.save(by_mode={**self.by_mode, mode: {**self.by_mode.get(mode, {}), key: value}})
        else:
            self.save(settings={**self.settings, key: value})

    def zone_ranges(self, zone):
        return list(self.preset_ranges.get(zone, PRESET_SCAN_RANGES.get(zone, ())))

    def set_zone_ranges(self, zone, ranges, expected=None):
        if expected is not None and expected != self.zone_ranges(zone):
            raise ValueError("These ranges changed. Reopen the preset and try again.")
        overrides = self.preset_ranges.copy()
        if ranges is None:
            overrides.pop(zone, None)
        else:
            overrides[zone] = [{**preset, "name": normalize_favorite_name(preset["name"])} for preset in ranges]
        if overrides == self.preset_ranges:
            return False
        self.save(preset_ranges=overrides)
        return True

    def _range_plan(self, zone):
        plan = self._preset_plans.get(zone)
        if plan is not None:
            return plan
        if zone in CUSTOM_ZONES:
            preset = self.custom_presets.get(zone)
            if not preset:
                return None
            plan = PresetRangePlan((self._custom_band(zone, preset),))
        else:
            ranges = self.zone_ranges(zone)
            if not ranges:
                return None
            bands = tuple(self._custom_band(zone, preset) for preset in ranges)
            if zone in self.preset_ranges:
                plan = PresetRangePlan(bands)
            else:
                listed = {entry["hz"] for entry in self.zone_entries(zone)
                          if any(band.lower <= entry["hz"] <= band.upper for band in bands)}
                changes = self.zones.get(zone, {}).get("changes", {})
                removed = {entry["hz"] for index in changes if (entry := self.zone_defaults[zone].get(int(index)))}
                plan = PresetRangePlan(bands, tuple(sorted(listed)), frozenset(removed - listed))
        self._preset_plans[zone] = plan
        return plan

    def zone_indices(self, zone):
        if zone in CUSTOM_ZONES:
            return []
        if zone == NEARBY_ZONE:
            return list(reversed(self._nearby))
        if zone == "FAV":
            return sorted(group * 100 + slot for group, slot in self.memories if group < 2)
        indices = set(self.zone_defaults[zone])
        for index, entry in self.zones.get(zone, {}).get("changes", {}).items():
            if entry is None:
                indices.discard(int(index))
            else:
                indices.add(int(index))
        return sorted(indices)

    def zone_entry(self, zone, index):
        if zone in CUSTOM_ZONES:
            plan = self._range_plan(zone)
            return plan.entry(zone, index) if plan else None
        if zone == NEARBY_ZONE:
            return self._nearby.get(index)
        if zone in PRESET_SCAN_RANGES and index is not None and index < 0:
            return self._range_plan(zone).entry(zone, -index - 1)
        if zone == "FAV":
            return self.memories.get(divmod(index, 100))
        changes = self.zones.get(zone, {}).get("changes", {})
        return changes.get(str(index), self.zone_defaults[zone].get(index))

    def zone_entries(self, zone):
        return [{"index": index, "hz": entry["hz"], "mode": entry["mode"], "name": entry.get("name", "")}
                for index in self.zone_indices(zone) if (entry := self.zone_entry(zone, index)) is not None]

    def observe_nearby(self, channel, revision, now, *, name=None):
        """Remember locally received channels without changing programmed zones."""
        if not self.active or not channel or channel[1] not in MODES:
            self._nearby_candidate = None
            return
        candidate = self._nearby_candidate
        key = (channel, revision)
        since = (candidate[1] if candidate and candidate[0] == key
                 and now - candidate[2] <= NEARBY_MAX_GAP else now)
        self._nearby_candidate = (key, since, now)
        if now - since < NEARBY_MIN_SECONDS:
            return
        frequency, mode = channel
        # Stable IDs prevent an open preset menu from recalling a different hit.
        index = frequency * len(MODES) + MODES.index(mode)
        previous = self._nearby.pop(index, {})
        self._nearby[index] = {"hz": frequency, "mode": mode,
                               "name": (normalize_favorite_name(name) if name else None)
                               or previous.get("name", "")}
        if len(self._nearby) > NEARBY_LIMIT:
            del self._nearby[next(iter(self._nearby))]

    def change_zone(self, zone, index, entry):
        """Store sparse edits against the legacy factory list under stable IDs."""
        if zone == NEARBY_ZONE:
            raise ValueError("Nearby presets are receive history")
        old = self.zones.get(zone, {})
        next_index = old.get("next_index", max(ZONE_LIMIT, len(self.zone_defaults[zone])))
        changes = old.get("changes", {}).copy()
        if index is None:
            if next_index > ZONE_MAX_INDEX or len(self.zone_indices(zone)) >= ZONE_LIMIT:
                raise ValueError("This preset zone is full")
            index, next_index = next_index, next_index + 1
        elif self.zone_entry(zone, index) is None:
            raise ValueError("This preset no longer exists")
        if self.zone_defaults[zone].get(index) == entry:
            changes.pop(str(index), None)
        else:
            changes[str(index)] = entry
        if len(changes) > ZONE_LIMIT:
            raise ValueError("This preset zone has reached the editing limit")
        self.save(zones={**self.zones, zone: {"next_index": next_index, "changes": changes}})

    def snapshot(self, power, mode, tuned_hz=None, *, memory_revision=None):
        status = self.trunk_status
        now = monotonic()
        active = self.active_memory
        memory = self.memories.get((active["group"], active["slot"])) if active else None
        memory_locked = memory and memory["hz"] == tuned_hz and memory.get("skip")
        zone_scan = self.scan if power and self.scan and self.scan["kind"] == "ZONE" else None
        zone_point = zone_scan.get("current_point") if zone_scan else None
        return {
            "active": self.active,
            "settings": self.current_settings(mode),
            "settings_by_mode": deepcopy(self.by_mode),
            "memory_revision": self.memory_revision,
            **({"memories": [self.memories[key].copy() for key in sorted(self.memories)]}
               if memory_revision != self.memory_revision else {}),
            "bank_names": self.bank_names.copy(),
            "frequency_mode": self.frequency_mode,
            "group": self.group, "slot": self.slot,
            "vfo": {"hz": self.vfo[0], "mode": self.vfo[1]},
            "auto_mode": power and self.active and self.automatic_mode,
            "scan_kind": self.scan["kind"] if power and self.scan else None,
            "scan_scope_span_hz": self.scan.get("scope_span_hz") if power and self.scan else None,
            "scan_zone": zone_point[0] if zone_point else zone_scan["zones"][0] if zone_scan else None,
            "scan_zones": zone_scan["zones"].copy() if zone_scan else [],
            "custom_presets": deepcopy(self.custom_presets),
            "scan_held": bool(zone_scan and zone_scan["held"]),
            **self.nearby_status(power),
            "current_lockout": bool(power and (memory_locked or self.frequency_locked(tuned_hz, now))),
            "edges": [self.edges[key].copy() for key in sorted(self.edges)],
            "timer": self.timer.copy(),
            "scan_direction": self.scan_direction,
            "scan_fine": bool(self.scan and self.scan.get("fine")),
            "scan_paused": bool(self.paused_at),
            "priority": {"enabled": bool(power and self.priority), "checking": self.priority_return is not None,
                         "group": self.priority[0] if self.priority else self.group,
                         "slot": self.priority[1] if self.priority else self.slot},
            "trunk": {"profile": deepcopy(self.trunk_profile or self.profile), "enabled": power and self.trunk_enabled,
                      "phase": status.get("phase", "control" if self.trunk_enabled else "off"),
                      "cc_freq_hz": status.get("cc_freq_hz"), "vc_freq_hz": status.get("vc_freq_hz"),
                      "error": self.trunk_error},
            "monitor": power and self.monitor, "error": self.error,
            "capabilities": {"modes": list(MODES), "min_hz": MANUAL_MIN_HZ,
                             "max_hz": MANUAL_MAX_HZ, "auto_mode": True,
                             "rf": ["rf_gain", "rf_gain_tenths_db", "cw_pitch", *sorted(RF_PER_MODE)]},
        }

    def stop_scan(self):
        if self.scan:
            self.preset_point = self.preset_position()
        self.scan = None
        self.paused_at = self.closed_at = 0.0

    def start_scan(self, action, current, now):
        kind = action["kind"]
        direction = action.get("direction", self.scan_direction)
        scope_span_hz = action.get("scope_span_hz")
        if kind == "ZONE":
            zones = action["zones"] if "zones" in action else [action["zone"]]
            return self._start_zone_scan(zones, current, now, scope_span_hz=scope_span_hz)
        if kind in ("MEMO", "SEL", "MODE"):
            group = action.get("group")
            mode = action.get("mode", current[1])
            select_tag = action.get("select_tag")
            lower_slot = action.get("start_slot", 0)
            upper_slot = action.get("end_slot", 199)
            channels = sorted(key for key, entry in self.memories.items()
                              if (group is None or key[0] == group) and not entry.get("skip")
                              and lower_slot <= key[1] <= upper_slot
                              and (kind != "SEL" or (entry.get("selected") if select_tag is None
                                                     else entry.get("selected") == select_tag))
                              and (kind != "MODE" or entry["mode"] == mode))
            if len(channels) < 2:
                raise ValueError("At least two eligible memories are needed for this scan")
            anchor = (self.group, self.slot)
            index = bisect_right(channels, anchor) - 1 if self.scan_direction > 0 else bisect_left(channels, anchor)
            scan = {"kind": kind, "points": channels, "index": index}
        else:
            lower, upper = MANUAL_MIN_HZ, MANUAL_MAX_HZ
            step = self.settings["tune_step"]
            mode = action.get("mode", current[1])
            anchor = None
            if kind == "SIMPLE":
                anchor = current[0]
                step = action.get("step", step)
            elif kind == "DELTA":
                center, span = action.get("center_hz", current[0]), action.get("span_hz", 100_000)
                lower, upper = max(lower, center - span), min(upper, center + span)
                if step > span:
                    raise ValueError("The tuning step must fit inside the delta-frequency scan span")
                anchor = center
            elif "edge" in action:
                edge = self.edges.get(action["edge"])
                if not edge:
                    raise ValueError("Store the scan edges first")
                lower, upper, mode = edge["start_hz"], edge["end_hz"], edge["mode"]
                step = edge.get("step", step)
            else:
                lower, upper = action.get("start_hz", lower), action.get("end_hz", upper)
            if lower >= upper:
                raise ValueError("Scan end must be above the start")
            points = range(lower, upper + 1, step) if anchor is None else range(
                anchor - ((anchor - lower) // step) * step, upper + 1, step)
            if len(points) < 2:
                raise ValueError("The tuning step is wider than the scan edges")
            index = points.index(anchor) - direction if anchor is not None else -1 if direction > 0 else 0
            scan = {"kind": kind, "points": points, "index": index, "mode": mode, "fine": False}
            if kind == "SIMPLE" and self.auto_tune:
                scan["automatic"] = True
                scan["entry_hz"] = anchor
        if kind == "AUTO" and action.get("auto_clear", self.settings["auto_memory_clear"] == "always"):
            self.save(memories={key: entry for key, entry in self.memories.items() if key[0] != AUTO_GROUP})
        self.stop_scan()
        self.scan = scan
        self.set_scan_scope_span(scope_span_hz)
        self.scan_direction = direction
        self.scan_origin = current
        self.next_scan_at = now
        return self.next_scan(now)

    def _zone_scan_points(self, zone):
        indices = self.zone_indices(zone)
        points = [(zone, index) for index in indices]
        if zone == "BCAST":
            return sorted(points, key=lambda point: self.zone_entry(*point)["hz"])
        return points

    def _start_zone_scan(self, zones, current, now, *, refreshing=False, scope_span_hz=None):
        if any(zone in CUSTOM_ZONES and zone not in self.custom_presets for zone in zones):
            raise ValueError("Set the frequency range for the custom preset first")
        plans = {zone: plan for zone in zones if (plan := self._range_plan(zone)) is not None}
        channels = [self._zone_scan_points(zone) for zone in zones if zone not in plans]
        # Give each selected zone a turn before continuing a long channel list.
        points = []
        longest_list = max(map(len, channels), default=0)
        for offset in range(0, max(longest_list, 1), PRESET_SCAN_BATCH):
            points.extend((zone, -1 - offset) for zone in plans)
            for channel_list in channels:
                points.extend(channel_list[offset:offset + PRESET_SCAN_BATCH])
        if not plans and not refreshing and not any(
                (entry := self.zone_entry(*point)) and not entry.get("skip") for point in points):
            raise ValueError("The selected zones have no eligible presets")
        if not points:
            self.stop_scan()
            return None
        favorite_memories = self.memories if "FAV" in zones else None
        if self.scan and self.scan["kind"] == "ZONE":
            scan = self.scan
            self.set_scan_scope_span(scope_span_hz)
            current_point = scan.get("current_point")
            previous_points, previous_index = scan["points"], scan["index"]
            previous_plans = scan["range_plans"]
            scan.update(points=points, zones=zones.copy(), favorite_memories=favorite_memories, range_plans=plans)
            scan["range_freqs"] = {zone: frequency for zone, frequency in scan["range_freqs"].items() if zone in plans}
            if current_point in points:
                scan["index"] = points.index(current_point)
                zone = current_point[0]
                if refreshing and zone == "FAV":
                    favorite = self.zone_entry(*current_point)
                    if favorite and not favorite.get("skip") and not self.frequency_locked(favorite["hz"], now):
                        if self.active_memory is not None:
                            tuning = ("hz", "mode", "tone", "color_code", "timeslot", "talkgroup")
                            if any(favorite.get(key) != self.active_memory.get(key) for key in tuning):
                                self._measure_zone_entry(favorite, now)
                                return favorite
                            self.active_memory = favorite.copy()
                        return None
                elif zone not in plans:
                    return None
                elif (entry := plans[zone].entry(zone, scan["range_freqs"].get(zone))) and not self.frequency_locked(
                        entry["hz"], now):
                    if previous_plans.get(zone) is plans[zone]:
                        return None
                    self._measure_zone_entry(entry, now)
                    return entry
                scan["index"] -= self.scan_direction
            else:
                # Continue with the next preset that survived the change of zones.
                remaining = {point: index for index, point in enumerate(points)}
                for step in range(1, len(previous_points) + 1):
                    point = previous_points[(previous_index + step * self.scan_direction) % len(previous_points)]
                    if point in remaining:
                        scan["index"] = remaining[point] - self.scan_direction
                        break
                else:
                    scan["index"] = -1 if self.scan_direction > 0 else 0
            scan.pop("current_point", None)
        else:
            self.stop_scan()
            index = -1 if self.scan_direction > 0 else 0
            frequencies = {"BCAST": current[0]} if zones == ["BCAST"] and auto_band_mode(current[0]) == "WFM" else {}
            self.scan = {"kind": "ZONE", "zones": zones.copy(), "points": points, "held": False,
                         "index": index, "range_freqs": frequencies, "range_plans": plans,
                         "favorite_memories": favorite_memories}
            self.set_scan_scope_span(scope_span_hz)
            self.scan_origin = current
        return self.next_scan(now)

    def refresh_favorite_scan(self, current, now):
        scan = self.scan
        if (not scan or scan["kind"] != "ZONE" or "FAV" not in scan["zones"] or scan["held"]
                or self.priority_return is not None or self.nearby_current
                or scan.get("favorite_memories") is self.memories):
            return None
        return self._start_zone_scan(scan["zones"], current, now, refreshing=True)

    def hold_zone_scan(self):
        self.scan["held"] = True

    def preset_position(self):
        """Identify the preset in use by the current scan or manual recall."""
        if self.scan is None:
            return self.preset_point
        if self.scan["kind"] != "ZONE" or not (point := self.scan.get("current_point")):
            return None
        zone = point[0]
        if zone in self.scan["range_plans"]:
            frequency = self.scan["range_freqs"].get(zone)
            if frequency is None:
                return None
            return zone, frequency if zone in CUSTOM_ZONES else -frequency - 1
        return point

    def step_preset(self, steps, now):
        """Move within one zone in its programmed order and hold an active scan."""
        position = self.preset_position()
        if position is None:
            return None
        zone, channel = position
        scan = self.scan
        if zone in CUSTOM_ZONES or zone in PRESET_SCAN_RANGES and channel < 0:
            plan = self._range_plan(zone)
            if plan is None:
                return None
            direction = 1 if steps > 0 else -1
            frequency = channel if zone in CUSTOM_ZONES else -channel - 1
            blocked = self.temporary_locks | {hz for hz, expires in self.skipped.items() if now < expires}
            for _ in range(abs(steps)):
                frequency = plan.next_unlocked(frequency, direction, blocked)
                if frequency is None:
                    return None
            entry = plan.entry(zone, frequency)
            if scan:
                scan["range_freqs"][zone] = frequency
            else:
                self.preset_point = zone, frequency if zone in CUSTOM_ZONES else -frequency - 1
        else:
            manual_favorites = zone == "FAV" and scan is None
            if manual_favorites and (group := channel // 100) >= 2:
                points = [(zone, group * 100 + slot) for bank, slot in sorted(self.memories) if bank == group]
            else:
                points = scan["points"] if scan else self._zone_scan_points(zone)
            eligible = []
            anchor = -1
            for rank, point in enumerate(points):
                if point[0] != zone:
                    continue
                if point == position:
                    anchor = rank
                entry = self.zone_entry(*point)
                if not entry:
                    continue
                if not manual_favorites and (entry.get("skip") or self.frequency_locked(entry["hz"], now)):
                    continue
                eligible.append((rank, entry))
            if not eligible:
                return None
            ranks = [rank for rank, _ in eligible]
            if steps > 0:
                offset = bisect_right(ranks, anchor) + steps - 1
            else:
                offset = bisect_left(ranks, anchor) + steps
            rank, entry = eligible[offset % len(eligible)]
            if scan:
                scan["index"] = rank
                scan["current_point"] = points[rank]
            else:
                self.preset_point = points[rank]
        if scan:
            self._measure_zone_entry(entry, now)
            self.hold_zone_scan()
        return entry

    def enable_nearby(self, current, now):
        if self.nearby is None:
            self.nearby = {"windows": nearby_windows(current), "step": 0, "ready_at": now, "live_ready_at": now}

    def nearby_status(self, power):
        enabled = bool(power and self.nearby)
        return {
            "nearby_enabled": enabled,
            "nearby_sweeping": enabled and self.nearby_sweeping,
            "nearby_receiving": enabled and self.nearby_current,
            "nearby_held": enabled and bool(self.nearby.get("held")),
        }

    @property
    def nearby_current(self):
        return bool(self.nearby and self.nearby.get("hit"))

    @property
    def automatic_mode(self):
        return self.priority_return is None and (self.auto_tune or self.nearby_current)

    @property
    def nearby_sweeping(self):
        return bool(self.nearby and not self.nearby_current and not self.trunk_enabled)

    def nearby_auto_detect(self, frequency):
        entry = self.nearby.get("hit") if self.nearby_current else None
        return bool(entry and entry["hz"] == frequency and entry["auto_detect"])

    def classify_nearby(self, mode):
        self.nearby["hit"]["mode"] = mode

    def invalidate_nearby_probe(self):
        if self.nearby:
            self.nearby.pop("job", None)
            self.nearby.pop("live_job", None)
            self.nearby.pop("candidate", None)

    def reject_nearby_candidate(self, frequency, now):
        if self.nearby:
            self.nearby.setdefault("unconfirmed", {})[frequency] = now + NEARBY_RETRY_SECONDS

    def next_nearby_live_probe(self, now, current):
        if not self.nearby_sweeping or self.nearby.get("live_job") or now < self.nearby["live_ready_at"]:
            return None
        local = nearby_origin_window(current)
        if local is None:
            return None
        band, _, _, exclude = local
        job, blocked = self._begin_nearby_probe("live_job", ("live", current[0]), now, current, band, exclude)
        return job, None, band, band.lower, band.upper, (), blocked

    def next_nearby_probe(self, now, current):
        if not self.nearby_sweeping or self.nearby.get("job") or now < self.nearby["ready_at"]:
            return None
        watch = self.nearby
        step = watch["step"]
        watch["step"] += 1
        if step % 4 == 0:
            band, lower, upper, exclude = NEARBY_PMR, PMR[0], PMR[-1], None
            center = (lower + upper) // 2 + 180_000
            exact = PMR
        else:
            wide_step = step - step // 4 - 1
            pass_number, window = divmod(wide_step, len(watch["windows"]))
            band, lower, upper, exclude = watch["windows"][window]
            center = (lower + upper) // 2 + (60_000 if pass_number % 2 == 0 else -60_000)
            if lower < 25_000_000 <= center:
                center = (lower + upper) // 2 - 60_000
            elif lower >= 25_000_000 > center:
                center = (lower + upper) // 2 + 60_000
            center = max(MANUAL_MIN_HZ, min(center, MANUAL_MAX_HZ))
            exact = ()
        job, blocked = self._begin_nearby_probe("job", (center, lower, upper), now, current, band, exclude)
        return job, center, band, lower, upper, exact, blocked

    def _begin_nearby_probe(self, job_key, window, now, current, band, exclude):
        job = NearbyProbe(window)
        self.nearby[job_key] = job
        blocked = self.temporary_locks | {hz for hz, expires in self.skipped.items() if expires > now}
        blocked |= {band.nearest(hz, band.step) for hz in blocked if band.lower <= hz <= band.upper}
        if unconfirmed := self.nearby.get("unconfirmed"):
            self.nearby["unconfirmed"] = {hz: until for hz, until in unconfirmed.items() if until > now}
            blocked.update(self.nearby["unconfirmed"])
        guard = NEARBY_OFFSET_LIMITS[band.modulation]
        for frequency in (current[0], exclude):
            if frequency is not None and band.lower <= frequency <= band.upper:
                blocked.add(band.nearest(frequency, band.step))
                # An off-grid foreground can appear in two adjacent FFT candidates.
                for edge in (frequency - guard, frequency + guard):
                    neighbor = band.nearest(edge, band.step)
                    if abs(neighbor - frequency) < guard:
                        blocked.add(neighbor)
        return job, blocked

    def _nearby_hit_eligible(self, frequency, current, now):
        return (frequency != current[0] and auto_band_mode(frequency) != "WFM"
                and not self.frequency_locked(frequency, now)
                and self.nearby.get("unconfirmed", {}).get(frequency, 0) <= now)

    def _pending_nearby_hit(self, now, current):
        if not self.nearby_sweeping:
            return None
        candidate = self.nearby.get("candidate")
        if candidate and not self._nearby_hit_eligible(candidate.entry["hz"], current, now):
            self.nearby.pop("candidate")
            return None
        return candidate

    def take_nearby_candidate(self, now, current):
        """Take a ranked hit when its short comparison period expires."""
        candidate = self._pending_nearby_hit(now, current)
        if candidate and now >= candidate.deadline:
            return self.nearby.pop("candidate").entry
        return None

    def finish_nearby_probe(self, job, peak, mode, now, *, current, listening, live=False):
        watch = self.nearby
        job_key = "live_job" if live else "job"
        if not watch or watch.get(job_key) is not job:
            return None
        watch.pop(job_key)
        if live:
            watch["live_ready_at"] = now + NEARBY_LIVE_INTERVAL
        else:
            interval = NEARBY_LISTENING_INTERVAL if listening else NEARBY_IDLE_INTERVAL
            watch["ready_at"] = now + interval
        if not self.nearby_sweeping:
            return None
        if peak is None or not self._nearby_hit_eligible(peak.hz, current, now):
            return self.take_nearby_candidate(now, current)
        frequency = peak.hz
        candidate = self._pending_nearby_hit(now, current)
        compared = candidate and candidate.window != job.window and candidate.entry["hz"] != frequency
        if candidate is None or peak.strength_db > candidate.strength_db:
            entry = self._nearby_entry(frequency, mode)
            if candidate is None:
                watch["candidate"] = PendingNearbyHit(job.window, now + NEARBY_COMPARE_SECONDS, peak.strength_db, entry)
            else:
                watch["candidate"] = PendingNearbyHit(candidate.window, candidate.deadline, peak.strength_db, entry)
        if compared:
            return watch.pop("candidate").entry
        return self.take_nearby_candidate(now, current)

    @staticmethod
    def _nearby_entry(frequency, mode):
        pmr = NEARBY_PMR.lower - 6_250 <= frequency <= NEARBY_PMR.upper + 6_250
        known_mode = "FM" if pmr else auto_band_mode(frequency)
        mode = known_mode or mode
        return {"hz": frequency, "mode": mode,
                "auto_detect": known_mode is None and mode in ("AM", "FM")}

    def resume_zone_scan(self, now):
        scan = self.scan
        scan["held"] = False
        point = scan.get("current_point")
        if point and (plan := scan["range_plans"].get(point[0])):
            entry = plan.entry(point[0], scan["range_freqs"].get(point[0]))
        else:
            entry = self.zone_entry(*point) if point else None
        if not entry or entry.get("skip") or self.frequency_locked(entry["hz"], now):
            return self.next_scan(now)
        self._measure_zone_entry(entry, now)
        return entry

    def _measure_zone_entry(self, entry, now):
        self.scan.pop("waiting", None)
        self.scan.update(sample_at=None, carrier_at=None, mode=entry["mode"])
        self.next_scan_at = now
        self.paused_at = self.closed_at = 0.0

    def set_scan_scope_span(self, scope_span_hz):
        if self.scan is not None and scope_span_hz is not None:
            self.scan["scope_span_hz"] = scope_span_hz

    def _quiet_scan_dwell(self):
        speed = self.settings["scan_speed"]
        span = self.scan.get("scope_span_hz", 0)
        if self.settings["scan_follow_scope"] and span > SCAN_SCOPE_BASE_HZ:
            fraction = (span - SCAN_SCOPE_BASE_HZ) / (SCAN_SCOPE_MAX_HZ - SCAN_SCOPE_BASE_HZ)
            speed = min(MAX_SCAN_SPEED, speed * (1 + fraction))
        return 1 / speed

    def _scan_hold_time(self, entry):
        mode = entry["mode"]
        if mode in VOICE_MODES:
            return DIGITAL_SCAN_ACQUISITION_SECONDS
        dwell = self._quiet_scan_dwell()
        if mode == "WFM":
            # Preserve the manual rate if it is already faster than WFM acquisition.
            return max(dwell, min(WFM_SCAN_ACQUISITION_SECONDS, 1 / self.settings["scan_speed"]))
        return dwell

    @staticmethod
    def _custom_band(zone, preset):
        low, high = preset["start_hz"], preset["end_hz"]
        step = preset.get("step_hz")
        if step is None:
            if 117_975_000 <= low <= high < 137_000_000:
                step = AIR_STEP
            elif 87_500_000 <= low <= high < 108_000_000:
                step = 100_000
            else:
                step = 1_000 if high < 30_000_000 else 6_250
        return Band(zone, low, high, step, preset.get("mode", "AUTO"))

    def _next_range_entry(self, zone, plan, blocked):
        frequencies = self.scan["range_freqs"]
        frequency = plan.next_unlocked(frequencies.get(zone), self.scan_direction, blocked)
        if frequency is None:
            return None
        frequencies[zone] = frequency
        return plan.entry(zone, frequency)

    def next_scan(self, now):
        scan = self.scan
        if scan is None:
            return None
        points = scan["points"]
        self.skipped = {frequency: until for frequency, until in self.skipped.items() if now < until}
        blocked = self.skipped.keys() | self.temporary_locks
        zone_scan = scan["kind"] == "ZONE"
        attempts = len(points) if zone_scan else min(len(points), len(blocked) + len(self.memories) + 1)
        for _ in range(attempts):
            scan["index"] = (scan["index"] + self.scan_direction) % len(points)
            point = points[scan["index"]]
            if zone_scan:
                plan = scan["range_plans"].get(point[0])
                entry = self._next_range_entry(point[0], plan, blocked) if plan else self.zone_entry(*point)
            elif isinstance(point, tuple):
                entry = self.memories.get(point)
            elif scan.get("automatic"):
                mode = scan["mode"] if point == scan["entry_hz"] else auto_band_mode(point) or "FM"
                entry = {"hz": point, "mode": mode, "automatic": True}
            else:
                entry = {"hz": point, "mode": scan["mode"]}
            if entry and not entry.get("skip") and entry["hz"] not in blocked:
                if zone_scan:
                    scan["current_point"] = point
                    self._measure_zone_entry(entry, now)
                    return entry
                scan.pop("waiting", None)
                self.next_scan_at = now + self._scan_hold_time(entry)
                self.paused_at = self.closed_at = 0.0
                scan.pop("fine_hz", None)
                return entry
        scan["waiting"] = True
        self.next_scan_at = now + 0.25
        self.paused_at = self.closed_at = 0.0
        if zone_scan:
            scan.pop("current_point", None)
        return None

    def frequency_locked(self, frequency, now):
        return frequency in self.temporary_locks or self.skipped.get(frequency, 0) > now

    def clear_temporary_lockouts(self):
        self.skipped.clear()
        self.temporary_locks.clear()

    def temporary_skip(self, frequency, now):
        self.skipped = {hz: until for hz, until in self.skipped.items() if hz != frequency and until > now}
        if len(self.skipped) == 5:
            self.skipped.pop(next(iter(self.skipped)))
        self.skipped[frequency] = now + 60 * self.settings["skip_timer"]
        return self.next_scan(now)

    def temporary_lockout(self, frequency, now):
        if self.scan:
            self.temporary_locks.add(frequency)
            return self.next_scan(now)
        if frequency in self.temporary_locks:
            self.temporary_locks.remove(frequency)
        else:
            self.temporary_locks.add(frequency)

    @property
    def scan_waiting(self):
        return bool(self.scan and self.scan.get("waiting") and not self.scan.get("held") and self.priority_return is None
                    and not self.nearby_current)

    @property
    def wfm_preset_scanning(self):
        return bool(self.scan and self.scan["kind"] == "ZONE" and self.scan.get("mode") == "WFM" and not self.scan["held"]
                    and self.priority_return is None and not self.nearby_current)

    def scan_tick(self, now, receiving, *, digital_carrier=None):
        scan = self.scan
        if scan is None:
            return None
        if self.scan_waiting:
            return self.next_scan(now) if now >= self.next_scan_at else None
        if scan["kind"] == "ZONE":
            return self._zone_scan_tick(now, receiving, digital_carrier)
        if scan.get("fine"):
            if receiving:
                if now < self.next_scan_at:
                    return None
                points = scan["points"]
                current = scan.get("fine_hz", points[scan["index"]])
                frequency = current + self.scan_direction * 10
                if points[0] <= frequency <= points[-1] and not self.frequency_locked(frequency, now):
                    scan["index"] = (max(0, bisect_right(points, frequency) - 1) if self.scan_direction > 0
                                     else min(len(points) - 1, bisect_left(points, frequency)))
                    scan["fine_hz"] = frequency
                    self.next_scan_at = now + .22
                    return {"hz": frequency, "mode": scan["mode"]}
            if now >= self.next_scan_at:
                return self.next_scan(now)
            return None
        if receiving:
            self.closed_at = 0.0
            if not self.paused_at:
                self.paused_at = now
                if not self.settings["scan_resume"] and scan["kind"] != "AUTO":
                    self.stop_scan()
                    return None
            pause = self.settings["pause_time"]
            if pause == 0 or now < self.paused_at + pause:
                return None
        elif self.paused_at:
            resume = self.settings["resume_time"]
            pause = self.settings["pause_time"]
            if resume < 0 and (pause == 0 or now < self.paused_at + pause):
                return None
            self.closed_at = self.closed_at or now
            if now < self.closed_at + resume:
                return None
        if now >= self.next_scan_at:
            return self.next_scan(now)
        return None

    def _zone_scan_tick(self, now, receiving, digital_carrier):
        scan = self.scan
        if scan["held"]:
            return None
        if not scan.get("current_point"):
            return self.next_scan(now) if now >= self.next_scan_at else None
        if scan["sample_at"] is None:
            scan["sample_at"] = now
        if digital_carrier is True and scan["carrier_at"] is None:
            scan["carrier_at"] = now
        if receiving:
            self.paused_at = self.paused_at or now
            self.closed_at = 0.0
            return None
        if self.paused_at:
            self.closed_at = self.closed_at or now
            resume = self.settings["resume_time"]
            if now < self.closed_at + resume:
                return None
        if len(scan["points"]) == 1 and scan["current_point"][0] not in scan["range_plans"]:
            self.paused_at = self.closed_at = 0.0
            return None
        start = scan["sample_at"]
        dwell = self._quiet_scan_dwell()
        if scan["mode"] == "WFM":
            dwell = max(dwell, WFM_SCAN_ACQUISITION_SECONDS)
        elif scan["mode"] in VOICE_MODES:
            if scan["carrier_at"] is not None or digital_carrier is None:
                start = scan["carrier_at"] if scan["carrier_at"] is not None else start
                dwell += DIGITAL_SCAN_ACQUISITION_SECONDS
            else:
                dwell += DIGITAL_SCAN_PROBE_SECONDS
        if now >= start + dwell:
            return self.next_scan(now)
        return None

    def start_trunk(self, profile=None):
        profile = self.profile if profile is None else profile
        if profile is None:
            raise ValueError("Save a Tier III site and its channel map first")
        self.stop_scan()
        self.preset_point = None
        self.priority = self.priority_return = None
        self.trunk_profile = profile
        self.trunk_frequency = profile["control_hz"]
        self.trunk_generation += 1
        self.trunk_enabled = True
        self.trunk_status = {}
        self.trunk_error = None
        self.trunk_audio = False

    def stop_trunk(self):
        if self.trunk_enabled:
            self.trunk_enabled = False
            self.trunk_profile = None
            self.trunk_generation += 1
            self.trunk_status = {}
            self.trunk_error = None
            self.trunk_audio = False

    def power_off(self):
        self.nearby = None
        self.stop_scan()
        self.preset_point = None
        self.stop_trunk()
        self.priority = self.priority_return = None
        self.monitor = False
        self.auto_tune = False
        self.sleep_until = 0.0
        self.clear_temporary_lockouts()

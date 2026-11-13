"""Front-panel logic independent of the USB tuner and browser."""

import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from pathlib import Path
from time import localtime, monotonic
from unicodedata import category

from bandplan import (
    AIR_INDEX, AIR_STEP, AIR_STEPS, ANALOGUE_MODES, BAND_ORDER, BAND_SEARCH_RANGES, BANK_KEYS, BANKS, BANDS,
    CLOSE_GROUPS, FULL_LOCKOUT, GENERAL_STEPS, GERMAN_BANDS, SIDEBANDS, SLOTS, WRITABLE, Channel,
    band_search_frequencies, band_search_mode, factory_banks,
)
from dab import BAND_III, normalize_service_id, normalize_station_ids
from tone_search import normalize_tone_filter, tone_matches

KEYS = {*BANK_KEYS, "hold", "up", "down", "mute", "close", "scan", "search", "lock", "band", "prog",
        "sideband"}
KEY_ALIASES = {"frn": "pmr", "emg": "fav"}
STEP_LABELS = {
    0: "Auto", 100: "0.1", 500: "0.5", 1_000: "1.0", 2_500: "2.5",
    5_000: "5.0", 6_250: "6.25", AIR_STEP: "8.33",
    10_000: "10.0", 12_500: "12.5",
}
PAIR_STATES = ((), (0, 1), (0,), (1,))
FREQUENCY_SKIP_LIMIT = 100
REPLY_SECONDS = 2.0
TONE_ACQUIRE_SECONDS = .75
FINE_STEP_HZ = 100
MANUAL_MIN_HZ = 100_000
MANUAL_MAX_HZ = 1_750_000_000
FULL_SEARCH_STEP_HZ = 12_500
SIDEBAND_SEARCH_STEP_HZ = 1_000
SIDEBAND_STEPS = (100, 500, SIDEBAND_SEARCH_STEP_HZ, 2_500)
FAVORITE_COUNT = 24
FAVORITE_SLOTS = FAVORITE_COUNT + SLOTS
FAVORITE_NAME_LIMIT = 80
FAVORITE_EDIT_SECONDS = 5
DAB_FREQUENCIES = dict(BAND_III)
CHIRP_ZONES = tuple(bank for bank in BANKS if bank not in ("PVT", "FAV"))
CHIRP_ZONE_LIMIT = 8192
CHIRP_SETTINGS = ("plan", "general_step", "air_step", "rds_enabled", "clock_offset",
                  "program_locked", "close_groups", "close_alert")


class ChirpConflict(ValueError):
    """A clone cannot be applied to the scanner's current configuration."""


def normalize_favorite_name(name):
    if not isinstance(name, str) or len(name) > FAVORITE_NAME_LIMIT:
        return None
    name = name.strip()
    if any(category(character) in ("Cc", "Cs", "Zl", "Zp") for character in name):
        return None
    return name


def _chirp_frequency_mode(entry):
    hz, mode = entry["hz"], entry["mode"]
    if type(hz) is not int or not MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ or mode not in ANALOGUE_MODES:
        raise ValueError("Invalid frequency or modulation")
    return hz, mode


def _chirp_zone(bank, entries):
    if not isinstance(entries, list) or len(entries) > CHIRP_ZONE_LIMIT:
        raise ValueError(f"Invalid {bank} zone size")
    channels = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.keys() != {"hz", "mode", "number"}:
            raise ValueError(f"Invalid {bank} zone channel")
        hz, mode = _chirp_frequency_mode(entry)
        number = entry["number"]
        if number is not None and (type(number) is not int or not 1 <= number <= 9999):
            raise ValueError(f"Invalid {bank} preset number")
        channels.append(Channel(hz, mode, bank, preset_number=number))
    if len(channels) != len(set(channels)):
        raise ValueError(f"{bank} contains duplicate channels")
    return tuple(channels)


def _chirp_settings(changes):
    if not isinstance(changes, dict) or changes.keys() - set(CHIRP_SETTINGS):
        raise ValueError("Invalid scanner settings")
    choices = {"plan": (1, 2), "general_step": GENERAL_STEPS, "air_step": AIR_STEPS}
    for key, value in changes.items():
        if key in choices:
            valid = type(value) is int and value in choices[key]
        elif key == "clock_offset":
            valid = type(value) is int and abs(value) < 1440
        elif key == "close_groups":
            valid = (isinstance(value, list) and len(value) == len(CLOSE_GROUPS)
                     and all(type(group) is bool for group in value) and any(value))
        else:
            valid = type(value) is bool
        if not valid:
            raise ValueError(f"Invalid value for {key}")
    return changes.copy()


def _normalize_dab_station(station):
    if not isinstance(station, (list, tuple)) or len(station) != 2:
        return None
    block, sid = station
    if not isinstance(block, str) or block not in DAB_FREQUENCIES:
        return None
    service_id = normalize_service_id(sid)
    return (block, service_id) if service_id else None


def _normalize_dab_blocks(blocks, station):
    known = {block for block in blocks if isinstance(block, str) and block in DAB_FREQUENCIES}
    if station:
        known.add(station[0])
    return tuple(block for block in DAB_FREQUENCIES if block in known)


def _normalize_dab_service_counts(counts, blocks):
    if not isinstance(counts, dict):
        return {}
    return {block: count for block in blocks
            if type(count := counts.get(block)) is int and 0 < count <= 65535}


def _normalize_dab_favorite(block, sid, hz, name):
    station = _normalize_dab_station((block, sid))
    if station is None or type(hz) is not int or DAB_FREQUENCIES[block] != hz:
        return None
    return {"block": block, "sid": station[1], "hz": hz,
            "name": normalize_favorite_name(name) or ""}


def format_frequency(hz):
    # Truncate to the LCD's five available fractional places, including 8⅓ kHz air steps.
    if hz == 0:
        return "000.0000"
    whole, fractional = divmod(hz, 1_000_000)
    digits = f"{fractional // 10:05d}".rstrip("0").ljust(4, "0")
    return f"{whole}.{digits}"


class Scanner:
    """One physical panel's state, with persistent slots and temporary tuning state."""

    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)
        self._load()
        self.volume = 0.0
        self.squelch = 0.5
        self.muted = False
        self.activity = "scan"
        self.full_search = False
        self.band_search = None
        self.band_search_range = None
        self.search_frequencies = ()
        self.sideband = None
        self.sideband_step = SIDEBAND_SEARCH_STEP_HZ
        self.hold = False
        self.direct_frequency = False
        self.enabled = set()
        self.selected_scan_slots = None
        self.favorite_edit_mode = None
        self.favorite_edit_until = 0
        self.scan_list = []
        self.scan_cursor = -1
        self.scan_direction = 1
        self.search_cursor = 0
        self.direction = 1
        self.current = None
        self.last_held = None
        self.signal_open = False
        self.audio_open = False
        self.waiting_reply = None
        self.received_signal = False
        self.tune_revision = 0
        self.tone_deadline = None
        self.tone_matched = None
        self.next_advance = 0
        self.preview_until = 0
        self.notice = ""
        self.notice_until = 0
        self.lock_notice = 0
        self.lock_notice_until = 0
        self.last_lock = None
        self.temporary_frequencies = {}
        self.temporary_slots = set()
        self.close_mode = "off"
        self.close_return = "off"
        self.next_probe = 0
        self.probe_request = None
        self.capture = None
        self.capture_acknowledged = False
        self.capture_hold = False
        self.menu = None
        self.menu_index = 0
        self.close_group_index = 0
        self.menu_choice = False
        self.menu_until = 0
        self.pending_step = self.general_step
        self.pending_air_step = self.air_step
        self.clock_edit_minutes = 0
        self.clock_light_until = monotonic() + 3
        self.program_source = None
        self.program_bank = None
        self.program_slot = 0
        self.program_at = 0
        self.dab_program_source = None
        self.tone = {"id": 0, "count": 0, "kind": "key"}
        self._build_factory()

    @property
    def power(self):
        return self.volume > 0

    @property
    def squelch_db(self):
        return -120.0 if self.squelch == 0 else -98.0 + 100.0 * self.squelch

    @property
    def audio_gate_open(self):
        return self.audio_open and self.power and not self.menu and not self.preview_until

    @property
    def bands(self):
        return GERMAN_BANDS if self.plan == 2 else BANDS

    @property
    def favorite_edit(self):
        return self.favorite_edit_mode if monotonic() < self.favorite_edit_until else None

    @property
    def scan_banks(self):
        return self.enabled if self.selected_scan_slots is None else {"FAV"}

    @property
    def band(self):
        return self.bands[self.band_index]

    @property
    def search_step(self):
        if self.sideband:
            return self.sideband_step
        if self.full_search:
            return self.general_step or FULL_SEARCH_STEP_HZ
        return self._band_step(self.band_index)

    def _band_step(self, index):
        return self.air_step if index == AIR_INDEX else self.general_step or self.bands[index].step

    @property
    def tuned(self):
        return self.capture or self.current

    @property
    def tone_filter(self):
        channel = self.tuned
        if (channel and channel.bank == "FAV" and channel.modulation == "FM"
                and channel == self.memories["FAV"][channel.slot]):
            return self.favorite_tones.get(channel.slot)
        return None

    @property
    def dab_programming(self):
        return self.menu == "program" and self.dab_program_source is not None

    @property
    def dab_program_slot(self):
        if self.dab_programming and self.program_bank == "FAV":
            return self.program_slot + 1
        return None

    def _load(self):
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, UnicodeError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.plan = 2 if raw.get("plan") == 2 else 1
        self.general_step = raw.get("general_step", 0)
        if type(self.general_step) is not int or self.general_step not in GENERAL_STEPS:
            self.general_step = 0
        self.air_step = raw.get("air_step", AIR_STEP)
        if type(self.air_step) is not int or self.air_step not in AIR_STEPS:
            self.air_step = AIR_STEP
        self.band_index = raw.get("band_index", BAND_ORDER[0])
        if type(self.band_index) is not int or not 0 <= self.band_index < len(BANDS):
            self.band_index = BAND_ORDER[0]
        self.clock_offset = raw.get("clock_offset", 0)
        if type(self.clock_offset) is not int or abs(self.clock_offset) >= 1440:
            self.clock_offset = 0
        self.close_groups = raw.get("close_groups")
        if (not isinstance(self.close_groups, list) or len(self.close_groups) != len(CLOSE_GROUPS)
                or not all(type(enabled) is bool for enabled in self.close_groups)
                or not any(self.close_groups)):
            self.close_groups = [True] * len(CLOSE_GROUPS)
        self.close_alert = raw.get("close_alert", True) is True
        self.program_locked = raw.get("program_locked", False) is True
        self.rds_enabled = raw.get("rds_enabled") is True
        parent = raw.get("recording_parent")
        self.recording_parent = (Path(parent) if isinstance(parent, str) and "\0" not in parent
                                 and Path(parent).is_absolute() and ".." not in Path(parent).parts
                                 else None)
        saved = raw.get("memories")
        if not isinstance(saved, dict):
            saved = {}
        self.memories = {
            "PVT": self._load_slots(saved.get("PVT"), "PVT"),
            "FAV": [None] * FAVORITE_COUNT
            + self._load_slots(saved.get("EMG"), "FAV", FAVORITE_COUNT),
        }
        self.archived_frn = self._load_slots(saved.get("FRN"), "FRN")
        self.favorite_names = [""] * FAVORITE_SLOTS
        self.favorite_tones = {}
        names = raw.get("favorite_names")
        if isinstance(names, list):
            for index, name in enumerate(names[:FAVORITE_COUNT]):
                self.favorite_names[index] = normalize_favorite_name(name) or ""
        extended_names = raw.get("extended_favorite_names")
        if isinstance(extended_names, list):
            for index, name in enumerate(extended_names[:SLOTS], FAVORITE_COUNT):
                self.favorite_names[index] = normalize_favorite_name(name) or ""
        favorites = raw.get("favorites")
        if isinstance(favorites, list):
            for index, entry in enumerate(favorites[:FAVORITE_COUNT]):
                if not isinstance(entry, dict):
                    continue
                hz, mode = entry.get("hz"), entry.get("mode")
                if (type(hz) is int and MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ
                        and mode in ANALOGUE_MODES):
                    self.memories["FAV"][index] = Channel(hz, mode, "FAV", index)
                    if mode == "FM" and (tone := normalize_tone_filter(entry.get("tone"))):
                        self.favorite_tones[index] = tone
        extended_tones = raw.get("extended_favorite_tones")
        if isinstance(extended_tones, list):
            for index, value in enumerate(extended_tones[:SLOTS], FAVORITE_COUNT):
                channel = self.memories["FAV"][index]
                if channel and channel.modulation == "FM" and (tone := normalize_tone_filter(value)):
                    self.favorite_tones[index] = tone
        favorite_slots = len(self.memories["FAV"])
        self.dab_favorites = [None] * favorite_slots
        dab_favorites = raw.get("dab_favorites")
        if isinstance(dab_favorites, list):
            for index, entry in enumerate(dab_favorites[:favorite_slots]):
                if not isinstance(entry, dict):
                    continue
                favorite = _normalize_dab_favorite(entry.get("block"), entry.get("sid"),
                                                   entry.get("hz"), entry.get("name"))
                if favorite:
                    self.dab_favorites[index] = favorite
                    self.memories["FAV"][index] = None
                    self.favorite_tones.pop(index, None)
        self.zone_overrides = {}
        zones = raw.get("zone_overrides")
        if isinstance(zones, dict):
            for bank in CHIRP_ZONES:
                if bank in zones:
                    try:
                        self.zone_overrides[bank] = _chirp_zone(bank, zones[bank])
                    except ValueError:
                        continue
        self.dab_last_station = _normalize_dab_station(raw.get("dab_last_station"))
        dab_known_blocks = raw.get("dab_known_blocks")
        self.dab_known_blocks = _normalize_dab_blocks(
            dab_known_blocks if isinstance(dab_known_blocks, list) else (), self.dab_last_station)
        self.dab_service_counts = _normalize_dab_service_counts(raw.get("dab_service_counts"), self.dab_known_blocks)
        self.dab_station_ids = normalize_station_ids(raw.get("dab_station_ids"), blocks=self.dab_known_blocks)
        frequencies = raw.get("permanent_frequencies")
        self.permanent_frequencies = {}
        if isinstance(frequencies, list):
            self.permanent_frequencies = {
                hz: None for hz in frequencies[-FREQUENCY_SKIP_LIMIT:]
                if type(hz) is int and MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ
            }
        slots = raw.get("permanent_slots")
        self.permanent_slots = set()
        self.archived_frn_locks = set()
        if isinstance(slots, list):
            for entry in slots:
                if (not isinstance(entry, list) or len(entry) != 2
                        or not isinstance(entry[0], str) or type(entry[1]) is not int):
                    continue
                bank, slot = entry
                if bank == "FRN":
                    if 0 <= slot < len(self.archived_frn) and self.archived_frn[slot]:
                        self.archived_frn_locks.add(slot)
                    continue
                if bank == "EMG" and 0 <= slot < SLOTS:
                    bank, slot = "FAV", slot + FAVORITE_COUNT
                memories = self.memories.get(bank, ())
                if 0 <= slot < len(memories) and memories[slot]:
                    self.permanent_slots.add((bank, slot))

    @staticmethod
    def _load_slots(records, bank, offset=0):
        slots = [None] * SLOTS
        if isinstance(records, list):
            for slot, record in enumerate(records[:SLOTS]):
                if not isinstance(record, list) or len(record) != 2:
                    continue
                hz, mode = record
                if (type(hz) is int and MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ
                        and mode in ANALOGUE_MODES):
                    slots[slot] = Channel(hz, mode, bank, slot + offset)
        return slots

    def _save(self):
        # Preserve the old disk layout so rolling back can still read legacy
        # emergency memories and any archived user-programmed Freenet entries.
        memories = {
            "PVT": self.memories["PVT"],
            "EMG": self.memories["FAV"][FAVORITE_COUNT:],
            "FRN": self.archived_frn,
        }
        locks = [
            ("EMG", slot - FAVORITE_COUNT) if bank == "FAV" and slot >= FAVORITE_COUNT
            else (bank, slot)
            for bank, slot in self.permanent_slots
        ]
        locks.extend(("FRN", slot) for slot in self.archived_frn_locks)
        data = {
            "plan": self.plan,
            "general_step": self.general_step,
            "air_step": self.air_step,
            "band_index": self.band_index,
            "clock_offset": self.clock_offset,
            "close_groups": self.close_groups,
            "close_alert": self.close_alert,
            "program_locked": self.program_locked,
            "rds_enabled": self.rds_enabled,
            "favorites": self._favorite_entries(),
            "dab_favorites": self.dab_favorites,
            "dab_last_station": self.dab_last_station,
            "dab_known_blocks": self.dab_known_blocks,
            "dab_service_counts": self.dab_service_counts,
            "dab_station_ids": self.dab_station_ids,
            "favorite_names": self.favorite_names[:FAVORITE_COUNT],
            "permanent_frequencies": list(self.permanent_frequencies),
            "permanent_slots": sorted(locks),
            "memories": {
                bank: [[channel.hz, channel.modulation] if channel else None for channel in slots]
                for bank, slots in memories.items()
            },
        }
        names = self.favorite_names[FAVORITE_COUNT:]
        if any(names):
            data["extended_favorite_names"] = names
        if any(slot >= FAVORITE_COUNT for slot in self.favorite_tones):
            data["extended_favorite_tones"] = [self.favorite_tones.get(slot)
                                               for slot in range(FAVORITE_COUNT, FAVORITE_SLOTS)]
        if self.zone_overrides:
            data["zone_overrides"] = {bank: self._chirp_zone_entries(channels)
                                      for bank, channels in self.zone_overrides.items()}
        if self.recording_parent is not None:
            data["recording_parent"] = str(self.recording_parent)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, separators=(",", ":")) + "\n")
        temporary.replace(self.state_path)

    def set_recording_parent(self, parent):
        previous = self.recording_parent
        self.recording_parent = parent
        try:
            self._save()
        except OSError:
            self.recording_parent = previous
            raise

    def _build_factory(self):
        self.factory = factory_banks(self.air_step)
        self.factory.update(self.zone_overrides)

    def _favorite_entries(self):
        entries = []
        for index, channel in enumerate(self.memories["FAV"][:FAVORITE_COUNT]):
            entry = {"hz": channel.hz, "mode": channel.modulation} if channel else None
            if entry is not None and (tone := self.favorite_tones.get(index)):
                entry["tone"] = tone.copy()
            entries.append(entry)
        return entries

    @staticmethod
    def _chirp_zone_entries(channels):
        return [{"hz": channel.hz, "mode": channel.modulation, "number": channel.preset_number}
                for channel in channels]

    def chirp_download(self):
        """Export the analogue configuration while the caller holds the runtime lock."""
        channels = [
            {"hz": channel.hz, "mode": channel.modulation, "name": self.favorite_names[index],
             "tone": self.favorite_tones[index].copy() if index in self.favorite_tones else None}
            if channel else None
            for index, channel in enumerate(self.memories["FAV"])
        ]
        settings = {key: getattr(self, key) for key in CHIRP_SETTINGS}
        settings["close_groups"] = settings["close_groups"].copy()
        image = {
            "version": 1,
            "channels": channels,
            "reserved": [index for index, favorite in enumerate(self.dab_favorites, 1) if favorite],
            "zones": {bank: self._chirp_zone_entries(self.factory[bank]) for bank in CHIRP_ZONES},
            "custom_zones": sorted(self.zone_overrides),
            "settings": settings,
        }
        # Include DAB identities in the revision without exposing or editing them.
        dab = [(favorite, self.favorite_names[index]) for index, favorite in enumerate(self.dab_favorites)
               if favorite]
        digest_input = json.dumps([image, dab], sort_keys=True, separators=(",", ":"))
        image["revision"] = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        return image

    def chirp_upload(self, revision, changes, zones, settings, *, commit_access=None):
        """Validate and atomically save a clone patch under the runtime lock."""
        before = self.chirp_download()
        if revision != before["revision"]:
            raise ChirpConflict("Scanner configuration changed. Download From Radio again before uploading.")
        if self.menu or self.favorite_edit:
            raise ChirpConflict("Close the scanner's programming or selection menu before uploading.")
        if not isinstance(changes, list) or len(changes) > FAVORITE_SLOTS:
            raise ValueError("Invalid favourite changes")
        if not isinstance(zones, dict) or zones.keys() - set(CHIRP_ZONES):
            raise ValueError("Invalid scanner zones")
        updates = _chirp_settings(settings)
        if "close_groups" in updates:
            updates["close_groups"] = updates["close_groups"].copy()
        overrides = self.zone_overrides.copy()
        for bank, entries in zones.items():
            if entries is None:
                overrides.pop(bank, None)
            else:
                overrides[bank] = _chirp_zone(bank, entries)
        favorites = self.memories["FAV"].copy()
        names = self.favorite_names.copy()
        tones = self.favorite_tones.copy()
        changed_slots = set()
        seen = set()
        for change in changes:
            if not isinstance(change, dict) or change.keys() != {"slot", "channel"}:
                raise ValueError("Invalid favourite change")
            slot = change["slot"]
            if type(slot) is not int or not 1 <= slot <= FAVORITE_SLOTS or slot in seen:
                raise ValueError("Invalid or duplicate favourite slot")
            seen.add(slot)
            index = slot - 1
            if self.dab_favorites[index]:
                raise ChirpConflict(f"Favourite {slot} is reserved for DAB.")
            entry = change["channel"]
            if entry is None:
                channel, name, tone = None, "", None
            else:
                if not isinstance(entry, dict) or entry.keys() != {"hz", "mode", "name", "tone"}:
                    raise ValueError(f"Invalid favourite {slot}")
                hz, mode = _chirp_frequency_mode(entry)
                name = normalize_favorite_name(entry["name"])
                tone = normalize_tone_filter(entry["tone"])
                if name is None or (entry["tone"] is not None and (not tone or mode != "FM")):
                    raise ValueError(f"Invalid name or receive tone in favourite {slot}")
                channel = Channel(hz, mode, "FAV", index)
            if channel != favorites[index]:
                changed_slots.add(index)
            favorites[index] = channel
            names[index] = name
            if tone is None:
                tones.pop(index, None)
            else:
                tones[index] = tone

        previous_settings = {key: getattr(self, key) for key in updates}
        previous_favorites = self.memories["FAV"]
        previous_names, previous_tones = self.favorite_names, self.favorite_tones
        previous_overrides = self.zone_overrides
        if (favorites, names, tones, overrides, updates) == (
                previous_favorites, previous_names, previous_tones, previous_overrides, previous_settings):
            if commit_access is not None:
                commit_access()
            return before
        previous_permanent = self.permanent_slots
        changed_locks = {("FAV", index) for index in changed_slots}
        self.memories["FAV"] = favorites
        self.favorite_names, self.favorite_tones = names, tones
        self.zone_overrides = overrides
        self.permanent_slots = previous_permanent - changed_locks
        for key, value in updates.items():
            setattr(self, key, value)
        saved = False
        try:
            self._save()
            saved = True
            if commit_access is not None:
                commit_access()
        except OSError:
            self.memories["FAV"] = previous_favorites
            self.favorite_names, self.favorite_tones = previous_names, previous_tones
            self.zone_overrides = previous_overrides
            self.permanent_slots = previous_permanent
            for key, value in previous_settings.items():
                setattr(self, key, value)
            if saved:
                self._save()
            raise

        self.temporary_slots.difference_update(changed_locks)
        if self.selected_scan_slots is not None:
            self.selected_scan_slots.difference_update(index for index in changed_slots if favorites[index] is None)
        if overrides != previous_overrides or previous_settings.get("air_step", self.air_step) != self.air_step:
            self._build_factory()
        search_grid_changed = any(previous_settings[key] != updates[key]
                                  for key in ("plan", "general_step", "air_step") if key in updates)
        if self.activity == "search" and search_grid_changed:
            self.search_cursor = None
        if self.band_search and previous_settings.get("general_step", self.general_step) != self.general_step:
            self._build_band_search()
        if previous_settings.get("close_groups", self.close_groups) != self.close_groups:
            self.probe_request = None
            self.next_probe = 0
        self._chirp_refresh_tuning(previous_tones)
        return self.chirp_download()

    def _chirp_refresh_tuning(self, previous_tones):
        current = self.current
        if current and current.bank == "FAV":
            updated = self.memories["FAV"][current.slot]
            if updated is not None:
                tone_changed = previous_tones.get(current.slot) != self.favorite_tones.get(current.slot)
                if updated != current or tone_changed:
                    self._retune(updated, force=True, direct=self.direct_frequency)
            elif self.hold:
                self._retune(Channel(current.hz, current.modulation), direct=True)
        elif current and current.bank in CHIRP_ZONES and current not in self.factory[current.bank]:
            if self.hold:
                self._retune(Channel(current.hz, current.modulation), direct=True)
        self._rebuild_scan()
        if (self.power and self.activity == "scan" and not self.hold and not self.capture
                and self.close_mode != "only" and self.scan_cursor < 0):
            self._advance_scan()

    def set_favorite_tone(self, number, tone, expected_hz=None):
        if type(number) is not int or not 1 <= number <= FAVORITE_COUNT:
            return False
        index = number - 1
        entry = self.memories["FAV"][index]
        value = normalize_tone_filter(tone)
        if (not entry or entry.modulation != "FM" or (tone is not None and value is None)
                or (expected_hz is not None and entry.hz != expected_hz)):
            return False
        previous = self.favorite_tones.get(index)
        if value == previous:
            return True
        if value is None:
            self.favorite_tones.pop(index, None)
        else:
            self.favorite_tones[index] = value
        try:
            self._save()
        except OSError:
            if previous is None:
                self.favorite_tones.pop(index, None)
            else:
                self.favorite_tones[index] = previous
            raise
        if self.tuned == entry:
            self._clear_signal()
            self.tune_revision += 1
        return True

    def remember_dab(self, station, blocks, service_counts=None, station_ids=None):
        """Persist the last playable service and observed multiplex data when they change."""
        normalized_station = _normalize_dab_station(station)
        if station is not None and normalized_station is None:
            raise ValueError("Invalid DAB station")
        if (not isinstance(blocks, (list, tuple, set, frozenset))
                or any(not isinstance(block, str) or block not in DAB_FREQUENCIES for block in blocks)):
            raise ValueError("Invalid DAB blocks")
        known = _normalize_dab_blocks(blocks, normalized_station)
        counts = _normalize_dab_service_counts(
            self.dab_service_counts if service_counts is None else service_counts, known)
        directory = normalize_station_ids(self.dab_station_ids if station_ids is None else station_ids, blocks=known)
        previous = self.dab_last_station, self.dab_known_blocks, self.dab_service_counts, self.dab_station_ids
        updated = normalized_station, known, counts, directory
        if previous == updated:
            return False
        self.dab_last_station, self.dab_known_blocks, self.dab_service_counts, self.dab_station_ids = updated
        try:
            self._save()
        except OSError:
            self.dab_last_station, self.dab_known_blocks, self.dab_service_counts, self.dab_station_ids = previous
            raise
        return True

    def get_dab_favorite(self, number):
        if type(number) is not int or not 1 <= number <= len(self.dab_favorites):
            raise ValueError("Invalid favourite number")
        entry = self.dab_favorites[number - 1]
        return entry.copy() if entry else None

    def save_dab_favorite(self, number, *, block, sid, hz, name=""):
        if type(number) is not int or not 1 <= number <= len(self.dab_favorites):
            raise ValueError("Invalid favourite number")
        favorite = _normalize_dab_favorite(block, sid, hz, name)
        if favorite is None:
            raise ValueError("Invalid DAB favourite")
        if not self.power or self.menu:
            return False
        self._save_dab_slot(number - 1, favorite)
        self._tone()
        return True

    def _save_dab_slot(self, index, favorite):
        previous = self.dab_favorites[index]
        label = favorite["name"]
        if previous and (previous["block"], previous["sid"]) == (favorite["block"], favorite["sid"]):
            favorite = favorite.copy()
            favorite["name"] = favorite["name"] or previous["name"]
            label = self.favorite_names[index]
        self._persist_slot("FAV", index, None, dab=favorite, favorite_name=label)
        current = self.current
        if current and current.bank == "FAV" and current.slot == index:
            self._retune(Channel(current.hz, current.modulation), direct=self.hold or self.direct_frequency)
        self._rebuild_scan()

    def rename_favorite(self, number, name):
        name = normalize_favorite_name(name)
        if type(number) is not int or not 1 <= number <= FAVORITE_COUNT or name is None:
            raise ValueError("Invalid favourite name")
        index = number - 1
        previous = self.favorite_names[index]
        if name == previous:
            return
        self.favorite_names[index] = name
        try:
            self._save()
        except OSError:
            self.favorite_names[index] = previous
            raise

    def favorite(self, number, gesture, *, rds_ps=None):
        if (type(number) is not int or not 1 <= number <= FAVORITE_COUNT
                or gesture not in ("tap", "long", "lock", "select", "delete")):
            raise ValueError("Invalid favourite action")
        if not self.power or self.menu:
            return
        index = number - 1
        if gesture in ("select", "delete"):
            mode = "scan" if gesture == "select" else "delete"
            if self.favorite_edit != mode:
                return
            if mode == "delete":
                self.favorite_edit_until = monotonic() + FAVORITE_EDIT_SECONDS
                return self._delete_favorite(index)
            if self.memories["FAV"][index]:
                self._toggle_selected_slot(index)
            self.favorite_edit_until = monotonic() + FAVORITE_EDIT_SECONDS
            return
        if gesture == "long":
            channel = self.tuned
            selected = self.hold or self.capture_hold or self.received_signal
            if not channel or not channel.hz or not selected or self.preview_until:
                self._show("Error")
                self._tone("error")
                return
            self._save_favorite(index, channel, rds_ps)
        else:
            entry = self.memories["FAV"][index]
            if not entry:
                return
            if gesture == "lock":
                if self._lock_kind(entry):
                    return False
                self.temporary_slots.add(("FAV", index))
                self.lock_notice = 1
                self.lock_notice_until = monotonic() + 0.8
                if self.selected_scan_slots is not None and entry == self.current and not self.hold:
                    self._advance_scan()
                return True
            self._hold_channel(entry)
        self._tone()

    def edit_favorites(self, mode):
        self.favorite_edit_mode = mode
        self.favorite_edit_until = monotonic() + FAVORITE_EDIT_SECONDS
        self.notice_until = 0

    def end_favorite_edit(self):
        self.favorite_edit_until = 0

    def exit_selected_scan(self):
        self.end_favorite_edit()
        if self.selected_scan_slots is not None:
            self.selected_scan_slots = None
            self._rebuild_scan()

    def _toggle_selected_slot(self, index):
        if self.selected_scan_slots is None:
            self.selected_scan_slots = set()
        self.selected_scan_slots.symmetric_difference_update({index})
        self._start_selected_scan()
        self._tone()

    def _delete_favorite(self, index):
        entry = self.memories["FAV"][index]
        if not (entry or self.dab_favorites[index]):
            return False
        self._persist_slot("FAV", index, None, favorite_name="")
        if self.selected_scan_slots is not None:
            self.selected_scan_slots.discard(index)
        self._rebuild_scan()
        if entry and entry == self.current:
            if self.hold:
                self._retune(Channel(entry.hz, entry.modulation), direct=True)
            else:
                self._advance_scan()
        self._tone()
        return True

    def _save_favorite(self, index, channel, rds_ps):
        previous = self.memories["FAV"][index]
        label = self.favorite_names[index]
        if not label or self.dab_favorites[index] or (previous and previous.hz != channel.hz):
            label = normalize_favorite_name(rds_ps) or ""
        self._persist_slot("FAV", index, channel, favorite_name=label)
        self._rebuild_scan()

    def toggle_rds(self):
        self.rds_enabled = not self.rds_enabled
        self._save()
        if self.power:
            self._tone()

    def _clock_minutes(self):
        # time.localtime reflects the Ubuntu host time zone and daylight-saving rules.
        clock = localtime()
        return clock.tm_hour * 60 + clock.tm_min

    def _tone(self, kind="key", count=1):
        if not self.muted:
            self.tone = {"id": self.tone["id"] + 1, "kind": kind, "count": count}

    def _show(self, text, seconds=1.0):
        self.notice = text
        self.notice_until = monotonic() + seconds

    def _clear_signal(self):
        self.signal_open = False
        self.audio_open = False
        self.waiting_reply = None
        self.received_signal = False
        self.tone_deadline = None
        self.tone_matched = None

    def _retune(self, channel, force=False, direct=False):
        if channel != self.current or force:
            self._clear_signal()
            self.tune_revision += 1
        self.current = channel
        self.direct_frequency = direct
        if self.hold and channel and channel.hz:
            self.last_held = Channel(channel.hz, channel.modulation)
        steps_per_second = 50
        if self.activity == "search":
            steps_per_second = 180 if self.search_step == 5_000 else 60
        self.next_advance = monotonic() + 1 / steps_per_second

    def _drop_capture(self):
        if self.capture:
            self._clear_signal()
        self.capture = None
        self.capture_acknowledged = False
        self.capture_hold = False

    def _lock_kind(self, channel):
        if not channel or not channel.hz:
            return 0
        if channel.slot is not None:
            identity = (channel.bank, channel.slot)
            if identity in self.permanent_slots:
                return 2
            return 1 if identity in self.temporary_slots else 0
        if channel.hz in self.permanent_frequencies:
            return 2
        return 1 if channel.hz in self.temporary_frequencies else 0

    def _bank_entries(self, bank):
        saved = [channel for channel in self.memories.get(bank, ()) if channel]
        return (*self.factory[bank], *saved)

    def _rebuild_scan(self):
        if self.selected_scan_slots is None:
            self.scan_list = [entry for bank in BANKS if bank in self.enabled
                              for entry in self._bank_entries(bank)]
        else:
            self.scan_list = [entry for index in sorted(self.selected_scan_slots)
                              if (entry := self.memories["FAV"][index])]
        self.scan_cursor = self.scan_list.index(self.current) if self.current in self.scan_list else -1

    def _advance_scan(self, direction=None, manual=False, first_bank=None):
        count = len(self.scan_list)
        if not count:
            self.scan_cursor = -1
            self._retune(None)
            return
        direction = self.scan_direction if direction is None else direction
        start = self.scan_cursor
        if first_bank:
            indexes = range(count) if direction > 0 else range(count - 1, -1, -1)
            start = next((index - direction for index in indexes if self.scan_list[index].bank == first_bank), start)
        elif start < 0 and direction < 0:
            start = 0
        for offset in range(1, count + 1):
            index = (start + direction * offset) % count
            channel = self.scan_list[index]
            if manual or not self._lock_kind(channel):
                self.scan_cursor = index
                self._retune(channel, force=True)
                return
        self._retune(None)

    def next_favorite_slot(self, direction, current_slot=None, *, include_dab=False):
        slots = self.memories["FAV"]
        start = current_slot - 1 if current_slot is not None else (-1 if direction > 0 else 0)
        for offset in range(1, len(slots) + 1):
            index = (start + direction * offset) % len(slots)
            if slots[index] or (include_dab and self.dab_favorites[index]):
                return index + 1

    def _step_favorite(self, direction, recall_favorite=None):
        channel = self.current
        current_slot = channel.slot + 1 if channel and channel.bank == "FAV" else None
        if slot := self.next_favorite_slot(direction, current_slot, include_dab=recall_favorite is not None):
            if recall_favorite is None:
                self._hold_channel(self.memories["FAV"][slot - 1])
            else:
                recall_favorite(slot)

    def hold_favorites(self, slot=None, *, recall_favorite=None):
        self.exit_selected_scan()
        self._clear_band_search()
        self._leave_only()
        self._drop_capture()
        self.probe_request = None
        self.preview_until = self.notice_until = 0
        self.enabled = {"FAV"}
        self.activity = "scan"
        self.hold = True
        self._rebuild_scan()

        if slot is None or not (self.memories["FAV"][slot - 1] or
                                recall_favorite is not None and self.dab_favorites[slot - 1]):
            slot = self.next_favorite_slot(1, include_dab=recall_favorite is not None)
        if slot is None:
            self._retune(None)
        elif recall_favorite is None:
            self._hold_channel(self.memories["FAV"][slot - 1])
        else:
            if self.dab_favorites[slot - 1]:
                self._retune(None)
            recall_favorite(slot)

    def _advance_search(self, direction, manual=False, *, first=False, origin=None):
        band, step = self.band, self.search_step
        if self.band_search:
            count = len(self.search_frequencies)
            frequency = self.search_frequencies.__getitem__
        elif self.full_search:
            span = MANUAL_MAX_HZ - MANUAL_MIN_HZ
            count = (span + step - 1) // step + 1
            frequency = lambda position: min(MANUAL_MIN_HZ + position * step, MANUAL_MAX_HZ)
        else:
            count = band.count(step)
            frequency = lambda position: band.frequency(position, step)
        index = self.search_cursor
        if first or (origin is None and self.current is None and index is None):
            index = -1 if direction > 0 else 0
        elif origin is not None or self.direct_frequency or index is None:
            if origin is None:
                origin = self.current.hz
            if direction > 0:
                index = bisect_right(range(count), origin, key=frequency) - 1
            else:
                index = bisect_left(range(count), origin, key=frequency)
        for _ in range(count):
            index = (index + direction) % count
            hz = frequency(index)
            if self.band_search:
                modulation = self.sideband or band_search_mode(self.band_search, hz)
            else:
                modulation = self.sideband or (self._automatic_modulation(hz) if self.full_search else band.modulation)
            channel = Channel(hz, modulation)
            if manual or not self._lock_kind(channel):
                self.search_cursor = index
                self._retune(channel, force=True)
                return
        self._retune(None)

    def _advance(self, manual=False, direction=None):
        if self.activity == "search":
            self._advance_search(direction or self.direction, manual)
        elif self.activity == "scan":
            self._advance_scan(direction, manual)

    def tune_frequency(self, hz):
        if not self.power or self.menu:
            return
        if type(hz) is not int or not MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ:
            self._show("Error")
            self._tone("error")
            return
        self._hold_frequency(hz, self._manual_modulation(hz))

    def _manual_modulation(self, hz):
        if self.sideband:
            return self.sideband
        channel = self.tuned
        if channel and self.direct_frequency and channel.modulation in SIDEBANDS:
            return channel.modulation
        if (channel and 25_000_000 <= hz < 30_000_000
                and (channel.bank in ("CBAM", "CBFM")
                     or self.direct_frequency and 25_000_000 <= channel.hz < 30_000_000)):
            return channel.modulation
        return self._automatic_modulation(hz)

    def _automatic_modulation(self, hz):
        if 87_500_000 <= hz < 108_000_000:
            return "WFM"
        if hz < 25_000_000 or 108_000_000 <= hz < 137_000_000 or 225_000_000 <= hz < 400_000_000:
            return "AM"
        band = next((band for band in self.bands if band.lower <= hz <= band.upper), None)
        return band.modulation if band else "FM"

    def _hold_frequency(self, hz, modulation):
        self._hold_channel(Channel(hz, modulation))

    def _cycle_sideband(self):
        channel = self.tuned
        current = self.sideband or (channel.modulation if channel and channel.modulation in SIDEBANDS else None)
        modes = (None, *SIDEBANDS)
        self.sideband = modes[(modes.index(current) + 1) % len(modes)]
        if channel:
            if self.activity == "search" and self.band_search:
                modulation = self.sideband or band_search_mode(self.band_search, channel.hz)
                self._retune(Channel(channel.hz, modulation), force=True, direct=self.direct_frequency)
            else:
                modulation = self.sideband or self._automatic_modulation(channel.hz)
                self._hold_frequency(channel.hz, modulation)
        self._show(self.sideband or "Auto")

    def _hold_channel(self, channel):
        self.exit_selected_scan()
        self._leave_only()
        self._drop_capture()
        self.probe_request = None
        self.preview_until = 0
        self.notice_until = 0
        self.hold = True
        self._retune(channel, force=True, direct=True)
        if channel.bank == "FAV" and self.activity == "scan" and "FAV" in self.enabled:
            self.scan_cursor = self.scan_list.index(channel)

    def _frequency_step(self, direction, hz):
        if self.current.modulation in SIDEBANDS:
            target = hz + direction * self.sideband_step
            if MANUAL_MIN_HZ <= target <= MANUAL_MAX_HZ:
                self._hold_frequency(target, self.current.modulation)
            return
        bands = self.bands
        index = next((index for index, band in enumerate(bands)
                      if band.lower <= hz <= band.upper), None)
        if index is None:
            if 87_500_000 <= hz < 108_000_000:
                step = 100_000
            else:
                step = self.general_step or (5_000 if hz < 25_000_000 else 12_500)
            hz += direction * step
            if MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ:
                self.tune_frequency(hz)
            return
        band = bands[index]
        step = self._band_step(index)
        count = band.count(step)
        frequency = lambda position: band.frequency(position, step)
        if direction > 0:
            position = bisect_right(range(count), hz, key=frequency)
        else:
            position = bisect_left(range(count), hz, key=frequency) - 1
        if not 0 <= position < count:
            adjacent = BAND_ORDER.index(index) + direction
            if not 0 <= adjacent < len(BAND_ORDER):
                return
            index = BAND_ORDER[adjacent]
            band = bands[index]
            step = self._band_step(index)
            position = 0 if direction > 0 else band.count(step) - 1
        modulation = band.modulation
        if (self.direct_frequency and 25_000_000 <= hz < 30_000_000
                and 25_000_000 <= band.lower < 30_000_000):
            modulation = self.current.modulation
        self._hold_frequency(band.frequency(position, step), modulation)

    def tune_step(self, direction, step_hz=FINE_STEP_HZ):
        if not self.power or self.menu:
            return
        channel = self.tuned
        if channel is None:
            if self.activity == "search" and self.full_search:
                self.tune_frequency(MANUAL_MIN_HZ if direction > 0 else MANUAL_MAX_HZ)
            else:
                band = self.band
                step = self._band_step(self.band_index)
                position = 0 if direction > 0 else band.count(step) - 1
                self._hold_frequency(band.frequency(position, step), self.sideband or band.modulation)
            return
        hz = channel.hz + direction * step_hz
        if not MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ:
            if not self.hold:
                self._hold_frequency(channel.hz, channel.modulation)
            return
        if (channel.bank == "HAM" or self.activity == "search" and self.band_search == "HAM") and (
                channel.modulation in SIDEBANDS and not self.sideband):
            self._hold_frequency(hz, channel.modulation)
        else:
            self.tune_frequency(hz)

    def _start_scan(self):
        search_zone = self.band_search
        self._clear_band_search()
        if self.selected_scan_slots:
            self._start_selected_scan()
            return
        self.exit_selected_scan()
        if (self.activity == "scan" and self.enabled and self.current and not self.hold
                and not self.capture and self.close_mode != "only"):
            return

        held_bank = self.current.bank if self.hold and self.current else None
        first_bank = None
        if held_bank and held_bank not in self.enabled and (held_bank == "FAV" or not self.enabled):
            first_bank = held_bank
        elif not self.enabled and search_zone:
            first_bank = search_zone
        elif not self.enabled and any(self.memories["FAV"]):
            first_bank = "FAV"
        enabled = self.enabled | {first_bank} if first_bank else self.enabled
        if not any(not self._lock_kind(channel) for bank in BANKS if bank in enabled
                   for channel in self._bank_entries(bank)):
            if self.activity == "search":
                self.activity = "scan"
                self.preview_until = 0
                if self.tuned:
                    self._hold_channel(self.tuned)
            self._show("no Ch")
            self._tone("error")
            return

        self.enabled = enabled
        self.activity = "scan"
        self.hold = False
        self.preview_until = 0
        self.notice_until = 0
        self._leave_only()
        self._drop_capture()
        self._rebuild_scan()
        self._advance_scan(first_bank=first_bank)

    def _start_selected_scan(self):
        self._clear_band_search()
        keep_channel = (self.activity == "scan" and not self.hold and not self.capture
                        and self.close_mode != "only")
        self.activity = "scan"
        self.hold = False
        self.preview_until = self.notice_until = 0
        self._leave_only()
        self._drop_capture()
        self.probe_request = None
        self._rebuild_scan()
        if not keep_channel or self.scan_cursor < 0 or self._lock_kind(self.current):
            self._advance_scan()

    def _start_search(self, reset=False):
        origin = self.tuned
        self.exit_selected_scan()
        if not self.band_search and self.sideband is None and origin and origin.modulation in SIDEBANDS:
            self.sideband = origin.modulation
        if not self.band_search and self.activity != "search":
            self.full_search = True
        self.activity = "search"
        self.hold = False
        self.preview_until = 0
        self._leave_only()
        self._drop_capture()
        self._advance_search(self.direction, first=reset or origin is None,
                             origin=origin.hz if origin else None)

    def _clear_band_search(self):
        self.band_search = self.band_search_range = None
        self.search_frequencies = ()

    def _build_band_search(self):
        self.search_frequencies = band_search_frequencies(
            self.band_search, self.band_search_range, self.general_step)

    def _start_band_search(self, bank):
        self.band_search = bank
        self.band_search_range = f"{bank} ALL"
        self.full_search = False
        self._build_band_search()
        self._start_search()

    def _preview_search_band(self, delta):
        if not self.band_search:
            self._preview_band(delta)
            return
        labels = (f"{self.band_search} ALL", *BAND_SEARCH_RANGES[self.band_search])
        position = (labels.index(self.band_search_range) + delta) % len(labels)
        self.band_search_range = labels[position]
        self._build_band_search()
        self.hold = False
        self._leave_only()
        self._drop_capture()
        self.preview_until = monotonic() + 3
        self._retune(None)

    def _preview_band(self, delta=0):
        self.exit_selected_scan()
        self._leave_only()
        self._drop_capture()
        band_count = len(BAND_ORDER)
        position = (band_count if self.activity == "search" and self.full_search
                    else BAND_ORDER.index(self.band_index))
        position = (position + delta) % (band_count + 1)
        self.activity = "search"
        self.hold = False
        self.full_search = position == band_count
        if not self.full_search:
            self.band_index = BAND_ORDER[position]
        self.preview_until = monotonic() + 3
        self._retune(None)
        if delta and not self.full_search:
            self._save()

    def _select_bank(self, key):
        if self.hold and self.enabled == {"FAV"} and self.selected_scan_slots is None:
            if key == "fav":
                self._start_scan()
                return
            self.enabled.clear()
        self._clear_band_search()
        self.exit_selected_scan()
        groups = BANK_KEYS[key]
        selected = tuple(index for index, bank in enumerate(groups) if bank in self.enabled)
        if self.activity != "scan" or self.capture:
            updated = selected or tuple(range(len(groups)))
        elif len(groups) == 1:
            updated = () if selected else (0,)
        else:
            updated = PAIR_STATES[(PAIR_STATES.index(selected) + 1) % len(PAIR_STATES)]
        self.enabled.difference_update(groups)
        self.enabled.update(groups[index] for index in updated)
        self.preview_until = 0
        self._leave_only()
        self._drop_capture()
        self.activity = "scan"
        self.hold = False
        self._rebuild_scan()
        if not self.enabled and self.last_held:
            self._hold_channel(self.last_held)
            return
        active = [groups[index] for index in updated]
        self._advance_scan(first_bank=active[0] if active else None)
        if active and not any(not self._lock_kind(channel) for bank in active
                              for channel in self._bank_entries(bank)):
            self._tone("error")

    def _unlock_permanent(self, channel):
        saved_slots, saved_frequencies = self.permanent_slots, self.permanent_frequencies
        if channel.slot is not None:
            self.permanent_slots = saved_slots - {(channel.bank, channel.slot)}
        else:
            self.permanent_frequencies = saved_frequencies.copy()
            del self.permanent_frequencies[channel.hz]
        try:
            self._save()
        except OSError:
            self.permanent_slots, self.permanent_frequencies = saved_slots, saved_frequencies
            raise

    def clear_temporary_lockouts(self):
        had_locks = bool(self.temporary_slots or self.temporary_frequencies)
        self.temporary_slots.clear()
        self.temporary_frequencies.clear()
        self.lock_notice = 0
        self.lock_notice_until = 0
        self.last_lock = None
        return had_locks

    def _clear_lockouts(self, channel):
        permanent = self._lock_kind(channel) == 2
        if permanent:
            self._unlock_permanent(channel)
        if self.clear_temporary_lockouts() or permanent:
            self._tone(count=2)
            if self.selected_scan_slots and not self.current:
                self._advance_scan()

    def _lock(self, gesture):
        now = monotonic()
        channel = self.tuned
        if gesture == "long":
            self._clear_lockouts(channel)
            return
        if gesture == "double" and self.last_lock and now - self.last_lock[0] < 0.6:
            channel = self.last_lock[1]
        if not channel or not channel.hz:
            return
        kind = self._lock_kind(channel)
        permanent = gesture == "double"
        if not permanent and kind == 2:
            return
        held = self.capture_hold if self.capture else self.hold
        restore = not permanent and held and kind == 1
        if permanent and channel.bank and channel.bank not in FULL_LOCKOUT and kind != 2:
            available = sum(not self._lock_kind(entry) or entry == channel
                            for entry in self.factory[channel.bank])
            if available <= 1:
                self._tone("error")
                return
        if channel.slot is not None:
            identity = (channel.bank, channel.slot)
            self.temporary_slots.discard(identity)
            if permanent:
                self.permanent_slots.add(identity)
            elif not restore:
                self.temporary_slots.add(identity)
        else:
            self.temporary_frequencies.pop(channel.hz, None)
            if not restore:
                skips = self.permanent_frequencies if permanent else self.temporary_frequencies
                skips[channel.hz] = None
                if len(skips) > FREQUENCY_SKIP_LIMIT:
                    del skips[next(iter(skips))]
        if permanent:
            self._save()
        self.lock_notice = 0 if restore else 2 if permanent else 1
        self.lock_notice_until = 0 if restore else now + 0.8
        self.last_lock = (now, channel) if not permanent and not restore else None
        if not held and channel == self.tuned:
            self._drop_capture()
            if not self.hold:
                self._advance()

    def _begin_program(self):
        if self.program_locked:
            self._show("Pg.L")
            self._tone("error")
            return
        self.exit_selected_scan()
        self.preview_until = 0
        self.dab_program_source = None
        self.program_source = self.tuned or Channel(0, "FM")
        self.program_bank = None
        self.menu = "program"
        self.audio_open = False

    def begin_dab_program(self, *, block, sid, hz, name=""):
        favorite = _normalize_dab_favorite(block, sid, hz, name)
        if favorite is None:
            raise ValueError("Invalid DAB favourite")
        if not self.power or self.menu:
            return False
        self._tone()
        self._begin_program()
        if self.menu != "program":
            return False
        self.dab_program_source = favorite
        return True

    def cancel_dab_program(self):
        if self.dab_programming:
            self.menu = None
        self.dab_program_source = None

    def dab_program_key(self, key, gesture="tap"):
        key = KEY_ALIASES.get(key, key)
        if not self.dab_programming or key not in ("fav", "up", "down", "hold", "prog"):
            return None
        if key == "prog" and gesture == "long":
            self.key(key, gesture)
            return None
        self._tone()
        if key == "hold":
            self.cancel_dab_program()
        elif key == "prog" and self.dab_program_slot is not None:
            if self.program_locked:
                self._show("Pg.L")
                self._tone("error")
                return None
            slot = self.program_slot
            self._save_dab_slot(slot, self.dab_program_source)
            self.cancel_dab_program()
            return slot + 1
        else:
            self._program(key, gesture)
        return None

    def _program(self, key, gesture):
        if key == "lock" and gesture == "long" and not self.program_bank:
            self.program_source = Channel(0, "FM")
        elif key in BANK_KEYS:
            bank = WRITABLE.get(key)
            if bank is None:
                self._show("Error")
                self._tone("error")
                return
            self.program_bank = bank
            slots = self.memories[bank]
            self.program_slot = next((index for index in range(len(slots))
                                      if not self._slot_hz(bank, index)), 0)
            self.program_at = monotonic()
        elif key in ("up", "down") and self.program_bank:
            slots = self.memories[self.program_bank]
            self.program_slot = (self.program_slot + (1 if key == "up" else -1)) % len(slots)
            self.program_at = monotonic()
        elif key == "prog" and self.program_bank:
            self._write_slot()
        elif key == "hold":
            self.menu = None

    def _slot_hz(self, bank, slot):
        channel = self.memories[bank][slot]
        if channel:
            return channel.hz
        if bank == "FAV" and self.dab_favorites[slot]:
            return self.dab_favorites[slot]["hz"]
        return 0

    def _persist_slot(self, bank, slot, source, *, dab=None, favorite_name=None):
        entries = self.memories[bank]
        previous = entries[slot]
        identity = (bank, slot)
        permanent = identity in self.permanent_slots
        temporary = identity in self.temporary_slots
        favorite_bank = bank == "FAV"
        channel = Channel(source.hz, source.modulation, bank, slot) if source else None
        if favorite_bank:
            previous_dab = self.dab_favorites[slot]
            self.dab_favorites[slot] = dab
            previous_name = self.favorite_names[slot]
            if favorite_name is not None or previous_dab or (slot >= FAVORITE_COUNT and channel != previous):
                self.favorite_names[slot] = favorite_name or ""
        previous_tone = self.favorite_tones.get(slot) if favorite_bank else None
        if previous_tone and channel != previous:
            self.favorite_tones.pop(slot)
        entries[slot] = channel if channel and channel.hz else None
        self.permanent_slots.discard(identity)
        self.temporary_slots.discard(identity)
        try:
            self._save()
        except OSError:
            entries[slot] = previous
            if favorite_bank:
                self.dab_favorites[slot] = previous_dab
            if favorite_bank:
                self.favorite_names[slot] = previous_name
                if previous_tone:
                    self.favorite_tones[slot] = previous_tone
            if permanent:
                self.permanent_slots.add(identity)
            if temporary:
                self.temporary_slots.add(identity)
            raise
        return channel

    def _write_slot(self):
        if self.program_locked:
            self._show("Pg.L")
            self._tone("error")
            return
        bank, slot = self.program_bank, self.program_slot
        channel = self._persist_slot(bank, slot, self.program_source)
        self.menu = None
        self._drop_capture()
        self._leave_only()
        self._clear_band_search()
        self.activity = "scan"
        self.hold = True
        self.enabled.add(bank)
        self._retune(channel)
        self._rebuild_scan()

    def _edit_steps(self, key, gesture):
        if key == "prog":
            self._finish_steps()
        elif key == "band" and gesture == "tap":
            if self.menu != "step_sideband":
                self.menu = "step_air" if self.menu == "step_general" else "step_general"
            self.menu_until = monotonic() + 3
        elif key in ("up", "down"):
            delta = 1 if key == "up" else -1
            if self.menu == "step_air":
                self.pending_air_step = AIR_STEPS[(AIR_STEPS.index(self.pending_air_step) + delta) % len(AIR_STEPS)]
            else:
                steps = SIDEBAND_STEPS if self.menu == "step_sideband" else GENERAL_STEPS
                self.pending_step = steps[(steps.index(self.pending_step) + delta) % len(steps)]
            self.menu_until = monotonic() + 3

    def _finish_steps(self):
        sideband = self.menu == "step_sideband"
        self.menu = None
        if sideband:
            self.sideband_step = self.pending_step
        else:
            self.general_step, self.air_step = self.pending_step, self.pending_air_step
            self._build_factory()
            self._save()
            if self.band_search:
                self._build_band_search()
        self._start_search(reset=not sideband)

    def _leave_only(self):
        if self.close_mode == "only":
            self.close_mode = self.close_return
            self.probe_request = None
        self.next_probe = monotonic() + 2

    def _set_close_mode(self, gesture):
        if gesture == "long":
            self.exit_selected_scan()
            if self.close_mode != "only":
                self.close_return = self.close_mode
            self.close_mode = "only"
            self._retune(None)
            self._drop_capture()
            self.hold = False
            self.preview_until = 0
        elif self.close_mode == "only":
            self.menu = "close"
            self.menu_index = 0
        else:
            order = ("off", "dnd", "priority")
            self.close_mode = order[(order.index(self.close_mode) + 1) % len(order)]
        self.probe_request = None
        self.next_probe = monotonic() + (0 if self.close_mode == "only" else 2)

    def _edit_close(self, key):
        menu = self.menu
        delta = 1 if key == "up" else -1 if key == "down" else 0
        if key == "close":
            self.menu = None
        elif key == "band":
            previous = {"close": None, "close_groups": "close", "close_group_value": "close_groups",
                        "close_alert": "close"}
            self.menu = previous[menu]
        elif menu == "close":
            if delta:
                self.menu_index = (self.menu_index + delta) % 2
            elif key == "prog":
                self.menu = "close_groups" if self.menu_index == 0 else "close_alert"
                self.menu_choice = self.close_alert
        elif menu == "close_groups":
            if delta:
                self.close_group_index = (self.close_group_index + delta) % len(CLOSE_GROUPS)
            elif key == "prog":
                self.menu_choice = self.close_groups[self.close_group_index]
                self.menu = "close_group_value"
        elif delta:
            self.menu_choice = not self.menu_choice
        elif key == "prog":
            if menu == "close_group_value":
                self.close_groups[self.close_group_index] = self.menu_choice
                if not any(self.close_groups):
                    self.close_groups[0] = True
                self.menu = "close_groups"
            else:
                self.close_alert = self.menu_choice
                self.menu = "close"
            self._save()

    def _edit_clock(self, key, gesture):
        now = monotonic()
        if self.menu == "plan":
            if key in ("up", "down"):
                self.menu_index = 3 - self.menu_index
            elif key == "prog":
                self.plan = self.menu_index
                self._build_factory()
                self._rebuild_scan()
                self._save()
                self.menu = None
                self.clock_light_until = now + 3
            elif key == "band":
                self.menu = None
            return
        if self.menu == "clock":
            self.menu_until = now + 180
            if key in ("up", "down"):
                interval = 15 if gesture == "long" else 1
                self.clock_edit_minutes = (self.clock_edit_minutes + (interval if key == "up" else -interval)) % 1440
            elif key == "band":
                self.menu = None
                self.clock_light_until = now + 3
            elif key == "prog" and gesture == "tap":
                self.clock_offset = self.clock_edit_minutes - self._clock_minutes()
                self.menu = None
                self.clock_light_until = now + 3
                self._save()
        elif key == "prog" and gesture == "long":
            self.menu = "clock"
            self.clock_edit_minutes = (self._clock_minutes() + self.clock_offset) % 1440
            self.menu_until = now + 180

    def knob(self, name, value, held=(), *, start_favorites=None):
        if name not in ("volume", "squelch") or type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Invalid knob value")
        value = min(1.0, max(0.0, value))
        if name == "squelch":
            self.squelch = value
            return
        was_on = self.power
        cleared = False
        if not was_on and value > 0:
            chord = frozenset(held)
            if chord == {"hold", "down"}:
                self.menu = "plan"
                self.menu_index = self.plan
                return
            if chord == {"lock", "prog"}:
                self._clear_memory()
                cleared = True
        self.volume = value
        if self.power == was_on:
            return
        self.cancel_dab_program()
        self.menu = None
        self.preview_until = 0
        self.probe_request = None
        self._drop_capture()
        self._clear_signal()
        if not self.power:
            self.exit_selected_scan()
            self.clock_light_until = monotonic() + 3
            self.temporary_frequencies.clear()
            self.temporary_slots.clear()
        else:
            (start_favorites or self.hold_favorites)()
            if cleared:
                self._show("Clear")

    def _clear_memory(self):
        self.exit_selected_scan()
        for bank, memories in self.memories.items():
            self.memories[bank] = [None] * len(memories)
        self.dab_favorites = [None] * len(self.memories["FAV"])
        self.favorite_names = [""] * FAVORITE_SLOTS
        self.favorite_tones.clear()
        self.archived_frn = [None] * SLOTS
        self.archived_frn_locks.clear()
        self.permanent_frequencies.clear()
        self.temporary_frequencies.clear()
        self.permanent_slots.clear()
        self.temporary_slots.clear()
        self.program_locked = False
        self.hold = False
        self.last_held = None
        self._rebuild_scan()
        self._save()

    def key(self, name, gesture="tap", *, recall_favorite=None):
        name = KEY_ALIASES.get(name, name)
        if name not in KEYS or gesture not in ("tap", "long", "double"):
            raise ValueError("Invalid panel input")
        if not self.power:
            self._edit_clock(name, gesture)
            return
        if name == "mute":
            if gesture == "tap":
                self.muted = not self.muted
            return
        self._tone()
        if (self.capture and not self.capture_acknowledged and name != "scan"
                and (name, gesture) not in (("lock", "long"), ("fav", "long"), ("hold", "long"))):
            self.capture_acknowledged = True
            return
        if name == "prog" and gesture == "long":
            self.program_locked = not self.program_locked
            self._show("Pg.L" if self.program_locked else "OF Pg.L")
            self._save()
        elif self.menu == "program":
            self._program(name, gesture)
        elif self.menu and self.menu.startswith("step_"):
            self._edit_steps(name, gesture)
        elif self.menu and self.menu.startswith("close"):
            self._edit_close(name)
        elif name == "fav" and gesture == "long":
            self.edit_favorites("delete")
        elif name in ("amateur", "business") and gesture == "long":
            self._start_band_search(BANK_KEYS[name][0])
        elif name in BANK_KEYS:
            self._select_bank(name)
        elif name == "close":
            if self.capture and gesture != "long":
                self._drop_capture()
            else:
                self._set_close_mode(gesture)
        elif name == "sideband":
            self._cycle_sideband()
        elif name == "band":
            if gesture == "long" and self.activity == "search":
                self.pending_step = self.sideband_step if self.sideband else self.general_step
                self.pending_air_step = self.air_step
                self.preview_until = 0
                self.menu = "step_sideband" if self.sideband else "step_general"
                self.menu_until = monotonic() + 3
                self.audio_open = False
            elif gesture == "tap":
                delta = int(self.activity == "search" and bool(self.preview_until or self.current))
                self._preview_search_band(delta)
        elif name == "scan":
            if gesture == "long":
                self.edit_favorites("scan")
            else:
                self.end_favorite_edit()
                self._start_scan()
        elif name == "search":
            self._start_search(reset=bool(self.preview_until))
        elif name == "hold":
            if gesture == "long":
                channel = self.current
                slot = channel.slot + 1 if channel and channel.bank == "FAV" else None
                self.hold_favorites(slot, recall_favorite=recall_favorite)
            else:
                self._toggle_hold()
        elif name in ("up", "down"):
            self._arrow(1 if name == "up" else -1, recall_favorite)
        elif name == "lock":
            self._lock(gesture)
        elif name == "prog":
            self._begin_program()

    def _toggle_hold(self):
        if self.preview_until:
            self._start_search(reset=True)
        if self.capture:
            if self.capture_hold:
                self._drop_capture()
                if not self.hold:
                    self._advance()
            else:
                self.capture_hold = True
                self.last_held = Channel(self.capture.hz, self.capture.modulation)
            return
        if not self.tuned:
            if self.close_mode != "only" and (self.scan_list or self.activity == "search"):
                self.hold = True
                self._advance(manual=True)
            return
        self.hold = not self.hold
        if self.hold:
            self.last_held = Channel(self.tuned.hz, self.tuned.modulation)
        else:
            self._drop_capture()
            if self.direct_frequency or not self.audio_open or self._lock_kind(self.current):
                self._advance()

    def _arrow(self, direction, recall_favorite=None):
        if self.preview_until:
            self._preview_search_band(direction)
        elif self.close_mode == "only" and not self.capture:
            return
        elif self.selected_scan_slots is not None:
            if not self.hold:
                self.scan_direction = direction
            self._drop_capture()
            self._advance_scan(direction=direction)
        elif self.hold and not self.capture and (
            (self.current and self.current.bank == "FAV")
            or (self.activity == "scan" and self.enabled == {"FAV"} and not self.current)
        ):
            self._step_favorite(direction, recall_favorite)
        elif self.direct_frequency:
            self._frequency_step(direction, self.current.hz)
        elif self.hold:
            self._drop_capture()
            self._advance(manual=True, direction=direction)
        elif self.activity == "search":
            self.direction = direction
            self._drop_capture()
            self._advance()
        elif self.activity == "scan":
            self.scan_direction = direction
            self._drop_capture()
            self._advance_scan()

    def tick(self, level=None, detected_tone=None):
        """Apply a fresh current-channel dBFS measurement, or refresh only the timers."""
        now = monotonic()
        if self.menu == "clock" and now >= self.menu_until:
            self.menu = None
            self.clock_light_until = now + 3
        if not self.power:
            return
        if self.menu and self.menu.startswith("step_") and now >= self.menu_until:
            self._finish_steps()
            level = None
        if self.preview_until and now >= self.preview_until:
            self._start_search(reset=True)
            level = None

        if self.menu or self.preview_until:
            self.audio_open = False
            return
        if self.tuned and level is not None and math.isfinite(level):
            self._measure(level, now, detected_tone)
        if (self.selected_scan_slots is not None or self.close_mode == "off"
                or (self.tone_filter and self.signal_open)
                or self.capture or now < self.next_probe):
            return
        self.next_probe = now + (0.35 if self.close_mode == "only" else 2)
        if self.close_mode != "dnd" or not self.signal_open:
            self.probe_request = [(lower, upper) for enabled, (_, lower, upper)
                                  in zip(self.close_groups, CLOSE_GROUPS) if enabled]

    def _measure(self, level, now, detected_tone):
        self.signal_open = self.squelch == 0 or level >= self.squelch_db
        self.audio_open = self.signal_open
        tone_filter = self.tone_filter
        if tone_filter and self.signal_open:
            self.audio_open = tone_matches(tone_filter, detected_tone)
            if self.tone_deadline is None or self.audio_open:
                self.tone_deadline = now + TONE_ACQUIRE_SECONDS
            self.tone_matched = (self.audio_open if detected_tone is not None
                                 or now >= self.tone_deadline else None)
            if not self.received_signal and not self.audio_open and now < self.tone_deadline:
                return
        else:
            self.tone_deadline = None
            self.tone_matched = None
        if self.audio_open:
            self.waiting_reply = None
            self.received_signal = True
            return
        if (self.capture_hold if self.capture else self.hold) or now < self.next_advance:
            return
        if self.received_signal:
            if self.waiting_reply is None:
                self.waiting_reply = now + REPLY_SECONDS
                return
            if now < self.waiting_reply:
                return
        self._clear_signal()
        if self.capture:
            self._drop_capture()
            if self.close_mode == "only":
                self._retune(None)
                return
        if not self.hold:
            self._advance()

    def take_close_probe(self):
        request = self.probe_request
        self.probe_request = None
        return request

    def close_hit(self, hz):
        if (type(hz) is not int or not self.power or self.menu or self.capture or self.selected_scan_slots is not None
                or self.close_mode == "off" or (self.close_mode == "dnd" and self.signal_open)):
            return False
        group = next(((index, lower, upper) for index, (_, lower, upper) in enumerate(CLOSE_GROUPS)
                      if lower <= hz <= upper and self.close_groups[index]), None)
        if group is None:
            return False
        _, lower, upper = group
        candidate_bands = (band for band in self.bands if lower <= band.lower <= upper)
        band = min(candidate_bands, key=lambda item: max(item.lower - hz, hz - item.upper, 0))
        step = self.air_step if band.modulation == "AM" and band.lower == 108_000_000 else self.general_step or band.step
        channel = Channel(band.nearest(hz, step), band.modulation)
        if self._lock_kind(channel):
            return False
        self.capture = channel
        self.capture_acknowledged = False
        self.probe_request = None
        self._clear_signal()
        self.received_signal = True
        if self.close_alert:
            self._tone("close")
        return True

    def _clock_display(self):
        if self.menu == "plan":
            return f"bAnd-{self.menu_index}"
        minutes = self.clock_edit_minutes if self.menu == "clock" else (self._clock_minutes() + self.clock_offset) % 1440
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def _menu_display(self, now):
        if self.menu == "program":
            if not self.program_bank or int((now - self.program_at) / 0.7) % 2:
                hz = self.dab_program_source["hz"] if self.dab_programming else self.program_source.hz
                return format_frequency(hz), None
            hz = self._slot_hz(self.program_bank, self.program_slot)
            return format_frequency(hz), self.program_slot + 1
        if self.menu in ("step_general", "step_sideband"):
            return STEP_LABELS[self.pending_step], None
        if self.menu == "step_air":
            return STEP_LABELS[self.pending_air_step], None
        if self.menu == "close":
            return ("C-C.bnd", "C-C.ALt")[self.menu_index], None
        if self.menu == "close_groups":
            return CLOSE_GROUPS[self.close_group_index][0], None
        if self.menu in ("close_group_value", "close_alert"):
            return "On" if self.menu_choice else "OFF", None
        return "", None

    def _display(self, now):
        channel = self.tuned
        if not self.power:
            return self._clock_display(), None
        if self.notice_until > now:
            return self.notice, None
        if self.menu:
            return self._menu_display(now)
        if mode := self.favorite_edit:
            return ("SEL FAV" if mode == "scan" else "dEL FAV"), None
        if self.preview_until:
            return self.band_search_range or ("ALL" if self.full_search else self.band.label), None
        if self.capture and not self.capture_acknowledged:
            group = next(index for index, (_, lower, upper) in enumerate(CLOSE_GROUPS, 1)
                         if lower <= channel.hz <= upper)
            return f"b{group} Found", None
        if self.close_mode == "only" and not self.capture:
            return "-------", None
        if self._scanning_presets():
            return "SCAN", None
        if channel:
            number = channel.slot + 1 if channel.slot is not None else channel.preset_number
            return format_frequency(channel.hz), number
        return "-------", None

    def _scanning_presets(self):
        return (self.activity == "scan" and self.scan_banks and self.current and self.close_mode != "only"
                and not (self.hold or self.received_signal or self.capture or self.menu))

    def snapshot(self, *, parked=False, parked_backlight=False):
        """Current LCD and tuner state, with fresh containers for the HTTP layer."""
        now = monotonic()
        power = self.power and not parked
        channel = None if parked else self.tuned
        clock_backlight = self.menu in ("clock", "plan") or now < self.clock_light_until
        if parked:
            display, number = self._clock_display(), None
            backlight = parked_backlight or (not self.power and clock_backlight)
        else:
            display, number = self._display(now)
            backlight = power or clock_backlight
        upper = []
        lower = []
        blink_bank = None
        banks = self.scan_banks
        selected_scan = self.selected_scan_slots is not None
        favorite_edit = self.favorite_edit if power else None
        if power:
            if (self.activity == "scan" and self.close_mode != "only") or self.menu == "program":
                upper = [bank for bank in BANKS if bank in banks and bank not in ("CBAM", "CBFM")]
                if banks.intersection({"CBAM", "CBFM"}):
                    upper.append("CB")
                if self.program_bank and self.menu == "program" and self.program_bank not in upper:
                    upper.append(self.program_bank)
                if display == "SCAN" and channel and channel.bank:
                    blink_bank = "CB" if channel.bank.startswith("CB") else channel.bank
            if self.activity == "search" and self.band_search and self.band_search not in upper:
                upper.append(self.band_search)
            if channel and channel.hz:
                if self.hold and channel.bank == "FAV" and "FAV" not in upper:
                    upper.append("FAV")
                if (self.activity == "scan" and not self.hold and not self.menu
                        and channel.bank in ("AIR", "MRN") and {"AIR", "MRN"} <= self.enabled):
                    upper.extend(("AM", "NFM"))
                else:
                    upper.append("NFM" if channel.modulation == "FM" else channel.modulation)
            if self.close_mode != "off":
                lower.append("CLOSE")
            if self.close_mode != "only" and not self.direct_frequency:
                if self.activity == "scan" and banks and channel:
                    lower.append("SCAN")
                    if not (self.hold or self.capture_hold or self.menu):
                        lower.append("▲" if self.scan_direction > 0 else "▼")
                elif self.activity == "search":
                    lower.append("SEARCH")
                    lower.append("▲" if self.direction > 0 else "▼")
            if self.hold or self.capture_hold or self.menu == "program":
                lower.append("HOLD")
            if self.menu == "program":
                lower.append("CH")
            lock = self._lock_kind(channel) or (self.lock_notice if now < self.lock_notice_until else 0)
            if lock == 1:
                lower.append("T")
            if lock:
                lower.append("L/O")
        tune_hz = channel.hz if power and channel and channel.hz else None
        active_favorite = (channel.slot + 1 if tune_hz is not None and channel.bank == "FAV"
                           and channel.slot is not None and 0 <= channel.slot < FAVORITE_SLOTS else None)
        tone_filter = self.tone_filter if power else None
        return {
            "display": display,
            "channel": number,
            "upper": upper,
            "lower": lower,
            "blink_bank": blink_bank,
            "blink": self.menu == "clock" or bool(not parked and self.capture and not self.capture_acknowledged),
            "close_blink": not parked and self.close_mode == "only",
            "scan_scroll": display == "SCAN",
            "scan_active": bool(tune_hz is not None and (display == "SCAN"
                                or favorite_edit and self._scanning_presets())),
            "search_active": bool(tune_hz and self.activity == "search" and not self.hold
                                  and not self.capture and not self.received_signal and not self.menu
                                  and self.notice_until <= now),
            "search_full": not parked and self.activity == "search" and self.full_search,
            "band_search": self.band_search if power and self.activity == "search" else None,
            "band_search_range": self.band_search_range if power and self.activity == "search" else None,
            "backlight": backlight,
            "volume": 0.0 if parked else self.volume,
            "muted": self.muted and not parked,
            "squelch": self.squelch,
            "power": power,
            "can_tune": power and not self.menu,
            "tune_hz": tune_hz,
            "modulation": channel.modulation if channel else "FM",
            "sideband": self.sideband if power else None,
            "favorites": self._favorite_entries(),
            "dab_favorites": [entry.copy() if entry else None for entry in self.dab_favorites],
            "dab_station_count": sum(self.dab_service_counts.values()),
            "favorite_names": self.favorite_names.copy(),
            "active_favorite": active_favorite,
            "tone_filter": tone_filter.copy() if tone_filter else None,
            "tone_matched": self.tone_matched if tone_filter else None,
            "favorite_edit": favorite_edit,
            "scan_favorites": [entry.slot + 1 for entry in self.scan_list if not self._lock_kind(entry)]
                              if power and selected_scan else [],
            "scan_favorites_running": power and selected_scan and not (self.hold or self.capture),
            "rds_enabled": self.rds_enabled and not parked,
            "squelch_db": self.squelch_db,
            "close_probe": list(self.probe_request) if not parked and self.probe_request else None,
            "audio_open": power and self.audio_gate_open and not self.muted,
            "tone": dict(self.tone),
        }

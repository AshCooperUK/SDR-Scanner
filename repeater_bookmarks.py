"""Receive frequencies from the UK Repeater directory published by RSGB ETCC."""

import json
import threading
import time
from urllib.request import urlopen

from scanner import MANUAL_MAX_HZ, MANUAL_MIN_HZ


REPEATER_ROUTE = "/api/bookmarks/uk/"
REPEATER_BANDS = ("10m", "6m", "4m", "2m", "70cm", "23cm")
_API_BASE = "https://api-beta.rsgb.online/band/"
_MAX_BYTES = 1024 * 1024
_CACHE_SECONDS = 24 * 60 * 60
_MODES = {"A": "FM", "M": "DMR", "D": "D-STAR", "N": "NXDN-VN", "P": "P25"}


class RepeaterDirectoryError(Exception):
    pass


def _label(value, limit):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def repeater_entries(payload):
    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError("Invalid repeater directory")
    entries = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        callsign = _label(record.get("repeater"), 20)
        hz = record.get("tx")  # In the ETCC API, TX and RX are from the repeater's perspective.
        codes = record.get("modeCodes")
        if (not callsign or type(hz) is not int or not MANUAL_MIN_HZ <= hz <= MANUAL_MAX_HZ
                or not isinstance(codes, list)):
            continue
        modes = list(dict.fromkeys(_MODES[code.partition(":")[0]] for code in codes
                                   if isinstance(code, str) and code.partition(":")[0] in _MODES))
        if not modes:
            continue
        key = (callsign, hz)
        if key in entries:
            for mode in modes:
                if mode not in entries[key]["modes"]:
                    entries[key]["modes"].append(mode)
            continue
        entries[key] = {
            "callsign": callsign, "hz": hz, "modes": modes,
            "town": _label(record.get("town"), 80),
            "locator": _label(record.get("locator"), 8),
            "status": _label(record.get("status"), 30),
        }
    return sorted(entries.values(), key=lambda entry: (entry["callsign"], entry["hz"]))


class RepeaterDirectory:
    def __init__(self):
        self.cache = {}
        self.locks = {band: threading.Lock() for band in REPEATER_BANDS}

    def band(self, band):
        with self.locks[band]:
            expires_at, entries = self.cache.get(band, (0, None))
            if time.monotonic() < expires_at:
                return entries
            try:
                with urlopen(_API_BASE + band, timeout=3) as response:
                    data = response.read(_MAX_BYTES + 1)
                if len(data) > _MAX_BYTES:
                    raise ValueError("Repeater directory response is too large")
                updated = repeater_entries(json.loads(data))
            except (OSError, ValueError) as exc:
                if entries is None:
                    raise RepeaterDirectoryError("UK Repeater is unavailable. Try again shortly.") from exc
                self.cache[band] = (time.monotonic() + 60, entries)
                return entries
            self.cache[band] = (time.monotonic() + _CACHE_SECONDS, updated)
            return updated

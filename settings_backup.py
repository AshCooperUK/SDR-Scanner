"""Owner-only configuration backups, separate from recording and replay media."""

from contextlib import ExitStack
from copy import deepcopy
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from access_pin import AccessPin
from dmr import DmrState, SCOPES, SEARCH_STEPS
from recording import Recorder
from scanner import FAVORITE_COUNT, MANUAL_MAX_HZ, MANUAL_MIN_HZ, Scanner
from v10 import AUTO_GROUP, FILE_LIMIT, VFO_MODES, ReceiverPanel, valid_backup


FORMAT = "sdr-scanner-server-settings"


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def scanner_document(scanner):
    """Use the scanner's existing on-disk representation without exposing internals in the UI."""
    memories = {"PVT": scanner.memories["PVT"], "EMG": scanner.memories["FAV"][FAVORITE_COUNT:],
                "FRN": scanner.archived_frn}
    locks = [("EMG", slot - FAVORITE_COUNT) if bank == "FAV" and slot >= FAVORITE_COUNT else (bank, slot)
             for bank, slot in scanner.permanent_slots]
    locks.extend(("FRN", slot) for slot in scanner.archived_frn_locks)
    data = {"plan": scanner.plan, "general_step": scanner.general_step, "air_step": scanner.air_step,
            "band_index": scanner.band_index, "clock_offset": scanner.clock_offset,
            "close_groups": scanner.close_groups, "close_alert": scanner.close_alert,
            "program_locked": scanner.program_locked, "rds_enabled": scanner.rds_enabled,
            "favorites": scanner._favorite_entries(), "dab_favorites": scanner.dab_favorites,
            "dab_last_station": scanner.dab_last_station, "dab_known_blocks": scanner.dab_known_blocks,
            "dab_service_counts": scanner.dab_service_counts, "dab_station_ids": scanner.dab_station_ids,
            "favorite_names": scanner.favorite_names[:FAVORITE_COUNT],
            "permanent_frequencies": list(scanner.permanent_frequencies), "permanent_slots": sorted(locks),
            "memories": {bank: [[entry.hz, entry.modulation] if entry else None for entry in entries]
                         for bank, entries in memories.items()}}
    if any(names := scanner.favorite_names[FAVORITE_COUNT:]):
        data["extended_favorite_names"] = names
    if any(slot >= FAVORITE_COUNT for slot in scanner.favorite_tones):
        data["extended_favorite_tones"] = [scanner.favorite_tones.get(slot)
                                           for slot in range(FAVORITE_COUNT, len(scanner.favorite_names))]
    if scanner.zone_overrides:
        data["zone_overrides"] = {bank: scanner._chirp_zone_entries(entries)
                                  for bank, entries in scanner.zone_overrides.items()}
    if scanner.recording_parent is not None:
        data["recording_parent"] = str(scanner.recording_parent)
    return _json_copy(data)


def dmr_document(dmr):
    return _json_copy({"version": 1, "entries": list(dmr.entries.values()), "avoids": sorted(dmr.permanent_avoids)})


def server_backup(runtime, pin):
    with pin.guard(), runtime.lock:
        hz, mode = runtime._v10_current()
        if runtime.v10.automatic_mode:
            mode = "AUTO"
        return {"format": FORMAT, "version": 1, "receiver": runtime.v10.backup(),
                "scanner": scanner_document(runtime.scanner), "dmr": dmr_document(runtime.dmr),
                "runtime": {"squelch": runtime.scanner.squelch, "volume": runtime.scanner.volume,
                            "history_logging": runtime.discovery._logging["v10"] is True, "tune": {"hz": hz, "mode": mode},
                            "frequency_mode": runtime.v10.frequency_mode,
                            "group": runtime.v10.group, "slot": runtime.v10.slot,
                            "dmr": {"search": list(runtime.dmr.search), "direction": runtime.dmr.direction,
                                    "hold_scope": runtime.dmr.hold_scope}},
                "access": pin.backup()}


def validate_server_backup(data):
    """Reject ignored, coerced or partial settings before touching the live receiver."""
    if (not isinstance(data, dict)
            or data.keys() != {"format", "version", "receiver", "scanner", "dmr", "runtime", "access"}
            or data["format"] != FORMAT or type(data["version"]) is not int or data["version"] != 1
            or not valid_backup(data["receiver"])):
        raise ValueError("Choose a supported complete SDr Scanner settings backup.")
    AccessPin.validate_backup(data["access"])
    live = data["runtime"]
    if (not isinstance(live, dict)
            or live.keys() != {"squelch", "volume", "history_logging", "tune", "frequency_mode", "group", "slot", "dmr"}
            or any(type(live[key]) not in (int, float) or not math.isfinite(live[key]) or not 0 <= live[key] <= 1
                   for key in ("squelch", "volume"))
            or type(live["history_logging"]) is not bool or live["frequency_mode"] not in ("vfo", "memory")
            or type(live["group"]) is not int or not 0 <= live["group"] <= AUTO_GROUP
            or type(live["slot"]) is not int or not 0 <= live["slot"] < 200):
        raise ValueError("Invalid listening settings in backup.")
    tune = live["tune"]
    if (not isinstance(tune, dict) or tune.keys() != {"hz", "mode"} or type(tune["hz"]) is not int
            or not MANUAL_MIN_HZ <= tune["hz"] <= MANUAL_MAX_HZ or tune["mode"] not in VFO_MODES):
        raise ValueError("Invalid tuning settings in backup.")
    dmr_live = live["dmr"]
    if (not isinstance(dmr_live, dict) or dmr_live.keys() != {"search", "direction", "hold_scope"}
            or type(dmr_live["direction"]) is not int or dmr_live["direction"] not in (-1, 1)
            or dmr_live["hold_scope"] not in SCOPES or not isinstance(dmr_live["search"], list)
            or len(dmr_live["search"]) != 3 or any(type(value) is not int for value in dmr_live["search"])
            or not MANUAL_MIN_HZ <= dmr_live["search"][0] <= dmr_live["search"][1] <= MANUAL_MAX_HZ
            or dmr_live["search"][2] not in SEARCH_STEPS):
        raise ValueError("Invalid digital search settings in backup.")
    # Existing loaders enforce the scanner and DMR schemas. Comparing their canonical
    # exports rejects fields they would otherwise ignore or replace with defaults.
    with TemporaryDirectory(prefix="sdr-settings-check-") as folder:
        root = Path(folder)
        scanner_path, dmr_path = root / "scanner.json", root / "dmr.json"
        scanner_path.write_text(json.dumps(data["scanner"]))
        dmr_path.write_text(json.dumps(data["dmr"]))
        scanner, dmr = Scanner(scanner_path), DmrState(dmr_path)
        if scanner_document(scanner) != data["scanner"] or dmr_document(dmr) != data["dmr"]:
            raise ValueError("Invalid or incomplete scanner settings in backup.")
    for field in ("receiver", "scanner", "dmr"):
        if len(json.dumps(data[field]).encode("utf-8")) > FILE_LIMIT:
            raise ValueError("Settings backup exceeds the programming size limit.")
    return deepcopy(data)


def _stage_bytes(path, payload, cleanup):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("wb", dir=path.parent, prefix=path.name + ".", delete=False) as stream:
        pending = Path(stream.name)
        cleanup.callback(pending.unlink, missing_ok=True)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return pending


def _stage(path, data, cleanup):
    return _stage_bytes(path, json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), cleanup)


def restore_server_backup(runtime, pin, data):
    """Run on the ordered recording worker; all validation precedes live changes."""
    with pin.guard(), runtime.lock, ExitStack() as cleanup:
        if runtime.stopped.is_set() or runtime.recording.choosing:
            raise ValueError("Finish the current folder change before restoring settings.")
        scanner, panel, dmr = runtime.scanner, runtime.v10, runtime.dmr
        paths = [(scanner.state_path, data["scanner"]), (panel.path, data["receiver"]), (dmr.path, data["dmr"])]
        staged = [(path, _stage(path, image, cleanup)) for path, image in paths]
        restored_scanner = Scanner(staged[0][1])
        restored_dmr = DmrState(staged[2][1])
        restored_panel = ReceiverPanel(staged[1][1], restored_scanner, restored_dmr)
        parent = restored_scanner.recording_parent or scanner.state_path.parent.resolve() / "recordings"
        if restored_scanner.recording_parent is None:
            parent.mkdir(parents=True, exist_ok=True)
        recorder = Recorder(parent)
        # A removed/unmounted recording directory fails before programming is replaced.
        recorder.set_parent(parent, v10=True)
        originals = {path: path.read_bytes() if path.exists() else None for path, _ in staged}
        committed = []
        with pin.restore_backup(data["access"]) as commit_access:
            runtime.recording.recorder.finish()
            try:
                for path, pending in staged:
                    pending.replace(path)
                    committed.append(path)
                commit_access()
            except OSError:
                for path in reversed(committed):
                    old = originals[path]
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        _stage_bytes(path, old, cleanup).replace(path)
                raise
        # The disk transaction is complete. Keep all receive/scan/record operations stopped.
        runtime.recording.enabled = False
        runtime.recording._epoch += 1
        runtime.recording._capture = None
        runtime.recording._writing = False
        runtime.recording.recorder = recorder
        runtime.recording.parent = parent
        runtime.recording.message = "Settings restored. Recording is stopped."
        restored_scanner.state_path = scanner.state_path
        scanner.__dict__.update(restored_scanner.__dict__)
        restored_dmr.path = dmr.path
        runtime.dmr = restored_dmr
        restored_dmr.search = tuple(data["runtime"]["dmr"]["search"])
        restored_dmr.direction = data["runtime"]["dmr"]["direction"]
        restored_dmr.hold_scope = data["runtime"]["dmr"]["hold_scope"]
        restored_panel.path = panel.path
        runtime.v10 = restored_panel
        live = data["runtime"]
        scanner.squelch = live["squelch"]
        runtime._v10_activate()
        runtime.discovery._logging["v10"] = live["history_logging"]
        runtime.discovery.boundary()
        runtime._v10_vfo(live["tune"]["hz"], live["tune"]["mode"])
        if live["frequency_mode"] == "memory" and (live["group"], live["slot"]) in restored_panel.memories:
            restored_panel.frequency_mode = "memory"
            restored_panel.group, restored_panel.slot = live["group"], live["slot"]
            restored_panel.active_memory = restored_panel.memories[live["group"], live["slot"]].copy()
        runtime.discard_audio()
        runtime.signal = runtime.waterfall = runtime.tone_result = None
        runtime.rf_replay.clear_live()
        runtime.wake.set()

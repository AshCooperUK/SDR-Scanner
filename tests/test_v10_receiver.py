"""Regressions for the V10 receiver's programming and DSP."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from app import ScannerRuntime, valid_action
from fsk import LETTERS, RttyDecoder
from receiver import IQ_RATE, IQ_SAMPLES, SAMPLE_RATE, Receiver, _ChannelDecoder
from scanner import MANUAL_MAX_HZ, MANUAL_MIN_HZ
from v10 import AUTO_GROUP


class ProgrammingTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "state.json"
        self.runtime = ScannerRuntime(self.path)
        self.addCleanup(self.runtime.close)
        self.runtime.act({"knob": "volume", "value": .5, "panel": "v10"})

    def act(self, command, **values):
        request = {"v10": command, **values}
        self.assertTrue(valid_action(request), request)
        result = self.runtime.act(request)
        self.assertIsNone(result["v10_error"])
        return result

    def write(self, slot, frequency, mode, **fields):
        return self.act("memory", operation="write", group=0, slot=slot,
                        hz=frequency, mode=mode, **fields)

    def test_rx_history_records_before_opening_and_preserves_pause(self):
        state = self.act("tune", hz=128_125_000, mode="AM")
        self.assertTrue(state["discovery_started"])
        self.assertTrue(state["discovery_enabled"])
        pcm = bytes(960)
        self.runtime.discovery.receive(pcm, channel=(128_125_000, "AM"))
        self.assertEqual(self.runtime.discovery.status()["hits"][0]["hz"], 128_125_000)

        self.assertFalse(self.runtime.act({"discovery": "toggle"})["discovery_enabled"])
        self.assertFalse(self.runtime.act({"discovery": "open"})["discovery_enabled"])
        state = self.act("tune", hz=446_006_250, mode="FM")
        self.assertFalse(state["discovery_enabled"])
        self.runtime.discovery.receive(pcm, channel=(446_006_250, "FM"))
        self.assertEqual(len(self.runtime.discovery.status()["hits"]), 1)
        self.assertTrue(self.runtime.discovery.replay_available)
        self.runtime.act({"knob": "volume", "value": 0, "panel": "v10"})
        state = self.runtime.act({"knob": "volume", "value": .5, "panel": "v10"})
        self.assertFalse(state["discovery_enabled"])
        self.assertTrue(self.runtime.act({"discovery": "toggle"})["discovery_enabled"])

    def test_rx_history_retains_late_tone_station_and_signal_measurements_in_one_hit(self):
        discovery = self.runtime.discovery
        pcm = bytes(960)
        channel = (446_006_250, "FM")
        discovery.receive(pcm, channel=channel, revision=1, signal_dbfs=-64.5)
        tone = {"kind": "CTCSS", "hz": 123.0}
        discovery.receive(pcm, channel=channel, revision=1, tone=tone, station_name="Local channel",
                          signal_dbfs=-55.5, snr_db=24.5, offset_hz=-42.6, mode_detected=True)
        discovery.receive(pcm, channel=channel, revision=1, signal_dbfs=-62.0, snr_db=18.0,
                          offset_hz=float("nan"))
        hit, = discovery.status()["hits"]
        self.assertEqual((hit["hz"], hit["mode"], hit["mode_detected"]), (*channel, True))
        self.assertEqual((hit["tone"], hit["station_name"]), (tone, "Local channel"))
        self.assertEqual((hit["signal_dbfs"], hit["snr_db"], hit["offset_hz"]), (-55.5, 24.5, -42.6))
        self.assertEqual(hit["duration_ms"], 60)
        self.assertEqual(discovery.audio(hit["id"]), (pcm, pcm, pcm))

        dcs = {"kind": "DCS", "code": "023", "polarity": "N"}
        discovery.receive(pcm, channel=channel, revision=2, tone=dcs)
        hit, previous = discovery.status()["hits"]
        self.assertFalse(hit["mode_detected"])
        self.assertEqual((hit["tone"], previous["tone"]), (dcs, tone))

    def test_digital_rx_history_receives_protocol_specific_observed_fields(self):
        self.act("tune", hz=443_012_500, mode="P25")
        radio = self.runtime
        def receive(**metadata):
            radio._receive_digital((443_012_500, "P25"), radio.dmr.revision, radio._dab_epoch,
                                   radio.scanner.squelch_db + 12, {
                                       "voice": True, "sync": True, "encrypted": False,
                                       "receive_kind": "NAC", **metadata,
                                   }, bytes(960), None)

        receive(talkgroup=132, unit_id=1234567)
        receive(talkgroup=132, unit_id=1234567, receive_code=0x293, source="MYCALL",
                decoder_private="Not published")
        hit, = radio.discovery.status()["hits"]
        self.assertEqual((hit["mode"], hit["mode_detected"], hit["dmr"]), ("P25", True, None))
        self.assertEqual(hit["digital"], {
            "receive_kind": "NAC", "receive_code": 0x293, "talkgroup": 132,
            "unit_id": 1234567, "source": "MYCALL", "encrypted": False,
        })
        self.assertEqual(hit["duration_ms"], 40)
        receive(talkgroup=132, unit_id=7654321)
        next_hit, previous = radio.discovery.status()["hits"]
        self.assertEqual((next_hit["digital"]["unit_id"], previous["duration_ms"]), (7654321, 40))

    def test_rx_history_pause_survives_tuner_handoff_with_separate_legacy_setting(self):
        standby_receiver = ScannerRuntime(self.path.parent / "spare.json", receiver_index=1)
        self.addCleanup(standby_receiver.close)
        state = self.act("tune", hz=128_125_000, mode="AM")
        standby_receiver.resume_v10(state, .5)
        self.assertTrue(standby_receiver.state()["discovery_enabled"])
        state = self.runtime.act({"discovery": "toggle"})
        standby_receiver.resume_v10(state, .5)
        self.assertFalse(standby_receiver.state()["discovery_enabled"])

        state = self.runtime.act({"tune": "frequency", "mhz": "128.125"})
        self.assertFalse(state["discovery_started"])
        self.assertFalse(state["discovery_enabled"])
        self.assertTrue(self.runtime.act({"discovery": "open"})["discovery_enabled"])
        self.assertFalse(self.act("tune", hz=128_125_000, mode="AM")["discovery_enabled"])
        state = self.runtime.act({"tune": "frequency", "mhz": "128.125"})
        self.assertTrue(state["discovery_enabled"])

    def test_each_memory_mode_and_receive_step_persist(self):
        self.write(1, 102_400_000, "WFM", name="Broadcast", selected=2)
        self.write(3, 446_006_250, "DMR", name="Test", color_code=1, timeslot=2)
        state = self.act("memory", operation="recall", group=0, slot=1)
        self.assertEqual((state["tune_hz"], state["modulation"]), (102_400_000, "WFM"))
        state = self.act("step", direction=1)
        self.assertTrue(state["dmr_enabled"])
        self.assertEqual(state["tune_hz"], 446_006_250)
        self.assertEqual(self.runtime.v10.active_memory["timeslot"], 2)
        reloaded = ScannerRuntime(self.path)
        self.addCleanup(reloaded.close)
        self.assertEqual(reloaded.v10.memories[(0, 1)]["selected"], 2)
        self.assertEqual(reloaded.v10.memories[(0, 3)]["name"], "Test")

    def test_favourite_clear_confirms_the_same_memory_and_bank(self):
        self.write(6, 128_125_000, "AM", name="Air")
        self.act("memory", operation="write", group=1, slot=6, hz=443_006_250, mode="FM", name="UHF")
        panel = self.runtime.v10
        original = panel.memories[(0, 6)].copy()
        delete = {"v10": "memory", "operation": "delete", "group": 0, "slot": 6, "expected": original}
        self.assertTrue(valid_action(delete))
        self.assertFalse(valid_action({**delete, "group": 1}))
        self.write(6, 128_150_000, "AM", name="Replacement")
        result = self.runtime.act(delete)
        self.assertIn("memory changed", result["v10_error"])
        self.assertEqual(panel.memories[(0, 6)]["hz"], 128_150_000)
        self.act("memory", operation="delete", group=0, slot=6, expected=panel.memories[(0, 6)].copy())
        self.assertNotIn((0, 6), panel.memories)
        self.assertEqual(panel.memories[(1, 6)]["hz"], 443_006_250)

    def test_versioned_backup_round_trip(self):
        self.write(4, 162_000_000, "FM", name="Harbour")
        self.act("setting", key="tone_bass", value=3, mode="FM")
        backup = self.runtime.v10.backup()
        self.assertNotIn("access", backup)
        self.act("reset", scope="all")
        self.assertEqual(self.runtime.v10.memories, {})
        state = self.act("restore", backup=backup)
        self.assertEqual(state["v10"]["settings_by_mode"]["FM"]["tone_bass"], 3)
        self.assertEqual(state["v10"]["memories"][0]["name"], "Harbour")
        self.assertFalse(valid_action({"v10": "restore", "backup": {**backup, "pin": "0000"}}))

    def test_fine_delta_and_recall(self):
        self.act("tune", hz=446_050_000, mode="FM")
        state = self.act("scan", kind="DELTA", enabled=True, span_hz=100_000)
        self.assertEqual(state["tune_hz"], 446_050_000)
        self.act("scan_control", operation="fine", enabled=True)
        with self.runtime.lock:
            self.runtime._v10_advance(self.runtime.v10.next_scan_at, True)
        self.assertEqual(self.runtime.state()["tune_hz"], 446_050_010)
        state = self.act("scan_control", operation="recall")
        self.assertEqual(state["tune_hz"], 446_050_000)
        self.assertFalse(state["search_active"])

    def test_edges_auto_group_and_skip_timer(self):
        self.act("edge", number=7, start_hz=145_400_000, end_hz=145_425_000, mode="FM", step=12_500)
        state = self.act("scan", kind="AUTO", enabled=True, edge=7)
        self.assertEqual(state["tune_hz"], 145_400_000)
        with self.runtime.lock:
            panel = self.runtime.v10
            now = panel.next_scan_at
            self.runtime._v10_advance(now, True)
            self.assertEqual(panel.memories[(AUTO_GROUP, 0)]["hz"], 145_400_000)
            panel.temporary_skip(145_400_000, now)
            self.assertAlmostEqual(panel.skipped[145_400_000] - now, 300)

    def test_settings_are_per_mode_and_timer_is_saved(self):
        self.write(0, 98_400_000, "WFM")
        self.act("setting", key="bandwidth_hz", value=500, mode="CW")
        state = self.act("tune", hz=7_030_000, mode="CW")
        self.assertEqual(state["v10"]["settings"]["bandwidth_hz"], 500)
        state = self.act("tune", hz=98_400_000, mode="WFM")
        self.assertIsNone(state["v10"]["settings"]["bandwidth_hz"])
        state = self.act("timer", enabled=True, on="13:15", off="14:15", group=0, slot=0,
                         weekday=0, repeat=True)
        self.assertTrue(state["v10"]["timer"]["enabled"])
        self.assertFalse(valid_action({"v10": "tune", "hz": 0, "mode": "CW"}))
        self.assertFalse(valid_action({"v10": "memory", "operation": "write", "group": 100, "slot": 200,
                                       "hz": 98_400_000, "mode": "WFM"}))

    def test_auto_vfo_uses_band_mode_and_keeps_manual_memories_explicit(self):
        state = self.act("tune", hz=128_125_000, mode="AUTO")
        self.assertEqual(state["modulation"], "AM")
        self.assertTrue(state["v10"]["capabilities"]["auto_mode"])
        self.assertTrue(state["v10"]["auto_mode"])
        self.assertEqual(state["v10"]["vfo"]["mode"], "AUTO")
        state = self.act("step", direction=1)
        self.assertEqual((state["tune_hz"], state["modulation"]), (128_137_500, "AM"))
        self.assertTrue(state["v10"]["auto_mode"])
        state = self.act("tune", hz=98_400_000, mode="AUTO")
        self.assertEqual(state["modulation"], "WFM")
        state = self.act("scan", kind="DELTA", enabled=True, span_hz=100_000, mode="FM")
        self.assertFalse(state["v10"]["auto_mode"])
        state = self.act("scan_control", operation="recall")
        self.assertEqual(state["modulation"], "WFM")
        self.assertTrue(state["v10"]["auto_mode"])
        self.write(1, 128_125_000, "FM")
        state = self.act("memory", group=0, operation="recall", slot=1)
        self.assertEqual(state["modulation"], "FM")
        self.assertFalse(state["v10"]["auto_mode"])
        state = self.act("frequency_mode", value="vfo")
        self.assertTrue(state["v10"]["auto_mode"])
        self.assertEqual(state["modulation"], "WFM")
        self.assertFalse(valid_action({"v10": "memory", "operation": "write", "group": 0,
                                       "slot": 2, "hz": 128_125_000, "mode": "AUTO"}))

    def test_custom_scan_includes_its_endpoints_and_keeps_position_on_hold(self):
        self.act("zone", operation="configure", zone="CUSTOM1", name=" Air local ",
                 start_hz=128_125_000, end_hz=128_145_000)
        state = self.act("scan", kind="ZONE", zone="CUSTOM1", enabled=True)
        self.assertEqual((state["tune_hz"], state["modulation"]), (128_125_000, "AM"))
        self.assertTrue(state["v10"]["auto_mode"])
        self.assertEqual(state["v10"]["custom_presets"]["CUSTOM1"]["name"], "Air local")
        panel = self.runtime.v10
        self.act("setting", key="tune_step", value=1_000_000)
        with self.runtime.lock:
            now = panel.next_scan_at
            self.runtime._v10_advance(now, False)
            self.runtime._v10_advance(now + 1, False)
        self.assertEqual(self.runtime.state()["tune_hz"], 128_133_333)
        self.act("scan_control", operation="inspect", hz=128_142_000)
        self.assertEqual(self.act("scan_control", operation="resume")["tune_hz"], 128_133_333)
        self.assertEqual(self.act("step", direction=1)["tune_hz"], 128_141_667)
        self.assertEqual(self.act("step", direction=1)["tune_hz"], 128_145_000)
        self.assertEqual(self.act("step", direction=-1)["tune_hz"], 128_141_667)
        self.assertEqual(self.act("step", direction=1)["tune_hz"], 128_145_000)
        self.assertEqual(self.act("step", direction=1)["tune_hz"], 128_125_000)
        state = self.act("zone", operation="configure", zone="CUSTOM1", name="PMR local",
                         start_hz=446_006_250, end_hz=446_031_250, step_hz=2_500)
        self.assertEqual((state["tune_hz"], state["modulation"]), (446_006_250, "FM"))
        self.assertEqual(state["v10"]["scan_zones"], ["CUSTOM1"])

    def test_full_range_scan_uses_segment_grids_without_changing_programmed_channel_ids(self):
        panel = self.runtime.v10
        for zone, key in (("HAM", "ham_full_range"), ("BUS", "bus_full_range")):
            with self.subTest(zone=zone):
                original = panel.zone_entries(zone)
                self.act("scan", kind="ZONE", zone=zone, enabled=True)
                self.assertEqual(len(panel.scan["points"]), len(original))
                self.act("setting", key=key, value=True)
                self.assertEqual(panel.zone_entries(zone), original)
                points = [panel.zone_entry(*point) for point in panel.scan["points"]]
                self.assertEqual(points[0]["hz"], original[0]["hz"])
                self.assertGreater(len(points), len(original))
                frequencies = {entry["hz"] for entry in points}
                if zone == "HAM":
                    self.assertTrue({14_150_000, 14_151_000, 14_152_000} <= frequencies)
                else:
                    self.assertTrue({166_900_000, 166_906_250, 166_912_500} <= frequencies)
                self.act("scan", kind="ZONE", enabled=False)

    def test_custom_configuration_is_bounded_saved_and_backup_compatible(self):
        command = {"v10": "zone", "operation": "configure", "zone": "CUSTOM1", "name": "Full span",
                   "start_hz": MANUAL_MIN_HZ, "end_hz": MANUAL_MAX_HZ}
        for invalid in ({"zone": "CUSTOM13"}, {"name": " "}, {"name": "x" * 25}, {"end_hz": MANUAL_MIN_HZ},
                        {"end_hz": MANUAL_MAX_HZ + 1}, {"step_hz": True}, {"step_hz": 1_000_000}):
            with self.subTest(invalid=invalid):
                self.assertFalse(valid_action({**command, **invalid}))
        self.act("zone", **{key: value for key, value in command.items() if key != "v10"})
        self.act("zone", operation="configure", zone="CUSTOM2", name="UHF local",
                 start_hz=446_006_250, end_hz=446_031_250)
        self.act("zone", operation="configure", zone="CUSTOM12", name="Air local",
                 start_hz=128_125_000, end_hz=128_150_000)
        self.act("setting", key="tune_step", value=1)
        state = self.act("scan", kind="ZONE", zones=["CUSTOM1", "CUSTOM2", "CUSTOM12", "PMR"], enabled=True)
        self.assertEqual(state["tune_hz"], MANUAL_MIN_HZ)
        self.assertLess(len(self.runtime.v10.scan["points"]), 100)
        self.assertEqual(self.act("step", direction=1)["tune_hz"], 446_006_250)
        self.assertEqual(self.act("step", direction=1)["tune_hz"], 128_125_000)
        reloaded = ScannerRuntime(self.path)
        self.addCleanup(reloaded.close)
        backup = self.runtime.v10.backup()
        self.assertEqual(reloaded.v10.custom_presets, backup["custom_presets"])
        self.assertEqual(len(backup["custom_presets"]), 3)
        self.assertTrue(valid_action({"v10": "restore", "backup": backup}))
        del backup["custom_presets"]
        state = self.act("restore", backup=backup)
        self.assertEqual(state["v10"]["custom_presets"], {})

    def test_factory_presets_have_independent_persistent_edits_and_stable_ids(self):
        defaults = self.runtime.v10.zone_entries("PMR")
        self.assertEqual([entry["index"] for entry in defaults if entry["mode"] == "FM"], list(range(16)))
        self.assertEqual(defaults[0]["hz"], 446_006_250)
        self.act("zone", zone="PMR", operation="write", index=0, hz=446_012_500, mode="FM", name="Custom")
        self.act("zone", zone="PMR", operation="delete", index=1)
        self.act("zone", zone="PMR", operation="write", hz=446_200_000, mode="FM")
        presets = self.runtime.v10.zone_entries("PMR")
        self.assertEqual(presets[1], defaults[2])
        self.assertEqual(self.runtime.scanner.factory["PMR"][0].hz, defaults[0]["hz"])
        state = self.act("zone", zone="PMR", operation="recall", index=presets[-1]["index"])
        self.assertEqual((state["tune_hz"], state["modulation"]), (446_200_000, "FM"))
        reloaded = ScannerRuntime(self.path)
        self.addCleanup(reloaded.close)
        self.assertEqual(reloaded.v10.zone_entries("PMR"), presets)
        backup = reloaded.v10.backup()
        self.assertTrue(valid_action({"v10": "restore", "backup": backup}))
        del backup["zones"]
        self.act("restore", backup=backup)
        self.assertEqual(self.runtime.v10.zone_entries("PMR"), defaults)

    def test_favorite_zone_banks_and_permanent_channel_lockout(self):
        self.write(0, 145_500_000, "FM", selected=2)
        self.act("memory", operation="write", group=1, slot=0, hz=128_125_000, mode="AM", name="Air")
        self.assertEqual([item["index"] for item in self.runtime.v10.zone_entries("FAV")], [0, 100])
        self.act("zone", zone="FAV", operation="write", index=0, hz=145_525_000, mode="FM", name="Renamed")
        self.assertEqual(self.runtime.v10.memories[(0, 0)]["selected"], 2)
        state = self.act("zone", zone="FAV", operation="recall", index=100)
        self.assertEqual((state["v10"]["group"], state["tune_hz"]), (1, 128_125_000))
        state = self.act("lockout", operation="toggle")
        self.assertTrue(state["v10"]["current_lockout"])
        self.assertTrue(self.runtime.v10.memories[(1, 0)]["skip"])
        state = self.act("lockout", operation="clear-temporary")
        self.assertTrue(state["v10"]["current_lockout"])
        self.assertFalse(self.act("lockout", operation="toggle")["v10"]["current_lockout"])

    def test_temporary_lockouts_skip_preset_frequencies_and_clear(self):
        presets = self.runtime.v10.zone_entries("PMR")
        self.act("tune", hz=presets[0]["hz"], mode="FM")
        self.assertTrue(self.act("lockout", operation="toggle")["v10"]["current_lockout"])
        state = self.act("scan", kind="ZONE", zone="PMR", enabled=True)
        self.assertEqual((state["v10"]["scan_zone"], state["tune_hz"]), ("PMR", presets[1]["hz"]))
        state = self.act("lockout", operation="toggle")
        self.assertEqual(state["tune_hz"], presets[2]["hz"])
        self.act("scan", kind="ZONE", enabled=False)
        self.act("zone", zone="PMR", operation="recall", index=0)
        self.assertTrue(self.runtime.state()["v10"]["current_lockout"])
        self.assertFalse(self.act("lockout", operation="clear-temporary")["v10"]["current_lockout"])

    def test_front_panel_locks_survive_a_scan_restart_and_clear_on_power_off(self):
        presets = self.runtime.v10.zone_entries("PMR")
        self.act("scan", kind="ZONE", zone="PMR", enabled=True)
        for index in range(6):
            self.assertEqual(self.runtime.state()["tune_hz"], presets[index]["hz"])
            self.act("lockout", operation="toggle")
        panel = self.runtime.v10
        later = panel.next_scan_at + 3600
        self.assertTrue(panel.frequency_locked(presets[0]["hz"], later))
        self.act("scan", kind="ZONE", enabled=False)
        with patch("app.time.monotonic", return_value=later):
            self.assertEqual(self.act("scan", kind="ZONE", zone="PMR", enabled=True)["tune_hz"], presets[6]["hz"])

        self.runtime.act({"knob": "volume", "value": 0, "panel": "v10"})
        self.runtime.act({"knob": "volume", "value": .5, "panel": "v10"})
        self.assertEqual(self.act("scan", kind="ZONE", zone="PMR", enabled=True)["tune_hz"], presets[0]["hz"])

    def test_timed_skip_survives_a_scan_restart_until_its_expiry(self):
        presets = self.runtime.v10.zone_entries("PMR")
        self.act("scan", kind="ZONE", zone="PMR", enabled=True)
        panel = self.runtime.v10
        with self.runtime.lock:
            start = panel.next_scan_at + .01
            self.runtime._v10_advance(start, True)
        with patch("app.time.monotonic", return_value=start):
            self.assertEqual(self.act("skip")["tune_hz"], presets[1]["hz"])
        self.act("scan", kind="ZONE", enabled=False)
        with patch("app.time.monotonic", return_value=start + 299):
            self.assertEqual(self.act("scan", kind="ZONE", zone="PMR", enabled=True)["tune_hz"], presets[1]["hz"])
        self.act("scan", kind="ZONE", enabled=False)
        with patch("app.time.monotonic", return_value=start + 300):
            self.assertEqual(self.act("scan", kind="ZONE", zone="PMR", enabled=True)["tune_hz"], presets[0]["hz"])

    def test_all_locked_presets_are_silent_until_hold_or_an_eligible_channel(self):
        radio = self.runtime
        panel = radio.v10
        self.write(0, 128_125_000, "AM")
        self.act("scan", kind="ZONE", zone="FAV", enabled=True)
        level = radio.scanner.squelch_db + 10
        radio.scanner.tick(level)
        self.assertTrue(radio.state()["audio_open"])
        self.assertFalse(self.act("lockout", operation="toggle")["audio_open"])
        self.assertTrue(panel.scan_waiting)
        radio.scanner.tick(level)
        self.assertFalse(radio.state()["audio_open"])

        self.act("scan_control", operation="hold")
        self.assertTrue(radio.state()["audio_open"])
        self.assertFalse(self.act("scan_control", operation="resume")["audio_open"])
        self.assertTrue(panel.scan_waiting)
        self.act("lockout", operation="clear-temporary")
        with radio.lock:
            radio._v10_advance(panel.next_scan_at, False)
        self.assertFalse(panel.scan_waiting)
        self.assertEqual(radio.state()["tune_hz"], 128_125_000)

    def test_preset_scan_keeps_position_through_zone_changes_and_scope_inspection(self):
        pmr = self.runtime.v10.zone_entries("PMR")
        self.write(1, 98_400_000, "WFM")
        state = self.act("scan", kind="ZONE", zones=["PMR", "FAV"], enabled=True)
        self.assertEqual(state["tune_hz"], pmr[0]["hz"])
        self.assertEqual(state["v10"]["scan_zones"], ["PMR", "FAV"])
        revision = self.runtime.scanner.tune_revision
        self.assertTrue(self.act("scan_control", operation="hold")["v10"]["scan_held"])
        self.assertFalse(self.act("scan_control", operation="resume")["v10"]["scan_held"])
        self.assertEqual(self.runtime.scanner.tune_revision, revision)
        state = self.act("scan_control", operation="inspect", hz=128_125_000, mode="AUTO")
        self.assertTrue(state["v10"]["scan_held"])
        self.assertEqual((state["tune_hz"], state["modulation"]), (128_125_000, "AM"))
        state = self.act("scan", kind="ZONE", zones=["CB", "PMR", "FAV"], enabled=True)
        self.assertEqual((state["tune_hz"], state["v10"]["scan_zone"]), (128_125_000, "PMR"))
        with self.runtime.lock:
            self.runtime._v10_advance(self.runtime.v10.next_scan_at + 120, False)
        self.assertEqual(self.runtime.state()["tune_hz"], 128_125_000)
        state = self.act("scan_control", operation="resume")
        self.assertFalse(state["v10"]["scan_held"])
        self.assertEqual((state["tune_hz"], state["modulation"]), (pmr[0]["hz"], "FM"))
        self.assertEqual(self.act("step", direction=1)["tune_hz"], pmr[1]["hz"])
        state = self.act("scan", kind="ZONE", zones=["FAV"], enabled=True)
        self.assertEqual((state["tune_hz"], state["v10"]["scan_zone"]), (98_400_000, "FAV"))
        self.assertEqual(state["v10"]["frequency_mode"], "memory")
        self.act("scan_control", operation="inspect", hz=98_410_000)
        state = self.act("scan_control", operation="resume")
        self.assertEqual((state["tune_hz"], state["v10"]["frequency_mode"]), (98_400_000, "memory"))
        state = self.act("scan", kind="ZONE", enabled=False)
        self.assertEqual(state["v10"]["scan_zones"], [])
        self.assertFalse(valid_action({"v10": "scan", "kind": "ZONE", "enabled": True, "zones": ["PMR", "PMR"]}))

    def test_scope_inspection_leaves_digital_mode_for_distant_signals_until_scan_resumes(self):
        self.write(2, 446_196_875, "dPMR")
        self.write(3, 128_125_000, "AM")
        state = self.act("scan", kind="ZONE", zone="FAV", enabled=True)
        self.assertEqual((state["tune_hz"], state["modulation"]), (446_196_875, "dPMR"))
        point = self.runtime.v10.scan["current_point"]

        state = self.act("scan_control", operation="inspect", hz=446_200_000)
        self.assertEqual((state["tune_hz"], state["modulation"]), (446_200_000, "dPMR"))
        self.assertFalse(state["v10"]["auto_mode"])
        self.assertTrue(state["v10"]["scan_held"])
        state = self.act("scan_control", operation="resume")
        self.assertEqual((state["tune_hz"], state["modulation"]), (446_196_875, "dPMR"))

        for frequency, mode in ((446_399_995, "FM"), (98_400_000, "WFM"), (128_125_000, "AM")):
            with self.subTest(frequency=frequency):
                state = self.act("scan_control", operation="inspect", hz=frequency)
                self.assertEqual((state["tune_hz"], state["modulation"]), (frequency, mode))
                self.assertTrue(state["v10"]["auto_mode"])
                self.assertTrue(state["v10"]["scan_held"])
                self.assertEqual(state["v10"]["scan_zone"], "FAV")
                self.assertEqual(self.runtime.v10.scan["current_point"], point)

        state = self.act("scan_control", operation="resume")
        self.assertEqual((state["tune_hz"], state["modulation"]), (446_196_875, "dPMR"))
        self.assertFalse(state["v10"]["auto_mode"])
        self.assertFalse(state["v10"]["scan_held"])
        self.assertEqual((state["v10"]["group"], state["v10"]["slot"]), (0, 2))
        self.assertEqual(self.runtime.v10.scan["current_point"], point)
        self.assertEqual(self.act("step", direction=1)["tune_hz"], 128_125_000)

    def test_scope_inspection_honours_explicit_mode_and_resumes_fixed_preset_mode(self):
        self.write(0, 446_196_875, "dPMR")
        self.act("scan", kind="ZONE", zone="FAV", enabled=True)
        state = self.act("scan_control", operation="inspect", hz=446_399_995, mode="dPMR")
        self.assertEqual((state["tune_hz"], state["modulation"]), (446_399_995, "dPMR"))
        self.assertFalse(state["v10"]["auto_mode"])

        self.act("scan", kind="ZONE", enabled=False)
        self.act("tune", hz=98_400_000, mode="WFM")
        state = self.act("scan", kind="ZONE", zone="BCAST", enabled=True)
        frequency = state["tune_hz"]
        state = self.act("scan_control", operation="inspect", hz=frequency, mode="AUTO")
        self.assertEqual((state["tune_hz"], state["modulation"]), (frequency, "WFM"))
        self.assertTrue(state["v10"]["auto_mode"])
        state = self.act("scan_control", operation="resume")
        self.assertEqual((state["tune_hz"], state["modulation"]), (frequency, "WFM"))
        self.assertFalse(state["v10"]["auto_mode"])

    def test_preset_speed_uses_settled_measurement_and_holds_until_squelch_closes(self):
        self.act("setting", key="scan_speed", value=1)
        self.act("setting", key="pause_time", value=1)
        self.act("setting", key="resume_time", value=2)
        self.act("scan", kind="ZONE", zone="PMR", enabled=True)
        panel = self.runtime.v10
        pmr = panel.zone_entries("PMR")
        with self.runtime.lock:
            # The first read may arrive well after the hardware tune request.
            measured = panel.next_scan_at + 3
            self.runtime._v10_advance(measured, False)
            self.runtime._v10_advance(measured + .30, False)
        self.assertEqual(self.runtime.state()["tune_hz"], pmr[0]["hz"])
        self.act("setting", key="scan_speed", value=10)
        with self.runtime.lock:
            self.runtime._v10_advance(measured + .31, False)
            self.runtime._v10_advance(measured + .4, True)
            self.runtime._v10_advance(measured + 5, True)
        state = self.runtime.state()
        self.assertTrue(state["v10"]["scan_paused"])
        self.assertEqual(state["tune_hz"], pmr[1]["hz"])
        with self.runtime.lock:
            self.runtime._v10_advance(measured + 5.1, False)
            self.runtime._v10_advance(measured + 6.9, False)
        self.assertEqual(self.runtime.state()["tune_hz"], pmr[1]["hz"])
        with self.runtime.lock:
            self.runtime._v10_advance(measured + 7.2, False)
        self.assertEqual(self.runtime.state()["tune_hz"], pmr[2]["hz"])
        self.assertFalse(self.runtime.state()["v10"]["scan_paused"])

    def test_preset_digital_slot_has_acquisition_time_before_dwell_expires(self):
        self.write(1, 430_000_000, "DMR")
        self.write(2, 435_000_000, "FM")
        self.act("setting", key="scan_speed", value=30)
        self.act("scan", kind="ZONE", zone="FAV", enabled=True)
        with self.runtime.lock:
            measured = self.runtime.v10.next_scan_at + .05
            self.runtime._v10_advance(measured, False)
            self.runtime._v10_advance(measured + 1.05, False)
        self.assertEqual(self.runtime.state()["tune_hz"], 430_000_000)
        with self.runtime.lock:
            self.runtime._v10_advance(measured + 1.09, False)
        self.assertEqual(self.runtime.state()["tune_hz"], 435_000_000)

    def test_digital_preset_probes_quiet_rf_then_waits_for_late_carrier(self):
        self.write(1, 430_000_000, "DMR")
        self.write(2, 430_012_500, "DMR")
        self.write(3, 435_000_000, "FM")
        self.act("setting", key="scan_speed", value=30)
        self.act("scan", kind="ZONE", zone="FAV", enabled=True)
        radio = self.runtime
        start = radio.v10.next_scan_at + .05

        def sample(offset, level_margin):
            with patch("app.time.monotonic", return_value=start + offset):
                radio._receive_dmr((radio.dmr.hz, "DMR"), radio.dmr.revision, radio._dab_epoch,
                                   radio.scanner.squelch_db + level_margin, {"sync": False}, None)

        sample(0, -10)
        sample(.08, -10)
        self.assertEqual(radio.state()["tune_hz"], 430_000_000)
        sample(.10, -10)
        self.assertEqual(radio.state()["tune_hz"], 430_012_500)
        sample(.20, -10)
        sample(.27, 10)
        sample(1.30, -10)
        self.assertEqual(radio.state()["tune_hz"], 430_012_500)
        sample(1.36, -10)
        self.assertEqual(radio.state()["tune_hz"], 435_000_000)

    def test_simple_scan_starts_on_current_frequency_and_reverses(self):
        self.write(0, 145_500_000, "FM", name="Calling channel")
        self.act("edge", number=0, start_hz=145_400_000, end_hz=145_425_000, mode="FM")
        self.act("setting", key="tune_step", value=500)
        origin = 144_510_123
        self.act("tune", hz=origin, mode="USB")
        saved = self.runtime.v10.backup()
        state = self.act("scan", kind="SIMPLE", enabled=True, direction=-1)
        self.assertEqual((state["tune_hz"], state["modulation"], state["v10"]["scan_direction"]),
                         (origin, "USB", -1))
        with self.runtime.lock:
            self.runtime._v10_advance(self.runtime.v10.next_scan_at, False)
        self.assertEqual(self.runtime.state()["tune_hz"], origin - 500)
        state = self.act("step", direction=1)
        self.assertEqual((state["tune_hz"], state["v10"]["scan_direction"]), (origin, 1))
        state = self.act("scan", kind="SIMPLE", enabled=False)
        self.assertIsNone(state["v10"]["scan_kind"])
        self.assertEqual(self.runtime.v10.backup(), saved)

    def test_simple_scan_respects_tuning_limits_and_custom_step(self):
        self.act("tune", hz=MANUAL_MIN_HZ, mode="AM")
        state = self.act("scan", kind="SIMPLE", enabled=True, direction=-1, step=1_000_000)
        self.assertEqual(state["tune_hz"], MANUAL_MIN_HZ)
        state = self.act("step", direction=-1)
        self.assertTrue(MANUAL_MAX_HZ - 1_000_000 < state["tune_hz"] <= MANUAL_MAX_HZ)
        self.assertEqual(self.act("step", direction=1)["tune_hz"], MANUAL_MIN_HZ)
        self.assertEqual(self.act("step", direction=1)["tune_hz"], MANUAL_MIN_HZ + 1_000_000)
        self.assertFalse(valid_action({"v10": "scan", "kind": "SIMPLE", "enabled": True, "step": True}))
        self.assertFalse(valid_action({"v10": "scan", "kind": "SIMPLE", "enabled": True, "direction": 0}))

    def test_simple_scan_keeps_auto_mode_across_band_changes(self):
        self.act("tune", hz=117_970_000, mode="AUTO")
        state = self.act("scan", kind="SIMPLE", enabled=True, direction=1, step=10_000)
        self.assertTrue(state["v10"]["auto_mode"])
        self.assertEqual((state["tune_hz"], state["modulation"]), (117_970_000, "FM"))
        state = self.act("step", direction=1)
        self.assertTrue(state["v10"]["auto_mode"])
        self.assertEqual((state["tune_hz"], state["modulation"]), (117_980_000, "AM"))


class SignalTests(unittest.TestCase):
    def test_auto_waits_for_strong_consistent_am_or_fm_evidence(self):
        time = np.arange(16 * IQ_SAMPLES) / IQ_RATE
        audio = .9 * np.sin(2 * np.pi * 910 * time) + .1 * np.sin(2 * np.pi * 1370 * time)
        carrier = 2 * np.pi * 225 * time
        variations = {
            "AM": (1 + .43 * audio) * np.exp(1j * carrier),
            "FM": np.exp(1j * (carrier + 2 * np.pi * np.cumsum(audio) * 2000 / IQ_RATE)),
            "carrier": np.exp(1j * carrier),
            "SSB": .7 * np.exp(2j * np.pi * 1270 * time) + .3 * np.exp(2j * np.pi * 780 * time),
        }
        random = np.random.default_rng(6)
        for kind, snr, expected in (("AM", 25, "AM"), ("FM", 25, "FM"),
                                    ("AM", 8, None), ("carrier", 25, None), ("SSB", 25, None)):
            with self.subTest(kind=kind, snr=snr):
                receiver = Receiver()
                receiver._tuning_snr_db = snr
                noise = random.normal(size=len(time)) + 1j * random.normal(size=len(time))
                iq = (.06 * variations[kind] + .006 * noise).astype(np.complex64)
                hints = []
                for offset in range(0, len(iq), IQ_SAMPLES):
                    receiver._decoder.decode(iq[offset:offset + IQ_SAMPLES])
                    hints.append(receiver.auto_mode_hint(1))
                self.assertEqual(hints[:7], [None] * 7)
                self.assertEqual(hints[-1], expected)
                self.assertIsNone(receiver.auto_mode_hint(2))

    def test_cw_and_coherent_am_produce_audio(self):
        decoder = _ChannelDecoder()
        points = np.arange(IQ_SAMPLES * 8) / IQ_RATE
        iq = (.08 * (1 + .4 * np.sin(2 * np.pi * 950 * points))
              * np.exp(2j * np.pi * 40 * points)).astype(np.complex64)

        def decoded(mode):
            decoder.reset(0, mode)
            audio = []
            for offset in range(0, len(iq), IQ_SAMPLES):
                _, pcm = decoder.decode(iq[offset:offset + IQ_SAMPLES])
                audio.append(np.frombuffer(pcm, "<i2"))
            return np.concatenate(audio)[2 * SAMPLE_RATE // 100:]

        def peak_hz(data):
            frequencies = np.fft.rfftfreq(len(data), 1 / SAMPLE_RATE)
            return frequencies[np.argmax(np.abs(np.fft.rfft(data * np.hanning(len(data)))))]

        self.assertAlmostEqual(peak_hz(decoded("S-AM(D)")), 950, delta=18)
        self.assertAlmostEqual(peak_hz(decoded("CW")), 640, delta=18)
        self.assertAlmostEqual(peak_hz(decoded("CW-R")), 560, delta=18)

    def test_fsk_ita2_across_irregular_blocks(self):
        baud, rate = 45.45, 48_000
        message = "HELLO RADIO"
        bits = [1] * 10
        for char in message:
            code = LETTERS.index(char)
            bits.extend([0, *[(code >> bit) & 1 for bit in range(5)], 1, 1])
        bits.extend([1] * 5)
        frequencies = np.where(np.repeat(bits, round(rate / baud)), 85, -85)
        iq = np.exp(2j * np.pi * np.cumsum(frequencies) / rate).astype(np.complex64)
        for reverse in (False, True):
            decoder = RttyDecoder(rate, reverse=reverse)
            stream = iq.conj() if reverse else iq
            for offset in range(0, len(stream), 631):
                decoder.feed(stream[offset:offset + 631])
            self.assertEqual(decoder.snapshot(), message)


if __name__ == "__main__":
    unittest.main()

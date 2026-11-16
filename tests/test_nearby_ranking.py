from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app import ScannerRuntime
from receiver import NearbyPeak
from v10 import NEARBY_COMPARE_SECONDS, NEARBY_LISTENING_INTERVAL, NEARBY_RETRY_SECONDS


class NearbyRankingTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = ScannerRuntime(Path(directory.name) / "state.json")
        self.addCleanup(self.runtime.close)
        self.panel = self.runtime.v10
        self.now = 100
        self.current = (98_400_000, "WFM")

    def begin(self, when, *, live=False):
        probe = self.panel.next_nearby_live_probe if live else self.panel.next_nearby_probe
        request = probe(when, self.current)
        self.assertIsNotNone(request)
        return request

    def finish(self, request, peak, when, *, live=False):
        job, _, band, *_ = request
        return self.panel.finish_nearby_probe(job, peak, band.modulation, when, current=self.current,
                                             listening=True, live=live)

    def test_two_sweep_windows_choose_the_stronger_gain_adjusted_peak(self):
        self.panel.enable_nearby(self.current, self.now)
        first = self.begin(self.now)
        self.assertEqual(first[2].label, "PMR446")
        pmr = NearbyPeak(446_006_250, -20, -52)
        vhf = NearbyPeak(144_125_000, -27, -38)
        self.assertIsNone(self.finish(first, pmr, self.now))
        second_at = self.now + NEARBY_LISTENING_INTERVAL
        self.assertIsNone(self.panel.next_nearby_probe(second_at - .001, self.current))
        second = self.begin(second_at)
        self.assertEqual(second[2].label, "VHF")
        hit = self.finish(second, vhf, second_at)
        self.assertEqual(hit["hz"], vhf.hz)
        self.assertIsNone(self.panel.take_nearby_candidate(self.now + NEARBY_COMPARE_SECONDS, self.current))

    def test_second_window_may_confirm_a_stronger_first_hit(self):
        self.current = (146_500_000, "FM")
        self.panel.enable_nearby(self.current, self.now)
        vhf = NearbyPeak(146_450_000, -18, -30)
        pmr = NearbyPeak(446_006_250, -32, -45)
        self.assertIsNone(self.finish(self.begin(self.now, live=True), vhf, self.now, live=True))
        # An available external probe can finish while the primary SDR still has live IQ.
        hit = self.finish(self.begin(self.now), pmr, self.now + .03)
        self.assertEqual(hit["hz"], vhf.hz)

    def test_same_frequency_in_both_windows_does_not_end_comparison_early(self):
        self.current = (446_300_000, "FM")
        self.panel.enable_nearby(self.current, self.now)
        peak = NearbyPeak(446_006_250, -27, -40)
        self.assertIsNone(self.finish(self.begin(self.now, live=True), peak, self.now, live=True))
        self.assertIsNone(self.finish(self.begin(self.now), peak, self.now + .03))
        self.assertIsNone(self.panel.take_nearby_candidate(self.now + NEARBY_COMPARE_SECONDS - .001,
                                                          self.current))
        hit = self.panel.take_nearby_candidate(self.now + NEARBY_COMPARE_SECONDS, self.current)
        self.assertEqual(hit["hz"], peak.hz)
        self.assertEqual(hit["mode"], "FM")
        self.assertFalse(hit["auto_detect"])

    def test_pending_peak_is_discarded_when_locked_or_already_tuned(self):
        peak = NearbyPeak(446_006_250, -27, -40)
        for current, locked in ((self.current, True), ((peak.hz, "FM"), False)):
            with self.subTest(current=current, locked=locked):
                self.panel.nearby = None
                self.panel.temporary_locks.clear()
                self.panel.enable_nearby(self.current, self.now)
                self.assertIsNone(self.finish(self.begin(self.now), peak, self.now))
                if locked:
                    self.panel.temporary_locks.add(peak.hz)
                self.assertIsNone(self.panel.take_nearby_candidate(self.now + NEARBY_COMPARE_SECONDS, current))
                self.assertIsNone(self.panel.take_nearby_candidate(self.now + NEARBY_COMPARE_SECONDS, self.current))

    def test_invalidating_probes_drops_pending_and_in_flight_results(self):
        self.current = (146_500_000, "FM")
        self.panel.enable_nearby(self.current, self.now)
        pmr = NearbyPeak(446_006_250, -28, -41)
        vhf = NearbyPeak(146_450_000, -10, -21)
        self.assertIsNone(self.finish(self.begin(self.now), pmr, self.now))
        job = self.begin(self.now, live=True)
        self.panel.invalidate_nearby_probe()
        self.assertIsNone(self.finish(job, vhf, self.now + .03, live=True))
        self.assertIsNone(self.panel.take_nearby_candidate(self.now + NEARBY_COMPARE_SECONDS, self.current))

    def test_unconfirmed_peak_is_temporarily_blocked_in_both_probe_paths(self):
        self.current = (446_300_000, "FM")
        self.panel.enable_nearby(self.current, self.now)
        peak = NearbyPeak(446_006_250, -27, -40)
        self.panel.reject_nearby_candidate(peak.hz, self.now)
        primary = self.begin(self.now, live=True)
        sweep = self.begin(self.now)
        self.assertIn(peak.hz, primary[-1])
        self.assertIn(peak.hz, sweep[-1])
        self.assertFalse(self.panel.frequency_locked(peak.hz, self.now))
        self.assertIsNone(self.finish(primary, peak, self.now, live=True))
        self.assertIsNone(self.finish(sweep, peak, self.now))
        retry = self.now + NEARBY_RETRY_SECONDS
        live = self.begin(retry, live=True)
        self.assertNotIn(peak.hz, live[-1])
        self.assertIsNone(self.finish(live, peak, retry, live=True))
        hit = self.panel.take_nearby_candidate(retry + NEARBY_COMPARE_SECONDS, self.current)
        self.assertEqual(hit["hz"], peak.hz)

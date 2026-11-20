import io
import json
import unittest
from unittest.mock import patch

from repeater_bookmarks import RepeaterDirectory, RepeaterDirectoryError, repeater_entries


class RepeaterBookmarksTests(unittest.TestCase):
    def test_directory_uses_repeater_output_and_expands_supported_modes(self):
        data = {"data": [
            {"repeater": "GB7CT", "tx": 145_637_500, "rx": 145_037_500,
             "modeCodes": ["M:3", "D", "M:7", "F"], "town": " TRING ",
             "locator": "IO91PS", "status": "OPERATIONAL", "keeperCallsign": "M0ZPU"},
            {"repeater": "GB7CT", "tx": 145_637_500, "modeCodes": ["A"]},
            {"repeater": "GB7MB", "tx": 439_700_000, "modeCodes": ["F"]},
            {"repeater": "OUTSIDE", "tx": 2_400_000_000, "modeCodes": ["A"]},
        ]}
        self.assertEqual(repeater_entries(data), [{
            "callsign": "GB7CT", "hz": 145_637_500, "modes": ["DMR", "D-STAR", "FM"],
            "town": "TRING", "locator": "IO91PS", "status": "OPERATIONAL",
        }])

    def test_band_uses_cached_results_during_api_outage(self):
        directory = RepeaterDirectory()
        payload = {"data": [{"repeater": "GB7CT", "tx": 145_637_500, "modeCodes": ["M:3"]}]}
        with patch("repeater_bookmarks.urlopen", side_effect=[
            io.BytesIO(json.dumps(payload).encode()), OSError(), OSError(),
        ]) as fetch, patch("repeater_bookmarks.time.monotonic", return_value=100_000) as now:
            live = directory.band("2m")
            self.assertEqual(directory.band("2m"), live)
            fetch.assert_called_once_with("https://api-beta.rsgb.online/band/2m", timeout=3)
            now.return_value += 24 * 60 * 60 + 1
            self.assertEqual(directory.band("2m"), live)
            with self.assertRaises(RepeaterDirectoryError):
                directory.band("70cm")

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from app import PanelHandler, ScannerRuntime
from launcher import panel_ready


class ReleaseHealthTests(unittest.TestCase):
    def test_launcher_uses_small_health_response(self):
        handler = PanelHandler.__new__(PanelHandler)
        handler.path = "/api/health"
        handler.accept_request = Mock(return_value=True)
        handler.json_response = Mock()
        handler.radio = Mock(side_effect=AssertionError("Health check must not read radio state"))
        handler.do_GET()

        with patch("launcher.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = json.dumps(handler.json_response.call_args.args[0]).encode()
            self.assertTrue(panel_ready())
            connection.return_value.request.assert_called_once_with("GET", "/api/health")
            response.read.assert_called_once_with(1024)
            response.read.return_value = b'{"ready": true}'
            self.assertFalse(panel_ready())

    def test_memory_revision_preserves_full_state_for_older_callers(self):
        with TemporaryDirectory() as folder:
            runtime = ScannerRuntime(Path(folder) / "scan.json")
            self.addCleanup(runtime.close)
            channels = {
                divmod(index, 100): {"group": index // 100, "slot": index % 100,
                                    "hz": 145_500_000 + index, "mode": "FM",
                                    "name": f"Test channel {index}"}
                for index in range(1000)
            }
            runtime.v10.save(memories=channels)
            full = runtime.state()
            revision = full["v10"]["memory_revision"]
            self.assertGreater(len(json.dumps(full)), 65_536)
            self.assertEqual(len(full["v10"]["memories"]), 1000)

            compact = runtime.state(memory_revision=revision)
            self.assertNotIn("memories", compact["v10"])
            self.assertEqual(compact["v10"]["memory_revision"], revision)
            self.assertEqual(full["power"], compact["power"])

            runtime.v10.save(bank_names={0: "Test bank"})
            renamed = runtime.state(memory_revision=revision)
            self.assertEqual(renamed["v10"]["bank_names"], {0: "Test bank"})
            self.assertNotIn("memories", renamed["v10"])

            runtime.v10.save(memories={})
            updated = runtime.state(memory_revision=revision)
            self.assertNotEqual(updated["v10"]["memory_revision"], revision)
            self.assertEqual(updated["v10"]["memories"], [])

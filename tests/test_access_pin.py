"""Remote PINs retain their full numeric value regardless of digit count."""

import json
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

from access_pin import AccessPin, PIN_PATTERN, SOURCE_ATTEMPTS


class AccessPinTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "access.json"
        self.pin = AccessPin(self.path)
        self.session = "a" * 32

    def test_different_lengths_persist_and_unlock_with_the_full_pin(self):
        for digits in ("7", "0042", "123456", "01234567890123456789", "0" * 4096):
            with self.subTest(length=len(digits)):
                self.pin.set_pin(digits)
                self.assertTrue(PIN_PATTERN.fullmatch(digits))
                data = json.loads(self.path.read_text())
                self.assertEqual(set(data), {"salt", "digest"})
                self.assertEqual(len(data["salt"]), 64)
                self.assertEqual(len(data["digest"]), 64)
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
                restored = AccessPin(self.path)
                capability, retry_after = restored.unlock(digits, "remote", self.session)
                self.assertIsNotNone(capability)
                self.assertEqual(retry_after, 0)
                self.assertTrue(restored.authorized(capability, self.session))
                self.assertIsNone(restored.unlock(digits + "1", "remote", self.session)[0])

    def test_leading_zeroes_are_significant_and_invalid_values_do_not_replace_credentials(self):
        self.pin.set_pin("0007")
        for invalid in ("", " 0007", "0007 ", "0007\n", "-7", "1.2", "１２３", 7, None):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    self.pin.set_pin(invalid)
        self.assertIsNone(self.pin.unlock("7", "remote", self.session)[0])
        self.assertIsNotNone(self.pin.unlock("0007", "remote", self.session)[0])

    def test_variable_pin_preserves_attempt_limits_and_change_revokes_grants(self):
        self.pin.set_pin("42")
        grant, _ = self.pin.unlock("42", "owner", self.session)
        for _ in range(SOURCE_ATTEMPTS):
            self.assertEqual(self.pin.unlock("1", "remote", self.session), (None, 0))
        capability, retry_after = self.pin.unlock("42", "remote", self.session)
        self.assertIsNone(capability)
        self.assertGreater(retry_after, 0)
        self.pin.set_pin("12345678")
        self.assertFalse(self.pin.authorized(grant, self.session))
        self.assertIsNotNone(self.pin.unlock("12345678", "remote", self.session)[0])


if __name__ == "__main__":
    unittest.main()

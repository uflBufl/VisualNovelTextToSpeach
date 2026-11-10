import json
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import psutil

from vntts.process_identity import process_fingerprint, validate_process_fingerprint


class ProcessFingerprintTest(unittest.TestCase):
    def test_fingerprint_survives_reading_from_another_process(self):
        expected = process_fingerprint(psutil.Process())
        output = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import json, psutil; "
                "from vntts.process_identity import process_fingerprint; "
                f"print(json.dumps(process_fingerprint(psutil.Process({os.getpid()}))))",
            ],
            text=True,
            timeout=10,
        )
        self.assertEqual(json.loads(output), expected)

    @unittest.skipUnless(sys.platform == "darwin", "macOS clock correction")
    def test_macos_boot_clock_correction_does_not_change_identity(self):
        from psutil import _psosx

        with patch.object(_psosx, "INIT_BOOT_TIME", 1000):
            with patch.object(_psosx, "boot_time", return_value=1000):
                before = psutil.Process()
                created = before.create_time()
                expected = process_fingerprint(before)
            with patch.object(_psosx, "boot_time", return_value=999):
                after = psutil.Process()
                self.assertEqual(after.create_time(), created + 1)
                self.assertEqual(process_fingerprint(after), expected)

    def test_windows_uses_creation_time_even_if_process_hash_is_unchanged(self):
        process = Mock(pid=42)
        process.create_time.return_value = 1000.0
        with patch("vntts.process_identity.sys.platform", "win32"):
            expected = process_fingerprint(process)
            self.assertEqual(expected["scheme"], "windows-create-time-v1")
            self.assertEqual(process_fingerprint(process), expected)
            process.create_time.return_value = 1001.0
            self.assertNotEqual(process_fingerprint(process), expected)

    def test_disappeared_or_inaccessible_process_has_no_fingerprint(self):
        process = Mock(pid=42)
        process.is_running.return_value = False
        with self.assertRaises(psutil.NoSuchProcess):
            process_fingerprint(process)
        process.create_time.side_effect = psutil.AccessDenied(42)
        with self.assertRaises(psutil.AccessDenied):
            process_fingerprint(process)

    def test_saved_fingerprint_requires_nonempty_string_fields(self):
        self.assertIsNone(validate_process_fingerprint(None))
        expected = {"scheme": "test", "token": "lifetime"}
        self.assertEqual(validate_process_fingerprint(expected), expected)
        for invalid in (
            [],
            "test",
            {},
            {"scheme": "test"},
            {**expected, "token": 1},
            {**expected, "scheme": ""},
        ):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_process_fingerprint(invalid)

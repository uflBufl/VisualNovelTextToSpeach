import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import psutil

from vntts.authoring.generation_lease import (
    BulkGenerationError,
    GenerationLease,
    inspect_process_status,
    process_is_alive,
    process_started_at,
)


class ProcessInspectionTests(unittest.TestCase):
    def test_windows_start_identity_survives_missing_ps(self):
        with (
            patch("vntts.authoring.generation_lease.sys.platform", "win32"),
            patch(
                "vntts.authoring.generation_lease.subprocess.run",
                side_effect=FileNotFoundError,
            ),
            patch("psutil.Process") as process,
        ):
            process.return_value.create_time.return_value = 1720000000.125
            self.assertEqual(process_started_at(123), "psutil:1720000000.125")
            process.return_value.create_time.side_effect = psutil.AccessDenied(pid=123)
            self.assertIsNone(process_started_at(123))

    def test_unix_probe_preserves_unknown_state(self):
        if sys.platform == "win32":
            self.skipTest("Unix signal behavior")
        with patch(
            "vntts.authoring.generation_lease.os.kill", side_effect=PermissionError
        ):
            self.assertEqual(inspect_process_status(123), "unknown")
            self.assertTrue(process_is_alive(123))

    def test_live_child_survives_repeated_liveness_probes(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _attempt in range(3):
                self.assertEqual(inspect_process_status(child.pid), "live")
                self.assertTrue(process_is_alive(child.pid))
                self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=10)

        self.assertEqual(inspect_process_status(child.pid), "dead")
        self.assertFalse(process_is_alive(child.pid))

    def test_generation_lease_schema_version_requires_exact_integer(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            lease_path = output / ".generation-lease.json"
            for version in (True, 1.0):
                lease_path.write_bytes(
                    json.dumps(
                        {
                            "schema": "vntts.authoring-generation-lease",
                            "schema_version": version,
                            "queue_sha256": "1" * 64,
                            "pid": 123,
                        }
                    ).encode()
                )
                before = lease_path.read_bytes()
                lease = GenerationLease(
                    output, "1" * 64, process_checker=lambda _pid: False
                )
                with (
                    self.subTest(version=version),
                    self.assertRaisesRegex(
                        BulkGenerationError, "Unrecognized generation lease"
                    ),
                ):
                    lease.__enter__()
                self.assertEqual(lease_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

import subprocess
import sys
import unittest
from unittest.mock import patch

from vntts.authoring.generation_lease import (
    inspect_process_status,
    process_is_alive,
)


class ProcessInspectionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

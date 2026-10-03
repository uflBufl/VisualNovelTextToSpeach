import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.symlink_support import symlink_or_skip
from vntts.authoring.advisory_lock import exclusive_advisory_lock


class AdvisoryLockTest(unittest.TestCase):
    def test_cancellation_after_acquisition_releases_persistent_guard(self) -> None:
        with TemporaryDirectory() as directory:
            guard = Path(directory) / "setup.lock"
            checks = 0

            def check_cancelled() -> None:
                nonlocal checks
                checks += 1
                if checks == 2:
                    raise InterruptedError("cancelled after acquisition")

            with self.assertRaises(InterruptedError):
                with exclusive_advisory_lock(
                    guard, blocking=True, check_cancelled=check_cancelled
                ):
                    self.fail("Cancelled guard must not enter the body")
            self.assertEqual(checks, 2)
            self.assertTrue(guard.is_file())
            with exclusive_advisory_lock(guard):
                pass

    def test_lock_does_not_follow_alias_to_unrelated_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / "unrelated.txt"
            unrelated.write_bytes(b"")
            lock_path = root / "setup.lock"
            symlink_or_skip(lock_path, unrelated)

            with self.assertRaises(OSError), exclusive_advisory_lock(lock_path):
                pass

            self.assertEqual(unrelated.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()

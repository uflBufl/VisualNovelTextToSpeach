import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.symlink_support import symlink_or_skip
from vntts.authoring.advisory_lock import exclusive_advisory_lock


class AdvisoryLockTest(unittest.TestCase):
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

import unittest
from pathlib import Path
from unittest.mock import patch

from tests.symlink_support import symlink_or_skip


class SymlinkSupportTest(unittest.TestCase):
    @patch("tests.symlink_support.sys.platform", "win32")
    def test_skips_only_missing_windows_symlink_privilege(self):
        privilege_error = OSError("privilege unavailable")
        privilege_error.winerror = 1314
        with (
            patch.object(Path, "symlink_to", side_effect=privilege_error),
            self.assertRaises(unittest.SkipTest),
        ):
            symlink_or_skip(Path("link"), Path("target"))

        with (
            patch.object(Path, "symlink_to", side_effect=OSError("other failure")),
            self.assertRaisesRegex(OSError, "other failure"),
        ):
            symlink_or_skip(Path("link"), Path("target"))


if __name__ == "__main__":
    unittest.main()

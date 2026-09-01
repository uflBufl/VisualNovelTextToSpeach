import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.authoring.private_files import private_file_is_restricted


class PrivateFilesTest(unittest.TestCase):
    def test_windows_uses_profile_acl_instead_of_posix_mode_bits(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "key.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o644)

            self.assertFalse(private_file_is_restricted(path, platform="linux"))
            self.assertTrue(private_file_is_restricted(path, platform="win32"))
            with patch.object(Path, "is_symlink", return_value=True):
                self.assertFalse(private_file_is_restricted(path, platform="win32"))


if __name__ == "__main__":
    unittest.main()

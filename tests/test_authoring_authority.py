import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.symlink_support import symlink_or_skip
from vntts.authoring.authority import (
    AuthoringAuthorityError,
    capture_authority_file,
    write_json_document_no_replace,
)


class AuthoringAuthorityTest(unittest.TestCase):
    def test_capture_rejects_relative_symlink(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            symlink_or_skip(root / "alias.json", target)
            previous_directory = Path.cwd()
            try:
                os.chdir(root)
                with self.assertRaisesRegex(AuthoringAuthorityError, "unavailable"):
                    capture_authority_file("alias.json", "test authority")
            finally:
                os.chdir(previous_directory)

    def test_temp_cleanup_failure_preserves_publication_outcome(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            real_unlink = Path.unlink

            def fail_temp_cleanup(path, *args, **kwargs):
                if path.suffix == ".tmp":
                    raise PermissionError("temporary file is still open")
                return real_unlink(path, *args, **kwargs)

            published = root / "published.json"
            with patch.object(Path, "unlink", fail_temp_cleanup):
                self.assertEqual(
                    write_json_document_no_replace(
                        published, {"value": 1}, "test document"
                    ),
                    published.resolve(),
                )
            self.assertEqual(json.loads(published.read_text()), {"value": 1})

            failed = root / "failed.json"
            with (
                patch.object(Path, "unlink", fail_temp_cleanup),
                patch(
                    "vntts.authoring.authority.os.link",
                    side_effect=OSError("link failed"),
                ),
                self.assertRaisesRegex(AuthoringAuthorityError, "link failed"),
            ):
                write_json_document_no_replace(failed, {}, "test document")
            self.assertFalse(failed.exists())


if __name__ == "__main__":
    unittest.main()

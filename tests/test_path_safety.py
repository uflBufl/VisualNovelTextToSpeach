import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.authoring.generation_manifest import (
    BulkGenerationError,
    safe_generation_relative_path,
)
from vntts.path_safety import contained_regular_file, safe_relative_path


class PathBoundaryError(RuntimeError):
    pass


class PathSafetyTest(unittest.TestCase):
    def test_generation_relative_path_rejects_nul_with_its_domain_error(self):
        self.assertEqual(
            safe_generation_relative_path("./nested//voice.wav", "Audio"),
            Path("nested/voice.wav"),
        )
        for value in ("\x00", "nested/bad\x00.wav"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(BulkGenerationError, "relative POSIX path"),
            ):
                safe_generation_relative_path(value, "Audio")

    def test_embedded_nul_uses_requested_error_boundary(self):
        with TemporaryDirectory() as directory:
            for value in ("\x00", "nested/bad\x00.json", Path("bad\x00.json")):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(PathBoundaryError, "POSIX-relative"):
                        safe_relative_path(
                            str(value), "artifact", error_type=PathBoundaryError
                        )
                    with self.assertRaisesRegex(PathBoundaryError, "leaves its root"):
                        contained_regular_file(
                            directory, value, "artifact", error_type=PathBoundaryError
                        )


if __name__ == "__main__":
    unittest.main()

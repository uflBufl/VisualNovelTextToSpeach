import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.authoring.publication import staged_directory


class StagedDirectoryTest(unittest.TestCase):
    def test_cleans_unpublished_directory_and_keeps_renamed_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with staged_directory(root, prefix=".staging-") as staging:
                (staging / "value").write_text("ok", encoding="utf-8")
                unpublished = staging
            self.assertFalse(unpublished.exists())

            destination = root / "published"
            with staged_directory(root, prefix=".staging-") as staging:
                (staging / "value").write_text("ok", encoding="utf-8")
                staging.rename(destination)
            self.assertEqual((destination / "value").read_text(encoding="utf-8"), "ok")


if __name__ == "__main__":
    unittest.main()

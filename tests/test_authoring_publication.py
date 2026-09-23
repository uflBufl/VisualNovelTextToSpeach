import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.authoring.publication import publish_single_base_successor, staged_directory


class StagedDirectoryTest(unittest.TestCase):
    def test_cleans_unpublished_directory_after_base_exception(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(KeyboardInterrupt):
                with staged_directory(root, prefix=".staging-") as staging:
                    unpublished = staging
                    raise KeyboardInterrupt
            self.assertFalse(unpublished.exists())

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

    def test_single_base_successor_rejects_changed_snapshot_before_publish(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            (base / "generated-audio").mkdir(parents=True)
            source = base / "workspace.json"
            source.write_bytes(b"original")
            digest = sha256(source.read_bytes()).hexdigest()
            staging = root / "staging"
            staging.mkdir()
            source.write_bytes(b"changed")

            with self.assertRaisesRegex(ValueError, "authority changed"):
                publish_single_base_successor(
                    staging,
                    root / "published",
                    base,
                    "0" * 64,
                    [(source, digest)],
                    label="Test",
                    publish_label="test",
                    error_type=ValueError,
                )
            self.assertTrue(staging.exists())
            self.assertFalse((root / "published").exists())


if __name__ == "__main__":
    unittest.main()

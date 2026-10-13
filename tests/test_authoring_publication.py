import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.authoring.generation_lease import BulkGenerationError, GenerationLease
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

    def test_staging_cleanup_failure_preserves_only_its_own_operation_error(self):
        for primary_type in (None, ValueError, KeyboardInterrupt):
            with self.subTest(primary_type=primary_type), TemporaryDirectory() as root:
                primary = primary_type("publication failed") if primary_type else None
                cleanup_error = OSError("staging cleanup failed")
                with patch(
                    "vntts.cleanup.TemporaryDirectory.cleanup",
                    side_effect=cleanup_error,
                ) as cleanup:
                    with self.assertRaises(type(primary or cleanup_error)) as raised:
                        with staged_directory(root, prefix=".staging-"):
                            if primary is not None:
                                raise primary
                self.assertIs(raised.exception, primary or cleanup_error)
                cleanup.assert_called_once_with()
                if primary is not None:
                    self.assertEqual(
                        primary.__notes__,
                        ["Publication staging cleanup failed: staging cleanup failed"],
                    )

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

    def test_single_base_active_lease_keeps_source_and_uses_caller_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            output = base / "generated-audio"
            output.mkdir(parents=True)
            staging = root / "staging"
            staging.mkdir()
            destination = root / "published"
            lease_path = output / ".generation-lease.json"
            with GenerationLease(output, "0" * 64, process_checker=lambda _pid: True):
                before = lease_path.read_bytes()
                with self.assertRaises(ValueError) as caught:
                    publish_single_base_successor(
                        staging,
                        destination,
                        base,
                        "0" * 64,
                        (),
                        label="Test",
                        publish_label="test",
                        error_type=ValueError,
                    )
                self.assertIsInstance(caught.exception.__cause__, BulkGenerationError)
                self.assertEqual(str(caught.exception), str(caught.exception.__cause__))
                self.assertEqual(lease_path.read_bytes(), before)
                self.assertTrue(staging.is_dir())
                self.assertFalse(destination.exists())
            self.assertFalse(lease_path.exists())

    def test_single_base_lost_lease_keeps_successor_lease_and_never_publishes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            output = base / "generated-audio"
            output.mkdir(parents=True)
            source = base / "workspace.json"
            source.write_bytes(b"original")
            digest = sha256(source.read_bytes()).hexdigest()
            staging = root / "staging"
            staging.mkdir()
            destination = root / "published"
            lease_path = output / ".generation-lease.json"

            def steal_and_hash(path, *, error_type):
                self.assertIs(error_type, OSError)
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                lease["lease_id"] = "successor"
                lease_path.write_text(json.dumps(lease), encoding="utf-8")
                return sha256(Path(path).read_bytes()).hexdigest()

            with (
                patch(
                    "vntts.document_identity.file_sha256", side_effect=steal_and_hash
                ),
                patch(
                    "vntts.authoring.publication.rename_directory_no_replace"
                ) as rename,
                self.assertRaisesRegex(ValueError, "lease ownership changed") as caught,
            ):
                publish_single_base_successor(
                    staging,
                    destination,
                    base,
                    "0" * 64,
                    [(source, digest)],
                    label="Test",
                    publish_label="test",
                    error_type=ValueError,
                )
            self.assertIsInstance(caught.exception.__cause__, BulkGenerationError)
            rename.assert_not_called()
            self.assertEqual(
                json.loads(lease_path.read_text())["lease_id"], "successor"
            )
            self.assertEqual(source.read_bytes(), b"original")
            self.assertTrue(staging.is_dir())
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()

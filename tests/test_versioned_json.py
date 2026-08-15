import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.versioned_json import load_versioned_json, write_versioned_json


class VersionedJsonTest(unittest.TestCase):
    def test_loader_applies_explicit_compatibility_policy(self):
        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "document.json"
            path.write_text(
                json.dumps({"schema_version": 1, "value": "old"}),
                encoding="utf-8",
            )

            accepted = load_versioned_json(
                path,
                schema_version=2,
                document_name="test document",
                decode=lambda payload: payload["value"],
                fallback=lambda: "fallback",
                warn=warnings.append,
                allow_older=True,
            )
            rejected = load_versioned_json(
                path,
                schema_version=2,
                document_name="test document",
                decode=lambda payload: payload["value"],
                fallback=lambda: "fallback",
                warn=warnings.append,
            )

        self.assertEqual(accepted, "old")
        self.assertEqual(rejected, "fallback")
        self.assertIn("unsupported test document schema version", warnings[0])

    def test_malformed_and_future_documents_use_fresh_fallbacks(self):
        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "document.json"
            path.write_text("not json", encoding="utf-8")
            malformed = load_versioned_json(
                path,
                schema_version=1,
                document_name="test document",
                decode=lambda payload: payload,
                fallback=dict,
                warn=warnings.append,
            )
            path.write_text(
                json.dumps({"schema_version": 2}),
                encoding="utf-8",
            )
            future = load_versioned_json(
                path,
                schema_version=1,
                document_name="test document",
                decode=lambda payload: payload,
                fallback=dict,
                warn=warnings.append,
            )

        self.assertEqual(malformed, {})
        self.assertEqual(future, {})
        self.assertEqual(len(warnings), 2)

    def test_atomic_writer_keeps_existing_document_when_publication_fails(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "document.json"
            path.write_text('{"schema_version": 1, "value": "stable"}\n')

            with (
                patch(
                    "vntts.versioned_json.atomic_write_json",
                    side_effect=OSError("blocked"),
                ),
                self.assertRaisesRegex(OSError, "blocked"),
            ):
                write_versioned_json(path, 1, {"value": "replacement"})

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload, {"schema_version": 1, "value": "stable"})

    def test_writer_rejects_conflicting_schema_version(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "document.json"
            with self.assertRaisesRegex(ValueError, "conflicts"):
                write_versioned_json(path, 1, {"schema_version": 2})


if __name__ == "__main__":
    unittest.main()

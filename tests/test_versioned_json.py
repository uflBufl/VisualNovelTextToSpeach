import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.versioned_json import (
    load_versioned_json,
    read_versioned_json,
    read_versioned_json_snapshot,
    write_versioned_json,
)


class VersionedJsonTest(unittest.TestCase):
    def test_snapshot_digest_is_for_bytes_read_before_document_replacement(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "document.json"
            original = b'{"schema_version": 1, "value": "original"}'
            path.write_bytes(original)
            replacement = b'{"schema_version": 1, "value": "replacement"}'
            real_loads = json.loads

            def replace_before_decode(raw):
                path.write_bytes(replacement)
                return real_loads(raw)

            with patch("vntts.versioned_json.decode_json", replace_before_decode):
                payload, revision = read_versioned_json_snapshot(
                    path,
                    schema_version=1,
                    document_name="test document",
                )

            self.assertEqual(payload["value"], "original")
            self.assertEqual(revision, sha256(original).digest())

    def test_nested_json_uses_parse_errors_and_fresh_fallbacks(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "document.json"
            payload = (
                '{"schema_version":1,"value":' + "[" * 100000 + "0" + "]" * 100000 + "}"
            )
            path.write_text(payload, encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(json.JSONDecodeError, "nesting") as caught:
                read_versioned_json(
                    path, schema_version=1, document_name="test document"
                )
            self.assertIsInstance(caught.exception.__cause__, RecursionError)
            warnings = []
            revisions = []
            decoded = []
            results = [
                load_versioned_json(
                    path,
                    schema_version=1,
                    document_name="test document",
                    decode=decoded.append,
                    fallback=dict,
                    warn=warnings.append,
                    on_revision=revisions.append,
                )
                for _ in range(2)
            ]
            self.assertEqual(results, [{}, {}])
            self.assertIsNot(results[0], results[1])
            self.assertEqual(decoded, [])
            self.assertEqual(revisions, [])
            self.assertEqual(len(warnings), 2)
            self.assertTrue(all("nesting" in warning for warning in warnings))
            self.assertEqual(path.read_bytes(), before)

    def test_legacy_reader_delegates_to_snapshot_payload(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "document.json"
            path.write_text(
                json.dumps({"schema_version": 1, "value": "stable"}),
                encoding="utf-8",
            )

            self.assertEqual(
                read_versioned_json(
                    path,
                    schema_version=1,
                    document_name="test document",
                )["value"],
                "stable",
            )

    def test_loader_revision_callback_only_runs_after_successful_decode(self):
        revisions = []
        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "document.json"
            path.write_text(
                json.dumps({"schema_version": 1, "value": "stable"}),
                encoding="utf-8",
            )
            loaded = load_versioned_json(
                path,
                schema_version=1,
                document_name="test document",
                decode=lambda payload: payload["value"],
                fallback=lambda: "fallback",
                warn=warnings.append,
                on_revision=revisions.append,
            )

            def fail_decode(_payload):
                raise ValueError("bad")

            failed = load_versioned_json(
                path,
                schema_version=1,
                document_name="test document",
                decode=fail_decode,
                fallback=lambda: "fallback",
                warn=warnings.append,
                on_revision=revisions.append,
            )
            path.write_text("not json", encoding="utf-8")
            malformed = load_versioned_json(
                path,
                schema_version=1,
                document_name="test document",
                decode=lambda payload: payload,
                fallback=dict,
                warn=warnings.append,
                on_revision=revisions.append,
            )
            path.unlink()
            missing = load_versioned_json(
                path,
                schema_version=1,
                document_name="test document",
                decode=lambda payload: payload,
                fallback=dict,
                warn=warnings.append,
                on_revision=revisions.append,
            )

        self.assertEqual(loaded, "stable")
        self.assertEqual(failed, "fallback")
        self.assertEqual(malformed, {})
        self.assertEqual(missing, {})
        self.assertEqual(len(revisions), 1)
        self.assertEqual(len(warnings), 2)

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

    def test_nonpositive_schema_versions_are_not_treated_as_compatible(self):
        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "document.json"
            results = []
            for version in (0, -1):
                path.write_text(
                    json.dumps({"schema_version": version, "value": "damaged"}),
                    encoding="utf-8",
                )
                results.append(
                    load_versioned_json(
                        path,
                        schema_version=2,
                        document_name="test document",
                        decode=lambda payload: payload["value"],
                        fallback=lambda: "fallback",
                        warn=warnings.append,
                        allow_older=True,
                    )
                )

        self.assertEqual(results, ["fallback", "fallback"])
        self.assertEqual(len(warnings), 2)

    def test_loader_bounds_document_read_after_size_changes(self):
        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "document.json"
            path.write_text(
                json.dumps({"schema_version": 1, "value": "x" * 256}),
                encoding="utf-8",
            )
            with patch("vntts.versioned_json._DOCUMENT_READ_LIMIT", 128, create=True):
                result = load_versioned_json(
                    path,
                    schema_version=1,
                    document_name="test document",
                    decode=lambda payload: payload["value"],
                    fallback=lambda: "fallback",
                    warn=warnings.append,
                )

        self.assertEqual(result, "fallback")
        self.assertIn("size limit", warnings[0])

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

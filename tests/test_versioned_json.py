import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

from durable_file import atomic_write_bytes, atomic_write_json

from vntts import versioned_json
from vntts.versioned_json import (
    load_versioned_json,
    read_versioned_json,
    read_versioned_json_snapshot,
    write_versioned_json,
    write_versioned_json_if_unchanged,
)


class VersionedJsonTest(unittest.TestCase):
    def test_missing_document_fallback_failure_propagates_once_without_damage_log(self):
        failure = ValueError("fallback factory failed")
        fallback = Mock(side_effect=failure)
        with TemporaryDirectory() as directory:
            with (
                self.assertNoLogs("vntts.versioned_json"),
                self.assertRaises(ValueError) as caught,
            ):
                load_versioned_json(
                    Path(directory) / "absent.json",
                    schema_version=1,
                    document_name="settings",
                    decode=dict,
                    fallback=fallback,
                )
        self.assertIs(caught.exception, failure)
        fallback.assert_called_once_with()

    def test_fallback_logs_document_damage_and_read_errors_but_not_missing_files(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"

            def load():
                return load_versioned_json(
                    path,
                    schema_version=1,
                    document_name="settings",
                    decode=dict,
                    fallback=dict,
                )

            with self.assertNoLogs("vntts.versioned_json"):
                self.assertEqual(load(), {})
            path.write_text("invalid JSON", encoding="utf-8")
            with self.assertLogs("vntts.versioned_json", level="ERROR") as logged:
                self.assertEqual(load(), {})
            self.assertIn(str(path), logged.output[0])
            self.assertIsInstance(logged.records[0].exc_info[1], ValueError)
            failure = PermissionError("access denied")
            with (
                patch.object(
                    versioned_json, "read_versioned_json_snapshot", side_effect=failure
                ),
                self.assertLogs("vntts.versioned_json", level="ERROR") as logged,
            ):
                self.assertEqual(load(), {})
            self.assertIs(logged.records[0].exc_info[1], failure)

    def test_writers_preserve_the_existing_json_bytes(self):
        documents = (
            {"value": "Zażółć 🐈", "nested": {"b": 2, "a": [None, True, 1.25]}},
            {"schema_version": 1, "value": ("first", "second")},
            {"schema_version": 1.0, "value": "equivalent supplied version"},
            {},
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for number, fields in enumerate(documents):
                with self.subTest(fields=fields):
                    expected = root / f"expected-{number}.json"
                    atomic_write_json(expected, dict(fields, schema_version=1))
                    before = dict(fields)
                    direct, guarded = (
                        root / f"direct-{number}.json",
                        root / f"guarded-{number}.json",
                    )
                    self.assertEqual(write_versioned_json(direct, 1, fields), direct)
                    revision = write_versioned_json_if_unchanged(
                        guarded, 1, fields, revision=None, document_name="test document"
                    )
                    self.assertEqual(direct.read_bytes(), expected.read_bytes())
                    self.assertEqual(guarded.read_bytes(), expected.read_bytes())
                    self.assertEqual(revision, sha256(expected.read_bytes()).digest())
                    self.assertEqual(fields, before)

    def test_guarded_writer_refuses_replaced_or_missing_published_document(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            foreign = b'{"schema_version":1,"value":"external"}\n'
            for disappeared in (False, True):
                with self.subTest(disappeared=disappeared):
                    path = root / f"document-{disappeared}.json"
                    write_versioned_json(path, 1, {"value": "before"})
                    previous = versioned_json.file_revision(path)

                    def replace_after_publication(destination, raw):
                        result = atomic_write_bytes(destination, raw)
                        if disappeared:
                            path.unlink()
                        else:
                            path.write_bytes(foreign)
                        return result

                    with (
                        patch.object(
                            versioned_json,
                            "atomic_write_bytes",
                            side_effect=replace_after_publication,
                        ) as publish,
                        self.assertRaisesRegex(
                            OSError,
                            "disappeared after saving"
                            if disappeared
                            else "changed while saving",
                        ),
                    ):
                        write_versioned_json_if_unchanged(
                            path,
                            1,
                            {"value": "candidate"},
                            revision=previous,
                            document_name="test document",
                        )
                    publish.assert_called_once()
                    self.assertEqual(path.exists(), not disappeared)
                    if not disappeared:
                        self.assertEqual(path.read_bytes(), foreign)
                    fresh = versioned_json.file_revision(path)
                    revision = write_versioned_json_if_unchanged(
                        path,
                        1,
                        {"value": "recovered"},
                        revision=fresh,
                        document_name="test document",
                    )
                    self.assertEqual(revision, sha256(path.read_bytes()).digest())

    def test_guarded_writer_keeps_its_receipt_when_replaced_after_verification(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "document.json"
            write_versioned_json(path, 1, {"value": "before"})
            previous = versioned_json.file_revision(path)
            foreign = b'{"schema_version":1,"value":"external"}\n'
            native_revision = versioned_json.file_revision
            revisions = []

            def replace_after_read(source):
                revision = native_revision(source)
                revisions.append(revision)
                if len(revisions) == 2:
                    path.write_bytes(foreign)
                return revision

            with patch.object(
                versioned_json, "file_revision", side_effect=replace_after_read
            ):
                owned = write_versioned_json_if_unchanged(
                    path,
                    1,
                    {"value": "candidate"},
                    revision=previous,
                    document_name="test document",
                )
            self.assertEqual(owned, revisions[1])
            self.assertNotEqual(owned, sha256(foreign).digest())
            with self.assertRaisesRegex(OSError, "changed on disk"):
                write_versioned_json_if_unchanged(
                    path,
                    1,
                    {"value": "next candidate"},
                    revision=owned,
                    document_name="test document",
                )
            self.assertEqual(path.read_bytes(), foreign)

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
        decoder_error = RecursionError("native parser limit")
        with (
            TemporaryDirectory() as directory,
            patch("vntts.json_types.json.loads", side_effect=decoder_error) as decode,
        ):
            path = Path(directory) / "document.json"
            payload = '{"schema_version":1,"value":[0]}'
            path.write_text(payload, encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(json.JSONDecodeError, "nesting") as caught:
                read_versioned_json(
                    path, schema_version=1, document_name="test document"
                )
            self.assertIs(caught.exception.__cause__, decoder_error)
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
            self.assertEqual(decode.call_args_list, [call(before)] * 3)
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
                    "vntts.versioned_json.atomic_write_bytes",
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

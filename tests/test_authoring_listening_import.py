import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file

from tests.authoring_fixtures import tree_hashes
from tests.listening_fixtures import write_listening_fixture
from vntts.authoring.cli import main
from vntts.authoring.listening_import import (
    IMPORT_SCHEMA,
    ListeningImportError,
    import_listening_session,
    inspect_listening_session,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)


class ListeningImportTest(unittest.TestCase):
    def test_atomic_publication_failure_is_translated_and_cleans_staging(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            failure = AtomicPublicationError("Atomic publication unavailable")
            with (
                patch(
                    "vntts.authoring.listening_import.rename_directory_no_replace",
                    side_effect=failure,
                ),
                self.assertRaisesRegex(
                    ListeningImportError, "Atomic publication unavailable"
                ) as caught,
            ):
                import_listening_session(source, root / "app-data")
            self.assertIs(caught.exception.__cause__, failure)
            self.assertEqual(list((root / "app-data").iterdir()), [])

    def test_rejects_unhashable_assignment_ids_and_rating_preferences(self):
        mutations = (
            ("assignment", "trial_id", []),
            ("rating", "preference", {}),
        )
        for kind, field, value in mutations:
            with (
                self.subTest(kind=kind, field=field),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = write_listening_fixture(root)
                session_path = source / "session.json"
                key_path = source / ".blind-key.json"
                session = json.loads(session_path.read_text(encoding="utf-8"))
                key = json.loads(key_path.read_text(encoding="utf-8"))
                if kind == "assignment":
                    key["assignments"][0][field] = value
                else:
                    session["trials"][0]["rating"][field] = value
                key_path.write_text(json.dumps(key, sort_keys=True), encoding="utf-8")
                session["blind_key_sha256"] = sha256_file(key_path)
                session_path.write_text(
                    json.dumps(session, sort_keys=True), encoding="utf-8"
                )
                source_hashes = tree_hashes(source)

                with self.assertRaises(ListeningImportError):
                    inspect_listening_session(source)
                with self.assertRaises(ListeningImportError):
                    import_listening_session(source, root / "app-data")

                self.assertFalse((root / "app-data").exists())
                self.assertEqual(
                    source_hashes,
                    tree_hashes(source),
                )

    def test_rejects_non_integer_schema_versions_and_session_counts(self):
        mutations = (
            ("session.json", "schema_version", True),
            (".blind-key.json", "schema_version", 1.0),
            ("report.json", "schema_version", True),
            ("report.json", "completed_trials", True),
            ("report.json", "completed_trials", 1.0),
            ("session.json", "completed_count", True),
            ("session.json", "completed_count", 1.0),
            ("session.json", "trial_count", True),
            ("session.json", "trial_count", 1.0),
        )
        for filename, field, value in mutations:
            with (
                self.subTest(filename=filename, field=field, value=value),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = write_listening_fixture(root)
                path = source / filename
                document = json.loads(path.read_text(encoding="utf-8"))
                document[field] = value
                path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
                if field == "trial_count":
                    (source / "report.json").unlink()

                with self.assertRaises(ListeningImportError):
                    inspect_listening_session(source)

    def test_reimport_rejects_non_integer_manifest_counts(self):
        mutations = (
            ("schema_version", True),
            ("schema_version", 1.0),
            ("summary.completed_count", True),
            ("summary.trial_count", 1.0),
        )
        for field, value in mutations:
            with (
                self.subTest(field=field, value=value),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source = write_listening_fixture(root)
                destination = import_listening_session(
                    source, root / "app-data"
                ).destination
                path = destination / "import.json"
                manifest = json.loads(path.read_text(encoding="utf-8"))
                target = (
                    manifest["summary"] if field.startswith("summary.") else manifest
                )
                target[field.removeprefix("summary.")] = value
                path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

                with self.assertRaisesRegex(
                    ListeningImportError, "manifest was modified"
                ):
                    import_listening_session(source, root / "app-data")

    def test_reimport_rejects_forged_manifest_and_modified_hidden_key_mode(self):
        mutations = ("artifacts", "summary")
        if os.name != "nt":
            mutations += ("mode",)
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                source = write_listening_fixture(root)
                first = import_listening_session(source, root / "app-data")
                if mutation == "mode":
                    key = first.destination / ".blind-key.json"
                    original_mode = key.stat().st_mode & 0o777
                    key.chmod(0o600 if original_mode != 0o600 else 0o644)
                    expected = "mode changed"
                else:
                    manifest_path = first.destination / "import.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if mutation == "artifacts":
                        manifest["artifacts"] = []
                    else:
                        manifest["summary"]["completed_count"] = 0
                    manifest_path.write_text(
                        json.dumps(manifest, sort_keys=True), encoding="utf-8"
                    )
                    expected = "manifest was modified"

                with self.assertRaisesRegex(ListeningImportError, expected):
                    import_listening_session(source, root / "app-data")

    def test_source_mutation_during_copy_aborts_before_publish(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            session_path = source / "session.json"
            original_copy = __import__("shutil").copy2
            mutated = False

            def mutate_after_report_copy(source_path, destination):
                nonlocal mutated
                result = original_copy(source_path, destination)
                if Path(source_path).name == "report.json" and not mutated:
                    session = json.loads(session_path.read_text(encoding="utf-8"))
                    session["updated_at"] = "2026-08-15T10:00:00+00:00"
                    session_path.write_text(
                        json.dumps(session, sort_keys=True), encoding="utf-8"
                    )
                    mutated = True
                return result

            with (
                patch(
                    "vntts.authoring.listening_import.shutil.copy2",
                    side_effect=mutate_after_report_copy,
                ),
                self.assertRaisesRegex(ListeningImportError, "retry when idle"),
            ):
                import_listening_session(source, root / "app-data")

            self.assertEqual(list((root / "app-data").iterdir()), [])

    def test_keyboard_interrupt_cleans_staging_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            original_copy = __import__("shutil").copy2

            def copy_and_interrupt(source_path, destination):
                original_copy(source_path, destination)
                raise KeyboardInterrupt

            with patch(
                "vntts.authoring.listening_import.shutil.copy2",
                side_effect=copy_and_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    import_listening_session(source, root / "app-data")

            self.assertEqual(list((root / "app-data").iterdir()), [])

    def test_blind_audio_mutation_during_copy_aborts_before_publish(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            audio_path = source / "audio/trial-0001-a.wav"
            original_copy = __import__("shutil").copy2
            mutated = False

            def mutate_after_audio_copy(source_path, destination):
                nonlocal mutated
                result = original_copy(source_path, destination)
                if Path(source_path).resolve() == audio_path.resolve() and not mutated:
                    audio_path.write_bytes(b"changed after copy")
                    mutated = True
                return result

            with (
                patch(
                    "vntts.authoring.listening_import.shutil.copy2",
                    side_effect=mutate_after_audio_copy,
                ),
                self.assertRaisesRegex(ListeningImportError, "retry when idle"),
            ):
                import_listening_session(source, root / "app-data")

            self.assertEqual(list((root / "app-data").iterdir()), [])

    def test_session_semantics_are_bound_to_the_exact_snapshotted_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            session_path = source / "session.json"

            def mutate_after_validation(_root, session):
                mutated = dict(session)
                mutated["updated_at"] = "2026-08-15T10:00:00+00:00"
                session_path.write_text(
                    json.dumps(mutated, sort_keys=True), encoding="utf-8"
                )
                return session["trials"], {
                    Path("audio/trial-0001-a.wav"): source / "audio/trial-0001-a.wav",
                    Path("audio/trial-0001-b.wav"): source / "audio/trial-0001-b.wav",
                }

            with (
                patch(
                    "vntts.authoring.listening_import._validate_session",
                    side_effect=mutate_after_validation,
                ),
                self.assertRaisesRegex(ListeningImportError, "changed"),
            ):
                import_listening_session(source, root / "app-data")

            self.assertFalse((root / "app-data").exists())

    def test_import_preserves_session_key_report_audio_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            source_hashes = tree_hashes(source)

            inspection = inspect_listening_session(source)
            first = import_listening_session(source, root / "app-data")
            second = import_listening_session(source, root / "app-data")

            self.assertEqual(inspection.trial_count, 1)
            self.assertEqual(inspection.audio_count, 2)
            self.assertEqual(first.manifest["schema"], IMPORT_SCHEMA)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.destination, second.destination)
            self.assertTrue((first.destination / ".blind-key.json").is_file())
            self.assertEqual(
                source_hashes,
                tree_hashes(source),
            )

    def test_concurrent_import_does_not_overwrite_existing_destination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)

            def publish_first(staging, destination):
                shutil.copytree(staging, destination)
                (Path(destination) / "published-elsewhere").write_text("keep")
                return rename_directory_no_replace(staging, destination)

            with patch(
                "vntts.authoring.listening_import.rename_directory_no_replace",
                side_effect=publish_first,
            ):
                result = import_listening_session(source, root / "app-data")

            self.assertFalse(result.created)
            self.assertEqual(
                (result.destination / "published-elsewhere").read_text(), "keep"
            )

    def test_rejects_changed_key_path_escape_and_inconsistent_report(self):
        mutations = ("key", "path", "audio", "report")
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                source = write_listening_fixture(root)
                if mutation == "key":
                    key_path = source / ".blind-key.json"
                    key = json.loads(key_path.read_text(encoding="utf-8"))
                    key["assignments"][0]["a"]["model_id"] = "provider/model-two"
                    key_path.write_text(
                        json.dumps(key, sort_keys=True), encoding="utf-8"
                    )
                elif mutation == "path":
                    session_path = source / "session.json"
                    session = json.loads(session_path.read_text(encoding="utf-8"))
                    session["trials"][0]["audio"]["a"] = "../escape.wav"
                    session_path.write_text(
                        json.dumps(session, sort_keys=True), encoding="utf-8"
                    )
                elif mutation == "audio":
                    (source / "audio/trial-0001-a.wav").write_bytes(b"tampered")
                else:
                    report_path = source / "report.json"
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    report["completed_trials"] = 0
                    report_path.write_text(
                        json.dumps(report, sort_keys=True), encoding="utf-8"
                    )

                with self.assertRaises(ListeningImportError):
                    inspect_listening_session(source)

    def test_changed_session_after_import_is_a_hard_conflict(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            first = import_listening_session(source, root / "app-data")
            imported_session_hash = sha256_file(first.destination / "session.json")
            session_path = source / "session.json"
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["updated_at"] = "2026-08-15T10:00:00+00:00"
            session_path.write_text(
                json.dumps(session, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaisesRegex(ListeningImportError, "changed after import"):
                import_listening_session(source, root / "app-data")

            self.assertEqual(
                sha256_file(first.destination / "session.json"), imported_session_hash
            )

    def test_cli_inspection_is_read_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                result = main(["inspect-listening", str(source)])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(payload["trial_count"], 1)
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                [
                    "listening-session",
                    "source-a.wav",
                    "source-b.wav",
                    "source-report.json",
                ],
            )


if __name__ == "__main__":
    unittest.main()

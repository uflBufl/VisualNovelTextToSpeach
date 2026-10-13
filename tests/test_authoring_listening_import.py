import io
import json
import os
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file

from tests.authoring_fixtures import tree_hashes
from tests.listening_fixtures import write_listening_fixture
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from vntts.authoring import listening_import as listening_import_module
from vntts.authoring.cli import main
from vntts.authoring.legacy_import import discover_legacy_jobs
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
    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_public_hash_acquisitions_refuse_fifo_clean_up_and_recover(self):
        for phase in ("initial", "prepublication"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source = write_listening_fixture(root)
                inspection = inspect_listening_session(source)
                selected = source / "audio/trial-0001-a.wav"
                destination = root / "app-data"
                before = tree_hashes(root)
                original = listening_import_module.sha256_file
                refused = []

                def checksum(path):
                    staged_manifest = any(destination.glob(".*/import.json"))
                    if Path(path) != selected or (
                        phase == "prepublication" and not staged_manifest
                    ):
                        return original(path)
                    self.assertEqual(staged_manifest, phase == "prepublication")
                    gate: RegularFileGate = {"path": selected, "armed": True}
                    with (
                        swap_at_acquisition(self, gate),
                        self.assertRaisesRegex(OSError, "regular file") as caught,
                    ):
                        original(path)
                    refused.append(caught.exception)
                    raise caught.exception

                with (
                    patch.object(listening_import_module, "sha256_file", checksum),
                    self.assertRaisesRegex(
                        ListeningImportError, "regular file"
                    ) as caught,
                ):
                    import_listening_session(source, destination)
                self.assertEqual(len(refused), 1)
                self.assertIs(caught.exception.__cause__, refused[0])
                self.assertEqual(tree_hashes(root), before)
                if destination.exists():
                    self.assertEqual(list(destination.iterdir()), [])
                recovered = import_listening_session(source, destination)
                self.assertTrue(recovered.created)
                self.assertEqual(
                    recovered.manifest["source"]["logical_identity"],
                    inspection.logical_identity,
                )
                for _role, original_source, relative, digest in inspection.artifacts:
                    copied = recovered.destination / relative
                    self.assertEqual(copied.read_bytes(), original_source.read_bytes())
                    self.assertEqual(sha256_file(copied), digest)
                recovered_tree = tree_hashes(root)
                repeated = import_listening_session(source, destination)
                self.assertFalse(repeated.created)
                self.assertEqual(repeated.destination, recovered.destination)
                self.assertEqual(repeated.manifest, recovered.manifest)
                self.assertEqual(tree_hashes(root), recovered_tree)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_copy_sources_refuse_swapped_acquisition_and_recover(self):
        for relative in (".blind-key.json", "audio/trial-0001-a.wav"):
            with self.subTest(source=relative), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source = write_listening_fixture(root)
                inspection = inspect_listening_session(source)
                selected = source / relative
                mode, mtime = (
                    selected.stat().st_mode & 0o777,
                    selected.stat().st_mtime_ns,
                )
                before = tree_hashes(root)
                original = listening_import_module.copy_regular_file
                refused = []

                def copy(path, target, **kwargs):
                    if Path(path) != selected:
                        return original(path, target, **kwargs)
                    self.assertEqual(kwargs, {"preserve_metadata": True})
                    gate: RegularFileGate = {"path": selected, "armed": True}
                    with (
                        swap_at_acquisition(self, gate),
                        self.assertRaisesRegex(OSError, "regular file") as caught,
                    ):
                        original(path, target, **kwargs)
                    self.assertFalse(Path(target).exists())
                    refused.append(caught.exception)
                    raise caught.exception

                with (
                    patch.object(listening_import_module, "copy_regular_file", copy),
                    self.assertRaisesRegex(
                        ListeningImportError, "regular file"
                    ) as caught,
                ):
                    import_listening_session(source, root / "app-data")
                self.assertEqual(len(refused), 1)
                self.assertIs(caught.exception.__cause__, refused[0])
                self.assertEqual(tree_hashes(root), before)
                self.assertEqual(list((root / "app-data").iterdir()), [])
                # The acquisition fixture restores bytes/mode; restore original copy mtime.
                os.utime(selected, ns=(mtime, mtime))
                recovered = import_listening_session(source, root / "app-data")
                repeated = import_listening_session(source, root / "app-data")
                self.assertTrue(recovered.created)
                self.assertFalse(repeated.created)
                self.assertEqual(recovered.destination, repeated.destination)
                self.assertEqual(
                    recovered.manifest["source"]["logical_identity"],
                    inspection.logical_identity,
                )
                artifacts = {
                    path: digest
                    for _role, _source, path, digest in inspection.artifacts
                }
                self.assertEqual(
                    {
                        str(path): sha256_file(recovered.destination / path)
                        for path in artifacts
                    },
                    {str(path): digest for path, digest in artifacts.items()},
                )
                copied = recovered.destination / relative
                self.assertEqual(copied.stat().st_mode & 0o777, mode)
                self.assertEqual(copied.stat().st_mtime_ns, mtime)

    def test_final_inspection_read_failure_preserves_cause_and_cli_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            before = tree_hashes(root)
            failure = PermissionError("Final listening source read was blocked")
            original_check = listening_import_module._verify_controls_unchanged

            def fail_final_read(inspection):
                with patch.object(
                    listening_import_module, "sha256_file", side_effect=failure
                ):
                    return original_check(inspection)

            with patch.object(
                listening_import_module,
                "_verify_controls_unchanged",
                side_effect=fail_final_read,
            ):
                with self.assertRaises(ListeningImportError) as caught:
                    inspect_listening_session(source)
                self.assertIs(caught.exception.__cause__, failure)
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as exited,
                ):
                    main(["inspect-listening", str(source)])
                self.assertEqual(exited.exception.code, 2)
                self.assertIn(str(failure), stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(tree_hashes(root), before)

    def test_initial_inspection_read_failure_preserves_cause_and_user_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            jobs_root = root / "jobs"
            jobs_root.mkdir()
            before = tree_hashes(root)
            failure = PermissionError("Initial listening audio read was blocked")
            with patch.object(
                listening_import_module, "sha256_file", side_effect=failure
            ):
                with self.assertRaises(ListeningImportError) as caught:
                    inspect_listening_session(source)
                self.assertIs(caught.exception.__cause__, failure)
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as exited,
                ):
                    main(["inspect-listening", str(source)])
                self.assertEqual(exited.exception.code, 2)
                self.assertIn(str(failure), stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")
                candidates = discover_legacy_jobs(jobs_root)
                listening = [
                    candidate
                    for candidate in candidates
                    if candidate.kind == "model-listening-session"
                ]
                self.assertEqual(len(listening), 1)
                self.assertEqual(listening[0].status, "incompatible")
                self.assertIn(str(failure), listening[0].compatibility_error)
            self.assertEqual(tree_hashes(root), before)

    def test_admitted_report_disappearance_aborts_before_publish(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            report_path = source / "report.json"
            report_bytes = report_path.read_bytes()
            original_validate = listening_import_module._validate_report

            def remove_validated_report(*args):
                original_validate(*args)
                report_path.unlink()

            with (
                patch.object(
                    listening_import_module,
                    "_validate_report",
                    side_effect=remove_validated_report,
                ),
                self.assertRaisesRegex(ListeningImportError, "presence changed"),
            ):
                import_listening_session(source, root / "app-data")
            self.assertFalse((root / "app-data").exists())
            report_path.write_bytes(report_bytes)
            self.assertTrue(import_listening_session(source, root / "app-data").created)

    def test_late_report_appearance_cleans_staging_and_reportless_import_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            report_path = source / "report.json"
            report_bytes = report_path.read_bytes()
            report_path.unlink()
            inspection = inspect_listening_session(source)
            self.assertFalse(inspection.report_present)
            self.assertNotIn(
                "listening_report", [role for role, *_ in inspection.artifacts]
            )
            original_copy = listening_import_module.copy_regular_file

            def create_report_during_copy(source_path, destination, **kwargs):
                original_copy(source_path, destination, **kwargs)
                if Path(source_path).name == "session.json":
                    report_path.write_bytes(report_bytes)

            with (
                patch.object(
                    listening_import_module,
                    "copy_regular_file",
                    side_effect=create_report_during_copy,
                ),
                self.assertRaisesRegex(ListeningImportError, "presence changed"),
            ):
                import_listening_session(source, root / "app-data")
            self.assertEqual(list((root / "app-data").iterdir()), [])
            report_path.unlink()
            recovered = import_listening_session(source, root / "app-data")
            self.assertTrue(recovered.created)
            self.assertFalse(recovered.manifest["summary"]["report_present"])
            self.assertFalse((recovered.destination / "report.json").exists())
            self.assertFalse(
                import_listening_session(source, root / "app-data").created
            )

    def test_reimport_rejects_report_appearance_after_inspection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            report_path = source / "report.json"
            report_bytes = report_path.read_bytes()
            report_path.unlink()
            destination = import_listening_session(
                source, root / "app-data"
            ).destination
            before = tree_hashes(destination)

            def create_report_after_inspection(session_directory):
                inspection = inspect_listening_session(session_directory)
                report_path.write_bytes(report_bytes)
                return inspection

            with (
                patch.object(
                    listening_import_module,
                    "inspect_listening_session",
                    side_effect=create_report_after_inspection,
                ),
                self.assertRaisesRegex(ListeningImportError, "presence changed"),
            ):
                import_listening_session(source, root / "app-data")
            self.assertEqual(tree_hashes(destination), before)

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

    def test_reimport_reports_surrogate_manifest_metadata_as_domain_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_listening_fixture(root)
            destination = import_listening_session(
                source, root / "app-data"
            ).destination
            manifest_path = destination / "import.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["metadata"] = {"label": "\ud800"}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            before = tree_hashes(root)

            with self.assertRaisesRegex(ListeningImportError, "manifest was modified"):
                import_listening_session(source, root / "app-data")

            self.assertEqual(tree_hashes(root), before)

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
            original_copy = listening_import_module.copy_regular_file
            mutated = False

            def mutate_after_report_copy(source_path, destination, **kwargs):
                nonlocal mutated
                result = original_copy(source_path, destination, **kwargs)
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
                    "vntts.authoring.listening_import.copy_regular_file",
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
            original_copy = listening_import_module.copy_regular_file

            def copy_and_interrupt(source_path, destination, **kwargs):
                original_copy(source_path, destination, **kwargs)
                raise KeyboardInterrupt

            with patch(
                "vntts.authoring.listening_import.copy_regular_file",
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
            original_copy = listening_import_module.copy_regular_file
            mutated = False

            def mutate_after_audio_copy(source_path, destination, **kwargs):
                nonlocal mutated
                result = original_copy(source_path, destination, **kwargs)
                if Path(source_path).resolve() == audio_path.resolve() and not mutated:
                    audio_path.write_bytes(b"changed after copy")
                    mutated = True
                return result

            with (
                patch(
                    "vntts.authoring.listening_import.copy_regular_file",
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

"""Public upgrade behavior: preserve authority and paid audio across the switch."""

import hashlib
import json
import os
import shutil
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.audio import write_pcm16_wav

import vntts.storage_upgrade as upgrade_module
from tests.generation_state_fixtures import replace_generation_state
from tests.symlink_support import symlink_or_skip
from tests.test_sqlite_generation_integration import legacy_worker_output
from vntts.audio_cache import PersistentAudioCache
from vntts.audio_store import SharedAudioStore
from vntts.authoring.generation_failures import failure_record
from vntts.authoring.generation_lease import GenerationLease, process_is_alive
from vntts.generation_database import read_state
from vntts.pregeneration_queue import load_generation_input_metadata
from vntts.pregeneration_setup import PregenerationJobStore, job_schema_version
from vntts.pregeneration_voices import VoicePlanStore, voice_plan_schema_version
from vntts.preparation_catalog import (
    PreparationCatalog,
    PreparationCatalogError,
    PreparationCatalogUpgradeRequired,
)
from vntts.storage_upgrade import (
    StorageUpgradeError,
    readback_main,
    storage_upgrade_required,
    upgrade_storage,
)
from vntts.synthesis import SynthesisCompletion
from vntts.versioned_json import write_versioned_json


class StorageUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name).resolve()
        self.root = self.parent / "data"
        self.root.mkdir()
        self.backup = self.parent / "backup"

    def snapshot(self, directory):
        if not directory.exists():
            return None
        return {
            str(path.relative_to(directory)): (
                ("link", os.readlink(path))
                if path.is_symlink()
                else ("directory",)
                if path.is_dir()
                else ("file", path.read_bytes())
            )
            for path in directory.rglob("*")
        }

    def assert_refused_without_mutation(self, *, backup=None):
        backup = backup or self.backup
        before, backup_before = self.snapshot(self.root), self.snapshot(backup)
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            upgrade_storage(self.root, backup, apply=True)
        self.assertEqual(self.snapshot(self.root), before)
        self.assertEqual(self.snapshot(backup), backup_before)

    def assert_backed_up(self, source, original):
        relative = source.relative_to(self.root).parts
        candidates = [
            path
            for path in self.backup.rglob(source.name)
            if path.parts[-len(relative) :] == relative
        ]
        self.assertTrue(any(path.read_bytes() == original for path in candidates))

    def legacy_audio(self):
        directory = self.root / "audio-cache" / "moss-tts"
        source = self.parent / "paid.wav"
        write_pcm16_wav(source, [0.1, -0.1] * 60, 16000)
        store = SharedAudioStore(directory)
        saved = store.put("paid", source)
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        binding = directory / "bindings" / "paid.json"
        write_versioned_json(binding, 1, {"audio_sha256": digest})
        store.catalog_path.unlink()
        return store, saved, binding

    def legacy_preparation(self):
        prepared, plan, generated = legacy_worker_output(self.root / "pregeneration")
        jobs = PregenerationJobStore(self.root / "pregeneration" / "jobs")
        job = jobs.load(plan.job_id)
        metadata = load_generation_input_metadata(prepared.directory)
        directory = jobs.directory_for(job.job_id)
        documents = {
            directory / "job.json": (job_schema_version, job.to_document()),
            directory / "voice-plan.json": (
                voice_plan_schema_version,
                plan.to_document(),
            ),
            prepared.directory / "input.json": (4, metadata),
            jobs.root / "selections" / f"{job.catalog_story_index_sha256}.json": (
                1,
                {
                    "story_index_sha256": job.catalog_story_index_sha256,
                    "selected_story_ids": list(job.selected_story_ids),
                },
            ),
        }
        for path, (version, document) in documents.items():
            write_versioned_json(path, version, document)
        jobs.catalog.path.unlink()
        jobs.catalog.required_marker.unlink()
        sources = {path: path.read_bytes() for path in (*documents, generated.state)}
        return prepared, plan, job, sources

    def test_detection_finds_each_legacy_authority_without_mutation(self):
        catalog = PreparationCatalog(self.root / "pregeneration/jobs", owned_root=True)
        catalog.initialize()
        before = self.snapshot(self.root)
        self.assertFalse(storage_upgrade_required(self.root))
        self.assertEqual(self.snapshot(self.root), before)
        job_id = "a" * 24
        for relative in (
            f"pregeneration/jobs/{job_id}/job.json",
            f"pregeneration/jobs/{job_id}/voice-plan.json",
            f"pregeneration/jobs/{job_id}/generation-input-test/input.json",
            "pregeneration/jobs/selections/selection.json",
            "pregeneration/voice-decisions.json",
            "audio-cache/moss-tts/bindings/key.json",
        ):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
                before = self.snapshot(self.root)
                self.assertTrue(storage_upgrade_required(self.root))
                self.assertEqual(self.snapshot(self.root), before)
                path.unlink()
        absent = self.parent / "not-created"
        self.assertFalse(storage_upgrade_required(absent))
        self.assertFalse(absent.exists())

    def test_detection_recognizes_v1_and_incomplete_preparation_without_upgrading(self):
        for version_one in (False, True):
            with self.subTest(version_one=version_one):
                root = self.root / str(version_one)
                catalog = PreparationCatalog(
                    root / "pregeneration/jobs", owned_root=True
                )
                catalog.initialize()
                with closing(sqlite3.connect(catalog.path)) as connection:
                    if version_one:
                        connection.execute("DROP TABLE input_publications")
                        connection.execute("DROP TABLE catalog_state")
                        connection.execute("PRAGMA user_version=1")
                    else:
                        connection.execute(
                            "UPDATE catalog_state SET migration_complete=0"
                        )
                    connection.commit()
                before = self.snapshot(root)
                self.assertTrue(storage_upgrade_required(root))
                self.assertEqual(self.snapshot(root), before)

    def test_reserved_job_directories_without_authority_offer_safe_initialization(self):
        jobs = self.root / "pregeneration/jobs"
        for name in ("selections", "story-snapshots"):
            (jobs / name).mkdir(parents=True)
        before = self.snapshot(self.root)
        self.assertTrue(storage_upgrade_required(self.root))
        self.assertEqual(self.snapshot(self.root), before)
        self.assertFalse((jobs / "preparation.sqlite3").exists())

    def test_detection_refuses_lost_or_corrupt_preparation_before_offering_upgrade(
        self,
    ):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                root = self.root / str(corrupt)
                catalog = PreparationCatalog(
                    root / "pregeneration/jobs", owned_root=True
                )
                catalog.initialize()
                if corrupt:
                    catalog.path.write_bytes(b"corrupt SQLite")
                else:
                    catalog.path.unlink()
                before = self.snapshot(root)
                with self.assertRaises(PreparationCatalogError) as caught:
                    storage_upgrade_required(root)
                self.assertNotIsInstance(
                    caught.exception, PreparationCatalogUpgradeRequired
                )
                self.assertEqual(self.snapshot(root), before)

    def test_legacy_job_estimate_defaults_are_admitted_without_inventing_identity(self):
        _prepared, _plan, job, _sources = self.legacy_preparation()
        path = self.root / "pregeneration/jobs" / job.job_id / "job.json"
        document = json.loads(path.read_bytes())
        for field in ("game_id", "source_story_index", "source_story_index_sha256"):
            document.pop(field, None)
        for field in ("generation_text_characters", "rough_audio_seconds"):
            document["estimate"].pop(field)
        path.write_text(json.dumps(document), encoding="utf-8")
        original = path.read_bytes()
        self.assertTrue(storage_upgrade_required(self.root))
        upgrade_storage(self.root, self.backup, apply=True)
        loaded = PregenerationJobStore(self.root / "pregeneration/jobs").load(
            job.job_id
        )
        self.assertEqual(loaded.selected_line_ids, job.selected_line_ids)
        self.assertIsNone(loaded.game_id)
        self.assertIsNone(loaded.source_story_index)
        self.assertEqual(loaded.estimate.generation_text_characters, 0)
        self.assertEqual(loaded.estimate.rough_audio_seconds, 0)
        self.assert_backed_up(path, original)
        self.assertFalse(storage_upgrade_required(self.root))

    def test_legacy_job_unknown_or_conflicting_fields_are_still_refused(self):
        _prepared, _plan, job, _sources = self.legacy_preparation()
        path = self.root / "pregeneration/jobs" / job.job_id / "job.json"
        document = json.loads(path.read_bytes())
        for mutation in (
            "unknown_job",
            "unknown_estimate",
            "job_identity",
            "invalid_default",
        ):
            with self.subTest(mutation=mutation):
                value = json.loads(json.dumps(document))
                if mutation == "unknown_job":
                    value["unknown"] = "preserve me"
                elif mutation == "unknown_estimate":
                    value["estimate"]["unknown"] = 1
                elif mutation == "job_identity":
                    value["job_id"] = "b" * 24
                else:
                    value["estimate"]["rough_audio_seconds"] = None
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assert_refused_without_mutation()

    def test_legacy_voice_decisions_are_staged_and_verified_before_retirement(self):
        path = self.root / "pregeneration/voice-decisions.json"
        path.parent.mkdir(parents=True)
        context = "a" * 64
        group_id = "b" * 64
        key = upgrade_module._decision_key(group_id, context)
        write_versioned_json(
            path,
            1,
            {
                "decisions": {
                    key: {
                        "group_id": group_id,
                        "decision_context_sha256": context,
                        "source_id": "preset:alba",
                        "decided_at": "2026-10-15T00:00:00Z",
                    }
                }
            },
        )
        original = path.read_bytes()
        self.assertTrue(storage_upgrade_required(self.root))
        before = self.snapshot(self.root)
        upgrade_storage(self.root, self.backup)
        self.assertEqual(self.snapshot(self.root), before)
        upgrade_storage(self.root, self.backup, apply=True)
        catalog = PreparationCatalog(self.root / "pregeneration/jobs", owned_root=True)
        self.assertEqual(catalog.decision(key), "preset:alba")
        self.assertFalse(path.exists())
        self.assert_backed_up(path, original)

    def test_current_audio_detection_uses_only_read_only_catalog_metadata(self):
        store = SharedAudioStore(self.root / "audio-cache/moss-tts")
        store.initialize()
        before = self.snapshot(self.root)
        with (
            patch.object(
                upgrade_module._Upgrade,
                "capture",
                side_effect=AssertionError("full catalog read"),
            ),
            patch.object(
                upgrade_module._Upgrade,
                "verify_sources",
                side_effect=AssertionError("catalog rehash"),
            ),
        ):
            self.assertFalse(storage_upgrade_required(self.root))
        self.assertEqual(self.snapshot(self.root), before)
        with closing(sqlite3.connect(store.catalog_path)) as connection:
            connection.execute("DROP TABLE unavailable_keys")
            connection.execute(
                "CREATE TABLE legacy_errors (filename TEXT PRIMARY KEY NOT NULL, error TEXT NOT NULL)"
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        before = self.snapshot(self.root)
        self.assertTrue(storage_upgrade_required(self.root))
        self.assertEqual(self.snapshot(self.root), before)

    def test_frozen_fresh_readback_uses_result_file_instead_of_stdout(self):
        catalog = PreparationCatalog(self.root / "pregeneration/jobs", owned_root=True)
        catalog.initialize()
        digest = upgrade_module._readback_digest(catalog.path, "preparation")
        output_paths = []

        def child(command, **_options):
            self.assertEqual(
                command[:4],
                [
                    upgrade_module.sys.executable,
                    "--storage-upgrade-readback",
                    str(catalog.path),
                    "preparation",
                ],
            )
            result_path = Path(command[4])
            output_paths.append(result_path)
            with patch.object(upgrade_module.sys, "stdout", None):
                self.assertEqual(readback_main(command[2:]), 0)
            self.assertEqual(result_path.read_text(encoding="ascii").strip(), digest)
            return CompletedProcess(command, 0, stdout=None)

        with (
            patch.object(upgrade_module.sys, "frozen", True, create=True),
            patch.object(upgrade_module.subprocess, "run", side_effect=child),
        ):
            upgrade_module._fresh_readback(
                catalog.path, "preparation", catalog.path.read_bytes()
            )
        self.assertEqual(len(output_paths), 1)
        self.assertFalse(output_paths[0].parent.exists())

    def test_readback_rejects_unknown_paths_kinds_and_existing_result_files(self):
        catalog = PreparationCatalog(self.root / "pregeneration/jobs", owned_root=True)
        catalog.initialize()
        output = self.parent / "readback.txt"
        for arguments in (
            (),
            (str(catalog.path), "preparation"),
            (str(catalog.path), "unknown", str(output)),
            (str(catalog.path), "preparation", "relative-result"),
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(StorageUpgradeError),
            ):
                readback_main(arguments)
        output.write_text("existing evidence", encoding="ascii")
        with self.assertRaises(FileExistsError):
            readback_main((str(catalog.path), "preparation", str(output)))
        self.assertEqual(output.read_text(encoding="ascii"), "existing evidence")

    def test_frozen_readback_preserves_child_failure_without_stdout(self):
        catalog = PreparationCatalog(self.root / "pregeneration/jobs", owned_root=True)
        catalog.initialize()
        payload = catalog.path.read_bytes()
        failure = RuntimeError("Child catalog admission failed")

        def child(command, **options):
            self.assertFalse(options["check"])
            with (
                patch.object(upgrade_module.sys, "stdout", None),
                patch.object(upgrade_module, "_readback_digest", side_effect=failure),
            ):
                returncode = readback_main(command[2:])
            self.assertEqual(returncode, 1)
            return CompletedProcess(command, returncode, stdout=None, stderr=None)

        with (
            patch.object(upgrade_module.sys, "frozen", True, create=True),
            patch.object(upgrade_module.subprocess, "run", side_effect=child),
            self.assertRaisesRegex(
                StorageUpgradeError, "Child catalog admission failed"
            ),
        ):
            upgrade_module._fresh_readback(catalog.path, "preparation", payload)
        self.assertEqual(catalog.path.read_bytes(), payload)

    def test_dry_run_reads_the_complete_inventory_without_creating_any_files(self):
        self.legacy_audio()
        prepared, _plan, _job, _sources = self.legacy_preparation()
        before = self.snapshot(self.root)
        report = upgrade_storage(self.root, self.backup)
        self.assertFalse(report["applied"])
        self.assertGreaterEqual(report["counts"]["generation_states"], 1)
        self.assertEqual(self.snapshot(self.root), before)
        self.assertFalse(self.backup.exists())
        self.assertFalse(
            (prepared.output_directory / "generation-state.sqlite3").exists()
        )

    def test_apply_backups_and_admits_jobs_selections_plans_and_input_metadata(self):
        prepared, plan, job, sources = self.legacy_preparation()
        report = upgrade_storage(self.root, self.backup, apply=True)
        self.assertTrue(report["applied"])
        jobs = PregenerationJobStore(self.root / "pregeneration/jobs")
        self.assertEqual(jobs.load(job.job_id), job)
        self.assertEqual(
            jobs.catalog.selection(job.catalog_story_index_sha256),
            job.selected_story_ids,
        )
        self.assertEqual(
            VoicePlanStore(jobs).saved_documents()[0]["synthesis_controls_sha256"],
            plan.synthesis_controls_sha256,
        )
        self.assertEqual(
            load_generation_input_metadata(prepared.directory)["identity"],
            prepared.identity,
        )
        for path, original in sources.items():
            self.assertFalse(path.exists())
            self.assert_backed_up(path, original)

    def test_canonical_story_snapshots_are_preserved_as_content_during_upgrade(self):
        self.legacy_preparation()
        snapshot = (
            self.root / "pregeneration/jobs/story-snapshots/frozen/story-index.jsonl"
        )
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(b"canonical frozen story content")
        before = snapshot.read_bytes(), snapshot.stat().st_ino
        upgrade_storage(self.root, self.backup, apply=True)
        self.assertEqual((snapshot.read_bytes(), snapshot.stat().st_ino), before)
        self.assertEqual(
            len(PregenerationJobStore(self.root / "pregeneration/jobs").catalog.jobs()),
            1,
        )

    def test_audio_binding_upgrade_and_repeat_preserve_the_exact_wav_inode(self):
        store, saved, binding = self.legacy_audio()
        before, content, original = (
            saved.stat(),
            saved.read_bytes(),
            binding.read_bytes(),
        )
        for _ in range(2):
            upgrade_storage(self.root, self.backup, apply=True)
            restored = SharedAudioStore(store.directory).get("paid")
            self.assertTrue(restored.samefile(saved))
            self.assertEqual(
                (restored.stat().st_dev, restored.stat().st_ino),
                (before.st_dev, before.st_ino),
            )
            self.assertEqual(restored.read_bytes(), content)
        self.assertFalse(binding.exists())
        self.assert_backed_up(binding, original)

    def test_obsolete_numpy_decoy_does_not_reserve_a_synthesis_key(self):
        directory = self.root / "audio-cache/moss-tts"
        directory.mkdir(parents=True)
        key = "a" * 48
        (directory / f"{key}.npy").write_bytes(b"obsolete unconfirmed artifact")
        upgrade_storage(self.root, self.backup, apply=True)
        cache = PersistentAudioCache(directory, sample_rate=16000)
        self.assertIsNone(cache.get_existing(key))
        saved = cache.put(key, [0.125])
        self.assertEqual(cache.store.get(key), saved)
        self.assertEqual(list(self.backup.rglob("*.npy")), [])

    def test_current_generation_json_becomes_sql_without_replacing_recordings(self):
        prepared, _plan, generated = legacy_worker_output(self.root / "pregeneration")
        original = generated.state.read_bytes()
        document = json.loads(original)
        recordings = {
            prepared.output_directory / item["path"]: (
                (prepared.output_directory / item["path"]).stat(),
                (prepared.output_directory / item["path"]).read_bytes(),
            )
            for item in document["items"].values()
        }
        upgrade_storage(self.root, self.backup, apply=True)
        self.assertEqual(
            read_state(prepared.output_directory / "generation-state.sqlite3"), document
        )
        self.assertFalse(generated.state.exists())
        self.assert_backed_up(generated.state, original)
        for path, (before, content) in recordings.items():
            self.assertEqual(
                (path.stat().st_dev, path.stat().st_ino), (before.st_dev, before.st_ino)
            )
            self.assertEqual(path.read_bytes(), content)

    def test_interrupted_generation_publication_keeps_sources_and_retry_succeeds(self):
        prepared, _plan, _job, sources = self.legacy_preparation()
        replace = upgrade_module.os.replace
        target = prepared.output_directory / "generation-state.sqlite3"

        def interrupt(source, destination):
            replace(source, destination)
            if Path(destination) == target:
                raise OSError("interrupted after committed generation publication")

        with (
            patch.object(upgrade_module.os, "replace", side_effect=interrupt),
            self.assertRaises((OSError, StorageUpgradeError)),
        ):
            upgrade_storage(self.root, self.backup, apply=True)
        for path, original in sources.items():
            self.assertEqual(path.read_bytes(), original)
            self.assert_backed_up(path, original)
        self.assertTrue(
            (prepared.output_directory / "generation-state.sqlite3").is_file()
        )
        upgrade_storage(self.root, self.backup, apply=True)
        self.assertTrue(
            read_state(prepared.output_directory / "generation-state.sqlite3")["items"]
        )
        self.assertTrue(all(not path.exists() for path in sources))

    def test_readback_failure_never_retires_the_original_generation_json(self):
        prepared, _plan, generated = legacy_worker_output(self.root / "pregeneration")
        original = generated.state.read_bytes()
        with (
            patch.object(
                upgrade_module.subprocess,
                "run",
                return_value=CompletedProcess([], 0, stdout="different digest\n"),
            ),
            self.assertRaises(StorageUpgradeError),
        ):
            upgrade_storage(self.root, self.backup, apply=True)
        self.assertEqual(generated.state.read_bytes(), original)
        self.assert_backed_up(generated.state, original)
        self.assertTrue(
            (prepared.output_directory / "generation-state.sqlite3").exists()
        )

    def counted_cancellation(self):
        prepared, _plan, generated = legacy_worker_output(self.root / "pregeneration")
        document = json.loads(generated.state.read_bytes())
        queue_id, result = next(iter(document["items"].items()))
        result.update(
            status="failed",
            attempts=2,
            attempts_by_provider={result["provider"]: 2},
            failure={
                **failure_record(
                    RuntimeError("cancelled"),
                    text=next(
                        value["text"]
                        for line in prepared.queue.read_text().splitlines()
                        if (value := json.loads(line)).get("queue_id") == queue_id
                    ),
                    completion=SynthesisCompletion.CANCELLED,
                ),
                "attempt_counted": True,
            },
        )
        result.pop("review_status", None)
        generated.state.write_text(json.dumps(document), encoding="utf-8")
        return prepared, generated.state, queue_id, result["provider"]

    def test_counted_legacy_cancellation_is_normalized_once_during_explicit_upgrade(
        self,
    ):
        prepared, source, queue_id, provider = self.counted_cancellation()
        original = source.read_bytes()
        for _ in range(2):
            upgrade_storage(self.root, self.backup, apply=True)
            normalized = read_state(
                prepared.output_directory / "generation-state.sqlite3"
            )["items"][queue_id]
            self.assertEqual(normalized["attempts"], 1)
            self.assertEqual(normalized["attempts_by_provider"], {provider: 1})
            self.assertIs(normalized["failure"]["attempt_counted"], False)
        self.assert_backed_up(source, original)

    def test_sql_only_counted_cancellation_preserves_audio_and_repeat_sql_bytes(self):
        prepared, legacy, queue_id, provider = self.counted_cancellation()
        database = legacy.with_suffix(".sqlite3")
        replace_generation_state(database, json.loads(legacy.read_bytes()))
        legacy.unlink()
        original = database.read_bytes()
        recordings = {
            path: (path.stat().st_ino, path.read_bytes())
            for path in prepared.output_directory.rglob("*.wav")
        }
        self.assertTrue(recordings)
        upgrade_storage(self.root, self.backup, apply=True)
        self.assert_backed_up(database, original)
        normalized = read_state(database)["items"][queue_id]
        self.assertEqual(normalized["attempts"], 1)
        self.assertEqual(normalized["attempts_by_provider"], {provider: 1})
        self.assertIs(normalized["failure"]["attempt_counted"], False)
        published = database.read_bytes()
        upgrade_storage(self.root, self.backup, apply=True)
        self.assertEqual(database.read_bytes(), published)
        self.assertFalse(legacy.exists())
        for path, (inode, content) in recordings.items():
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(path.read_bytes(), content)

    def test_explicit_workspace_upgrade_preserves_the_bound_json_proof(self):
        prepared, _plan, generated = legacy_worker_output(self.root / "pregeneration")
        workspace = self.parent / "authoring-workspace"
        workspace.mkdir()
        shutil.copyfile(prepared.queue, workspace / "queue.jsonl")
        output = workspace / "generated-audio"
        shutil.copytree(prepared.output_directory, output)
        source = output / generated.state.name
        original, before = source.read_bytes(), source.stat()
        for _ in range(2):
            report = upgrade_storage(
                self.root, self.backup, apply=True, workspace_roots=(workspace,)
            )
            self.assertTrue(report["applied"])
            self.assertEqual(
                read_state(source.with_suffix(".sqlite3")), json.loads(original)
            )
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(
                (source.stat().st_dev, source.stat().st_ino),
                (before.st_dev, before.st_ino),
            )
        self.assertTrue(
            any(
                path.read_bytes() == original for path in self.backup.rglob(source.name)
            )
        )

    def test_late_wav_drift_blocks_source_retirement_and_keeps_verified_backup(self):
        _store, saved, binding = self.legacy_audio()
        original = saved.read_bytes()
        readback = upgrade_module._fresh_readback

        def readback_then_mutate(path, kind, payload):
            readback(path, kind, payload)
            if kind == "preparation":
                saved.write_bytes(b"external source drift")

        with patch.object(
            upgrade_module, "_fresh_readback", side_effect=readback_then_mutate
        ):
            with self.assertRaisesRegex(StorageUpgradeError, "source changed"):
                upgrade_storage(self.root, self.backup, apply=True)
        self.assertTrue(binding.exists())
        self.assert_backed_up(saved, original)

    def test_corrupt_or_future_audio_catalog_is_refused_before_any_publication(self):
        store, _saved, _binding = self.legacy_audio()
        upgrade_storage(self.root, self.backup, apply=True)
        original = store.catalog_path.read_bytes()
        self.backup = self.parent / "refusal-backup"
        for damage in ("corrupt", "future"):
            with self.subTest(damage=damage):
                if damage == "corrupt":
                    store.catalog_path.write_bytes(b"broken SQL authority")
                else:
                    store.catalog_path.write_bytes(original)
                    with closing(sqlite3.connect(store.catalog_path)) as connection:
                        connection.execute("PRAGMA user_version = 999")
                self.assert_refused_without_mutation()
        store.catalog_path.write_bytes(original)

    def test_invalid_late_job_document_prevents_earlier_valid_audio_publication(self):
        store, _saved, binding = self.legacy_audio()
        job = self.root / "pregeneration/jobs" / ("a" * 24) / "job.json"
        job.parent.mkdir(parents=True)
        job.write_text('{"schema_version":999}', encoding="utf-8")
        self.assert_refused_without_mutation()
        self.assertFalse(store.catalog_path.exists())
        self.assertTrue(binding.exists())

    def test_source_sqlite_sidecars_require_recovery_before_any_mutation(self):
        store, _saved, _binding = self.legacy_audio()
        upgrade_storage(self.root, self.backup, apply=True)
        self.backup = self.parent / "sidecar-backup"
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                sidecar = store.catalog_path.with_name(store.catalog_path.name + suffix)
                sidecar.write_bytes(b"pending recovery evidence")
                try:
                    self.assert_refused_without_mutation()
                finally:
                    sidecar.unlink()

    def test_conflicting_json_and_sql_bindings_are_refused_without_rebinding_paid_audio(
        self,
    ):
        directory = self.root / "audio-cache/moss-tts"
        store = SharedAudioStore(directory)
        first, second = self.parent / "first.wav", self.parent / "second.wav"
        write_pcm16_wav(first, [0.1, -0.1], 16000)
        write_pcm16_wav(second, [0.2, -0.2], 16000)
        saved = store.put("paid", first)
        conflicting = store.put("other", second)
        write_versioned_json(
            directory / "bindings/paid.json",
            1,
            {"audio_sha256": hashlib.sha256(conflicting.read_bytes()).hexdigest()},
        )
        self.assert_refused_without_mutation()
        self.assertTrue(store.get("paid").samefile(saved))

    def test_lost_preparation_sql_required_marker_demands_restore(self):
        jobs = self.root / "pregeneration/jobs"
        catalog = PreparationCatalog(jobs, owned_root=True)
        catalog.initialize()
        catalog.path.unlink()
        self.assertTrue(catalog.required_marker.is_file())
        self.assert_refused_without_mutation()
        self.assertFalse(catalog.path.exists())

    def test_linked_audio_or_job_directories_are_refused_without_following_them(self):
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"must remain untouched")
        for relative in ("audio-cache/moss-tts", "pregeneration/jobs"):
            with self.subTest(relative=relative):
                alias = self.root / relative
                alias.parent.mkdir(parents=True, exist_ok=True)
                symlink_or_skip(alias, outside, target_is_directory=True)
                try:
                    self.assert_refused_without_mutation()
                    self.assertEqual(
                        (outside / "sentinel").read_bytes(), b"must remain untouched"
                    )
                finally:
                    alias.unlink()

    def test_dangling_generation_sql_target_is_refused_before_migrating_json(self):
        prepared, _plan, generated = legacy_worker_output(self.root / "pregeneration")
        alias = prepared.output_directory / "generation-state.sqlite3"
        symlink_or_skip(alias, self.parent / "absent.sqlite3")
        self.assert_refused_without_mutation()
        self.assertTrue(generated.state.is_file())

    def test_active_generation_lease_refuses_inspection_and_apply_without_mutation(
        self,
    ):
        prepared, _plan, generated = legacy_worker_output(self.root / "pregeneration")
        with GenerationLease(
            prepared.output_directory,
            prepared.queue_sha256,
            process_checker=process_is_alive,
        ):
            before = self.snapshot(self.root)
            for apply in (False, True):
                with self.subTest(apply=apply), self.assertRaises(StorageUpgradeError):
                    upgrade_storage(self.root, self.backup, apply=apply)
                self.assertEqual(self.snapshot(self.root), before)
                self.assertFalse(self.backup.exists())
        self.assertTrue(generated.state.exists())

    def test_conflicting_existing_backup_is_refused_before_mutating_active_sources(
        self,
    ):
        _store, _saved, binding = self.legacy_audio()
        digest = hashlib.sha256(binding.read_bytes()).hexdigest()
        target = self.backup / "revisions" / digest / binding.relative_to(self.root)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unrelated backup contents")
        self.assert_refused_without_mutation()

    def test_backup_inside_active_jobs_is_refused_without_creating_it(self):
        self.legacy_audio()
        backup = self.root / "pregeneration/jobs/backup"
        self.assert_refused_without_mutation(backup=backup)
        self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()

"""SQLite working state across real generation, migration, review, and player paths."""

import json
import socket
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    load_generated_audio_document,
)
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

import vntts.authoring.bulk_generation as bulk
from tests.bulk_generation_fixtures import SyntheticRenderer, queue_item, write_queue
from tests.symlink_support import symlink_or_skip
from tests.test_pregeneration_queue import PocketRenderer, write_content, write_manifest
from vntts.authoring.generation_failures import generation_failure_report
from vntts.authoring.generation_fallback import authorize_live_fallback
from vntts.authoring.generation_lease import GenerationLease, process_is_alive
from vntts.authoring.generation_review import (
    generation_review_authority,
    review_generation_item,
)
from vntts.authoring.generation_state import (
    load_generation_state,
    write_generation_state,
)
from vntts.authoring.missing_voice_policy import NARRATOR_ROLES, MissingVoicePolicy
from vntts.document_identity import sha256_file
from vntts.generation_database import read_state, state_path
from vntts.pregeneration_generation import (
    OfflineGenerationError,
    OfflineGenerationWorker,
)
from vntts.pregeneration_pack import OfflinePackPublisher
from vntts.pregeneration_queue import PregenerationInputStore
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import VoicePlanStore
from vntts.settings import AppSettings
from vntts.synthesis import SynthesisCompletion
from vntts.voice_library import VoiceLibrary
from vntts.voices import CharacterVoiceRegistry, remember_voice_binding


class _MossRenderer(SyntheticRenderer):
    name = "moss-tts"
    model_name = "moss-tts"

    def shutdown(self):
        self.stop()


def prepared_input(root):
    content = inspect_story_index(write_content(root / "content"))
    jobs = PregenerationJobStore(root / "jobs")
    job = jobs.create_or_resume(content, ("selected",))
    manifest = write_manifest(root / "voices")
    library = VoiceLibrary(root / "library")
    remember_voice_binding(
        library,
        CharacterVoiceRegistry.from_file(manifest),
        "Narrator",
        "character:centurion",
    )
    plan = VoicePlanStore(jobs, voice_library=library).create(
        job, AppSettings(speech_backend="moss-tts"), manifest_path=manifest
    )
    return PregenerationInputStore(jobs).materialize(job, plan), plan, job


def legacy_worker_output(root):
    prepared, plan, _job = prepared_input(root)
    result = bulk.run_bulk_generation(
        prepared.queue,
        prepared.output_directory,
        _MossRenderer(),
        provider="moss-tts",
        model="moss-tts",
        generation_profile=plan.synthesis_profile,
        retries=0,
        control_files={"voice_manifest": prepared.voice_manifest},
        narrator_character="Narrator",
        synthesis_character_overrides={
            role: "Narrator" for role in prepared.narrator_fallback_roles
        },
        missing_voice_policy=MissingVoicePolicy(
            NARRATOR_ROLES, prepared.narrator_fallback_roles
        ),
        approve_validated_audio=True,
    )
    return prepared, plan, result


class SqliteGenerationIntegrationTest(unittest.TestCase):
    def generate(self, queue, output, renderer, **options):
        return bulk.run_bulk_generation(
            queue,
            output,
            renderer,
            provider="synthetic",
            model="synthetic-v1",
            generation_profile="stable",
            retries=0,
            **options,
        )

    def test_sqlite_active_progress_commits_and_restart_reuses_exact_wav(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            value = queue_item()
            queue = write_queue(root / "queue.jsonl", [value])
            output = root / "output"
            snapshots = []

            def observe(state):
                self.assertEqual(read_state(state_path(output)), state)
                snapshots.append(state["active"]["phase"] if state["active"] else None)

            renderer = SyntheticRenderer()
            first = self.generate(
                queue,
                output,
                renderer,
                state_storage="sqlite",
                progress_callback=observe,
            )
            state = load_generation_state(first.state, queue)
            audio = output / state["items"][value["queue_id"]]["path"]
            before = audio.stat()
            digest = sha256_file(audio)
            restarted_renderer = SyntheticRenderer()
            second = self.generate(queue, output, restarted_renderer)
            self.assertEqual(first.state.name, "generation-state.sqlite3")
            self.assertEqual(second.state, first.state)
            self.assertFalse((output / "generation-state.json").exists())
            self.assertEqual(
                (first.generated, second.generated, second.skipped_existing), (1, 0, 1)
            )
            self.assertEqual(restarted_renderer.requests, [])
            self.assertEqual(
                (audio.stat().st_dev, audio.stat().st_ino),
                (before.st_dev, before.st_ino),
            )
            self.assertEqual(sha256_file(audio), digest)
            for phase in ("generating", "validating", "publishing", None):
                self.assertIn(phase, snapshots)

    def test_completed_and_partial_json_migration_preserve_originals_and_only_render_missing(
        self,
    ):
        for partial in (False, True):
            with self.subTest(partial=partial), TemporaryDirectory() as directory:
                root = Path(directory)
                values = [queue_item("one"), queue_item("two")]
                queue = write_queue(root / "queue.jsonl", values)
                output = root / "output"
                original = self.generate(
                    queue, output, SyntheticRenderer(), limit=1 if partial else None
                )
                self.assertEqual(original.state.name, "generation-state.json")
                legacy_payload = original.state.read_bytes()
                legacy_stat = original.state.stat()
                previous = load_generation_state(original.state, queue)
                recordings = {
                    key: (
                        output / item["path"],
                        (output / item["path"]).stat(),
                        sha256_file(output / item["path"]),
                    )
                    for key, item in previous["items"].items()
                }
                renderer = SyntheticRenderer()
                migrated = self.generate(
                    queue, output, renderer, state_storage="sqlite"
                )
                self.assertEqual(migrated.state.name, "generation-state.sqlite3")
                self.assertEqual(len(renderer.requests), 1 if partial else 0)
                self.assertEqual(migrated.skipped_existing, 1 if partial else 2)
                self.assertEqual(original.state.read_bytes(), legacy_payload)
                self.assertEqual(
                    original.state.stat().st_mtime_ns, legacy_stat.st_mtime_ns
                )
                after = load_generation_state(migrated.state, queue)
                self.assertEqual(len(after["items"]), 2)
                for key, (audio, before, digest) in recordings.items():
                    self.assertEqual(after["items"][key], previous["items"][key])
                    self.assertEqual(
                        (audio.stat().st_dev, audio.stat().st_ino),
                        (before.st_dev, before.st_ino),
                    )
                    self.assertEqual(sha256_file(audio), digest)

    def test_sqlite_review_approve_reject_reapprove_preserves_audio_and_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            value = queue_item()
            queue = write_queue(root / "queue.jsonl", [value])
            output = root / "output"
            result = self.generate(
                queue, output, SyntheticRenderer(), state_storage="sqlite"
            )
            record = load_generation_state(result.state, queue)["items"][
                value["queue_id"]
            ]
            audio = output / record["path"]
            before = audio.stat()
            digest = sha256_file(audio)
            for decision in ("approved", "rejected", "approved"):
                authority = generation_review_authority(result.state, value["queue_id"])
                review_generation_item(
                    result.state,
                    value["queue_id"],
                    decision,
                    expected_authority=authority,
                    queue_path=queue,
                )
                state = load_generation_state(result.state, queue)
                self.assertEqual(
                    state["items"][value["queue_id"]]["review_status"], decision
                )
                self.assertEqual(
                    len(GeneratedAudioIndex.load(result.manifest).entries),
                    1 if decision == "approved" else 0,
                )
                self.assertEqual(
                    (audio.stat().st_dev, audio.stat().st_ino),
                    (before.st_dev, before.st_ino),
                )
                self.assertEqual(sha256_file(audio), digest)
            self.assertFalse((output / "generation-state.json").exists())
            self.assertEqual(list(output.glob(".generation-state*")), [])

    def test_sqlite_failure_report_and_live_fallback_use_binary_state_snapshots(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            value = queue_item()
            queue = write_queue(root / "queue.jsonl", [value])
            output = root / "output"
            renderer = PocketRenderer([SynthesisCompletion.LIMITED])
            failed = bulk.run_bulk_generation(
                queue,
                output,
                renderer,
                provider="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                retries=0,
                state_storage="sqlite",
            )
            report = generation_failure_report(failed.state, queue)
            self.assertEqual(report["records"][0]["queue_id"], value["queue_id"])
            decision = authorize_live_fallback(
                failed.state,
                queue,
                value["queue_id"],
                reason="automatic_recovery_exhausted",
                model="pocket-tts",
            )
            stored = load_generation_state(failed.state, queue)["items"][
                value["queue_id"]
            ]
            self.assertEqual(decision["schema_version"], 8)
            self.assertEqual(
                (stored["status"], stored["review_status"]),
                ("live_fallback", "live_fallback"),
            )
            self.assertFalse((output / "generation-state.json").exists())
            self.assertEqual(GeneratedAudioIndex.load(failed.manifest).entries, ())

    def test_completed_legacy_worker_output_upgrades_before_return_without_model_calls(
        self,
    ):
        with TemporaryDirectory() as directory:
            prepared, plan, legacy = legacy_worker_output(Path(directory))
            self.assertEqual(legacy.state.name, "generation-state.json")
            original = legacy.state.read_bytes()
            original_stat = legacy.state.stat()
            state = load_generation_state(legacy.state, prepared.queue)
            recordings = {
                key: (
                    prepared.output_directory / value["path"],
                    (prepared.output_directory / value["path"]).stat(),
                    value["file_sha256"],
                )
                for key, value in state["items"].items()
            }
            sql = prepared.output_directory / "generation-state.sqlite3"
            self.assertEqual(
                OfflineGenerationWorker().inspect_progress(prepared).generated,
                prepared.ready_items,
            )
            self.assertFalse(sql.exists())
            backend = Mock(side_effect=AssertionError("model was started"))
            process = Mock(side_effect=AssertionError("worker process was started"))
            upgraded = OfflineGenerationWorker(
                backend_factory=backend, popen_factory=process
            ).generate(prepared, plan)
            self.assertEqual(upgraded.state, sql)
            self.assertEqual(upgraded.generated, prepared.ready_items)
            self.assertEqual(load_generation_state(sql, prepared.queue), state)
            self.assertEqual(legacy.state.read_bytes(), original)
            self.assertEqual(legacy.state.stat().st_mtime_ns, original_stat.st_mtime_ns)
            backend.assert_not_called()
            process.assert_not_called()
            for audio, before, digest in recordings.values():
                self.assertEqual(
                    (audio.stat().st_dev, audio.stat().st_ino),
                    (before.st_dev, before.st_ino),
                )
                self.assertEqual(sha256_file(audio), digest)

    def test_legacy_worker_upgrade_refuses_corrupt_state_queue_or_recording(self):
        for damage in ("state", "queue", "recording"):
            with self.subTest(damage=damage), TemporaryDirectory() as directory:
                prepared, plan, legacy = legacy_worker_output(Path(directory))
                state = load_generation_state(legacy.state, prepared.queue)
                audio = (
                    prepared.output_directory
                    / next(iter(state["items"].values()))["path"]
                )
                if damage == "state":
                    legacy.state.write_bytes(b"corrupt saved legacy state")
                elif damage == "queue":
                    with prepared.queue.open("ab") as queue:
                        queue.write(b"\n")
                else:
                    audio.write_bytes(b"corrupt saved recording")
                original = legacy.state.read_bytes()
                audio_bytes = audio.read_bytes()
                backend = Mock(side_effect=AssertionError("model was started"))
                process = Mock(side_effect=AssertionError("worker process was started"))
                with self.assertRaises(OfflineGenerationError):
                    OfflineGenerationWorker(
                        backend_factory=backend, popen_factory=process
                    ).generate(prepared, plan)
                self.assertFalse(
                    (prepared.output_directory / "generation-state.sqlite3").exists()
                )
                self.assertEqual(legacy.state.read_bytes(), original)
                self.assertEqual(audio.read_bytes(), audio_bytes)
                backend.assert_not_called()
                process.assert_not_called()

    def test_legacy_worker_upgrade_respects_live_generation_lease(self):
        with TemporaryDirectory() as directory:
            prepared, plan, legacy = legacy_worker_output(Path(directory))
            original = legacy.state.read_bytes()
            backend = Mock(side_effect=AssertionError("model was started"))
            with GenerationLease(
                prepared.output_directory,
                prepared.queue_sha256,
                process_checker=process_is_alive,
            ):
                with self.assertRaises(OfflineGenerationError):
                    OfflineGenerationWorker(backend_factory=backend).generate(
                        prepared, plan
                    )
            self.assertFalse(
                (prepared.output_directory / "generation-state.sqlite3").exists()
            )
            self.assertEqual(legacy.state.read_bytes(), original)
            backend.assert_not_called()

    def test_completed_sqlite_worker_output_keeps_authority_over_corrupt_legacy_json(
        self,
    ):
        with TemporaryDirectory() as directory:
            prepared, plan, legacy = legacy_worker_output(Path(directory))
            state = load_generation_state(legacy.state, prepared.queue)
            sql = prepared.output_directory / "generation-state.sqlite3"
            write_generation_state(sql, state)
            legacy.state.write_bytes(b"obsolete corrupt JSON must not be read")
            original = sql.read_bytes()
            backend = Mock(side_effect=AssertionError("model was started"))
            with patch(
                "vntts.pregeneration_generation.generation_state_snapshot",
                side_effect=AssertionError("legacy JSON was read"),
            ):
                result = OfflineGenerationWorker(backend_factory=backend).generate(
                    prepared, plan
                )
            self.assertEqual(result.state, sql)
            self.assertEqual(sql.read_bytes(), original)
            self.assertEqual(
                legacy.state.read_bytes(), b"obsolete corrupt JSON must not be read"
            )
            backend.assert_not_called()

    def test_offline_worker_uses_sqlite_and_resumes_partial_output_after_restart(self):
        with TemporaryDirectory() as directory:
            prepared, plan, job = prepared_input(Path(directory))
            queue = VoiceGenerationQueue.load(prepared.queue)
            selected = tuple(item.queue_id for item in queue.items[:2])
            backends = []
            observations = []
            worker = None

            def factory(name, registry, cache, **options):
                def inspect(_request):
                    progress = worker.inspect_progress(prepared)
                    observations.append(progress)
                    durable = read_state(state_path(prepared.output_directory))
                    self.assertEqual(durable["active"]["phase"], "generating")

                backend = _MossRenderer(inspect_state=inspect)
                backend.registry = registry
                backends.append(backend)
                return backend

            worker = OfflineGenerationWorker(backend_factory=factory)
            partial = worker.generate(prepared, plan, queue_ids=selected)
            self.assertEqual(partial.generated, 2)
            self.assertEqual(partial.state.name, "generation-state.sqlite3")
            saved = load_generation_state(partial.state, prepared.queue)
            old_files = {
                key: (
                    prepared.output_directory / value["path"],
                    (prepared.output_directory / value["path"]).stat(),
                    value["file_sha256"],
                )
                for key, value in saved["items"].items()
            }
            worker = OfflineGenerationWorker(backend_factory=factory)
            resumed = worker.generate(prepared, plan)
            self.assertEqual(resumed.generated, prepared.ready_items)
            self.assertEqual(
                sum(len(backend.requests) for backend in backends), prepared.ready_items
            )
            self.assertEqual(len(backends[-1].requests), prepared.ready_items - 2)
            self.assertFalse(
                (prepared.output_directory / "generation-state.json").exists()
            )
            cold = OfflineGenerationWorker().inspect_progress(prepared)
            self.assertEqual(cold.generated, prepared.ready_items)
            self.assertTrue(
                any(progress.active_phase == "generating" for progress in observations)
            )
            for audio, before, digest in old_files.values():
                self.assertEqual(
                    (audio.stat().st_dev, audio.stat().st_ino),
                    (before.st_dev, before.st_ino),
                )
                self.assertEqual(sha256_file(audio), digest)
            published = OfflinePackPublisher().publish(job, prepared, resumed)
            self.assertEqual(published.approved, prepared.ready_items)
            packed = load_generated_audio_document(
                published.imported.generated_audio_manifest
            )
            original_by_line = {
                value["line_id"]: prepared.output_directory / value["path"]
                for value in load_generation_state(resumed.state, prepared.queue)[
                    "items"
                ].values()
            }
            for record in packed.records:
                self.assertTrue(record.audio.samefile(original_by_line[record.line_id]))
                self.assertEqual(sha256_file(record.audio), record.audio_sha256)

    def test_sqlite_parent_cancellation_witness_refunds_once_and_resumes_same_seed(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            value = queue_item()
            queue = write_queue(root / "queue.jsonl", [value])
            output = root / "output"
            path = output / "generation-state.sqlite3"
            renderer = SyntheticRenderer(
                inspect_state=lambda request: (_ for _ in ()).throw(
                    SystemExit("child stopped")
                )
            )
            with self.assertRaises(SystemExit):
                self.generate(queue, output, renderer, state_storage="sqlite", seed=5)
            process = Mock(pid=999999, returncode=None)
            process.poll.side_effect = lambda: process.returncode
            state = load_generation_state(path, queue)
            state["active"]["generation_owner_pid"] = process.pid
            write_generation_state(path, state, changed_items=())
            lease = {
                "schema": bulk.LEASE_SCHEMA,
                "schema_version": bulk.LEASE_VERSION,
                "queue_sha256": sha256_file(queue),
                "pid": process.pid,
                "hostname": socket.gethostname(),
                "process_started_at": "owned-child-start",
                "lease_id": uuid4().hex,
                "started_at": "2026-10-23T00:00:00+00:00",
            }
            (output / ".generation-lease.json").write_text(json.dumps(lease))
            with patch.object(
                bulk, "process_started_at", return_value="owned-child-start"
            ):
                witness = bulk.capture_generation_cancellation(
                    output, queue, sha256_file(queue), process
                )
            self.assertIsNotNone(witness)
            process.returncode = -15
            self.assertTrue(bulk.reconcile_generation_cancellation(witness, process))
            cancelled = load_generation_state(path, queue)
            record = cancelled["items"][value["queue_id"]]
            self.assertEqual(record["attempts"], 0)
            self.assertFalse(record["failure"]["attempt_counted"])
            self.assertIsNone(cancelled["active"])
            self.assertFalse(bulk.reconcile_generation_cancellation(witness, process))
            resumed_renderer = SyntheticRenderer()
            resumed = self.generate(queue, output, resumed_renderer, seed=5)
            self.assertEqual(resumed.generated, 1)
            self.assertEqual(
                [request.seed for request in resumed_renderer.requests], [5]
            )
            self.assertFalse((output / "generation-state.json").exists())

    def test_invalid_legacy_json_or_sqlite_authority_never_rerenders_or_replaces_saved_audio(
        self,
    ):
        for damage in ("legacy", "database"):
            with self.subTest(damage=damage), TemporaryDirectory() as directory:
                root = Path(directory)
                queue = write_queue(root / "queue.jsonl", [queue_item()])
                output = root / "output"
                saved = self.generate(queue, output, SyntheticRenderer())
                value = load_generation_state(saved.state, queue)
                recording = output / next(iter(value["items"].values()))["path"]
                before = recording.stat()
                digest = sha256_file(recording)
                if damage == "legacy":
                    value["schema_version"] = 999
                    saved.state.write_text(json.dumps(value))
                    authority = saved.state
                else:
                    authority = output / "generation-state.sqlite3"
                    authority.write_bytes(b"saved damaged database")
                original = authority.read_bytes()
                renderer = SyntheticRenderer()
                with self.assertRaises(bulk.BulkGenerationError):
                    self.generate(queue, output, renderer, state_storage="sqlite")
                self.assertEqual(renderer.requests, [])
                self.assertEqual(authority.read_bytes(), original)
                self.assertEqual(
                    (recording.stat().st_dev, recording.stat().st_ino),
                    (before.st_dev, before.st_ino),
                )
                self.assertEqual(sha256_file(recording), digest)
                if damage == "legacy":
                    self.assertFalse((output / "generation-state.sqlite3").exists())

    def test_relocated_controls_keep_original_evidence_but_nonpath_tampering_is_rejected(
        self,
    ):
        for field in ("role", "kind", "sha256", "files"):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                root = Path(directory)
                queue = write_queue(root / "queue.jsonl", [queue_item()])
                source = root / "original-controls"
                source.mkdir()
                (source / "config.txt").write_text("same model configuration")
                relocated = root / "relocated-controls"
                relocated.mkdir()
                (relocated / "config.txt").write_bytes(
                    (source / "config.txt").read_bytes()
                )
                result = self.generate(
                    queue,
                    root / "output",
                    SyntheticRenderer(),
                    state_storage="sqlite",
                    control_files={"model_config": source},
                )
                original = load_generation_state(result.state, queue)
                audio = (
                    result.state.parent / next(iter(original["items"].values()))["path"]
                )
                identity = audio.stat()
                digest = sha256_file(audio)
                renderer = SyntheticRenderer()
                resumed = self.generate(
                    queue,
                    result.state.parent,
                    renderer,
                    control_files={"model_config": relocated},
                )
                self.assertEqual((resumed.generated, resumed.skipped_existing), (0, 1))
                self.assertEqual(renderer.requests, [])
                after = load_generation_state(result.state, queue)
                self.assertEqual(
                    after["synthesis_controls"], original["synthesis_controls"]
                )
                damaged = deepcopy(after)
                records = next(iter(damaged["synthesis_controls"].values()))
                control = records[0]
                if field == "role":
                    control[field] = "different_role"
                elif field == "kind":
                    control[field] = "file"
                    control.pop("files")
                elif field == "sha256":
                    control[field] = "e" * 64
                else:
                    control[field][0]["sha256"] = "e" * 64
                write_generation_state(result.state, damaged)
                saved = result.state.read_bytes()
                blocked = SyntheticRenderer()
                with self.assertRaises(bulk.BulkGenerationError):
                    self.generate(
                        queue,
                        result.state.parent,
                        blocked,
                        control_files={"model_config": relocated},
                    )
                self.assertEqual(blocked.requests, [])
                self.assertEqual(result.state.read_bytes(), saved)
                self.assertEqual(
                    (audio.stat().st_dev, audio.stat().st_ino),
                    (identity.st_dev, identity.st_ino),
                )
                self.assertEqual(sha256_file(audio), digest)

    def test_public_loader_rejects_sqlite_leaf_symlinks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(root / "queue.jsonl", [queue_item()])
            result = self.generate(
                queue, root / "output", SyntheticRenderer(), state_storage="sqlite"
            )
            alias = result.state.with_name("alias.sqlite3")
            symlink_or_skip(alias, result.state)
            with self.assertRaises((bulk.BulkGenerationError, ValueError)):
                load_generation_state(alias, queue)

    def test_dangling_sqlite_authority_does_not_fall_back_to_new_json(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(root / "queue.jsonl", [queue_item()])
            output = root / "output"
            output.mkdir()
            symlink_or_skip(
                output / "generation-state.sqlite3", root / "missing.sqlite3"
            )
            renderer = SyntheticRenderer()
            with self.assertRaises((bulk.BulkGenerationError, ValueError)):
                self.generate(queue, output, renderer)
            self.assertEqual(renderer.requests, [])
            self.assertFalse((output / "generation-state.json").exists())


if __name__ == "__main__":
    unittest.main()

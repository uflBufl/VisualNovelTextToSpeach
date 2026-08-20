import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import (
    PCM16_MONO_WAV_FORMAT,
    probe_pcm16_mono_wav,
    write_pcm16_wav,
)
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import write_generated_audio_manifest
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    expected_voice_generation_queue_id,
    write_voice_generation_queue,
)

from vntts.authoring.cli import main
from vntts.authoring.legacy_import import (
    IMPORT_SCHEMA,
    LegacyAuthoringImportError,
    discover_legacy_jobs,
    import_legacy_job,
    import_standalone_generation,
    inspect_standalone_generation,
)


def write_legacy_fixture(
    root,
    *,
    job_name="original-job",
    title="Patch 3.7",
    text="Preserve this generated line exactly.",
):
    root.mkdir(parents=True, exist_ok=True)
    story = root / "story-index.jsonl"
    story.write_text("synthetic story provenance\n", encoding="utf-8")
    voices = root / "voice-manifest.json"
    voices.write_text('{"version": 2, "voices": []}\n', encoding="utf-8")
    text_hash = text_sha256(text)
    line_id = "reverse1999:315401:7"
    queue_id = expected_voice_generation_queue_id(line_id, text_hash)
    queue = root / "shared" / "queue.jsonl"
    write_voice_generation_queue(
        queue,
        {
            "game": "Reverse: 1999",
            "language": "en",
            "generated_at": "2026-08-16T17:00:00+00:00",
        },
        [
            {
                "record_type": "generation_item",
                "queue_id": queue_id,
                "line_id": line_id,
                "text_sha256": text_hash,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": text,
                "action": "generate",
                "state": "pending",
                "emotion": "warm",
                "provider_extension": {"keep": True},
            }
        ],
    )
    queue_hash = sha256_file(queue)
    output = root / "shared" / "generated-audio"
    wav = output / "audio" / "rhiannon" / "line.wav"
    write_pcm16_wav(
        wav,
        np.sin(np.linspace(0, 4 * np.pi, 4_000, dtype=np.float32)) * 0.1,
        16_000,
    )
    info = probe_pcm16_mono_wav(wav)
    quality = {
        "duration_seconds": round(info.duration_seconds, 4),
        "sample_rate": info.sample_rate,
        "channels": 1,
        "sample_count": info.sample_count,
        "peak": round(info.peak, 6),
    }
    state_path = output / "generation-state.json"
    state = {
        "schema": "r1999.bulk-generation-state",
        "schema_version": 1,
        "queue_sha256": queue_hash,
        "game": "Reverse: 1999",
        "language": "en",
        "active": {
            "queue_id": queue_id,
            "line_id": line_id,
            "phase": "retrying",
            "attempt": 1,
            "attempt_limit": 3,
            "total_attempts": 4,
            "seed": 12,
            "started_at": "2026-08-16T17:00:00+00:00",
            "updated_at": "2026-08-16T17:00:01+00:00",
            "last_error": "interrupted diagnostic",
        },
        "items": {
            queue_id: {
                "status": "approved",
                "review_status": "approved",
                "attempts": 3,
                "path": "audio/rhiannon/line.wav",
                "line_id": line_id,
                "text_sha256": text_hash,
                "file_sha256": sha256_file(wav),
                "provider": "moss-tts",
                "model": "moss-v1.5",
                "prompt_sha256": "a" * 64,
                "seed": 11,
                "quality": quality,
                "updated_at": "2026-08-16T17:05:00+00:00",
            }
        },
    }
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    manifest = output / "manifest.json"
    write_generated_audio_manifest(
        manifest,
        {
            "game": "Reverse: 1999",
            "language": "en",
            "source_queue_sha256": queue_hash,
            "generated_at": "2026-08-16T17:06:00+00:00",
        },
        [
            {
                "queue_id": queue_id,
                "line_id": line_id,
                "text_sha256": text_hash,
                "audio": "audio/rhiannon/line.wav",
                "audio_format": PCM16_MONO_WAV_FORMAT,
                "audio_sha256": sha256_file(wav),
                "sample_rate": info.sample_rate,
                "sample_count": info.sample_count,
                "provider": "moss-tts",
                "model": "moss-v1.5",
                "prompt_sha256": "a" * 64,
                "seed": 11,
                "review_status": "approved",
            }
        ],
    )
    jobs = root / "jobs"
    job_directory = jobs / job_name
    job_directory.mkdir(parents=True)
    job = {
        "schema": "r1999.pregeneration-job",
        "schema_version": 1,
        "created_at": "2026-08-16T16:00:00+00:00",
        "updated_at": "2026-08-16T17:00:00+00:00",
        "status": "complete",
        "title": title,
        "targets": [
            {
                "target_id": "hero-story:rhiannon",
                "category": "Character stories",
                "title": "The Eaglet Takes Wing",
                "chapters": ["315401"],
                "episode_count": 1,
                "line_count": 1,
            }
        ],
        "story_index": str(story),
        "queue": str(queue),
        "output": str(output),
        "voice_manifest": str(voices),
        "vntts_python": "/legacy/vntts/python",
        "model": "moss-v1.5",
        "narrator_character": "Matilda",
    }
    (job_directory / "job.json").write_text(
        json.dumps(job, sort_keys=True), encoding="utf-8"
    )
    return {
        "job_directory": job_directory,
        "jobs": jobs,
        "job": job,
        "queue": queue,
        "queue_id": queue_id,
        "line_id": line_id,
        "text_hash": text_hash,
        "state": state_path,
        "manifest": manifest,
        "wav": wav,
    }


class LegacyAuthoringImportTest(unittest.TestCase):
    def test_preserves_validated_source_job_timestamps(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)

            result = import_legacy_job(fixture["job_directory"], root / "app-data")

        self.assertEqual(
            result.manifest["legacy_job"]["created_at"],
            "2026-08-16T16:00:00+00:00",
        )
        self.assertEqual(
            result.manifest["legacy_job"]["updated_at"],
            "2026-08-16T17:00:00+00:00",
        )

    def test_optional_source_job_update_timestamp_is_strict_and_backward_compatible(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root / "missing")
            job_path = fixture["job_directory"] / "job.json"
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job.pop("updated_at")
            job_path.write_text(json.dumps(job, sort_keys=True), encoding="utf-8")

            result = import_legacy_job(
                fixture["job_directory"], root / "missing-app-data"
            )

            invalid = write_legacy_fixture(root / "invalid")
            invalid_path = invalid["job_directory"] / "job.json"
            invalid_job = json.loads(invalid_path.read_text(encoding="utf-8"))
            invalid_job["updated_at"] = "2026-08-16T17:00:00"
            invalid_path.write_text(
                json.dumps(invalid_job, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                LegacyAuthoringImportError, "updated_at must include a timezone"
            ):
                import_legacy_job(invalid["job_directory"], root / "invalid-app-data")

            reversed_fixture = write_legacy_fixture(root / "reversed")
            reversed_path = reversed_fixture["job_directory"] / "job.json"
            reversed_job = json.loads(reversed_path.read_text(encoding="utf-8"))
            reversed_job["updated_at"] = "2026-08-16T15:59:59+00:00"
            reversed_path.write_text(
                json.dumps(reversed_job, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                LegacyAuthoringImportError, "must not precede created_at"
            ):
                import_legacy_job(
                    reversed_fixture["job_directory"], root / "reversed-app-data"
                )

        self.assertNotIn("updated_at", result.manifest["legacy_job"])

    def test_version_one_import_without_job_timestamps_remains_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            first = import_legacy_job(fixture["job_directory"], root / "app-data")
            manifest_path = first.destination / "import.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 1
            manifest["legacy_job"].pop("created_at")
            manifest["legacy_job"].pop("updated_at")
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )

            second = import_legacy_job(fixture["job_directory"], root / "app-data")

        self.assertFalse(second.created)
        self.assertEqual(second.manifest["schema_version"], 1)
        self.assertNotIn("created_at", second.manifest["legacy_job"])

    def test_reimport_rejects_forged_manifest_inventory_and_identity(self):
        for mutation in ("artifacts", "identity", "summary"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = write_legacy_fixture(root)
                first = import_legacy_job(fixture["job_directory"], root / "app-data")
                manifest_path = first.destination / "import.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if mutation == "artifacts":
                    manifest["artifacts"] = []
                elif mutation == "identity":
                    manifest["identities"][0]["review_status"] = "rejected"
                else:
                    manifest["summary"]["generated_items"] = 0
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True), encoding="utf-8"
                )

                with self.assertRaisesRegex(
                    LegacyAuthoringImportError, "manifest was modified"
                ):
                    import_legacy_job(fixture["job_directory"], root / "app-data")

    def test_active_state_change_after_import_is_not_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            first = import_legacy_job(fixture["job_directory"], root / "app-data")
            imported_state = first.destination / "generated-audio/generation-state.json"
            imported_hash = sha256_file(imported_state)
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            state["active"]["last_error"] = "new crash diagnostic"
            fixture["state"].write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaisesRegex(LegacyAuthoringImportError, "source changed"):
                import_legacy_job(fixture["job_directory"], root / "app-data")

            self.assertEqual(sha256_file(imported_state), imported_hash)

    def test_queue_record_extensions_participate_in_cross_import_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first_fixture = write_legacy_fixture(root / "first")
            second_fixture = write_legacy_fixture(root / "second")
            rows = [
                json.loads(line)
                for line in second_fixture["queue"]
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            rows[1]["voice_character"] = "Different Voice"
            second_fixture["queue"].write_text(
                "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
                encoding="utf-8",
            )
            queue_hash = sha256_file(second_fixture["queue"])
            state = json.loads(second_fixture["state"].read_text(encoding="utf-8"))
            state["queue_sha256"] = queue_hash
            second_fixture["state"].write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )
            manifest = json.loads(
                second_fixture["manifest"].read_text(encoding="utf-8")
            )
            manifest["source_queue_sha256"] = queue_hash
            second_fixture["manifest"].write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )
            import_legacy_job(first_fixture["job_directory"], root / "app-data")

            with self.assertRaisesRegex(LegacyAuthoringImportError, "conflicts"):
                import_legacy_job(second_fixture["job_directory"], root / "app-data")

    def test_rejects_invalid_status_review_combinations(self):
        for status, review in (("failed", "approved"), ("generated", "approved")):
            with (
                self.subTest(status=status, review=review),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                fixture = write_legacy_fixture(root)
                state = json.loads(fixture["state"].read_text(encoding="utf-8"))
                item = state["items"][fixture["queue_id"]]
                item["status"] = status
                item["review_status"] = review
                fixture["state"].write_text(
                    json.dumps(state, sort_keys=True), encoding="utf-8"
                )

                with self.assertRaisesRegex(
                    LegacyAuthoringImportError, "status and review combination"
                ):
                    import_legacy_job(fixture["job_directory"], root / "app-data")

    def test_active_job_and_source_mutation_during_copy_are_deferred(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            job_path = fixture["job_directory"] / "job.json"
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job["status"] = "running"
            job["pid"] = os.getpid()
            job_path.write_text(json.dumps(job, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(LegacyAuthoringImportError, "active"):
                import_legacy_job(fixture["job_directory"], root / "app-data")

            job["status"] = "complete"
            job["pid"] = None
            job_path.write_text(json.dumps(job, sort_keys=True), encoding="utf-8")
            original_copy = __import__("shutil").copy2
            mutated = False

            def mutate_after_queue_copy(source, destination):
                nonlocal mutated
                result = original_copy(source, destination)
                if Path(source).resolve() == fixture["queue"].resolve() and not mutated:
                    state = json.loads(fixture["state"].read_text(encoding="utf-8"))
                    state["active"]["updated_at"] = "2026-08-16T17:00:02+00:00"
                    fixture["state"].write_text(
                        json.dumps(state, sort_keys=True), encoding="utf-8"
                    )
                    mutated = True
                return result

            with (
                patch(
                    "vntts.authoring.legacy_import.shutil.copy2",
                    side_effect=mutate_after_queue_copy,
                ),
                self.assertRaisesRegex(LegacyAuthoringImportError, "retry when idle"),
            ):
                import_legacy_job(fixture["job_directory"], root / "app-data")

            self.assertFalse((root / "app-data" / "legacy-").exists())
            self.assertEqual(list((root / "app-data").iterdir()), [])

    def test_running_job_with_proven_dead_pid_imports_as_interrupted_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            job_path = fixture["job_directory"] / "job.json"
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job["status"] = "running"
            job["pid"] = 2_147_483_647
            job_path.write_text(json.dumps(job, sort_keys=True), encoding="utf-8")

            with patch(
                "vntts.authoring.legacy_import.os.kill",
                side_effect=ProcessLookupError,
            ):
                candidate = next(
                    item
                    for item in discover_legacy_jobs(fixture["jobs"])
                    if item.kind == "pregeneration-job"
                )
                result = import_legacy_job(fixture["job_directory"], root / "app-data")

        self.assertTrue(candidate.compatible)
        self.assertEqual(candidate.status, "interrupted")
        self.assertTrue(candidate.diagnostics)
        self.assertTrue(result.created)
        self.assertEqual(result.manifest["legacy_job"]["status"], "running")
        self.assertEqual(
            result.manifest["legacy_job"]["snapshot_status"], "interrupted"
        )
        self.assertTrue(result.manifest["source"]["diagnostics"])

    def test_running_job_with_unknown_pid_fails_closed(self):
        for pid, kill_error in (
            (None, None),
            ("bad", None),
            (2_147_483_647, PermissionError()),
        ):
            with self.subTest(pid=pid), TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = write_legacy_fixture(root)
                job_path = fixture["job_directory"] / "job.json"
                job = json.loads(job_path.read_text(encoding="utf-8"))
                job["status"] = "running"
                job["pid"] = pid
                job_path.write_text(json.dumps(job, sort_keys=True), encoding="utf-8")
                context = (
                    patch(
                        "vntts.authoring.legacy_import.os.kill",
                        side_effect=kill_error,
                    )
                    if kill_error is not None
                    else patch("vntts.authoring.legacy_import.os.kill")
                )

                with (
                    context,
                    self.assertRaisesRegex(
                        LegacyAuthoringImportError,
                        "missing, invalid, or cannot be inspected",
                    ),
                ):
                    import_legacy_job(fixture["job_directory"], root / "app-data")

                self.assertFalse((root / "app-data").exists())

    def test_generated_wav_mutation_during_copy_aborts_before_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            original_copy = __import__("shutil").copy2
            mutated = False

            def mutate_after_wav_copy(source, destination):
                nonlocal mutated
                result = original_copy(source, destination)
                if Path(source).resolve() == fixture["wav"].resolve() and not mutated:
                    fixture["wav"].write_bytes(b"changed after copy")
                    mutated = True
                return result

            with (
                patch(
                    "vntts.authoring.legacy_import.shutil.copy2",
                    side_effect=mutate_after_wav_copy,
                ),
                self.assertRaisesRegex(LegacyAuthoringImportError, "retry when idle"),
            ):
                import_legacy_job(fixture["job_directory"], root / "app-data")

            self.assertEqual(list((root / "app-data").iterdir()), [])

    def test_job_semantics_are_bound_to_the_exact_snapshotted_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            job_path = fixture["job_directory"] / "job.json"

            def mutate_after_validation(job):
                mutated = dict(job)
                mutated["title"] = "Mutated after semantic read"
                job_path.write_text(
                    json.dumps(mutated, sort_keys=True), encoding="utf-8"
                )

            with (
                patch(
                    "vntts.authoring.legacy_import._validate_job",
                    side_effect=mutate_after_validation,
                ),
                self.assertRaisesRegex(LegacyAuthoringImportError, "changed"),
            ):
                import_legacy_job(fixture["job_directory"], root / "app-data")

            self.assertFalse((root / "app-data").exists())

    def test_state_validated_wav_hash_is_bound_without_a_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            fixture["manifest"].unlink()
            original_validate = __import__(
                "vntts.authoring.legacy_import", fromlist=["_validate_state"]
            )._validate_state

            def mutate_after_state_validation(*args, **kwargs):
                result = original_validate(*args, **kwargs)
                with fixture["wav"].open("ab") as stream:
                    stream.write(b"changed after validation")
                return result

            with (
                patch(
                    "vntts.authoring.legacy_import._validate_state",
                    side_effect=mutate_after_state_validation,
                ),
                self.assertRaisesRegex(LegacyAuthoringImportError, "changed"),
            ):
                import_legacy_job(fixture["job_directory"], root / "app-data")

            self.assertFalse((root / "app-data").exists())

    def test_standalone_import_requires_and_preserves_exact_queue_output_pair(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            source_hashes = {
                name: sha256_file(fixture[name])
                for name in ("queue", "state", "manifest", "wav")
            }

            plan = inspect_standalone_generation(
                fixture["queue"], fixture["state"].parent
            )
            first = import_standalone_generation(
                fixture["queue"], fixture["state"].parent, root / "app-data"
            )
            second = import_standalone_generation(
                fixture["queue"], fixture["state"].parent, root / "app-data"
            )

            self.assertEqual(plan.summary["generated_items"], 1)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.destination, second.destination)
            self.assertFalse((first.destination / "legacy/job.json").exists())
            self.assertEqual(
                source_hashes,
                {
                    name: sha256_file(fixture[name])
                    for name in ("queue", "state", "manifest", "wav")
                },
            )

    def test_standalone_pairing_rejects_different_full_queue_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            other = write_legacy_fixture(root / "other", title="same-prefix-name")
            queue_text = other["queue"].read_text(encoding="utf-8")
            other["queue"].write_text(
                queue_text.replace(
                    "2026-08-16T17:00:00+00:00", "2026-08-16T17:00:01+00:00"
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(LegacyAuthoringImportError, "full SHA-256"):
                inspect_standalone_generation(other["queue"], fixture["state"].parent)

    def test_manifest_only_standalone_output_is_preserved_as_unconfirmed_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            fixture["state"].unlink()

            result = import_standalone_generation(
                fixture["queue"], fixture["manifest"].parent, root / "app-data"
            )

            self.assertEqual(
                result.manifest["summary"]["generated_manifest_state"], "stale"
            )
            self.assertTrue(
                (
                    result.destination / "legacy/stale-generated-audio-manifest.json"
                ).is_file()
            )

    def test_import_preserves_identity_attempts_review_and_valid_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            source_hashes = {
                name: sha256_file(fixture[name])
                for name in ("queue", "state", "manifest", "wav")
            }

            result = import_legacy_job(fixture["job_directory"], root / "app-data")
            imported_queue = VoiceGenerationQueue.load(
                result.destination / "queue.jsonl"
            )
            imported_state = json.loads(
                (
                    result.destination / "generated-audio/generation-state.json"
                ).read_text(encoding="utf-8")
            )
            imported_wav = (
                result.destination / "generated-audio/audio/rhiannon/line.wav"
            )
            identity = result.manifest["identities"][0]

            self.assertTrue(result.created)
            self.assertEqual(result.manifest["schema"], IMPORT_SCHEMA)
            self.assertEqual(imported_queue.items[0].queue_id, fixture["queue_id"])
            self.assertEqual(imported_queue.items[0].line_id, fixture["line_id"])
            self.assertEqual(imported_queue.items[0].text_sha256, fixture["text_hash"])
            self.assertTrue(
                imported_queue.items[0].document["provider_extension"]["keep"]
            )
            self.assertEqual(identity["attempts"], 3)
            self.assertEqual(identity["seed"], 11)
            self.assertEqual(identity["status"], "approved")
            self.assertEqual(identity["review_status"], "approved")
            self.assertEqual(imported_state["active"]["phase"], "retrying")
            self.assertEqual(sha256_file(imported_wav), source_hashes["wav"])
            self.assertEqual(
                source_hashes,
                {
                    name: sha256_file(fixture[name])
                    for name in ("queue", "state", "manifest", "wav")
                },
            )

    def test_duplicate_registered_job_is_idempotent_by_queue_and_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            duplicate = fixture["jobs"] / "registered-copy"
            duplicate.mkdir()
            duplicate_job = dict(fixture["job"])
            duplicate_job.update(
                title="Registered existing Patch 3.7",
                registered_existing_job=True,
            )
            (duplicate / "job.json").write_text(
                json.dumps(duplicate_job, sort_keys=True), encoding="utf-8"
            )

            first = import_legacy_job(fixture["job_directory"], root / "app-data")
            second = import_legacy_job(duplicate, root / "app-data")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.destination, second.destination)
        self.assertEqual(first.manifest["imported_at"], second.manifest["imported_at"])

    def test_registered_job_accepts_historical_missing_model_and_empty_targets(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            job_path = fixture["job_directory"] / "job.json"
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job.pop("model")
            job["targets"] = []
            job["registered_existing_job"] = True
            job_path.write_text(json.dumps(job, sort_keys=True), encoding="utf-8")

            result = import_legacy_job(fixture["job_directory"], root / "app-data")

        self.assertTrue(result.created)
        self.assertIsNone(result.manifest["legacy_job"]["model"])

    def test_older_target_accepts_missing_or_null_episode_count(self):
        for episode_count in (None, "missing"):
            with self.subTest(episode_count=episode_count):
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    fixture = write_legacy_fixture(root)
                    job_path = fixture["job_directory"] / "job.json"
                    job = json.loads(job_path.read_text(encoding="utf-8"))
                    if episode_count == "missing":
                        job["targets"][0].pop("episode_count")
                    else:
                        job["targets"][0]["episode_count"] = episode_count
                    job_path.write_text(
                        json.dumps(job, sort_keys=True), encoding="utf-8"
                    )

                    result = import_legacy_job(
                        fixture["job_directory"], root / "app-data"
                    )

                self.assertTrue(result.created)

    def test_changed_source_is_a_hard_collision_and_does_not_overwrite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            first = import_legacy_job(fixture["job_directory"], root / "app-data")
            imported_state = first.destination / "generated-audio/generation-state.json"
            imported_hash = sha256_file(imported_state)
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            state["items"][fixture["queue_id"]]["seed"] = 12
            fixture["state"].write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )
            manifest = json.loads(fixture["manifest"].read_text(encoding="utf-8"))
            manifest["entries"][0]["seed"] = 12
            fixture["manifest"].write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaisesRegex(LegacyAuthoringImportError, "source changed"):
                import_legacy_job(fixture["job_directory"], root / "app-data")

            self.assertEqual(sha256_file(imported_state), imported_hash)

    def test_same_queue_record_can_coexist_in_distinct_generation_histories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first_fixture = write_legacy_fixture(root / "first")
            second_fixture = write_legacy_fixture(root / "second")
            state = json.loads(second_fixture["state"].read_text(encoding="utf-8"))
            state["items"][second_fixture["queue_id"]]["provider"] = "other-provider"
            second_fixture["state"].write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )
            manifest = json.loads(
                second_fixture["manifest"].read_text(encoding="utf-8")
            )
            manifest["entries"][0]["provider"] = "other-provider"
            second_fixture["manifest"].write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )
            first = import_legacy_job(first_fixture["job_directory"], root / "app-data")
            second = import_legacy_job(
                second_fixture["job_directory"], root / "app-data"
            )

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertNotEqual(first.destination, second.destination)
        self.assertEqual(
            first.manifest["identities"][0]["queue_item_sha256"],
            second.manifest["identities"][0]["queue_item_sha256"],
        )
        self.assertNotEqual(
            first.manifest["identities"][0]["provider"],
            second.manifest["identities"][0]["provider"],
        )

    def test_tampered_source_wav_is_rejected_before_writing_app_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            fixture["wav"].write_bytes(b"tampered")

            with self.assertRaisesRegex(
                LegacyAuthoringImportError, "checksum mismatch"
            ):
                import_legacy_job(fixture["job_directory"], root / "app-data")

            self.assertFalse((root / "app-data").exists())

    def test_state_bound_to_different_queue_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            state["queue_sha256"] = "0" * 64
            fixture["state"].write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                LegacyAuthoringImportError, "belongs to different queue"
            ):
                import_legacy_job(fixture["job_directory"], root / "app-data")

    def test_state_audio_path_cannot_escape_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            state["items"][fixture["queue_id"]]["path"] = "../outside.wav"
            fixture["state"].write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaisesRegex(LegacyAuthoringImportError, "must stay within"):
                import_legacy_job(fixture["job_directory"], root / "app-data")

    def test_modified_import_is_never_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            first = import_legacy_job(fixture["job_directory"], root / "app-data")
            imported_queue = first.destination / "queue.jsonl"
            imported_queue.write_text("modified", encoding="utf-8")

            with self.assertRaisesRegex(
                LegacyAuthoringImportError, "missing or modified"
            ):
                import_legacy_job(fixture["job_directory"], root / "app-data")

            self.assertEqual(imported_queue.read_text(encoding="utf-8"), "modified")

    def test_approved_state_imports_when_derived_manifest_is_missing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            fixture["manifest"].unlink()

            result = import_legacy_job(fixture["job_directory"], root / "app-data")
            imported_manifest_exists = (
                result.destination / "generated-audio/manifest.json"
            ).exists()

        self.assertEqual(result.manifest["summary"]["generated_items"], 1)
        self.assertEqual(result.manifest["summary"]["review_counts"], {"approved": 1})
        self.assertFalse(imported_manifest_exists)

    def test_stale_manifest_is_quarantined_without_losing_newer_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            state_item = state["items"][fixture["queue_id"]]
            state_item["status"] = "generated"
            state_item["review_status"] = "rejected"
            state_item["seed"] = 12
            fixture["state"].write_text(
                json.dumps(state, sort_keys=True), encoding="utf-8"
            )

            candidate = next(
                value
                for value in discover_legacy_jobs(fixture["jobs"])
                if value.kind == "pregeneration-job"
            )
            result = import_legacy_job(fixture["job_directory"], root / "app-data")
            stale_manifest_exists = (
                result.destination / "legacy/stale-generated-audio-manifest.json"
            ).is_file()
            current_manifest_exists = (
                result.destination / "generated-audio/manifest.json"
            ).exists()
            imported_wav_exists = (
                result.destination / "generated-audio/audio/rhiannon/line.wav"
            ).is_file()

        self.assertEqual(result.manifest["identities"][0]["review_status"], "rejected")
        self.assertEqual(result.manifest["identities"][0]["seed"], 12)
        self.assertEqual(
            result.manifest["summary"]["generated_manifest_state"], "stale"
        )
        self.assertTrue(result.manifest["summary"]["generated_manifest_diagnostics"])
        self.assertTrue(candidate.compatible)
        self.assertTrue(candidate.diagnostics)
        self.assertTrue(stale_manifest_exists)
        self.assertFalse(current_manifest_exists)
        self.assertTrue(imported_wav_exists)

    def test_discovery_keeps_actionable_incompatibility_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            broken = fixture["jobs"] / "future-job"
            broken.mkdir()
            future_job = dict(fixture["job"])
            future_job["schema_version"] = 99
            (broken / "job.json").write_text(json.dumps(future_job), encoding="utf-8")

            candidates = discover_legacy_jobs(fixture["jobs"])

        self.assertEqual(len(candidates), 2)
        self.assertEqual(sum(candidate.compatible for candidate in candidates), 1)
        incompatible = next(
            candidate for candidate in candidates if not candidate.compatible
        )
        self.assertIn(
            "expected 'r1999.pregeneration-job' version 1",
            incompatible.compatibility_error,
        )

    def test_discovery_reports_unsupported_standalone_and_listening_work(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            standalone_queue = root / "standalone-generation-queue.jsonl"
            standalone_queue.write_bytes(fixture["queue"].read_bytes())
            standalone_output = root / "standalone-output"
            standalone_output.mkdir()
            standalone_state = json.loads(fixture["state"].read_text(encoding="utf-8"))
            (standalone_output / "generation-state.json").write_text(
                json.dumps(standalone_state), encoding="utf-8"
            )
            listening = root / "model-benchmark" / "listening-session"
            listening.mkdir(parents=True)
            (listening / "session.json").write_text(
                json.dumps(
                    {
                        "schema": "r1999.model-listening-session",
                        "schema_version": 1,
                    }
                ),
                encoding="utf-8",
            )
            (listening / ".blind-key.json").write_text("{}", encoding="utf-8")
            (listening / "report.json").write_text("{}", encoding="utf-8")

            candidates = discover_legacy_jobs(fixture["jobs"])

        kinds = {candidate.kind for candidate in candidates}
        self.assertIn("pregeneration-job", kinds)
        self.assertIn("standalone-generation-queue", kinds)
        self.assertIn("standalone-generation-output", kinds)
        self.assertIn("model-listening-session", kinds)
        for candidate in candidates:
            if candidate.kind.startswith("standalone-"):
                self.assertFalse(candidate.compatible)
                self.assertIn("explicit", candidate.compatibility_error)
            if candidate.kind == "model-listening-session":
                self.assertFalse(candidate.compatible)
                self.assertIn("Unsupported", candidate.compatibility_error)

    def test_cli_import_reports_idempotent_destination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root)
            first_output = io.StringIO()
            second_output = io.StringIO()

            with redirect_stdout(first_output):
                self.assertEqual(
                    main(
                        [
                            "import-legacy",
                            str(fixture["job_directory"]),
                            "--destination-root",
                            str(root / "app-data"),
                        ]
                    ),
                    0,
                )
            with redirect_stdout(second_output):
                self.assertEqual(
                    main(
                        [
                            "import-legacy",
                            str(fixture["job_directory"]),
                            "--destination-root",
                            str(root / "app-data"),
                        ]
                    ),
                    0,
                )

        self.assertTrue(json.loads(first_output.getvalue())["created"])
        self.assertFalse(json.loads(second_output.getvalue())["created"])


if __name__ == "__main__":
    unittest.main()

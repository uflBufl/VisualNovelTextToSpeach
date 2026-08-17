import hashlib
import json
import math
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import GeneratedAudioIndex
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

import vntts.authoring.bulk_generation as bulk_module
from vntts.authoring.bulk_generation import (
    LEGACY_STATE_SCHEMA,
    STATE_SCHEMA,
    BulkGenerationError,
    load_generation_state,
    publish_generated_manifest,
    review_generation_item,
    run_bulk_generation,
)
from vntts.authoring.cli import main as authoring_main
from vntts.synthesis import (
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


def queue_item(name="one", *, action="generate", character="Hero"):
    text = f"Exact text for {name}."
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "record_type": "generation_item",
        "queue_id": f"line:{name}:{text_hash[:16]}",
        "line_id": f"line:{name}",
        "text_sha256": text_hash,
        "text": text,
        "speaker": character,
        "voice_character": character,
        "action": action,
        "prompt_adapters": {"generic": f"Delivery for {name}"},
    }


def write_queue(path, items):
    return write_voice_generation_queue(
        path,
        {
            "game": "Synthetic Game",
            "language": "en",
        },
        items,
    )


def audio_samples(sample_rate=16_000):
    indexes = np.arange(sample_rate // 4, dtype=np.float32)
    return (0.25 * np.sin(2 * math.pi * 220 * indexes / sample_rate)).astype(np.float32)


class SyntheticRenderer:
    name = "synthetic"
    model_name = "synthetic-v1"

    def __init__(
        self,
        outcomes=None,
        *,
        inspect_state=None,
        diagnostics_backend=None,
        pcm=None,
    ):
        self.outcomes = list(outcomes or [SynthesisCompletion.COMPLETE])
        self.requests = []
        self.inspect_state = inspect_state
        self.diagnostics_backend = diagnostics_backend
        self.pcm = pcm
        self.stop_calls = 0

    def render(self, request):
        self.requests.append(request)
        if self.inspect_state is not None:
            self.inspect_state(request)
        outcome = (
            self.outcomes.pop(0) if self.outcomes else SynthesisCompletion.COMPLETE
        )
        if isinstance(outcome, BaseException):
            raise outcome
        pcm = audio_samples() if self.pcm is None else self.pcm

        def produce():
            yield SynthesisChunk(pcm, 16_000, 0, 1.0)
            return SynthesisResult(
                pcm=pcm,
                sample_rate=16_000,
                completion=outcome,
                limits=SynthesisLimits(256, 180.0),
                timing=SynthesisTiming(1.0, 2.0),
                diagnostics=SynthesisDiagnostics(
                    backend=self.diagnostics_backend or self.name,
                    cache_source="fresh-generation",
                    generation_profile=request.generation_profile,
                    seed=request.seed,
                    chunk_count=1,
                    sample_count=len(pcm),
                ),
            )

        return SynthesisChunkStream(produce())

    def stop(self):
        self.stop_calls += 1


class AuthoringBulkGenerationTest(unittest.TestCase):
    def run_generation(self, queue, output, renderer, **options):
        return run_bulk_generation(
            queue,
            output,
            renderer,
            provider="synthetic",
            model="synthetic-v1",
            generation_profile="stable",
            **options,
        )

    def test_persists_active_before_render_and_resumes_exact_wav(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            observed = {}

            def inspect_state(_request):
                state = json.loads(
                    (output / "generation-state.json").read_text(encoding="utf-8")
                )
                observed.update(state["active"])

            renderer = SyntheticRenderer(inspect_state=inspect_state)
            first = self.run_generation(queue, output, renderer, seed=7)
            second = self.run_generation(queue, output, renderer, seed=7)
            state = load_generation_state(first.state, queue)
            raw_manifest = json.loads(first.manifest.read_text(encoding="utf-8"))
            result = state["items"][item["queue_id"]]
            audio = output / result["path"]
            audio_hash = sha256_file(audio)

        self.assertEqual(observed["phase"], "generating")
        self.assertEqual(observed["attempt"], 1)
        self.assertEqual(observed["total_attempts"], 1)
        self.assertEqual(observed["seed"], 7)
        self.assertEqual(first.generated, 1)
        self.assertEqual(second.generated, 0)
        self.assertEqual(second.skipped_existing, 1)
        self.assertEqual(len(renderer.requests), 1)
        self.assertEqual(renderer.requests[0].cache_policy.value, "bypass")
        self.assertEqual(result["status"], "generated")
        self.assertEqual(result["review_status"], "pending_review")
        self.assertEqual(result["file_sha256"], audio_hash)
        self.assertEqual(result["quality"]["sample_rate"], 16_000)
        self.assertEqual(raw_manifest["entry_count"], 0)

    def test_exact_queue_id_selection_validates_before_writes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = queue_item("first")
            second = queue_item("second")
            queue = write_queue(root / "queue.jsonl", [first, second])
            output = root / "output"
            renderer = SyntheticRenderer()

            result = self.run_generation(
                queue,
                output,
                renderer,
                include_queue_ids=[second["queue_id"]],
            )
            state = load_generation_state(result.state, queue)

            self.assertEqual(set(state["items"]), {second["queue_id"]})
            self.assertEqual(
                [request.text for request in renderer.requests], [second["text"]]
            )

            untouched = root / "unknown-output"
            with self.assertRaisesRegex(BulkGenerationError, "absent"):
                self.run_generation(
                    queue,
                    untouched,
                    SyntheticRenderer(),
                    include_queue_ids=["unknown:queue-id"],
                )
            self.assertFalse(untouched.exists())

    def test_stale_active_consumes_attempt_and_continues_next_seed_in_legacy_state(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            output.mkdir()
            state = {
                "schema": LEGACY_STATE_SCHEMA,
                "schema_version": 1,
                "queue_sha256": sha256_file(queue),
                "items": {
                    item["queue_id"]: {
                        "status": "failed",
                        "attempts": 2,
                        "seed": 1,
                        "last_error": "Earlier failure",
                        "updated_at": "2026-08-16T10:00:00+00:00",
                    }
                },
                "active": {
                    "queue_id": item["queue_id"],
                    "line_id": item["line_id"],
                    "phase": "generating",
                    "attempt": 1,
                    "attempt_limit": 3,
                    "total_attempts": 3,
                    "seed": 2,
                    "started_at": "2026-08-16T10:01:00+00:00",
                },
            }
            atomic_write_json(output / "generation-state.json", state, sort_keys=True)
            renderer = SyntheticRenderer()

            self.run_generation(queue, output, renderer, retries=0, seed=0)
            resumed = load_generation_state(output / "generation-state.json", queue)

        self.assertEqual(renderer.requests[0].seed, 3)
        self.assertEqual(resumed["schema"], LEGACY_STATE_SCHEMA)
        self.assertEqual(resumed["items"][item["queue_id"]]["attempts"], 4)
        self.assertEqual(resumed["interrupted_attempts"][0]["seed"], 2)

    def test_retry_and_limited_or_cancelled_results_never_publish_partial_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            renderer = SyntheticRenderer(
                [SynthesisCompletion.LIMITED, SynthesisCompletion.COMPLETE]
            )
            result = self.run_generation(
                queue, root / "retry", renderer, retries=1, seed=10
            )
            state = load_generation_state(result.state, queue)

            self.assertEqual([request.seed for request in renderer.requests], [10, 11])
            self.assertEqual(state["items"][item["queue_id"]]["attempts"], 2)
            self.assertEqual(state["items"][item["queue_id"]]["seed"], 11)

            for completion in (
                SynthesisCompletion.LIMITED,
                SynthesisCompletion.CANCELLED,
            ):
                with self.subTest(completion=completion):
                    output = root / completion.value
                    failed = self.run_generation(
                        queue,
                        output,
                        SyntheticRenderer([completion]),
                        retries=0,
                    )
                    failed_state = load_generation_state(failed.state, queue)
                    self.assertEqual(failed.generated, 0)
                    self.assertEqual(failed.failed, 1)
                    self.assertEqual(
                        failed_state["items"][item["queue_id"]]["status"], "failed"
                    )
                    self.assertEqual(list((output / "audio").rglob("*.wav")), [])
                    self.assertEqual(
                        json.loads(failed.manifest.read_text())["entry_count"], 0
                    )
                    if completion is SynthesisCompletion.LIMITED:
                        failure = failed_state["items"][item["queue_id"]]["last_error"]
                        self.assertIn("sample_count=4000", failure)
                        self.assertIn("chunk_count=1", failure)
                        self.assertIn("max_audio_seconds=180.0", failure)
                        self.assertIn("max_tokens=256", failure)

    def test_stereo_renderer_is_downmixed_without_doubling_wav_duration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            mono = audio_samples()
            stereo = np.column_stack((mono, mono * 0.5))

            result = self.run_generation(
                queue,
                root / "output",
                SyntheticRenderer(pcm=stereo),
                retries=0,
            )
            state = load_generation_state(result.state, queue)
            generated = state["items"][item["queue_id"]]

        self.assertEqual(generated["quality"]["channels"], 1)
        self.assertEqual(generated["quality"]["sample_count"], len(mono))
        self.assertEqual(generated["quality"]["duration_seconds"], 0.25)

    def test_crash_leaves_active_and_resume_does_not_repeat_seed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            with self.assertRaises(KeyboardInterrupt):
                self.run_generation(
                    queue,
                    output,
                    SyntheticRenderer([KeyboardInterrupt()]),
                    retries=0,
                    seed=4,
                )
            crashed = json.loads(
                (output / "generation-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(crashed["active"]["seed"], 4)
            self.assertFalse((output / ".generation-lease.json").exists())

            renderer = SyntheticRenderer()
            self.run_generation(queue, output, renderer, retries=0, seed=4)
            resumed = load_generation_state(output / "generation-state.json", queue)

        self.assertEqual(renderer.requests[0].seed, 5)
        self.assertEqual(resumed["items"][item["queue_id"]]["attempts"], 2)

    def test_crash_after_wav_replace_preserves_orphan_before_regeneration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            original_atomic_write = bulk_module.atomic_write_json

            def crash_after_replace(path, document, **options):
                if (
                    Path(path).name == "generation-state.json"
                    and document.get("active") is None
                    and document.get("items", {})
                    .get(item["queue_id"], {})
                    .get("status")
                    == "generated"
                ):
                    raise KeyboardInterrupt()
                return original_atomic_write(path, document, **options)

            with (
                patch.object(
                    bulk_module, "atomic_write_json", side_effect=crash_after_replace
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                self.run_generation(
                    queue, output, SyntheticRenderer(), retries=0, seed=0
                )

            orphan = next((output / "audio").rglob("*.wav"))
            orphan_hash = sha256_file(orphan)
            renderer = SyntheticRenderer()
            self.run_generation(queue, output, renderer, retries=0, seed=0)

            archived = list((output / "interrupted").glob("*.wav"))
            archived_hash = sha256_file(archived[0])
            state = load_generation_state(output / "generation-state.json", queue)

        self.assertEqual(renderer.requests[0].seed, 1)
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived_hash, orphan_hash)
        self.assertEqual(state["items"][item["queue_id"]]["attempts"], 2)

    def test_review_reject_reapprove_and_stale_manifest_recovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())

            review_generation_item(result.state, item["queue_id"], "approved")
            approved = GeneratedAudioIndex.load(result.manifest)
            self.assertIsNotNone(approved.find(item["line_id"], item["text_sha256"]))

            review_generation_item(result.state, item["queue_id"], "rejected")
            self.assertEqual(GeneratedAudioIndex.load(result.manifest).entries, ())

            review_generation_item(result.state, item["queue_id"], "approved")
            result.manifest.write_text(
                json.dumps(
                    {
                        "schema": "vntts.generated-audio",
                        "schema_version": 1,
                        "entry_count": 0,
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )
            publish_generated_manifest(result.state)
            recovered = json.loads(result.manifest.read_text(encoding="utf-8"))
            state = load_generation_state(result.state, queue)

        self.assertEqual(recovered["entry_count"], 1)
        self.assertEqual(recovered["entries"][0]["queue_id"], item["queue_id"])
        self.assertEqual(
            recovered["entries"][0]["audio_sha256"],
            state["items"][item["queue_id"]]["file_sha256"],
        )

    def test_tampered_completed_wav_blocks_resume_and_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            state = json.loads(result.state.read_text(encoding="utf-8"))
            audio = result.state.parent / state["items"][item["queue_id"]]["path"]
            write_pcm16_wav(audio, audio_samples() * 0.5, 16_000)

            with self.assertRaisesRegex(BulkGenerationError, "checksum"):
                self.run_generation(queue, root / "output", SyntheticRenderer())
            with self.assertRaisesRegex(BulkGenerationError, "checksum"):
                review_generation_item(result.state, item["queue_id"], "approved")

    def test_skip_filters_and_limit_apply_before_existing_skip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            items = [
                queue_item("existing"),
                queue_item("limited"),
                queue_item("other", character="Other"),
                queue_item("manual", action="manual_review"),
            ]
            queue = write_queue(root / "queue.jsonl", items)
            output = root / "output"
            self.run_generation(queue, output, SyntheticRenderer(), limit=1)
            renderer = SyntheticRenderer()
            result = self.run_generation(
                queue,
                output,
                renderer,
                limit=1,
                include_characters={"Hero"},
            )

        self.assertEqual(result.skipped_existing, 1)
        self.assertEqual(result.skipped_actions, 1)
        self.assertEqual(result.skipped_characters, 1)
        self.assertEqual(result.generated, 0)
        self.assertEqual(renderer.requests, [])

    def test_live_lease_or_job_pid_blocks_and_stale_lease_is_preserved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "job" / "generated-audio"
            output.mkdir(parents=True)
            lease = {
                "schema": "vntts.authoring-generation-lease",
                "schema_version": 1,
                "queue_sha256": sha256_file(queue),
                "pid": 123,
            }
            atomic_write_json(output / ".generation-lease.json", lease)
            with self.assertRaisesRegex(BulkGenerationError, "Another generation"):
                self.run_generation(
                    queue,
                    output,
                    SyntheticRenderer(),
                    process_checker=lambda _pid: True,
                )

            result = self.run_generation(
                queue,
                output,
                SyntheticRenderer(),
                process_checker=lambda _pid: False,
            )
            self.assertEqual(result.generated, 1)
            self.assertTrue(list((output / "interrupted").glob("*.json")))

            second_output = root / "active-job" / "generated-audio"
            second_output.mkdir(parents=True)
            atomic_write_json(
                second_output.parent / "job.json",
                {"status": "running", "pid": os.getpid()},
            )
            with self.assertRaisesRegex(BulkGenerationError, "active"):
                self.run_generation(
                    queue,
                    second_output,
                    SyntheticRenderer(),
                    process_checker=lambda _pid: True,
                )

    def test_live_lease_with_unknown_start_identity_blocks_takeover(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            output.mkdir()
            lease_path = output / ".generation-lease.json"
            atomic_write_json(
                lease_path,
                {
                    "schema": "vntts.authoring-generation-lease",
                    "schema_version": 1,
                    "queue_sha256": sha256_file(queue),
                    "pid": 123,
                    "hostname": bulk_module.socket.gethostname(),
                    "process_started_at": "known-start",
                    "lease_id": "live-unknown-start",
                },
            )

            with (
                patch(
                    "vntts.authoring.bulk_generation.process_started_at",
                    return_value=None,
                ),
                self.assertRaisesRegex(BulkGenerationError, "Another generation"),
            ):
                self.run_generation(
                    queue,
                    output,
                    SyntheticRenderer(),
                    process_checker=lambda _pid: True,
                )

            preserved = json.loads(lease_path.read_text(encoding="utf-8"))

        self.assertEqual(preserved["lease_id"], "live-unknown-start")

    def test_sparse_legacy_failure_resumes_without_rewriting_schema(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first_item = queue_item("first")
            second_item = queue_item("second")
            queue = write_queue(root / "queue.jsonl", [first_item, second_item])
            output = root / "output"
            self.run_generation(queue, output, SyntheticRenderer(), limit=1)
            state_path = output / "generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema"] = LEGACY_STATE_SCHEMA
            state["items"][second_item["queue_id"]] = {
                "status": "failed",
                "attempts": 7,
                "seed": 6,
                "last_error": "Legacy limit",
                "updated_at": "2026-08-16T10:00:00+00:00",
            }
            atomic_write_json(state_path, state, sort_keys=True)
            renderer = SyntheticRenderer()

            result = self.run_generation(queue, output, renderer, retries=0, seed=0)
            resumed = load_generation_state(state_path, queue)

        self.assertEqual(result.generated, 1)
        self.assertEqual(renderer.requests[0].seed, 7)
        self.assertEqual(resumed["schema"], LEGACY_STATE_SCHEMA)
        self.assertEqual(resumed["items"][second_item["queue_id"]]["attempts"], 8)
        self.assertEqual(
            resumed["items"][first_item["queue_id"]]["status"], "generated"
        )
        self.assertNotEqual(resumed["schema"], STATE_SCHEMA)

    def test_rejects_forged_active_identity_before_consuming_attempt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            output.mkdir()
            atomic_write_json(
                output / "generation-state.json",
                {
                    "schema": LEGACY_STATE_SCHEMA,
                    "schema_version": 1,
                    "queue_sha256": sha256_file(queue),
                    "items": {},
                    "active": {
                        "queue_id": item["queue_id"],
                        "line_id": "wrong-line",
                        "text": "wrong text",
                        "phase": "generating",
                        "attempt": 1,
                        "attempt_limit": 1,
                        "total_attempts": 1,
                        "seed": 0,
                    },
                },
            )

            with self.assertRaisesRegex(BulkGenerationError, "line_id"):
                self.run_generation(queue, output, SyntheticRenderer())
            unchanged = json.loads(
                (output / "generation-state.json").read_text(encoding="utf-8")
            )

        self.assertEqual(unchanged["active"]["line_id"], "wrong-line")
        self.assertEqual(unchanged["items"], {})

    def test_control_byte_change_with_restored_mtime_aborts_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            control = root / "reference.wav"
            control.write_bytes(b"AAAA")
            original_stat = control.stat()

            def mutate_control(_request):
                control.write_bytes(b"BBBB")
                os.utime(
                    control,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )

            with self.assertRaisesRegex(BulkGenerationError, "changed"):
                self.run_generation(
                    queue,
                    root / "output",
                    SyntheticRenderer(inspect_state=mutate_control),
                    control_files={"voice_reference": control},
                )

            state = json.loads(
                (root / "output/generation-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["items"], {})
            self.assertIsNotNone(state["active"])
            self.assertFalse((root / "output/manifest.json").exists())

    def test_directory_control_inventory_binds_every_tree_entry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            model = root / "model"
            (model / "nested").mkdir(parents=True)
            (model / "config.json").write_text("{}", encoding="utf-8")
            (model / "nested" / "weights.bin").write_bytes(b"weights")

            result = self.run_generation(
                queue,
                root / "output",
                SyntheticRenderer(),
                control_files={"model_artifact": model},
            )

            state = json.loads(result.state.read_text(encoding="utf-8"))
            generated = state["items"][item["queue_id"]]
            controls = state["synthesis_controls"][
                generated["synthesis_provenance_sha256"]
            ]
            model_control = controls[0]
            self.assertEqual(model_control["kind"], "directory")
            self.assertEqual(
                [record["path"] for record in model_control["files"]],
                ["config.json", "nested/weights.bin"],
            )
            self.assertEqual(
                load_generation_state(result.state, queue)["synthesis_controls"],
                state["synthesis_controls"],
            )

    def test_lease_takeover_is_detected_and_successor_lease_is_not_unlinked(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"

            def replace_lease(_request):
                lease_path = output / ".generation-lease.json"
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                lease["lease_id"] = "successor"
                atomic_write_json(lease_path, lease, sort_keys=True)

            with self.assertRaisesRegex(BulkGenerationError, "ownership"):
                self.run_generation(
                    queue,
                    output,
                    SyntheticRenderer(inspect_state=replace_lease),
                )

            successor = json.loads(
                (output / ".generation-lease.json").read_text(encoding="utf-8")
            )

        self.assertEqual(successor["lease_id"], "successor")

    def test_review_and_publish_respect_live_generation_lease(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            lease_path = result.state.parent / ".generation-lease.json"
            atomic_write_json(
                lease_path,
                {
                    "schema": "vntts.authoring-generation-lease",
                    "schema_version": 1,
                    "queue_sha256": sha256_file(queue),
                    "pid": os.getpid(),
                    "hostname": bulk_module.socket.gethostname(),
                    "process_started_at": bulk_module._process_started_at(os.getpid()),
                    "lease_id": "live",
                },
            )

            with self.assertRaisesRegex(BulkGenerationError, "Another generation"):
                review_generation_item(result.state, item["queue_id"], "approved")
            with self.assertRaisesRegex(BulkGenerationError, "Another generation"):
                publish_generated_manifest(result.state)

            state = json.loads(result.state.read_text(encoding="utf-8"))

        self.assertEqual(state["items"][item["queue_id"]]["status"], "generated")

    def test_missing_current_provenance_fails_before_review_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            state = json.loads(result.state.read_text(encoding="utf-8"))
            del state["items"][item["queue_id"]]["provider"]
            atomic_write_json(result.state, state, sort_keys=True)

            with self.assertRaisesRegex(BulkGenerationError, "provider"):
                review_generation_item(result.state, item["queue_id"], "approved")

            unchanged = json.loads(result.state.read_text(encoding="utf-8"))

        self.assertEqual(unchanged["items"][item["queue_id"]]["status"], "generated")

    def test_backend_provider_model_and_diagnostics_must_match(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            renderer = SyntheticRenderer()
            with self.assertRaisesRegex(BulkGenerationError, "provider"):
                run_bulk_generation(
                    queue,
                    root / "provider",
                    renderer,
                    provider="forged",
                    model="synthetic-v1",
                )
            renderer.model_name = "actual-model"
            with self.assertRaisesRegex(BulkGenerationError, "model"):
                self.run_generation(queue, root / "model", renderer)

            renderer.name = "synthetic"
            renderer.model_name = "synthetic-v1"
            renderer.diagnostics_backend = "other"
            with self.assertRaisesRegex(BulkGenerationError, "diagnostics"):
                self.run_generation(queue, root / "diagnostics", renderer, retries=0)

    def test_silence_gate_rejects_long_spans_that_pass_peak_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            pcm = np.zeros(16_000 * 2, dtype=np.float32)
            pcm[-4_000:] = audio_samples()[:4_000]
            result = self.run_generation(
                queue,
                root / "output",
                SyntheticRenderer(pcm=pcm),
                retries=0,
            )
            state = load_generation_state(result.state, queue)

        self.assertEqual(result.generated, 0)
        self.assertIn("leading silence", state["items"][item["queue_id"]]["last_error"])

    def test_short_ellipsis_transform_and_sfx_filter_are_recorded(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            spoken = queue_item("ellipsis")
            spoken["text"] = "Wait..."
            spoken["text_sha256"] = hashlib.sha256(b"Wait...").hexdigest()
            spoken["queue_id"] = f"line:ellipsis:{spoken['text_sha256'][:16]}"
            sfx = queue_item("sfx")
            sfx["text"] = "*Door closes*"
            sfx["text_sha256"] = hashlib.sha256(sfx["text"].encode()).hexdigest()
            sfx["queue_id"] = f"line:sfx:{sfx['text_sha256'][:16]}"
            queue = write_queue(root / "queue.jsonl", [spoken, sfx])
            renderer = SyntheticRenderer()
            result = self.run_generation(
                queue,
                root / "output",
                renderer,
                item_filter=bulk_module.is_spoken_queue_item,
                text_transform=bulk_module.normalize_short_trailing_ellipsis,
                text_transform_id="short-trailing-ellipsis-v1",
            )
            state = load_generation_state(result.state, queue)
            generated = state["items"][spoken["queue_id"]]

        self.assertEqual(result.generated, 1)
        self.assertEqual(result.skipped_items, 1)
        self.assertEqual(renderer.requests[0].text, "Wait.")
        self.assertEqual(generated["text_transform"], "short-trailing-ellipsis-v1")
        self.assertEqual(
            generated["synthesis_text_sha256"],
            hashlib.sha256(b"Wait.").hexdigest(),
        )

    def test_state_game_and_language_must_match_bound_queue(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            state = json.loads(result.state.read_text(encoding="utf-8"))
            state["game"] = "Different Game"
            atomic_write_json(result.state, state, sort_keys=True)

            with self.assertRaisesRegex(BulkGenerationError, "game"):
                load_generation_state(result.state, queue)

    def test_cli_generate_review_publish_and_status_use_public_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            renderer = SyntheticRenderer()
            generated_output = StringIO()
            voice_manifest = root / "voices.json"
            voice_manifest.write_text("{}\n", encoding="utf-8")
            reference = root / "hero.wav"
            reference.write_bytes(b"synthetic reference")
            registry = CharacterVoiceRegistry(
                [CharacterVoice("Hero", "hero", references=(reference,))]
            )

            def create_renderer(name, _registry, _cache, **_options):
                renderer.name = name
                renderer.model_name = name
                return renderer

            with (
                patch(
                    "vntts.authoring.cli._load_stable_voice_registry",
                    return_value=(
                        registry,
                        sha256_file(voice_manifest),
                    ),
                ),
                patch(
                    "vntts.authoring.cli.create_backend",
                    side_effect=create_renderer,
                ),
                redirect_stdout(generated_output),
            ):
                exit_code = authoring_main(
                    [
                        "generate",
                        "--queue",
                        str(queue),
                        "--output",
                        str(output),
                        "--voice-manifest",
                        str(voice_manifest),
                        "--backend",
                        "pocket-tts",
                        "--narrator-character",
                        "Hero",
                        "--retries",
                        "0",
                    ]
                )
            generated = json.loads(generated_output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(generated["generated"], 1)
            self.assertEqual(renderer.stop_calls, 1)
            state = load_generation_state(output / "generation-state.json", queue)
            controls = next(iter(state["synthesis_controls"].values()))
            self.assertTrue(
                any(
                    control["role"] == "narrator_selection:Hero"
                    and control["sha256"] == sha256_file(reference)
                    for control in controls
                )
            )

            review_output = StringIO()
            with redirect_stdout(review_output):
                self.assertEqual(
                    authoring_main(
                        [
                            "review",
                            "--state",
                            str(output / "generation-state.json"),
                            item["queue_id"],
                            "approved",
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                json.loads(review_output.getvalue())["decision"], "approved"
            )

            manifest_output = StringIO()
            with redirect_stdout(manifest_output):
                self.assertEqual(
                    authoring_main(
                        [
                            "publish",
                            "--state",
                            str(output / "generation-state.json"),
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                Path(json.loads(manifest_output.getvalue())["manifest"]),
                (output / "manifest.json").resolve(),
            )

            status_output = StringIO()
            with redirect_stdout(status_output):
                self.assertEqual(
                    authoring_main(
                        [
                            "status",
                            "--state",
                            str(output / "generation-state.json"),
                            "--queue",
                            str(queue),
                        ]
                    ),
                    0,
                )
            status = json.loads(status_output.getvalue())
            self.assertEqual(status["approved"], 1)
            self.assertEqual(status["schema"], STATE_SCHEMA)

    def test_cli_missing_manifest_is_actionable_without_traceback(self):
        errors = StringIO()
        with TemporaryDirectory() as directory, redirect_stderr(errors):
            root = Path(directory)
            queue = write_queue(root / "queue.jsonl", [queue_item()])
            with self.assertRaises(SystemExit):
                authoring_main(
                    [
                        "generate",
                        "--queue",
                        str(queue),
                        "--output",
                        str(root / "output"),
                        "--voice-manifest",
                        str(root / "missing.json"),
                        "--backend",
                        "pocket-tts",
                    ]
                )
        self.assertIn("Unable to read voice manifest", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()

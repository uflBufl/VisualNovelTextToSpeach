import hashlib
import json
import math
import os
import socket
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import GeneratedAudioIndex
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

import vntts.authoring.bulk_generation as bulk_module
import vntts.authoring.generation_lease as generation_lease_module
from vntts.audio_cache import PersistentAudioCache
from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.authoring.bulk_generation import (
    LEGACY_STATE_SCHEMA,
    STATE_SCHEMA,
    BulkGenerationError,
    authorize_live_fallback,
    generation_failure_repair_plan,
    generation_failure_report,
    generation_review_authorities,
    generation_review_authority,
    load_generation_state,
    publish_generated_manifest,
    review_generation_item,
    run_bulk_generation,
)
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.failure_repair import FailureRepairPolicy
from vntts.authoring.missing_voice_policy import NARRATOR_ROLES, MissingVoicePolicy
from vntts.authoring.silence_evidence import (
    SilenceFailureEvidenceError,
    load_silence_failure_evidence,
)
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


def queue_item(name="one", *, action="generate", character="Hero", text=None):
    text = text or f"Exact text for {name}."
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


class CacheAwareSyntheticRenderer:
    name = "pocket-tts"
    model_name = "pocket-tts"

    def __init__(self, cache_root):
        self.cache = PersistentAudioCache(Path(cache_root) / "audio")
        self.fresh_renders = 0
        self.stop_calls = 0

    def render(self, request):
        key = self.cache.key(
            backend=self.name,
            model=self.model_name,
            voice=request.voice,
            text=request.text,
            settings={
                "generation_profile": request.generation_profile,
                "seed": request.seed,
            },
        )
        pcm = (
            self.cache.get(key)
            if request.cache_policy is SynthesisCachePolicy.USE
            else None
        )
        cache_source = "persistent-cache" if pcm is not None else "fresh-generation"
        if pcm is None:
            self.fresh_renders += 1
            pcm = audio_samples()
            if request.cache_policy is not SynthesisCachePolicy.BYPASS:
                self.cache.put(key, pcm)

        def produce():
            yield SynthesisChunk(pcm, 16_000, 0, 1.0)
            return SynthesisResult(
                pcm=pcm,
                sample_rate=16_000,
                completion=SynthesisCompletion.COMPLETE,
                limits=SynthesisLimits(256, 180.0),
                timing=SynthesisTiming(0.0, 0.0),
                diagnostics=SynthesisDiagnostics(
                    backend=self.name,
                    cache_source=cache_source,
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

    def test_explicit_synthesis_cache_policy_reaches_backend(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            renderer = SyntheticRenderer()

            self.run_generation(
                queue,
                root / "output",
                renderer,
                synthesis_cache_policy=SynthesisCachePolicy.USE,
            )

        self.assertEqual(renderer.requests[0].cache_policy, SynthesisCachePolicy.USE)

    def test_unknown_synthesis_cache_policy_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(root / "queue.jsonl", [queue_item()])

            with self.assertRaisesRegex(
                BulkGenerationError,
                "Unknown synthesis cache policy",
            ):
                self.run_generation(
                    queue,
                    root / "output",
                    SyntheticRenderer(),
                    synthesis_cache_policy="invented",
                )

    def test_self_service_pocket_failure_becomes_evidenced_live_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            renderer = SyntheticRenderer([SynthesisCompletion.LIMITED])
            renderer.name = "pocket-tts"
            renderer.model_name = "pocket-tts"
            failed = run_bulk_generation(
                queue,
                output,
                renderer,
                provider="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                retries=0,
            )

            decision = authorize_live_fallback(
                failed.state,
                queue,
                item["queue_id"],
                reason="automatic_recovery_exhausted",
                model="pocket-tts",
            )
            state = load_generation_state(failed.state, queue)
            stored = state["items"][item["queue_id"]]

        self.assertEqual(decision["schema_version"], 8)
        self.assertEqual(decision["evidence"]["recovery_action"], "bounded_seed_retry")
        self.assertEqual(stored["status"], "live_fallback")
        self.assertEqual(stored["review_status"], "live_fallback")
        self.assertEqual(
            stored["live_fallback"]["previous_result_sha256"],
            stored["live_fallback"]["evidence"]["base_result_sha256"],
        )

    def test_batch_review_authorities_share_one_state_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            items = [queue_item("one"), queue_item("two")]
            queue = write_queue(root / "queue.jsonl", items)
            result = self.run_generation(
                queue,
                root / "output",
                SyntheticRenderer(),
            )

            authorities = generation_review_authorities(
                result.state,
                (items[1]["queue_id"], items[0]["queue_id"]),
            )

        self.assertEqual(set(authorities), {item["queue_id"] for item in items})
        self.assertEqual(
            len({authority.state_sha256 for authority in authorities.values()}),
            1,
        )

    def test_explicit_regeneration_replaces_only_pending_review_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            first_renderer = SyntheticRenderer()
            self.run_generation(queue, output, first_renderer, seed=0)
            first_state = load_generation_state(output / "generation-state.json", queue)
            first_item = first_state["items"][item["queue_id"]]
            first_sha256 = first_item["file_sha256"]

            replacement_pcm = audio_samples() * 0.5
            replacement_renderer = SyntheticRenderer(pcm=replacement_pcm)
            result = self.run_generation(
                queue,
                output,
                replacement_renderer,
                seed=0,
                include_characters=("Hero",),
                regenerate_existing=True,
            )
            state = load_generation_state(result.state, queue)
            replaced = state["items"][item["queue_id"]]

            self.assertEqual(result.generated, 1)
            self.assertEqual(result.skipped_existing, 0)
            self.assertEqual(len(replacement_renderer.requests), 1)
            self.assertEqual(replacement_renderer.requests[0].seed, 1)
            self.assertEqual(replaced["attempts"], 2)
            self.assertNotEqual(replaced["file_sha256"], first_sha256)
            self.assertEqual(replaced["review_status"], "pending_review")

            review_generation_item(result.state, item["queue_id"], "approved")
            approved_payload = result.state.read_bytes()
            with self.assertRaisesRegex(
                BulkGenerationError, "cannot overwrite an approved or rejected"
            ):
                self.run_generation(
                    queue,
                    output,
                    SyntheticRenderer(),
                    include_queue_ids=(item["queue_id"],),
                    regenerate_existing=True,
                )
            self.assertEqual(result.state.read_bytes(), approved_payload)

    def test_regeneration_requires_an_explicit_scope(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(root / "queue.jsonl", [queue_item()])
            with self.assertRaisesRegex(
                BulkGenerationError, "requires explicit queue IDs or characters"
            ):
                self.run_generation(
                    queue,
                    root / "output",
                    SyntheticRenderer(),
                    regenerate_existing=True,
                )

    def test_regeneration_rejects_protected_decisions_before_rendering(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = queue_item("first")
            second = queue_item("second")
            queue = write_queue(root / "queue.jsonl", [first, second])
            output = root / "output"
            self.run_generation(queue, output, SyntheticRenderer())
            state_path = output / "generation-state.json"
            review_generation_item(state_path, second["queue_id"], "approved")
            before = state_path.read_bytes()
            renderer = SyntheticRenderer()

            with self.assertRaisesRegex(
                BulkGenerationError, "cannot overwrite an approved or rejected"
            ):
                self.run_generation(
                    queue,
                    output,
                    renderer,
                    include_characters=("Hero",),
                    regenerate_existing=True,
                )

            self.assertEqual(renderer.requests, [])
            self.assertEqual(state_path.read_bytes(), before)

    def test_exact_unknown_voice_character_renders_as_narrator(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(character="Hero")
            item["speaker"] = "???"
            queue = write_queue(root / "queue.jsonl", [item])
            renderer = SyntheticRenderer()

            result = self.run_generation(queue, root / "output", renderer)
            state = load_generation_state(result.state, queue)

        self.assertEqual(renderer.requests[0].voice, "Narrator")
        self.assertEqual(
            state["items"][item["queue_id"]]["voice_character"], "Narrator"
        )

    def test_explicit_missing_voice_fallback_preserves_source_and_narrator_provenance(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(character="Poacher I")
            queue = write_queue(root / "queue.jsonl", [item])
            renderer = SyntheticRenderer()
            policy = MissingVoicePolicy(NARRATOR_ROLES, ("Poacher I",))
            narrator_reference = root / "centurion.wav"
            narrator_reference.write_bytes(b"bound narrator reference")
            voice_manifest = root / "voices.json"
            voice_manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Centurion",
                                "speaker": "Centurion",
                                "reference": "centurion.wav",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_generation(
                queue,
                root / "output",
                renderer,
                synthesis_character_overrides={"Poacher I": "Narrator"},
                missing_voice_policy=policy.to_document(),
                narrator_character="Centurion",
                control_files={
                    "voice_manifest": voice_manifest,
                    "narrator_selection:Centurion": narrator_reference,
                },
            )
            state = load_generation_state(result.state, queue)
            generated = state["items"][item["queue_id"]]
            review_generation_item(result.state, item["queue_id"], "approved")
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            published = manifest["entries"][0]

        self.assertEqual(renderer.requests[0].voice, "Narrator")
        self.assertEqual(generated["requested_voice_character"], "Poacher I")
        self.assertEqual(generated["voice_character"], "Narrator")
        self.assertEqual(generated["narrator_character"], "Centurion")
        self.assertEqual(
            generated["synthesis_configuration"],
            {
                "missing_voice_policy": policy.to_document(),
                "synthesis_character_overrides": {"poacheri": "Narrator"},
                "failure_repair_policy": FailureRepairPolicy().to_document(),
            },
        )
        self.assertEqual(
            generated["synthesis_fallback"],
            {
                "schema_version": 1,
                "kind": "missing_voice_to_narrator",
                "policy": policy.to_document(),
                "source_voice_character": "Poacher I",
                "synthesis_voice_character": "Narrator",
                "narrator_character": "Centurion",
            },
        )
        self.assertEqual(
            published["synthesis_fallback"], generated["synthesis_fallback"]
        )
        self.assertEqual(published["requested_voice_character"], "Poacher I")
        self.assertEqual(published["speaker"], "Poacher I")
        self.assertEqual(published["voice_character"], "Narrator")
        self.assertEqual(
            published["synthesis_configuration"],
            generated["synthesis_configuration"],
        )

    def test_missing_voice_override_must_be_authorized_before_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(
                root / "queue.jsonl", [queue_item(character="Hotelier")]
            )
            output = root / "output"

            with self.assertRaisesRegex(
                BulkGenerationError, "does not authorize role 'Hotelier'"
            ):
                self.run_generation(
                    queue,
                    output,
                    SyntheticRenderer(),
                    synthesis_character_overrides={"Hotelier": "Narrator"},
                    missing_voice_policy=MissingVoicePolicy().to_document(),
                    narrator_character="Centurion",
                )

            self.assertFalse(output.exists())

    def test_pocket_fallback_accepts_allowlisted_reference_free_narrator(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(character="Poacher I")
            queue = write_queue(root / "queue.jsonl", [item])
            voice_manifest = root / "voices.json"
            voice_manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Narrator",
                                "speaker": "alba",
                                "aliases": [],
                                "references": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            renderer = SyntheticRenderer()
            renderer.name = "pocket-tts"
            renderer.model_name = "pocket-tts"
            policy = MissingVoicePolicy(NARRATOR_ROLES, ("Poacher I",))

            result = run_bulk_generation(
                queue,
                root / "output",
                renderer,
                provider="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                retries=0,
                synthesis_character_overrides={"Poacher I": "Narrator"},
                missing_voice_policy=policy.to_document(),
                narrator_character="Narrator",
                control_files={"voice_manifest": voice_manifest},
            )

        self.assertEqual(result.generated, 1)
        self.assertEqual(renderer.requests[0].voice, "Narrator")

    def test_fallback_refuses_a_role_that_still_has_manifest_references(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(
                root / "queue.jsonl", [queue_item(character="Hotelier")]
            )
            for name in ("hotelier.wav", "centurion.wav"):
                (root / name).write_bytes(name.encode("utf-8"))
            voice_manifest = root / "voices.json"
            voice_manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Hotelier",
                                "speaker": "Hotelier",
                                "reference": "hotelier.wav",
                            },
                            {
                                "character": "Centurion",
                                "speaker": "Centurion",
                                "reference": "centurion.wav",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "output"

            with self.assertRaisesRegex(
                BulkGenerationError, "still has configured references"
            ):
                self.run_generation(
                    queue,
                    output,
                    SyntheticRenderer(),
                    synthesis_character_overrides={"Hotelier": "Narrator"},
                    missing_voice_policy=MissingVoicePolicy(
                        NARRATOR_ROLES, ("Hotelier",)
                    ).to_document(),
                    narrator_character="Centurion",
                    control_files={"voice_manifest": voice_manifest},
                )

            self.assertFalse(output.exists())

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
                        typed = failed_state["items"][item["queue_id"]]["failure"]
                        self.assertEqual(typed["kind"], "missed_eos_audio_limit")
                        self.assertEqual(typed["render"]["sample_count"], 4000)
                        self.assertEqual(typed["text_features"]["word_count"], 4)
                    else:
                        self.assertEqual(
                            failed_state["items"][item["queue_id"]]["failure"]["kind"],
                            "cancelled",
                        )

    def test_failure_report_reconciles_typed_and_legacy_cohorts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            limited_item = queue_item("limited", character="Narrator")
            silence_item = queue_item("silence", character="Rhiannon")
            unbound_item = queue_item("unbound", character="Narrator")
            queue = write_queue(
                root / "queue.jsonl", [limited_item, silence_item, unbound_item]
            )
            result = self.run_generation(
                queue,
                root / "output",
                SyntheticRenderer([SynthesisCompletion.LIMITED]),
                retries=0,
                include_queue_ids=[limited_item["queue_id"]],
            )
            state = json.loads(result.state.read_text(encoding="utf-8"))
            limited = state["items"][limited_item["queue_id"]]
            limited["attempts"] = 5
            limited["attempts_by_provider"] = {
                "legacy-unbound": 3,
                limited["provider"]: 2,
            }
            state["items"][silence_item["queue_id"]] = {
                "status": "failed",
                "attempts": 3,
                "seed": 2,
                "last_error": "MOSS output failed speech quality: 3.20s internal silence",
                "provider": "moss-tts",
                "model": "moss-local",
                "generation_profile": "stable",
                "synthesis_provenance_sha256": "a" * 64,
                "updated_at": "2026-08-18T00:00:00+00:00",
            }
            state["items"][unbound_item["queue_id"]] = {
                "status": "failed",
                "attempts": 3,
                "seed": 2,
                "last_error": "MOSS generation hit the text-length audio limit before EOS",
                "updated_at": "2026-08-18T00:00:00+00:00",
            }
            atomic_write_json(result.state, state, sort_keys=True)

            report = generation_failure_report(result.state, queue)
            repair_plan = generation_failure_repair_plan(result.state, queue)
            output = StringIO()
            with redirect_stdout(output):
                exit_code = authoring_main(
                    [
                        "failure-report",
                        "--state",
                        str(result.state),
                        "--queue",
                        str(queue),
                    ]
                )
            repair_output = StringIO()
            with redirect_stdout(repair_output):
                repair_exit_code = authoring_main(
                    [
                        "failure-repair-plan",
                        "--state",
                        str(result.state),
                        "--queue",
                        str(queue),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(repair_exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), report)
        self.assertEqual(json.loads(repair_output.getvalue()), repair_plan)
        self.assertEqual(report["failure_count"], 3)
        self.assertEqual(
            {entry["value"]: entry["count"] for entry in report["cohorts"]["kind"]},
            {"missed_eos_audio_limit": 2, "speech_silence": 1},
        )
        legacy = next(
            record
            for record in report["records"]
            if record["queue_id"] == silence_item["queue_id"]
        )
        self.assertTrue(legacy["failure"]["inferred_from_legacy_error"])
        self.assertEqual(legacy["requested_voice_character"], "Rhiannon")
        self.assertEqual(
            repair_plan["action_counts"],
            {
                "bounded_seed_retry": 1,
                "provenance_recovery_or_regeneration": 1,
                "reference_comparison": 1,
            },
        )
        actions = {
            record["queue_id"]: record["action"] for record in repair_plan["records"]
        }
        self.assertEqual(actions[limited_item["queue_id"]], "bounded_seed_retry")
        limited_plan = next(
            record
            for record in repair_plan["records"]
            if record["queue_id"] == limited_item["queue_id"]
        )
        self.assertEqual(limited_plan["attempts"], 5)
        self.assertIn("current provider", limited_plan["reason"])
        self.assertEqual(actions[silence_item["queue_id"]], "reference_comparison")
        self.assertEqual(
            actions[unbound_item["queue_id"]],
            "provenance_recovery_or_regeneration",
        )

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

    def test_pocket_generation_is_one_unseeded_attempt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            renderer = SyntheticRenderer(diagnostics_backend="pocket-tts")
            renderer.name = "pocket-tts"
            renderer.model_name = "pocket-tts"

            result = run_bulk_generation(
                queue,
                root / "output",
                renderer,
                provider="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                retries=0,
                seed=17,
            )
            state = load_generation_state(result.state, queue)
            generated = state["items"][item["queue_id"]]

            with self.assertRaisesRegex(
                BulkGenerationError, "unseeded and permits exactly one attempt"
            ):
                run_bulk_generation(
                    queue,
                    root / "retry-output",
                    renderer,
                    provider="pocket-tts",
                    model="pocket-tts",
                    generation_profile="default",
                    retries=1,
                    seed=17,
                )

        self.assertIsNone(renderer.requests[0].seed)
        self.assertEqual(generated["seed"], 17)
        self.assertFalse(generated["seed_applied"])
        self.assertEqual(generated["attempts_by_provider"], {"pocket-tts": 1})

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

    def test_review_lease_loss_is_detected_before_state_or_manifest_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            authority = generation_review_authority(result.state, item["queue_id"])
            state_before = result.state.read_bytes()
            manifest_before = result.manifest.read_bytes()
            original_writer = bulk_module._write_generated_manifest_from_state

            def steal_lease_during_staging(*arguments, **options):
                written = original_writer(*arguments, **options)
                lease_path = result.state.parent / ".generation-lease.json"
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                lease["lease_id"] = "stolen-during-manifest-staging"
                atomic_write_json(lease_path, lease, sort_keys=True)
                return written

            with (
                patch.object(
                    bulk_module,
                    "_write_generated_manifest_from_state",
                    side_effect=steal_lease_during_staging,
                ),
                self.assertRaisesRegex(BulkGenerationError, "lease ownership changed"),
            ):
                review_generation_item(
                    result.state,
                    item["queue_id"],
                    "approved",
                    expected_authority=authority,
                    queue_path=queue,
                )

            self.assertEqual(result.state.read_bytes(), state_before)
            self.assertEqual(result.manifest.read_bytes(), manifest_before)

    def test_review_wav_change_during_staging_prevents_canonical_commit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            authority = generation_review_authority(result.state, item["queue_id"])
            state = load_generation_state(result.state, queue)
            audio = result.state.parent / state["items"][item["queue_id"]]["path"]
            state_before = result.state.read_bytes()
            manifest_before = result.manifest.read_bytes()
            original_writer = bulk_module._write_generated_manifest_from_state

            def change_wav_during_staging(*arguments, **options):
                written = original_writer(*arguments, **options)
                write_pcm16_wav(audio, audio_samples() * 0.5, 16_000)
                return written

            with (
                patch.object(
                    bulk_module,
                    "_write_generated_manifest_from_state",
                    side_effect=change_wav_during_staging,
                ),
                self.assertRaisesRegex(BulkGenerationError, "authority changed"),
            ):
                review_generation_item(
                    result.state,
                    item["queue_id"],
                    "approved",
                    expected_authority=authority,
                    queue_path=queue,
                )

            self.assertEqual(result.state.read_bytes(), state_before)
            self.assertEqual(result.manifest.read_bytes(), manifest_before)

    def test_lease_change_after_state_commit_reports_manifest_recovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            authority = generation_review_authority(result.state, item["queue_id"])
            manifest_before = result.manifest.read_bytes()
            original_replace = os.replace

            def steal_after_state_replace(source, destination):
                replaced = original_replace(source, destination)
                if Path(destination).resolve() == result.state.resolve():
                    lease_path = result.state.parent / ".generation-lease.json"
                    lease = json.loads(lease_path.read_text(encoding="utf-8"))
                    lease["lease_id"] = "stolen-after-state-commit"
                    atomic_write_json(lease_path, lease, sort_keys=True)
                return replaced

            with (
                patch.object(
                    bulk_module.os,
                    "replace",
                    side_effect=steal_after_state_replace,
                ),
                self.assertRaisesRegex(BulkGenerationError, "decision was saved"),
            ):
                review_generation_item(
                    result.state,
                    item["queue_id"],
                    "approved",
                    expected_authority=authority,
                    queue_path=queue,
                )

            saved = json.loads(result.state.read_text(encoding="utf-8"))
            self.assertEqual(saved["items"][item["queue_id"]]["status"], "approved")
            self.assertEqual(result.manifest.read_bytes(), manifest_before)

    def test_cohort_authority_uses_one_shared_state_and_queue_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            queue.write_bytes(b"exact queue bytes\n")
            queue_sha256 = sha256_file(queue)
            audio_paths = {}
            items = {}
            for index in (1, 2):
                queue_id = f"queue-{index}"
                audio = root / f"audio-{index}.wav"
                audio.write_bytes(f"exact audio {index}".encode())
                audio_paths[queue_id] = audio
                items[queue_id] = {
                    "status": "generated",
                    "review_status": "pending_review",
                    "path": audio.name,
                }
            state_path = root / "generation-state.json"
            atomic_write_json(
                state_path,
                {"queue_sha256": queue_sha256, "items": items},
                sort_keys=True,
            )
            state_sha256 = sha256_file(state_path)
            authorities = {
                queue_id: bulk_module.ReviewAuthority(
                    queue_sha256=queue_sha256,
                    state_sha256=state_sha256,
                    item_sha256=bulk_module._canonical_sha256(item),
                    audio_sha256=sha256_file(audio_paths[queue_id]),
                )
                for queue_id, item in items.items()
            }
            reads = []
            real_read_bytes = Path.read_bytes

            def tracked_read_bytes(path):
                reads.append(Path(path).resolve())
                return real_read_bytes(path)

            with patch.object(Path, "read_bytes", tracked_read_bytes):
                state, snapshots = bulk_module._assert_review_authorities(
                    state_path, authorities, queue
                )

            self.assertEqual(state["items"], items)
            self.assertEqual(set(snapshots), set(items))
            self.assertEqual(reads.count(state_path.resolve()), 1)
            self.assertEqual(reads.count(queue.resolve()), 1)
            audio_paths["queue-1"].write_bytes(b"changed audio")
            with self.assertRaisesRegex(BulkGenerationError, "authority changed"):
                bulk_module._assert_review_authorities(state_path, authorities, queue)

    def test_cohort_commit_validates_only_newly_approved_wavs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = queue_item("first")
            second = queue_item("second")
            queue = write_queue(root / "queue.jsonl", [first, second])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            first_authority = generation_review_authority(
                result.state, first["queue_id"]
            )
            review_generation_item(
                result.state,
                first["queue_id"],
                "approved",
                expected_authority=first_authority,
                queue_path=queue,
            )
            second_authority = generation_review_authority(
                result.state, second["queue_id"]
            )
            state = json.loads(result.state.read_text(encoding="utf-8"))
            first_wav = result.state.parent / state["items"][first["queue_id"]]["path"]
            write_pcm16_wav(first_wav, audio_samples() * 0.5, 16_000)
            checked = []
            original_validate = bulk_module._validate_success_file

            def track_validation(queue_id, item, audio):
                checked.append(queue_id)
                return original_validate(queue_id, item, audio)

            with patch.object(
                bulk_module,
                "_validate_success_file",
                side_effect=track_validation,
            ):
                bulk_module._review_generation_cohort(
                    result.state,
                    queue,
                    {second["queue_id"]: second_authority},
                    "approved",
                    provenance={"test": "approved-manifest-integrity"},
                )
            state = json.loads(result.state.read_text(encoding="utf-8"))
            manifest = GeneratedAudioIndex.load(result.manifest)

            with self.assertRaisesRegex(BulkGenerationError, "checksum mismatch"):
                publish_generated_manifest(result.state)

        self.assertEqual(set(checked), {second["queue_id"]})
        self.assertEqual(checked.count(second["queue_id"]), 2)
        self.assertEqual(
            state["items"][second["queue_id"]]["review_status"], "approved"
        )
        self.assertEqual(len(manifest.entries), 2)

    def test_mixed_cohort_commit_projects_each_item_and_only_approved_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = queue_item("first")
            second = queue_item("second")
            queue = write_queue(root / "queue.jsonl", [first, second])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            authorities = {
                item["queue_id"]: generation_review_authority(
                    result.state, item["queue_id"]
                )
                for item in (first, second)
            }

            commits = bulk_module._review_generation_cohort(
                result.state,
                queue,
                authorities,
                {
                    first["queue_id"]: "rejected",
                    second["queue_id"]: "approved",
                },
                provenance={"test": "mixed-cohort"},
            )

            state = load_generation_state(result.state)
            manifest = GeneratedAudioIndex.load(result.manifest)

        self.assertEqual(
            {
                queue_id: (item["status"], item["review_status"])
                for queue_id, item in state["items"].items()
            },
            {
                first["queue_id"]: ("generated", "rejected"),
                second["queue_id"]: ("approved", "approved"),
            },
        )
        self.assertEqual(
            [(commit.queue_id, commit.review_status) for commit in commits],
            [
                (first["queue_id"], "rejected"),
                (second["queue_id"], "approved"),
            ],
        )
        self.assertEqual(
            [entry.line_id for entry in manifest.entries],
            [second["line_id"]],
        )

    def test_partial_mixed_cohort_leaves_unsampled_authority_pending(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = queue_item("first")
            second = queue_item("second")
            third = queue_item("third")
            queue = write_queue(root / "queue.jsonl", [first, second, third])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            authorities = {
                item["queue_id"]: generation_review_authority(
                    result.state, item["queue_id"]
                )
                for item in (first, second, third)
            }
            pending_before = load_generation_state(result.state)["items"][
                third["queue_id"]
            ]

            commits = bulk_module._review_generation_cohort(
                result.state,
                queue,
                authorities,
                {
                    first["queue_id"]: "rejected",
                    second["queue_id"]: "approved",
                    third["queue_id"]: "pending_review",
                },
                provenance={"test": "partial-mixed-cohort"},
            )

            state = load_generation_state(result.state)
            manifest = GeneratedAudioIndex.load(result.manifest)

        self.assertEqual(
            [(commit.queue_id, commit.review_status) for commit in commits],
            [(first["queue_id"], "rejected"), (second["queue_id"], "approved")],
        )
        self.assertEqual(
            (
                state["items"][third["queue_id"]]["status"],
                state["items"][third["queue_id"]]["review_status"],
            ),
            ("generated", "pending_review"),
        )
        self.assertEqual(state["items"][third["queue_id"]], pending_before)
        self.assertEqual(
            [entry.line_id for entry in manifest.entries], [second["line_id"]]
        )

    def test_mixed_cohort_wav_change_during_staging_commits_nothing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = queue_item("first")
            second = queue_item("second")
            queue = write_queue(root / "queue.jsonl", [first, second])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            authorities = {
                item["queue_id"]: generation_review_authority(
                    result.state, item["queue_id"]
                )
                for item in (first, second)
            }
            state = load_generation_state(result.state, queue)
            audio = result.state.parent / state["items"][first["queue_id"]]["path"]
            state_before = result.state.read_bytes()
            manifest_before = result.manifest.read_bytes()
            original_writer = bulk_module._write_generated_manifest_from_state
            writes = 0

            def mutate_after_conservative_staging(*arguments, **options):
                nonlocal writes
                written = original_writer(*arguments, **options)
                writes += 1
                if writes == 2:
                    write_pcm16_wav(audio, audio_samples() * 0.5, 16_000)
                return written

            with (
                patch.object(
                    bulk_module,
                    "_write_generated_manifest_from_state",
                    side_effect=mutate_after_conservative_staging,
                ),
                self.assertRaisesRegex(BulkGenerationError, "authority changed"),
            ):
                bulk_module._review_generation_cohort(
                    result.state,
                    queue,
                    authorities,
                    {
                        first["queue_id"]: "rejected",
                        second["queue_id"]: "approved",
                    },
                    provenance={"test": "mixed-wav-race"},
                )

            self.assertEqual(result.state.read_bytes(), state_before)
            self.assertEqual(result.manifest.read_bytes(), manifest_before)

    def test_mixed_cohort_lease_loss_after_state_keeps_manifest_conservative(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = queue_item("first")
            second = queue_item("second")
            queue = write_queue(root / "queue.jsonl", [first, second])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            authorities = {
                item["queue_id"]: generation_review_authority(
                    result.state, item["queue_id"]
                )
                for item in (first, second)
            }
            original_replace = os.replace

            def steal_after_state_replace(source, destination):
                replaced = original_replace(source, destination)
                if Path(destination).resolve() == result.state.resolve():
                    lease_path = result.state.parent / ".generation-lease.json"
                    lease = json.loads(lease_path.read_text(encoding="utf-8"))
                    lease["lease_id"] = "stolen-after-mixed-state"
                    atomic_write_json(lease_path, lease, sort_keys=True)
                return replaced

            with (
                patch.object(
                    bulk_module.os,
                    "replace",
                    side_effect=steal_after_state_replace,
                ),
                self.assertRaisesRegex(BulkGenerationError, "lease ownership changed"),
            ):
                bulk_module._review_generation_cohort(
                    result.state,
                    queue,
                    authorities,
                    {
                        first["queue_id"]: "rejected",
                        second["queue_id"]: "approved",
                    },
                    provenance={"test": "mixed-lease-race"},
                )

            state = load_generation_state(result.state, queue)
            manifest = GeneratedAudioIndex.load(result.manifest)

        self.assertEqual(state["items"][first["queue_id"]]["review_status"], "rejected")
        self.assertEqual(
            state["items"][second["queue_id"]]["review_status"], "approved"
        )
        self.assertEqual(manifest.entries, ())

    def test_cohort_approval_wav_change_after_state_keeps_manifest_conservative(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item("cohort-race")
            queue = write_queue(root / "queue.jsonl", [item])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            authority = generation_review_authority(result.state, item["queue_id"])
            state = load_generation_state(result.state, queue)
            audio = result.state.parent / state["items"][item["queue_id"]]["path"]
            original_replace = os.replace

            def mutate_after_state_replace(source, destination):
                replaced = original_replace(source, destination)
                if Path(destination).resolve() == result.state.resolve():
                    write_pcm16_wav(audio, audio_samples() * 0.5, 16_000)
                return replaced

            with (
                patch.object(
                    bulk_module.os,
                    "replace",
                    side_effect=mutate_after_state_replace,
                ),
                self.assertRaisesRegex(BulkGenerationError, "decision was saved"),
            ):
                bulk_module._review_generation_cohort(
                    result.state,
                    queue,
                    {item["queue_id"]: authority},
                    "approved",
                    provenance={"test": "cohort-wav-race"},
                )

            committed = json.loads(result.state.read_text(encoding="utf-8"))
            manifest = GeneratedAudioIndex.load(result.manifest)

        self.assertEqual(
            committed["items"][item["queue_id"]]["review_status"], "approved"
        )
        self.assertEqual(manifest.entries, ())

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

    def test_stale_lease_recovery_cannot_archive_a_replacement_owner(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            output.mkdir()
            lease_path = output / ".generation-lease.json"
            stale = {
                "schema": "vntts.authoring-generation-lease",
                "schema_version": 1,
                "queue_sha256": sha256_file(queue),
                "pid": 999999,
                "hostname": socket.gethostname(),
                "process_started_at": "stale-start",
                "lease_id": "stale-owner",
            }
            replacement = {
                **stale,
                "pid": os.getpid(),
                "process_started_at": bulk_module.process_started_at(os.getpid()),
                "lease_id": "live-replacement",
            }
            atomic_write_json(lease_path, stale)
            archive = generation_lease_module.archive_interrupted_artifact

            def replace_before_archive(*args, **kwargs):
                atomic_write_json(lease_path, replacement)
                return archive(*args, **kwargs)

            with (
                patch.object(
                    generation_lease_module,
                    "archive_interrupted_artifact",
                    side_effect=replace_before_archive,
                ),
                self.assertRaisesRegex(BulkGenerationError, "changed before recovery"),
            ):
                self.run_generation(
                    queue,
                    output,
                    SyntheticRenderer(),
                    process_checker=lambda _pid: False,
                )

            self.assertEqual(
                json.loads(lease_path.read_text(encoding="utf-8")), replacement
            )
            self.assertEqual(list((output / "interrupted").glob("*.json")), [])

    def test_generation_lease_cleanup_waits_for_guard_and_removes_its_owner(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            output.mkdir()
            lease = bulk_module._GenerationLease(
                output,
                "1" * 64,
                process_checker=lambda _pid: False,
            )
            lease.__enter__()
            acquired = Event()

            def hold_guard():
                with exclusive_advisory_lock(output / ".generation-lease.guard"):
                    acquired.set()
                    Event().wait(0.05)

            holder = Thread(target=hold_guard)
            holder.start()
            self.assertTrue(acquired.wait(1))

            lease.__exit__(None, None, None)
            holder.join(1)

            self.assertFalse((output / ".generation-lease.json").exists())
            self.assertFalse(holder.is_alive())

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
                    "hostname": socket.gethostname(),
                    "process_started_at": "known-start",
                    "lease_id": "live-unknown-start",
                },
            )

            with (
                patch(
                    "vntts.authoring.generation_lease.process_started_at",
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

    def test_explicit_legacy_failure_regeneration_starts_fresh_provider_seed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item("legacy")
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            state_path = output / "generation-state.json"
            output.mkdir()
            atomic_write_json(
                state_path,
                {
                    "schema": LEGACY_STATE_SCHEMA,
                    "schema_version": 1,
                    "queue_sha256": sha256_file(queue),
                    "game": None,
                    "language": None,
                    "active": None,
                    "items": {
                        item["queue_id"]: {
                            "status": "failed",
                            "attempts": 7,
                            "seed": 6,
                            "last_error": "Legacy limit",
                            "updated_at": "2026-08-16T10:00:00+00:00",
                        }
                    },
                },
                sort_keys=True,
            )
            renderer = SyntheticRenderer()

            result = self.run_generation(
                queue,
                output,
                renderer,
                retries=0,
                seed=0,
                include_queue_ids=(item["queue_id"],),
                regenerate_existing=True,
            )
            regenerated = load_generation_state(state_path, queue)["items"][
                item["queue_id"]
            ]

        self.assertEqual(result.generated, 1)
        self.assertEqual(renderer.requests[0].seed, 0)
        self.assertEqual(regenerated["attempts"], 8)
        self.assertEqual(
            regenerated["attempts_by_provider"],
            {"legacy-unbound": 7, "synthetic": 1},
        )

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
                    "hostname": socket.gethostname(),
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
        failure = state["items"][item["queue_id"]]["failure"]
        self.assertEqual(failure["kind"], "speech_silence")
        self.assertGreater(failure["speech_quality"]["leading_silence_seconds"], 0.8)
        self.assertIn("leading silence", failure["silence_failures"][0])

    def test_selected_silence_failure_can_be_captured_only_as_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(
                text="The gate is already open. We should leave before dawn."
            )
            queue = write_queue(root / "queue.jsonl", [item])
            tone = audio_samples()
            pcm = np.concatenate((tone, np.zeros(16_000 * 2, dtype=np.float32), tone))
            evidence = root / "evidence"

            result = self.run_generation(
                queue,
                root / "output",
                SyntheticRenderer(pcm=pcm),
                retries=0,
                include_queue_ids=(item["queue_id"],),
                silence_failure_evidence=evidence,
            )
            document = load_silence_failure_evidence(evidence)
            state = load_generation_state(result.state, queue)
            state_sha256 = sha256_file(result.state)
            published_wavs = list((root / "output").rglob("*.wav"))
            tampered = json.loads((evidence / "evidence.json").read_text())
            tampered["metadata"]["state_item"]["seed"] = 999
            (evidence / "evidence.json").write_text(json.dumps(tampered))
            with self.assertRaisesRegex(SilenceFailureEvidenceError, "state item"):
                load_silence_failure_evidence(evidence)

        self.assertEqual(result.generated, 0)
        self.assertEqual(state["items"][item["queue_id"]]["status"], "failed")
        self.assertFalse(document["reviewable"])
        self.assertFalse(document["generated_outcome"])
        self.assertEqual(document["metadata"]["queue_id"], item["queue_id"])
        self.assertEqual(document["metadata"]["state_sha256"], state_sha256)
        self.assertEqual(
            document["metadata"]["state_item"]["failure"]["kind"],
            "speech_silence",
        )
        self.assertEqual(published_wavs, [])

    def test_silence_failure_capture_requires_exact_one_attempt_scope(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            with self.assertRaisesRegex(BulkGenerationError, "one exact queue ID"):
                self.run_generation(
                    queue,
                    root / "output",
                    SyntheticRenderer(),
                    silence_failure_evidence=root / "evidence",
                )

    def test_silence_gate_normalizes_pcm16_before_applying_dbfs_threshold(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            wav = root / "room-tone-pause.wav"
            tone = audio_samples()
            room_tone = np.full(16_000 * 2, 0.001, dtype=np.float32)
            write_pcm16_wav(wav, np.concatenate((tone, room_tone, tone)), 16_000)

            legacy = bulk_module.inspect_generated_speech(
                wav,
                analysis_version=bulk_module.LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION,
            )
            with self.assertRaises(bulk_module.SpeechSilenceValidationError) as raised:
                bulk_module.inspect_generated_speech(wav)

        self.assertEqual(legacy.analysis_version, 1)
        self.assertEqual(legacy.longest_internal_silence_seconds, 0.0)
        self.assertEqual(raised.exception.quality.analysis_version, 2)
        self.assertGreater(
            raised.exception.quality.longest_internal_silence_seconds, 1.2
        )
        self.assertIn("internal silence", raised.exception.failures[0])

    def test_silence_failure_persists_versioned_pause_spans_and_text_shape(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(text="What happened? You're hurt.")
            queue = write_queue(root / "queue.jsonl", [item])
            tone = audio_samples()
            pcm = np.concatenate(
                (
                    np.zeros(16_000, dtype=np.float32),
                    tone,
                    np.zeros(16_000 * 2, dtype=np.float32),
                    tone,
                    np.zeros(16_000, dtype=np.float32),
                )
            )

            result = self.run_generation(
                queue,
                root / "output",
                SyntheticRenderer(pcm=pcm),
                retries=0,
            )
            failed = load_generation_state(result.state, queue)["items"][
                item["queue_id"]
            ]
            tampered = json.loads(result.state.read_text(encoding="utf-8"))
            tampered["items"][item["queue_id"]]["failure"]["pause_diagnosis"][
                "attempt_binding"
            ]["seed"] = 999
            result.state.write_text(
                json.dumps(tampered, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(BulkGenerationError, "binding changed"):
                load_generation_state(result.state, queue)

        diagnosis = failed["failure"]["pause_diagnosis"]
        self.assertEqual(diagnosis["schema_version"], 1)
        self.assertEqual(diagnosis["analysis_version"], 2)
        self.assertEqual(
            diagnosis["classification"], "sentence_boundary_pause_candidate"
        )
        self.assertEqual(diagnosis["sentence_boundary_count"], 2)
        self.assertFalse(diagnosis["repairable_by_safe_segmentation"])
        self.assertEqual(
            diagnosis["attempt_binding"],
            {
                "provider": "synthetic",
                "model": "synthetic-v1",
                "generation_profile": "stable",
                "seed": 0,
                "synthesis_provenance_sha256": failed["synthesis_provenance_sha256"],
            },
        )
        self.assertEqual(
            [span["kind"] for span in diagnosis["spans"]],
            ["leading", "internal", "trailing"],
        )
        self.assertGreater(diagnosis["spans"][1]["duration_seconds"], 1.2)

    def test_inline_pause_repair_binds_derived_prompt_without_changing_queue_text(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(text="What happened? You're hurt.")
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            tone = audio_samples()
            failed_pcm = np.concatenate(
                (tone, np.zeros(16_000 * 2, dtype=np.float32), tone)
            )
            failed_renderer = SyntheticRenderer(
                pcm=failed_pcm, diagnostics_backend="moss-tts"
            )
            failed_renderer.name = "moss-tts"
            failed_renderer.model_name = "moss-local"
            run_bulk_generation(
                queue,
                output,
                failed_renderer,
                provider="moss-tts",
                model="moss-local",
                retries=0,
                seed=0,
            )
            plan = generation_failure_repair_plan(
                output / "generation-state.json", queue
            )
            observed_active = {}

            def inspect_active(_request):
                observed_active.update(
                    load_generation_state(output / "generation-state.json", queue)[
                        "active"
                    ]
                )

            repair_renderer = SyntheticRenderer(
                diagnostics_backend="moss-tts", inspect_state=inspect_active
            )
            repair_renderer.name = "moss-tts"
            repair_renderer.model_name = "moss-local"
            policy = FailureRepairPolicy(
                inline_pause_queue_ids=(item["queue_id"],),
                inline_pause_ms=180,
            )

            repaired = run_bulk_generation(
                queue,
                output,
                repair_renderer,
                provider="moss-tts",
                model="moss-local",
                retries=0,
                seed=0,
                include_queue_ids=(item["queue_id"],),
                failure_repair_policy=policy,
            )
            stored = load_generation_state(repaired.state, queue)["items"][
                item["queue_id"]
            ]

        self.assertEqual(
            repair_renderer.requests[0].text,
            "What happened? [pause 0.18s] You're hurt.",
        )
        self.assertEqual(plan["records"][0]["action"], "inline_pause_marker_comparison")
        self.assertIn("failure_repair", observed_active, observed_active)
        self.assertEqual(
            observed_active["failure_repair"]["derived_prompt_sha256"],
            stored["failure_repair"]["derived_prompt_sha256"],
        )
        self.assertEqual(stored["status"], "generated")
        self.assertEqual(stored["failure_repair"]["strategy"], "inline_pause_marker")
        self.assertEqual(stored["failure_repair"]["marker_count"], 1)
        self.assertEqual(stored["failure_repair"]["pause_ms"], 180)
        self.assertEqual(stored["text_sha256"], item["text_sha256"])
        self.assertEqual(
            stored["synthesis_text_sha256"],
            stored["failure_repair"]["derived_prompt_sha256"],
        )

    def test_inline_pause_repair_stops_after_three_provider_attempts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(text="What happened? You're hurt.")
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            tone = audio_samples()
            failed_pcm = np.concatenate(
                (tone, np.zeros(16_000 * 2, dtype=np.float32), tone)
            )

            def renderer():
                value = SyntheticRenderer(
                    pcm=failed_pcm, diagnostics_backend="moss-tts"
                )
                value.name = "moss-tts"
                value.model_name = "moss-local"
                return value

            run_bulk_generation(
                queue,
                output,
                renderer(),
                provider="moss-tts",
                model="moss-local",
                retries=0,
                seed=0,
            )
            policy = FailureRepairPolicy(
                inline_pause_queue_ids=(item["queue_id"],), inline_pause_ms=180
            )
            first = renderer()
            run_bulk_generation(
                queue,
                output,
                first,
                provider="moss-tts",
                model="moss-local",
                retries=0,
                seed=0,
                include_queue_ids=(item["queue_id"],),
                failure_repair_policy=policy,
            )
            second = renderer()
            result = run_bulk_generation(
                queue,
                output,
                second,
                provider="moss-tts",
                model="moss-local",
                retries=0,
                seed=0,
                include_queue_ids=(item["queue_id"],),
                failure_repair_policy=policy,
            )
            state = load_generation_state(result.state, queue)
            before = result.state.read_bytes()
            plan = generation_failure_repair_plan(result.state, queue)
            with self.assertRaisesRegex(BulkGenerationError, "no longer matches"):
                run_bulk_generation(
                    queue,
                    output,
                    renderer(),
                    provider="moss-tts",
                    model="moss-local",
                    retries=0,
                    seed=0,
                    include_queue_ids=(item["queue_id"],),
                    failure_repair_policy=policy,
                )
            after = result.state.read_bytes()

        stored = state["items"][item["queue_id"]]
        self.assertEqual([request.seed for request in first.requests], [1])
        self.assertEqual([request.seed for request in second.requests], [2])
        self.assertEqual(stored["attempts"], 3)
        self.assertEqual(stored["attempts_by_provider"], {"moss-tts": 3})
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["failure_repair"]["strategy"], "inline_pause_marker")
        self.assertEqual(plan["records"][0]["action"], "offline_fallback_backend")
        self.assertEqual(after, before)
        self.assertEqual(list((output / "audio").rglob("*.wav")), [])

    def test_current_state_records_v2_speech_quality_and_loads_legacy_metrics(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            result = self.run_generation(queue, root / "output", SyntheticRenderer())
            state = json.loads(result.state.read_text(encoding="utf-8"))
            generated = state["items"][item["queue_id"]]
            self.assertEqual(generated["speech_quality"]["analysis_version"], 2)

            audio = result.state.parent / generated["path"]
            legacy = bulk_module.inspect_generated_speech(
                audio,
                analysis_version=bulk_module.LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION,
            )
            generated["speech_quality"] = {
                "silence_ratio": legacy.silence_ratio,
                "leading_silence_seconds": legacy.leading_silence_seconds,
                "trailing_silence_seconds": legacy.trailing_silence_seconds,
                "longest_internal_silence_seconds": (
                    legacy.longest_internal_silence_seconds
                ),
            }
            result.state.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            loaded = load_generation_state(result.state, queue)

        self.assertNotIn(
            "analysis_version",
            loaded["items"][item["queue_id"]]["speech_quality"],
        )

    def test_exact_failed_sentence_repair_renders_segments_with_distinct_seeds(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(
                text="The gate is already open. We should leave before dawn."
            )
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            self.run_generation(
                queue,
                output,
                SyntheticRenderer([SynthesisCompletion.LIMITED]),
                retries=0,
                seed=0,
            )
            renderer = SyntheticRenderer()
            policy = FailureRepairPolicy((item["queue_id"],))

            repaired = self.run_generation(
                queue,
                output,
                renderer,
                retries=0,
                seed=0,
                include_queue_ids=[item["queue_id"]],
                failure_repair_policy=policy,
            )
            state = load_generation_state(repaired.state, queue)
            result = state["items"][item["queue_id"]]
            review_generation_item(repaired.state, item["queue_id"], "approved")
            manifest = json.loads(repaired.manifest.read_text(encoding="utf-8"))

        self.assertEqual(repaired.generated, 1)
        self.assertEqual(
            [request.text for request in renderer.requests],
            ["The gate is already open.", "We should leave before dawn."],
        )
        self.assertEqual([request.seed for request in renderer.requests], [1, 2])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["seed"], 1)
        self.assertEqual(
            result["failure_repair"]["strategy"],
            "sentence_boundary_segmentation",
        )
        self.assertEqual(result["failure_repair"]["pause_ms"], 180)
        self.assertEqual(result["failure_repair"]["planned_segment_seeds"], [1, 2])
        self.assertEqual(
            manifest["entries"][0]["failure_repair"], result["failure_repair"]
        )

    def test_failed_sentence_repair_transitions_and_cannot_repeat(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(
                text="The gate is already open. We should leave before dawn."
            )
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            self.run_generation(
                queue,
                output,
                SyntheticRenderer(
                    [SynthesisCompletion.LIMITED, SynthesisCompletion.LIMITED]
                ),
                retries=1,
                seed=0,
            )
            policy = FailureRepairPolicy((item["queue_id"],))
            repair_renderer = SyntheticRenderer([SynthesisCompletion.LIMITED])
            failed = self.run_generation(
                queue,
                output,
                repair_renderer,
                retries=0,
                seed=0,
                include_queue_ids=[item["queue_id"]],
                failure_repair_policy=policy,
            )
            state = load_generation_state(failed.state, queue)
            stored = state["items"][item["queue_id"]]
            report = generation_failure_report(failed.state, queue)
            plan = generation_failure_repair_plan(failed.state, queue)
            before = failed.state.read_bytes()
            repeated_renderer = SyntheticRenderer()

            with self.assertRaisesRegex(
                BulkGenerationError, "Sentence repair already failed"
            ):
                self.run_generation(
                    queue,
                    output,
                    repeated_renderer,
                    retries=0,
                    seed=0,
                    include_queue_ids=[item["queue_id"]],
                    failure_repair_policy=policy,
                )
            after = failed.state.read_bytes()
            published_wavs = list((output / "audio").rglob("*.wav"))

        self.assertEqual(failed.generated, 0)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(
            stored["failure_repair"]["strategy"],
            "sentence_boundary_segmentation",
        )
        self.assertEqual(
            report["records"][0]["failure_repair"], stored["failure_repair"]
        )
        self.assertEqual(
            plan["records"][0]["attempted_repair_strategy"],
            "sentence_boundary_segmentation",
        )
        self.assertEqual(plan["records"][0]["action"], "offline_fallback_backend")
        self.assertEqual(after, before)
        self.assertEqual(repeated_renderer.requests, [])
        self.assertEqual(published_wavs, [])

    def test_failed_sentence_repair_keeps_last_bounded_provider_attempt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(
                text="The gate is already open. We should leave before dawn."
            )
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            self.run_generation(
                queue,
                output,
                SyntheticRenderer([SynthesisCompletion.LIMITED]),
                retries=0,
                seed=0,
            )
            policy = FailureRepairPolicy((item["queue_id"],))
            repaired = self.run_generation(
                queue,
                output,
                SyntheticRenderer([SynthesisCompletion.LIMITED]),
                retries=0,
                seed=0,
                include_queue_ids=[item["queue_id"]],
                failure_repair_policy=policy,
            )
            state = load_generation_state(repaired.state, queue)
            stored = state["items"][item["queue_id"]]
            before_plan = generation_failure_repair_plan(repaired.state, queue)
            bounded_policy = FailureRepairPolicy(
                bounded_seed_retry_queue_ids=(item["queue_id"],)
            )
            exhausted = self.run_generation(
                queue,
                output,
                SyntheticRenderer([SynthesisCompletion.LIMITED]),
                retries=0,
                seed=0,
                include_queue_ids=[item["queue_id"]],
                failure_repair_policy=bounded_policy,
            )
            exhausted_item = load_generation_state(exhausted.state, queue)["items"][
                item["queue_id"]
            ]
            exhausted_plan = generation_failure_repair_plan(exhausted.state, queue)

        self.assertEqual(stored["attempts_by_provider"], {"synthetic": 2})
        self.assertEqual(
            stored["failure_repair"]["strategy"],
            "sentence_boundary_segmentation",
        )
        self.assertEqual(before_plan["records"][0]["action"], "bounded_seed_retry")
        self.assertEqual(exhausted_item["attempts_by_provider"], {"synthetic": 3})
        self.assertEqual(
            exhausted_item["failure_repair"]["strategy"], "bounded_seed_retry"
        )
        self.assertEqual(
            exhausted_plan["records"][0]["attempted_repair_strategy"],
            "bounded_seed_retry",
        )
        self.assertEqual(
            exhausted_plan["records"][0]["action"], "offline_fallback_backend"
        )

    def test_failed_silent_sentence_repair_requires_reference_comparison(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(
                text="The first warning is clear. The second warning is equally clear."
            )
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            tone = audio_samples()
            failed_pcm = np.concatenate(
                (tone, np.zeros(16_000 * 2, dtype=np.float32), tone)
            )
            self.run_generation(
                queue,
                output,
                SyntheticRenderer(pcm=failed_pcm),
                retries=0,
                seed=0,
            )
            policy = FailureRepairPolicy((item["queue_id"],))
            repaired = self.run_generation(
                queue,
                output,
                SyntheticRenderer(pcm=failed_pcm),
                retries=0,
                seed=0,
                include_queue_ids=[item["queue_id"]],
                failure_repair_policy=policy,
            )
            state = load_generation_state(repaired.state, queue)
            stored = state["items"][item["queue_id"]]
            plan = generation_failure_repair_plan(repaired.state, queue)

        self.assertEqual(repaired.generated, 0)
        self.assertEqual(stored["failure"]["kind"], "speech_silence")
        self.assertEqual(
            stored["failure_repair"]["strategy"],
            "sentence_boundary_segmentation",
        )
        self.assertEqual(plan["records"][0]["action"], "reference_comparison")

    def test_nested_repair_history_prevents_sentence_segmentation_cycle(self):
        result = {
            "failure_repair": {
                "schema_version": 1,
                "strategy": "bounded_seed_retry",
            },
            "carry_forward": {
                "source_repair_strategy": "sentence_boundary_segmentation",
                "source_parent_carry_forward": {
                    "source_repair_strategy": "bounded_seed_retry"
                },
            },
        }
        history = bulk_module._failure_repair_history(result)
        report = {
            "state": "/tmp/state.json",
            "state_sha256": "a" * 64,
            "queue": "/tmp/queue.jsonl",
            "queue_sha256": "b" * 64,
            "failure_count": 1,
            "records": [
                {
                    "queue_id": "game:1:hash",
                    "line_id": "game:1",
                    "speaker": "Narrator",
                    "text": "The first warning is clear. The second is clear.",
                    "requested_voice_character": "Narrator",
                    "synthesis_voice_character": "Narrator",
                    "provider": "moss-tts",
                    "model": "moss-local",
                    "generation_profile": "stable",
                    "synthesis_control_digest": "c" * 64,
                    "attempts": 3,
                    "attempts_by_provider": {"moss-tts": 3},
                    "seed": 2,
                    "last_error": "speech silence",
                    "failure": {
                        "kind": "speech_silence",
                        "speech_quality": {
                            "leading_silence_seconds": 0.0,
                            "trailing_silence_seconds": 0.0,
                            "longest_internal_silence_seconds": 3.0,
                        },
                        "text_features": {"sentence_boundary_count": 2},
                    },
                    "failure_repair": result["failure_repair"],
                    "failure_repair_history": history,
                }
            ],
        }
        with patch.object(
            bulk_module, "generation_failure_report", return_value=report
        ):
            plan = generation_failure_repair_plan("state.json", "queue.jsonl")

        self.assertEqual(
            history,
            ["bounded_seed_retry", "sentence_boundary_segmentation"],
        )
        self.assertEqual(plan["records"][0]["action"], "reference_comparison")
        self.assertIn("earlier sentence-boundary repair", plan["records"][0]["reason"])

    def test_internal_silence_failure_repairs_only_at_safe_sentence_boundaries(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item(
                text="The first warning is clear. The second warning is equally clear."
            )
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            tone = audio_samples()
            failed_pcm = np.concatenate(
                (tone, np.zeros(16_000 * 2, dtype=np.float32), tone)
            )
            failed = self.run_generation(
                queue,
                output,
                SyntheticRenderer(pcm=failed_pcm),
                retries=0,
                seed=0,
            )
            failed_state = load_generation_state(failed.state, queue)
            failed_item = failed_state["items"][item["queue_id"]]
            repair_plan = generation_failure_repair_plan(failed.state, queue)
            renderer = SyntheticRenderer()
            policy = FailureRepairPolicy((item["queue_id"],))

            repaired = self.run_generation(
                queue,
                output,
                renderer,
                retries=0,
                seed=0,
                include_queue_ids=[item["queue_id"]],
                failure_repair_policy=policy,
            )
            repaired_item = load_generation_state(repaired.state, queue)["items"][
                item["queue_id"]
            ]

        self.assertEqual(failed_item["failure"]["kind"], "speech_silence")
        self.assertGreater(
            failed_item["failure"]["speech_quality"][
                "longest_internal_silence_seconds"
            ],
            1.2,
        )
        self.assertEqual(
            repair_plan["records"][0]["action"],
            "sentence_boundary_segmentation",
        )
        self.assertEqual(repaired.generated, 1)
        self.assertEqual(
            [request.text for request in renderer.requests],
            ["The first warning is clear.", "The second warning is equally clear."],
        )
        self.assertEqual([request.seed for request in renderer.requests], [1, 2])
        self.assertEqual(
            repaired_item["failure_repair"]["strategy"],
            "sentence_boundary_segmentation",
        )
        self.assertLessEqual(
            repaired_item["speech_quality"]["longest_internal_silence_seconds"],
            1.2,
        )

    def test_exact_edge_silence_repair_trims_before_quality_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            pcm = np.concatenate(
                (np.zeros(16_000 * 2, dtype=np.float32), audio_samples())
            )
            self.run_generation(
                queue,
                output,
                SyntheticRenderer(pcm=pcm),
                retries=0,
            )
            policy = FailureRepairPolicy((), (item["queue_id"],))

            repaired = self.run_generation(
                queue,
                output,
                SyntheticRenderer(pcm=pcm),
                retries=0,
                include_queue_ids=[item["queue_id"]],
                failure_repair_policy=policy,
            )
            state = load_generation_state(repaired.state, queue)
            result = state["items"][item["queue_id"]]

        self.assertEqual(repaired.generated, 1)
        self.assertGreater(result["failure_repair"]["leading_trimmed_samples"], 0)
        self.assertLessEqual(result["speech_quality"]["leading_silence_seconds"], 0.08)

    def test_repair_policy_requires_exact_current_failed_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            policy = FailureRepairPolicy((item["queue_id"],))

            with self.assertRaisesRegex(BulkGenerationError, "exact --queue-id"):
                self.run_generation(
                    queue,
                    output,
                    SyntheticRenderer(),
                    failure_repair_policy=policy,
                )

        self.assertFalse(output.exists())

    def test_bounded_seed_repair_never_exceeds_three_total_attempts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            item = queue_item()
            queue = write_queue(root / "queue.jsonl", [item])
            output = root / "output"
            self.run_generation(
                queue,
                output,
                SyntheticRenderer([SynthesisCompletion.LIMITED]),
                retries=0,
                seed=0,
            )
            renderer = SyntheticRenderer(
                [SynthesisCompletion.LIMITED, SynthesisCompletion.LIMITED]
            )
            policy = FailureRepairPolicy(
                bounded_seed_retry_queue_ids=(item["queue_id"],)
            )

            repaired = self.run_generation(
                queue,
                output,
                renderer,
                retries=20,
                seed=0,
                include_queue_ids=[item["queue_id"]],
                failure_repair_policy=policy,
            )
            state = load_generation_state(repaired.state, queue)
            result = state["items"][item["queue_id"]]
            before = repaired.state.read_bytes()
            with self.assertRaisesRegex(BulkGenerationError, "no longer matches"):
                self.run_generation(
                    queue,
                    output,
                    SyntheticRenderer(),
                    retries=0,
                    seed=0,
                    include_queue_ids=[item["queue_id"]],
                    failure_repair_policy=policy,
                )
            after = repaired.state.read_bytes()
            wavs = list((output / "audio").rglob("*.wav"))

        self.assertEqual([request.seed for request in renderer.requests], [1, 2])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["seed"], 2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_repair"]["strategy"], "bounded_seed_retry")
        self.assertEqual(before, after)
        self.assertEqual(wavs, [])

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
            mixed = queue_item("mixed-sfx")
            mixed["text"] = "N-No! *gurgle*"
            mixed["text_sha256"] = hashlib.sha256(mixed["text"].encode()).hexdigest()
            mixed["queue_id"] = f"line:mixed-sfx:{mixed['text_sha256'][:16]}"
            interjection = queue_item("interjection")
            interjection["text"] = "Tsk!"
            interjection["text_sha256"] = hashlib.sha256(b"Tsk!").hexdigest()
            interjection["queue_id"] = (
                f"line:interjection:{interjection['text_sha256'][:16]}"
            )
            queue = write_queue(
                root / "queue.jsonl", [spoken, sfx, mixed, interjection]
            )
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
        self.assertEqual(result.skipped_items, 3)
        self.assertEqual(renderer.requests[0].text, "Wait.")
        self.assertEqual(generated["text_transform"], "short-trailing-ellipsis-v1")
        self.assertEqual(
            generated["synthesis_text_sha256"],
            hashlib.sha256(b"Wait.").hexdigest(),
        )

    def test_mixed_audio_event_spoken_projection_is_exact_and_tamper_evident(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mixed = queue_item("mixed-sigh")
            mixed["text"] = "I can't believe she'd ... *sigh*"
            mixed["text_sha256"] = hashlib.sha256(mixed["text"].encode()).hexdigest()
            mixed["queue_id"] = f"line:mixed-sigh:{mixed['text_sha256'][:16]}"
            queue = write_queue(root / "queue.jsonl", [mixed])
            renderer = SyntheticRenderer()

            result = self.run_generation(
                queue,
                root / "output",
                renderer,
                include_queue_ids=[mixed["queue_id"]],
                item_filter=lambda _item: True,
                text_transform=bulk_module.audio_event_spoken_projection,
                text_transform_id="audio-event-spoken-projection-v1",
                audio_event_spoken_projection_queue_ids=[mixed["queue_id"]],
            )
            state = load_generation_state(result.state, queue)
            generated = state["items"][mixed["queue_id"]]

            self.assertEqual(renderer.requests[0].text, "I can't believe she'd...")
            self.assertEqual(
                generated["text_transform"], "audio-event-spoken-projection-v1"
            )
            self.assertEqual(
                generated["synthesis_configuration"][
                    "audio_event_spoken_projection_queue_ids"
                ],
                [mixed["queue_id"]],
            )
            state["items"][mixed["queue_id"]]["synthesis_text_sha256"] = "0" * 64
            atomic_write_json(result.state, state, sort_keys=True)
            with self.assertRaisesRegex(
                BulkGenerationError, "audio-event spoken projection changed"
            ):
                load_generation_state(result.state, queue)

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
                    "vntts.authoring.cli_generation._load_stable_voice_registry",
                    return_value=(
                        registry,
                        sha256_file(voice_manifest),
                        {},
                        (),
                    ),
                ),
                patch(
                    "vntts.authoring.cli_generation.create_backend",
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

    def test_cli_persistent_cache_reuses_audio_across_independent_outputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(root / "queue.jsonl", [queue_item()])
            cache = root / "shared-cache"
            voice_manifest = root / "voices.json"
            voice_manifest.write_text("{}\n", encoding="utf-8")
            reference = root / "hero.wav"
            reference.write_bytes(b"synthetic reference")
            registry = CharacterVoiceRegistry(
                [CharacterVoice("Hero", "hero", references=(reference,))]
            )
            renderers = []

            def create_renderer(_name, _registry, cache_root, **_options):
                renderer = CacheAwareSyntheticRenderer(cache_root)
                renderers.append(renderer)
                return renderer

            with (
                patch(
                    "vntts.authoring.cli_generation._load_stable_voice_registry",
                    return_value=(registry, sha256_file(voice_manifest), {}, ()),
                ),
                patch(
                    "vntts.authoring.cli_generation.create_backend",
                    side_effect=create_renderer,
                ),
            ):
                for output_name in ("first-output", "second-output"):
                    with redirect_stdout(StringIO()):
                        self.assertEqual(
                            authoring_main(
                                [
                                    "generate",
                                    "--queue",
                                    str(queue),
                                    "--output",
                                    str(root / output_name),
                                    "--cache-directory",
                                    str(cache),
                                    "--voice-manifest",
                                    str(voice_manifest),
                                    "--backend",
                                    "pocket-tts",
                                    "--narrator-character",
                                    "Hero",
                                    "--retries",
                                    "0",
                                ]
                            ),
                            0,
                        )

            first_state = load_generation_state(
                root / "first-output" / "generation-state.json",
                queue,
            )
            second_state = load_generation_state(
                root / "second-output" / "generation-state.json",
                queue,
            )

        self.assertEqual([renderer.fresh_renders for renderer in renderers], [1, 0])
        self.assertEqual(len(first_state["items"]), 1)
        self.assertEqual(len(second_state["items"]), 1)
        self.assertIsNot(first_state, second_state)

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

    def test_cli_pocket_generation_accepts_allowlisted_embedded_voice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(root / "queue.jsonl", [queue_item()])
            voice_manifest = root / "voices.json"
            voice_manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Hero",
                                "speaker": "anna",
                                "aliases": [],
                                "references": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            renderer = SyntheticRenderer()
            options = []

            def create_renderer(name, _registry, _cache, **values):
                options.append(values)
                renderer.name = name
                renderer.model_name = name
                return renderer

            output = StringIO()
            with (
                patch(
                    "vntts.authoring.cli_generation.create_backend",
                    side_effect=create_renderer,
                ),
                redirect_stdout(output),
            ):
                exit_code = authoring_main(
                    [
                        "generate",
                        "--queue",
                        str(queue),
                        "--output",
                        str(root / "output"),
                        "--voice-manifest",
                        str(voice_manifest),
                        "--backend",
                        "pocket-tts",
                        "--retries",
                        "0",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["generated"], 1)
            self.assertEqual(options[0]["narrator_reference"], "alba")


if __name__ == "__main__":
    unittest.main()

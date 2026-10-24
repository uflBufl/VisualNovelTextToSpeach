import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import Mock, patch

import numpy as np
from vntts_artifacts.audio import PCM16_MONO_WAV_FORMAT, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import write_generated_audio_manifest
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.story_index import write_story_index_document

from tests.bulk_generation_fixtures import SyntheticRenderer, queue_item, write_queue
from tests.generation_state_fixtures import replace_generation_state
from tests.story_fixtures import write_story_index
from tests.symlink_support import symlink_or_skip
from vntts import generation_database, path_safety
from vntts.authoring.bulk_generation import (
    GenerationProgressEvent,
    GenerationProgressItem,
    _generation_progress_event,
    run_bulk_generation,
)
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_state import STATE_SCHEMA, STATE_VERSION
from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationError,
    OfflineGenerationResult,
    OfflineGenerationWorker,
    _cached_static_ready_line_ids,
    _static_ready_line_ids,
    _terminal_counts,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_setup import load_verified_story_index_document
from vntts.pregeneration_voices import VoicePlan
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class FinishedProcess:
    def __init__(self, returncode=0, *, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class RunningProcess(FinishedProcess):
    def __init__(self):
        super().__init__(None)

    def communicate(self, timeout=None):
        if self.terminated or self.killed:
            return "", ""
        raise subprocess.TimeoutExpired(("worker",), timeout)


class FailingProcess(RunningProcess):
    def communicate(self, timeout=None):
        raise OSError("captured output failed")


def generation_inputs(root, *, backend="pocket-tts", model=None):
    identity = "a" * 64
    directory = root / f"generation-input-{identity[:16]}"
    directory.mkdir(parents=True)
    queue = directory / "queue.jsonl"
    voices = directory / "voice-manifest.json"
    queue.write_text("queue", encoding="utf-8")
    voices.write_text("voices", encoding="utf-8")
    generation_input = PregenerationInput(
        identity=identity,
        directory=directory,
        story_index=directory / "story-index.jsonl",
        voice_manifest=voices,
        queue=queue,
        queue_sha256="b" * 64,
        queue_items=3,
        ready_items=3,
        narrator_fallback_roles=("Hotelier", "Poacher"),
        story_index_sha256="c" * 64,
        voice_manifest_sha256="d" * 64,
        source_audio_semantic_evidence_sha256=None,
    )
    plan = VoicePlan(
        job_id="c" * 24,
        created_at="2026-08-31T00:00:00+00:00",
        story_index_sha256="d" * 64,
        voice_manifest=None,
        voice_manifest_sha256=None,
        synthesis_backend=backend,
        synthesis_model=model,
        synthesis_language="en",
        synthesis_profile="stable",
        pocket_voice_cloning=False,
        synthesis_controls_sha256="e" * 64,
        groups=(),
    )
    return generation_input, plan


def cold_progress_inputs(root):
    inputs, _plan = generation_inputs(root)
    text = "Previously prepared dialogue."
    write_story_index_document(
        inputs.story_index,
        {"game": "Synthetic", "language": "en"},
        [
            {
                "record_type": "line",
                "line_id": "saved",
                "chapter": "1",
                "sequence": 1,
                "speaker": "Ada",
                "text": text,
                "kind": "dialogue",
                "source_audio_status": "absent",
                "speakable": True,
            }
        ],
    )
    inputs = replace(inputs, story_index_sha256=sha256_file(inputs.story_index))
    output = inputs.output_directory
    output.mkdir()
    audio = output / "line.wav"
    write_pcm16_wav(audio, np.linspace(-0.1, 0.1, 120, dtype=np.float32), 24_000)
    write_generated_audio_manifest(
        output / "live-progress-manifest.json",
        {"source_queue_sha256": inputs.queue_sha256, "vntts.runtime.progress": True},
        [
            {
                "line_id": "saved",
                "text_sha256": text_sha256(text),
                "audio": "line.wav",
                "audio_format": PCM16_MONO_WAV_FORMAT,
                "audio_sha256": sha256_file(audio),
                "sample_rate": 24_000,
                "sample_count": 120,
            }
        ],
    )
    state = {
        "schema": STATE_SCHEMA,
        "schema_version": STATE_VERSION,
        "queue_sha256": inputs.queue_sha256,
        "items": {
            "saved": {
                "status": "approved",
                "review_status": "approved",
                "line_id": "saved",
                "text_sha256": text_sha256(text),
            }
        },
        "active": None,
    }
    replace_generation_state(
        output / "generation-state.sqlite3",
        state,
    )
    return inputs, audio, state


class SessionBackend:
    def __init__(self, registry, **options):
        self.registry = registry
        self.narrator_reference = options.get("narrator_reference")
        self.startup_cancellation = options.get("startup_cancellation")
        self.startup_progress = options.get("startup_progress")
        self.process = RunningProcess()
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1
        self.process.terminate()


class OfflineGenerationSessionTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.generation_input, self.plan = generation_inputs(
            self.root, backend="moss-tts", model="model-id"
        )
        self.generation_input = replace(
            self.generation_input,
            voice_manifest_sha256=sha256_file(self.generation_input.voice_manifest),
        )
        self.registry = CharacterVoiceRegistry()
        self.backends = []
        self.factory_calls = []
        self.worker = OfflineGenerationWorker(
            backend_factory=self.create_backend, retain_isolated_backend=True
        )
        self.cpp = self.enterContext(
            patch(
                "vntts.pregeneration_generation.moss_cpp_requested", return_value=False
            )
        )

    def create_backend(self, name, registry, cache_root, **options):
        self.factory_calls.append((name, cache_root, options))
        backend = SessionBackend(registry, **options)
        self.backends.append(backend)
        return backend

    def lease(self, **options):
        return self.worker._backend_session.factory(
            "moss-tts", self.registry, self.root / "synthesis-cache", **options
        )

    def test_per_line_generate_and_repair_share_worker_until_session_exit(self):
        output = self.root / f"generation-output-{self.generation_input.identity[:16]}"
        result = OfflineGenerationResult(
            output,
            output / "generation-state.sqlite3",
            output / "manifest.json",
            3,
            0,
            0,
        )
        calls = []

        def run(parsed, **options):
            calls.append(tuple(parsed.queue_ids))
            backend = options["backend_factory"](
                parsed.backend,
                self.registry,
                parsed.cache_directory,
                model_name=parsed.model,
                startup_cancellation=options["cancellation"],
                startup_progress=options["startup_progress"],
            )
            backend.shutdown()
            self.assertEqual(self.backends[0].shutdown_calls, 0)

        with (
            patch("vntts.authoring.cli_generation.run_generation", side_effect=run),
            patch(
                "vntts.pregeneration_generation.load_generation_state",
                return_value={"items": {}},
            ),
            patch("vntts.pregeneration_generation._load_result", return_value=result),
            patch("vntts.pregeneration_generation._ensure_remaining_disk_space"),
            self.worker.generation_session(self.generation_input, self.plan),
        ):
            self.worker.generate(self.generation_input, self.plan, queue_ids=("a",))
            self.worker.repair(
                self.generation_input,
                self.plan,
                result,
                action="safe_resume",
                queue_ids=("a",),
            )
            self.worker.generate(self.generation_input, self.plan, queue_ids=("b",))
            self.assertEqual(len(self.backends), 1)
        self.assertEqual(calls, [("a",), ("a",), ("b",)])
        self.assertEqual(self.backends[0].shutdown_calls, 1)
        self.assertEqual(
            self.factory_calls[0][1], (self.root / "synthesis-cache").resolve()
        )
        self.assertIsNone(self.worker._backend_session)

    def test_session_reuses_with_fresh_cancel_and_progress_then_closes_each_job(self):
        first_cancel, second_cancel = Event(), Event()
        first_progress, second_progress = Mock(), Mock()
        with self.worker.generation_session(self.generation_input, self.plan):
            first = self.lease(
                startup_cancellation=first_cancel, startup_progress=first_progress
            )
            second = self.lease(
                startup_cancellation=second_cancel, startup_progress=second_progress
            )
            self.assertIs(first._backend, second._backend)
            self.assertIs(second.startup_cancellation, second_cancel)
            self.assertIs(second.startup_progress, second_progress)
        with self.worker.generation_session(self.generation_input, self.plan):
            self.lease()
        self.assertEqual(len(self.backends), 2)
        self.assertEqual([b.shutdown_calls for b in self.backends], [1, 1])

    def test_session_replaces_model_registry_and_dead_worker(self):
        with self.worker.generation_session(self.generation_input, self.plan):
            self.lease(model_name="first")
            self.lease(model_name="second")
            self.backends[-1].process.returncode = 1
            self.lease(model_name="second")
            self.registry = CharacterVoiceRegistry(
                [CharacterVoice("Narrator", "changed-voice")]
            )
            self.lease(model_name="second")
        self.assertEqual(len(self.backends), 4)
        self.assertEqual([b.shutdown_calls for b in self.backends], [1, 1, 1, 1])

    def test_session_rejects_changed_input_plan_cache_and_concurrent_access(self):
        errors = []
        with self.worker.generation_session(self.generation_input, self.plan):
            with self.assertRaisesRegex(OfflineGenerationError, "identity"):
                self.worker.generate(
                    replace(self.generation_input, identity="f" * 64), self.plan
                )
            with self.assertRaisesRegex(OfflineGenerationError, "identity"):
                self.worker.generate(
                    self.generation_input, replace(self.plan, synthesis_model="changed")
                )
            with self.assertRaisesRegex(OfflineGenerationError, "cache identity"):
                self.worker._backend_session.factory(
                    "moss-tts", self.registry, self.root / "other"
                )
            with self.assertRaisesRegex(OfflineGenerationError, "already active"):
                with self.worker.generation_session(self.generation_input, self.plan):
                    self.fail("nested session entered")

            def concurrent():
                try:
                    self.worker.generate(
                        self.generation_input, self.plan, queue_ids=("a",)
                    )
                except OfflineGenerationError as error:
                    errors.append(str(error))

            thread = Thread(target=concurrent)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["Offline generation session identity changed"])
        self.assertEqual(self.backends, [])

    def test_cancel_and_primary_failure_close_once_without_replacing_error(self):
        for failure in (
            OfflineGenerationCancelled("cancelled"),
            RuntimeError("render failed"),
        ):
            with self.subTest(failure=type(failure)):
                with self.assertRaises(type(failure)) as caught:
                    with self.worker.generation_session(
                        self.generation_input, self.plan
                    ):
                        backend = self.lease()._backend
                        original_shutdown = backend.shutdown

                        def broken_shutdown():
                            original_shutdown()
                            raise OSError("cleanup failed")

                        backend.shutdown = broken_shutdown
                        raise failure
                self.assertIs(caught.exception, failure)
                self.assertEqual(backend.shutdown_calls, 1)
                self.assertIsNone(self.worker._backend_session)

    def test_closed_session_factory_cannot_resurrect_worker(self):
        with self.worker.generation_session(self.generation_input, self.plan):
            self.lease()
            factory = self.worker._backend_session.factory
        with self.assertRaisesRegex(OfflineGenerationError, "session identity"):
            factory("moss-tts", self.registry, self.root / "synthesis-cache")
        self.assertEqual(len(self.backends), 1)

    def test_same_path_reference_mutation_rejects_before_reused_lease(self):
        reference = self.root / "reference.wav"
        reference.write_bytes(b"admitted reference")
        self.registry = CharacterVoiceRegistry(
            [CharacterVoice("Narrator", "voice", references=(reference,))]
        )
        with self.worker.generation_session(self.generation_input, self.plan):
            self.lease().shutdown()
            reference.write_bytes(b"changed reference")
            with self.assertRaisesRegex(BulkGenerationError, "changed during the run"):
                self.lease()
            self.assertEqual(len(self.backends), 1)
        with self.worker.generation_session(self.generation_input, self.plan):
            self.lease()
        self.assertEqual(len(self.backends), 2)
        self.assertEqual([b.shutdown_calls for b in self.backends], [1, 1])

    def test_changed_reference_inventory_is_not_admitted_into_retained_job(self):
        with self.worker.generation_session(self.generation_input, self.plan):
            self.lease()
            self.registry = CharacterVoiceRegistry(
                [
                    CharacterVoice(
                        "Narrator", "voice", references=(self.root / "other.wav",)
                    )
                ]
            )
            with self.assertRaisesRegex(OfflineGenerationError, "control inventory"):
                self.lease()
        self.assertEqual(len(self.backends), 1)

    def test_default_custom_factory_keeps_per_call_backend_ownership(self):
        custom = Mock(return_value=object())
        worker = OfflineGenerationWorker(backend_factory=custom)
        with worker.generation_session(self.generation_input, self.plan):
            self.assertIsNone(worker._backend_session)
            self.assertIs(worker.backend_factory, custom)
        custom.assert_not_called()

    def test_native_backend_keeps_existing_factory_and_per_call_shutdown(self):
        self.cpp.return_value = True
        with self.worker.generation_session(self.generation_input, self.plan):
            self.lease(model_name="native.gguf").shutdown()
            self.lease(model_name="native.gguf").shutdown()
        self.assertEqual(len(self.backends), 2)
        self.assertEqual([b.shutdown_calls for b in self.backends], [1, 1])


class OfflineGenerationWorkerTest(unittest.TestCase):
    def test_exact_scope_keeps_projection_flags_on_the_selected_dialogue(self):
        with TemporaryDirectory() as directory:
            generation_input, plan = generation_inputs(
                Path(directory), backend="moss-tts", model="model-id"
            )
            generation_input = replace(
                generation_input,
                audio_event_projection_queue_ids=("mixed-event",),
            )
            worker = OfflineGenerationWorker(command=("worker",))
            expected = Mock()

            with (
                patch.object(
                    worker, "inspect", side_effect=OfflineGenerationError
                ) as inspect,
                patch.object(worker, "_execute", return_value=expected) as execute,
            ):
                result = worker.generate(
                    generation_input,
                    plan,
                    queue_ids=("mixed-event",),
                )

        arguments = execute.call_args.args[0]
        self.assertIs(result, expected)
        inspect.assert_not_called()
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(arguments.count("--queue-id"), 1)
        self.assertIn("mixed-event", arguments)
        self.assertIn("--audio-event-spoken-projection", arguments)

    def test_cancelled_records_are_pending_in_progress_and_terminal_counts(self):
        with TemporaryDirectory() as directory:
            inputs, _plan = generation_inputs(Path(directory))
            state = {
                "queue_sha256": inputs.queue_sha256,
                "active": None,
                "items": {
                    "cancelled": {"status": "failed", "failure": {"kind": "cancelled"}},
                    "failure": {
                        "status": "failed",
                        "failure": {"kind": "backend_error"},
                    },
                    "ready": {"status": "approved", "review_status": "approved"},
                    "omitted": {"status": "omitted", "review_status": "omitted"},
                },
            }
            with patch(
                "vntts.pregeneration_generation._static_ready_line_ids", return_value=()
            ):
                worker = OfflineGenerationWorker(backend_factory=Mock())
                worker._record_progress(
                    _generation_progress_event(state, state["items"], None), inputs
                )
                progress = worker.inspect_progress(inputs)
        self.assertEqual(
            (progress.generated, progress.failed, progress.other_terminal), (1, 1, 1)
        )
        self.assertEqual(progress.completed, 3)
        self.assertEqual(_terminal_counts(state), (1, 1, 1))

    def test_safe_resume_restores_the_cancelled_repair_and_custom_pause(self):
        with TemporaryDirectory() as directory:
            inputs, plan = generation_inputs(
                Path(directory), backend="moss-tts", model="model-id"
            )
            output = inputs.output_directory
            result = OfflineGenerationResult(
                output,
                output / "generation-state.sqlite3",
                output / "manifest.json",
                0,
                0,
                0,
            )
            state = {
                "items": {
                    "one": {
                        "status": "failed",
                        "failure": {"kind": "cancelled"},
                        "failure_repair": {
                            "strategy": "sentence_boundary_segmentation",
                            "pause_ms": 250,
                        },
                    }
                }
            }
            worker = OfflineGenerationWorker(command=("worker",))
            with (
                patch(
                    "vntts.pregeneration_generation.load_generation_state",
                    return_value=state,
                ),
                patch.object(worker, "_execute", return_value=result) as execute,
            ):
                worker.repair(
                    inputs, plan, result, action="safe_resume", queue_ids=("one",)
                )
            arguments = execute.call_args.args[0]
            self.assertEqual(
                arguments[arguments.index("--sentence-segment-failed") + 1], "one"
            )
            self.assertEqual(
                arguments[arguments.index("--segment-pause-ms") + 1], "250"
            )
            self.assertEqual(arguments[arguments.index("--retries") + 1], "0")

    def test_progress_reports_generated_and_already_playable_story_lines(self):
        with TemporaryDirectory() as directory:
            inputs, _plan = generation_inputs(Path(directory))
            write_story_index_document(
                inputs.story_index,
                {
                    "game": "Synthetic",
                    "language": "en",
                    "source_audio_completion": "verified-media-duration-seconds",
                },
                [
                    {
                        "record_type": "line",
                        "line_id": "original",
                        "chapter": "1",
                        "sequence": 1,
                        "speaker": "Ada",
                        "text": "Already voiced.",
                        "kind": "dialogue",
                        "source_audio_status": "available",
                        "source_audio_completeness": "full",
                        "source_audio_completeness_reason": (
                            "exact-normalized-asr-transcript"
                        ),
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_duration_media_id": 7,
                        "source_audio_duration_media_sha256": "c" * 64,
                        "source_audio_duration_sample_rate": 24000,
                        "source_audio_duration_sample_count": 24000,
                        "source_audio_duration_decoder": "synthetic",
                        "source_media_ids": [7],
                        "available_media_ids": [7],
                        "speakable": True,
                    },
                    {
                        "record_type": "line",
                        "line_id": "generated",
                        "chapter": "1",
                        "sequence": 2,
                        "speaker": "Ada",
                        "text": "Prepared now.",
                        "kind": "dialogue",
                        "source_audio_status": "absent",
                        "speakable": True,
                    },
                ],
            )
            inputs = replace(inputs, story_index_sha256=sha256_file(inputs.story_index))
            output = inputs.directory.parent / (
                f"generation-output-{inputs.identity[:16]}"
            )
            output.mkdir()
            replace_generation_state(
                output / "generation-state.sqlite3",
                {
                    "schema": STATE_SCHEMA,
                    "schema_version": STATE_VERSION,
                    "queue_sha256": inputs.queue_sha256,
                    "items": {
                        "generated": {
                            "status": "generated",
                            "review_status": "pending_review",
                            "line_id": "generated",
                            "text_sha256": text_sha256("Prepared now."),
                        }
                    },
                    "active": None,
                },
            )

            audio = output / "generated.wav"
            write_pcm16_wav(audio, [0.1, -0.1] * 60, 24000)
            write_generated_audio_manifest(
                output / "live-progress-manifest.json",
                {
                    "source_queue_sha256": inputs.queue_sha256,
                    "vntts.runtime.progress": True,
                },
                [
                    {
                        "line_id": "generated",
                        "text_sha256": text_sha256("Prepared now."),
                        "audio": audio.name,
                        "audio_format": PCM16_MONO_WAV_FORMAT,
                        "audio_sha256": sha256_file(audio),
                        "sample_rate": 24000,
                        "sample_count": 120,
                    }
                ],
            )

            with patch(
                "vntts.pregeneration_generation._validated_source_audio_line_ids",
                return_value=frozenset({"original"}),
            ):
                progress = OfflineGenerationWorker().inspect_progress(inputs)

        self.assertEqual(progress.ready_line_ids, ("generated", "original"))

    def test_progress_refreshes_static_ready_lines_after_story_reimport(self):
        with TemporaryDirectory() as directory:
            inputs, _plan = generation_inputs(Path(directory))
            worker = OfflineGenerationWorker()
            for revision, line_id in (("c", "first"), ("d", "second")):
                write_story_index_document(
                    inputs.story_index,
                    {"game": "Synthetic", "language": "en"},
                    [
                        {
                            "record_type": "line",
                            "line_id": line_id,
                            "chapter": "1",
                            "sequence": 1,
                            "speaker": "Ada",
                            "text": "Silent cue.",
                            "kind": "dialogue",
                            "speakable": False,
                        }
                    ],
                )
                current = replace(
                    inputs,
                    identity=revision * 64,
                    story_index_sha256=sha256_file(inputs.story_index),
                )
                output = current.directory.parent / (
                    f"generation-output-{current.identity[:16]}"
                )
                output.mkdir()
                replace_generation_state(
                    output / "generation-state.sqlite3",
                    {
                        "schema": STATE_SCHEMA,
                        "schema_version": STATE_VERSION,
                        "queue_sha256": current.queue_sha256,
                        "items": {},
                    },
                )
                self.assertEqual(
                    worker.inspect_progress(current).ready_line_ids, (line_id,)
                )

    def test_static_ready_lines_require_the_bound_story_snapshot(self):
        with TemporaryDirectory() as directory:
            story = Path(directory) / "story-index.jsonl"
            metadata = {"game": "Synthetic", "language": "en"}
            records = [
                {
                    "record_type": "line",
                    "line_id": "silent",
                    "chapter": "1",
                    "sequence": 1,
                    "speaker": "Ada",
                    "text": "A silent cue.",
                    "kind": "dialogue",
                    "speakable": False,
                }
            ]
            write_story_index_document(story, metadata, records)
            expected_sha256 = sha256_file(story)
            _cached_static_ready_line_ids.cache_clear()
            self.addCleanup(_cached_static_ready_line_ids.cache_clear)
            self.assertEqual(
                _static_ready_line_ids(story, expected_sha256), ("silent",)
            )

            changed = [dict(records[0], text="Changed silent cue.")]
            write_story_index_document(story, metadata, changed)
            self.assertEqual(_static_ready_line_ids(story, expected_sha256), ())

            write_story_index_document(story, metadata, records)
            _cached_static_ready_line_ids.cache_clear()
            with patch(
                "vntts.pregeneration_generation.load_verified_story_index_document",
                wraps=load_verified_story_index_document,
            ) as load_verified:
                self.assertEqual(
                    _static_ready_line_ids(story, expected_sha256), ("silent",)
                )
                self.assertEqual(
                    _static_ready_line_ids(story, expected_sha256), ("silent",)
                )
            self.assertEqual(load_verified.call_count, 1)

    def test_static_ready_cache_rechecks_source_evidence_and_current_story(self):
        with TemporaryDirectory() as directory:
            story = write_story_index(Path(directory))
            checksum = sha256_file(story)
            evidence = story.parent / "source-audio-semantic-evidence.json"
            payload = evidence.read_bytes()
            story_payload = story.read_bytes()
            _cached_static_ready_line_ids.cache_clear()
            self.addCleanup(_cached_static_ready_line_ids.cache_clear)
            self.assertEqual(
                _static_ready_line_ids(story, checksum), ("reverse1999:1",)
            )
            evidence.unlink()
            self.assertEqual(_static_ready_line_ids(story, checksum), ())
            evidence.write_bytes(b"broken evidence")
            self.assertEqual(_static_ready_line_ids(story, checksum), ())
            evidence.write_bytes(payload)
            self.assertEqual(
                _static_ready_line_ids(story, checksum), ("reverse1999:1",)
            )
            story.write_bytes(story_payload + b"changed story")
            self.assertEqual(_static_ready_line_ids(story, checksum), ())
            story.unlink()
            self.assertEqual(_static_ready_line_ids(story, checksum), ())
            story.write_bytes(story_payload)
            self.assertEqual(
                _static_ready_line_ids(story, checksum), ("reverse1999:1",)
            )

    def test_evidence_change_during_readiness_does_not_poison_a_valid_cache_key(self):
        with TemporaryDirectory() as directory:
            story = write_story_index(Path(directory))
            checksum = sha256_file(story)
            evidence = story.parent / "source-audio-semantic-evidence.json"
            payload = evidence.read_bytes()
            from vntts.chapter_voice_preload import _source_audio_evidence_payload

            captures = 0

            def capture_then_remove(path):
                nonlocal captures
                captures += 1
                captured = _source_audio_evidence_payload(path)
                if captures == 1:
                    evidence.unlink()
                return captured

            _cached_static_ready_line_ids.cache_clear()
            self.addCleanup(_cached_static_ready_line_ids.cache_clear)
            with patch(
                "vntts.pregeneration_generation._source_audio_evidence_payload",
                side_effect=capture_then_remove,
            ):
                self.assertEqual(_static_ready_line_ids(story, checksum), ())
            evidence.write_bytes(payload)
            self.assertEqual(
                _static_ready_line_ids(story, checksum), ("reverse1999:1",)
            )

    def test_missing_progress_is_not_a_report_of_zero_completed_work(self):
        with TemporaryDirectory() as directory:
            inputs, _plan = generation_inputs(Path(directory))
            self.assertFalse(
                OfflineGenerationWorker().inspect_progress(inputs).available
            )

    def test_in_process_startup_progress_is_visible_before_generation_state(self):
        with TemporaryDirectory() as directory:
            inputs, _plan = generation_inputs(Path(directory))
            worker = OfflineGenerationWorker(backend_factory=Mock())
            worker._in_process_active = True
            worker._set_startup_status("Downloading OpenMOSS: 12%")

            progress = worker.inspect_progress(inputs)

        self.assertFalse(progress.available)
        self.assertEqual(progress.runtime_status, "Downloading OpenMOSS: 12%")

    def test_cold_startup_preserves_admitted_recordings_and_startup_status(self):
        with TemporaryDirectory() as directory:
            inputs, _audio, _state = cold_progress_inputs(Path(directory))
            worker = OfflineGenerationWorker(backend_factory=Mock())
            worker._set_startup_status("Loading model...")
            progress = worker.inspect_progress(inputs)

        self.assertTrue(progress.available)
        self.assertEqual(progress.generated, 1)
        self.assertEqual(progress.ready_line_ids, ("saved",))
        self.assertEqual(progress.runtime_status, "Loading model...")
        self.assertIsNone(progress.active_phase)

    def test_cold_progress_does_not_admit_missing_corrupt_or_foreign_audio(self):
        for failure in (
            "missing",
            "corrupt",
            "foreign-manifest",
            "malformed-manifest",
            "changed-story",
        ):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                inputs, audio, _state = cold_progress_inputs(Path(directory))
                manifest = inputs.output_directory / "live-progress-manifest.json"
                if failure == "missing":
                    audio.unlink()
                elif failure == "corrupt":
                    audio.write_bytes(b"modified WAV")
                elif failure == "foreign-manifest":
                    value = json.loads(manifest.read_text())
                    value["source_queue_sha256"] = "f" * 64
                    manifest.write_text(json.dumps(value))
                elif failure == "malformed-manifest":
                    manifest.write_text("not JSON")
                else:
                    inputs.story_index.write_text("changed story")
                progress = OfflineGenerationWorker(
                    backend_factory=Mock()
                ).inspect_progress(inputs)
                self.assertEqual(progress.generated, 1)
                self.assertEqual(progress.ready_line_ids, ())

    def test_invalid_sql_authority_refuses_models_and_retained_sessions_before_startup(
        self,
    ):
        for damage in ("corrupt", "future-database", "future-state", "foreign-queue"):
            with self.subTest(damage=damage), TemporaryDirectory() as directory:
                root = Path(directory)
                inputs, audio, state = cold_progress_inputs(root)
                _other, plan = generation_inputs(root / "plan", backend="moss-tts")
                path = inputs.output_directory / "generation-state.sqlite3"
                if damage == "corrupt":
                    path.write_bytes(b"corrupt saved authority")
                elif damage == "future-database":
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute("PRAGMA user_version = 999")
                else:
                    state[
                        "schema_version" if damage == "future-state" else "queue_sha256"
                    ] = 999 if damage == "future-state" else "f" * 64
                    replace_generation_state(path, state)
                before, audio_before = path.read_bytes(), audio.read_bytes()
                backend, process = Mock(), Mock()
                worker = OfflineGenerationWorker(
                    backend_factory=backend,
                    popen_factory=process,
                    retain_isolated_backend=True,
                )
                with self.assertRaises(OfflineGenerationError):
                    worker.generate(inputs, plan)
                with (
                    self.assertRaises(OfflineGenerationError),
                    worker.generation_session(inputs, plan),
                ):
                    self.fail("Invalid SQL authority admitted a retained model session")
                backend.assert_not_called()
                process.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(audio.read_bytes(), audio_before)

    def test_cold_progress_rejects_foreign_or_malformed_state(self):
        for failure in ("foreign", "malformed"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                inputs, _audio, state = cold_progress_inputs(Path(directory))
                state["queue_sha256"] = "f" * 64
                path = inputs.output_directory / "generation-state.sqlite3"
                if failure == "malformed":
                    path.write_bytes(b"broken SQLite")
                else:
                    replace_generation_state(path, state)
                with self.assertRaises(OfflineGenerationError):
                    OfflineGenerationWorker(backend_factory=Mock()).inspect_progress(
                        inputs
                    )

    def test_cold_progress_keeps_authoritative_originals_without_generated_manifest(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs, _plan = generation_inputs(root)
            story = write_story_index(root / "source")
            inputs = replace(
                inputs, story_index=story, story_index_sha256=sha256_file(story)
            )
            output = inputs.output_directory
            output.mkdir()
            replace_generation_state(
                output / "generation-state.sqlite3",
                {
                    "schema": STATE_SCHEMA,
                    "schema_version": STATE_VERSION,
                    "queue_sha256": inputs.queue_sha256,
                    "items": {},
                    "active": None,
                },
            )
            progress = OfflineGenerationWorker(backend_factory=Mock()).inspect_progress(
                inputs
            )

        self.assertEqual(progress.generated, 0)
        self.assertEqual(progress.ready_line_ids, ("reverse1999:1",))

    def test_live_callback_overtakes_cold_inspection_without_stale_counts(self):
        with TemporaryDirectory() as directory:
            inputs, _audio, state = cold_progress_inputs(Path(directory))
            worker = OfflineGenerationWorker(backend_factory=Mock())
            state["items"]["second"] = {
                "status": "approved",
                "review_status": "approved",
                "line_id": "second",
            }

            def callback(_inputs, _progress):
                worker._record_progress(
                    _generation_progress_event(state, state["items"], None), inputs
                )
                return ("saved",)

            with patch(
                "vntts.pregeneration_generation._durable_ready_line_ids",
                side_effect=callback,
            ):
                progress = worker.inspect_progress(inputs)
            self.assertEqual(progress.generated, 2)
            self.assertEqual(progress.ready_line_ids, ("saved", "second"))
            foreign = replace(inputs, identity="f" * 64)
            self.assertFalse(worker.inspect_progress(foreign).available)

    def test_progress_reads_partial_durable_state_before_manifest_publication(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            generation_input, _plan = generation_inputs(root)
            output = root / f"generation-output-{generation_input.identity[:16]}"
            output.mkdir()
            replace_generation_state(
                output / "generation-state.sqlite3",
                {
                    "schema": STATE_SCHEMA,
                    "schema_version": STATE_VERSION,
                    "queue_sha256": generation_input.queue_sha256,
                    "items": {
                        "one": {
                            "status": "generated",
                            "review_status": "pending_review",
                        },
                        "two": {"status": "failed"},
                        "three": {
                            "status": "live_fallback",
                            "review_status": "live_fallback",
                        },
                    },
                    "active": {
                        "phase": "validating",
                        "runtime_status": "GPU: RTX 2070 SUPER; auxiliary: CPU",
                        "runtime_worker_pid": 12345,
                    },
                },
            )

            worker = OfflineGenerationWorker()
            progress = worker.inspect_progress(generation_input)
            self.assertIsNone(progress.runtime_status)
            worker._process = Mock(pid=12345)
            worker._process.poll.return_value = None
            self.assertIn(
                "RTX 2070 SUPER",
                worker.inspect_progress(generation_input).runtime_status,
            )
            worker._process.pid = 999
            self.assertIsNone(worker.inspect_progress(generation_input).runtime_status)
            worker._process.pid = 12345
            worker._process.poll.return_value = 0
            self.assertIsNone(worker.inspect_progress(generation_input).runtime_status)

        self.assertEqual(progress.completed, 3)
        self.assertEqual(progress.generated, 1)
        self.assertEqual(progress.failed, 1)
        self.assertEqual(progress.other_terminal, 1)
        self.assertEqual(progress.active_phase, "validating")

    def test_sqlite_progress_uses_indexed_projection_and_owned_runtime_status(self):
        with TemporaryDirectory() as directory:
            inputs, _audio, state = cold_progress_inputs(Path(directory))
            state["items"]["saved"].update(
                review_status="approved",
                text_sha256=text_sha256("Previously prepared dialogue."),
            )
            state["items"]["cancelled"] = {
                "status": "failed",
                "failure": {"kind": "cancelled", "attempt_counted": False},
            }
            state["items"]["failed"] = {"status": "failed"}
            state["active"] = {
                "phase": "validating",
                "generation_owner_pid": 12345,
                "runtime_status": "GPU: owned worker",
            }
            path = inputs.output_directory / "generation-state.sqlite3"
            generation_database.write_state(path, state)
            before = path.read_bytes()
            worker = OfflineGenerationWorker()
            with patch(
                "vntts.generation_database.read_state",
                side_effect=AssertionError("Progress loaded full state evidence"),
            ):
                progress = worker.inspect_progress(inputs)
                self.assertEqual((progress.generated, progress.failed), (1, 1))
                self.assertEqual(progress.ready_line_ids, ("saved",))
                self.assertEqual(progress.active_phase, "validating")
                self.assertIsNone(progress.runtime_status)
                worker._process = Mock(pid=12345)
                worker._process.poll.return_value = None
                self.assertEqual(
                    worker.inspect_progress(inputs).runtime_status, "GPU: owned worker"
                )
                worker._process.pid = 999
                self.assertIsNone(worker.inspect_progress(inputs).runtime_status)
                worker._process.pid = 12345
                worker._process.poll.return_value = 0
                self.assertIsNone(worker.inspect_progress(inputs).runtime_status)
                with self.assertRaises(OfflineGenerationError):
                    worker.inspect_progress(replace(inputs, queue_sha256="f" * 64))
            self.assertEqual(path.read_bytes(), before)

    def test_sqlite_cold_progress_keeps_manifest_admission_and_exact_text_identity(
        self,
    ):
        with TemporaryDirectory() as directory:
            inputs, audio, state = cold_progress_inputs(Path(directory))
            state["items"]["saved"].update(
                review_status="approved",
                text_sha256=text_sha256("Previously prepared dialogue."),
            )
            path = inputs.output_directory / "generation-state.sqlite3"
            generation_database.write_state(path, state)
            worker = OfflineGenerationWorker(backend_factory=Mock())
            self.assertEqual(worker.inspect_progress(inputs).ready_line_ids, ("saved",))
            state["items"]["saved"]["text_sha256"] = "f" * 64
            generation_database.write_state(path, state)
            self.assertEqual(worker.inspect_progress(inputs).ready_line_ids, ())
            state["items"]["saved"]["text_sha256"] = text_sha256(
                "Previously prepared dialogue."
            )
            generation_database.write_state(path, state)
            audio.write_bytes(b"corrupt audio")
            self.assertEqual(worker.inspect_progress(inputs).ready_line_ids, ())

    def test_runs_exact_private_inputs_and_reports_terminal_counts(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            generation_input, plan = generation_inputs(
                root, backend="moss-tts", model="model-id"
            )
            generation_input = replace(
                generation_input,
                audio_event_projection_queue_ids=("mixed-event",),
            )
            output = generation_input.directory.parent / (
                f"generation-output-{generation_input.identity[:16]}"
            )
            output.mkdir()
            replace_generation_state(
                output / "generation-state.sqlite3",
                {
                    "schema": STATE_SCHEMA,
                    "schema_version": STATE_VERSION,
                    "queue_sha256": generation_input.queue_sha256,
                    "items": {},
                },
            )
            (output / "manifest.json").write_text("{}", encoding="utf-8")
            popen = Mock(return_value=FinishedProcess())
            worker = OfflineGenerationWorker(
                command=("vntts-worker",), popen_factory=popen
            )
            state = {
                "items": {
                    "one": {"status": "generated", "review_status": "pending_review"},
                    "two": {"status": "failed"},
                    "three": {
                        "status": "live_fallback",
                        "review_status": "live_fallback",
                    },
                }
            }

            with (
                patch(
                    "vntts.pregeneration_generation.load_generation_state",
                    side_effect=[{"items": {}}, state, state],
                ),
            ):
                result = worker.generate(generation_input, plan)

        self.assertEqual(popen.call_count, 2)
        ordinary_arguments = popen.call_args_list[0].args[0]
        arguments = popen.call_args_list[1].args[0]
        self.assertNotIn("--audio-event-spoken-projection", ordinary_arguments)
        self.assertEqual(arguments[:2], ("vntts-worker", "generate"))
        self.assertIn(str(generation_input.queue), arguments)
        self.assertIn("model-id", arguments)
        self.assertIn("2", arguments)
        cache_option = arguments.index("--cache-directory")
        self.assertEqual(
            arguments[cache_option + 1],
            str(root / "synthesis-cache"),
        )
        self.assertEqual(arguments.count("--narrator-fallback-role"), 2)
        projection = arguments.index("--audio-event-spoken-projection")
        self.assertEqual(arguments[projection + 1], "mixed-event")
        self.assertEqual(result.generated, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.other_terminal, 1)
        self.assertEqual(result.total, 3)

    def test_completed_job_does_not_start_a_backend(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            generation_input, plan = generation_inputs(root)
            output = generation_input.directory.parent / (
                f"generation-output-{generation_input.identity[:16]}"
            )
            output.mkdir()
            replace_generation_state(
                output / "generation-state.sqlite3",
                {
                    "schema": STATE_SCHEMA,
                    "schema_version": STATE_VERSION,
                    "queue_sha256": generation_input.queue_sha256,
                    "items": {},
                },
            )
            (output / "manifest.json").write_text("{}", encoding="utf-8")
            popen = Mock()
            state = {
                "items": {
                    "one": {"status": "approved", "review_status": "approved"},
                    "two": {
                        "status": "live_fallback",
                        "review_status": "live_fallback",
                    },
                    "three": {"status": "failed"},
                }
            }

            with (
                patch(
                    "vntts.pregeneration_generation.load_generation_state",
                    return_value=state,
                ),
            ):
                result = OfflineGenerationWorker(
                    command=("worker",), popen_factory=popen
                ).generate(generation_input, plan)

        popen.assert_not_called()
        self.assertEqual(result.total, generation_input.ready_items)

    def test_omitted_audio_event_does_not_hide_unfinished_dialogue(self):
        with TemporaryDirectory() as directory:
            generation_input, plan = generation_inputs(Path(directory))
            generation_input = replace(
                generation_input,
                audio_event_omission_queue_ids=("pure-event",),
            )
            worker = OfflineGenerationWorker(command=("worker",))
            partial = OfflineGenerationResult(
                Path(directory), Path(directory), Path(directory), 2, 0, 1
            )
            completed = replace(partial, generated=3)
            generated = Mock()
            with (
                patch.object(worker, "inspect", return_value=partial) as inspect,
                patch.object(worker, "_execute", return_value=generated) as execute,
                patch("vntts.pregeneration_generation._ensure_remaining_disk_space"),
            ):
                self.assertIs(worker.generate(generation_input, plan), generated)
                inspect.return_value = completed
                self.assertIs(worker.generate(generation_input, plan), completed)

            execute.assert_called_once()

    def test_moss_generation_uses_the_in_process_retained_backend(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            generation_input, plan = generation_inputs(
                root, backend="moss-tts", model="model-id"
            )
            output = root / f"generation-output-{generation_input.identity[:16]}"
            expected = OfflineGenerationResult(
                output,
                output / "generation-state.sqlite3",
                output / "manifest.json",
                3,
                0,
                0,
            )
            backend_factory = Mock()
            popen = Mock()
            worker = OfflineGenerationWorker(
                command=("worker",),
                popen_factory=popen,
                backend_factory=backend_factory,
            )
            cancellation = Event()
            snapshots = []
            calls = 0
            open_text = path_safety.open_regular_text

            def reject_state_poll(path, *args, **kwargs):
                if Path(path).name == "generation-state.sqlite3":
                    raise AssertionError("disk poll")
                return open_text(path, *args, **kwargs)

            def generate_with_progress(_parsed, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    snapshots.append(worker.inspect_progress(generation_input))
                    return
                self.assertNotIn("progress_callback", kwargs)
                progress = kwargs["progress_observer"]
                state = {
                    "queue_sha256": generation_input.queue_sha256,
                    "items": {
                        "one": {
                            "status": "approved",
                            "review_status": "approved",
                            "line_id": "first",
                        }
                    },
                    "active": {
                        "phase": "publishing",
                        "runtime_status": "GPU: RTX 2070 SUPER",
                        "runtime_worker_pid": os.getpid(),
                    },
                }
                progress(_generation_progress_event(state, state["items"], None))
                snapshots.append(worker.inspect_progress(generation_input))
                state["items"]["two"] = {
                    "status": "approved",
                    "review_status": "approved",
                    "line_id": "second",
                }
                snapshots.append(worker.inspect_progress(generation_input))
                progress(_generation_progress_event(state, state["items"], ("two",)))
                snapshots.append(worker.inspect_progress(generation_input))

            with (
                patch(
                    "vntts.authoring.cli_generation.run_generation",
                    side_effect=generate_with_progress,
                ) as run_generation,
                patch(
                    "vntts.pregeneration_generation._load_result",
                    return_value=expected,
                ),
                patch.object(worker, "inspect", side_effect=OfflineGenerationError),
                patch(
                    "vntts.pregeneration_generation._static_ready_line_ids",
                    return_value=(),
                ),
                patch.object(path_safety, "open_regular_text", reject_state_poll),
            ):
                result = worker.generate(
                    generation_input, plan, cancel_event=cancellation
                )
                snapshots.append(worker.inspect_progress(generation_input))
                worker.generate(generation_input, plan, cancel_event=cancellation)

        popen.assert_not_called()
        self.assertIs(result, expected)
        self.assertIs(
            run_generation.call_args.kwargs["backend_factory"], backend_factory
        )
        self.assertIsNotNone(run_generation.call_args.kwargs["startup_progress"])
        self.assertIs(run_generation.call_args.kwargs["cancellation"], cancellation)
        self.assertEqual([value.generated for value in snapshots], [1, 1, 2, 2, 2])
        self.assertEqual(snapshots[0].active_phase, "publishing")
        self.assertIn("RTX 2070 SUPER", snapshots[0].runtime_status)

    def test_in_process_generation_uses_incremental_events_without_state_conversion(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs, plan = generation_inputs(root, backend="moss-tts", model="model-id")
            write_queue(inputs.queue, [queue_item(str(index)) for index in range(6)])
            inputs = replace(
                inputs,
                queue_sha256=sha256_file(inputs.queue),
                queue_items=6,
                ready_items=6,
            )
            worker = OfflineGenerationWorker(backend_factory=Mock())
            events = []
            snapshots = []

            def generate(_parsed, **options):
                self.assertNotIn("progress_callback", options)
                observer = options["progress_observer"]

                def observe(event):
                    events.append(event)
                    observer(event)
                    snapshots.append(worker.inspect_progress(inputs))

                return run_bulk_generation(
                    inputs.queue,
                    inputs.output_directory,
                    SyntheticRenderer(),
                    provider="synthetic",
                    model="synthetic-v1",
                    progress_observer=observe,
                )

            with (
                patch.object(worker, "inspect", side_effect=OfflineGenerationError),
                patch("vntts.pregeneration_generation._ensure_remaining_disk_space"),
                patch(
                    "vntts.authoring.cli_generation.run_generation",
                    side_effect=generate,
                ),
                patch("vntts.pregeneration_generation._load_result"),
                patch(
                    "vntts.pregeneration_generation._state_items",
                    side_effect=AssertionError("full state conversion"),
                ),
                patch("vntts.pregeneration_generation.sys.platform", "darwin"),
                patch(
                    "vntts.pregeneration_generation._static_ready_line_ids",
                    return_value=("source",),
                ) as static_ready,
            ):
                worker.generate(inputs, plan)

            static_ready.assert_called_once_with(
                inputs.story_index, inputs.story_index_sha256
            )
            self.assertTrue(events[0].reset)
            self.assertEqual(len(events), 1 + 4 * 6)
            self.assertEqual(sum(len(event.items) for event in events), 6)
            for previous, current, event in zip(
                snapshots[:-1], snapshots[1:], events[1:], strict=True
            ):
                if not event.items:
                    self.assertIs(previous.ready_line_ids, current.ready_line_ids)
            self.assertEqual(snapshots[-1].generated, 6)
            self.assertEqual(
                snapshots[-1].ready_line_ids,
                tuple(sorted(("source", *(f"line:{index}" for index in range(6))))),
            )

    def test_incremental_progress_retries_cancellation_duplicate_lines_and_reset(self):
        with TemporaryDirectory() as directory:
            inputs, _plan = generation_inputs(Path(directory))
            worker = OfflineGenerationWorker(backend_factory=Mock())
            queue_sha256 = inputs.queue_sha256
            with patch(
                "vntts.pregeneration_generation._static_ready_line_ids",
                return_value=("source",),
            ) as static_ready:
                worker._record_progress(
                    GenerationProgressEvent(
                        queue_sha256,
                        None,
                        (
                            GenerationProgressItem("one", "approved", "shared"),
                            GenerationProgressItem("two", "generated", "shared"),
                            GenerationProgressItem("failed", "failed", None),
                            GenerationProgressItem("cancelled", "cancelled", None),
                            GenerationProgressItem("omitted", "omitted", None),
                        ),
                        reset=True,
                    ),
                    inputs,
                )
                initial = worker.inspect_progress(inputs)
                self.assertEqual(
                    (initial.generated, initial.failed, initial.other_terminal),
                    (2, 1, 1),
                )
                worker._record_progress(
                    GenerationProgressEvent(
                        queue_sha256,
                        {"phase": "retrying"},
                        (GenerationProgressItem("one", "failed", None),),
                    ),
                    inputs,
                )
                retrying = worker.inspect_progress(inputs)
                self.assertEqual((retrying.generated, retrying.failed), (1, 2))
                self.assertEqual(retrying.ready_line_ids, ("shared", "source"))
                worker._record_progress(
                    GenerationProgressEvent(
                        queue_sha256,
                        None,
                        (GenerationProgressItem("two", "cancelled", None),),
                    ),
                    inputs,
                )
                cancelled = worker.inspect_progress(inputs)
                self.assertEqual((cancelled.generated, cancelled.failed), (0, 2))
                self.assertEqual(cancelled.ready_line_ids, ("source",))
                worker._record_progress(
                    GenerationProgressEvent(
                        queue_sha256,
                        None,
                        (GenerationProgressItem("failed", "approved", "repaired"),),
                    ),
                    inputs,
                )
                recovered = worker.inspect_progress(inputs)
                self.assertEqual((recovered.generated, recovered.failed), (1, 1))
                self.assertEqual(recovered.ready_line_ids, ("repaired", "source"))
                worker._record_progress(
                    GenerationProgressEvent(
                        queue_sha256,
                        None,
                        (GenerationProgressItem("cancelled", "cancelled", None),),
                        reset=True,
                    ),
                    inputs,
                )
                reset = worker.inspect_progress(inputs)
                self.assertEqual(reset.completed, 0)
                self.assertEqual(reset.ready_line_ids, ("source",))
            self.assertEqual(
                static_ready.call_count, 5 if sys.platform == "win32" else 2
            )

    def test_incremental_static_readiness_rechecks_source_changes_and_windows(self):
        for platform in ("darwin", "win32"):
            for changed in ("story-index.jsonl", "source-audio-semantic-evidence.json"):
                with (
                    self.subTest(platform=platform, changed=changed),
                    TemporaryDirectory() as directory,
                ):
                    inputs, _plan = generation_inputs(Path(directory))
                    inputs.story_index.write_bytes(b"original")
                    evidence = inputs.story_index.with_name(
                        "source-audio-semantic-evidence.json"
                    )
                    evidence.write_bytes(b"original")
                    target = inputs.story_index.with_name(changed)
                    original = target.stat()
                    worker = OfflineGenerationWorker(backend_factory=Mock())
                    with (
                        patch("vntts.pregeneration_generation.sys.platform", platform),
                        (
                            patch(
                                "vntts.pregeneration_generation._progress_readiness_stamp",
                                return_value=(),
                            )
                            if platform == "win32"
                            else nullcontext()
                        ),
                        patch(
                            "vntts.pregeneration_generation._static_ready_line_ids",
                            side_effect=[("source",), ()],
                        ) as static_ready,
                    ):
                        worker._record_progress(
                            GenerationProgressEvent(
                                inputs.queue_sha256, None, reset=True
                            ),
                            inputs,
                        )
                        self.assertEqual(
                            worker.inspect_progress(inputs).ready_line_ids, ("source",)
                        )
                        target.write_bytes(b"modified")
                        os.utime(
                            target, ns=(original.st_atime_ns, original.st_mtime_ns)
                        )
                        worker._record_progress(
                            GenerationProgressEvent(
                                inputs.queue_sha256, {"phase": "validating"}
                            ),
                            inputs,
                        )
                        self.assertEqual(
                            worker.inspect_progress(inputs).ready_line_ids, ()
                        )
                        self.assertEqual(static_ready.call_count, 2)

    def test_incremental_static_readiness_checks_symlink_targets(self):
        for filename in ("story-index.jsonl", "source-audio-semantic-evidence.json"):
            with self.subTest(filename=filename), TemporaryDirectory() as directory:
                inputs, _plan = generation_inputs(Path(directory))
                inputs.story_index.write_bytes(b"original")
                evidence = inputs.story_index.with_name(
                    "source-audio-semantic-evidence.json"
                )
                evidence.write_bytes(b"original")
                linked = inputs.story_index.with_name(filename)
                target = linked.with_name("target-" + filename)
                linked.replace(target)
                symlink_or_skip(linked, target)
                worker = OfflineGenerationWorker(backend_factory=Mock())
                with (
                    patch("vntts.pregeneration_generation.sys.platform", "darwin"),
                    patch(
                        "vntts.pregeneration_generation._static_ready_line_ids",
                        side_effect=[("source",), ()],
                    ) as static_ready,
                ):
                    worker._record_progress(
                        GenerationProgressEvent(inputs.queue_sha256, None, reset=True),
                        inputs,
                    )
                    target.write_bytes(b"modified")
                    worker._record_progress(
                        GenerationProgressEvent(
                            inputs.queue_sha256, {"phase": "validating"}
                        ),
                        inputs,
                    )
                    self.assertEqual(worker.inspect_progress(inputs).ready_line_ids, ())
                self.assertEqual(static_ready.call_count, 2)

    def test_incremental_static_readiness_rejects_sources_changed_during_check(self):
        with TemporaryDirectory() as directory:
            inputs, _plan = generation_inputs(Path(directory))
            inputs.story_index.write_bytes(b"original")
            worker = OfflineGenerationWorker(backend_factory=Mock())

            def changing_source(*_args):
                inputs.story_index.write_bytes(b"changed during check")
                return ("source",)

            with patch(
                "vntts.pregeneration_generation._static_ready_line_ids",
                side_effect=changing_source,
            ):
                worker._record_progress(
                    GenerationProgressEvent(inputs.queue_sha256, None, reset=True),
                    inputs,
                )
            self.assertEqual(worker.inspect_progress(inputs).ready_line_ids, ())

    def test_stalled_second_retry_progress_callback_is_captured_until_cancel_settles(
        self,
    ):
        from vntts.generation_stall import GenerationStallWatchdog

        entered = Event()
        release = Event()
        captured = Event()
        cancellation = Event()
        snapshots = []
        errors = []

        with TemporaryDirectory() as directory:
            inputs, plan = generation_inputs(
                Path(directory), backend="moss-tts", model="model-id"
            )
            worker = OfflineGenerationWorker(backend_factory=Mock())

            def progress_callback(_state, _inputs):
                entered.set()
                release.wait(3)

            def render(_parsed, **kwargs):
                kwargs["progress_observer"](
                    GenerationProgressEvent(
                        inputs.queue_sha256,
                        {"phase": "generating", "attempt": 2, "seed": 1},
                    )
                )

            def record(**details):
                snapshots.append(details)
                captured.set()

            def generate():
                try:
                    worker.generate(inputs, plan, cancellation)
                except BaseException as error:
                    errors.append(error)

            with (
                patch.object(worker, "inspect", side_effect=OfflineGenerationError),
                patch("vntts.pregeneration_generation._ensure_remaining_disk_space"),
                patch.object(worker, "_record_progress", side_effect=progress_callback),
                patch(
                    "vntts.authoring.cli_generation.run_generation", side_effect=render
                ),
                patch.object(GenerationStallWatchdog, "STALL_SECONDS", 0.02),
                patch.object(GenerationStallWatchdog, "CANCEL_SECONDS", 0.02),
                patch.object(GenerationStallWatchdog, "POLL_SECONDS", 0.005),
                patch(
                    "vntts.generation_stall.record_generation_stall", side_effect=record
                ),
            ):
                thread = Thread(target=generate)
                thread.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(captured.wait(2))
                    self.assertFalse(snapshots[-1]["cancellation_requested"])
                    captured.clear()
                    cancellation.set()
                    self.assertTrue(captured.wait(2))
                    self.assertTrue(snapshots[-1]["cancellation_requested"])
                finally:
                    release.set()
                    thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], OfflineGenerationCancelled)
            self.assertEqual(snapshots[0]["attempt"], 2)
            frames = next(t["frames"] for t in snapshots[0]["threads"] if t["worker"])
            self.assertIn("progress_callback", [f["function"] for f in frames])
            self.assertIn("_execute", [f["function"] for f in frames])

    def test_pocket_cloning_opt_in_reaches_isolated_worker(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, plan = generation_inputs(Path(temporary_directory))
            plan = replace(plan, pocket_voice_cloning=True)

            arguments = OfflineGenerationWorker(command=("worker",))._base_arguments(
                generation_input,
                plan,
                generation_input.directory.parent / "output",
            )

        self.assertIn("--allow-gated-model-access", arguments)

    def test_xtts_terms_acceptance_reaches_isolated_worker(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, plan = generation_inputs(
                Path(temporary_directory), backend="coqui-xtts"
            )
            plan = replace(plan, xtts_terms_accepted=True, synthesis_language="ru")
            arguments = OfflineGenerationWorker(command=("worker",))._base_arguments(
                generation_input,
                plan,
                generation_input.directory.parent / "output",
            )

        self.assertIn("--accept-xtts-terms", arguments)
        self.assertEqual(arguments[arguments.index("--language") + 1], "ru")

    def test_selection_jobs_share_one_content_addressed_synthesis_cache(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "jobs"
            first_input, first_plan = generation_inputs(root / ("a" * 24))
            second_input, second_plan = generation_inputs(root / ("b" * 24))
            worker = OfflineGenerationWorker(command=("worker",))

            first = worker._base_arguments(
                first_input,
                first_plan,
                first_input.directory.parent / "output",
            )
            second = worker._base_arguments(
                second_input,
                second_plan,
                second_input.directory.parent / "output",
            )

        first_cache = first[first.index("--cache-directory") + 1]
        second_cache = second[second.index("--cache-directory") + 1]
        self.assertEqual(first_cache, str(root / "synthesis-cache"))
        self.assertEqual(second_cache, first_cache)

    def test_pocket_generation_uses_one_unseeded_attempt(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, plan = generation_inputs(Path(temporary_directory))
            process = FinishedProcess(2, stderr="details\nmodel unavailable\n")
            popen = Mock(return_value=process)

            with self.assertRaisesRegex(OfflineGenerationError, "model unavailable"):
                OfflineGenerationWorker(
                    command=("worker",), popen_factory=popen
                ).generate(generation_input, plan)

        arguments = popen.call_args.args[0]
        retries = arguments.index("--retries")
        self.assertEqual(arguments[retries + 1], "0")
        profile = arguments.index("--generation-profile")
        self.assertEqual(arguments[profile + 1], "default")

    def test_repair_scopes_one_typed_batch_without_regenerating_successes(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            generation_input, plan = generation_inputs(
                root, backend="moss-tts", model="model-id"
            )
            generation_input = replace(
                generation_input,
                audio_event_projection_queue_ids=("two",),
            )
            output = generation_input.directory.parent / (
                f"generation-output-{generation_input.identity[:16]}"
            )
            output.mkdir()
            replace_generation_state(
                output / "generation-state.sqlite3",
                {
                    "schema": STATE_SCHEMA,
                    "schema_version": STATE_VERSION,
                    "queue_sha256": generation_input.queue_sha256,
                    "items": {},
                },
            )
            (output / "manifest.json").write_text("{}", encoding="utf-8")
            generation_result = OfflineGenerationResult(
                output,
                output / "generation-state.sqlite3",
                output / "manifest.json",
                1,
                2,
                0,
            )
            popen = Mock(return_value=FinishedProcess())
            worker = OfflineGenerationWorker(command=("worker",), popen_factory=popen)
            state = {
                "items": {
                    "one": {"status": "generated", "review_status": "pending_review"}
                }
            }

            with patch(
                "vntts.pregeneration_generation.load_generation_state",
                return_value=state,
            ):
                worker.repair(
                    generation_input,
                    plan,
                    generation_result,
                    action="sentence_boundary_segmentation",
                    queue_ids=("two", "one"),
                )

        self.assertEqual(popen.call_count, 2)
        ordinary, projection = (call.args[0] for call in popen.call_args_list)
        self.assertEqual(ordinary.count("--queue-id"), 1)
        self.assertNotIn("--audio-event-spoken-projection", ordinary)
        self.assertEqual(projection.count("--queue-id"), 1)
        self.assertIn("--audio-event-spoken-projection", projection)
        for arguments in (ordinary, projection):
            self.assertEqual(arguments.count("--sentence-segment-failed"), 1)
            self.assertEqual(arguments[arguments.index("--retries") + 1], "0")
            self.assertNotIn("--regenerate-existing", arguments)
            self.assertIn("--approve-validated-audio", arguments)

    def test_cancellation_terminates_only_the_owned_worker(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, plan = generation_inputs(Path(temporary_directory))
            process = RunningProcess()
            cancellation = Event()
            cancellation.set()

            with self.assertRaises(OfflineGenerationCancelled):
                OfflineGenerationWorker(
                    command=("worker",), popen_factory=Mock(return_value=process)
                ).generate(generation_input, plan, cancellation)

        self.assertFalse(process.terminated)
        self.assertFalse(process.killed)

    def test_offline_fallback_repair_uses_exact_pocket_option(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            generation_input, plan = generation_inputs(root)
            output = root / f"generation-output-{generation_input.identity[:16]}"
            output.mkdir()
            state = output / "generation-state.sqlite3"
            manifest = output / "manifest.json"
            replace_generation_state(
                state,
                {
                    "schema": STATE_SCHEMA,
                    "schema_version": STATE_VERSION,
                    "queue_sha256": generation_input.queue_sha256,
                    "items": {},
                },
            )
            manifest.write_text("{}", encoding="utf-8")
            result = OfflineGenerationResult(output, state, manifest, 1, 2, 0)
            popen = Mock(return_value=FinishedProcess())
            worker = OfflineGenerationWorker(command=("worker",), popen_factory=popen)

            with patch(
                "vntts.pregeneration_generation.load_generation_state",
                return_value={
                    "items": {
                        "a": {"status": "generated", "review_status": "pending_review"}
                    }
                },
            ):
                worker.repair(
                    generation_input,
                    plan,
                    result,
                    action="offline_fallback_backend",
                    queue_ids=("a", "b"),
                )

        arguments = popen.call_args.args[0]
        self.assertEqual(arguments.count("--offline-fallback-failed"), 2)
        self.assertEqual(arguments[arguments.index("--retries") + 1], "0")

    def test_running_cancellation_terminates_the_owned_worker(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, plan = generation_inputs(Path(temporary_directory))
            process = RunningProcess()
            cancellation = Mock()
            cancellation.is_set.side_effect = (False, True)

            with self.assertRaises(OfflineGenerationCancelled) as raised:
                OfflineGenerationWorker(
                    command=("worker",), popen_factory=Mock(return_value=process)
                ).generate(generation_input, plan, cancellation)

        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_communicate_failure_terminates_the_owned_worker(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, plan = generation_inputs(Path(temporary_directory))
            process = FailingProcess()

            with self.assertRaisesRegex(OSError, "captured output failed"):
                OfflineGenerationWorker(
                    command=("worker",), popen_factory=Mock(return_value=process)
                ).generate(generation_input, plan)

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)

    def test_frozen_app_uses_hidden_generation_worker(self):
        worker = OfflineGenerationWorker()

        with patch.object(sys, "frozen", True, create=True):
            command = worker.command()

        self.assertEqual(command, (sys.executable, "--offline-generation-worker"))

    def test_source_app_uses_generation_only_worker(self):
        self.assertEqual(
            OfflineGenerationWorker().command(),
            (sys.executable, "-m", "vntts.authoring.cli_generation"),
        )


if __name__ == "__main__":
    unittest.main()

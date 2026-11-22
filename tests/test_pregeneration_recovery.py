import os
import stat
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

from tests.generation_state_fixtures import replace_generation_state
from tests.pregeneration_fixtures import InProcessPocketGenerator, fixture
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from vntts.authoring import generation_state
from vntts.authoring.generation_failures import failure_record
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.speech_quality import SpeechQuality, SpeechSilenceValidationError
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.generation_database import measure_generation_phase, measure_generation_run
from vntts.pregeneration_contract import PlannedLiveFallback
from vntts.pregeneration_generation import (
    OfflineGenerationError,
    OfflineGenerationResult,
    OfflineGenerationWorker,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_recovery import (
    OfflineRecoveryBatch,
    OfflineRecoveryError,
    OfflineRecoveryPlan,
    OfflineRecoveryResult,
    OfflineRecoveryWorker,
    _ordered_generation_queue_ids,
    _terminalize_exhausted_failures,
    plan_automatic_recovery,
)
from vntts.pregeneration_voices import VoicePlan
from vntts.support import PerformanceLog


def inputs(root):
    identity = "a" * 64
    directory = root / f"generation-input-{identity[:16]}"
    directory.mkdir()
    queue = directory / "queue.jsonl"
    queue.write_text("queue", encoding="utf-8")
    generation_input = PregenerationInput(
        identity,
        directory,
        directory / "story-index.jsonl",
        directory / "voice-manifest.json",
        queue,
        "b" * 64,
        3,
        3,
        (),
        "c" * 64,
        "d" * 64,
        None,
    )
    output = root / f"generation-output-{identity[:16]}"
    result = OfflineGenerationResult(
        output,
        output / "generation-state.sqlite3",
        1,
        2,
        0,
    )
    voice_plan = VoicePlan(
        job_id="c" * 24,
        created_at="2026-08-31T00:00:00+00:00",
        story_index_sha256="d" * 64,
        voice_manifest=None,
        voice_manifest_sha256=None,
        synthesis_backend="moss-tts",
        synthesis_model="model-id",
        synthesis_language="en",
        synthesis_profile="stable",
        pocket_voice_cloning=False,
        synthesis_controls_sha256="e" * 64,
        groups=(),
    )
    return generation_input, result, voice_plan


class OfflineRecoveryPlanTest(unittest.TestCase):
    def test_terminal_fallback_refreshes_in_memory_progress_after_commit(self):
        with TemporaryDirectory() as temporary_directory:
            with patch("tests.pregeneration_fixtures.authorize_live_fallback"):
                _job, generation_input, _result, items = fixture(
                    Path(temporary_directory)
                )
            generator = OfflineGenerationWorker(backend_factory=Mock())
            first = generator.inspect(generation_input)
            generator.refresh_progress(generation_input)
            before = generator.inspect_progress(generation_input)
            self.assertEqual(
                (before.generated, before.failed, before.other_terminal), (1, 1, 0)
            )

            plan_root = Path(temporary_directory) / "plan"
            plan_root.mkdir()
            _input, _result, voice_plan = inputs(plan_root)
            plan = plan_automatic_recovery(generation_input, voice_plan, first)
            stale = replace(plan.live_fallbacks[0], source_result_sha256="0" * 64)
            with self.assertRaisesRegex(OfflineRecoveryError, "no longer matches"):
                _terminalize_exhausted_failures(
                    generation_input, first, (stale,), None, generator=generator
                )
            result = _terminalize_exhausted_failures(
                generation_input,
                first,
                plan.live_fallbacks,
                None,
                generator=generator,
            )

            progress = generator.inspect_progress(generation_input)
            self.assertEqual(
                (progress.generated, progress.failed, progress.other_terminal),
                (1, 0, 1),
            )
            self.assertEqual(progress.ready_line_ids, (items[0]["line_id"],))
            self.assertEqual(result, generator.inspect(generation_input))
            state = generation_state.load_generation_state(
                result.state, generation_input.queue
            )
            self.assertEqual(
                state["items"][items[1]["queue_id"]]["status"], "live_fallback"
            )

    def test_recovery_order_ignores_queue_replacement_after_capture(self):
        with TemporaryDirectory() as directory:
            _job, generation_input, _result, items = fixture(Path(directory))
            generation_input = replace(
                generation_input, narrator_fallback_roles=("Narrator",)
            )
            recovery_root = Path(directory) / "recovery-fixture"
            recovery_root.mkdir()
            _unused, _unused_result, voice_plan = inputs(recovery_root)
            original_read = generation_state.read_regular_bytes
            replaced = []

            def read_then_replace(path):
                payload = original_read(path)
                if path == generation_input.queue:
                    replaced.append(path)
                    write_voice_generation_queue(
                        path,
                        {"game": "Synthetic Game", "language": "en"},
                        [
                            {
                                **items[0],
                                "speaker": "Other",
                                "voice_character": "Other",
                            },
                            items[1],
                        ],
                    )
                return payload

            with patch.object(
                generation_state, "read_regular_bytes", read_then_replace
            ):
                queue_ids, _index = _ordered_generation_queue_ids(
                    generation_input, voice_plan
                )

            self.assertEqual(replaced, [generation_input.queue])
            self.assertNotEqual(
                sha256_file(generation_input.queue), generation_input.queue_sha256
            )
            self.assertEqual(queue_ids, tuple(item["queue_id"] for item in items))

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_recovery_plan_queue_capture_refuses_fifo_and_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _job, generation_input, result, _items = fixture(root)
            recovery_root = root / "recovery-fixture"
            recovery_root.mkdir()
            _unused, _unused_result, voice_plan = inputs(recovery_root)
            queue = generation_input.queue
            queue.chmod(0o444)
            mode = stat.S_IMODE(queue.stat().st_mode)
            before_paths = set(root.rglob("*"))
            before = {
                path: path.read_bytes() for path in root.rglob("*") if path.is_file()
            }
            expected = plan_automatic_recovery(generation_input, voice_plan, result)
            original_read = generation_state.read_regular_bytes
            refused = []

            def refuse_at_capture(path):
                self.assertEqual(path, queue)
                gate: RegularFileGate = {"path": queue, "armed": True}
                with (
                    swap_at_acquisition(self, gate),
                    self.assertRaisesRegex(OSError, "regular file") as caught,
                ):
                    original_read(path)
                refused.append(caught.exception)
                raise caught.exception

            with (
                patch.object(generation_state, "read_regular_bytes", refuse_at_capture),
                self.assertRaisesRegex(
                    OfflineRecoveryError,
                    "Unable to inspect offline generation failures",
                ) as caught,
            ):
                plan_automatic_recovery(generation_input, voice_plan, result)

            self.assertEqual(len(refused), 1)
            self.assertIsInstance(caught.exception.__cause__, BulkGenerationError)
            self.assertIs(caught.exception.__cause__.__cause__, refused[0])
            self.assertEqual(str(caught.exception.__cause__), str(refused[0]))
            self.assertEqual(stat.S_IMODE(queue.stat().st_mode), mode)
            self.assertEqual(set(root.rglob("*")), before_paths)
            self.assertEqual(
                {path: path.read_bytes() for path in root.rglob("*") if path.is_file()},
                before,
            )
            self.assertEqual(
                plan_automatic_recovery(generation_input, voice_plan, result), expected
            )
            self.assertEqual(
                {path: path.read_bytes() for path in root.rglob("*") if path.is_file()},
                before,
            )
            self.assertEqual(set(root.rglob("*")), before_paths)

    def test_recovery_rejects_a_changed_voice_manifest_before_loading_the_queue(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, _result, voice_plan = inputs(Path(temporary_directory))
            manifest = generation_input.voice_manifest
            manifest.write_text("original voices", encoding="utf-8")
            generation_input = replace(
                generation_input,
                queue_sha256=sha256_file(generation_input.queue),
                voice_manifest_sha256=sha256_file(manifest),
            )
            manifest.write_text("changed voices", encoding="utf-8")

            with self.assertRaisesRegex(OfflineRecoveryError, "voice manifest changed"):
                _ordered_generation_queue_ids(generation_input, voice_plan)

    def test_groups_only_safe_actions_and_defers_ambiguous_work(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, result, _voice_plan = inputs(Path(temporary_directory))
            document = {
                "state_sha256": "1" * 64,
                "queue_sha256": "2" * 64,
                "failure_count": 4,
                "records": [
                    {
                        "queue_id": "d",
                        "action": "reference_comparison",
                        "source_result_sha256": "f" * 64,
                        "provider": "moss-tts",
                        "failure_kind": "speech_silence",
                    },
                    {
                        "queue_id": "b",
                        "action": "edge_silence_trim",
                        "source_result_sha256": "f" * 64,
                        "provider": "moss-tts",
                        "failure_kind": "speech_silence",
                    },
                    {
                        "queue_id": "c",
                        "action": "bounded_seed_retry",
                        "source_result_sha256": "f" * 64,
                        "provider": "moss-tts",
                        "failure_kind": "missed_eos_audio_limit",
                    },
                    {
                        "queue_id": "a",
                        "action": "edge_silence_trim",
                        "source_result_sha256": "f" * 64,
                        "provider": "moss-tts",
                        "failure_kind": "speech_silence",
                    },
                ],
            }

            with patch(
                "vntts.pregeneration_recovery.generation_failure_repair_plan",
                return_value=document,
            ):
                plan = plan_automatic_recovery(generation_input, _voice_plan, result)

        self.assertEqual(
            plan.automatic_batches,
            (
                OfflineRecoveryBatch("edge_silence_trim", ("a", "b")),
                OfflineRecoveryBatch("bounded_seed_retry", ("c",)),
            ),
        )
        self.assertEqual(plan.deferred_action_counts, (("reference_comparison", 1),))
        self.assertEqual(
            plan.deferred_batches,
            (OfflineRecoveryBatch("reference_comparison", ("d",)),),
        )
        self.assertEqual(tuple(d.queue_id for d in plan.live_fallbacks), ("d",))

    def test_unknown_actions_and_providerless_failures_remain_deferred(self):
        with TemporaryDirectory() as directory:
            generation_input, result, voice_plan = inputs(Path(directory))
            document = {
                "state_sha256": "1" * 64,
                "queue_sha256": "2" * 64,
                "failure_count": 3,
                "records": [
                    {
                        "queue_id": "a",
                        "action": "reference_comparison",
                        "source_result_sha256": "f" * 64,
                        "provider": None,
                        "failure_kind": "speech_silence",
                    },
                    {
                        "queue_id": "b",
                        "action": "future_action",
                        "source_result_sha256": "f" * 64,
                        "provider": "moss-tts",
                        "failure_kind": "backend_error",
                    },
                    {
                        "queue_id": "c",
                        "action": "safe_resume",
                        "source_result_sha256": "f" * 64,
                        "provider": None,
                        "failure_kind": "cancelled",
                    },
                ],
            }
            with patch(
                "vntts.pregeneration_recovery.generation_failure_repair_plan",
                return_value=document,
            ):
                plan = plan_automatic_recovery(generation_input, voice_plan, result)
        self.assertEqual(
            plan.automatic_batches, (OfflineRecoveryBatch("safe_resume", ("c",)),)
        )
        self.assertEqual(
            plan.deferred_action_counts,
            (("future_action", 1), ("reference_comparison", 1)),
        )
        self.assertEqual(
            plan.deferred_batches,
            (
                OfflineRecoveryBatch("future_action", ("b",)),
                OfflineRecoveryBatch("reference_comparison", ("a",)),
            ),
        )
        self.assertEqual(tuple(d.queue_id for d in plan.live_fallbacks), ())

    def test_pocket_runs_safe_repairs_before_deferring_real_failures(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, result, voice_plan = inputs(Path(temporary_directory))
            voice_plan = replace(
                voice_plan,
                synthesis_backend="pocket-tts",
                synthesis_model="pocket-tts",
            )
            document = {
                "state_sha256": "1" * 64,
                "queue_sha256": "2" * 64,
                "failure_count": 5,
                "records": [
                    {
                        "queue_id": "b",
                        "action": "bounded_seed_retry",
                        "source_result_sha256": "f" * 64,
                        "provider": "pocket-tts",
                        "failure_kind": "missed_eos_audio_limit",
                    },
                    {
                        "queue_id": "c",
                        "action": "offline_fallback_backend",
                        "source_result_sha256": "f" * 64,
                        "provider": "pocket-tts",
                        "failure_kind": "missed_eos_audio_limit",
                    },
                    {
                        "queue_id": "a",
                        "action": "safe_resume",
                        "source_result_sha256": "f" * 64,
                        "provider": "pocket-tts",
                        "failure_kind": "cancelled",
                    },
                    {
                        "queue_id": "d",
                        "action": "sentence_boundary_segmentation",
                        "source_result_sha256": "f" * 64,
                        "provider": "pocket-tts",
                        "failure_kind": "speech_silence",
                    },
                    {
                        "queue_id": "e",
                        "action": "edge_silence_trim",
                        "source_result_sha256": "f" * 64,
                        "provider": "pocket-tts",
                        "failure_kind": "speech_silence",
                    },
                ],
            }

            with patch(
                "vntts.pregeneration_recovery.generation_failure_repair_plan",
                return_value=document,
            ):
                plan = plan_automatic_recovery(generation_input, voice_plan, result)

        self.assertEqual(
            plan.automatic_batches,
            (
                OfflineRecoveryBatch("safe_resume", ("a",)),
                OfflineRecoveryBatch("sentence_boundary_segmentation", ("d",)),
                OfflineRecoveryBatch("edge_silence_trim", ("e",)),
            ),
        )
        self.assertEqual(
            plan.deferred_action_counts,
            (("bounded_seed_retry", 1), ("offline_fallback_backend", 1)),
        )
        self.assertEqual(tuple(d.queue_id for d in plan.live_fallbacks), ("b", "c"))


class OfflineRecoveryWorkerTest(unittest.TestCase):
    def test_terminal_silence_falls_back_and_generation_continues_for_any_engine(self):
        for provider in ("qwen-tts", "future-tts"):
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                root = Path(directory)
                _job, generation_input, saved, items = fixture(root)
                generation_input = replace(
                    generation_input, narrator_fallback_roles=("Narrator",)
                )
                failed_id, next_id = (item["queue_id"] for item in items)
                state = generation_state.load_generation_state(
                    saved.state, generation_input.queue
                )
                error = SpeechSilenceValidationError(
                    SpeechQuality(
                        silence_ratio=0.7,
                        leading_silence_seconds=0.0,
                        trailing_silence_seconds=0.0,
                        longest_internal_silence_seconds=2.0,
                    ),
                    ("internal silence exceeded the limit",),
                )
                failure = {
                    "status": "failed",
                    "line_id": items[0]["line_id"],
                    "text_sha256": items[0]["text_sha256"],
                    "speaker": "Narrator",
                    "requested_voice_character": "Narrator",
                    "voice_character": "Narrator",
                    "attempts": 3,
                    "attempts_by_provider": {provider: 3},
                    "seed": 0,
                    "seed_applied": True,
                    "provider": provider,
                    "model": "fixture-model",
                    "generation_profile": "default",
                    "synthesis_provenance_sha256": "e" * 64,
                    "last_error": str(error),
                    "failure": failure_record(error, text=items[0]["text"]),
                }
                state["items"] = {failed_id: failure}
                replace_generation_state(saved.state, state)
                plan_root = root / "plan"
                plan_root.mkdir()
                _unused, _unused_result, voice_plan = inputs(plan_root)
                voice_plan = replace(voice_plan, synthesis_backend="pocket-tts")
                generator = InProcessPocketGenerator()

                plan = plan_automatic_recovery(
                    generation_input, voice_plan, generator.inspect(generation_input)
                )
                self.assertEqual(plan.automatic_batches, ())
                self.assertEqual(
                    tuple(d.queue_id for d in plan.live_fallbacks), (failed_id,)
                )
                self.assertEqual(
                    plan.deferred_action_counts, (("reference_comparison", 1),)
                )

                result = OfflineRecoveryWorker(generator).generate_and_recover(
                    generation_input, voice_plan
                )

                persisted = generation_state.load_generation_state(
                    result.generation.state, generation_input.queue
                )["items"]
                fallback = persisted[failed_id]
                self.assertEqual(fallback["status"], "live_fallback")
                self.assertEqual(fallback["provider"], provider)
                self.assertEqual(fallback["failure"], failure["failure"])
                decision = fallback["live_fallback"]
                self.assertEqual(decision["reason"], "automatic_recovery_exhausted")
                self.assertEqual(decision["provider"], "pocket-tts")
                self.assertEqual(decision["evidence"]["base_result"], failure)
                self.assertEqual(persisted[next_id]["status"], "approved")
                self.assertEqual(generator.calls, 1)
                self.assertEqual(result.live_fallbacks, 1)
                self.assertEqual(result.remaining_failed, 0)
                self.assertEqual(result.generation.failed, 0)
                self.assertEqual(result.remaining_action_counts, ())
                library = GeneratedAudioLibrary.load_optional(result.generation.state)
                self.assertIsNotNone(library)
                self.assertIsNotNone(
                    library.find_live_fallback(
                        items[0]["line_id"], items[0]["text_sha256"]
                    )
                )
                self.assertIsNotNone(
                    library.find(items[1]["line_id"], items[1]["text_sha256"])
                )

    def test_successful_partial_generation_trusts_result_without_reopening_state(self):
        with TemporaryDirectory() as directory:
            generation_input, base, voice_plan = inputs(Path(directory))
            completed_line = replace(base, generated=1, failed=0)
            planner = Mock(side_effect=AssertionError("No repair is needed"))
            worker = OfflineRecoveryWorker(planner=planner)
            with patch(
                "vntts.pregeneration_recovery._generation_queue_statuses",
                side_effect=AssertionError(
                    "Successful results must not audit prior audio"
                ),
            ) as statuses:
                result = worker.recover(generation_input, voice_plan, completed_line)

        self.assertIs(result.generation, completed_line)
        statuses.assert_not_called()

    def test_engine_start_failure_stops_the_queue_before_line_recovery(self):
        with TemporaryDirectory() as directory:
            generation_input, _, voice_plan = inputs(Path(directory))
            generator = Mock()
            generator.inspect.side_effect = OfflineGenerationError("No saved state")
            startup_error = OfflineGenerationError("Model failed to load")
            generator.generate.side_effect = startup_error
            planner = Mock()
            worker = OfflineRecoveryWorker(generator, planner=planner)
            with (
                patch(
                    "vntts.pregeneration_recovery._ordered_generation_queue_ids",
                    return_value=(("first", "next"), {}),
                ),
                self.assertRaises(OfflineGenerationError) as caught,
            ):
                worker.generate_and_recover(generation_input, voice_plan)
            self.assertIs(caught.exception, startup_error)
            generator.generate.assert_called_once_with(
                generation_input, voice_plan, None, queue_ids=("first",)
            )
            planner.assert_not_called()
        planner.assert_not_called()

    def test_real_generation_scope_covers_per_line_recovery_and_priority(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, result, voice_plan = inputs(Path(temporary_directory))
            generator = OfflineGenerationWorker(
                backend_factory=Mock(), retain_isolated_backend=True
            )
            worker = OfflineRecoveryWorker(generator)
            scopes, events = [], []
            generated_results = iter(
                replace(result, generated=count, failed=3 - count) for count in range(3)
            )

            def generate(*_args, **options):
                scopes.append(generator._backend_session)
                events.append(("generate", options["queue_ids"][0]))
                return next(generated_results)

            def recover(_input, _plan, current, _cancel=None, *, queue_id=None):
                scopes.append(generator._backend_session)
                events.append(("recover", queue_id))
                if queue_id == "a":
                    worker.prioritize_line("priority", "hash")
                repaired = replace(
                    current, generated=current.generated + 1, failed=current.failed - 1
                )
                return OfflineRecoveryResult(repaired, 1, 1, 0, ())

            with (
                patch.object(generator, "inspect", side_effect=OfflineGenerationError),
                patch.object(generator, "generate", side_effect=generate),
                patch.object(worker, "recover", side_effect=recover),
                patch(
                    "vntts.pregeneration_recovery._ordered_generation_queue_ids",
                    return_value=(("a", "b", "c"), {("priority", "hash"): "c"}),
                ),
            ):
                recovered = worker.generate_and_recover(generation_input, voice_plan)
            self.assertEqual(
                recovered.generation, replace(result, generated=3, failed=0)
            )
            self.assertEqual(recovered.attempted_actions, 3)
            self.assertEqual(recovered.recovered, 3)
            self.assertIsNotNone(scopes[0])
            self.assertTrue(all(scope is scopes[0] for scope in scopes))
            self.assertTrue(scopes[0].closed)
            self.assertIsNone(generator._backend_session)
            self.assertEqual(
                events,
                [
                    ("generate", "a"),
                    ("recover", "a"),
                    ("generate", "c"),
                    ("recover", "c"),
                    ("generate", "b"),
                    ("recover", "b"),
                ],
            )

    def test_generation_scope_closes_when_recovery_raises(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, _result, voice_plan = inputs(Path(temporary_directory))
            generator = OfflineGenerationWorker(
                backend_factory=Mock(), retain_isolated_backend=True
            )
            worker = OfflineRecoveryWorker(generator)
            failure = OfflineRecoveryError("recovery failed")
            scopes = []

            def fail(*_args):
                scopes.append(generator._backend_session)
                raise failure

            with patch.object(worker, "_generate_and_recover", side_effect=fail):
                with self.assertRaises(OfflineRecoveryError) as caught:
                    worker.generate_and_recover(generation_input, voice_plan)
            self.assertIs(caught.exception, failure)
            self.assertTrue(scopes[0].closed)
            self.assertIsNone(generator._backend_session)

    def test_whole_recovery_flushes_nested_generation_measurements_once(self):
        log = PerformanceLog()
        worker = OfflineRecoveryWorker(Mock())
        sentinel = object()

        def recover(*_args):
            for _ in range(3):
                with measure_generation_run():
                    with measure_generation_phase("render-wait"):
                        pass
                self.assertEqual(log.snapshot(), [])
            return sentinel

        with (
            patch("vntts.support.performance_log", log),
            patch.object(worker, "_generate_and_recover", side_effect=recover),
        ):
            self.assertIs(worker.generate_and_recover(None, None), sentinel)
        events = {event["operation"]: event for event in log.snapshot()}
        self.assertEqual(len(log.snapshot()), 2)
        self.assertEqual(events["bulk-generation-render-wait"]["count"], 3)
        self.assertEqual(events["bulk-generation-total"]["count"], 1)
        self.assertEqual(len({event["operation_id"] for event in events.values()}), 1)

    def test_scoped_recovery_finishes_one_dialogue_without_touching_the_next(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first, voice_plan = inputs(Path(temporary_directory))
            repaired = replace(first, generated=2, failed=1)
            plans = iter(
                (
                    OfflineRecoveryPlan(
                        "1" * 64,
                        "2" * 64,
                        2,
                        (OfflineRecoveryBatch("edge_silence_trim", ("a", "b")),),
                        (),
                    ),
                    OfflineRecoveryPlan(
                        "3" * 64,
                        "2" * 64,
                        1,
                        (),
                        (("reference_comparison", 1),),
                        (OfflineRecoveryBatch("reference_comparison", ("b",)),),
                    ),
                )
            )
            generator = Mock()
            generator.repair.return_value = repaired

            result = OfflineRecoveryWorker(
                generator, planner=lambda *_arguments: next(plans)
            ).recover(
                generation_input,
                voice_plan,
                first,
                queue_id="a",
            )

        generator.repair.assert_called_once_with(
            generation_input,
            voice_plan,
            first,
            action="edge_silence_trim",
            queue_ids=("a",),
            cancel_event=None,
        )
        self.assertEqual(result.recovered, 1)
        self.assertEqual(result.remaining_failed, 0)

    def test_scoped_report_counts_only_remaining_failures_for_that_dialogue(self):
        for queue_id, expected_failed, expected_counts in (
            ("a", 0, ()),
            ("b", 1, (("edge_silence_trim", 1),)),
        ):
            with self.subTest(queue_id=queue_id), TemporaryDirectory() as directory:
                generation_input, first, voice_plan = inputs(Path(directory))
                repaired = replace(first, generated=2, failed=1)
                plans = iter(
                    (
                        OfflineRecoveryPlan(
                            "1" * 64,
                            "2" * 64,
                            2,
                            (OfflineRecoveryBatch("edge_silence_trim", ("a", "b")),),
                            (),
                        ),
                        OfflineRecoveryPlan(
                            "3" * 64,
                            "2" * 64,
                            1,
                            (OfflineRecoveryBatch("edge_silence_trim", ("b",)),),
                            (),
                        ),
                    )
                )
                generator = Mock()
                generator.repair.return_value = repaired
                result = OfflineRecoveryWorker(
                    generator, planner=lambda *_arguments: next(plans)
                ).recover(generation_input, voice_plan, first, queue_id=queue_id)

            self.assertEqual(result.remaining_failed, expected_failed)
            self.assertEqual(result.remaining_action_counts, expected_counts)
            self.assertEqual(result.recovered, 1 - expected_failed)
            generator.repair.assert_called_once()
            self.assertEqual(
                generator.repair.call_args.kwargs["queue_ids"], (queue_id,)
            )

    def test_generation_and_recovery_skip_completed_dialogues_on_resume(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, current, voice_plan = inputs(Path(temporary_directory))
            current = replace(current, generated=2, failed=0)
            generator = Mock()
            generator.inspect.return_value = current
            generator.generate.return_value = current
            worker = OfflineRecoveryWorker(generator)
            recovered = OfflineRecoveryResult(current, 0, 0, 0, ())

            with (
                patch(
                    "vntts.pregeneration_recovery._ordered_generation_queue_ids",
                    return_value=(("a", "b"), {}),
                ),
                patch(
                    "vntts.pregeneration_recovery._generation_queue_statuses",
                    return_value={"a": "generated"},
                ),
                patch.object(worker, "recover", return_value=recovered),
            ):
                worker.generate_and_recover(generation_input, voice_plan)

        self.assertEqual(
            generator.generate.call_args.kwargs["queue_ids"],
            ("b",),
        )
        self.assertEqual(generator.generate.call_count, 1)

    def test_resume_without_manifest_starts_only_the_missing_dialogue(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, final, voice_plan = inputs(Path(temporary_directory))
            final.state.parent.mkdir()
            final.state.touch()
            generator = Mock()
            generator.inspect.side_effect = OfflineGenerationError("No manifest")
            generator.generate.return_value = final
            worker = OfflineRecoveryWorker(generator)
            recovered = OfflineRecoveryResult(final, 0, 0, 0, ())
            with (
                patch(
                    "vntts.pregeneration_recovery._ordered_generation_queue_ids",
                    return_value=(("a", "b"), {}),
                ),
                patch(
                    "vntts.pregeneration_recovery.load_generation_state",
                    return_value={
                        "items": {"a": {"status": "approved"}},
                        "active": None,
                    },
                ),
                patch.object(worker, "recover", return_value=recovered),
            ):
                worker.generate_and_recover(generation_input, voice_plan)

        generator.generate.assert_called_once_with(
            generation_input, voice_plan, None, queue_ids=("b",)
        )

    def test_resume_all_terminal_without_manifest_needs_no_generator(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, final, voice_plan = inputs(Path(temporary_directory))
            final.state.parent.mkdir()
            final.state.touch()
            generator = Mock()
            generator.inspect.side_effect = OfflineGenerationError("No manifest")
            worker = OfflineRecoveryWorker(generator)
            with (
                patch(
                    "vntts.pregeneration_recovery._ordered_generation_queue_ids",
                    return_value=(("a", "b"), {}),
                ),
                patch(
                    "vntts.pregeneration_recovery.load_generation_state",
                    return_value={
                        "items": {
                            "a": {"status": "approved"},
                            "b": {"status": "live_fallback"},
                        },
                        "active": None,
                    },
                ),
            ):
                result = worker.generate_and_recover(generation_input, voice_plan)

        generator.generate.assert_not_called()
        self.assertEqual(result.generation.state, final.state)
        self.assertEqual(
            (result.generation.generated, result.generation.other_terminal), (1, 1)
        )

    def test_priority_changes_preserve_remaining_order_and_ignore_completed_lines(self):
        with TemporaryDirectory() as directory:
            generation_input, current, voice_plan = inputs(Path(directory))
            generator = Mock()
            generator.inspect.side_effect = OfflineGenerationError
            worker = OfflineRecoveryWorker(generator)
            worker.prioritize_line("line:b", "b" * 64)
            events = []

            def generate(*_arguments, **options):
                queue_id = options["queue_ids"][0]
                events.append(queue_id)
                if queue_id == "b":
                    worker.prioritize_line("line:c", "c" * 64)
                elif queue_id == "c":
                    worker.prioritize_line("line:b", "b" * 64)
                return current

            generator.generate.side_effect = generate
            with (
                patch(
                    "vntts.pregeneration_recovery._ordered_generation_queue_ids",
                    return_value=(
                        ("a", "b", "c", "d"),
                        {
                            ("line:b", "b" * 64): "b",
                            ("line:c", "c" * 64): "c",
                        },
                    ),
                ),
                patch.object(
                    worker,
                    "recover",
                    return_value=OfflineRecoveryResult(current, 0, 0, 0, ()),
                ),
            ):
                worker.generate_and_recover(generation_input, voice_plan)
        self.assertEqual(events, ["b", "c", "a", "d"])

    def test_generation_resume_repairs_existing_failure_before_new_generation(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, current, voice_plan = inputs(Path(temporary_directory))
            generator = Mock()
            generator.inspect.return_value = current
            worker = OfflineRecoveryWorker(generator)
            recovered = OfflineRecoveryResult(current, 1, 1, 0, ())

            with (
                patch(
                    "vntts.pregeneration_recovery._ordered_generation_queue_ids",
                    return_value=(("a",), {}),
                ),
                patch(
                    "vntts.pregeneration_recovery._generation_queue_statuses",
                    return_value={"a": "failed"},
                ),
                patch.object(worker, "recover", return_value=recovered) as repair,
            ):
                worker.generate_and_recover(generation_input, voice_plan)

        generator.generate.assert_not_called()
        repair.assert_called_once()

    def test_cancelled_saved_line_resumes_without_new_generation_or_failure_count(self):
        with TemporaryDirectory() as directory:
            generation_input, base, voice_plan = inputs(Path(directory))
            current = replace(base, generated=0, failed=0, cancelled=1)
            completed = replace(base, generated=3, failed=0)
            generator = Mock()
            generator.inspect.return_value = current
            generator.repair.return_value = completed
            planner = Mock(
                side_effect=(
                    OfflineRecoveryPlan(
                        "1" * 64,
                        "2" * 64,
                        1,
                        (OfflineRecoveryBatch("safe_resume", ("a",)),),
                        (),
                        cancelled_queue_ids=("a",),
                    ),
                    OfflineRecoveryPlan("1" * 64, "2" * 64, 0, (), ()),
                )
            )
            worker = OfflineRecoveryWorker(generator, planner=planner)
            with (
                patch(
                    "vntts.pregeneration_recovery._ordered_generation_queue_ids",
                    return_value=(("a",), {}),
                ),
                patch(
                    "vntts.pregeneration_recovery._generation_queue_statuses",
                    return_value={"a": "cancelled"},
                ),
            ):
                result = worker.generate_and_recover(generation_input, voice_plan)
        generator.generate.assert_not_called()
        self.assertEqual(generator.repair.call_args.kwargs["action"], "safe_resume")
        self.assertEqual(generator.repair.call_args.kwargs["queue_ids"], ("a",))
        self.assertEqual(result.remaining_failed, 0)
        self.assertEqual(result.recovered, 0)
        self.assertEqual(result.generation, completed)

    def test_terminal_generation_skips_failure_planning(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first, voice_plan = inputs(Path(temporary_directory))
            terminal = replace(first, generated=3, failed=0)
            planner = Mock()

            result = OfflineRecoveryWorker(planner=planner).recover(
                generation_input,
                voice_plan,
                terminal,
            )

        planner.assert_not_called()
        self.assertEqual(result.generation, terminal)

    def test_replans_and_never_repeats_a_queue_action_pair(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first, voice_plan = inputs(Path(temporary_directory))
            second = OfflineGenerationResult(first.output, first.state, 2, 1, 0)
            plans = iter(
                (
                    OfflineRecoveryPlan(
                        "1" * 64,
                        "2" * 64,
                        2,
                        (OfflineRecoveryBatch("edge_silence_trim", ("a", "b")),),
                        (),
                    ),
                    OfflineRecoveryPlan(
                        "3" * 64,
                        "2" * 64,
                        1,
                        (OfflineRecoveryBatch("edge_silence_trim", ("b",)),),
                        (),
                    ),
                )
            )
            generator = Mock()
            generator.repair.return_value = second
            worker = OfflineRecoveryWorker(
                generator, planner=lambda *_arguments: next(plans)
            )

            result = worker.recover(generation_input, voice_plan, first)

        generator.repair.assert_called_once_with(
            generation_input,
            voice_plan,
            first,
            action="edge_silence_trim",
            queue_ids=("a", "b"),
            cancel_event=None,
        )
        self.assertEqual(result.attempted_actions, 2)
        self.assertEqual(result.recovered, 1)
        self.assertEqual(result.remaining_failed, 1)
        self.assertEqual(result.remaining_action_counts, (("edge_silence_trim", 1),))

    def test_runs_new_action_after_replanning_same_queue(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first, voice_plan = inputs(Path(temporary_directory))
            still_failed = OfflineGenerationResult(first.output, first.state, 1, 2, 0)
            recovered = OfflineGenerationResult(first.output, first.state, 2, 1, 0)
            plans = iter(
                (
                    OfflineRecoveryPlan(
                        "1" * 64,
                        "2" * 64,
                        2,
                        (OfflineRecoveryBatch("safe_resume", ("a",)),),
                        (("reference_comparison", 1),),
                    ),
                    OfflineRecoveryPlan(
                        "3" * 64,
                        "2" * 64,
                        2,
                        (OfflineRecoveryBatch("bounded_seed_retry", ("a",)),),
                        (("reference_comparison", 1),),
                    ),
                    OfflineRecoveryPlan(
                        "4" * 64,
                        "2" * 64,
                        1,
                        (),
                        (("reference_comparison", 1),),
                    ),
                )
            )
            generator = Mock()
            generator.repair.side_effect = (still_failed, recovered)

            result = OfflineRecoveryWorker(
                generator, planner=lambda *_arguments: next(plans)
            ).recover(generation_input, voice_plan, first)

        self.assertEqual(
            [
                (call.kwargs["action"], call.kwargs["queue_ids"])
                for call in generator.repair.call_args_list
            ],
            [("safe_resume", ("a",)), ("bounded_seed_retry", ("a",))],
        )
        self.assertEqual(result.attempted_actions, 2)
        self.assertEqual(result.recovered, 1)
        self.assertEqual(result.remaining_action_counts, (("reference_comparison", 1),))

    def test_terminalizes_deferred_pocket_failures_without_human_review(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first, voice_plan = inputs(Path(temporary_directory))
            voice_plan = replace(
                voice_plan,
                synthesis_backend="pocket-tts",
                synthesis_model="pocket-tts",
            )
            final = OfflineGenerationResult(first.output, first.state, 1, 0, 2)
            plans = iter(
                (
                    OfflineRecoveryPlan(
                        "1" * 64,
                        "2" * 64,
                        2,
                        (),
                        (("backend_diagnosis", 2),),
                        (OfflineRecoveryBatch("backend_diagnosis", ("a", "b")),),
                        tuple(
                            PlannedLiveFallback(key, "backend_diagnosis", "f" * 64)
                            for key in ("a", "b")
                        ),
                    ),
                    OfflineRecoveryPlan(
                        "3" * 64,
                        "2" * 64,
                        0,
                        (),
                        (),
                    ),
                )
            )
            terminalizer = Mock(return_value=final)

            result = OfflineRecoveryWorker(
                Mock(),
                planner=lambda *_arguments: next(plans),
                terminalizer=terminalizer,
            ).recover(generation_input, voice_plan, first)

        terminalizer.assert_called_once()
        self.assertEqual(
            tuple(d.queue_id for d in terminalizer.call_args.args[2]), ("a", "b")
        )
        self.assertEqual(result.recovered, 0)
        self.assertEqual(result.live_fallbacks, 2)
        self.assertEqual(result.remaining_failed, 0)


if __name__ == "__main__":
    unittest.main()

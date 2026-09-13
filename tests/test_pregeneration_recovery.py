import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts.pregeneration_generation import (
    OfflineGenerationError,
    OfflineGenerationResult,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_recovery import (
    OfflineRecoveryBatch,
    OfflineRecoveryPlan,
    OfflineRecoveryResult,
    OfflineRecoveryWorker,
    plan_automatic_recovery,
)
from vntts.pregeneration_voices import VoicePlan


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
    )
    output = root / f"generation-output-{identity[:16]}"
    result = OfflineGenerationResult(
        output,
        output / "generation-state.json",
        output / "manifest.json",
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
                        "provider": "moss-tts",
                    },
                    {
                        "queue_id": "b",
                        "action": "edge_silence_trim",
                        "provider": "moss-tts",
                    },
                    {
                        "queue_id": "c",
                        "action": "bounded_seed_retry",
                        "provider": "moss-tts",
                    },
                    {
                        "queue_id": "a",
                        "action": "edge_silence_trim",
                        "provider": "moss-tts",
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
                OfflineRecoveryBatch("offline_fallback_backend", ("d",)),
            ),
        )
        self.assertEqual(plan.deferred_action_counts, ())
        self.assertEqual(plan.deferred_batches, ())

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
                        "provider": "pocket-tts",
                    },
                    {
                        "queue_id": "c",
                        "action": "offline_fallback_backend",
                        "provider": "pocket-tts",
                    },
                    {
                        "queue_id": "a",
                        "action": "safe_resume",
                        "provider": "pocket-tts",
                    },
                    {
                        "queue_id": "d",
                        "action": "sentence_boundary_segmentation",
                        "provider": "pocket-tts",
                    },
                    {
                        "queue_id": "e",
                        "action": "edge_silence_trim",
                        "provider": "pocket-tts",
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
        self.assertEqual(plan.live_fallback_queue_ids, ("b", "c"))


class OfflineRecoveryWorkerTest(unittest.TestCase):
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

        self.assertEqual(
            generator.repair.call_args.kwargs["queue_ids"],
            ("a",),
        )
        self.assertEqual(result.recovered, 1)
        self.assertEqual(result.remaining_failed, 0)

    def test_generation_and_recovery_alternate_in_queue_order(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first, voice_plan = inputs(Path(temporary_directory))
            second = replace(first, generated=2, failed=1)
            final = replace(first, generated=3, failed=0)
            generator = Mock()
            generator.inspect.side_effect = OfflineGenerationError
            generator.generate.side_effect = (first, second)
            worker = OfflineRecoveryWorker(generator)
            events = []

            def recover(_input, _plan, current, _cancel=None, *, queue_id=None):
                events.append(("recover", queue_id))
                result = second if queue_id == "a" else final
                return OfflineRecoveryResult(result, 1, 1, 0, ())

            def generate(*_arguments, **options):
                queue_id = options["queue_ids"][0]
                events.append(("generate", queue_id))
                return first if queue_id == "a" else second

            generator.generate.side_effect = generate
            with (
                patch(
                    "vntts.pregeneration_recovery._ordered_generation_queue_ids",
                    return_value=(("a", "b"), {}),
                ),
                patch.object(worker, "recover", side_effect=recover),
            ):
                result = worker.generate_and_recover(
                    generation_input,
                    voice_plan,
                )

        self.assertEqual(
            events,
            [
                ("generate", "a"),
                ("recover", "a"),
                ("generate", "b"),
                ("recover", "b"),
            ],
        )
        self.assertIs(result.generation, final)
        self.assertEqual(result.attempted_actions, 2)
        self.assertEqual(result.recovered, 2)

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

    def test_waiting_dialogue_moves_to_the_next_render_boundary(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, current, voice_plan = inputs(Path(temporary_directory))
            generator = Mock()
            generator.inspect.side_effect = OfflineGenerationError
            generator.generate.return_value = current
            worker = OfflineRecoveryWorker(generator)
            worker.prioritize_line("line:b", "b" * 64)
            recovered = OfflineRecoveryResult(current, 0, 0, 0, ())

            with (
                patch(
                    "vntts.pregeneration_recovery._ordered_generation_queue_ids",
                    return_value=(
                        ("a", "b"),
                        {("line:b", "b" * 64): "b"},
                    ),
                ),
                patch.object(worker, "recover", return_value=recovered),
            ):
                worker.generate_and_recover(generation_input, voice_plan)

        self.assertEqual(
            [call.kwargs["queue_ids"] for call in generator.generate.call_args_list],
            [("b",), ("a",)],
        )

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
        self.assertIs(result.generation, terminal)

    def test_replans_and_never_repeats_a_queue_action_pair(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first, voice_plan = inputs(Path(temporary_directory))
            second = OfflineGenerationResult(
                first.output, first.state, first.manifest, 2, 1, 0
            )
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

        generator.repair.assert_called_once()
        self.assertEqual(result.attempted_actions, 2)
        self.assertEqual(result.recovered, 1)
        self.assertEqual(result.remaining_failed, 1)
        self.assertEqual(result.remaining_action_counts, (("edge_silence_trim", 1),))

    def test_runs_new_action_after_replanning_same_queue(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first, voice_plan = inputs(Path(temporary_directory))
            still_failed = OfflineGenerationResult(
                first.output, first.state, first.manifest, 1, 2, 0
            )
            recovered = OfflineGenerationResult(
                first.output, first.state, first.manifest, 2, 1, 0
            )
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

        self.assertEqual(generator.repair.call_count, 2)
        self.assertEqual(result.attempted_actions, 2)
        self.assertEqual(result.recovered, 1)
        self.assertEqual(result.remaining_action_counts, (("reference_comparison", 1),))

    def test_uses_one_pocket_attempt_for_residual_moss_failures(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first, voice_plan = inputs(Path(temporary_directory))
            recovered = OfflineGenerationResult(
                first.output, first.state, first.manifest, 3, 0, 0
            )
            plans = iter(
                (
                    OfflineRecoveryPlan(
                        "1" * 64,
                        "2" * 64,
                        2,
                        (OfflineRecoveryBatch("offline_fallback_backend", ("a", "b")),),
                        (),
                    ),
                    OfflineRecoveryPlan("3" * 64, "2" * 64, 0, (), ()),
                )
            )
            generator = Mock()
            generator.repair.return_value = recovered

            result = OfflineRecoveryWorker(
                generator, planner=lambda *_arguments: next(plans)
            ).recover(generation_input, voice_plan, first)

        selected_plan = generator.repair.call_args.args[1]
        self.assertEqual(selected_plan.synthesis_backend, "pocket-tts")
        self.assertIsNone(selected_plan.synthesis_model)
        self.assertEqual(selected_plan.synthesis_profile, "default")
        self.assertEqual(
            generator.repair.call_args.kwargs["action"],
            "offline_fallback_backend",
        )
        self.assertEqual(result.recovered, 2)

    def test_terminalizes_deferred_pocket_failures_without_human_review(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first, voice_plan = inputs(Path(temporary_directory))
            voice_plan = replace(
                voice_plan,
                synthesis_backend="pocket-tts",
                synthesis_model="pocket-tts",
            )
            final = OfflineGenerationResult(
                first.output, first.state, first.manifest, 1, 0, 2
            )
            plans = iter(
                (
                    OfflineRecoveryPlan(
                        "1" * 64,
                        "2" * 64,
                        2,
                        (),
                        (("backend_diagnosis", 2),),
                        (OfflineRecoveryBatch("backend_diagnosis", ("a", "b")),),
                        ("a", "b"),
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
        self.assertEqual(terminalizer.call_args.args[2], ("a", "b"))
        self.assertEqual(result.recovered, 0)
        self.assertEqual(result.live_fallbacks, 2)
        self.assertEqual(result.remaining_failed, 0)


if __name__ == "__main__":
    unittest.main()

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts.pregeneration_generation import OfflineGenerationResult
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_recovery import (
    OfflineRecoveryBatch,
    OfflineRecoveryPlan,
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
                    {"queue_id": "d", "action": "reference_comparison"},
                    {"queue_id": "b", "action": "edge_silence_trim"},
                    {"queue_id": "c", "action": "bounded_seed_retry"},
                    {"queue_id": "a", "action": "edge_silence_trim"},
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

    def test_unseeded_pocket_backend_defers_a_seed_retry(self):
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
                "failure_count": 1,
                "records": [
                    {"queue_id": "a", "action": "bounded_seed_retry"},
                ],
            }

            with patch(
                "vntts.pregeneration_recovery.generation_failure_repair_plan",
                return_value=document,
            ):
                plan = plan_automatic_recovery(generation_input, voice_plan, result)

        self.assertEqual(plan.automatic_batches, ())
        self.assertEqual(plan.deferred_action_counts, (("bounded_seed_retry", 1),))


class OfflineRecoveryWorkerTest(unittest.TestCase):
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

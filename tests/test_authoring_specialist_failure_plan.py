import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.authoring.cohort_review import CohortReviewError
from vntts.authoring.specialist_failure_plan import (
    OFFLINE_FALLBACK_BACKEND,
    REFERENCE_OR_LIVE,
    SENTENCE_REPAIR_RETRY,
    build_specialist_failure_plan,
    load_specialist_failure_plan,
    write_specialist_failure_plan,
)


class SpecialistFailurePlanTest(unittest.TestCase):
    def create_workspace(self, root, strategy, queue_id):
        workspace = root / queue_id
        (workspace / "generated-audio").mkdir(parents=True)
        configuration = {
            "workspace_id": f"workspace-{queue_id}",
            "config_fingerprint": "a" * 64,
            "carry_forward": {"failed_queue_ids": [queue_id]},
        }
        queue = [
            {"record_type": "metadata"},
            {
                "queue_id": queue_id,
                "line_id": f"line-{queue_id}",
                "text": "First sentence. Second sentence.",
                "speaker": "Narrator",
            },
        ]
        result = {
            "status": "failed",
            "provider": "moss-tts"
            if strategy != OFFLINE_FALLBACK_BACKEND
            else "pocket-tts",
            "model": "model",
            "generation_profile": "stable",
            "voice_character": "Narrator",
            "attempts_by_provider": {"moss-tts": 1},
            "failure_repair": {"strategy": strategy},
            "failure": {
                "kind": "missed_eos_audio_limit"
                if strategy != OFFLINE_FALLBACK_BACKEND
                else "speech_silence",
                "completion": "limited"
                if strategy != OFFLINE_FALLBACK_BACKEND
                else "complete",
                "error_type": "ExampleError",
                "text_features": {
                    "word_count": 4,
                    "sentence_boundary_count": 2,
                    "ellipsis_count": 0,
                },
            },
        }
        (workspace / "workspace.json").write_text(json.dumps(configuration))
        (workspace / "queue.jsonl").write_text(
            "\n".join(json.dumps(value) for value in queue) + "\n"
        )
        (workspace / "generated-audio/generation-state.json").write_text(
            json.dumps({"items": {queue_id: result}})
        )
        return workspace

    def test_plan_assigns_only_bounded_evidence_backed_actions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sentence = self.create_workspace(
                root, "sentence_boundary_segmentation", "a"
            )
            pocket = self.create_workspace(root, OFFLINE_FALLBACK_BACKEND, "b")

            plan = build_specialist_failure_plan((sentence, pocket))

        self.assertEqual(plan.document["item_count"], 2)
        self.assertEqual(
            plan.document["action_counts"],
            {
                SENTENCE_REPAIR_RETRY: 0,
                OFFLINE_FALLBACK_BACKEND: 1,
                REFERENCE_OR_LIVE: 1,
            },
        )

    def test_published_plan_rejects_identity_tamper(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(
                root, "sentence_boundary_segmentation", "a"
            )
            plan = build_specialist_failure_plan((workspace,))
            output = root / "plan.json"
            write_specialist_failure_plan(plan, output)
            document = json.loads(output.read_text())
            document["items"][0]["next_action"] = REFERENCE_OR_LIVE
            output.write_text(json.dumps(document))

            with self.assertRaisesRegex(CohortReviewError, "identity changed"):
                load_specialist_failure_plan(output)

    def test_complete_sentence_silence_is_not_sent_to_pocket(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(
                root, "sentence_boundary_segmentation", "a"
            )
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            failure = state["items"]["a"]["failure"]
            failure["kind"] = "speech_silence"
            failure["completion"] = "complete"
            state_path.write_text(json.dumps(state))

            plan = build_specialist_failure_plan((workspace,))

        self.assertEqual(
            plan.document["action_counts"],
            {
                SENTENCE_REPAIR_RETRY: 0,
                OFFLINE_FALLBACK_BACKEND: 0,
                REFERENCE_OR_LIVE: 1,
            },
        )

    def test_two_attempt_sentence_failure_gets_one_exact_retry_first(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(
                root, "sentence_boundary_segmentation", "a"
            )
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            state["items"]["a"]["attempts"] = 2
            state_path.write_text(json.dumps(state))

            plan = build_specialist_failure_plan((workspace,))

        self.assertEqual(
            plan.document["items"][0]["next_action"], SENTENCE_REPAIR_RETRY
        )


if __name__ == "__main__":
    unittest.main()

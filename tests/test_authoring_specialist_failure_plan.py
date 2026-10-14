import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.authoring_fixtures import create_specialist_failure_workspace
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.cohort_review import CohortReviewError
from vntts.authoring.specialist_failure_plan import (
    INLINE_PAUSE_MARKER,
    OFFLINE_FALLBACK_BACKEND,
    REFERENCE_OR_LIVE,
    SENTENCE_REPAIR_RETRY,
    build_specialist_failure_plan,
    load_specialist_failure_plan,
    write_specialist_failure_plan,
)


class SpecialistFailurePlanTest(unittest.TestCase):
    def test_plan_assigns_only_bounded_evidence_backed_actions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sentence = create_specialist_failure_workspace(
                root, "sentence_boundary_segmentation", "a"
            )
            pocket = create_specialist_failure_workspace(
                root, OFFLINE_FALLBACK_BACKEND, "b"
            )

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
            workspace = create_specialist_failure_workspace(
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

    def test_published_plan_rejects_checksum_valid_non_integer_counts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sentence = create_specialist_failure_workspace(
                root, "sentence_boundary_segmentation", "a"
            )
            pocket = create_specialist_failure_workspace(
                root, OFFLINE_FALLBACK_BACKEND, "b"
            )
            plan = build_specialist_failure_plan((sentence, pocket))
            output = root / "plan.json"

            for field, value, error in (
                ("schema_version", True, "version"),
                ("item_count", float(plan.document["item_count"]), "item count"),
                ("source_count", float(plan.document["source_count"]), "source count"),
                (
                    "cluster_count",
                    float(plan.document["cluster_count"]),
                    "cluster count",
                ),
            ):
                with self.subTest(field=field):
                    document = json.loads(json.dumps(plan.document))
                    document[field] = value
                    document["plan_id"] = canonical_document_sha256(
                        {
                            key: value
                            for key, value in document.items()
                            if key != "plan_id"
                        }
                    )
                    output.write_text(json.dumps(document))
                    with self.assertRaisesRegex(CohortReviewError, error):
                        load_specialist_failure_plan(output)

            for group, field, value, error in (
                ("sources", "failed_item_count", True, "source counts"),
                ("sources", "failed_item_count", 99, "source counts"),
                ("clusters", "item_count", 1.0, "cluster item counts"),
            ):
                with self.subTest(group=group, field=field):
                    document = json.loads(json.dumps(plan.document))
                    document[group][0][field] = value
                    document["plan_id"] = canonical_document_sha256(
                        {
                            key: value
                            for key, value in document.items()
                            if key != "plan_id"
                        }
                    )
                    output.write_text(json.dumps(document))
                    with self.assertRaisesRegex(CohortReviewError, error):
                        load_specialist_failure_plan(output)

    def test_complete_sentence_silence_is_not_sent_to_pocket(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = create_specialist_failure_workspace(
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
            workspace = create_specialist_failure_workspace(
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

    def test_exhausted_inline_pause_failure_gets_one_pocket_attempt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = create_specialist_failure_workspace(
                root, INLINE_PAUSE_MARKER, "a"
            )
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            item = state["items"]["a"]
            item["failure"]["kind"] = "speech_silence"
            item["failure"]["completion"] = "complete"
            item["attempts"] = 3
            item["attempts_by_provider"] = {"moss-tts": 3}
            state_path.write_text(json.dumps(state))

            plan = build_specialist_failure_plan((workspace,))

        self.assertEqual(
            plan.document["items"][0]["next_action"], OFFLINE_FALLBACK_BACKEND
        )


if __name__ == "__main__":
    unittest.main()

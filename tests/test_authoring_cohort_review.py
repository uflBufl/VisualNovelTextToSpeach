import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_review import (
    CohortReviewError,
    build_cohort_review_decision,
    build_cohort_review_plan,
    load_cohort_review_plan,
    write_cohort_review_decision,
    write_cohort_review_plan,
)


class AuthoringCohortReviewTest(unittest.TestCase):
    def create_pending_workspace(self, root):
        _fixture, _imported, created = create_test_workspace(root)
        state_path = created.directory / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        queue_id, result = next(iter(state["items"].items()))
        result.update(
            {
                "status": "generated",
                "review_status": "pending_review",
                "generation_profile": "stable",
                "voice_character": "Rhiannon",
                "prompt_applied": False,
                "synthesis_provenance_sha256": "b" * 64,
            }
        )
        state["active"] = None
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        return created.directory, state_path, queue_id

    def test_plan_is_deterministic_and_samples_every_attention_item(self):
        with TemporaryDirectory() as directory:
            workspace, _state, queue_id = self.create_pending_workspace(Path(directory))

            first = build_cohort_review_plan(workspace)
            second = build_cohort_review_plan(workspace)

        self.assertEqual(first, second)
        self.assertEqual(first.document["schema_version"], 1)
        self.assertEqual(first.document["cohort_count"], 1)
        self.assertEqual(first.document["pending_item_count"], 1)
        self.assertEqual(first.document["sample_item_count"], 1)
        cohort = first.document["cohorts"][0]
        self.assertEqual(cohort["sample_queue_ids"], [queue_id])
        self.assertEqual(cohort["items"][0]["queue_id"], queue_id)
        self.assertTrue(cohort["items"][0]["technical_flags"])
        self.assertTrue(cohort["items"][0]["sampled"])
        self.assertEqual(first.plan_id, first.document["plan_id"])

    def test_terminal_item_is_not_planned_again(self):
        with TemporaryDirectory() as directory:
            workspace, state_path, _queue_id = self.create_pending_workspace(
                Path(directory)
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            result = next(iter(state["items"].values()))
            result["status"] = "approved"
            result["review_status"] = "approved"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            plan = build_cohort_review_plan(workspace)

        self.assertEqual(plan.document["cohort_count"], 0)
        self.assertEqual(plan.document["pending_item_count"], 0)
        self.assertEqual(plan.document["sample_item_count"], 0)

    def test_incomplete_legacy_provenance_is_reported_not_guessed(self):
        with TemporaryDirectory() as directory:
            workspace, state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            next(iter(state["items"].values())).pop("generation_profile")
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            plan = build_cohort_review_plan(workspace)

        self.assertEqual(plan.document["cohort_count"], 0)
        self.assertEqual(plan.document["pending_item_count"], 0)
        self.assertEqual(plan.document["blocked_item_count"], 1)
        self.assertEqual(plan.document["blocked_items"][0]["queue_id"], queue_id)
        self.assertIn(
            "Generation profile",
            plan.document["blocked_items"][0]["reason"],
        )

    def test_seed_change_creates_a_distinct_plan_and_cohort(self):
        with TemporaryDirectory() as directory:
            workspace, state_path, _queue_id = self.create_pending_workspace(
                Path(directory)
            )
            first = build_cohort_review_plan(workspace)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            next(iter(state["items"].values()))["seed"] += 1
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            second = build_cohort_review_plan(workspace)

        self.assertNotEqual(first.plan_id, second.plan_id)
        self.assertNotEqual(
            first.document["cohorts"][0]["cohort_id"],
            second.document["cohorts"][0]["cohort_id"],
        )

    def test_state_change_during_projection_fails_closed(self):
        with TemporaryDirectory() as directory:
            workspace, state_path, _queue_id = self.create_pending_workspace(
                Path(directory)
            )

            def mutate(_workspace):
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["active"] = {"phase": "changed"}
                state_path.write_text(
                    json.dumps(state, sort_keys=True), encoding="utf-8"
                )
                return ()

            with (
                patch(
                    "vntts.authoring.cohort_review.list_review_items",
                    side_effect=mutate,
                ),
                self.assertRaisesRegex(CohortReviewError, "state changed"),
            ):
                build_cohort_review_plan(workspace)

    def test_sample_count_is_bounded(self):
        with self.assertRaisesRegex(CohortReviewError, "integer from 1 to 5"):
            build_cohort_review_plan("unused", clean_samples_per_bucket=0)

    def test_cli_prints_the_same_public_plan(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, _queue_id = self.create_pending_workspace(
                Path(directory)
            )
            expected = build_cohort_review_plan(workspace).document
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = authoring_main(["cohort-review-plan", str(workspace)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)

    def test_accept_requires_every_sample_and_binds_every_target_wav(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort = plan.document["cohorts"][0]

            with self.assertRaisesRegex(CohortReviewError, "Every sampled WAV"):
                build_cohort_review_decision(
                    plan,
                    cohort["cohort_id"],
                    "accepted",
                    reviewed_queue_ids=[],
                )
            decision = build_cohort_review_decision(
                plan,
                cohort["cohort_id"],
                "accepted",
                reviewed_queue_ids=[queue_id],
            )

        self.assertEqual(decision.document["projection_review_status"], "approved")
        self.assertEqual(decision.document["reviewed_samples"][0]["queue_id"], queue_id)
        self.assertEqual(decision.document["target_items"][0]["queue_id"], queue_id)
        self.assertEqual(
            decision.document["target_items"][0]["audio_sha256"],
            cohort["items"][0]["audio_sha256"],
        )

    def test_reject_requires_reviewed_evidence_but_not_every_sample(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]

            with self.assertRaisesRegex(CohortReviewError, "at least one"):
                build_cohort_review_decision(
                    plan, cohort_id, "rejected", reviewed_queue_ids=[]
                )
            decision = build_cohort_review_decision(
                plan, cohort_id, "rejected", reviewed_queue_ids=[queue_id]
            )

        self.assertEqual(decision.document["projection_review_status"], "rejected")

    def test_expand_requires_complete_current_sample_and_larger_bound(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]

            with self.assertRaisesRegex(CohortReviewError, "larger integer"):
                build_cohort_review_decision(
                    plan,
                    cohort_id,
                    "expand",
                    reviewed_queue_ids=[queue_id],
                    next_clean_samples_per_bucket=1,
                )
            decision = build_cohort_review_decision(
                plan,
                cohort_id,
                "expand",
                reviewed_queue_ids=[queue_id],
                next_clean_samples_per_bucket=2,
            )

        self.assertIsNone(decision.document["projection_review_status"])
        self.assertEqual(decision.document["next_clean_samples_per_bucket"], 2)

    def test_plan_identity_tamper_is_rejected(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, _queue_id = self.create_pending_workspace(
                Path(directory)
            )
            document = build_cohort_review_plan(workspace).to_dict()
            document["state_sha256"] = "0" * 64

            with self.assertRaisesRegex(CohortReviewError, "identity is invalid"):
                build_cohort_review_decision(
                    document,
                    document["cohorts"][0]["cohort_id"],
                    "accepted",
                    reviewed_queue_ids=document["cohorts"][0]["sample_queue_ids"],
                )

    def test_plan_and_decision_publish_without_replacement(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = self.create_pending_workspace(root)
            plan = build_cohort_review_plan(workspace)
            plan_path = root / "review-plan.json"
            write_cohort_review_plan(plan, plan_path)
            loaded = load_cohort_review_plan(plan_path)
            cohort_id = loaded.document["cohorts"][0]["cohort_id"]
            decision = build_cohort_review_decision(
                loaded, cohort_id, "accepted", reviewed_queue_ids=[queue_id]
            )
            decision_path = root / "decision.json"
            write_cohort_review_decision(decision, decision_path)

            with self.assertRaisesRegex(CohortReviewError, "output exists"):
                write_cohort_review_plan(plan, plan_path)
            with self.assertRaisesRegex(CohortReviewError, "output exists"):
                write_cohort_review_decision(decision, decision_path)
            self.assertEqual(
                json.loads(decision_path.read_text(encoding="utf-8")),
                decision.document,
            )

    def test_cli_records_exact_decision_from_published_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = self.create_pending_workspace(root)
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]
            plan_path = root / "review-plan.json"
            decision_path = root / "decision.json"
            write_cohort_review_plan(plan, plan_path)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "cohort-review-decision",
                        str(plan_path),
                        cohort_id,
                        "accepted",
                        "--reviewed-queue-id",
                        queue_id,
                        "--output",
                        str(decision_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads(stdout.getvalue()),
                json.loads(decision_path.read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.bulk_generation import _canonical_sha256
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_review import (
    CohortReviewError,
    apply_cohort_review_decision,
    build_cohort_review_decision,
    build_cohort_review_plan,
    execute_cohort_review_decision,
    load_cohort_review_decision,
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
        self.assertEqual(first.document["policy"]["schema_version"], 2)
        self.assertEqual(
            first.document["policy"]["attention_thresholds"],
            {
                "silence_ratio_at_least": 0.3,
                "internal_pause_seconds_at_least": 1.0,
            },
        )
        self.assertEqual(first.document["cohort_count"], 1)
        self.assertEqual(first.document["pending_item_count"], 1)
        self.assertEqual(first.document["sample_item_count"], 1)
        cohort = first.document["cohorts"][0]
        self.assertEqual(cohort["sample_queue_ids"], [queue_id])
        self.assertEqual(cohort["items"][0]["queue_id"], queue_id)
        self.assertTrue(cohort["items"][0]["technical_flags"])
        self.assertTrue(cohort["items"][0]["sampled"])
        self.assertEqual(first.plan_id, first.document["plan_id"])

    def test_legacy_policy_v1_plan_remains_readable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state, _queue_id = self.create_pending_workspace(root)
            document = deepcopy(build_cohort_review_plan(workspace).document)
            document["policy"]["schema_version"] = 1
            document["policy"].pop("attention_thresholds")
            document["plan_id"] = _canonical_sha256(
                {key: value for key, value in document.items() if key != "plan_id"}
            )
            path = root / "legacy-plan.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            loaded = load_cohort_review_plan(path)

        self.assertEqual(loaded.document["policy"]["schema_version"], 1)
        self.assertNotIn("attention_thresholds", loaded.document["policy"])

    def test_policy_v2_threshold_tamper_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state, _queue_id = self.create_pending_workspace(root)
            document = deepcopy(build_cohort_review_plan(workspace).document)
            document["policy"]["attention_thresholds"]["silence_ratio_at_least"] = 0.15
            document["plan_id"] = _canonical_sha256(
                {key: value for key, value in document.items() if key != "plan_id"}
            )
            path = root / "tampered-plan.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(CohortReviewError, "thresholds are invalid"):
                load_cohort_review_plan(path)

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

    def test_exact_selection_rejects_items_that_are_not_pending(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace, queue_ids=[queue_id])
            with self.assertRaisesRegex(CohortReviewError, "not pending"):
                build_cohort_review_plan(workspace, queue_ids=["missing"])

        self.assertEqual(plan.document["policy"]["selected_queue_ids"], [queue_id])
        self.assertEqual(plan.document["pending_item_count"], 1)

    def test_cli_prints_the_same_public_plan(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            expected = build_cohort_review_plan(
                workspace, queue_ids=[queue_id]
            ).document
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "cohort-review-plan",
                        str(workspace),
                        "--queue-id",
                        queue_id,
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        self.assertEqual(
            expected["policy"]["selected_queue_ids"],
            [queue_id],
        )

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
        self.assertEqual(
            decision.document["sample_assessments"],
            [
                {
                    "queue_id": queue_id,
                    "assessment": "heard",
                    "defect_reasons": [],
                }
            ],
        )
        self.assertEqual(decision.document["reviewed_samples"][0]["queue_id"], queue_id)
        self.assertEqual(decision.document["target_items"][0]["queue_id"], queue_id)
        self.assertEqual(
            decision.document["target_items"][0]["audio_sha256"],
            cohort["items"][0]["audio_sha256"],
        )

    def test_terminal_apply_uses_bound_state_instead_of_rescanning_plan(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            decision = build_cohort_review_decision(
                plan,
                plan.document["cohorts"][0]["cohort_id"],
                "accepted",
                reviewed_queue_ids=[queue_id],
            )

            with (
                patch(
                    "vntts.authoring.cohort_review.build_cohort_review_plan",
                    side_effect=AssertionError("full plan rescan"),
                ),
                patch(
                    "vntts.authoring.cohort_review.inspect_workspace",
                    side_effect=AssertionError("broad workspace inspection"),
                ),
            ):
                projection = apply_cohort_review_decision(workspace, plan, decision)

        self.assertEqual(projection.queue_ids, (queue_id,))

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

    def test_bad_sample_is_bound_to_rejection_and_blocks_acceptance(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]

            with self.assertRaisesRegex(CohortReviewError, "marked as bad"):
                build_cohort_review_decision(
                    plan,
                    cohort_id,
                    "accepted",
                    reviewed_queue_ids=[queue_id],
                    sample_assessments={queue_id: "bad"},
                )
            decision = build_cohort_review_decision(
                plan,
                cohort_id,
                "rejected",
                reviewed_queue_ids=[queue_id],
                sample_assessments={queue_id: "bad"},
            )

        self.assertEqual(
            decision.document["sample_assessments"],
            [
                {
                    "queue_id": queue_id,
                    "assessment": "bad",
                    "defect_reasons": ["unspecified"],
                }
            ],
        )

    def test_bad_sample_preserves_multiple_explicit_defect_reasons(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]

            decision = build_cohort_review_decision(
                plan,
                cohort_id,
                "rejected",
                reviewed_queue_ids=[queue_id],
                sample_assessments={
                    queue_id: {
                        "assessment": "bad",
                        "defect_reasons": [
                            "repetition",
                            "pause_or_pacing",
                        ],
                    }
                },
            )

        self.assertEqual(
            decision.document["sample_assessments"][0]["defect_reasons"],
            ["pause_or_pacing", "repetition"],
        )

    def test_version_one_decision_remains_readable_without_guessed_reasons(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state_path, queue_id = self.create_pending_workspace(root)
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]
            document = deepcopy(
                build_cohort_review_decision(
                    plan,
                    cohort_id,
                    "rejected",
                    reviewed_queue_ids=[queue_id],
                    sample_assessments={queue_id: "bad"},
                ).document
            )
            document["schema_version"] = 1
            document.pop("item_review_statuses")
            for assessment in document["sample_assessments"]:
                assessment.pop("defect_reasons")
            document["decision_id"] = _canonical_sha256(
                {key: value for key, value in document.items() if key != "decision_id"}
            )
            path = root / "legacy-decision.json"
            path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

            loaded = load_cohort_review_decision(path)

        self.assertNotIn("defect_reasons", loaded.document["sample_assessments"][0])

    def test_split_decision_binds_each_individually_sampled_target(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = deepcopy(build_cohort_review_plan(workspace).document)
            cohort = plan["cohorts"][0]
            second = deepcopy(cohort["items"][0])
            second.update(
                {
                    "queue_id": f"{queue_id}-second",
                    "line_id": f"{second['line_id']}-second",
                    "text_sha256": "c" * 64,
                    "audio_sha256": "d" * 64,
                    "sampled": True,
                }
            )
            cohort["items"].append(second)
            cohort["item_count"] = 2
            cohort["sample_queue_ids"].append(second["queue_id"])
            plan["pending_item_count"] = 2
            plan["sample_item_count"] = 2
            plan["plan_id"] = _canonical_sha256(
                {key: value for key, value in plan.items() if key != "plan_id"}
            )

            decision = build_cohort_review_decision(
                plan,
                cohort["cohort_id"],
                "split",
                reviewed_queue_ids=[queue_id, second["queue_id"]],
                sample_assessments={
                    queue_id: {
                        "assessment": "bad",
                        "defect_reasons": ["pause_or_pacing"],
                    },
                    second["queue_id"]: "acceptable",
                },
            )

        self.assertIsNone(decision.document["projection_review_status"])
        self.assertEqual(
            decision.document["item_review_statuses"],
            [
                {"queue_id": queue_id, "review_status": "rejected"},
                {"queue_id": second["queue_id"], "review_status": "approved"},
            ],
        )

    def test_split_decision_never_projects_an_unsampled_target(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = deepcopy(build_cohort_review_plan(workspace).document)
            cohort = plan["cohorts"][0]
            second = deepcopy(cohort["items"][0])
            second.update(
                {
                    "queue_id": f"{queue_id}-unsampled",
                    "line_id": f"{second['line_id']}-unsampled",
                    "text_sha256": "c" * 64,
                    "audio_sha256": "d" * 64,
                    "sampled": False,
                }
            )
            cohort["items"].append(second)
            cohort["item_count"] = 2
            plan["pending_item_count"] = 2
            plan["plan_id"] = _canonical_sha256(
                {key: value for key, value in plan.items() if key != "plan_id"}
            )

            with self.assertRaisesRegex(CohortReviewError, "unsampled targets"):
                build_cohort_review_decision(
                    plan,
                    cohort["cohort_id"],
                    "split",
                    reviewed_queue_ids=[queue_id],
                    sample_assessments={
                        queue_id: {
                            "assessment": "bad",
                            "defect_reasons": ["pause_or_pacing"],
                        }
                    },
                )

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

    def test_terminal_decision_projects_exact_state_and_manifest_provenance(self):
        with TemporaryDirectory() as directory:
            workspace, state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort = plan.document["cohorts"][0]
            decision = build_cohort_review_decision(
                plan,
                cohort["cohort_id"],
                "accepted",
                reviewed_queue_ids=[queue_id],
            )

            result = apply_cohort_review_decision(workspace, plan, decision)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            manifest = json.loads(
                (state_path.parent / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result.queue_ids, (queue_id,))
        self.assertEqual(result.review_status, "approved")
        self.assertEqual(state["items"][queue_id]["status"], "approved")
        self.assertEqual(state["items"][queue_id]["review_status"], "approved")
        provenance = state["items"][queue_id]["cohort_review"]
        self.assertEqual(provenance["decision_id"], decision.decision_id)
        self.assertEqual(
            provenance["target_audio_sha256"],
            cohort["items"][0]["audio_sha256"],
        )
        self.assertEqual(len(manifest["entries"]), 1)
        self.assertEqual(
            manifest["entries"][0]["cohort_review"]["decision_id"],
            decision.decision_id,
        )

    def test_changed_wav_blocks_projection_without_state_mutation(self):
        with TemporaryDirectory() as directory:
            workspace, state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort = plan.document["cohorts"][0]
            decision = build_cohort_review_decision(
                plan,
                cohort["cohort_id"],
                "rejected",
                reviewed_queue_ids=[queue_id],
            )
            before = state_path.read_bytes()
            state = json.loads(before.decode("utf-8"))
            audio = state_path.parent / state["items"][queue_id]["path"]
            audio.write_bytes(audio.read_bytes() + b"changed")

            with self.assertRaisesRegex(
                CohortReviewError, "checksum mismatch|authority changed"
            ):
                apply_cohort_review_decision(workspace, plan, decision)

            self.assertEqual(state_path.read_bytes(), before)

    def test_changed_workspace_configuration_blocks_projection(self):
        with TemporaryDirectory() as directory:
            workspace, state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            decision = build_cohort_review_decision(
                plan,
                plan.document["cohorts"][0]["cohort_id"],
                "accepted",
                reviewed_queue_ids=[queue_id],
            )
            state_before = state_path.read_bytes()
            workspace_path = workspace / "workspace.json"
            document = json.loads(workspace_path.read_text(encoding="utf-8"))
            document["narrator_character"] = "Changed narrator"
            workspace_path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(CohortReviewError, "configuration changed"):
                apply_cohort_review_decision(workspace, plan, decision)

            self.assertEqual(state_path.read_bytes(), state_before)

    def test_expand_decision_never_changes_generation_state(self):
        with TemporaryDirectory() as directory:
            workspace, state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]
            decision = build_cohort_review_decision(
                plan,
                cohort_id,
                "expand",
                reviewed_queue_ids=[queue_id],
                next_clean_samples_per_bucket=2,
            )
            before = state_path.read_bytes()

            with self.assertRaisesRegex(CohortReviewError, "cannot be applied"):
                apply_cohort_review_decision(workspace, plan, decision)

            self.assertEqual(state_path.read_bytes(), before)

    def test_mismatched_decision_creates_no_immutable_evidence(self):
        for decision_name in ("accepted", "expand"):
            with (
                self.subTest(decision=decision_name),
                TemporaryDirectory() as directory,
            ):
                workspace, _state_path, queue_id = self.create_pending_workspace(
                    Path(directory)
                )
                plan = build_cohort_review_plan(workspace)
                cohort_id = plan.document["cohorts"][0]["cohort_id"]
                options = (
                    {"next_clean_samples_per_bucket": 2}
                    if decision_name == "expand"
                    else {}
                )
                decision = build_cohort_review_decision(
                    plan,
                    cohort_id,
                    decision_name,
                    reviewed_queue_ids=[queue_id],
                    **options,
                )
                forged = deepcopy(decision.document)
                forged["plan_id"] = "0" * 64
                forged["decision_id"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in forged.items()
                        if key != "decision_id"
                    }
                )

                with self.assertRaisesRegex(
                    CohortReviewError, "belongs to a different plan"
                ):
                    execute_cohort_review_decision(workspace, plan, forged)

                self.assertFalse((workspace / "cohort-reviews").exists())

    def test_rejection_revokes_manifest_before_state_commit(self):
        with TemporaryDirectory() as directory:
            workspace, state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]
            decision = build_cohort_review_decision(
                plan, cohort_id, "rejected", reviewed_queue_ids=[queue_id]
            )
            manifest_path = state_path.parent / "manifest.json"
            original_replace = __import__("os").replace

            def fail_state_commit(source, destination):
                if Path(destination).resolve() == state_path.resolve():
                    raise OSError("injected state commit failure")
                return original_replace(source, destination)

            with (
                patch(
                    "vntts.authoring.bulk_generation.os.replace",
                    side_effect=fail_state_commit,
                ),
                self.assertRaisesRegex(CohortReviewError, "remains fail-closed"),
            ):
                apply_cohort_review_decision(workspace, plan, decision)

            state = json.loads(state_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["items"][queue_id]["review_status"], "pending_review"
            )
            self.assertNotIn(
                queue_id,
                {entry["queue_id"] for entry in manifest["entries"]},
            )

    def test_wav_change_during_staging_blocks_both_state_and_manifest(self):
        with TemporaryDirectory() as directory:
            workspace, state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort = plan.document["cohorts"][0]
            decision = build_cohort_review_decision(
                plan,
                cohort["cohort_id"],
                "accepted",
                reviewed_queue_ids=[queue_id],
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            audio = state_path.parent / state["items"][queue_id]["path"]
            state_before = state_path.read_bytes()
            manifest_path = state_path.parent / "manifest.json"
            manifest_before = manifest_path.read_bytes()
            from vntts.authoring import bulk_generation

            original = bulk_generation._write_generated_manifest_from_state

            def mutate_after_staging(*args, **kwargs):
                original(*args, **kwargs)
                audio.write_bytes(audio.read_bytes() + b"changed-during-staging")

            with (
                patch(
                    "vntts.authoring.bulk_generation._write_generated_manifest_from_state",
                    side_effect=mutate_after_staging,
                ),
                self.assertRaisesRegex(CohortReviewError, "authority changed"),
            ):
                apply_cohort_review_decision(workspace, plan, decision)

            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)

    def test_recomputed_decision_cannot_forge_reviewed_wav_evidence(self):
        with TemporaryDirectory() as directory:
            workspace, _state_path, queue_id = self.create_pending_workspace(
                Path(directory)
            )
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]
            decision = build_cohort_review_decision(
                plan, cohort_id, "rejected", reviewed_queue_ids=[queue_id]
            )
            forged = deepcopy(decision.document)
            forged["reviewed_samples"][0]["audio_sha256"] = "0" * 64
            forged["decision_id"] = _canonical_sha256(
                {key: value for key, value in forged.items() if key != "decision_id"}
            )

            with self.assertRaisesRegex(CohortReviewError, "evidence does not match"):
                apply_cohort_review_decision(workspace, plan, forged)

    def test_cli_applies_persisted_terminal_decision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, state_path, queue_id = self.create_pending_workspace(root)
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]
            decision = build_cohort_review_decision(
                plan, cohort_id, "rejected", reviewed_queue_ids=[queue_id]
            )
            plan_path = root / "plan.json"
            decision_path = root / "decision.json"
            write_cohort_review_plan(plan, plan_path)
            write_cohort_review_decision(decision, decision_path)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "cohort-review-apply",
                        str(workspace),
                        str(plan_path),
                        str(decision_path),
                    ]
                )
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["review_status"], "rejected")
        self.assertEqual(state["items"][queue_id]["review_status"], "rejected")

    def test_workspace_evidence_directory_symlink_fails_before_projection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, state_path, queue_id = self.create_pending_workspace(root)
            plan = build_cohort_review_plan(workspace)
            cohort_id = plan.document["cohorts"][0]["cohort_id"]
            decision = build_cohort_review_decision(
                plan, cohort_id, "rejected", reviewed_queue_ids=[queue_id]
            )
            outside = root / "outside"
            outside.mkdir()
            (workspace / "cohort-reviews").symlink_to(outside, target_is_directory=True)
            before = state_path.read_bytes()

            with self.assertRaisesRegex(CohortReviewError, "cannot be a symlink"):
                execute_cohort_review_decision(workspace, plan, decision)

            self.assertEqual(state_path.read_bytes(), before)
            self.assertEqual(tuple(outside.iterdir()), ())


if __name__ == "__main__":
    unittest.main()

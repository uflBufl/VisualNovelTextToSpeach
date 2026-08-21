import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from tests import test_authoring_cohort_review
from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_review import (
    build_cohort_review_decision,
    build_cohort_review_plan,
    write_cohort_review_decision,
    write_cohort_review_plan,
)
from vntts.authoring.voice_quality_gate import (
    VoiceQualityGateError,
    build_voice_quality_gate,
    inspect_voice_quality_gate,
    load_voice_quality_gate,
    write_voice_quality_gate,
)
from vntts.authoring.workbench import create_resume_workspace


class AuthoringVoiceQualityGateTest(unittest.TestCase):
    def create_review(self, root):
        fixture = test_authoring_cohort_review.AuthoringCohortReviewTest()
        workspace, state_path, queue_id = fixture.create_pending_workspace(root)
        state = json.loads(state_path.read_text())
        result = state["items"][queue_id]
        result.update(
            {
                "provider": "moss-tts",
                "model": "model with spaces",
                "generation_profile": "stable",
            }
        )
        state_path.write_text(json.dumps(state, sort_keys=True))
        return self.create_review_from_workspace(workspace, state_path, queue_id)

    def create_review_from_workspace(self, workspace, state_path, queue_id):
        plan = build_cohort_review_plan(workspace)
        decision = build_cohort_review_decision(
            plan,
            plan.document["cohorts"][0]["cohort_id"],
            "accepted",
            reviewed_queue_ids=[queue_id],
            sample_assessments={queue_id: "acceptable"},
        )
        return workspace, state_path, queue_id, plan, decision

    def test_gate_binds_controls_but_not_story_seed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, state_path, queue_id, plan, decision = self.create_review(root)
            gate = build_voice_quality_gate(workspace, plan, decision)
            state = json.loads(state_path.read_text())
            state["items"][queue_id]["seed"] += 1
            state_path.write_text(json.dumps(state, sort_keys=True))

            compatibility = inspect_voice_quality_gate(gate, workspace, queue_id)

        self.assertEqual(compatibility.status, "control_match_story_sample_required")
        self.assertTrue(compatibility.story_sample_required)
        self.assertEqual(compatibility.differences, ())
        self.assertNotIn("seed", gate.document["identity"])
        self.assertIsInstance(gate.document["source_review"]["source_seed"], int)

    def test_reordered_references_require_new_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state, _queue_id, plan, decision = self.create_review(
                root / "source"
            )
            gate = build_voice_quality_gate(workspace, plan, decision)

            fixture, imported, created = create_test_workspace(root / "later")
            manifest_path = Path(fixture["job"]["voice_manifest"])
            manifest = json.loads(manifest_path.read_text())
            manifest["voices"][0]["references"].reverse()
            manifest_path.write_text(json.dumps(manifest))
            later = create_resume_workspace(
                imported,
                root / "later-workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=manifest_path,
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Rhiannon",
            )
            later_state = later.directory / "generated-audio/generation-state.json"
            state = json.loads(later_state.read_text())
            queue_id, result = next(iter(state["items"].items()))
            result.update(
                {
                    "status": "generated",
                    "review_status": "pending_review",
                    "provider": "moss-tts",
                    "model": "model with spaces",
                    "generation_profile": "stable",
                    "voice_character": "Rhiannon",
                    "prompt_applied": False,
                    "synthesis_provenance_sha256": "b" * 64,
                }
            )
            later_state.write_text(json.dumps(state, sort_keys=True))

            compatibility = inspect_voice_quality_gate(gate, later.directory, queue_id)

        self.assertEqual(compatibility.status, "new_review")
        self.assertIn("ordered_reference_sha256", compatibility.differences)

    def test_model_bytes_change_requires_new_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model"
            model.mkdir()
            (model / "weights.bin").write_bytes(b"first")
            fixture, imported, _created = create_test_workspace(root / "seed")
            workspace = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model=str(model),
                generation_profile="stable",
                narrator_character="Rhiannon",
            )
            state_path = workspace.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            queue_id, result = next(iter(state["items"].items()))
            result.update(
                {
                    "status": "generated",
                    "review_status": "pending_review",
                    "provider": "moss-tts",
                    "model": str(model),
                    "generation_profile": "stable",
                    "voice_character": "Rhiannon",
                    "prompt_applied": False,
                    "synthesis_provenance_sha256": "b" * 64,
                }
            )
            state_path.write_text(json.dumps(state, sort_keys=True))
            workspace, _state, _queue, plan, decision = (
                self.create_review_from_workspace(
                    workspace.directory, state_path, queue_id
                )
            )
            gate = build_voice_quality_gate(workspace, plan, decision)
            (model / "weights.bin").write_bytes(b"other")

            compatibility = inspect_voice_quality_gate(gate, workspace, queue_id)

        self.assertEqual(compatibility.status, "new_review")
        self.assertIn("model_control", compatibility.differences)

    def test_rejected_decision_and_tampered_gate_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state, queue_id, plan, _decision = self.create_review(root)
            rejected = build_cohort_review_decision(
                plan,
                plan.document["cohorts"][0]["cohort_id"],
                "rejected",
                reviewed_queue_ids=[queue_id],
            )
            with self.assertRaisesRegex(VoiceQualityGateError, "accepted"):
                build_voice_quality_gate(workspace, plan, rejected)

            accepted = build_cohort_review_decision(
                plan,
                plan.document["cohorts"][0]["cohort_id"],
                "accepted",
                reviewed_queue_ids=[queue_id],
            )
            gate = build_voice_quality_gate(workspace, plan, accepted)
            output = root / "gate.json"
            write_voice_quality_gate(gate, output)
            document = json.loads(output.read_text())
            document["identity"]["generation_profile"] = "changed"
            output.write_text(json.dumps(document))

            with self.assertRaisesRegex(VoiceQualityGateError, "identity"):
                load_voice_quality_gate(output)

    def test_publication_never_replaces_existing_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state, _queue_id, plan, decision = self.create_review(root)
            gate = build_voice_quality_gate(workspace, plan, decision)
            output = root / "gate.json"
            write_voice_quality_gate(gate, output)

            with self.assertRaisesRegex(VoiceQualityGateError, "output exists"):
                write_voice_quality_gate(gate, output)

    def test_cli_publishes_and_checks_without_projecting_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state, queue_id, plan, decision = self.create_review(root)
            plan_path = root / "plan.json"
            decision_path = root / "decision.json"
            gate_path = root / "gate.json"
            write_cohort_review_plan(plan, plan_path)
            write_cohort_review_decision(decision, decision_path)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "voice-quality-gate",
                        str(workspace),
                        str(plan_path),
                        str(decision_path),
                        "--output",
                        str(gate_path),
                    ]
                )
            published = json.loads(stdout.getvalue())
            stdout = StringIO()
            with redirect_stdout(stdout):
                check_exit = authoring_main(
                    [
                        "voice-quality-check",
                        str(gate_path),
                        str(workspace),
                        queue_id,
                    ]
                )
            loaded_gate_id = load_voice_quality_gate(gate_path).gate_id
            check = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(check_exit, 0)
        self.assertEqual(published["gate_id"], loaded_gate_id)
        self.assertEqual(
            check["status"],
            "control_match_story_sample_required",
        )


if __name__ == "__main__":
    unittest.main()

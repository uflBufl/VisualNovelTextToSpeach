import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vntts.authoring.listening as listening
from tests.authoring_fixtures import create_failed_reference_workspace
from tests.source_reference_fixtures import (
    PreviewBackendFactory as _PreviewBackendFactory,
)
from vntts.authoring import reference_render_comparison
from vntts.authoring.failure_reference_audit import publish_failure_reference_audit
from vntts.authoring.listening import load_listening_session
from vntts.authoring.reference_render_comparison import (
    REFERENCE_RENDER_INPUT_SCHEMA,
    REFERENCE_RENDER_INPUT_VERSION,
    ReferenceRenderComparisonError,
    create_reference_render_listening,
    load_reference_render_comparison_document,
    load_reference_render_plan,
    publish_reference_render_comparison,
)


class ReferenceRenderListeningContractTest(unittest.TestCase):
    def _comparison(self, root: Path):
        workspace, queue_id = create_failed_reference_workspace(root)
        audit_root = root / "audit"
        audit = publish_failure_reference_audit(workspace, audit_root, seed=0)
        audit_document = json.loads((audit_root / "audit.json").read_text())
        group = audit_document["groups"][0]
        plan_path = root / "plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "schema": REFERENCE_RENDER_INPUT_SCHEMA,
                    "schema_version": REFERENCE_RENDER_INPUT_VERSION,
                    "audit": str(audit_root),
                    "audit_id": audit.audit_id,
                    "arms": [
                        {
                            "arm_id": f"candidate-{index}",
                            "samples": [
                                {
                                    "queue_id": queue_id,
                                    "case_group_id": group["group_id"],
                                    "candidate_group_id": group["group_id"],
                                    "candidate_id": candidate["candidate_id"],
                                }
                            ],
                        }
                        for index, candidate in enumerate(group["candidates"], 1)
                    ],
                }
            ),
            encoding="utf-8",
        )
        return publish_reference_render_comparison(
            load_reference_render_plan(plan_path),
            root / "comparison",
            backend_factory=_PreviewBackendFactory(),
        )

    def test_public_session_preserves_source_identity_and_empty_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comparison = self._comparison(root)
            output = root / "empty-session"
            output.mkdir()
            session_path = create_reference_render_listening(
                comparison.directory, output, seed=7
            )
            session = load_listening_session(session_path)
            self.assertEqual(session["trial_count"], 1)
            self.assertEqual(session["source_kind"], "model-reports")
            key = json.loads(session_path.with_name(".blind-key.json").read_text())
            self.assertEqual(session["source_sha256"], key["source_sha256"])
            document = load_reference_render_comparison_document(comparison.directory)
            self.assertEqual(
                {Path(source["path"]) for source in key["sources"]},
                {comparison.directory / arm["report"] for arm in document["arms"]},
            )

    def _assert_handoff_change_rejected(self, mutate):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comparison = self._comparison(root)
            document = load_reference_render_comparison_document(comparison.directory)
            captured = reference_render_comparison._create_listening_session_from_captured_reports
            ordinary = listening.create_listening_session_from_reports
            mutated = False

            def mutate_then_create(creator):
                def create(*args, **kwargs):
                    nonlocal mutated
                    mutate(comparison.directory, document)
                    mutated = True
                    return creator(*args, **kwargs)

                return create

            output = root / "session"
            with (
                patch.object(
                    reference_render_comparison,
                    "_create_listening_session_from_captured_reports",
                    mutate_then_create(captured),
                ),
                patch.object(
                    reference_render_comparison,
                    "create_listening_session_from_reports",
                    mutate_then_create(ordinary),
                    create=True,
                ),
                self.assertRaises(ReferenceRenderComparisonError),
            ):
                create_reference_render_listening(comparison.directory, output, seed=7)
            self.assertTrue(mutated)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".session-*")), [])

    def test_changed_comparison_is_rejected_at_handoff(self):
        def mutate(root, _document):
            path = root / "comparison.json"
            payload = json.loads(path.read_text())
            payload["generated_at"] = "changed-after-validation"
            path.write_text(json.dumps(payload))

        self._assert_handoff_change_rejected(mutate)

    def test_changed_control_is_rejected_at_handoff(self):
        def mutate(root, document):
            control = root / document["controls"][0]["audio"]
            control.write_bytes(control.read_bytes() + b"changed")

        self._assert_handoff_change_rejected(mutate)

    def test_changed_report_is_rejected_at_handoff(self):
        def mutate(root, document):
            report = root / document["arms"][0]["report"]
            payload = json.loads(report.read_text())
            payload["model"] = "changed-after-validation"
            report.write_text(json.dumps(payload))

        self._assert_handoff_change_rejected(mutate)


if __name__ == "__main__":
    unittest.main()

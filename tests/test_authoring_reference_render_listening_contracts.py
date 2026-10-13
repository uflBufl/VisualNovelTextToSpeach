import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import vntts.authoring.listening as listening
from tests.authoring_fixtures import create_failed_reference_workspace
from tests.source_reference_fixtures import (
    PreviewBackendFactory as _PreviewBackendFactory,
)
from vntts.authoring import reference_render_comparison
from vntts.authoring.failure_reference_audit import publish_failure_reference_audit
from vntts.authoring.failure_reference_preview import (
    FailureReferencePreviewError,
    FailureReferencePreviewService,
)
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

    def test_plan_rejects_noninteger_and_unsupported_schema_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan_path = Path(temporary) / "plan.json"
            for version in (True, False, 1.0, "1", None, 0, 2):
                with self.subTest(version=version):
                    plan_path.write_text(
                        json.dumps(
                            {
                                "schema": REFERENCE_RENDER_INPUT_SCHEMA,
                                "schema_version": version,
                                "audit": "unused",
                                "audit_id": "a" * 64,
                                "arms": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        ReferenceRenderComparisonError, "Unsupported.*schema"
                    ):
                        load_reference_render_plan(plan_path)

    def test_changed_loaded_plan_is_rejected_before_render_or_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_publish = publish_reference_render_comparison
            original_plan = None

            def mutate_then_publish(plan, output, **kwargs):
                nonlocal original_plan
                original_plan = plan
                plan.arms[0]["samples"][0]["candidate_id"] = plan.arms[1]["samples"][0][
                    "candidate_id"
                ]
                return original_publish(plan, output, **kwargs)

            with (
                patch(
                    f"{__name__}.publish_reference_render_comparison",
                    side_effect=mutate_then_publish,
                ),
                patch.object(
                    reference_render_comparison, "FailureReferencePreviewService"
                ) as service,
                self.assertRaisesRegex(ReferenceRenderComparisonError, "plan changed"),
            ):
                self._comparison(root)

            self.assertIsNotNone(original_plan)
            service.assert_not_called()
            self.assertFalse((root / "comparison").exists())
            self.assertEqual(list(root.glob(".comparison.staging-*")), [])

    def test_publication_uses_detached_plan_when_caller_mutates_loaded_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_publish = publish_reference_render_comparison
            expected = None

            def publish_with_mutating_factory(plan, output, **kwargs):
                nonlocal expected
                expected = [arm["samples"][0]["candidate_id"] for arm in plan.arms]
                factory = kwargs["backend_factory"]

                def mutate_then_create_backend(*args, **factory_kwargs):
                    plan.arms[1]["samples"][0]["candidate_id"] = expected[0]
                    return factory(*args, **factory_kwargs)

                return original_publish(
                    plan, output, backend_factory=mutate_then_create_backend
                )

            with patch(
                f"{__name__}.publish_reference_render_comparison",
                side_effect=publish_with_mutating_factory,
            ):
                comparison = self._comparison(root)

            document = load_reference_render_comparison_document(comparison.directory)
            self.assertEqual(
                [arm["renders"][0]["candidate_id"] for arm in document["arms"]],
                expected,
            )
            self.assertEqual(comparison.complete_pair_count, 1)

    def test_destination_io_failure_precedes_preview_startup(self):
        for boundary in ("destination", "mkdir"):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                original_publish = publish_reference_render_comparison
                mkdir = Path.mkdir
                error = PermissionError("comparison storage is read-only")

                def publish_with_refusal(plan, output, **kwargs):
                    def blocked_mkdir(path, *args, **options):
                        if path.resolve() == output.parent.resolve():
                            raise error
                        return mkdir(path, *args, **options)

                    failure = (
                        patch.object(
                            reference_render_comparison,
                            "no_replace_destination",
                            side_effect=error,
                        )
                        if boundary == "destination"
                        else patch.object(Path, "mkdir", blocked_mkdir)
                    )
                    with failure:
                        return original_publish(plan, output, **kwargs)

                with (
                    patch(
                        f"{__name__}.publish_reference_render_comparison",
                        side_effect=publish_with_refusal,
                    ),
                    patch.object(
                        reference_render_comparison, "FailureReferencePreviewService"
                    ) as service,
                    self.assertRaises(ReferenceRenderComparisonError) as caught,
                ):
                    self._comparison(root)
                self.assertIs(caught.exception.__cause__, error)
                service.assert_not_called()
                self.assertFalse((root / "comparison").exists())
                self.assertEqual(list(root.glob(".comparison.staging-*")), [])

    def test_publication_race_preserves_destination_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "comparison"
            rename = reference_render_comparison.rename_directory_no_replace

            def create_competitor_then_rename(staging, destination):
                destination.mkdir()
                (destination / "sentinel").write_bytes(b"competitor")
                return rename(staging, destination)

            with (
                patch.object(
                    reference_render_comparison,
                    "rename_directory_no_replace",
                    create_competitor_then_rename,
                ),
                self.assertRaisesRegex(
                    ReferenceRenderComparisonError, "destination already exists"
                ),
            ):
                self._comparison(root)

            self.assertEqual((output / "sentinel").read_bytes(), b"competitor")
            self.assertEqual(list(output.iterdir()), [output / "sentinel"])
            self.assertEqual(list(root.glob(".comparison.staging-*")), [])

    def test_render_error_survives_preview_shutdown_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = FailureReferencePreviewError("render failed")
            services = []

            def create_service(*args, **kwargs):
                service = FailureReferencePreviewService(*args, **kwargs)
                original_close = service.close

                def close_then_fail():
                    original_close()
                    raise RuntimeError("shutdown failed")

                service.close = Mock(side_effect=close_then_fail)
                services.append(service)
                return service

            with (
                patch.object(
                    reference_render_comparison,
                    "_render_comparison_arms",
                    side_effect=primary,
                ),
                patch.object(
                    reference_render_comparison,
                    "FailureReferencePreviewService",
                    side_effect=create_service,
                ),
                self.assertRaisesRegex(
                    ReferenceRenderComparisonError, "render failed"
                ) as raised,
            ):
                self._comparison(root)

            self.assertIs(raised.exception.__cause__, primary)
            self.assertIn(
                "Reference preview shutdown failed: shutdown failed", primary.__notes__
            )
            services[0].close.assert_called_once_with()
            self.assertTrue(services[0]._closed)
            self.assertFalse(services[0]._root.exists())
            self.assertFalse((root / "comparison").exists())
            self.assertEqual(list(root.glob(".comparison.staging-*")), [])

    def test_shutdown_failure_after_publication_stays_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            services = []

            def create_service(*args, **kwargs):
                service = FailureReferencePreviewService(*args, **kwargs)
                original_close = service.close

                def close_then_fail():
                    original_close()
                    raise RuntimeError("shutdown failed")

                service.close = Mock(side_effect=close_then_fail)
                services.append(service)
                return service

            with (
                patch.object(
                    reference_render_comparison,
                    "FailureReferencePreviewService",
                    side_effect=create_service,
                ),
                self.assertRaisesRegex(RuntimeError, "shutdown failed"),
            ):
                self._comparison(root)

            services[0].close.assert_called_once_with()
            self.assertFalse(services[0]._root.exists())
            load_reference_render_comparison_document(root / "comparison")
            self.assertEqual(list(root.glob(".comparison.staging-*")), [])

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

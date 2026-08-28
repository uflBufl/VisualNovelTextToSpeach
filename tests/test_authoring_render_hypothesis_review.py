import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from tests.test_authoring_failure_reference_audit import (
    _PreviewBackendFactory,
    create_failed_reference_workspace,
)
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.failure_reference_audit import (
    load_failure_reference_decisions,
    publish_failure_reference_audit,
)
from vntts.authoring.failure_reference_binding import (
    load_failure_reference_binding_document,
    publish_failure_reference_binding,
)
from vntts.authoring.reference_render_comparison import (
    REFERENCE_RENDER_INPUT_SCHEMA,
    REFERENCE_RENDER_INPUT_VERSION,
    load_reference_render_plan,
    publish_reference_render_comparison,
)
from vntts.authoring.render_hypothesis_review import (
    RenderHypothesisReviewError,
    import_accepted_render_hypothesis,
    load_render_hypothesis_review,
    publish_render_hypothesis_review,
    record_render_hypothesis_decision,
)


def canonical_sha256(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def write_comparison(root, *, reference_format="wav"):
    root.mkdir()
    controls = root / "controls"
    controls.mkdir()
    reference = controls / f"reference.{reference_format}"
    if reference_format == "wav":
        write_pcm16_wav(reference, np.full(1_200, 0.1, dtype=np.float32), 24_000)
    else:
        reference.write_bytes(b"OggS\x00checksum-bound-fixture")
    reference_sha = sha256_file(reference)
    text_sha = hashlib.sha256(b"A measured test line.").hexdigest()
    queue_id = "reverse1999:1:2:" + text_sha[:16]
    reports = []
    arms = []
    for index, arm_id in enumerate(("reference-02", "reference-03"), start=1):
        arm_root = root / "arms" / arm_id
        (arm_root / "audio").mkdir(parents=True)
        base = {
            "id": queue_id,
            "line_id": "reverse1999:1:2",
            "text": "A measured test line.",
            "text_sha256": text_sha,
            "case_group_id": "b" * 64,
            "candidate_group_id": "c" * 64,
            "candidate_id": "candidate-one",
            "reference_sha256": reference_sha,
        }
        if index == 1:
            audio = arm_root / "audio/0001.wav"
            write_pcm16_wav(audio, np.full(2_400, 0.2, dtype=np.float32), 24_000)
            render = {
                **base,
                "outcome": "complete",
                "audio": "audio/0001.wav",
                "audio_sha256": sha256_file(audio),
                "sample_rate": 24_000,
                "backend": "moss-tts",
                "model": "fixture",
                "generation_profile": "stable",
                "seed": 0,
            }
        else:
            render = {**base, "outcome": "error", "error": "typed limited"}
        report = {
            "schema": "vntts.voice-model-report",
            "schema_version": 1,
            "model_id": arm_id,
            "provider": "reference-render-comparison",
            "backend": "reference-render-comparison",
            "model": "one exact alternative reference per sample",
            "samples": [render],
        }
        report_path = arm_root / "report.json"
        atomic_write_json(report_path, report)
        report_relative = f"arms/{arm_id}/report.json"
        reports.append(report_relative)
        arms.append(
            {
                "arm_id": arm_id,
                "report": report_relative,
                "report_sha256": sha256_file(report_path),
                "complete_count": int(index == 1),
                "failure_count": int(index != 1),
                "renders": [render],
            }
        )
    body = {
        "schema": "vntts.authoring-reference-render-comparison",
        "schema_version": 1,
        "generated_at": "2026-08-27T00:00:00+00:00",
        "input_plan": "/immutable/plan.json",
        "input_plan_sha256": "d" * 64,
        "audit": "/immutable/audit",
        "audit_id": "e" * 64,
        "audit_sha256": "f" * 64,
        "queue_ids": [queue_id],
        "controls": [
            {
                "group_id": "c" * 64,
                "candidate_id": "candidate-one",
                "audio": f"controls/reference.{reference_format}",
                "sha256": reference_sha,
            }
        ],
        "arms": arms,
        "reports": reports,
        "complete_pair_queue_ids": [],
    }
    atomic_write_json(
        root / "comparison.json",
        {**body, "comparison_id": canonical_sha256(body)},
    )
    return queue_id


class RenderHypothesisReviewTest(unittest.TestCase):
    def test_accepted_hypothesis_imports_into_fresh_audit_and_binding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = create_failed_reference_workspace(root)
            source_audit = root / "source-audit"
            source = publish_failure_reference_audit(
                workspace, source_audit, seed=0, queue_ids=(queue_id,)
            )
            source_document = json.loads((source_audit / "audit.json").read_text())
            source_group = source_document["groups"][0]
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema": REFERENCE_RENDER_INPUT_SCHEMA,
                        "schema_version": REFERENCE_RENDER_INPUT_VERSION,
                        "audit": str(source_audit),
                        "audit_id": source.audit_id,
                        "arms": [
                            {
                                "arm_id": f"reference-{index}",
                                "samples": [
                                    {
                                        "queue_id": queue_id,
                                        "case_group_id": source_group["group_id"],
                                        "candidate_group_id": source_group["group_id"],
                                        "candidate_id": candidate["candidate_id"],
                                    }
                                ],
                            }
                            for index, candidate in enumerate(
                                source_group["candidates"], start=1
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )
            comparison = publish_reference_render_comparison(
                load_reference_render_plan(plan_path),
                root / "comparison",
                backend_factory=_PreviewBackendFactory(),
            )
            comparison_document = json.loads(
                (comparison.directory / "comparison.json").read_text()
            )
            arm = comparison_document["arms"][0]
            review_root = root / "review"
            publish_render_hypothesis_review(
                comparison.directory, queue_id, arm["arm_id"], review_root
            )
            record_render_hypothesis_decision(review_root, "accept_hypothesis")
            fresh_audit = root / "fresh-audit"
            publish_failure_reference_audit(
                workspace, fresh_audit, seed=19, queue_ids=(queue_id,)
            )
            rejected_review = root / "rejected-review"
            publish_render_hypothesis_review(
                comparison.directory,
                queue_id,
                arm["arm_id"],
                rejected_review,
            )
            record_render_hypothesis_decision(rejected_review, "need_different")
            with self.assertRaisesRegex(
                RenderHypothesisReviewError, "must be accepted"
            ):
                import_accepted_render_hypothesis(
                    fresh_audit,
                    comparison.directory,
                    rejected_review,
                    queue_id,
                )

            imported = import_accepted_render_hypothesis(
                fresh_audit, comparison.directory, review_root, queue_id
            )
            repeated = import_accepted_render_hypothesis(
                fresh_audit, comparison.directory, review_root, queue_id
            )
            binding_root = root / "binding"
            publish_failure_reference_binding(fresh_audit, binding_root)
            binding = load_failure_reference_binding_document(binding_root)
            decision = load_failure_reference_decisions(fresh_audit)["decisions"][0]

            self.assertTrue(imported.created)
            self.assertFalse(repeated.created)
            self.assertEqual(imported.decision_set_id, repeated.decision_set_id)
            self.assertEqual(
                decision["selection_authority"]["schema"],
                "vntts.authoring-render-hypothesis-selection",
            )
            self.assertEqual(
                binding["groups"][0]["selection_authority"],
                decision["selection_authority"],
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = authoring_main(
                    [
                        "render-hypothesis-review-import",
                        str(fresh_audit),
                        str(comparison.directory),
                        str(review_root),
                        queue_id,
                    ]
                )
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(stdout.getvalue())["created"])

    def test_publish_load_and_decide_are_self_contained(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            comparison = root / "comparison"
            queue_id = write_comparison(comparison)
            output = root / "review"
            published = publish_render_hypothesis_review(
                comparison, queue_id, "reference-02", output
            )
            self.assertEqual(published.queue_id, queue_id)
            self.assertIsNone(published.decision)
            self.assertTrue(published.reference.is_file())
            self.assertTrue(published.result.is_file())

            decided = record_render_hypothesis_decision(output, "accept_hypothesis")
            self.assertEqual(decided.decision, "accept_hypothesis")
            repeated = record_render_hypothesis_decision(output, "accept_hypothesis")
            self.assertEqual(repeated.decision, "accept_hypothesis")
            with self.assertRaisesRegex(RenderHypothesisReviewError, "already decided"):
                record_render_hypothesis_decision(output, "need_different")

    def test_rejects_incomplete_arm_and_ambiguous_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            comparison = root / "comparison"
            queue_id = write_comparison(comparison)
            with self.assertRaisesRegex(
                RenderHypothesisReviewError, "one complete exact"
            ):
                publish_render_hypothesis_review(
                    comparison, queue_id, "reference-03", root / "incomplete"
                )

            document = json.loads(
                (comparison / "comparison.json").read_text(encoding="utf-8")
            )
            body = {
                key: value for key, value in document.items() if key != "comparison_id"
            }
            body["controls"].append(dict(body["controls"][0]))
            atomic_write_json(
                comparison / "comparison.json",
                {**body, "comparison_id": canonical_sha256(body)},
            )
            with self.assertRaisesRegex(
                RenderHypothesisReviewError, "absent or ambiguous"
            ):
                publish_render_hypothesis_review(
                    comparison, queue_id, "reference-02", root / "ambiguous"
                )

    def test_tampered_result_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            comparison = root / "comparison"
            queue_id = write_comparison(comparison)
            output = root / "review"
            publish_render_hypothesis_review(
                comparison, queue_id, "reference-02", output
            )
            (output / "audio/result.wav").write_bytes(b"changed")
            with self.assertRaisesRegex(
                RenderHypothesisReviewError, "artifact changed"
            ):
                load_render_hypothesis_review(output)

    def test_reference_keeps_its_original_reviewable_format(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            comparison = root / "comparison"
            queue_id = write_comparison(comparison, reference_format="ogg")
            review = publish_render_hypothesis_review(
                comparison, queue_id, "reference-02", root / "review"
            )
            self.assertEqual(review.reference.suffix, ".ogg")
            self.assertEqual(
                review.reference.read_bytes(), b"OggS\x00checksum-bound-fixture"
            )

    def test_cli_publish_status_and_decide(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            comparison = root / "comparison"
            queue_id = write_comparison(comparison)
            output = root / "review"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = authoring_main(
                    [
                        "render-hypothesis-review-publish",
                        str(comparison),
                        queue_id,
                        "reference-02",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIsNone(json.loads(stdout.getvalue())["decision"])

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = authoring_main(["render-hypothesis-review-status", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["queue_id"], queue_id)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = authoring_main(
                    [
                        "render-hypothesis-review-decide",
                        str(output),
                        "need_different",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(stdout.getvalue())["decision"], "need_different"
            )


if __name__ == "__main__":
    unittest.main()

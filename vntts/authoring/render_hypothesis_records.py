"""Leaf validation for self-contained render-hypothesis review records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    AuthoritySnapshot,
    canonical_document_sha256,
    capture_authority_file,
)
from vntts.authoring.workspace_foundation import contained_regular_file


class RenderHypothesisRecordError(RuntimeError):
    """A persisted review/decision record is malformed or stale."""


@dataclass(frozen=True)
class RenderHypothesisRecord:
    directory: Path
    review: dict
    decision: dict | None
    review_snapshot: AuthoritySnapshot
    decision_snapshot: AuthoritySnapshot | None
    comparison_snapshot: AuthoritySnapshot
    report_snapshot: AuthoritySnapshot
    reference_snapshot: AuthoritySnapshot
    result_snapshot: AuthoritySnapshot

    @property
    def snapshots(self):
        values = [
            self.review_snapshot,
            self.comparison_snapshot,
            self.report_snapshot,
            self.reference_snapshot,
            self.result_snapshot,
        ]
        if self.decision_snapshot is not None:
            values.append(self.decision_snapshot)
        return tuple(values)


def load_render_hypothesis_record(directory):
    """Load and fully validate one review without importing publication code."""
    root = Path(directory).expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        raise RenderHypothesisRecordError(
            f"Render hypothesis review is unavailable: {root}"
        )
    try:
        review_snapshot = capture_authority_file(
            root / "review.json", "render hypothesis review", root=root
        )
        review = review_snapshot.json_document("render hypothesis review")
        comparison_snapshot = capture_authority_file(
            _contained_file(root, review.get("comparison"), "copied comparison"),
            "copied reference render comparison",
            root=root,
        )
        report_snapshot = capture_authority_file(
            _contained_file(root, review.get("arm_report"), "copied report"),
            "copied reference render report",
            root=root,
        )
        reference_snapshot = capture_authority_file(
            _contained_file(root, review.get("reference"), "copied reference"),
            "copied reference audio",
            root=root,
        )
        result_snapshot = capture_authority_file(
            _contained_file(root, review.get("result"), "copied result"),
            "copied render result",
            root=root,
        )
    except AuthoringAuthorityError as error:
        raise RenderHypothesisRecordError(str(error)) from error
    _validate_review(
        review,
        comparison_snapshot,
        report_snapshot,
        reference_snapshot,
        result_snapshot,
    )
    decision_snapshot = None
    decision = None
    decision_path = root / "decision.json"
    if decision_path.exists() or decision_path.is_symlink():
        try:
            decision_snapshot = capture_authority_file(
                decision_path, "render hypothesis decision", root=root
            )
            decision = decision_snapshot.json_document("render hypothesis decision")
        except AuthoringAuthorityError as error:
            raise RenderHypothesisRecordError(str(error)) from error
        _validate_decision(decision, review, review_snapshot.sha256)
    return RenderHypothesisRecord(
        root,
        review,
        decision,
        review_snapshot,
        decision_snapshot,
        comparison_snapshot,
        report_snapshot,
        reference_snapshot,
        result_snapshot,
    )


def _validate_review(review, comparison, report, reference, result):
    required = {
        "schema",
        "schema_version",
        "review_id",
        "created_at",
        "comparison",
        "comparison_id",
        "comparison_sha256",
        "arm_id",
        "arm_report",
        "arm_report_sha256",
        "queue_id",
        "line_id",
        "text",
        "text_sha256",
        "candidate_group_id",
        "candidate_id",
        "reference",
        "reference_sha256",
        "reference_format",
        "result",
        "result_sha256",
        "backend",
        "model",
        "generation_profile",
        "seed",
    }
    if (
        not isinstance(review, dict)
        or set(review) != required
        or review.get("schema") != "vntts.authoring-render-hypothesis-review"
        or review.get("schema_version") != 1
    ):
        raise RenderHypothesisRecordError("Render hypothesis review is malformed")
    identity = {
        key: value
        for key, value in review.items()
        if key
        not in {
            "review_id",
            "created_at",
            "comparison",
            "arm_report",
            "reference",
            "result",
        }
    }
    if review["review_id"] != canonical_document_sha256(identity):
        raise RenderHypothesisRecordError("Render hypothesis review ID changed")
    if (
        comparison.sha256 != review["comparison_sha256"]
        or report.sha256 != review["arm_report_sha256"]
        or reference.sha256 != review["reference_sha256"]
        or result.sha256 != review["result_sha256"]
    ):
        raise RenderHypothesisRecordError("Render hypothesis artifact changed")
    try:
        comparison_document = comparison.json_document("copied comparison")
        report_document = report.json_document("copied report")
        result_info = probe_pcm16_mono_wav(result.path)
    except (AuthoringAuthorityError, OSError, Pcm16MonoWavError) as error:
        raise RenderHypothesisRecordError(str(error)) from error
    arm = next(
        (
            value
            for value in comparison_document.get("arms", [])
            if isinstance(value, dict) and value.get("arm_id") == review["arm_id"]
        ),
        None,
    )
    render = next(
        (
            value
            for value in (arm or {}).get("renders", [])
            if isinstance(value, dict)
            and value.get("id") == review["queue_id"]
            and value.get("outcome") == "complete"
        ),
        None,
    )
    report_sample = next(
        (
            value
            for value in report_document.get("samples", [])
            if isinstance(value, dict) and value.get("id") == review["queue_id"]
        ),
        None,
    )
    if (
        comparison_document.get("comparison_id") != review["comparison_id"]
        or render is None
        or report_sample != render
    ):
        raise RenderHypothesisRecordError("Render hypothesis record changed")
    for field in (
        "line_id",
        "text",
        "text_sha256",
        "candidate_group_id",
        "candidate_id",
        "reference_sha256",
        "audio_sha256",
        "backend",
        "model",
        "generation_profile",
        "seed",
    ):
        review_field = "result_sha256" if field == "audio_sha256" else field
        if render.get(field) != review.get(review_field):
            raise RenderHypothesisRecordError(f"Render hypothesis {field} changed")
    if (
        not reference.payload
        or result_info.sample_count <= 0
        or reference.path.suffix.lower()
        != "." + _required_text(review["reference_format"], "reference format")
    ):
        raise RenderHypothesisRecordError("Render hypothesis audio changed")


def _validate_decision(decision, review, review_sha256):
    if (
        not isinstance(decision, dict)
        or set(decision)
        != {
            "schema",
            "schema_version",
            "review_id",
            "review_sha256",
            "reference_sha256",
            "result_sha256",
            "decision",
            "reviewed_at",
        }
        or decision.get("schema") != "vntts.authoring-render-hypothesis-decision"
        or decision.get("schema_version") != 1
        or decision.get("review_id") != review["review_id"]
        or decision.get("review_sha256") != review_sha256
        or decision.get("reference_sha256") != review["reference_sha256"]
        or decision.get("result_sha256") != review["result_sha256"]
        or decision.get("decision") not in {"accept_hypothesis", "need_different"}
    ):
        raise RenderHypothesisRecordError(
            "Render hypothesis decision is malformed or stale"
        )


def _contained_file(root, value, label):
    text = _required_text(value, label)
    return contained_regular_file(
        root, text, label, error_type=RenderHypothesisRecordError
    )


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RenderHypothesisRecordError(f"{label.capitalize()} must be text")
    return value


__all__ = [
    "RenderHypothesisRecord",
    "RenderHypothesisRecordError",
    "load_render_hypothesis_record",
]

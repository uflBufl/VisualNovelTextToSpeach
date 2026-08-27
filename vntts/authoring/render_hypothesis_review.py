"""Checksum-bound review for one unmatched alternative-reference render."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    assert_authority_snapshot,
    canonical_document_sha256,
    capture_authority_file,
    write_json_document_no_replace,
)
from vntts.authoring.failure_reference_audit import (
    FailureReferenceAuditError,
    load_failure_reference_audit,
    load_failure_reference_decisions,
    record_failure_reference_decision,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)
from vntts.authoring.reference_render_comparison import (
    ReferenceRenderComparisonError,
    load_reference_render_comparison_document,
)

RENDER_HYPOTHESIS_REVIEW_SCHEMA = "vntts.authoring-render-hypothesis-review"
RENDER_HYPOTHESIS_REVIEW_VERSION = 1
RENDER_HYPOTHESIS_DECISION_SCHEMA = "vntts.authoring-render-hypothesis-decision"
RENDER_HYPOTHESIS_DECISION_VERSION = 1
RENDER_HYPOTHESIS_DECISIONS = frozenset({"accept_hypothesis", "need_different"})


class RenderHypothesisReviewError(RuntimeError):
    """A single-render hypothesis review is invalid or cannot be published."""


@dataclass(frozen=True)
class RenderHypothesisReview:
    directory: Path
    review_id: str
    queue_id: str
    arm_id: str
    reference: Path
    reference_sha256: str
    result: Path
    result_sha256: str
    decision: str | None

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "review_id": self.review_id,
            "queue_id": self.queue_id,
            "arm_id": self.arm_id,
            "reference": str(self.reference),
            "reference_sha256": self.reference_sha256,
            "result": str(self.result),
            "result_sha256": self.result_sha256,
            "decision": self.decision,
        }


@dataclass(frozen=True)
class RenderHypothesisSelection:
    audit_directory: Path
    audit_id: str
    group_id: str
    candidate_id: str
    queue_id: str
    review_id: str
    selected_reference_sha256: str
    decision_set_id: str
    created: bool

    def to_dict(self):
        return {
            "audit_directory": str(self.audit_directory),
            "audit_id": self.audit_id,
            "group_id": self.group_id,
            "candidate_id": self.candidate_id,
            "queue_id": self.queue_id,
            "review_id": self.review_id,
            "selected_reference_sha256": self.selected_reference_sha256,
            "decision_set_id": self.decision_set_id,
            "created": self.created,
        }


def publish_render_hypothesis_review(
    comparison_directory,
    queue_id,
    arm_id,
    output,
):
    """Snapshot one complete unmatched render and its exact reference control."""
    supplied = Path(comparison_directory).expanduser()
    if supplied.is_symlink():
        raise RenderHypothesisReviewError("Reference render comparison is a symlink")
    comparison_root = supplied.resolve()
    output = Path(output).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise RenderHypothesisReviewError(
            f"Render hypothesis review output exists: {output}"
        )
    try:
        validated = load_reference_render_comparison_document(comparison_root)
        comparison_snapshot = capture_authority_file(
            comparison_root / "comparison.json",
            "reference render comparison",
            root=comparison_root,
        )
        comparison = comparison_snapshot.json_document("reference render comparison")
    except (AuthoringAuthorityError, ReferenceRenderComparisonError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    if comparison != validated:
        raise RenderHypothesisReviewError(
            "Reference render comparison changed while it was loaded"
        )
    queue_id = _required_text(queue_id, "queue ID")
    arm_id = _required_text(arm_id, "arm ID")
    arm = next(
        (value for value in comparison["arms"] if value.get("arm_id") == arm_id),
        None,
    )
    if arm is None:
        raise RenderHypothesisReviewError(f"Reference render arm is absent: {arm_id}")
    renders = [
        value
        for value in arm["renders"]
        if value.get("id") == queue_id and value.get("outcome") == "complete"
    ]
    if len(renders) != 1:
        raise RenderHypothesisReviewError(
            "Render hypothesis requires one complete exact queue item"
        )
    render = renders[0]
    controls = [
        value
        for value in comparison["controls"]
        if value.get("sha256") == render.get("reference_sha256")
    ]
    if len(controls) != 1:
        raise RenderHypothesisReviewError(
            "Render hypothesis reference control is absent or ambiguous"
        )
    control = controls[0]
    reference_suffix = Path(
        _required_text(control.get("audio"), "reference control path")
    ).suffix.lower()
    if reference_suffix not in {".flac", ".ogg", ".wav"}:
        raise RenderHypothesisReviewError(
            "Render hypothesis reference format is not reviewable"
        )
    try:
        report_snapshot = capture_authority_file(
            _contained_file(comparison_root, arm.get("report"), "arm report"),
            "reference render arm report",
            root=comparison_root,
        )
        reference_snapshot = capture_authority_file(
            _contained_file(comparison_root, control.get("audio"), "reference control"),
            "reference control",
            root=comparison_root,
        )
        result_snapshot = capture_authority_file(
            _contained_file(
                comparison_root / "arms" / arm_id,
                render.get("audio"),
                "render result",
            ),
            "render result",
            root=comparison_root,
        )
    except AuthoringAuthorityError as error:
        raise RenderHypothesisReviewError(str(error)) from error
    if (
        report_snapshot.sha256 != arm.get("report_sha256")
        or reference_snapshot.sha256 != render.get("reference_sha256")
        or result_snapshot.sha256 != render.get("audio_sha256")
    ):
        raise RenderHypothesisReviewError(
            "Render hypothesis source hashes do not match the comparison"
        )

    identity = {
        "schema": RENDER_HYPOTHESIS_REVIEW_SCHEMA,
        "schema_version": RENDER_HYPOTHESIS_REVIEW_VERSION,
        "comparison_id": comparison["comparison_id"],
        "comparison_sha256": comparison_snapshot.sha256,
        "arm_id": arm_id,
        "arm_report_sha256": report_snapshot.sha256,
        "queue_id": queue_id,
        "line_id": render["line_id"],
        "text": render["text"],
        "text_sha256": render["text_sha256"],
        "candidate_group_id": render["candidate_group_id"],
        "candidate_id": render["candidate_id"],
        "reference_sha256": reference_snapshot.sha256,
        "reference_format": reference_suffix[1:],
        "result_sha256": result_snapshot.sha256,
        "backend": render["backend"],
        "model": render["model"],
        "generation_profile": render["generation_profile"],
        "seed": render["seed"],
    }
    review_id = canonical_document_sha256(identity)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    try:
        (staging / "audio").mkdir(parents=True)
        (staging / "comparison.json").write_bytes(comparison_snapshot.payload)
        (staging / "arm-report.json").write_bytes(report_snapshot.payload)
        reference_relative = f"audio/reference{reference_suffix}"
        (staging / reference_relative).write_bytes(reference_snapshot.payload)
        (staging / "audio/result.wav").write_bytes(result_snapshot.payload)
        review = {
            **identity,
            "review_id": review_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "comparison": "comparison.json",
            "arm_report": "arm-report.json",
            "reference": reference_relative,
            "result": "audio/result.wav",
        }
        atomic_write_json(staging / "review.json", review, sort_keys=True)
        load_render_hypothesis_review(staging)
        for snapshot, label in (
            (comparison_snapshot, "reference render comparison"),
            (report_snapshot, "reference render arm report"),
            (reference_snapshot, "reference control"),
            (result_snapshot, "render result"),
        ):
            assert_authority_snapshot(snapshot, label)
        try:
            rename_directory_no_replace(staging, output)
        except (AtomicPublicationError, OSError) as error:
            raise RenderHypothesisReviewError(str(error)) from error
        return load_render_hypothesis_review(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def load_render_hypothesis_review(directory):
    """Load and verify one self-contained render hypothesis review."""
    directory = Path(directory).expanduser().resolve()
    if directory.is_symlink() or not directory.is_dir():
        raise RenderHypothesisReviewError(
            f"Render hypothesis review is unavailable: {directory}"
        )
    try:
        review_snapshot = capture_authority_file(
            directory / "review.json", "render hypothesis review", root=directory
        )
        review = review_snapshot.json_document("render hypothesis review")
        comparison_snapshot = capture_authority_file(
            _contained_file(directory, review.get("comparison"), "copied comparison"),
            "copied reference render comparison",
            root=directory,
        )
        report_snapshot = capture_authority_file(
            _contained_file(directory, review.get("arm_report"), "copied report"),
            "copied reference render report",
            root=directory,
        )
        reference_snapshot = capture_authority_file(
            _contained_file(directory, review.get("reference"), "copied reference"),
            "copied reference audio",
            root=directory,
        )
        result_snapshot = capture_authority_file(
            _contained_file(directory, review.get("result"), "copied result"),
            "copied render result",
            root=directory,
        )
    except AuthoringAuthorityError as error:
        raise RenderHypothesisReviewError(str(error)) from error
    _validate_review_document(
        review,
        comparison_snapshot,
        report_snapshot,
        reference_snapshot,
        result_snapshot,
    )
    decision = None
    decision_path = directory / "decision.json"
    if decision_path.exists() or decision_path.is_symlink():
        try:
            decision_snapshot = capture_authority_file(
                decision_path, "render hypothesis decision", root=directory
            )
            decision_document = decision_snapshot.json_document(
                "render hypothesis decision"
            )
        except AuthoringAuthorityError as error:
            raise RenderHypothesisReviewError(str(error)) from error
        decision = _validate_decision_document(
            decision_document, review, review_snapshot.sha256
        )["decision"]
        assert_authority_snapshot(decision_snapshot, "render hypothesis decision")
    for snapshot, label in (
        (review_snapshot, "render hypothesis review"),
        (comparison_snapshot, "copied reference render comparison"),
        (report_snapshot, "copied reference render report"),
        (reference_snapshot, "copied reference audio"),
        (result_snapshot, "copied render result"),
    ):
        assert_authority_snapshot(snapshot, label)
    return RenderHypothesisReview(
        directory=directory,
        review_id=review["review_id"],
        queue_id=review["queue_id"],
        arm_id=review["arm_id"],
        reference=reference_snapshot.path,
        reference_sha256=reference_snapshot.sha256,
        result=result_snapshot.path,
        result_sha256=result_snapshot.sha256,
        decision=decision,
    )


def record_render_hypothesis_decision(directory, decision):
    """Record one terminal hypothesis verdict without changing generation state."""
    decision = str(decision).strip()
    if decision not in RENDER_HYPOTHESIS_DECISIONS:
        raise RenderHypothesisReviewError(
            "Render hypothesis decision must be accept_hypothesis or need_different"
        )
    review = load_render_hypothesis_review(directory)
    decision_path = review.directory / "decision.json"
    if decision_path.exists() or decision_path.is_symlink():
        current = load_render_hypothesis_review(review.directory)
        if current.decision == decision:
            return current
        raise RenderHypothesisReviewError(
            f"Render hypothesis review is already decided: {current.decision}"
        )
    try:
        review_snapshot = capture_authority_file(
            review.directory / "review.json",
            "render hypothesis review",
            root=review.directory,
        )
        document = review_snapshot.json_document("render hypothesis review")
        comparison_snapshot = capture_authority_file(
            _contained_file(
                review.directory, document.get("comparison"), "copied comparison"
            ),
            "copied reference render comparison",
            root=review.directory,
        )
        report_snapshot = capture_authority_file(
            _contained_file(
                review.directory, document.get("arm_report"), "copied report"
            ),
            "copied reference render report",
            root=review.directory,
        )
        reference_snapshot = capture_authority_file(
            _contained_file(
                review.directory, document.get("reference"), "copied reference"
            ),
            "copied reference audio",
            root=review.directory,
        )
        result_snapshot = capture_authority_file(
            _contained_file(review.directory, document.get("result"), "copied result"),
            "copied render result",
            root=review.directory,
        )
        _validate_review_document(
            document,
            comparison_snapshot,
            report_snapshot,
            reference_snapshot,
            result_snapshot,
        )
        if document.get("review_id") != review.review_id:
            raise RenderHypothesisReviewError(
                "Render hypothesis review authority changed"
            )
        decision_document = {
            "schema": RENDER_HYPOTHESIS_DECISION_SCHEMA,
            "schema_version": RENDER_HYPOTHESIS_DECISION_VERSION,
            "review_id": review.review_id,
            "review_sha256": review_snapshot.sha256,
            "reference_sha256": reference_snapshot.sha256,
            "result_sha256": result_snapshot.sha256,
            "decision": decision,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        assert_authority_snapshot(review_snapshot, "render hypothesis review")
        assert_authority_snapshot(
            comparison_snapshot, "copied reference render comparison"
        )
        assert_authority_snapshot(report_snapshot, "copied reference render report")
        assert_authority_snapshot(reference_snapshot, "copied reference audio")
        assert_authority_snapshot(result_snapshot, "copied render result")
        write_json_document_no_replace(
            decision_path, decision_document, "render hypothesis decision"
        )
    except AuthoringAuthorityError as error:
        if (
            decision_path.exists()
            and load_render_hypothesis_review(review.directory).decision == decision
        ):
            return load_render_hypothesis_review(review.directory)
        raise RenderHypothesisReviewError(str(error)) from error
    return load_render_hypothesis_review(review.directory)


def import_accepted_render_hypothesis(
    audit_directory,
    comparison_directory,
    review_directory,
    queue_id,
):
    """Bind one accepted single-render hypothesis to one fresh exact audit."""
    queue_id = _required_text(queue_id, "queue ID")
    audit_directory = _safe_directory(audit_directory, "fresh failure audit")
    comparison_directory = _safe_directory(
        comparison_directory, "reference render comparison"
    )
    review_directory = _safe_directory(review_directory, "render hypothesis review")
    try:
        fresh = load_failure_reference_audit(audit_directory)
        review = load_render_hypothesis_review(review_directory)
        comparison = load_reference_render_comparison_document(comparison_directory)
    except (FailureReferenceAuditError, ReferenceRenderComparisonError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    if review.decision != "accept_hypothesis" or review.queue_id != queue_id:
        raise RenderHypothesisReviewError(
            "Render hypothesis must be accepted for the exact queue item"
        )
    try:
        snapshots = {
            "fresh_audit": capture_authority_file(
                audit_directory / "audit.json",
                "fresh failure audit",
                root=audit_directory,
            ),
            "fresh_key": capture_authority_file(
                audit_directory / ".blind-key.json",
                "fresh failure audit key",
                root=audit_directory,
            ),
            "comparison": capture_authority_file(
                comparison_directory / "comparison.json",
                "reference render comparison",
                root=comparison_directory,
            ),
            "review": capture_authority_file(
                review_directory / "review.json",
                "render hypothesis review",
                root=review_directory,
            ),
            "decision": capture_authority_file(
                review_directory / "decision.json",
                "render hypothesis decision",
                root=review_directory,
            ),
        }
        fresh_document = snapshots["fresh_audit"].json_document("fresh failure audit")
        fresh_key = snapshots["fresh_key"].json_document("fresh failure audit key")
        review_document = snapshots["review"].json_document("render hypothesis review")
        decision_document = snapshots["decision"].json_document(
            "render hypothesis decision"
        )
        snapshots["review_comparison"] = capture_authority_file(
            _contained_file(
                review_directory,
                review_document.get("comparison"),
                "copied reference render comparison",
            ),
            "copied reference render comparison",
            root=review_directory,
        )
        snapshots["review_arm_report"] = capture_authority_file(
            _contained_file(
                review_directory,
                review_document.get("arm_report"),
                "copied reference render report",
            ),
            "copied reference render report",
            root=review_directory,
        )
        snapshots["review_reference"] = capture_authority_file(
            _contained_file(
                review_directory,
                review_document.get("reference"),
                "copied reference audio",
            ),
            "copied reference audio",
            root=review_directory,
        )
        snapshots["review_result"] = capture_authority_file(
            _contained_file(
                review_directory,
                review_document.get("result"),
                "copied render result",
            ),
            "copied render result",
            root=review_directory,
        )
        _validate_review_document(
            review_document,
            snapshots["review_comparison"],
            snapshots["review_arm_report"],
            snapshots["review_reference"],
            snapshots["review_result"],
        )
        _validate_decision_document(
            decision_document,
            review_document,
            snapshots["review"].sha256,
        )
        exact_fresh = load_failure_reference_audit(audit_directory)
        exact_review = load_render_hypothesis_review(review_directory)
        exact_comparison = load_reference_render_comparison_document(
            comparison_directory
        )
    except (
        AuthoringAuthorityError,
        FailureReferenceAuditError,
        ReferenceRenderComparisonError,
    ) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    if (
        fresh.audit_id != exact_fresh.audit_id
        or fresh.audit_id != fresh_document.get("audit_id")
        or review.review_id != exact_review.review_id
        or review.review_id != review_document.get("review_id")
        or decision_document.get("decision") != "accept_hypothesis"
        or decision_document.get("review_id") != review.review_id
        or comparison != exact_comparison
        or comparison
        != snapshots["comparison"].json_document("reference render comparison")
        or comparison.get("comparison_id") != review_document.get("comparison_id")
        or snapshots["comparison"].sha256 != review_document.get("comparison_sha256")
    ):
        raise RenderHypothesisReviewError(
            "Accepted render hypothesis authority changed"
        )
    arm = next(
        (value for value in comparison["arms"] if value.get("arm_id") == review.arm_id),
        None,
    )
    selected_render = next(
        (
            value
            for value in (arm or {}).get("renders", [])
            if value.get("id") == queue_id and value.get("outcome") == "complete"
        ),
        None,
    )
    if (
        selected_render is None
        or selected_render.get("audio_sha256") != review.result_sha256
        or selected_render.get("reference_sha256") != review.reference_sha256
        or selected_render.get("text_sha256") != review_document.get("text_sha256")
    ):
        raise RenderHypothesisReviewError(
            "Accepted render no longer matches its comparison"
        )
    source_audit_directory = _source_audit_directory(
        comparison_directory, comparison.get("audit")
    )
    try:
        source = load_failure_reference_audit(source_audit_directory)
        snapshots["source_audit"] = capture_authority_file(
            source_audit_directory / "audit.json",
            "source failure audit",
            root=source_audit_directory,
        )
        snapshots["source_key"] = capture_authority_file(
            source_audit_directory / ".blind-key.json",
            "source failure audit key",
            root=source_audit_directory,
        )
        source_document = snapshots["source_audit"].json_document(
            "source failure audit"
        )
        source_key = snapshots["source_key"].json_document("source failure audit key")
        exact_source = load_failure_reference_audit(source_audit_directory)
    except (AuthoringAuthorityError, FailureReferenceAuditError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    if (
        source.audit_id != exact_source.audit_id
        or source.audit_id != comparison.get("audit_id")
        or source.audit_id != source_document.get("audit_id")
        or snapshots["source_audit"].sha256 != comparison.get("audit_sha256")
    ):
        raise RenderHypothesisReviewError(
            "Accepted render source audit authority changed"
        )
    source_group_id = selected_render.get("candidate_group_id")
    source_candidate_id = selected_render.get("candidate_id")
    source_group = _group_by_id(source_document, source_group_id, "source audit")
    source_private_group = _group_by_id(source_key, source_group_id, "source audit key")
    source_candidate = _candidate_by_id(
        source_group, source_candidate_id, "source audit"
    )
    source_private_candidate = _candidate_by_id(
        source_private_group, source_candidate_id, "source audit key"
    )
    if (
        source_candidate.get("sha256") != review.reference_sha256
        or source_private_candidate.get("source_sha256") != review.reference_sha256
    ):
        raise RenderHypothesisReviewError(
            "Accepted render reference changed in its source audit"
        )
    fresh_group = _one_group_for_queue(fresh_document, queue_id, "fresh audit")
    fresh_private_group = _group_by_id(
        fresh_key, fresh_group["group_id"], "fresh audit key"
    )
    fresh_candidates = [
        value
        for value in fresh_group["candidates"]
        if value.get("sha256") == review.reference_sha256
    ]
    if len(fresh_candidates) != 1:
        raise RenderHypothesisReviewError(
            "Accepted render reference is absent or ambiguous in the fresh audit"
        )
    fresh_candidate = fresh_candidates[0]
    fresh_private_candidate = _candidate_by_id(
        fresh_private_group, fresh_candidate["candidate_id"], "fresh audit key"
    )
    if (
        fresh_group.get("synthesis_voice_character")
        != source_group.get("synthesis_voice_character")
        or fresh_private_group.get("control_character")
        != source_private_group.get("control_character")
        or fresh_private_group.get("speaker") != source_private_group.get("speaker")
        or fresh_private_candidate.get("source_sha256") != review.reference_sha256
    ):
        raise RenderHypothesisReviewError(
            "Accepted render voice identity changed in the fresh audit"
        )
    fresh_cases = [
        value for value in fresh_group["cases"] if value["queue_id"] == queue_id
    ]
    if len(fresh_cases) != 1:
        raise RenderHypothesisReviewError(
            "Accepted render case is absent or ambiguous in the fresh audit"
        )
    fresh_case = fresh_cases[0]
    if (
        fresh_case.get("line_id") != selected_render.get("line_id")
        or fresh_case.get("text") != selected_render.get("text")
        or fresh_case.get("text_sha256") != selected_render.get("text_sha256")
    ):
        raise RenderHypothesisReviewError(
            "Accepted render text identity changed in the fresh audit"
        )
    selection_authority = {
        "schema": "vntts.authoring-render-hypothesis-selection",
        "schema_version": 1,
        "review_id": review.review_id,
        "review_sha256": snapshots["review"].sha256,
        "decision_sha256": snapshots["decision"].sha256,
        "comparison_id": comparison["comparison_id"],
        "comparison_sha256": snapshots["comparison"].sha256,
        "source_audit_id": source.audit_id,
        "source_audit_sha256": snapshots["source_audit"].sha256,
        "selected_arm_id": review.arm_id,
        "selected_arm_report_sha256": review_document["arm_report_sha256"],
        "selected_render_sha256": review.result_sha256,
        "source_candidate_group_id": source_group_id,
        "source_candidate_id": source_candidate_id,
        "source_reference": source_private_candidate["source_reference"],
        "selected_reference_sha256": review.reference_sha256,
        "queue_id": queue_id,
        "text_sha256": selected_render["text_sha256"],
    }
    try:
        for name, snapshot in snapshots.items():
            assert_authority_snapshot(snapshot, name.replace("_", " "))
        current = load_failure_reference_decisions(audit_directory)
    except (AuthoringAuthorityError, FailureReferenceAuditError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    existing = next(
        (
            value
            for value in current["decisions"]
            if value["group_id"] == fresh_group["group_id"]
        ),
        None,
    )
    if existing is not None:
        if (
            existing.get("decision") != fresh_candidate["candidate_id"]
            or existing.get("selection_authority") != selection_authority
        ):
            raise RenderHypothesisReviewError(
                "Fresh reference audit already has a different decision"
            )
        return RenderHypothesisSelection(
            audit_directory,
            fresh.audit_id,
            fresh_group["group_id"],
            fresh_candidate["candidate_id"],
            queue_id,
            review.review_id,
            review.reference_sha256,
            current["decision_set_id"],
            False,
        )
    try:
        for name, snapshot in snapshots.items():
            assert_authority_snapshot(snapshot, name.replace("_", " "))
        decisions = record_failure_reference_decision(
            audit_directory,
            fresh_group["group_id"],
            fresh_candidate["candidate_id"],
            selection_authority=selection_authority,
        )
    except (AuthoringAuthorityError, FailureReferenceAuditError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    return RenderHypothesisSelection(
        audit_directory,
        fresh.audit_id,
        fresh_group["group_id"],
        fresh_candidate["candidate_id"],
        queue_id,
        review.review_id,
        review.reference_sha256,
        decisions["decision_set_id"],
        True,
    )


def _validate_review_document(
    review,
    comparison_snapshot,
    report_snapshot,
    reference_snapshot,
    result_snapshot,
):
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
    if not isinstance(review, dict) or set(review) != required:
        raise RenderHypothesisReviewError("Render hypothesis review is malformed")
    if (
        review["schema"] != RENDER_HYPOTHESIS_REVIEW_SCHEMA
        or review["schema_version"] != RENDER_HYPOTHESIS_REVIEW_VERSION
    ):
        raise RenderHypothesisReviewError("Unsupported render hypothesis review")
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
    identity["schema"] = review["schema"]
    identity["schema_version"] = review["schema_version"]
    if review["review_id"] != canonical_document_sha256(identity):
        raise RenderHypothesisReviewError("Render hypothesis review ID changed")
    if (
        comparison_snapshot.sha256 != review["comparison_sha256"]
        or report_snapshot.sha256 != review["arm_report_sha256"]
        or reference_snapshot.sha256 != review["reference_sha256"]
        or result_snapshot.sha256 != review["result_sha256"]
    ):
        raise RenderHypothesisReviewError("Render hypothesis artifact changed")
    try:
        comparison = comparison_snapshot.json_document(
            "copied reference render comparison"
        )
        report = report_snapshot.json_document("copied reference render report")
        result_info = probe_pcm16_mono_wav(result_snapshot.path)
    except (AuthoringAuthorityError, OSError, Pcm16MonoWavError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    if comparison.get("comparison_id") != review["comparison_id"]:
        raise RenderHypothesisReviewError("Render hypothesis comparison ID changed")
    arm = next(
        (
            value
            for value in comparison.get("arms", [])
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
            for value in report.get("samples", [])
            if isinstance(value, dict) and value.get("id") == review["queue_id"]
        ),
        None,
    )
    if render is None or report_sample != render:
        raise RenderHypothesisReviewError("Render hypothesis record changed")
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
            raise RenderHypothesisReviewError(f"Render hypothesis {field} changed")
    if not reference_snapshot.payload or result_info.sample_count <= 0:
        raise RenderHypothesisReviewError("Render hypothesis audio is empty")
    expected_reference_suffix = "." + _required_text(
        review["reference_format"], "reference format"
    )
    if reference_snapshot.path.suffix.lower() != expected_reference_suffix:
        raise RenderHypothesisReviewError("Render hypothesis reference format changed")


def _validate_decision_document(decision, review, review_sha256):
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
        or decision.get("schema") != RENDER_HYPOTHESIS_DECISION_SCHEMA
        or decision.get("schema_version") != RENDER_HYPOTHESIS_DECISION_VERSION
        or decision.get("review_id") != review["review_id"]
        or decision.get("review_sha256") != review_sha256
        or decision.get("reference_sha256") != review["reference_sha256"]
        or decision.get("result_sha256") != review["result_sha256"]
        or decision.get("decision") not in RENDER_HYPOTHESIS_DECISIONS
    ):
        raise RenderHypothesisReviewError(
            "Render hypothesis decision is malformed or stale"
        )
    return decision


def _contained_file(root, value, label):
    root = Path(root).resolve()
    text = _required_text(value, label)
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise RenderHypothesisReviewError(f"{label.capitalize()} leaves its root")
    path = root / relative
    if path.is_symlink():
        raise RenderHypothesisReviewError(f"{label.capitalize()} is a symlink")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise RenderHypothesisReviewError(
            f"{label.capitalize()} leaves its root"
        ) from error
    if not resolved.is_file():
        raise RenderHypothesisReviewError(f"{label.capitalize()} is missing")
    return resolved


def _safe_directory(value, label):
    supplied = Path(value).expanduser()
    if supplied.is_symlink():
        raise RenderHypothesisReviewError(f"{label.capitalize()} is a symlink")
    resolved = supplied.resolve()
    if not resolved.is_dir():
        raise RenderHypothesisReviewError(f"{label.capitalize()} is missing")
    return resolved


def _source_audit_directory(comparison_root, value):
    text = _required_text(value, "source audit path")
    supplied = Path(text).expanduser()
    if not supplied.is_absolute():
        supplied = Path(comparison_root) / supplied
    return _safe_directory(supplied, "source failure audit")


def _group_by_id(document, group_id, label):
    groups = [
        value
        for value in document.get("groups", [])
        if value.get("group_id") == group_id
    ]
    if len(groups) != 1:
        raise RenderHypothesisReviewError(
            f"Accepted render group is absent or ambiguous in the {label}"
        )
    return groups[0]


def _candidate_by_id(group, candidate_id, label):
    candidates = [
        value
        for value in group.get("candidates", [])
        if value.get("candidate_id") == candidate_id
    ]
    if len(candidates) != 1:
        raise RenderHypothesisReviewError(
            f"Accepted render candidate is absent or ambiguous in the {label}"
        )
    return candidates[0]


def _one_group_for_queue(document, queue_id, label):
    groups = [
        group
        for group in document.get("groups", [])
        if queue_id in {case.get("queue_id") for case in group.get("cases", [])}
    ]
    if len(groups) != 1 or groups[0].get("case_count") != 1:
        raise RenderHypothesisReviewError(
            f"{label.capitalize()} must contain exactly one selected case"
        )
    return groups[0]


def _required_text(value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        raise RenderHypothesisReviewError(f"{label.capitalize()} is invalid")
    return value


__all__ = [
    "RENDER_HYPOTHESIS_DECISION_SCHEMA",
    "RENDER_HYPOTHESIS_DECISION_VERSION",
    "RENDER_HYPOTHESIS_DECISIONS",
    "RENDER_HYPOTHESIS_REVIEW_SCHEMA",
    "RENDER_HYPOTHESIS_REVIEW_VERSION",
    "RenderHypothesisReview",
    "RenderHypothesisReviewError",
    "RenderHypothesisSelection",
    "import_accepted_render_hypothesis",
    "load_render_hypothesis_review",
    "publish_render_hypothesis_review",
    "record_render_hypothesis_decision",
]

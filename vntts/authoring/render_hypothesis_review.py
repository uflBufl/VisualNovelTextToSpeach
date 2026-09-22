"""Checksum-bound review for one unmatched alternative-reference render."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    AuthoritySnapshot,
    assert_authority_snapshot,
    canonical_document_sha256,
    capture_authority_file,
    write_json_document_no_replace,
)
from vntts.authoring.failure_reference_audit import (
    FailureReferenceAudit,
    FailureReferenceAuditError,
    load_failure_reference_audit,
    load_failure_reference_decisions,
    record_failure_reference_decision,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.reference_render_comparison import (
    ReferenceRenderComparisonError,
    load_reference_render_comparison_document,
)
from vntts.path_safety import contained_regular_file

JsonObject: TypeAlias = dict[str, object]

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

    def to_dict(self) -> JsonObject:
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

    def to_dict(self) -> JsonObject:
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


@dataclass(frozen=True)
class _PublishArtifacts:
    queue_id: str
    arm_id: str
    arm: JsonObject
    render: JsonObject
    control: JsonObject
    reference_suffix: str


@dataclass(frozen=True)
class _ReviewSnapshots:
    comparison: AuthoritySnapshot
    report: AuthoritySnapshot
    reference: AuthoritySnapshot
    result: AuthoritySnapshot


@dataclass(frozen=True)
class _ImportContext:
    audit_directory: Path
    comparison_directory: Path
    review_directory: Path
    fresh: FailureReferenceAudit
    review: RenderHypothesisReview
    comparison: JsonObject


@dataclass(frozen=True)
class _ImportDocuments:
    snapshots: _ReviewSnapshots
    fresh_audit: AuthoritySnapshot
    fresh_key: AuthoritySnapshot
    comparison: AuthoritySnapshot
    review: AuthoritySnapshot
    decision: AuthoritySnapshot
    fresh_document: JsonObject
    fresh_key_document: JsonObject
    review_document: JsonObject
    decision_document: JsonObject


@dataclass(frozen=True)
class _SourceAudit:
    audit: FailureReferenceAudit
    audit_snapshot: AuthoritySnapshot
    key_snapshot: AuthoritySnapshot
    document: JsonObject
    key: JsonObject


@dataclass(frozen=True)
class _Selection:
    fresh_group: JsonObject
    fresh_candidate: JsonObject
    authority: JsonObject


def publish_render_hypothesis_review(
    comparison_directory: str | Path,
    queue_id: object,
    arm_id: object,
    output: str | Path,
) -> RenderHypothesisReview:
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
    comparison, comparison_snapshot = _publish_comparison(comparison_root)
    artifacts = _publish_artifacts(comparison, queue_id, arm_id)
    snapshots = _publish_snapshots(comparison_root, comparison_snapshot, artifacts)
    _assert_publish_hashes(artifacts, snapshots)
    identity = _review_identity(artifacts, comparison, snapshots)
    review_id = canonical_document_sha256(identity)
    output.parent.mkdir(parents=True, exist_ok=True)
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        _stage_review(staging, comparison_snapshot, snapshots, identity, review_id)
        _assert_publish_snapshots(comparison_snapshot, snapshots)
        try:
            rename_directory_no_replace(staging, output)
        except (AtomicPublicationError, OSError) as error:
            raise RenderHypothesisReviewError(str(error)) from error
        return load_render_hypothesis_review(output)


def _publish_comparison(comparison_root: Path) -> tuple[JsonObject, AuthoritySnapshot]:
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
    return comparison, comparison_snapshot


def _publish_artifacts(
    comparison: JsonObject, queue_id: object, arm_id: object
) -> _PublishArtifacts:
    selected_queue_id = _required_text(queue_id, "queue ID")
    selected_arm_id = _required_text(arm_id, "arm ID")
    arm = next(
        (
            value
            for value in _documents(comparison.get("arms"))
            if value.get("arm_id") == selected_arm_id
        ),
        None,
    )
    if arm is None:
        raise RenderHypothesisReviewError(
            f"Reference render arm is absent: {selected_arm_id}"
        )
    renders = [
        value
        for value in _documents(arm.get("renders"))
        if value.get("id") == selected_queue_id and value.get("outcome") == "complete"
    ]
    if len(renders) != 1:
        raise RenderHypothesisReviewError(
            "Render hypothesis requires one complete exact queue item"
        )
    render = renders[0]
    controls = [
        value
        for value in _documents(comparison.get("controls"))
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
    return _PublishArtifacts(
        selected_queue_id,
        selected_arm_id,
        arm,
        render,
        control,
        reference_suffix,
    )


def _publish_snapshots(
    comparison_root: Path,
    comparison_snapshot: AuthoritySnapshot,
    artifacts: _PublishArtifacts,
) -> _ReviewSnapshots:
    try:
        report_snapshot = capture_authority_file(
            _contained_file(comparison_root, artifacts.arm.get("report"), "arm report"),
            "reference render arm report",
            root=comparison_root,
        )
        reference_snapshot = capture_authority_file(
            _contained_file(
                comparison_root, artifacts.control.get("audio"), "reference control"
            ),
            "reference control",
            root=comparison_root,
        )
        result_snapshot = capture_authority_file(
            _contained_file(
                comparison_root / "arms" / artifacts.arm_id,
                artifacts.render.get("audio"),
                "render result",
            ),
            "render result",
            root=comparison_root,
        )
    except AuthoringAuthorityError as error:
        raise RenderHypothesisReviewError(str(error)) from error
    return _ReviewSnapshots(
        comparison=comparison_snapshot,
        report=report_snapshot,
        reference=reference_snapshot,
        result=result_snapshot,
    )


def _assert_publish_hashes(
    artifacts: _PublishArtifacts, snapshots: _ReviewSnapshots
) -> None:
    if (
        snapshots.report.sha256 != artifacts.arm.get("report_sha256")
        or snapshots.reference.sha256 != artifacts.render.get("reference_sha256")
        or snapshots.result.sha256 != artifacts.render.get("audio_sha256")
    ):
        raise RenderHypothesisReviewError(
            "Render hypothesis source hashes do not match the comparison"
        )


def _review_identity(
    artifacts: _PublishArtifacts,
    comparison: JsonObject,
    snapshots: _ReviewSnapshots,
) -> JsonObject:
    return {
        "schema": RENDER_HYPOTHESIS_REVIEW_SCHEMA,
        "schema_version": RENDER_HYPOTHESIS_REVIEW_VERSION,
        "comparison_id": comparison["comparison_id"],
        "comparison_sha256": snapshots.comparison.sha256,
        "arm_id": artifacts.arm_id,
        "arm_report_sha256": snapshots.report.sha256,
        "queue_id": artifacts.queue_id,
        "line_id": artifacts.render["line_id"],
        "text": artifacts.render["text"],
        "text_sha256": artifacts.render["text_sha256"],
        "candidate_group_id": artifacts.render["candidate_group_id"],
        "candidate_id": artifacts.render["candidate_id"],
        "reference_sha256": snapshots.reference.sha256,
        "reference_format": artifacts.reference_suffix[1:],
        "result_sha256": snapshots.result.sha256,
        "backend": artifacts.render["backend"],
        "model": artifacts.render["model"],
        "generation_profile": artifacts.render["generation_profile"],
        "seed": artifacts.render["seed"],
    }


def _stage_review(
    staging: Path,
    comparison_snapshot: AuthoritySnapshot,
    snapshots: _ReviewSnapshots,
    identity: JsonObject,
    review_id: str,
) -> None:
    (staging / "audio").mkdir(parents=True)
    (staging / "comparison.json").write_bytes(comparison_snapshot.payload)
    (staging / "arm-report.json").write_bytes(snapshots.report.payload)
    reference_relative = f"audio/reference.{identity['reference_format']}"
    (staging / reference_relative).write_bytes(snapshots.reference.payload)
    (staging / "audio/result.wav").write_bytes(snapshots.result.payload)
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


def _assert_publish_snapshots(
    comparison_snapshot: AuthoritySnapshot, snapshots: _ReviewSnapshots
) -> None:
    for snapshot, label in (
        (comparison_snapshot, "reference render comparison"),
        (snapshots.report, "reference render arm report"),
        (snapshots.reference, "reference control"),
        (snapshots.result, "render result"),
    ):
        assert_authority_snapshot(snapshot, label)


def load_render_hypothesis_review(directory: str | Path) -> RenderHypothesisReview:
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
        decision = _required_text(
            _validate_decision_document(
                decision_document, review, review_snapshot.sha256
            )["decision"],
            "render hypothesis decision",
        )
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
        review_id=_required_text(review["review_id"], "review ID"),
        queue_id=_required_text(review["queue_id"], "queue ID"),
        arm_id=_required_text(review["arm_id"], "arm ID"),
        reference=reference_snapshot.path,
        reference_sha256=reference_snapshot.sha256,
        result=result_snapshot.path,
        result_sha256=result_snapshot.sha256,
        decision=decision,
    )


def record_render_hypothesis_decision(
    directory: str | Path, decision: object
) -> RenderHypothesisReview:
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
    audit_directory: str | Path,
    comparison_directory: str | Path,
    review_directory: str | Path,
    queue_id: object,
) -> RenderHypothesisSelection:
    """Bind one accepted single-render hypothesis to one fresh exact audit."""
    selected_queue_id = _required_text(queue_id, "queue ID")
    context = _import_context(
        audit_directory, comparison_directory, review_directory, selected_queue_id
    )
    documents = _capture_import_documents(context)
    selected_render = _selected_import_render(context, documents, selected_queue_id)
    source = _capture_source_audit(context)
    selection = _import_selection(
        context, documents, source, selected_render, selected_queue_id
    )
    current = _load_import_decisions(context, documents, source)
    existing = _existing_import_selection(
        context, selection, current, selected_queue_id
    )
    if existing is not None:
        return existing
    return _record_import_selection(
        context, documents, source, selection, selected_queue_id
    )


def _import_context(
    audit_directory: str | Path,
    comparison_directory: str | Path,
    review_directory: str | Path,
    queue_id: str,
) -> _ImportContext:
    fresh_directory = _safe_directory(audit_directory, "fresh failure audit")
    comparison_root = _safe_directory(
        comparison_directory, "reference render comparison"
    )
    review_root = _safe_directory(review_directory, "render hypothesis review")
    try:
        fresh = load_failure_reference_audit(fresh_directory)
        review = load_render_hypothesis_review(review_root)
        comparison = load_reference_render_comparison_document(comparison_root)
    except (FailureReferenceAuditError, ReferenceRenderComparisonError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    if review.decision != "accept_hypothesis" or review.queue_id != queue_id:
        raise RenderHypothesisReviewError(
            "Render hypothesis must be accepted for the exact queue item"
        )
    return _ImportContext(
        fresh_directory, comparison_root, review_root, fresh, review, comparison
    )


def _capture_import_documents(context: _ImportContext) -> _ImportDocuments:
    try:
        fresh_audit = capture_authority_file(
            context.audit_directory / "audit.json",
            "fresh failure audit",
            root=context.audit_directory,
        )
        fresh_key = capture_authority_file(
            context.audit_directory / ".blind-key.json",
            "fresh failure audit key",
            root=context.audit_directory,
        )
        comparison = capture_authority_file(
            context.comparison_directory / "comparison.json",
            "reference render comparison",
            root=context.comparison_directory,
        )
        review = capture_authority_file(
            context.review_directory / "review.json",
            "render hypothesis review",
            root=context.review_directory,
        )
        decision = capture_authority_file(
            context.review_directory / "decision.json",
            "render hypothesis decision",
            root=context.review_directory,
        )
        fresh_document = fresh_audit.json_document("fresh failure audit")
        fresh_key_document = fresh_key.json_document("fresh failure audit key")
        review_document = review.json_document("render hypothesis review")
        decision_document = decision.json_document("render hypothesis decision")
        review_comparison = capture_authority_file(
            _contained_file(
                context.review_directory,
                review_document.get("comparison"),
                "copied reference render comparison",
            ),
            "copied reference render comparison",
            root=context.review_directory,
        )
        review_report = capture_authority_file(
            _contained_file(
                context.review_directory,
                review_document.get("arm_report"),
                "copied reference render report",
            ),
            "copied reference render report",
            root=context.review_directory,
        )
        review_reference = capture_authority_file(
            _contained_file(
                context.review_directory,
                review_document.get("reference"),
                "copied reference audio",
            ),
            "copied reference audio",
            root=context.review_directory,
        )
        review_result = capture_authority_file(
            _contained_file(
                context.review_directory,
                review_document.get("result"),
                "copied render result",
            ),
            "copied render result",
            root=context.review_directory,
        )
        snapshots = _ReviewSnapshots(
            review_comparison, review_report, review_reference, review_result
        )
        _validate_review_document(
            review_document,
            snapshots.comparison,
            snapshots.report,
            snapshots.reference,
            snapshots.result,
        )
        _validate_decision_document(
            decision_document,
            review_document,
            review.sha256,
        )
        exact_fresh = load_failure_reference_audit(context.audit_directory)
        exact_review = load_render_hypothesis_review(context.review_directory)
        exact_comparison = load_reference_render_comparison_document(
            context.comparison_directory
        )
    except (
        AuthoringAuthorityError,
        FailureReferenceAuditError,
        ReferenceRenderComparisonError,
    ) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    if (
        context.fresh.audit_id != exact_fresh.audit_id
        or context.fresh.audit_id != fresh_document.get("audit_id")
        or context.review.review_id != exact_review.review_id
        or context.review.review_id != review_document.get("review_id")
        or decision_document.get("decision") != "accept_hypothesis"
        or decision_document.get("review_id") != context.review.review_id
        or context.comparison != exact_comparison
        or context.comparison != comparison.json_document("reference render comparison")
        or context.comparison.get("comparison_id")
        != review_document.get("comparison_id")
        or comparison.sha256 != review_document.get("comparison_sha256")
    ):
        raise RenderHypothesisReviewError(
            "Accepted render hypothesis authority changed"
        )
    return _ImportDocuments(
        snapshots,
        fresh_audit,
        fresh_key,
        comparison,
        review,
        decision,
        fresh_document,
        fresh_key_document,
        review_document,
        decision_document,
    )


def _selected_import_render(
    context: _ImportContext, documents: _ImportDocuments, queue_id: str
) -> JsonObject:
    arm = next(
        (
            value
            for value in _documents(context.comparison.get("arms"))
            if value.get("arm_id") == context.review.arm_id
        ),
        None,
    )
    selected_render = next(
        (
            value
            for value in _documents((arm or {}).get("renders"))
            if value.get("id") == queue_id and value.get("outcome") == "complete"
        ),
        None,
    )
    if (
        selected_render is None
        or selected_render.get("audio_sha256") != context.review.result_sha256
        or selected_render.get("reference_sha256") != context.review.reference_sha256
        or selected_render.get("text_sha256")
        != documents.review_document.get("text_sha256")
    ):
        raise RenderHypothesisReviewError(
            "Accepted render no longer matches its comparison"
        )
    return selected_render


def _capture_source_audit(context: _ImportContext) -> _SourceAudit:
    source_audit_directory = _source_audit_directory(
        context.comparison_directory, context.comparison.get("audit")
    )
    try:
        source = load_failure_reference_audit(source_audit_directory)
        audit_snapshot = capture_authority_file(
            source_audit_directory / "audit.json",
            "source failure audit",
            root=source_audit_directory,
        )
        key_snapshot = capture_authority_file(
            source_audit_directory / ".blind-key.json",
            "source failure audit key",
            root=source_audit_directory,
        )
        source_document = audit_snapshot.json_document("source failure audit")
        source_key = key_snapshot.json_document("source failure audit key")
        exact_source = load_failure_reference_audit(source_audit_directory)
    except (AuthoringAuthorityError, FailureReferenceAuditError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    if (
        source.audit_id != exact_source.audit_id
        or source.audit_id != context.comparison.get("audit_id")
        or source.audit_id != source_document.get("audit_id")
        or audit_snapshot.sha256 != context.comparison.get("audit_sha256")
    ):
        raise RenderHypothesisReviewError(
            "Accepted render source audit authority changed"
        )
    return _SourceAudit(
        source, audit_snapshot, key_snapshot, source_document, source_key
    )


def _import_selection(
    context: _ImportContext,
    documents: _ImportDocuments,
    source: _SourceAudit,
    selected_render: JsonObject,
    queue_id: str,
) -> _Selection:
    source_group_id = selected_render.get("candidate_group_id")
    source_candidate_id = selected_render.get("candidate_id")
    source_group = _group_by_id(source.document, source_group_id, "source audit")
    source_private_group = _group_by_id(source.key, source_group_id, "source audit key")
    source_candidate = _candidate_by_id(
        source_group, source_candidate_id, "source audit"
    )
    source_private_candidate = _candidate_by_id(
        source_private_group, source_candidate_id, "source audit key"
    )
    if (
        source_candidate.get("sha256") != context.review.reference_sha256
        or source_private_candidate.get("source_sha256")
        != context.review.reference_sha256
    ):
        raise RenderHypothesisReviewError(
            "Accepted render reference changed in its source audit"
        )
    fresh_group = _one_group_for_queue(
        documents.fresh_document, queue_id, "fresh audit"
    )
    fresh_private_group = _group_by_id(
        documents.fresh_key_document, fresh_group["group_id"], "fresh audit key"
    )
    fresh_candidates = [
        value
        for value in _documents(fresh_group.get("candidates"))
        if value.get("sha256") == context.review.reference_sha256
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
        or fresh_private_candidate.get("source_sha256")
        != context.review.reference_sha256
    ):
        raise RenderHypothesisReviewError(
            "Accepted render voice identity changed in the fresh audit"
        )
    fresh_cases = [
        value
        for value in _documents(fresh_group.get("cases"))
        if value.get("queue_id") == queue_id
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
    authority = {
        "schema": "vntts.authoring-render-hypothesis-selection",
        "schema_version": 1,
        "review_id": context.review.review_id,
        "review_sha256": documents.review.sha256,
        "decision_sha256": documents.decision.sha256,
        "comparison_id": context.comparison["comparison_id"],
        "comparison_sha256": documents.snapshots.comparison.sha256,
        "source_audit_id": source.audit.audit_id,
        "source_audit_sha256": source.audit_snapshot.sha256,
        "selected_arm_id": context.review.arm_id,
        "selected_arm_report_sha256": documents.review_document["arm_report_sha256"],
        "selected_render_sha256": context.review.result_sha256,
        "source_candidate_group_id": source_group_id,
        "source_candidate_id": source_candidate_id,
        "source_reference": source_private_candidate["source_reference"],
        "selected_reference_sha256": context.review.reference_sha256,
        "queue_id": queue_id,
        "text_sha256": selected_render["text_sha256"],
    }
    return _Selection(fresh_group, fresh_candidate, authority)


def _load_import_decisions(
    context: _ImportContext,
    documents: _ImportDocuments,
    source: _SourceAudit,
) -> JsonObject:
    try:
        _assert_import_snapshots(documents, source)
        return load_failure_reference_decisions(context.audit_directory)
    except (AuthoringAuthorityError, FailureReferenceAuditError) as error:
        raise RenderHypothesisReviewError(str(error)) from error


def _existing_import_selection(
    context: _ImportContext,
    selection: _Selection,
    current: JsonObject,
    queue_id: str,
) -> RenderHypothesisSelection | None:
    existing = next(
        (
            value
            for value in _documents(current.get("decisions"))
            if value.get("group_id") == selection.fresh_group["group_id"]
        ),
        None,
    )
    if existing is None:
        return None
    if (
        existing.get("decision") != selection.fresh_candidate["candidate_id"]
        or existing.get("selection_authority") != selection.authority
    ):
        raise RenderHypothesisReviewError(
            "Fresh reference audit already has a different decision"
        )
    return RenderHypothesisSelection(
        context.audit_directory,
        context.fresh.audit_id,
        _required_text(selection.fresh_group["group_id"], "fresh group ID"),
        _required_text(selection.fresh_candidate["candidate_id"], "fresh candidate ID"),
        queue_id,
        context.review.review_id,
        context.review.reference_sha256,
        _required_text(current["decision_set_id"], "decision set ID"),
        False,
    )


def _record_import_selection(
    context: _ImportContext,
    documents: _ImportDocuments,
    source: _SourceAudit,
    selection: _Selection,
    queue_id: str,
) -> RenderHypothesisSelection:
    try:
        _assert_import_snapshots(documents, source)
        decisions = record_failure_reference_decision(
            context.audit_directory,
            _required_text(selection.fresh_group["group_id"], "fresh group ID"),
            _required_text(
                selection.fresh_candidate["candidate_id"], "fresh candidate ID"
            ),
            selection_authority=selection.authority,
        )
    except (AuthoringAuthorityError, FailureReferenceAuditError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    return RenderHypothesisSelection(
        context.audit_directory,
        context.fresh.audit_id,
        _required_text(selection.fresh_group["group_id"], "fresh group ID"),
        _required_text(selection.fresh_candidate["candidate_id"], "fresh candidate ID"),
        queue_id,
        context.review.review_id,
        context.review.reference_sha256,
        _required_text(decisions["decision_set_id"], "decision set ID"),
        True,
    )


def _assert_import_snapshots(documents: _ImportDocuments, source: _SourceAudit) -> None:
    for snapshot, label in (
        (documents.fresh_audit, "fresh audit"),
        (documents.fresh_key, "fresh key"),
        (documents.comparison, "comparison"),
        (documents.review, "review"),
        (documents.decision, "decision"),
        (documents.snapshots.comparison, "review comparison"),
        (documents.snapshots.report, "review arm report"),
        (documents.snapshots.reference, "review reference"),
        (documents.snapshots.result, "review result"),
        (source.audit_snapshot, "source audit"),
        (source.key_snapshot, "source key"),
    ):
        assert_authority_snapshot(snapshot, label)


def _validate_review_document(
    review: JsonObject,
    comparison_snapshot: AuthoritySnapshot,
    report_snapshot: AuthoritySnapshot,
    reference_snapshot: AuthoritySnapshot,
    result_snapshot: AuthoritySnapshot,
) -> None:
    _validate_review_shape(review)
    _validate_review_identity(review)
    _validate_review_hashes(
        review,
        comparison_snapshot,
        report_snapshot,
        reference_snapshot,
        result_snapshot,
    )
    comparison, report, result_sample_count = _review_documents_and_audio(
        comparison_snapshot, report_snapshot, result_snapshot
    )
    _validate_review_records(review, comparison, report)
    _validate_review_audio(review, reference_snapshot, result_sample_count)


def _validate_review_shape(review: JsonObject) -> None:
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


def _validate_review_identity(review: JsonObject) -> None:
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


def _validate_review_hashes(
    review: JsonObject,
    comparison_snapshot: AuthoritySnapshot,
    report_snapshot: AuthoritySnapshot,
    reference_snapshot: AuthoritySnapshot,
    result_snapshot: AuthoritySnapshot,
) -> None:
    if (
        comparison_snapshot.sha256 != review["comparison_sha256"]
        or report_snapshot.sha256 != review["arm_report_sha256"]
        or reference_snapshot.sha256 != review["reference_sha256"]
        or result_snapshot.sha256 != review["result_sha256"]
    ):
        raise RenderHypothesisReviewError("Render hypothesis artifact changed")


def _review_documents_and_audio(
    comparison_snapshot: AuthoritySnapshot,
    report_snapshot: AuthoritySnapshot,
    result_snapshot: AuthoritySnapshot,
) -> tuple[JsonObject, JsonObject, int]:
    try:
        comparison = comparison_snapshot.json_document(
            "copied reference render comparison"
        )
        report = report_snapshot.json_document("copied reference render report")
        result_info = probe_pcm16_mono_wav(result_snapshot.path)
    except (AuthoringAuthorityError, OSError, Pcm16MonoWavError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    return comparison, report, result_info.sample_count


def _validate_review_records(
    review: JsonObject, comparison: JsonObject, report: JsonObject
) -> None:
    if comparison.get("comparison_id") != review["comparison_id"]:
        raise RenderHypothesisReviewError("Render hypothesis comparison ID changed")
    arm = next(
        (
            value
            for value in _documents(comparison.get("arms"))
            if value.get("arm_id") == review["arm_id"]
        ),
        None,
    )
    render = next(
        (
            value
            for value in _documents((arm or {}).get("renders"))
            if value.get("id") == review["queue_id"]
            and value.get("outcome") == "complete"
        ),
        None,
    )
    report_sample = next(
        (
            value
            for value in _documents(report.get("samples"))
            if value.get("id") == review["queue_id"]
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


def _validate_review_audio(
    review: JsonObject, reference_snapshot: AuthoritySnapshot, result_sample_count: int
) -> None:
    if not reference_snapshot.payload or result_sample_count <= 0:
        raise RenderHypothesisReviewError("Render hypothesis audio is empty")
    expected_reference_suffix = "." + _required_text(
        review["reference_format"], "reference format"
    )
    if reference_snapshot.path.suffix.lower() != expected_reference_suffix:
        raise RenderHypothesisReviewError("Render hypothesis reference format changed")


def _validate_decision_document(
    decision: JsonObject, review: JsonObject, review_sha256: str
) -> JsonObject:
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


def _contained_file(root: Path, value: object, label: str) -> Path:
    text = _required_text(value, label)
    path: Path = contained_regular_file(
        root, text, label, error_type=RenderHypothesisReviewError
    )
    return path


def _safe_directory(value: str | Path, label: str) -> Path:
    supplied = Path(value).expanduser()
    if supplied.is_symlink():
        raise RenderHypothesisReviewError(f"{label.capitalize()} is a symlink")
    resolved = supplied.resolve()
    if not resolved.is_dir():
        raise RenderHypothesisReviewError(f"{label.capitalize()} is missing")
    return resolved


def _source_audit_directory(comparison_root: Path, value: object) -> Path:
    text = _required_text(value, "source audit path")
    supplied = Path(text).expanduser()
    if not supplied.is_absolute():
        supplied = Path(comparison_root) / supplied
    return _safe_directory(supplied, "source failure audit")


def _group_by_id(document: JsonObject, group_id: object, label: str) -> JsonObject:
    groups = [
        value
        for value in _documents(document.get("groups"))
        if value.get("group_id") == group_id
    ]
    if len(groups) != 1:
        raise RenderHypothesisReviewError(
            f"Accepted render group is absent or ambiguous in the {label}"
        )
    return groups[0]


def _candidate_by_id(group: JsonObject, candidate_id: object, label: str) -> JsonObject:
    candidates = [
        value
        for value in _documents(group.get("candidates"))
        if value.get("candidate_id") == candidate_id
    ]
    if len(candidates) != 1:
        raise RenderHypothesisReviewError(
            f"Accepted render candidate is absent or ambiguous in the {label}"
        )
    return candidates[0]


def _one_group_for_queue(document: JsonObject, queue_id: str, label: str) -> JsonObject:
    groups = [
        group
        for group in _documents(document.get("groups"))
        if queue_id in {case.get("queue_id") for case in _documents(group.get("cases"))}
    ]
    if len(groups) != 1 or groups[0].get("case_count") != 1:
        raise RenderHypothesisReviewError(
            f"{label.capitalize()} must contain exactly one selected case"
        )
    return groups[0]


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RenderHypothesisReviewError(f"{label.capitalize()} is invalid")
    return value


def _documents(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


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

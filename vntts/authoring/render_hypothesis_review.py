"""Checksum-bound review for one unmatched alternative-reference render."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

from durable_file import atomic_write_json

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
    _validate_audit_candidate_files,
    _validate_audit_documents,
    load_failure_reference_audit,
    load_failure_reference_decisions,
    record_failure_reference_decision,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    no_replace_destination,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.reference_render_comparison import (
    ReferenceRenderComparisonError,
    load_reference_render_comparison_document,
)
from vntts.authoring.render_hypothesis_records import (
    RENDER_HYPOTHESIS_DECISION_SCHEMA,
    RENDER_HYPOTHESIS_DECISION_VERSION,
    RENDER_HYPOTHESIS_DECISIONS,
    RENDER_HYPOTHESIS_REVIEW_SCHEMA,
    RENDER_HYPOTHESIS_REVIEW_VERSION,
    RenderHypothesisRecord,
    RenderHypothesisRecordError,
    load_render_hypothesis_record,
)
from vntts.path_safety import contained_regular_file

JsonObject: TypeAlias = dict[str, object]


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
    fresh_audit: AuthoritySnapshot
    fresh_key: AuthoritySnapshot
    comparison: AuthoritySnapshot
    fresh_document: JsonObject
    fresh_key_document: JsonObject
    record: RenderHypothesisRecord


@dataclass(frozen=True)
class _SourceAudit:
    audit: FailureReferenceAudit
    audit_snapshot: AuthoritySnapshot
    key_snapshot: AuthoritySnapshot
    document: JsonObject
    key: JsonObject


@dataclass(frozen=True)
class _Selection:
    group_id: str
    candidate_id: str
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
    output = no_replace_destination(output)
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
        _stage_review(staging, snapshots, identity, review_id)
        _assert_publish_snapshots(snapshots)
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
    snapshots: _ReviewSnapshots,
    identity: JsonObject,
    review_id: str,
) -> None:
    (staging / "audio").mkdir(parents=True)
    (staging / "comparison.json").write_bytes(snapshots.comparison.payload)
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


def _assert_publish_snapshots(snapshots: _ReviewSnapshots) -> None:
    try:
        for snapshot, label in (
            (snapshots.comparison, "reference render comparison"),
            (snapshots.report, "reference render arm report"),
            (snapshots.reference, "reference control"),
            (snapshots.result, "render result"),
        ):
            assert_authority_snapshot(snapshot, label)
    except AuthoringAuthorityError as error:
        raise RenderHypothesisReviewError(str(error)) from error


def _assert_record_snapshots(record: RenderHypothesisRecord) -> None:
    for snapshot in record.snapshots:
        assert_authority_snapshot(snapshot, f"render hypothesis {snapshot.path.name}")


def _load_review_record(directory: str | Path) -> RenderHypothesisRecord:
    try:
        record = load_render_hypothesis_record(directory)
        _assert_record_snapshots(record)
    except (RenderHypothesisRecordError, AuthoringAuthorityError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    return record


def load_render_hypothesis_review(directory: str | Path) -> RenderHypothesisReview:
    """Load and verify one self-contained render hypothesis review."""
    return _review_from_record(_load_review_record(directory))


def _review_from_record(record: RenderHypothesisRecord) -> RenderHypothesisReview:
    return RenderHypothesisReview(
        directory=record.directory,
        review_id=_required_text(record.review["review_id"], "review ID"),
        queue_id=_required_text(record.review["queue_id"], "queue ID"),
        arm_id=_required_text(record.review["arm_id"], "arm ID"),
        reference=record.reference_snapshot.path,
        reference_sha256=record.reference_snapshot.sha256,
        result=record.result_snapshot.path,
        result_sha256=record.result_snapshot.sha256,
        decision=(
            None
            if record.decision is None
            else _required_text(
                record.decision["decision"], "render hypothesis decision"
            )
        ),
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
    record = _load_review_record(directory)
    review = _review_from_record(record)
    if review.decision is not None:
        if review.decision == decision:
            return review
        raise RenderHypothesisReviewError(
            f"Render hypothesis review is already decided: {review.decision}"
        )
    decision_path = review.directory / "decision.json"
    decision_document = {
        "schema": RENDER_HYPOTHESIS_DECISION_SCHEMA,
        "schema_version": RENDER_HYPOTHESIS_DECISION_VERSION,
        "review_id": review.review_id,
        "review_sha256": record.review_snapshot.sha256,
        "reference_sha256": record.reference_snapshot.sha256,
        "result_sha256": record.result_snapshot.sha256,
        "decision": decision,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _assert_record_snapshots(record)
        write_json_document_no_replace(
            decision_path, decision_document, "render hypothesis decision"
        )
    except AuthoringAuthorityError as error:
        if decision_path.exists():
            current = load_render_hypothesis_review(review.directory)
            if current.decision == decision:
                return current
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
    return _save_import_selection(
        context, documents, source, selection, current, selected_queue_id
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
        record = _load_review_record(context.review_directory)
        fresh_document = fresh_audit.json_document("fresh failure audit")
        fresh_key_document = fresh_key.json_document("fresh failure audit key")
        _assert_audit_identity(
            fresh_document, fresh_key_document, context.fresh.audit_id
        )
        exact_fresh = load_failure_reference_audit(context.audit_directory)
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
        or context.review.review_id != record.review.get("review_id")
        or record.decision is None
        or record.decision.get("decision") != "accept_hypothesis"
        or record.decision.get("review_id") != context.review.review_id
        or context.comparison != exact_comparison
        or context.comparison != comparison.json_document("reference render comparison")
        or context.comparison.get("comparison_id") != record.review.get("comparison_id")
        or comparison.sha256 != record.review.get("comparison_sha256")
    ):
        raise RenderHypothesisReviewError(
            "Accepted render hypothesis authority changed"
        )
    return _ImportDocuments(
        fresh_audit, fresh_key, comparison, fresh_document, fresh_key_document, record
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
        != documents.record.review.get("text_sha256")
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
        _assert_audit_identity(source_document, source_key, source.audit_id)
        exact_source = load_failure_reference_audit(source_audit_directory)
    except (AuthoringAuthorityError, FailureReferenceAuditError, OSError) as error:
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
    decision_snapshot = documents.record.decision_snapshot
    if decision_snapshot is None:
        raise RenderHypothesisReviewError("Accepted render decision is absent")
    authority = {
        "schema": "vntts.authoring-render-hypothesis-selection",
        "schema_version": 1,
        "review_id": context.review.review_id,
        "review_sha256": documents.record.review_snapshot.sha256,
        "decision_sha256": decision_snapshot.sha256,
        "comparison_id": context.comparison["comparison_id"],
        "comparison_sha256": documents.record.comparison_snapshot.sha256,
        "source_audit_id": source.audit.audit_id,
        "source_audit_sha256": source.audit_snapshot.sha256,
        "selected_arm_id": context.review.arm_id,
        "selected_arm_report_sha256": documents.record.review["arm_report_sha256"],
        "selected_render_sha256": context.review.result_sha256,
        "source_candidate_group_id": source_group_id,
        "source_candidate_id": source_candidate_id,
        "source_reference": source_private_candidate["source_reference"],
        "selected_reference_sha256": context.review.reference_sha256,
        "queue_id": queue_id,
        "text_sha256": selected_render["text_sha256"],
    }
    return _Selection(
        _required_text(fresh_group["group_id"], "fresh group ID"),
        _required_text(fresh_candidate["candidate_id"], "fresh candidate ID"),
        authority,
    )


def _load_import_decisions(
    context: _ImportContext,
    documents: _ImportDocuments,
    source: _SourceAudit,
) -> JsonObject:
    try:
        _assert_import_snapshots(documents, source)
        return load_failure_reference_decisions(context.audit_directory)
    except (AuthoringAuthorityError, FailureReferenceAuditError, OSError) as error:
        raise RenderHypothesisReviewError(str(error)) from error


def _save_import_selection(
    context: _ImportContext,
    documents: _ImportDocuments,
    source: _SourceAudit,
    selection: _Selection,
    current: JsonObject,
    queue_id: str,
) -> RenderHypothesisSelection:
    existing = next(
        (
            value
            for value in _documents(current.get("decisions"))
            if value.get("group_id") == selection.group_id
        ),
        None,
    )
    try:
        _assert_import_snapshots(documents, source)
        if existing is not None:
            if (
                existing.get("decision") != selection.candidate_id
                or existing.get("selection_authority") != selection.authority
            ):
                raise RenderHypothesisReviewError(
                    "Fresh reference audit already has a different decision"
                )
            decisions = current
        else:
            decisions = record_failure_reference_decision(
                context.audit_directory,
                selection.group_id,
                selection.candidate_id,
                selection_authority=selection.authority,
            )
    except (AuthoringAuthorityError, FailureReferenceAuditError, OSError) as error:
        raise RenderHypothesisReviewError(str(error)) from error
    return RenderHypothesisSelection(
        context.audit_directory,
        context.fresh.audit_id,
        selection.group_id,
        selection.candidate_id,
        queue_id,
        context.review.review_id,
        context.review.reference_sha256,
        _required_text(decisions["decision_set_id"], "decision set ID"),
        existing is None,
    )


def _assert_import_snapshots(documents: _ImportDocuments, source: _SourceAudit) -> None:
    _assert_record_snapshots(documents.record)
    for snapshot, label in (
        (documents.fresh_audit, "fresh audit"),
        (documents.fresh_key, "fresh key"),
        (documents.comparison, "comparison"),
        (source.audit_snapshot, "source audit"),
        (source.key_snapshot, "source key"),
    ):
        assert_authority_snapshot(snapshot, label)

    for root, document in (
        (documents.fresh_audit.path.parent, documents.fresh_document),
        (source.audit.directory, source.document),
    ):
        for group in _documents(document.get("groups")):
            _validate_audit_candidate_files(root, _documents(group.get("candidates")))


def _assert_audit_identity(
    document: JsonObject, key: JsonObject, expected_id: str
) -> None:
    audit_id, _groups, _private_groups = _validate_audit_documents(document, key)
    if audit_id != expected_id:
        raise FailureReferenceAuditError("Accepted render audit authority changed")


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

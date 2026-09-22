"""Immutable render-only comparisons for alternative voice references."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, TypeAlias, TypedDict

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.failure_reference_audit import (
    FailureReferenceAudit,
    FailureReferenceAuditError,
    load_failure_reference_audit,
    load_failure_reference_decisions,
    prepare_failure_reference_audio,
    record_failure_reference_decision,
)
from vntts.authoring.failure_reference_preview import (
    FailureReferencePreviewCancelled,
    FailureReferencePreviewError,
    FailureReferencePreviewIncomplete,
    FailureReferencePreviewService,
)
from vntts.authoring.listening import (
    ListeningSession,
    ModelListeningError,
    aggregate_listening_report,
    create_listening_session_from_reports,
    load_listening_session,
)
from vntts.authoring.publication import rename_directory_no_replace, staged_directory
from vntts.document_identity import canonical_document_sha256, is_lowercase_sha256
from vntts.synthesis import SynthesisChunkStream, SynthesisRequest
from vntts.voices import CharacterVoiceRegistry

REFERENCE_RENDER_INPUT_SCHEMA = "vntts.authoring-reference-render-input"
REFERENCE_RENDER_INPUT_VERSION = 1
REFERENCE_RENDER_SCHEMA = "vntts.authoring-reference-render-comparison"
REFERENCE_RENDER_VERSION = 1
JsonDocument: TypeAlias = dict[str, object]


class _PreviewBackend(Protocol):
    registry: CharacterVoiceRegistry

    def render(self, request: SynthesisRequest) -> SynthesisChunkStream: ...


class _PreviewBackendFactory(Protocol):
    def __call__(
        self,
        name: str,
        registry: CharacterVoiceRegistry,
        cache_root: Path,
        *,
        model_name: str | None = None,
        startup_cancellation: threading.Event | None = None,
    ) -> _PreviewBackend: ...


class ReferenceRenderComparisonError(RuntimeError):
    """An alternative-reference comparison is malformed or unsafe."""


class _ReferenceRenderSample(TypedDict):
    queue_id: str
    case_group_id: str
    candidate_group_id: str
    candidate_id: str


class _ReferenceRenderArm(TypedDict):
    arm_id: str
    samples: list[_ReferenceRenderSample]


@dataclass(frozen=True)
class _ComparisonAudit:
    groups: dict[str, JsonDocument]
    cases: dict[tuple[str, str], JsonDocument]


@dataclass(frozen=True)
class _SelectedComparisonRender:
    arm_id: str
    render: JsonDocument
    audio_sha256: str
    candidate_group_id: str
    candidate_id: str


@dataclass(frozen=True)
class _PreferenceContext:
    audit_root: Path
    comparison_root: Path
    session_path: Path
    fresh_audit: FailureReferenceAudit
    source_audit_root: Path
    source_audit: FailureReferenceAudit
    source_audit_path: Path
    comparison: JsonDocument
    session: ListeningSession


@dataclass(frozen=True)
class _PreferenceAuditGroups:
    fresh_group: JsonDocument
    source_groups: dict[str, JsonDocument]
    source_private_groups: dict[str, JsonDocument]
    fresh_private_groups: dict[str, JsonDocument]


@dataclass(frozen=True)
class _PreferenceListeningSelection:
    trial: JsonDocument
    selected_side: str
    render: _SelectedComparisonRender


@dataclass(frozen=True)
class _SourceReference:
    group: JsonDocument
    private_group: JsonDocument
    private_candidate: JsonDocument
    sha256: str


@dataclass(frozen=True)
class _FreshReference:
    candidate: JsonDocument


@dataclass(frozen=True)
class ReferenceRenderPlan:
    path: Path
    sha256: str
    audit_directory: Path
    audit_id: str
    arms: tuple[_ReferenceRenderArm, ...]
    queue_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceRenderComparison:
    directory: Path
    comparison_id: str
    arm_count: int
    sample_count: int
    complete_pair_count: int


@dataclass(frozen=True)
class ReferenceRenderSelection:
    audit_directory: Path
    audit_id: str
    group_id: str
    candidate_id: str
    queue_id: str
    selected_arm_id: str
    selected_reference_sha256: str
    decision_set_id: str
    created: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "audit_directory": str(self.audit_directory),
            "audit_id": self.audit_id,
            "group_id": self.group_id,
            "candidate_id": self.candidate_id,
            "queue_id": self.queue_id,
            "selected_arm_id": self.selected_arm_id,
            "selected_reference_sha256": self.selected_reference_sha256,
            "decision_set_id": self.decision_set_id,
            "created": self.created,
        }


def load_reference_render_plan(path: str | Path) -> ReferenceRenderPlan:
    """Load a checksum-bound operator plan for exact cases and reference arms."""
    source, payload, document = _read_reference_render_plan(path)
    audit_directory, audit_id, groups = _load_planned_audit(source, document)
    parsed_arms, queue_ids = _parse_reference_render_arms(document, groups)
    return ReferenceRenderPlan(
        path=source,
        sha256=hashlib.sha256(payload).hexdigest(),
        audit_directory=audit_directory,
        audit_id=audit_id,
        arms=tuple(parsed_arms),
        queue_ids=queue_ids,
    )


def _read_reference_render_plan(
    path: str | Path,
) -> tuple[Path, bytes, JsonDocument]:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ReferenceRenderComparisonError("Reference render plan is a symlink")
    source = source.resolve()
    try:
        payload = source.read_bytes()
        document = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise ReferenceRenderComparisonError(
            f"Unable to read reference render plan: {error}"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "schema_version",
        "audit",
        "audit_id",
        "arms",
    }:
        raise ReferenceRenderComparisonError("Reference render plan is malformed")
    if (
        document["schema"] != REFERENCE_RENDER_INPUT_SCHEMA
        or not isinstance(document["schema_version"], int)
        or isinstance(document["schema_version"], bool)
        or document["schema_version"] != REFERENCE_RENDER_INPUT_VERSION
    ):
        raise ReferenceRenderComparisonError("Unsupported reference render plan schema")
    return source, payload, document


def _load_planned_audit(
    source: Path, document: JsonDocument
) -> tuple[Path, str, dict[str, JsonDocument]]:
    audit_directory = _planned_directory(source.parent, document["audit"])
    try:
        audit = load_failure_reference_audit(audit_directory)
    except FailureReferenceAuditError as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    if document["audit_id"] != audit.audit_id:
        raise ReferenceRenderComparisonError("Reference render audit identity changed")
    audit_document = _read_audit_document(audit_directory)
    groups = {
        _required_text(value.get("group_id"), "audit group ID"): value
        for value in _documents(audit_document.get("groups"), "audit groups")
    }
    return audit_directory, audit.audit_id, groups


def _parse_reference_render_arms(
    document: JsonDocument, groups: dict[str, JsonDocument]
) -> tuple[list[_ReferenceRenderArm], tuple[str, ...]]:
    arms = document["arms"]
    if not isinstance(arms, list) or len(arms) < 2:
        raise ReferenceRenderComparisonError(
            "Reference render plan requires at least two arms"
        )
    parsed_arms: list[_ReferenceRenderArm] = []
    arm_ids = set()
    expected_queue_ids = None
    selections_by_queue_id: dict[str, set[tuple[str, str]]] = {}
    for arm_index, arm in enumerate(arms, start=1):
        arm_id, parsed_samples, current_queue_ids = _parse_reference_render_arm(
            arm, arm_index, groups, selections_by_queue_id
        )
        if arm_id in arm_ids:
            raise ReferenceRenderComparisonError("Reference render arm IDs repeat")
        arm_ids.add(arm_id)
        if expected_queue_ids is None:
            expected_queue_ids = current_queue_ids
        elif current_queue_ids != expected_queue_ids:
            raise ReferenceRenderComparisonError(
                "Reference render arms must use the same ordered queue IDs"
            )
        parsed_arms.append({"arm_id": arm_id, "samples": parsed_samples})
    return parsed_arms, expected_queue_ids or ()


def _parse_reference_render_arm(
    arm: object,
    arm_index: int,
    groups: dict[str, JsonDocument],
    selections_by_queue_id: dict[str, set[tuple[str, str]]],
) -> tuple[str, list[_ReferenceRenderSample], tuple[str, ...]]:
    if not isinstance(arm, dict) or set(arm) != {"arm_id", "samples"}:
        raise ReferenceRenderComparisonError(
            f"Reference render arm {arm_index} is malformed"
        )
    arm_id = _safe_id(arm["arm_id"], f"arm {arm_index}")
    samples = arm["samples"]
    if not isinstance(samples, list) or not samples:
        raise ReferenceRenderComparisonError(
            f"Reference render arm {arm_id} has no samples"
        )
    parsed = [
        _parse_reference_render_sample(
            sample, arm_id, index, groups, selections_by_queue_id
        )
        for index, sample in enumerate(samples, start=1)
    ]
    queue_ids = tuple(sample["queue_id"] for sample in parsed)
    if len(queue_ids) != len(set(queue_ids)):
        raise ReferenceRenderComparisonError(
            f"Reference render arm {arm_id} repeats queue IDs"
        )
    return arm_id, parsed, queue_ids


def _parse_reference_render_sample(
    sample: object,
    arm_id: str,
    sample_index: int,
    groups: dict[str, JsonDocument],
    selections_by_queue_id: dict[str, set[tuple[str, str]]],
) -> _ReferenceRenderSample:
    if not isinstance(sample, dict) or set(sample) != {
        "queue_id",
        "case_group_id",
        "candidate_group_id",
        "candidate_id",
    }:
        raise ReferenceRenderComparisonError(
            f"Reference render sample {arm_id}/{sample_index} is malformed"
        )
    queue_id = _required_text(sample["queue_id"], "queue ID")
    case_group_id = _required_text(sample["case_group_id"], "case group ID")
    candidate_group_id = _required_text(
        sample["candidate_group_id"], "candidate group ID"
    )
    candidate_id = _required_text(sample["candidate_id"], "candidate ID")
    _validate_reference_render_sample(
        queue_id,
        case_group_id,
        candidate_group_id,
        candidate_id,
        groups,
        selections_by_queue_id,
    )
    return {
        "queue_id": queue_id,
        "case_group_id": case_group_id,
        "candidate_group_id": candidate_group_id,
        "candidate_id": candidate_id,
    }


def _validate_reference_render_sample(
    queue_id: str,
    case_group_id: str,
    candidate_group_id: str,
    candidate_id: str,
    groups: dict[str, JsonDocument],
    selections_by_queue_id: dict[str, set[tuple[str, str]]],
) -> None:
    case_group = groups.get(case_group_id)
    candidate_group = groups.get(candidate_group_id)
    if case_group is None or candidate_group is None:
        raise ReferenceRenderComparisonError(
            f"Reference render group is absent for {queue_id}"
        )
    if queue_id not in {
        _required_text(value.get("queue_id"), "queue ID")
        for value in _documents(case_group.get("cases"), "audit cases")
    }:
        raise ReferenceRenderComparisonError(
            f"Reference render case is absent for {queue_id}"
        )
    if candidate_id not in {
        _required_text(value.get("candidate_id"), "candidate ID")
        for value in _documents(candidate_group.get("candidates"), "audit candidates")
    }:
        raise ReferenceRenderComparisonError(
            f"Reference render candidate is absent for {queue_id}"
        )
    if case_group_id != candidate_group_id and _source_reference_family(
        case_group
    ) != _source_reference_family(candidate_group):
        raise ReferenceRenderComparisonError(
            "Cross-group reference render candidates must belong to the "
            "same exact source-reference character family"
        )
    selection = (candidate_group_id, candidate_id)
    prior = selections_by_queue_id.setdefault(queue_id, set())
    if selection in prior:
        raise ReferenceRenderComparisonError(
            f"Reference render arms repeat the same control for {queue_id}"
        )
    prior.add(selection)


def publish_reference_render_comparison(
    plan: object,
    output_directory: str | Path,
    *,
    backend_factory: _PreviewBackendFactory | None = None,
) -> ReferenceRenderComparison:
    """Render exact alternative references without changing generation state."""
    if not isinstance(plan, ReferenceRenderPlan):
        raise ReferenceRenderComparisonError(
            "Reference render publication requires a loaded plan"
        )
    output = Path(output_directory).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise ReferenceRenderComparisonError(
            f"Reference render destination already exists: {output}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    audit = _comparison_audit(plan.audit_directory)
    service_options = {}
    if backend_factory is not None:
        service_options["backend_factory"] = backend_factory
    service = FailureReferencePreviewService(plan.audit_directory, **service_options)
    try:
        with staged_directory(
            output.parent, prefix=f".{output.name}.staging-"
        ) as staging:
            reports, arm_documents, controls, shared = _render_comparison_arms(
                plan, staging, service, audit
            )
            body = _comparison_body(plan, reports, arm_documents, controls, shared)
            comparison_id = canonical_document_sha256(body)
            document = {**body, "comparison_id": comparison_id}
            atomic_write_json(staging / "comparison.json", document)
            _assert_plan_and_audit_unchanged(plan)
            rename_directory_no_replace(staging, output)
            return ReferenceRenderComparison(
                output,
                comparison_id,
                len(plan.arms),
                len(plan.queue_ids),
                len(shared),
            )
    except (FailureReferenceAuditError, FailureReferencePreviewError) as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    finally:
        service.close()


def _comparison_audit(directory: Path) -> _ComparisonAudit:
    audit_document = _read_audit_document(directory)
    audit_groups = _documents(audit_document.get("groups"), "audit groups")
    return _ComparisonAudit(
        {
            _required_text(value.get("group_id"), "audit group ID"): value
            for value in audit_groups
        },
        {
            (
                _required_text(group.get("group_id"), "audit group ID"),
                _required_text(case.get("queue_id"), "queue ID"),
            ): case
            for group in audit_groups
            for case in _documents(group.get("cases"), "audit cases")
        },
    )


def _render_comparison_arms(
    plan: ReferenceRenderPlan,
    staging: Path,
    service: FailureReferencePreviewService,
    audit: _ComparisonAudit,
) -> tuple[
    list[str], list[JsonDocument], dict[tuple[str, str], JsonDocument], set[str]
]:
    reports: list[str] = []
    arm_documents: list[JsonDocument] = []
    complete_by_arm: dict[str, set[str]] = {}
    copied_controls: dict[tuple[str, str], JsonDocument] = {}
    for arm in plan.arms:
        report, arm_document, complete_ids = _render_comparison_arm(
            plan, staging, service, audit, copied_controls, arm
        )
        reports.append(report)
        arm_documents.append(arm_document)
        complete_by_arm[arm["arm_id"]] = complete_ids
    shared = set(plan.queue_ids)
    for values in complete_by_arm.values():
        shared &= values
    return reports, arm_documents, copied_controls, shared


def _render_comparison_arm(
    plan: ReferenceRenderPlan,
    staging: Path,
    service: FailureReferencePreviewService,
    audit: _ComparisonAudit,
    copied_controls: dict[tuple[str, str], JsonDocument],
    arm: _ReferenceRenderArm,
) -> tuple[str, JsonDocument, set[str]]:
    arm_id = arm["arm_id"]
    arm_root = staging / "arms" / arm_id
    (arm_root / "audio").mkdir(parents=True)
    report_samples: list[JsonDocument] = []
    renders: list[JsonDocument] = []
    complete_ids: set[str] = set()
    for position, sample in enumerate(arm["samples"], start=1):
        report_sample, render, complete = _render_comparison_sample(
            plan, arm_root, service, audit, copied_controls, sample, position
        )
        report_samples.append(report_sample)
        renders.append(render)
        if complete:
            complete_ids.add(sample["queue_id"])
    report = {
        "schema": "vntts.voice-model-report",
        "schema_version": 1,
        "model_id": arm_id,
        "provider": "reference-render-comparison",
        "backend": "reference-render-comparison",
        "model": "one exact alternative reference per sample",
        "samples": report_samples,
    }
    report_path = arm_root / "report.json"
    atomic_write_json(report_path, report)
    report_relative = report_path.relative_to(staging).as_posix()
    return (
        report_relative,
        {
            "arm_id": arm_id,
            "report": report_relative,
            "report_sha256": sha256_file(report_path),
            "complete_count": len(complete_ids),
            "failure_count": len(renders) - len(complete_ids),
            "renders": renders,
        },
        complete_ids,
    )


def _render_comparison_sample(
    plan: ReferenceRenderPlan,
    arm_root: Path,
    service: FailureReferencePreviewService,
    audit: _ComparisonAudit,
    copied_controls: dict[tuple[str, str], JsonDocument],
    sample: _ReferenceRenderSample,
    position: int,
) -> tuple[JsonDocument, JsonDocument, bool]:
    queue_id = sample["queue_id"]
    case = audit.cases[(sample["case_group_id"], queue_id)]
    candidate_group = audit.groups[sample["candidate_group_id"]]
    candidate = next(
        value
        for value in _documents(candidate_group.get("candidates"), "audit candidates")
        if value.get("candidate_id") == sample["candidate_id"]
    )
    _copy_comparison_control(plan, arm_root.parents[1], copied_controls, sample)
    base_record = {
        "id": queue_id,
        "line_id": case["line_id"],
        "text": case["text"],
        "text_sha256": case["text_sha256"],
        "case_group_id": sample["case_group_id"],
        "candidate_group_id": sample["candidate_group_id"],
        "candidate_id": sample["candidate_id"],
        "reference_sha256": candidate["sha256"],
    }
    try:
        preview = service.generate(
            sample["case_group_id"],
            sample["candidate_id"],
            _required_text(case.get("text"), "case text"),
            candidate_group_id=sample["candidate_group_id"],
        )
    except FailureReferencePreviewCancelled:
        raise
    except FailureReferencePreviewIncomplete as error:
        failed = {**base_record, "outcome": "error", "error": str(error)}
        return failed, failed, False
    relative_audio = Path("audio") / f"{position:04d}.wav"
    target = arm_root / relative_audio
    target.write_bytes(preview.payload)
    if sha256_file(target) != preview.audio_sha256:
        raise ReferenceRenderComparisonError(
            "Rendered alternative-reference audio checksum changed"
        )
    complete = {
        **base_record,
        "outcome": "complete",
        "audio": relative_audio.as_posix(),
        "audio_sha256": preview.audio_sha256,
        "sample_rate": preview.sample_rate,
        "backend": preview.backend,
        "model": preview.model,
        "generation_profile": preview.generation_profile,
        "seed": preview.seed,
    }
    return complete, complete, True


def _copy_comparison_control(
    plan: ReferenceRenderPlan,
    staging: Path,
    copied_controls: dict[tuple[str, str], JsonDocument],
    sample: _ReferenceRenderSample,
) -> None:
    control_key = (sample["candidate_group_id"], sample["candidate_id"])
    if control_key in copied_controls:
        return
    control = prepare_failure_reference_audio(plan.audit_directory, *control_key)
    control_relative = (
        Path("controls")
        / sample["candidate_group_id"]
        / f"{sample['candidate_id']}{control.path.suffix.lower()}"
    )
    control_target = staging / control_relative
    control_target.parent.mkdir(parents=True, exist_ok=True)
    control_target.write_bytes(control.payload)
    if sha256_file(control_target) != control.sha256:
        raise ReferenceRenderComparisonError(
            "Copied alternative reference checksum changed"
        )
    copied_controls[control_key] = {
        "group_id": sample["candidate_group_id"],
        "candidate_id": sample["candidate_id"],
        "audio": control_relative.as_posix(),
        "sha256": control.sha256,
    }


def _comparison_body(
    plan: ReferenceRenderPlan,
    reports: list[str],
    arm_documents: list[JsonDocument],
    copied_controls: dict[tuple[str, str], JsonDocument],
    shared: set[str],
) -> JsonDocument:
    return {
        "schema": REFERENCE_RENDER_SCHEMA,
        "schema_version": REFERENCE_RENDER_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_plan": str(plan.path),
        "input_plan_sha256": plan.sha256,
        "audit": str(plan.audit_directory),
        "audit_id": plan.audit_id,
        "audit_sha256": sha256_file(plan.audit_directory / "audit.json"),
        "queue_ids": list(plan.queue_ids),
        "controls": sorted(
            copied_controls.values(),
            key=lambda value: (value["group_id"], value["candidate_id"]),
        ),
        "arms": arm_documents,
        "reports": reports,
        "complete_pair_queue_ids": sorted(shared),
        "policy": {
            "render_only": True,
            "generation_state_mutated": False,
            "review_decision_inferred": False,
            "requires_human_listening": True,
        },
    }


def create_reference_render_listening(
    comparison_directory: str | Path,
    output_directory: str | Path,
    *,
    seed: int = 0,
    arm_ids: Iterable[object] | None = None,
) -> Path:
    """Create a blind session for one exact complete pair of comparison arms."""
    supplied = Path(comparison_directory).expanduser()
    if supplied.is_symlink():
        raise ReferenceRenderComparisonError("Reference render comparison is a symlink")
    root = supplied.resolve()
    document = _load_comparison_document(root)
    arms_by_id = {
        _safe_id(value.get("arm_id"), "arm ID"): value
        for value in _documents(document.get("arms"), "comparison arms")
    }
    selected_arm_ids = (
        tuple(arms_by_id)
        if arm_ids is None
        else tuple(_safe_id(value, "listening arm ID") for value in arm_ids)
    )
    if len(selected_arm_ids) != 2 or len(set(selected_arm_ids)) != 2:
        raise ReferenceRenderComparisonError(
            "Reference render listening requires exactly two distinct arms"
        )
    unknown = [value for value in selected_arm_ids if value not in arms_by_id]
    if unknown:
        raise ReferenceRenderComparisonError(
            "Reference render listening arm is absent: " + ", ".join(unknown)
        )
    sample_ids = {
        _required_text(value, "queue ID")
        for value in _values(document.get("queue_ids"), "comparison queue IDs")
    }
    for arm_id in selected_arm_ids:
        sample_ids &= {
            _required_text(value.get("id"), "render ID")
            for value in _documents(
                arms_by_id[arm_id].get("renders"), "comparison renders"
            )
            if value.get("outcome") == "complete"
        }
    if not sample_ids:
        raise ReferenceRenderComparisonError(
            "Selected reference render arms have no complete matched samples"
        )
    reports = [
        _contained_file(root, arms_by_id[arm_id]["report"])
        for arm_id in selected_arm_ids
    ]
    try:
        return create_listening_session_from_reports(
            reports, output_directory, seed=seed, sample_ids=sorted(sample_ids)
        )
    except ModelListeningError as error:
        raise ReferenceRenderComparisonError(str(error)) from error


def load_reference_render_comparison_document(
    directory: str | Path,
) -> JsonDocument:
    """Load and validate every immutable artifact in one render comparison."""
    supplied = Path(directory).expanduser()
    if supplied.is_symlink():
        raise ReferenceRenderComparisonError("Reference render comparison is a symlink")
    return _load_comparison_document(supplied.resolve())


def import_reference_render_preference(
    audit_directory: str | Path,
    comparison_directory: str | Path,
    listening_session: str | Path,
    queue_id: object,
) -> ReferenceRenderSelection:
    """Bind one completed blind preference to one fresh exact failure audit."""
    queue_id = _required_text(queue_id, "queue ID")
    context = _preference_context(
        audit_directory, comparison_directory, listening_session
    )
    groups = _preference_audit_groups(context, queue_id)
    selection = _preference_listening_selection(context, queue_id)
    source = _source_reference(selection.render, groups)
    fresh = _fresh_reference(groups, source, queue_id, selection)
    snapshots = _reference_selection_snapshots(
        context.comparison_root,
        context.session_path,
        context.session_path.with_name("report.json"),
        context.source_audit_path,
    )
    authority = _selection_authority(context, selection, source, snapshots, queue_id)
    return _save_reference_selection(
        context, groups, selection, source, fresh, authority, snapshots, queue_id
    )


def _preference_context(
    audit_directory: str | Path,
    comparison_directory: str | Path,
    listening_session: str | Path,
) -> _PreferenceContext:
    arguments = tuple(
        Path(value).expanduser()
        for value in (audit_directory, comparison_directory, listening_session)
    )
    if any(path.is_symlink() for path in arguments):
        raise ReferenceRenderComparisonError(
            "Reference selection inputs must not be symlinks"
        )
    audit_root, comparison_root, session_path = (path.resolve() for path in arguments)
    try:
        fresh_audit = load_failure_reference_audit(audit_root)
        comparison = _load_comparison_document(comparison_root)
        session = load_listening_session(session_path)
    except (FailureReferenceAuditError, ModelListeningError) as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    source_audit_root = _planned_directory(comparison_root, comparison.get("audit"))
    try:
        source_audit = load_failure_reference_audit(source_audit_root)
    except FailureReferenceAuditError as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    source_audit_path = source_audit_root / "audit.json"
    if source_audit.audit_id != comparison.get("audit_id") or sha256_file(
        source_audit_path
    ) != _required_sha256(comparison.get("audit_sha256"), "source audit hash"):
        raise ReferenceRenderComparisonError(
            "Reference render source audit authority changed"
        )
    return _PreferenceContext(
        audit_root,
        comparison_root,
        session_path,
        fresh_audit,
        source_audit_root,
        source_audit,
        source_audit_path,
        comparison,
        session,
    )


def _preference_audit_groups(
    context: _PreferenceContext, queue_id: str
) -> _PreferenceAuditGroups:
    fresh_document, fresh_key = _load_audit_documents(context.audit_root)
    source_document, source_key = _load_audit_documents(context.source_audit_root)
    return _PreferenceAuditGroups(
        _one_group_for_queue(fresh_document, queue_id, "fresh audit"),
        _groups_by_id(source_document),
        _groups_by_id(source_key),
        _groups_by_id(fresh_key),
    )


def _groups_by_id(document: JsonDocument) -> dict[str, JsonDocument]:
    return {
        _required_text(value.get("group_id"), "audit group ID"): value
        for value in _documents(document.get("groups"), "audit groups")
    }


def _preference_listening_selection(
    context: _PreferenceContext, queue_id: str
) -> _PreferenceListeningSelection:
    trial, assignment, selected_side, selected_arm_id = _selected_listening_trial(
        context.comparison_root,
        context.comparison,
        context.session_path,
        context.session,
        queue_id,
    )
    selected_arm = next(
        (
            value
            for value in _documents(context.comparison.get("arms"), "comparison arms")
            if value.get("arm_id") == selected_arm_id
        ),
        None,
    )
    if selected_arm is None:
        raise ReferenceRenderComparisonError(
            "Blind preference selected an unknown reference-render arm"
        )
    selected_render = next(
        (
            value
            for value in _documents(selected_arm.get("renders"), "comparison renders")
            if value.get("id") == queue_id and value.get("outcome") == "complete"
        ),
        None,
    )
    if selected_render is None:
        raise ReferenceRenderComparisonError(
            "Blind preference selected no complete exact render"
        )
    audio_sha256 = _required_sha256(
        selected_render.get("audio_sha256"), "selected render hash"
    )
    if (
        _document(trial.get("audio_sha256"), "trial audio hashes").get(selected_side)
        != audio_sha256
        or _document(assignment.get(selected_side), "trial assignment").get(
            "audio_sha256"
        )
        != audio_sha256
    ):
        raise ReferenceRenderComparisonError(
            "Blind preference audio no longer matches the selected render"
        )
    selected_audio = _contained_file(
        context.comparison_root / "arms" / selected_arm_id, selected_render.get("audio")
    )
    source = Path(
        _required_text(
            _document(assignment.get(selected_side), "trial assignment").get("source"),
            "assignment source",
        )
    ).expanduser()
    if source.is_symlink() or source.resolve() != selected_audio:
        raise ReferenceRenderComparisonError(
            "Blind preference source no longer matches the selected render"
        )
    return _PreferenceListeningSelection(
        trial,
        selected_side,
        _SelectedComparisonRender(
            selected_arm_id,
            selected_render,
            audio_sha256,
            _required_sha256(
                selected_render.get("candidate_group_id"), "candidate group ID"
            ),
            _required_text(selected_render.get("candidate_id"), "candidate ID"),
        ),
    )


def _source_reference(
    render: _SelectedComparisonRender, groups: _PreferenceAuditGroups
) -> _SourceReference:
    group = groups.source_groups.get(render.candidate_group_id)
    private_group = groups.source_private_groups.get(render.candidate_group_id)
    if group is None or private_group is None:
        raise ReferenceRenderComparisonError(
            "Selected reference is absent from its source audit"
        )
    candidate = _candidate_by_id(group, render.candidate_id)
    private_candidate = _candidate_by_id(private_group, render.candidate_id)
    sha256 = _required_sha256(
        render.render.get("reference_sha256"), "selected reference hash"
    )
    if (
        candidate is None
        or private_candidate is None
        or candidate.get("sha256") != sha256
        or private_candidate.get("source_sha256") != sha256
    ):
        raise ReferenceRenderComparisonError(
            "Selected reference no longer matches its source audit"
        )
    return _SourceReference(group, private_group, private_candidate, sha256)


def _candidate_by_id(group: JsonDocument, candidate_id: str) -> JsonDocument | None:
    return next(
        (
            value
            for value in _documents(group.get("candidates"), "audit candidates")
            if value.get("candidate_id") == candidate_id
        ),
        None,
    )


def _fresh_reference(
    groups: _PreferenceAuditGroups,
    source: _SourceReference,
    queue_id: str,
    selection: _PreferenceListeningSelection,
) -> _FreshReference:
    group = groups.fresh_group
    private_group = groups.fresh_private_groups[
        _required_text(group.get("group_id"), "audit group ID")
    ]
    candidates = [
        value
        for value in _documents(group.get("candidates"), "audit candidates")
        if value.get("sha256") == source.sha256
    ]
    if len(candidates) != 1:
        raise ReferenceRenderComparisonError(
            "Selected reference is absent or ambiguous in the fresh audit"
        )
    candidate = candidates[0]
    private_candidate = _candidate_by_id(
        private_group, _required_text(candidate.get("candidate_id"), "candidate ID")
    )
    if (
        group.get("synthesis_voice_character")
        != source.group.get("synthesis_voice_character")
        or private_group.get("control_character")
        != source.private_group.get("control_character")
        or private_group.get("speaker") != source.private_group.get("speaker")
        or private_candidate is None
        or private_candidate.get("source_sha256") != source.sha256
    ):
        raise ReferenceRenderComparisonError(
            "Selected reference identity changed in the fresh audit"
        )
    fresh_case = next(
        value
        for value in _documents(group.get("cases"), "audit cases")
        if value.get("queue_id") == queue_id
    )
    render = selection.render.render
    trial = selection.trial
    if (
        fresh_case.get("line_id") != render.get("line_id")
        or fresh_case.get("text") != render.get("text")
        or fresh_case.get("text_sha256") != render.get("text_sha256")
        or trial.get("line_id") != render.get("line_id")
        or trial.get("text") != render.get("text")
        or trial.get("text_sha256") != render.get("text_sha256")
    ):
        raise ReferenceRenderComparisonError("Selected reference text identity changed")
    return _FreshReference(candidate)


def _selection_authority(
    context: _PreferenceContext,
    selection: _PreferenceListeningSelection,
    source: _SourceReference,
    snapshots: Mapping[Path, str],
    queue_id: str,
) -> JsonDocument:
    render = selection.render
    report_path = context.session_path.with_name("report.json")
    return {
        "schema": "vntts.authoring-reference-render-selection",
        "schema_version": 1,
        "comparison_id": context.comparison["comparison_id"],
        "comparison_sha256": snapshots[context.comparison_root / "comparison.json"],
        "source_audit_id": context.source_audit.audit_id,
        "source_audit_sha256": snapshots[context.source_audit_path],
        "listening_session_sha256": snapshots[context.session_path],
        "listening_key_sha256": snapshots[
            context.session_path.with_name(".blind-key.json")
        ],
        "listening_report_sha256": snapshots[report_path],
        "trial_id": selection.trial["trial_id"],
        "selected_side": selection.selected_side,
        "selected_arm_id": render.arm_id,
        "selected_render_sha256": render.audio_sha256,
        "source_candidate_group_id": render.candidate_group_id,
        "source_candidate_id": render.candidate_id,
        "source_reference": source.private_candidate["source_reference"],
        "selected_reference_sha256": source.sha256,
        "queue_id": queue_id,
        "text_sha256": render.render["text_sha256"],
    }


def _save_reference_selection(
    context: _PreferenceContext,
    groups: _PreferenceAuditGroups,
    selection: _PreferenceListeningSelection,
    source: _SourceReference,
    fresh: _FreshReference,
    authority: JsonDocument,
    snapshots: Mapping[Path, str],
    queue_id: str,
) -> ReferenceRenderSelection:
    group_id = _required_text(groups.fresh_group.get("group_id"), "audit group ID")
    candidate_id = _required_text(fresh.candidate.get("candidate_id"), "candidate ID")
    current = _document(
        load_failure_reference_decisions(context.audit_root), "reference decisions"
    )
    existing = next(
        (
            value
            for value in _documents(current.get("decisions"), "reference decisions")
            if value.get("group_id") == groups.fresh_group.get("group_id")
        ),
        None,
    )
    if existing is not None:
        if (
            existing.get("decision") != fresh.candidate["candidate_id"]
            or existing.get("selection_authority") != authority
        ):
            raise ReferenceRenderComparisonError(
                "Fresh reference audit already has a different decision"
            )
        return ReferenceRenderSelection(
            context.audit_root,
            context.fresh_audit.audit_id,
            group_id,
            candidate_id,
            queue_id,
            selection.render.arm_id,
            source.sha256,
            _required_text(current.get("decision_set_id"), "decision set ID"),
            False,
        )
    _assert_reference_selection_snapshots(snapshots)
    try:
        decisions = record_failure_reference_decision(
            context.audit_root, group_id, candidate_id, selection_authority=authority
        )
    except FailureReferenceAuditError as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    return ReferenceRenderSelection(
        context.audit_root,
        context.fresh_audit.audit_id,
        group_id,
        candidate_id,
        queue_id,
        selection.render.arm_id,
        source.sha256,
        _required_text(decisions.get("decision_set_id"), "decision set ID"),
        True,
    )


def _load_comparison_document(root: Path) -> JsonDocument:
    document = _read_comparison_document(root)
    controls, arms, reports = _comparison_inventory(document)
    _validate_comparison_controls(root, controls)
    _validate_comparison_arms(root, arms, reports)
    return document


def _read_comparison_document(root: Path) -> JsonDocument:
    path = _contained_file(root, "comparison.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != REFERENCE_RENDER_SCHEMA
        or document.get("schema_version") != REFERENCE_RENDER_VERSION
        or document.get("comparison_id")
        != canonical_document_sha256(
            {key: value for key, value in document.items() if key != "comparison_id"}
        )
    ):
        raise ReferenceRenderComparisonError(
            "Reference render comparison identity is invalid"
        )
    return document


def _comparison_inventory(
    document: JsonDocument,
) -> tuple[list[object], list[object], list[object]]:
    controls = document.get("controls")
    arms = document.get("arms")
    reports = document.get("reports")
    queue_ids = document.get("queue_ids")
    complete_pair_queue_ids = document.get("complete_pair_queue_ids")
    if (
        not isinstance(controls, list)
        or not isinstance(arms, list)
        or len(arms) < 2
        or not isinstance(reports, list)
        or len(reports) != len(arms)
        or not isinstance(queue_ids, list)
        or not queue_ids
        or any(not isinstance(value, str) or not value for value in queue_ids)
        or len(queue_ids) != len(set(queue_ids))
        or not isinstance(complete_pair_queue_ids, list)
        or any(
            not isinstance(value, str) or not value for value in complete_pair_queue_ids
        )
        or len(complete_pair_queue_ids) != len(set(complete_pair_queue_ids))
    ):
        raise ReferenceRenderComparisonError(
            "Reference render comparison inventory is invalid"
        )
    return controls, arms, reports


def _validate_comparison_controls(root: Path, controls: list[object]) -> None:
    for control in controls:
        if not isinstance(control, dict):
            raise ReferenceRenderComparisonError(
                "Reference render comparison control is invalid"
            )
        path = _contained_file(root, control.get("audio"))
        if sha256_file(path) != _required_sha256(control.get("sha256"), "control hash"):
            raise ReferenceRenderComparisonError(
                "Reference render comparison control changed"
            )


def _validate_comparison_arms(
    root: Path, arms: list[object], reports: list[object]
) -> None:
    arm_reports: list[str] = []
    arm_ids: set[str] = set()
    for arm in arms:
        arm_id = _comparison_arm_id(arm)
        if arm_id in arm_ids:
            raise ReferenceRenderComparisonError(
                "Reference render comparison arm IDs repeat"
            )
        arm_ids.add(arm_id)
        report = _validate_comparison_arm(root, arm, arm_id)
        arm_reports.append(report)
    if arm_reports != reports or len(set(reports)) != len(reports):
        raise ReferenceRenderComparisonError(
            "Reference render comparison report inventory changed"
        )


def _comparison_arm_id(arm: object) -> str:
    if not isinstance(arm, dict):
        raise ReferenceRenderComparisonError(
            "Reference render comparison arm is invalid"
        )
    return _safe_id(arm.get("arm_id"), "arm ID")


def _validate_comparison_arm(root: Path, arm: object, arm_id: str) -> str:
    if not isinstance(arm, dict):
        raise ReferenceRenderComparisonError(
            "Reference render comparison arm is invalid"
        )
    report_relative = _required_text(arm.get("report"), "report path")
    report = _contained_file(root, report_relative)
    if sha256_file(report) != _required_sha256(arm.get("report_sha256"), "report hash"):
        raise ReferenceRenderComparisonError(
            "Reference render comparison report changed"
        )
    renders = arm.get("renders")
    if not isinstance(renders, list):
        raise ReferenceRenderComparisonError(
            "Reference render comparison renders are invalid"
        )
    for render in renders:
        _validate_comparison_render(root, arm_id, render)
    return report_relative


def _validate_comparison_render(root: Path, arm_id: str, render: object) -> None:
    if not isinstance(render, dict) or render.get("outcome") not in {
        "complete",
        "error",
    }:
        raise ReferenceRenderComparisonError(
            "Reference render comparison outcome is invalid"
        )
    if render.get("outcome") != "complete":
        return
    audio = _contained_file(root / "arms" / arm_id, render.get("audio"))
    if sha256_file(audio) != _required_sha256(
        render.get("audio_sha256"), "rendered audio hash"
    ):
        raise ReferenceRenderComparisonError(
            "Reference render comparison audio changed"
        )


def _selected_listening_trial(
    comparison_root: Path,
    comparison: JsonDocument,
    session_path: Path,
    session: ListeningSession,
    queue_id: str,
) -> tuple[JsonDocument, JsonDocument, str, str]:
    session_document = _document(session, "listening session")
    trial, selected_side = _selected_completed_trial(session_document, queue_id)
    key, report = _load_listening_authority(session_path)
    assignment = _listening_assignment(key, trial)
    arms_by_id, selected_arm_ids = _listening_arms(comparison, key)
    _validate_listening_sources(comparison_root, arms_by_id, selected_arm_ids, key)
    _validate_listening_arm_samples(arms_by_id, selected_arm_ids, queue_id)
    _validate_listening_report(session_path, report)
    selected = assignment.get(selected_side)
    if not isinstance(selected, dict):
        raise ReferenceRenderComparisonError(
            "Reference render listening selection is malformed"
        )
    return (
        trial,
        assignment,
        selected_side,
        _safe_id(selected.get("model_id"), "selected arm ID"),
    )


def _selected_completed_trial(
    session_document: JsonDocument, queue_id: str
) -> tuple[JsonDocument, str]:
    if session_document.get("completed_count") != session_document.get("trial_count"):
        raise ReferenceRenderComparisonError(
            "Reference render listening session is incomplete"
        )
    matching_trials = [
        trial
        for trial in _documents(session_document.get("trials"), "listening trials")
        if trial.get("queue_id")
        == f"corpus:{queue_id}:{_required_text(trial.get('text_sha256'), 'trial text hash')[:16]}"
    ]
    if len(matching_trials) != 1:
        raise ReferenceRenderComparisonError(
            "Reference render listening trial is absent or ambiguous"
        )
    trial = matching_trials[0]
    rating = trial.get("rating")
    if (
        not isinstance(rating, dict)
        or rating.get("preference") not in {"a", "b"}
        or rating.get("acceptability") == "neither"
    ):
        raise ReferenceRenderComparisonError(
            "Reference render listening did not select one acceptable arm"
        )
    return trial, rating["preference"]


def _load_listening_authority(session_path: Path) -> tuple[JsonDocument, JsonDocument]:
    key_path = session_path.with_name(".blind-key.json")
    report_path = session_path.with_name("report.json")
    try:
        key = _document(
            json.loads(key_path.read_text(encoding="utf-8")), "listening key"
        )
        report = _document(
            json.loads(report_path.read_text(encoding="utf-8")), "listening report"
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    return key, report


def _listening_assignment(key: JsonDocument, trial: JsonDocument) -> JsonDocument:
    assignments = [
        value
        for value in _documents(key.get("assignments"), "listening assignments")
        if value.get("trial_id") == trial.get("trial_id")
    ]
    if len(assignments) != 1:
        raise ReferenceRenderComparisonError(
            "Reference render listening assignment is absent or ambiguous"
        )
    return assignments[0]


def _listening_arms(
    comparison: JsonDocument, key: JsonDocument
) -> tuple[dict[str, JsonDocument], list[str]]:
    arms_by_id = {
        _safe_id(value.get("arm_id"), "arm ID"): value
        for value in _documents(comparison.get("arms"), "comparison arms")
    }
    model_records = [
        value for value in _documents(key.get("models"), "listening models")
    ]
    selected_arm_ids = [
        _safe_id(value.get("model_id"), "listening model ID") for value in model_records
    ]
    if (
        len(selected_arm_ids) != 2
        or len(set(selected_arm_ids)) != 2
        or any(value not in arms_by_id for value in selected_arm_ids)
    ):
        raise ReferenceRenderComparisonError(
            "Reference render listening must bind exactly two known arms"
        )
    return arms_by_id, selected_arm_ids


def _validate_listening_sources(
    comparison_root: Path,
    arms_by_id: dict[str, JsonDocument],
    selected_arm_ids: list[str],
    key: JsonDocument,
) -> None:
    expected_reports = {
        str(
            _contained_file(comparison_root, arms_by_id[arm_id].get("report"))
        ): _required_sha256(arms_by_id[arm_id].get("report_sha256"), "report hash")
        for arm_id in selected_arm_ids
    }
    actual_reports: dict[str, str] = {}
    sources = _documents(key.get("sources"), "listening sources")
    for source in sources:
        path = Path(_required_text(source.get("path"), "listening source")).expanduser()
        if path.is_symlink():
            raise ReferenceRenderComparisonError(
                "Reference render listening source is a symlink"
            )
        actual_reports[str(path.resolve())] = _required_sha256(
            source.get("sha256"), "listening source hash"
        )
    if len(actual_reports) != len(sources) or actual_reports != expected_reports:
        raise ReferenceRenderComparisonError(
            "Reference render listening sources changed"
        )


def _validate_listening_arm_samples(
    arms_by_id: dict[str, JsonDocument], selected_arm_ids: list[str], queue_id: str
) -> None:
    actual_models = set(selected_arm_ids)
    if any(
        not any(
            render.get("id") == queue_id and render.get("outcome") == "complete"
            for render in _documents(
                arms_by_id[arm_id].get("renders"), "comparison renders"
            )
        )
        for arm_id in actual_models
    ):
        raise ReferenceRenderComparisonError("Reference render listening arms changed")


def _validate_listening_report(session_path: Path, report: JsonDocument) -> None:
    try:
        expected_report = aggregate_listening_report(session_path)
    except ModelListeningError as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    comparable_fields = set(expected_report) - {"generated_at"}
    if any(report.get(field) != expected_report[field] for field in comparable_fields):
        raise ReferenceRenderComparisonError(
            "Reference render listening report is stale or changed"
        )


def _load_audit_documents(directory: Path) -> tuple[JsonDocument, JsonDocument]:
    try:
        document = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
        key = json.loads((directory / ".blind-key.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    return _document(document, "audit document"), _document(key, "audit key")


def _one_group_for_queue(
    document: JsonDocument, queue_id: str, label: str
) -> JsonDocument:
    groups = [
        group
        for group in _documents(document.get("groups"), "audit groups")
        if queue_id
        in {
            _required_text(case.get("queue_id"), "queue ID")
            for case in _documents(group.get("cases"), "audit cases")
        }
    ]
    if len(groups) != 1 or groups[0].get("case_count") != 1:
        raise ReferenceRenderComparisonError(
            f"Reference render {label} must contain exactly one selected case"
        )
    return groups[0]


def _reference_selection_snapshots(
    comparison_root: Path,
    session_path: Path,
    report_path: Path,
    source_audit_path: Path,
) -> dict[Path, str]:
    paths = (
        comparison_root / "comparison.json",
        session_path,
        session_path.with_name(".blind-key.json"),
        report_path,
        source_audit_path,
    )
    snapshots = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ReferenceRenderComparisonError(
                "Reference selection authority is missing or unsafe"
            )
        snapshots[path] = sha256_file(path)
    return snapshots


def _assert_reference_selection_snapshots(snapshots: Mapping[Path, str]) -> None:
    for path, digest in snapshots.items():
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ReferenceRenderComparisonError(
                "Reference selection authority changed before decision save"
            )


def _assert_plan_and_audit_unchanged(plan: ReferenceRenderPlan) -> None:
    if sha256_file(plan.path) != plan.sha256:
        raise ReferenceRenderComparisonError(
            "Reference render plan changed during publication"
        )
    try:
        audit = load_failure_reference_audit(plan.audit_directory)
    except FailureReferenceAuditError as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    if audit.audit_id != plan.audit_id:
        raise ReferenceRenderComparisonError(
            "Reference render audit changed during publication"
        )


def _read_audit_document(directory: str | Path) -> JsonDocument:
    try:
        return _document(
            json.loads((Path(directory) / "audit.json").read_text(encoding="utf-8")),
            "audit document",
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ReferenceRenderComparisonError(str(error)) from error


def _planned_directory(root: Path, value: object) -> Path:
    text = _required_text(value, "audit path")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = root / path
    if path.is_symlink():
        raise ReferenceRenderComparisonError("Reference render audit is a symlink")
    path = path.resolve()
    if not path.is_dir():
        raise ReferenceRenderComparisonError("Reference render audit is missing")
    return path


def _contained_file(root: str | Path, value: object) -> Path:
    root = Path(root).resolve()
    text = _required_text(value, "artifact path")
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReferenceRenderComparisonError(
            "Reference render artifact leaves its root"
        )
    path = root / relative
    if path.is_symlink():
        raise ReferenceRenderComparisonError("Reference render artifact is a symlink")
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ReferenceRenderComparisonError(
            "Reference render artifact leaves its root"
        ) from error
    if not path.is_file():
        raise ReferenceRenderComparisonError(
            f"Reference render artifact is missing: {path}"
        )
    return path


def _safe_id(value: object, label: str) -> str:
    text = _required_text(value, label)
    if text in {".", ".."} or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in text
    ):
        raise ReferenceRenderComparisonError(
            f"Reference render {label} must be a safe lowercase identifier"
        )
    return text


def _source_reference_family(group: JsonDocument) -> str:
    value = _required_text(
        group.get("synthesis_voice_character"), "synthesis voice character"
    )
    prefix = "Source reference "
    marker = " cluster-"
    if not value.startswith(prefix) or marker not in value:
        raise ReferenceRenderComparisonError(
            "Cross-group reference rendering is restricted to source-reference "
            "character families"
        )
    character, separator, _cluster = value[len(prefix) :].partition(marker)
    if not separator or not character:
        raise ReferenceRenderComparisonError(
            "Cross-group source-reference identity is malformed"
        )
    return character


def _document(value: object, label: str) -> JsonDocument:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ReferenceRenderComparisonError(f"Reference render {label} is malformed")
    return {key: item for key, item in value.items()}


def _documents(value: object, label: str) -> list[JsonDocument]:
    if not isinstance(value, list):
        raise ReferenceRenderComparisonError(f"Reference render {label} is malformed")
    return [_document(item, label) for item in value]


def _values(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ReferenceRenderComparisonError(f"Reference render {label} is malformed")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReferenceRenderComparisonError(
            f"Reference render {label} must be non-empty text"
        )
    return value


def _required_sha256(value: object, label: str) -> str:
    text = _required_text(value, label)
    if not is_lowercase_sha256(text):
        raise ReferenceRenderComparisonError(
            f"Reference render {label} must be lowercase SHA-256"
        )
    return text


__all__ = [
    "REFERENCE_RENDER_INPUT_SCHEMA",
    "REFERENCE_RENDER_INPUT_VERSION",
    "ReferenceRenderComparison",
    "ReferenceRenderComparisonError",
    "ReferenceRenderPlan",
    "ReferenceRenderSelection",
    "create_reference_render_listening",
    "import_reference_render_preference",
    "load_reference_render_plan",
    "load_reference_render_comparison_document",
    "publish_reference_render_comparison",
]

"""Immutable render-only comparisons for alternative voice references."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.failure_reference_audit import (
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
from vntts.authoring.game_pack import _rename_directory_no_replace
from vntts.authoring.listening import (
    ModelListeningError,
    aggregate_listening_report,
    create_listening_session_from_reports,
    load_listening_session,
)
from vntts.document_identity import canonical_document_sha256

REFERENCE_RENDER_INPUT_SCHEMA = "vntts.authoring-reference-render-input"
REFERENCE_RENDER_INPUT_VERSION = 1
REFERENCE_RENDER_SCHEMA = "vntts.authoring-reference-render-comparison"
REFERENCE_RENDER_VERSION = 1


class ReferenceRenderComparisonError(RuntimeError):
    """An alternative-reference comparison is malformed or unsafe."""


@dataclass(frozen=True)
class ReferenceRenderPlan:
    path: Path
    sha256: str
    audit_directory: Path
    audit_id: str
    arms: tuple[dict, ...]
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

    def to_dict(self):
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


def load_reference_render_plan(path):
    """Load a checksum-bound operator plan for exact cases and reference arms."""
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
    audit_directory = _planned_directory(source.parent, document["audit"])
    try:
        audit = load_failure_reference_audit(audit_directory)
    except FailureReferenceAuditError as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    if document["audit_id"] != audit.audit_id:
        raise ReferenceRenderComparisonError("Reference render audit identity changed")
    audit_document = _read_audit_document(audit_directory)
    groups = {value["group_id"]: value for value in audit_document["groups"]}
    arms = document["arms"]
    if not isinstance(arms, list) or len(arms) < 2:
        raise ReferenceRenderComparisonError(
            "Reference render plan requires at least two arms"
        )
    parsed_arms = []
    arm_ids = set()
    expected_queue_ids = None
    selections_by_queue_id = {}
    for arm_index, arm in enumerate(arms, start=1):
        if not isinstance(arm, dict) or set(arm) != {"arm_id", "samples"}:
            raise ReferenceRenderComparisonError(
                f"Reference render arm {arm_index} is malformed"
            )
        arm_id = _safe_id(arm["arm_id"], f"arm {arm_index}")
        if arm_id in arm_ids:
            raise ReferenceRenderComparisonError("Reference render arm IDs repeat")
        arm_ids.add(arm_id)
        samples = arm["samples"]
        if not isinstance(samples, list) or not samples:
            raise ReferenceRenderComparisonError(
                f"Reference render arm {arm_id} has no samples"
            )
        parsed_samples = []
        queue_ids = []
        for sample_index, sample in enumerate(samples, start=1):
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
            case_group = groups.get(case_group_id)
            candidate_group = groups.get(candidate_group_id)
            if case_group is None or candidate_group is None:
                raise ReferenceRenderComparisonError(
                    f"Reference render group is absent for {queue_id}"
                )
            if queue_id not in {
                value["queue_id"] for value in case_group.get("cases", [])
            }:
                raise ReferenceRenderComparisonError(
                    f"Reference render case is absent for {queue_id}"
                )
            if candidate_id not in {
                value["candidate_id"] for value in candidate_group.get("candidates", [])
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
            parsed_samples.append(
                {
                    "queue_id": queue_id,
                    "case_group_id": case_group_id,
                    "candidate_group_id": candidate_group_id,
                    "candidate_id": candidate_id,
                }
            )
            queue_ids.append(queue_id)
            selection = (candidate_group_id, candidate_id)
            prior = selections_by_queue_id.setdefault(queue_id, set())
            if selection in prior:
                raise ReferenceRenderComparisonError(
                    f"Reference render arms repeat the same control for {queue_id}"
                )
            prior.add(selection)
        if len(queue_ids) != len(set(queue_ids)):
            raise ReferenceRenderComparisonError(
                f"Reference render arm {arm_id} repeats queue IDs"
            )
        current_queue_ids = tuple(queue_ids)
        if expected_queue_ids is None:
            expected_queue_ids = current_queue_ids
        elif current_queue_ids != expected_queue_ids:
            raise ReferenceRenderComparisonError(
                "Reference render arms must use the same ordered queue IDs"
            )
        parsed_arms.append({"arm_id": arm_id, "samples": parsed_samples})
    return ReferenceRenderPlan(
        path=source,
        sha256=hashlib.sha256(payload).hexdigest(),
        audit_directory=audit_directory,
        audit_id=audit.audit_id,
        arms=tuple(parsed_arms),
        queue_ids=expected_queue_ids or (),
    )


def publish_reference_render_comparison(
    plan, output_directory, *, backend_factory=None
):
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
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    audit_document = _read_audit_document(plan.audit_directory)
    groups = {value["group_id"]: value for value in audit_document["groups"]}
    cases = {
        (group["group_id"], case["queue_id"]): case
        for group in audit_document["groups"]
        for case in group["cases"]
    }
    service_options = {}
    if backend_factory is not None:
        service_options["backend_factory"] = backend_factory
    service = FailureReferencePreviewService(plan.audit_directory, **service_options)
    reports = []
    arm_documents = []
    complete_by_arm = {}
    copied_controls = {}
    try:
        for arm in plan.arms:
            arm_id = arm["arm_id"]
            arm_root = staging / "arms" / arm_id
            audio_root = arm_root / "audio"
            audio_root.mkdir(parents=True)
            report_samples = []
            renders = []
            complete_ids = set()
            for position, sample in enumerate(arm["samples"], start=1):
                queue_id = sample["queue_id"]
                case = cases[(sample["case_group_id"], queue_id)]
                candidate_group = groups[sample["candidate_group_id"]]
                candidate = next(
                    value
                    for value in candidate_group["candidates"]
                    if value["candidate_id"] == sample["candidate_id"]
                )
                control_key = (
                    sample["candidate_group_id"],
                    sample["candidate_id"],
                )
                if control_key not in copied_controls:
                    control = prepare_failure_reference_audio(
                        plan.audit_directory, *control_key
                    )
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
                        case["text"],
                        candidate_group_id=sample["candidate_group_id"],
                    )
                except FailureReferencePreviewCancelled:
                    raise
                except FailureReferencePreviewIncomplete as error:
                    report_samples.append(
                        {**base_record, "outcome": "error", "error": str(error)}
                    )
                    renders.append(
                        {**base_record, "outcome": "error", "error": str(error)}
                    )
                    continue
                relative_audio = Path("audio") / f"{position:04d}.wav"
                target = arm_root / relative_audio
                target.write_bytes(preview.payload)
                if sha256_file(target) != preview.audio_sha256:
                    raise ReferenceRenderComparisonError(
                        "Rendered alternative-reference audio checksum changed"
                    )
                complete_ids.add(queue_id)
                complete_record = {
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
                report_samples.append(complete_record)
                renders.append(complete_record)
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
            reports.append(report_path.relative_to(staging).as_posix())
            report_sha256 = sha256_file(report_path)
            complete_by_arm[arm_id] = complete_ids
            arm_documents.append(
                {
                    "arm_id": arm_id,
                    "report": report_path.relative_to(staging).as_posix(),
                    "report_sha256": report_sha256,
                    "complete_count": len(complete_ids),
                    "failure_count": len(renders) - len(complete_ids),
                    "renders": renders,
                }
            )
        shared = set(plan.queue_ids)
        for values in complete_by_arm.values():
            shared &= values
        body = {
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
        comparison_id = canonical_document_sha256(body)
        document = {**body, "comparison_id": comparison_id}
        atomic_write_json(staging / "comparison.json", document)
        _assert_plan_and_audit_unchanged(plan)
        _rename_directory_no_replace(staging, output)
        staging = None
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
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def create_reference_render_listening(
    comparison_directory, output_directory, *, seed=0, arm_ids=None
):
    """Create a blind session for one exact complete pair of comparison arms."""
    supplied = Path(comparison_directory).expanduser()
    if supplied.is_symlink():
        raise ReferenceRenderComparisonError("Reference render comparison is a symlink")
    root = supplied.resolve()
    document = _load_comparison_document(root)
    arms_by_id = {value["arm_id"]: value for value in document["arms"]}
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
    sample_ids = set(document["queue_ids"])
    for arm_id in selected_arm_ids:
        sample_ids &= {
            value["id"]
            for value in arms_by_id[arm_id]["renders"]
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


def load_reference_render_comparison_document(directory):
    """Load and validate every immutable artifact in one render comparison."""
    supplied = Path(directory).expanduser()
    if supplied.is_symlink():
        raise ReferenceRenderComparisonError("Reference render comparison is a symlink")
    return _load_comparison_document(supplied.resolve())


def import_reference_render_preference(
    audit_directory,
    comparison_directory,
    listening_session,
    queue_id,
):
    """Bind one completed blind preference to one fresh exact failure audit."""
    queue_id = _required_text(queue_id, "queue ID")
    audit_argument = Path(audit_directory).expanduser()
    comparison_argument = Path(comparison_directory).expanduser()
    session_argument = Path(listening_session).expanduser()
    if (
        audit_argument.is_symlink()
        or comparison_argument.is_symlink()
        or session_argument.is_symlink()
    ):
        raise ReferenceRenderComparisonError(
            "Reference selection inputs must not be symlinks"
        )
    audit_root = audit_argument.resolve()
    comparison_root = comparison_argument.resolve()
    session_path = session_argument.resolve()
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

    fresh_document, fresh_key = _load_audit_documents(audit_root)
    source_document, source_key = _load_audit_documents(source_audit_root)
    fresh_group = _one_group_for_queue(fresh_document, queue_id, "fresh audit")
    source_groups = {value["group_id"]: value for value in source_document["groups"]}
    source_private_groups = {value["group_id"]: value for value in source_key["groups"]}
    fresh_private_groups = {value["group_id"]: value for value in fresh_key["groups"]}

    trial, assignment, selected_side, selected_arm_id = _selected_listening_trial(
        comparison_root,
        comparison,
        session_path,
        session,
        queue_id,
    )
    selected_arm = next(
        (value for value in comparison["arms"] if value["arm_id"] == selected_arm_id),
        None,
    )
    if selected_arm is None:
        raise ReferenceRenderComparisonError(
            "Blind preference selected an unknown reference-render arm"
        )
    selected_render = next(
        (
            value
            for value in selected_arm["renders"]
            if value.get("id") == queue_id and value.get("outcome") == "complete"
        ),
        None,
    )
    if selected_render is None:
        raise ReferenceRenderComparisonError(
            "Blind preference selected no complete exact render"
        )
    render_sha256 = _required_sha256(
        selected_render.get("audio_sha256"), "selected render hash"
    )
    if (
        trial["audio_sha256"][selected_side] != render_sha256
        or assignment[selected_side].get("audio_sha256") != render_sha256
    ):
        raise ReferenceRenderComparisonError(
            "Blind preference audio no longer matches the selected render"
        )
    selected_audio = _contained_file(
        comparison_root / "arms" / selected_arm_id,
        selected_render.get("audio"),
    )
    assignment_source = Path(
        _required_text(assignment[selected_side].get("source"), "assignment source")
    ).expanduser()
    if assignment_source.is_symlink() or assignment_source.resolve() != selected_audio:
        raise ReferenceRenderComparisonError(
            "Blind preference source no longer matches the selected render"
        )

    source_group_id = _required_sha256(
        selected_render.get("candidate_group_id"), "candidate group ID"
    )
    source_candidate_id = _required_text(
        selected_render.get("candidate_id"), "candidate ID"
    )
    source_group = source_groups.get(source_group_id)
    source_private_group = source_private_groups.get(source_group_id)
    if source_group is None or source_private_group is None:
        raise ReferenceRenderComparisonError(
            "Selected reference is absent from its source audit"
        )
    source_candidate = next(
        (
            value
            for value in source_group["candidates"]
            if value["candidate_id"] == source_candidate_id
        ),
        None,
    )
    source_private_candidate = next(
        (
            value
            for value in source_private_group["candidates"]
            if value["candidate_id"] == source_candidate_id
        ),
        None,
    )
    selected_reference_sha256 = _required_sha256(
        selected_render.get("reference_sha256"), "selected reference hash"
    )
    if (
        source_candidate is None
        or source_private_candidate is None
        or source_candidate.get("sha256") != selected_reference_sha256
        or source_private_candidate.get("source_sha256") != selected_reference_sha256
    ):
        raise ReferenceRenderComparisonError(
            "Selected reference no longer matches its source audit"
        )

    fresh_private_group = fresh_private_groups[fresh_group["group_id"]]
    fresh_candidates = [
        value
        for value in fresh_group["candidates"]
        if value.get("sha256") == selected_reference_sha256
    ]
    if len(fresh_candidates) != 1:
        raise ReferenceRenderComparisonError(
            "Selected reference is absent or ambiguous in the fresh audit"
        )
    fresh_candidate = fresh_candidates[0]
    fresh_private_candidate = next(
        (
            value
            for value in fresh_private_group["candidates"]
            if value["candidate_id"] == fresh_candidate["candidate_id"]
        ),
        None,
    )
    if (
        fresh_group.get("synthesis_voice_character")
        != source_group.get("synthesis_voice_character")
        or fresh_private_group.get("control_character")
        != source_private_group.get("control_character")
        or fresh_private_group.get("speaker") != source_private_group.get("speaker")
        or fresh_private_candidate is None
        or fresh_private_candidate.get("source_sha256") != selected_reference_sha256
    ):
        raise ReferenceRenderComparisonError(
            "Selected reference identity changed in the fresh audit"
        )
    fresh_case = next(
        value for value in fresh_group["cases"] if value["queue_id"] == queue_id
    )
    if (
        fresh_case.get("line_id") != selected_render.get("line_id")
        or fresh_case.get("text") != selected_render.get("text")
        or fresh_case.get("text_sha256") != selected_render.get("text_sha256")
        or trial.get("line_id") != selected_render.get("line_id")
        or trial.get("text") != selected_render.get("text")
        or trial.get("text_sha256") != selected_render.get("text_sha256")
    ):
        raise ReferenceRenderComparisonError("Selected reference text identity changed")

    report_path = session_path.with_name("report.json")
    snapshots = _reference_selection_snapshots(
        comparison_root,
        session_path,
        report_path,
        source_audit_path,
    )
    selection_authority = {
        "schema": "vntts.authoring-reference-render-selection",
        "schema_version": 1,
        "comparison_id": comparison["comparison_id"],
        "comparison_sha256": snapshots[comparison_root / "comparison.json"],
        "source_audit_id": source_audit.audit_id,
        "source_audit_sha256": snapshots[source_audit_path],
        "listening_session_sha256": snapshots[session_path],
        "listening_key_sha256": snapshots[session_path.with_name(".blind-key.json")],
        "listening_report_sha256": snapshots[report_path],
        "trial_id": trial["trial_id"],
        "selected_side": selected_side,
        "selected_arm_id": selected_arm_id,
        "selected_render_sha256": render_sha256,
        "source_candidate_group_id": source_group_id,
        "source_candidate_id": source_candidate_id,
        "source_reference": source_private_candidate["source_reference"],
        "selected_reference_sha256": selected_reference_sha256,
        "queue_id": queue_id,
        "text_sha256": selected_render["text_sha256"],
    }
    current = load_failure_reference_decisions(audit_root)
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
            raise ReferenceRenderComparisonError(
                "Fresh reference audit already has a different decision"
            )
        return ReferenceRenderSelection(
            audit_root,
            fresh_audit.audit_id,
            fresh_group["group_id"],
            fresh_candidate["candidate_id"],
            queue_id,
            selected_arm_id,
            selected_reference_sha256,
            current["decision_set_id"],
            False,
        )
    _assert_reference_selection_snapshots(snapshots)
    try:
        decisions = record_failure_reference_decision(
            audit_root,
            fresh_group["group_id"],
            fresh_candidate["candidate_id"],
            selection_authority=selection_authority,
        )
    except FailureReferenceAuditError as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    return ReferenceRenderSelection(
        audit_root,
        fresh_audit.audit_id,
        fresh_group["group_id"],
        fresh_candidate["candidate_id"],
        queue_id,
        selected_arm_id,
        selected_reference_sha256,
        decisions["decision_set_id"],
        True,
    )


def _load_comparison_document(root):
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
    arm_reports = []
    arm_ids = set()
    for arm in arms:
        if not isinstance(arm, dict):
            raise ReferenceRenderComparisonError(
                "Reference render comparison arm is invalid"
            )
        arm_id = _safe_id(arm.get("arm_id"), "arm ID")
        if arm_id in arm_ids:
            raise ReferenceRenderComparisonError(
                "Reference render comparison arm IDs repeat"
            )
        arm_ids.add(arm_id)
        report_relative = _required_text(arm.get("report"), "report path")
        report = _contained_file(root, report_relative)
        if sha256_file(report) != _required_sha256(
            arm.get("report_sha256"), "report hash"
        ):
            raise ReferenceRenderComparisonError(
                "Reference render comparison report changed"
            )
        arm_reports.append(report_relative)
        renders = arm.get("renders")
        if not isinstance(renders, list):
            raise ReferenceRenderComparisonError(
                "Reference render comparison renders are invalid"
            )
        for render in renders:
            if not isinstance(render, dict) or render.get("outcome") not in {
                "complete",
                "error",
            }:
                raise ReferenceRenderComparisonError(
                    "Reference render comparison outcome is invalid"
                )
            if render.get("outcome") == "complete":
                audio = _contained_file(root / "arms" / arm_id, render.get("audio"))
                if sha256_file(audio) != _required_sha256(
                    render.get("audio_sha256"), "rendered audio hash"
                ):
                    raise ReferenceRenderComparisonError(
                        "Reference render comparison audio changed"
                    )
    if arm_reports != reports or len(set(reports)) != len(reports):
        raise ReferenceRenderComparisonError(
            "Reference render comparison report inventory changed"
        )
    return document


def _selected_listening_trial(
    comparison_root,
    comparison,
    session_path,
    session,
    queue_id,
):
    if session.get("completed_count") != session.get("trial_count"):
        raise ReferenceRenderComparisonError(
            "Reference render listening session is incomplete"
        )
    matching_trials = [
        trial
        for trial in session["trials"]
        if trial.get("queue_id")
        == f"corpus:{queue_id}:{trial.get('text_sha256', '')[:16]}"
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
    key_path = session_path.with_name(".blind-key.json")
    report_path = session_path.with_name("report.json")
    try:
        key = json.loads(key_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    assignments = [
        value
        for value in key.get("assignments", [])
        if isinstance(value, dict) and value.get("trial_id") == trial["trial_id"]
    ]
    if len(assignments) != 1:
        raise ReferenceRenderComparisonError(
            "Reference render listening assignment is absent or ambiguous"
        )
    arms_by_id = {value["arm_id"]: value for value in comparison["arms"]}
    model_records = [
        value for value in key.get("models", []) if isinstance(value, dict)
    ]
    selected_arm_ids = [value.get("model_id") for value in model_records]
    if (
        len(selected_arm_ids) != 2
        or len(set(selected_arm_ids)) != 2
        or any(value not in arms_by_id for value in selected_arm_ids)
    ):
        raise ReferenceRenderComparisonError(
            "Reference render listening must bind exactly two known arms"
        )
    expected_reports = {
        str(
            _contained_file(comparison_root, arms_by_id[arm_id]["report"])
        ): _required_sha256(arms_by_id[arm_id].get("report_sha256"), "report hash")
        for arm_id in selected_arm_ids
    }
    actual_reports = {}
    for source in key.get("sources", []):
        if not isinstance(source, dict):
            raise ReferenceRenderComparisonError(
                "Reference render listening source inventory is malformed"
            )
        path = Path(_required_text(source.get("path"), "listening source")).expanduser()
        if path.is_symlink():
            raise ReferenceRenderComparisonError(
                "Reference render listening source is a symlink"
            )
        actual_reports[str(path.resolve())] = _required_sha256(
            source.get("sha256"), "listening source hash"
        )
    if (
        len(actual_reports) != len(key.get("sources", []))
        or actual_reports != expected_reports
    ):
        raise ReferenceRenderComparisonError(
            "Reference render listening sources changed"
        )
    actual_models = set(selected_arm_ids)
    if any(
        not any(
            render.get("id") == queue_id and render.get("outcome") == "complete"
            for render in arms_by_id[arm_id]["renders"]
        )
        for arm_id in actual_models
    ):
        raise ReferenceRenderComparisonError("Reference render listening arms changed")
    try:
        expected_report = aggregate_listening_report(session_path)
    except ModelListeningError as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    comparable_fields = set(expected_report) - {"generated_at"}
    if any(report.get(field) != expected_report[field] for field in comparable_fields):
        raise ReferenceRenderComparisonError(
            "Reference render listening report is stale or changed"
        )
    selected_side = rating["preference"]
    assignment = assignments[0]
    selected = assignment.get(selected_side)
    if not isinstance(selected, dict):
        raise ReferenceRenderComparisonError(
            "Reference render listening selection is malformed"
        )
    selected_arm_id = _safe_id(selected.get("model_id"), "selected arm ID")
    return trial, assignment, selected_side, selected_arm_id


def _load_audit_documents(directory):
    try:
        document = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
        key = json.loads((directory / ".blind-key.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReferenceRenderComparisonError(str(error)) from error
    return document, key


def _one_group_for_queue(document, queue_id, label):
    groups = [
        group
        for group in document["groups"]
        if queue_id in {case["queue_id"] for case in group["cases"]}
    ]
    if len(groups) != 1 or groups[0].get("case_count") != 1:
        raise ReferenceRenderComparisonError(
            f"Reference render {label} must contain exactly one selected case"
        )
    return groups[0]


def _reference_selection_snapshots(
    comparison_root,
    session_path,
    report_path,
    source_audit_path,
):
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


def _assert_reference_selection_snapshots(snapshots):
    for path, digest in snapshots.items():
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ReferenceRenderComparisonError(
                "Reference selection authority changed before decision save"
            )


def _assert_plan_and_audit_unchanged(plan):
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


def _read_audit_document(directory):
    try:
        return json.loads((Path(directory) / "audit.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReferenceRenderComparisonError(str(error)) from error


def _planned_directory(root, value):
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


def _contained_file(root, value):
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


def _safe_id(value, label):
    text = _required_text(value, label)
    if text in {".", ".."} or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in text
    ):
        raise ReferenceRenderComparisonError(
            f"Reference render {label} must be a safe lowercase identifier"
        )
    return text


def _source_reference_family(group):
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


def _required_text(value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ReferenceRenderComparisonError(
            f"Reference render {label} must be non-empty text"
        )
    return value


def _required_sha256(value, label):
    text = _required_text(value, label)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
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

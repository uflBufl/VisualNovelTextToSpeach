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
    prepare_failure_reference_audio,
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
    create_listening_session_from_reports,
)

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
        comparison_id = _canonical_sha256(body)
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
    comparison_directory, output_directory, *, seed=0
):
    """Create a blind session for the complete matched subset of a comparison."""
    supplied = Path(comparison_directory).expanduser()
    if supplied.is_symlink():
        raise ReferenceRenderComparisonError("Reference render comparison is a symlink")
    root = supplied.resolve()
    document = _load_comparison_document(root)
    sample_ids = document["complete_pair_queue_ids"]
    if not sample_ids:
        raise ReferenceRenderComparisonError(
            "Reference render comparison has no complete matched samples"
        )
    reports = [_contained_file(root, value) for value in document["reports"]]
    try:
        return create_listening_session_from_reports(
            reports, output_directory, seed=seed, sample_ids=sample_ids
        )
    except ModelListeningError as error:
        raise ReferenceRenderComparisonError(str(error)) from error


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
        != _canonical_sha256(
            {key: value for key, value in document.items() if key != "comparison_id"}
        )
    ):
        raise ReferenceRenderComparisonError(
            "Reference render comparison identity is invalid"
        )
    controls = document.get("controls")
    arms = document.get("arms")
    reports = document.get("reports")
    complete_pair_queue_ids = document.get("complete_pair_queue_ids")
    if (
        not isinstance(controls, list)
        or not isinstance(arms, list)
        or len(arms) < 2
        or not isinstance(reports, list)
        or len(reports) != len(arms)
        or not isinstance(complete_pair_queue_ids, list)
        or len(complete_pair_queue_ids) != len(set(complete_pair_queue_ids))
        or any(
            not isinstance(value, str) or not value for value in complete_pair_queue_ids
        )
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
    for arm in arms:
        if not isinstance(arm, dict):
            raise ReferenceRenderComparisonError(
                "Reference render comparison arm is invalid"
            )
        arm_id = _safe_id(arm.get("arm_id"), "arm ID")
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


def _canonical_sha256(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "REFERENCE_RENDER_INPUT_SCHEMA",
    "REFERENCE_RENDER_INPUT_VERSION",
    "ReferenceRenderComparison",
    "ReferenceRenderComparisonError",
    "ReferenceRenderPlan",
    "create_reference_render_listening",
    "load_reference_render_plan",
    "publish_reference_render_comparison",
]

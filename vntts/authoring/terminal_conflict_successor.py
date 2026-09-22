"""Resolution-aware successor projection for terminal authority conflicts."""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeAlias, TypedDict, TypeGuard

from vntts_artifacts.atomic_io import atomic_write_json

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    AuthoritySnapshot,
    assert_authority_snapshot,
    canonical_document_sha256,
    capture_authority_file,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.reconciliation_schema import (
    RECONCILIATION_ACTIONS,
    TERMINAL_AUTHORITIES,
    WORKSPACE_NAME_PATTERN,
    AuthoringReconciliationSchemaError,
    validate_authoring_reconciliation_document,
)
from vntts.authoring.terminal_conflict_records import (
    require_terminal_conflict_directory,
    require_terminal_conflict_sha256,
    require_terminal_conflict_text,
    require_terminal_conflict_timestamp,
)
from vntts.authoring.terminal_conflict_resolution import (
    TerminalConflictResolutionDocument,
    TerminalConflictResolutionError,
    TerminalConflictResolutionRecord,
    assert_terminal_conflict_resolution_source_authorities,
    validate_terminal_conflict_resolution_document,
)

TERMINAL_CONFLICT_SUCCESSOR_SCHEMA = (
    "vntts.authoring-terminal-conflict-successor-reconciliation"
)
TERMINAL_CONFLICT_SUCCESSOR_VERSION = 1

JsonDocument: TypeAlias = dict[str, object]


class _HistoricalOccurrence(TypedDict):
    workspace_id: str
    authority: str
    line_id: str
    text_sha256: str
    queue_record_sha256: str


class _ResolutionProjection(TypedDict):
    case_id: str
    queue_id: str
    line_id: str
    queue_record_sha256: str
    text_sha256: str
    candidate_ids: list[str]
    reviewed_at: str
    decision: str
    selected_candidate_id: str | None
    selected_authority: str | None
    selected_audio: str | None
    selected_audio_sha256: str | None
    sample_rate: int | None
    sample_count: int | None


class _SuccessorRecord(TypedDict):
    queue_id: str
    next_action: str
    historical_conflict: object
    resolution: object


class _SuccessorSummary(TypedDict):
    historical_conflict_count: int
    resolved_conflict_count: int
    unresolved_conflict_count: int
    action_counts: dict[str, int]


class TerminalConflictSuccessorDocument(TypedDict):
    schema: str
    schema_version: int
    successor_id: str
    source_reconciliation: str
    source_reconciliation_sha256: str
    source_report_id: str
    terminal_resolution: str
    terminal_resolution_sha256: str
    terminal_resolution_id: str
    policy: dict[str, str]
    summary: _SuccessorSummary
    resolved_terminal_conflicts: list[_SuccessorRecord]
    unresolved_terminal_conflicts: list[object]


@dataclass(frozen=True)
class _SuccessorInputs:
    report_snapshot: AuthoritySnapshot
    report: JsonDocument
    resolution_snapshot: AuthoritySnapshot
    resolution: TerminalConflictResolutionDocument


def _is_successor_document(
    value: object,
) -> TypeGuard[TerminalConflictSuccessorDocument]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_successor_record(value: object) -> TypeGuard[_SuccessorRecord]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_resolution_projection(value: object) -> TypeGuard[_ResolutionProjection]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


APPLY_APPROVED_OUTCOME = "apply_selected_approved_outcome"
RETAIN_EXPLICIT_REJECTION = "retain_explicit_rejection"
NEW_REPAIR_HYPOTHESIS = "new_repair_hypothesis_required"
SUCCESSOR_ACTIONS = {
    APPLY_APPROVED_OUTCOME,
    RETAIN_EXPLICIT_REJECTION,
    NEW_REPAIR_HYPOTHESIS,
}


class TerminalConflictSuccessorError(RuntimeError):
    """A resolution cannot safely project a successor reconciliation."""


def _text(value: object, label: str) -> str:
    return str(
        require_terminal_conflict_text(
            value, label, error_type=TerminalConflictSuccessorError
        )
    )


def _directory(value: str | Path, label: str) -> Path:
    return Path(
        require_terminal_conflict_directory(
            value, label, error_type=TerminalConflictSuccessorError
        )
    )


def _sha256(value: object, label: str) -> str:
    return str(
        require_terminal_conflict_sha256(
            value, label, error_type=TerminalConflictSuccessorError
        )
    )


def _aware_timestamp(value: object, label: str) -> object:
    return require_terminal_conflict_timestamp(
        value, label, error_type=TerminalConflictSuccessorError
    )


def _objects(value: object, label: str) -> list[JsonDocument]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TerminalConflictSuccessorError(f"{label.capitalize()} are malformed")
    return [item for item in value if isinstance(item, dict)]


@dataclass(frozen=True)
class TerminalConflictSuccessor:
    directory: Path
    successor_id: str
    resolved_count: int
    action_counts: dict[str, int]
    created: bool = False

    @property
    def successor(self) -> Path:
        return self.directory / "successor.json"

    def to_dict(self) -> JsonDocument:
        return {
            "directory": str(self.directory),
            "successor": str(self.successor),
            "successor_id": self.successor_id,
            "resolved_count": self.resolved_count,
            "action_counts": dict(self.action_counts),
            "created": self.created,
        }


def publish_terminal_conflict_successor(
    reconciliation_path: str | Path,
    resolution_directory: str | Path,
    output_directory: str | Path,
) -> TerminalConflictSuccessor:
    """Publish a read-only successor that retains every historical occurrence."""
    reconciliation_path = Path(reconciliation_path).expanduser().resolve()
    resolution_root = _directory(resolution_directory, "terminal conflict resolution")
    output = Path(output_directory).expanduser().resolve()
    inputs = _load_successor_inputs(reconciliation_path, resolution_root)
    if inputs.resolution["source_report_id"] != inputs.report["report_id"]:
        raise TerminalConflictSuccessorError(
            "Terminal conflict resolution belongs to another reconciliation"
        )

    conflicts = {
        _text(item.get("queue_id"), "Conflict queue ID"): item
        for item in _objects(
            inputs.report.get("terminal_conflicts"), "terminal conflicts"
        )
    }
    resolutions = {item["queue_id"]: item for item in inputs.resolution["resolutions"]}
    if not conflicts or set(conflicts) != set(resolutions):
        raise TerminalConflictSuccessorError(
            "Terminal conflict resolution does not cover the exact source conflicts"
        )
    resolved, actions = _build_successor_records(conflicts, resolutions)
    body = _successor_body(inputs, conflicts, resolved, actions)
    successor_id = canonical_document_sha256(body)
    document = {**body, "successor_id": successor_id}

    output.parent.mkdir(parents=True, exist_ok=True)
    output_exists = output.exists() or output.is_symlink()
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        atomic_write_json(staging / "successor.json", document, sort_keys=True)
        load_terminal_conflict_successor(staging)
        _assert_successor_publication_sources(inputs, resolution_root)
        if output_exists:
            existing = load_terminal_conflict_successor(output)
            if existing.successor_id != successor_id:
                raise TerminalConflictSuccessorError(
                    f"Terminal conflict successor output has another identity: {output}"
                )
            return existing
        try:
            rename_directory_no_replace(staging, output)
        except (AtomicPublicationError, OSError) as error:
            raise TerminalConflictSuccessorError(
                f"Unable to publish terminal conflict successor: {error}"
            ) from error
    return TerminalConflictSuccessor(
        output,
        successor_id,
        len(resolved),
        dict(sorted(actions.items())),
        True,
    )


def _load_successor_inputs(
    reconciliation_path: Path, resolution_root: Path
) -> _SuccessorInputs:
    try:
        report_snapshot = capture_authority_file(
            reconciliation_path, "source authoring reconciliation"
        )
        report = validate_authoring_reconciliation_document(
            report_snapshot.json_document("source authoring reconciliation")
        )
        resolution_snapshot = capture_authority_file(
            resolution_root / "resolution.json", "terminal conflict resolution"
        )
        resolution = validate_terminal_conflict_resolution_document(
            resolution_snapshot.json_document("terminal conflict resolution"),
            resolution_root,
        )
        checked_resolution = assert_terminal_conflict_resolution_source_authorities(
            resolution_root
        )
        if checked_resolution != resolution:
            raise TerminalConflictSuccessorError(
                "Terminal conflict resolution changed while it was inspected"
            )
    except (
        AuthoringAuthorityError,
        AuthoringReconciliationSchemaError,
        TerminalConflictResolutionError,
    ) as error:
        raise TerminalConflictSuccessorError(str(error)) from error
    return _SuccessorInputs(report_snapshot, report, resolution_snapshot, resolution)


def _build_successor_records(
    conflicts: dict[str, JsonDocument],
    resolutions: dict[str, TerminalConflictResolutionRecord],
) -> tuple[list[_SuccessorRecord], Counter[str]]:
    resolved: list[_SuccessorRecord] = []
    actions: Counter[str] = Counter()
    for queue_id in sorted(conflicts):
        conflict = conflicts[queue_id]
        decision = resolutions[queue_id]
        occurrences = _objects(
            conflict.get("occurrences"), "terminal conflict occurrences"
        )
        queue_records = {item["queue_record_sha256"] for item in occurrences}
        text_hashes = {item["text_sha256"] for item in occurrences}
        line_ids = {item["line_id"] for item in occurrences}
        if (
            queue_records != {decision["queue_record_sha256"]}
            or text_hashes != {decision["text_sha256"]}
            or line_ids != {decision["line_id"]}
        ):
            raise TerminalConflictSuccessorError(
                f"Terminal conflict identity changed: {queue_id}"
            )
        if decision["decision"] == "neither_acceptable":
            action = NEW_REPAIR_HYPOTHESIS
        elif decision["selected_authority"] == "approved":
            action = APPLY_APPROVED_OUTCOME
        else:
            action = RETAIN_EXPLICIT_REJECTION
        actions[action] += 1
        resolved.append(
            {
                "queue_id": queue_id,
                "next_action": action,
                "historical_conflict": copy.deepcopy(conflict),
                "resolution": copy.deepcopy(decision),
            }
        )
    return resolved, actions


def _successor_body(
    inputs: _SuccessorInputs,
    conflicts: dict[str, JsonDocument],
    resolved: list[_SuccessorRecord],
    actions: Counter[str],
) -> JsonDocument:
    return {
        "schema": TERMINAL_CONFLICT_SUCCESSOR_SCHEMA,
        "schema_version": TERMINAL_CONFLICT_SUCCESSOR_VERSION,
        "source_reconciliation": str(inputs.report_snapshot.path),
        "source_reconciliation_sha256": inputs.report_snapshot.sha256,
        "source_report_id": inputs.report["report_id"],
        "terminal_resolution": str(inputs.resolution_snapshot.path),
        "terminal_resolution_sha256": inputs.resolution_snapshot.sha256,
        "terminal_resolution_id": inputs.resolution["resolution_id"],
        "policy": {
            "historical_occurrences": "retained",
            "resolution_match": "exact queue, line, text and queue-record identity",
            "workspace_mutation": "forbidden",
        },
        "summary": {
            "historical_conflict_count": len(conflicts),
            "resolved_conflict_count": len(resolved),
            "unresolved_conflict_count": 0,
            "action_counts": dict(sorted(actions.items())),
        },
        "resolved_terminal_conflicts": resolved,
        "unresolved_terminal_conflicts": [],
    }


def _assert_successor_publication_sources(
    inputs: _SuccessorInputs, resolution_root: Path
) -> None:
    try:
        assert_authority_snapshot(
            inputs.report_snapshot, "source authoring reconciliation"
        )
        assert_authority_snapshot(
            inputs.resolution_snapshot, "terminal conflict resolution"
        )
        if (
            assert_terminal_conflict_resolution_source_authorities(resolution_root)
            != inputs.resolution
        ):
            raise TerminalConflictSuccessorError(
                "Terminal conflict resolution changed before publication"
            )
    except (AuthoringAuthorityError, TerminalConflictResolutionError) as error:
        raise TerminalConflictSuccessorError(str(error)) from error


def load_terminal_conflict_successor(
    directory: str | Path,
) -> TerminalConflictSuccessor:
    root = _directory(directory, "terminal conflict successor")
    document = load_terminal_conflict_successor_document(root)
    return TerminalConflictSuccessor(
        root,
        document["successor_id"],
        document["summary"]["resolved_conflict_count"],
        document["summary"]["action_counts"],
        False,
    )


def load_terminal_conflict_successor_document(
    directory: str | Path,
) -> TerminalConflictSuccessorDocument:
    root = _directory(directory, "terminal conflict successor")
    try:
        snapshot = capture_authority_file(
            root / "successor.json", "terminal conflict successor"
        )
        document = validate_terminal_conflict_successor_document(
            snapshot.json_document("terminal conflict successor"), root
        )
        assert_authority_snapshot(snapshot, "terminal conflict successor")
    except AuthoringAuthorityError as error:
        raise TerminalConflictSuccessorError(str(error)) from error
    return document


def validate_terminal_conflict_successor_document(
    document: object, directory: str | Path
) -> TerminalConflictSuccessorDocument:
    value = copy.deepcopy(document)
    value, raw_records, root = _validate_successor_document_header(value, directory)
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    records: list[_SuccessorRecord] = []
    for raw_record in raw_records:
        record, action = _validate_successor_record(raw_record, seen)
        records.append(record)
        counts[action] += 1
    _validate_successor_document_completion(value, records, counts, root)
    return value


def _validate_successor_document_header(
    value: object, directory: str | Path
) -> tuple[TerminalConflictSuccessorDocument, list[_SuccessorRecord], Path]:
    fields = {
        "schema",
        "schema_version",
        "successor_id",
        "source_reconciliation",
        "source_reconciliation_sha256",
        "source_report_id",
        "terminal_resolution",
        "terminal_resolution_sha256",
        "terminal_resolution_id",
        "policy",
        "summary",
        "resolved_terminal_conflicts",
        "unresolved_terminal_conflicts",
    }
    if (
        not _is_successor_document(value)
        or set(value) != fields
        or value.get("schema") != TERMINAL_CONFLICT_SUCCESSOR_SCHEMA
        or value.get("schema_version") != TERMINAL_CONFLICT_SUCCESSOR_VERSION
    ):
        raise TerminalConflictSuccessorError("Unsupported terminal conflict successor")
    _validate_successor_document_identity(value)
    raw_records = value["resolved_terminal_conflicts"]
    if not isinstance(raw_records, list) or not raw_records:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor resolutions are empty"
        )
    return value, raw_records, Path(directory).resolve()


def _validate_successor_document_identity(
    value: TerminalConflictSuccessorDocument,
) -> None:
    successor_id = _sha256(value["successor_id"], "Successor ID")
    if (
        canonical_document_sha256(
            {key: item for key, item in value.items() if key != "successor_id"}
        )
        != successor_id
    ):
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor identity changed"
        )
    for field in ("source_reconciliation", "terminal_resolution"):
        path = Path(_text(value[field], field.replace("_", " ").title()))
        if not path.is_absolute():
            raise TerminalConflictSuccessorError(
                "Terminal conflict successor source paths must be absolute"
            )
    for digest, label in (
        (value["source_reconciliation_sha256"], "Source Reconciliation Sha256"),
        (value["source_report_id"], "Source Report Id"),
        (value["terminal_resolution_sha256"], "Terminal Resolution Sha256"),
        (value["terminal_resolution_id"], "Terminal Resolution Id"),
    ):
        _sha256(digest, label)
    if value["policy"] != {
        "historical_occurrences": "retained",
        "resolution_match": "exact queue, line, text and queue-record identity",
        "workspace_mutation": "forbidden",
    }:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor policy changed"
        )
    if value["unresolved_terminal_conflicts"] != []:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor has unresolved conflicts"
        )


def _validate_successor_record(
    value: object, seen: set[str]
) -> tuple[_SuccessorRecord, str]:
    if not _is_successor_record(value) or set(value) != {
        "queue_id",
        "next_action",
        "historical_conflict",
        "resolution",
    }:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor record is malformed"
        )
    record = value
    queue_id = _text(record["queue_id"], "Successor queue ID")
    if queue_id in seen:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor record is duplicated"
        )
    seen.add(queue_id)
    action = record["next_action"]
    if action not in SUCCESSOR_ACTIONS:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor action is invalid"
        )
    occurrences = _validate_historical_conflict(record["historical_conflict"], queue_id)
    resolution, expected_action = _validate_resolution_projection(
        record["resolution"], queue_id
    )
    if (
        {item["queue_record_sha256"] for item in occurrences}
        != {resolution["queue_record_sha256"]}
        or {item["text_sha256"] for item in occurrences} != {resolution["text_sha256"]}
        or {item["line_id"] for item in occurrences} != {resolution["line_id"]}
    ):
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor authority identity changed"
        )
    if action != expected_action:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor action changed"
        )
    return record, action


def _validate_successor_document_completion(
    value: TerminalConflictSuccessorDocument,
    records: list[_SuccessorRecord],
    counts: Counter[str],
    root: Path,
) -> None:
    if records != sorted(records, key=lambda item: item["queue_id"]):
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor resolutions are not sorted"
        )
    summary = value["summary"]
    expected_summary = {
        "historical_conflict_count": len(records),
        "resolved_conflict_count": len(records),
        "unresolved_conflict_count": 0,
        "action_counts": dict(sorted(counts.items())),
    }
    if summary != expected_summary:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor summary changed"
        )
    inventory = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if inventory != {"successor.json"}:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor inventory changed"
        )


def _validate_historical_conflict(
    value: object, queue_id: str
) -> list[_HistoricalOccurrence]:
    if not isinstance(value, dict) or set(value) != {
        "queue_id",
        "reason",
        "occurrences",
    }:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor authority ledger is malformed"
        )
    if value["queue_id"] != queue_id:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor authority queue changed"
        )
    _text(value["reason"], "Historical conflict reason")
    occurrences = value["occurrences"]
    if not isinstance(occurrences, list) or len(occurrences) < 2:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor authority ledger is malformed"
        )
    seen = set()
    for occurrence in occurrences:
        if not isinstance(occurrence, dict) or set(occurrence) != {
            "workspace_id",
            "authority",
            "line_id",
            "text_sha256",
            "queue_record_sha256",
        }:
            raise TerminalConflictSuccessorError(
                "Terminal conflict successor occurrence is malformed"
            )
        workspace_id = _text(occurrence["workspace_id"], "Historical workspace ID")
        if not WORKSPACE_NAME_PATTERN.fullmatch(workspace_id):
            raise TerminalConflictSuccessorError(
                "Terminal conflict successor workspace identity is invalid"
            )
        authority = occurrence["authority"]
        if authority not in RECONCILIATION_ACTIONS | TERMINAL_AUTHORITIES:
            raise TerminalConflictSuccessorError(
                "Terminal conflict successor historical authority is invalid"
            )
        identity = (workspace_id, authority)
        if identity in seen:
            raise TerminalConflictSuccessorError(
                "Terminal conflict successor occurrence is duplicated"
            )
        seen.add(identity)
        _text(occurrence["line_id"], "Historical line ID")
        _sha256(occurrence["text_sha256"], "Historical text hash")
        _sha256(occurrence["queue_record_sha256"], "Historical queue-record hash")
    return occurrences


def _validate_resolution_projection(
    value: object, queue_id: str
) -> tuple[_ResolutionProjection, str]:
    projection, candidate_ids = _validate_resolution_projection_identity(
        value, queue_id
    )
    return projection, _validate_resolution_projection_selection(
        projection, candidate_ids, queue_id
    )


def _validate_resolution_projection_identity(
    value: object, queue_id: str
) -> tuple[_ResolutionProjection, list[str]]:
    fields = {
        "case_id",
        "queue_id",
        "line_id",
        "queue_record_sha256",
        "text_sha256",
        "candidate_ids",
        "reviewed_at",
        "decision",
        "selected_candidate_id",
        "selected_authority",
        "selected_audio",
        "selected_audio_sha256",
        "sample_rate",
        "sample_count",
    }
    if not _is_resolution_projection(value) or set(value) != fields:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor resolution is malformed"
        )
    if value["queue_id"] != queue_id:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor resolution queue changed"
        )
    case_id = _sha256(value["case_id"], "Resolution case ID")
    _text(value["line_id"], "Resolution line ID")
    _sha256(value["queue_record_sha256"], "Resolution queue-record hash")
    _sha256(value["text_sha256"], "Resolution text hash")
    candidate_ids = value["candidate_ids"]
    if (
        not isinstance(candidate_ids, list)
        or len(candidate_ids) < 2
        or len(candidate_ids) != len(set(candidate_ids))
    ):
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor candidate identities are invalid"
        )
    for candidate_id in candidate_ids:
        _sha256(candidate_id, "Resolution candidate ID")
    expected_case_id = canonical_document_sha256(
        {
            "queue_id": queue_id,
            "queue_record_sha256": value["queue_record_sha256"],
            "text_sha256": value["text_sha256"],
            "candidate_ids": candidate_ids,
        }
    )
    if case_id != expected_case_id:
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor case identity changed"
        )
    _aware_timestamp(value["reviewed_at"], "Resolution review timestamp")
    return value, candidate_ids


def _validate_resolution_projection_selection(
    value: _ResolutionProjection, candidate_ids: list[str], queue_id: str
) -> str:
    if value["decision"] == "neither_acceptable":
        if any(
            value[field] is not None
            for field in (
                "selected_candidate_id",
                "selected_authority",
                "selected_audio",
                "selected_audio_sha256",
                "sample_rate",
                "sample_count",
            )
        ):
            raise TerminalConflictSuccessorError(
                "Neither successor resolution must not select audio"
            )
        return NEW_REPAIR_HYPOTHESIS
    if value["decision"] != "selected_candidate":
        raise TerminalConflictSuccessorError(
            "Terminal conflict successor resolution decision is invalid"
        )
    selected_id = _sha256(value["selected_candidate_id"], "Selected candidate ID")
    if selected_id not in candidate_ids:
        raise TerminalConflictSuccessorError(
            "Selected successor candidate is unavailable"
        )
    authority = value["selected_authority"]
    if authority not in {"approved", "rejected"}:
        raise TerminalConflictSuccessorError("Selected successor authority is invalid")
    digest = _sha256(value["selected_audio_sha256"], "Selected audio hash")
    selected_audio = PurePosixPath(
        _text(value["selected_audio"], "Selected audio path")
    )
    if selected_audio.is_absolute() or any(
        part in {"", ".", ".."} for part in selected_audio.parts
    ):
        raise TerminalConflictSuccessorError("Selected successor audio path is invalid")
    for field, amount in (
        ("sample_rate", value["sample_rate"]),
        ("sample_count", value["sample_count"]),
    ):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise TerminalConflictSuccessorError(
                f"Selected successor {field.replace('_', ' ')} is invalid"
            )
    if selected_id != canonical_document_sha256(
        {"queue_id": queue_id, "authority": authority, "audio_sha256": digest}
    ):
        raise TerminalConflictSuccessorError(
            "Selected successor candidate identity changed"
        )
    action = (
        APPLY_APPROVED_OUTCOME if authority == "approved" else RETAIN_EXPLICIT_REJECTION
    )
    return action


__all__ = [
    "APPLY_APPROVED_OUTCOME",
    "NEW_REPAIR_HYPOTHESIS",
    "RETAIN_EXPLICIT_REJECTION",
    "SUCCESSOR_ACTIONS",
    "TERMINAL_CONFLICT_SUCCESSOR_SCHEMA",
    "TERMINAL_CONFLICT_SUCCESSOR_VERSION",
    "TerminalConflictSuccessor",
    "TerminalConflictSuccessorError",
    "load_terminal_conflict_successor",
    "load_terminal_conflict_successor_document",
    "publish_terminal_conflict_successor",
    "validate_terminal_conflict_successor_document",
]

"""Publish and record bounded human review of terminal authority conflicts."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, NotRequired, TypeAlias, TypedDict, TypeGuard

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav

from vntts.authoring.advisory_lock import (
    AdvisoryLockBusyError,
    exclusive_advisory_lock,
)
from vntts.authoring.authority import (
    AuthoringAuthorityError,
    AuthoritySnapshot,
    assert_authority_snapshot,
    canonical_document_sha256,
    capture_authority_file,
    write_json_document_no_replace,
)
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    ReviewAuthority,
    load_review_audio_bytes,
    process_is_alive,
    process_started_at,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.reconciliation import (
    AuthoringReconciliationError,
    load_authoring_reconciliation,
)
from vntts.authoring.terminal_conflict_records import (
    require_terminal_conflict_file,
    require_terminal_conflict_sha256,
    require_terminal_conflict_text,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    list_review_items,
    prepare_review_audio,
)
from vntts.authoring.workbench_contracts import ReviewItem

TERMINAL_CONFLICT_REVIEW_SCHEMA = "vntts.authoring-terminal-conflict-review"
TERMINAL_CONFLICT_REVIEW_VERSION = 1
TERMINAL_CONFLICT_PROGRESS_SCHEMA = "vntts.authoring-terminal-conflict-review-progress"
TERMINAL_CONFLICT_PROGRESS_VERSION = 1
TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION = 2
SUPPORTED_TERMINAL_CONFLICT_PROGRESS_VERSIONS = frozenset(
    {TERMINAL_CONFLICT_PROGRESS_VERSION, TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION}
)
NEITHER_ACCEPTABLE = "neither_acceptable"
PROGRESS_LEASE_SCHEMA = "vntts.authoring-terminal-conflict-progress-lease"
PROGRESS_LEASE_VERSION = 1

JsonDocument: TypeAlias = dict[str, object]


class TerminalConflictReviewAuthority(TypedDict):
    queue_sha256: str
    state_sha256: str
    item_sha256: str
    audio_sha256: str


class TerminalConflictReviewSourceAuthority(TypedDict):
    workspace_id: str
    state: str
    queue: str
    review_authority: TerminalConflictReviewAuthority


class TerminalConflictReviewCandidate(TypedDict):
    candidate_id: str
    authority: str
    audio: str
    audio_sha256: str
    sample_rate: int
    sample_count: int
    source_authorities: list[TerminalConflictReviewSourceAuthority]
    workspace_ids: list[str]


class _TerminalConflictCandidateDraft(TypedDict):
    authority: str
    audio_sha256: str
    workspace_ids: list[str]
    source_authorities: list[TerminalConflictReviewSourceAuthority]
    audio_bytes: NotRequired[bytes]


class TerminalConflictReviewCase(TypedDict):
    case_id: str
    queue_id: str
    line_id: str
    queue_record_sha256: str
    text_sha256: str
    speaker: str
    voice_character: str
    text: str
    candidates: list[TerminalConflictReviewCandidate]


class TerminalConflictReviewDocument(TypedDict):
    schema: str
    schema_version: int
    review_id: str
    source_reconciliation: str
    source_reconciliation_sha256: str
    source_report_id: str
    policy: dict[str, str]
    case_count: int
    candidate_count: int
    cases: list[TerminalConflictReviewCase]


class TerminalConflictReviewDecision(TypedDict):
    case_id: str
    decision: str
    reviewed_at: str


class TerminalConflictReviewProgress(TypedDict):
    schema: str
    schema_version: int
    review_id: str
    updated_at: str
    decisions: list[TerminalConflictReviewDecision]
    carry_forward: NotRequired[dict[str, object]]


class _ProgressLease(TypedDict):
    schema: str
    schema_version: int
    pid: int
    hostname: str
    process_started_at: str | None
    lease_id: str
    started_at: str


class _StoredProgressLease(TypedDict):
    schema: str
    schema_version: int
    pid: int
    hostname: str
    lease_id: str
    process_started_at: NotRequired[object]


@dataclass(frozen=True)
class _ReviewPublicationInput:
    snapshot: AuthoritySnapshot
    report: JsonDocument
    conflicts: list[JsonDocument]
    workspace_records: dict[str, JsonDocument]
    workspace_paths: dict[str, Path]


def _is_review_document(value: object) -> TypeGuard[TerminalConflictReviewDocument]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_progress_document(value: object) -> TypeGuard[TerminalConflictReviewProgress]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_progress_decision(value: object) -> TypeGuard[TerminalConflictReviewDecision]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_review_case(value: object) -> TypeGuard[TerminalConflictReviewCase]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_review_candidate(value: object) -> TypeGuard[TerminalConflictReviewCandidate]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_source_authority(
    value: object,
) -> TypeGuard[TerminalConflictReviewSourceAuthority]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


class TerminalConflictReviewError(RuntimeError):
    """Terminal authority conflict evidence is invalid or changed."""


def _contained_file(root: str | Path, value: object, label: str) -> Path:
    return Path(
        require_terminal_conflict_file(
            root, value, label, error_type=TerminalConflictReviewError
        )
    )


def _text(value: object, label: str) -> str:
    return str(
        require_terminal_conflict_text(
            value, label, error_type=TerminalConflictReviewError
        )
    )


def _sha256(value: object, label: str) -> str:
    return str(
        require_terminal_conflict_sha256(
            value, label, error_type=TerminalConflictReviewError
        )
    )


def _text_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TerminalConflictReviewError(f"{label} is invalid")
    return list(value)


def _aware_timestamp(value: object, label: str) -> datetime:
    parsed = datetime.fromisoformat(_text(value, label))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TerminalConflictReviewError(f"{label} requires a timezone")
    return parsed


@dataclass(frozen=True)
class TerminalConflictReview:
    directory: Path
    review_id: str
    case_count: int
    candidate_count: int
    completed_count: int = 0
    created: bool = False

    @property
    def review(self) -> Path:
        return self.directory / "review.json"

    @property
    def progress(self) -> Path:
        return self.directory / "progress.json"

    def to_dict(self) -> JsonDocument:
        return {
            "directory": str(self.directory),
            "review": str(self.review),
            "progress": str(self.progress),
            "review_id": self.review_id,
            "case_count": self.case_count,
            "candidate_count": self.candidate_count,
            "completed_count": self.completed_count,
            "created": self.created,
        }


def _review_publication_input(reconciliation_path: Path) -> _ReviewPublicationInput:
    try:
        report_snapshot = capture_authority_file(
            reconciliation_path, "authoring reconciliation"
        )
        report = load_authoring_reconciliation(report_snapshot.path).document
    except (
        AuthoringAuthorityError,
        AuthoringReconciliationError,
        OSError,
        ValueError,
    ) as error:
        raise TerminalConflictReviewError(str(error)) from error
    if report_snapshot.path.read_bytes() != report_snapshot.payload:
        raise TerminalConflictReviewError(
            "Authoring reconciliation changed while it was loaded"
        )
    conflicts = _objects(report.get("terminal_conflicts"), "terminal conflicts")
    if not conflicts:
        raise TerminalConflictReviewError(
            "Authoring reconciliation has no terminal conflicts"
        )
    workspaces = _objects(report.get("workspaces"), "reconciliation workspaces")
    workspace_records = {
        _text(value.get("workspace_id"), "Workspace ID"): value for value in workspaces
    }
    workspace_paths = {
        workspace_id: Path(_text(value.get("workspace"), "Workspace path")).resolve()
        for workspace_id, value in workspace_records.items()
    }
    if len(workspace_records) != len(workspaces):
        raise TerminalConflictReviewError("Reconciliation workspaces are duplicated")
    return _ReviewPublicationInput(
        report_snapshot, report, conflicts, workspace_records, workspace_paths
    )


def _conflict_workspace_queue_ids(
    conflicts: list[JsonDocument],
) -> dict[str, set[str]]:
    workspace_queue_ids: dict[str, set[str]] = {}
    for conflict in conflicts:
        queue_id = _text(conflict.get("queue_id"), "Conflict queue ID")
        for occurrence in _objects(
            conflict.get("occurrences"), "terminal conflict occurrences"
        ):
            workspace_queue_ids.setdefault(
                _text(occurrence.get("workspace_id"), "Conflict workspace ID"),
                set(),
            ).add(queue_id)
    return workspace_queue_ids


def _review_rows_for_conflicts(
    inputs: _ReviewPublicationInput,
) -> dict[tuple[str, str], ReviewItem]:
    review_rows: dict[tuple[str, str], ReviewItem] = {}
    for workspace_id, queue_ids in sorted(
        _conflict_workspace_queue_ids(inputs.conflicts).items()
    ):
        workspace = inputs.workspace_paths.get(workspace_id)
        if workspace is None:
            raise TerminalConflictReviewError(
                f"Conflict references an unknown workspace: {workspace_id}"
            )
        try:
            rows = list_review_items(workspace, tuple(sorted(queue_ids)))
        except AuthoringWorkbenchError as error:
            raise TerminalConflictReviewError(str(error)) from error
        indexed = {row.queue_id: row for row in rows}
        if set(indexed) != queue_ids:
            raise TerminalConflictReviewError(
                f"Conflict outcomes are unavailable: {workspace_id}"
            )
        review_rows.update(
            ((workspace_id, queue_id), row) for queue_id, row in indexed.items()
        )
    return review_rows


def _review_candidate_drafts(
    queue_id: str,
    occurrences: list[JsonDocument],
    inputs: _ReviewPublicationInput,
    review_rows: dict[tuple[str, str], ReviewItem],
) -> tuple[
    dict[tuple[str, str], _TerminalConflictCandidateDraft],
    tuple[str, str, str, str] | None,
]:
    candidates: dict[tuple[str, str], _TerminalConflictCandidateDraft] = {}
    shared: tuple[str, str, str, str] | None = None
    for occurrence in occurrences:
        workspace_id = _text(occurrence.get("workspace_id"), "Conflict workspace ID")
        authority_name = _text(occurrence.get("authority"), "Conflict authority")
        if inputs.workspace_paths.get(workspace_id) is None:
            raise TerminalConflictReviewError(
                f"Conflict references an unknown workspace: {workspace_id}"
            )
        row = review_rows[(workspace_id, queue_id)]
        expected_review = {"approved": "approved", "rejected": "rejected"}.get(
            authority_name
        )
        if (
            expected_review is None
            or row.review_status != expected_review
            or row.line_id != occurrence["line_id"]
            or hashlib.sha256(row.text.encode("utf-8")).hexdigest()
            != occurrence["text_sha256"]
            or row.authority is None
            or row.state is None
            or row.queue is None
        ):
            raise TerminalConflictReviewError(
                f"Conflict authority changed: {workspace_id}/{queue_id}"
            )
        workspace_record = inputs.workspace_records[workspace_id]
        if (
            row.authority.state_sha256 != workspace_record["state_sha256"]
            or row.authority.queue_sha256 != workspace_record["queue_sha256"]
        ):
            raise TerminalConflictReviewError(
                f"Conflict source changed after reconciliation: {workspace_id}"
            )
        try:
            audio = prepare_review_audio(row)
        except AuthoringWorkbenchError as error:
            raise TerminalConflictReviewError(str(error)) from error
        digest = hashlib.sha256(audio).hexdigest()
        if digest != row.authority.audio_sha256:
            raise TerminalConflictReviewError(
                f"Conflict WAV changed: {workspace_id}/{queue_id}"
            )
        candidate = candidates.setdefault(
            (authority_name, digest),
            {
                "authority": authority_name,
                "audio_sha256": digest,
                "audio_bytes": audio,
                "workspace_ids": [],
                "source_authorities": [],
            },
        )
        candidate["workspace_ids"].append(workspace_id)
        candidate["source_authorities"].append(
            {
                "workspace_id": workspace_id,
                "state": str(row.state.resolve()),
                "queue": str(row.queue.resolve()),
                "review_authority": {
                    "queue_sha256": row.authority.queue_sha256,
                    "state_sha256": row.authority.state_sha256,
                    "item_sha256": row.authority.item_sha256,
                    "audio_sha256": row.authority.audio_sha256,
                },
            }
        )
        row_shared = (row.line_id, row.speaker, row.voice_character, row.text)
        if shared is None:
            shared = row_shared
        elif shared != row_shared:
            raise TerminalConflictReviewError(
                f"Conflict display identity changed: {queue_id}"
            )
    return candidates, shared


def _stable_review_candidates(
    candidates: dict[tuple[str, str], _TerminalConflictCandidateDraft],
    queue_id: str,
    position: int,
    staging: Path,
) -> list[TerminalConflictReviewCandidate]:
    if len(candidates) != 2:
        raise TerminalConflictReviewError(
            f"Terminal conflict review requires exactly two distinct WAVs: {queue_id}"
        )
    stable_candidates: list[TerminalConflictReviewCandidate] = []
    for candidate_position, ((_authority, digest), candidate) in enumerate(
        sorted(candidates.items()), start=1
    ):
        candidate_id = canonical_document_sha256(
            {
                "queue_id": queue_id,
                "authority": candidate["authority"],
                "audio_sha256": digest,
            }
        )
        relative = (
            Path("audio") / f"{position:02d}" / f"candidate-{candidate_position}.wav"
        )
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        audio_bytes = candidate.pop("audio_bytes", None)
        if audio_bytes is None:
            raise TerminalConflictReviewError(
                f"Conflict WAV changed while copied: {queue_id}"
            )
        destination.write_bytes(audio_bytes)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise TerminalConflictReviewError(
                f"Conflict WAV changed while copied: {queue_id}"
            )
        try:
            info = probe_pcm16_mono_wav(destination)
        except Pcm16MonoWavError as error:
            raise TerminalConflictReviewError(str(error)) from error
        stable_candidates.append(
            {
                "candidate_id": candidate_id,
                "audio": relative.as_posix(),
                "authority": candidate["authority"],
                "audio_sha256": candidate["audio_sha256"],
                "sample_rate": info.sample_rate,
                "sample_count": info.sample_count,
                "workspace_ids": sorted(candidate["workspace_ids"]),
                "source_authorities": sorted(
                    candidate["source_authorities"],
                    key=lambda value: value["workspace_id"],
                ),
            }
        )
    return stable_candidates


def _review_case(
    conflict: JsonDocument,
    position: int,
    inputs: _ReviewPublicationInput,
    review_rows: dict[tuple[str, str], ReviewItem],
    staging: Path,
) -> JsonDocument:
    queue_id = _text(conflict.get("queue_id"), "Conflict queue ID")
    occurrences = _objects(conflict.get("occurrences"), "terminal conflict occurrences")
    if len({value["queue_record_sha256"] for value in occurrences}) != 1:
        raise TerminalConflictReviewError(
            f"Conflict changes queue content and cannot be audio-reviewed: {queue_id}"
        )
    if len({value["text_sha256"] for value in occurrences}) != 1:
        raise TerminalConflictReviewError(
            f"Conflict changes text and cannot be audio-reviewed: {queue_id}"
        )
    candidates, shared = _review_candidate_drafts(
        queue_id, occurrences, inputs, review_rows
    )
    stable_candidates = _stable_review_candidates(
        candidates, queue_id, position, staging
    )
    if shared is None:
        raise TerminalConflictReviewError(
            f"Conflict display identity changed: {queue_id}"
        )
    line_id, speaker, voice_character, text = shared
    candidate_ids = [value["candidate_id"] for value in stable_candidates]
    queue_record_sha256 = occurrences[0]["queue_record_sha256"]
    text_sha256 = occurrences[0]["text_sha256"]
    return {
        "case_id": canonical_document_sha256(
            {
                "queue_id": queue_id,
                "queue_record_sha256": queue_record_sha256,
                "text_sha256": text_sha256,
                "candidate_ids": candidate_ids,
            }
        ),
        "queue_id": queue_id,
        "line_id": line_id,
        "queue_record_sha256": queue_record_sha256,
        "text_sha256": text_sha256,
        "speaker": speaker,
        "voice_character": voice_character,
        "text": text,
        "candidates": stable_candidates,
    }


def _review_document(
    inputs: _ReviewPublicationInput,
    review_rows: dict[tuple[str, str], ReviewItem],
    staging: Path,
) -> tuple[JsonDocument, int]:
    cases = [
        _review_case(conflict, position, inputs, review_rows, staging)
        for position, conflict in enumerate(inputs.conflicts, start=1)
    ]
    candidate_total = len(cases) * 2
    body: JsonDocument = {
        "schema": TERMINAL_CONFLICT_REVIEW_SCHEMA,
        "schema_version": TERMINAL_CONFLICT_REVIEW_VERSION,
        "source_reconciliation": str(inputs.snapshot.path),
        "source_reconciliation_sha256": inputs.snapshot.sha256,
        "source_report_id": inputs.report["report_id"],
        "policy": {
            "candidate_order": "stable opaque digest order",
            "decision_scope": "one explicit winner or neither per exact queue ID",
            "workspace_mutation": "forbidden",
        },
        "case_count": len(cases),
        "candidate_count": candidate_total,
        "cases": cases,
    }
    review_id = canonical_document_sha256(body)
    return {**body, "review_id": review_id}, candidate_total


def publish_terminal_conflict_review(
    reconciliation_path: str | Path, output_directory: str | Path
) -> TerminalConflictReview:
    """Publish exact distinct WAV choices for every current terminal conflict."""
    inputs = _review_publication_input(Path(reconciliation_path).expanduser().resolve())
    output = Path(output_directory).expanduser().resolve()

    output.parent.mkdir(parents=True, exist_ok=True)
    output_exists = output.exists() or output.is_symlink()
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        review_rows = _review_rows_for_conflicts(inputs)
        document, candidate_total = _review_document(inputs, review_rows, staging)
        atomic_write_json(staging / "review.json", document, sort_keys=True)
        load_terminal_conflict_review(staging)
        assert_authority_snapshot(inputs.snapshot, "authoring reconciliation")
        review = validate_terminal_conflict_review_document(document, staging)
        _assert_source_authorities(review)
        review_id = review["review_id"]
        if output_exists:
            existing = load_terminal_conflict_review(output)
            if existing.review_id != review_id:
                raise TerminalConflictReviewError(
                    f"Terminal conflict review output has another identity: {output}"
                )
            return existing
        try:
            rename_directory_no_replace(staging, output)
        except (AtomicPublicationError, OSError) as error:
            raise TerminalConflictReviewError(
                f"Unable to publish terminal conflict review: {error}"
            ) from error
    return TerminalConflictReview(
        output, review_id, review["case_count"], candidate_total, 0, True
    )


def load_terminal_conflict_review(directory: str | Path) -> TerminalConflictReview:
    """Load one immutable conflict review and its optional decision progress."""
    directory = _review_directory(directory)
    review_path = directory / "review.json"
    try:
        payload = review_path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TerminalConflictReviewError(str(error)) from error
    document = validate_terminal_conflict_review_document(document, directory)
    if review_path.read_bytes() != payload:
        raise TerminalConflictReviewError(
            "Terminal conflict review changed while loaded"
        )
    completed = 0
    progress_path = directory / "progress.json"
    if progress_path.exists() or progress_path.is_symlink():
        completed = len(load_terminal_conflict_review_progress(directory)["decisions"])
    return TerminalConflictReview(
        directory,
        document["review_id"],
        document["case_count"],
        document["candidate_count"],
        completed,
        False,
    )


def load_terminal_conflict_review_document(
    directory: str | Path,
) -> TerminalConflictReviewDocument:
    """Return one exact validated immutable review document."""
    directory = _review_directory(directory)
    try:
        snapshot = capture_authority_file(
            directory / "review.json", "terminal conflict review"
        )
        document = validate_terminal_conflict_review_document(
            snapshot.json_document("terminal conflict review"), directory
        )
        assert_authority_snapshot(snapshot, "terminal conflict review")
    except AuthoringAuthorityError as error:
        raise TerminalConflictReviewError(str(error)) from error
    return document


def load_terminal_conflict_candidate_audio(
    directory: str | Path, case_id: str, candidate_id: str
) -> bytes:
    """Return exact copied WAV bytes for one displayed blind candidate."""
    directory = _review_directory(directory)
    document = load_terminal_conflict_review_document(directory)
    case = next(
        (item for item in document["cases"] if item["case_id"] == case_id), None
    )
    if case is None:
        raise TerminalConflictReviewError(f"Unknown terminal conflict: {case_id}")
    candidate = next(
        (item for item in case["candidates"] if item["candidate_id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise TerminalConflictReviewError(
            f"Unknown terminal conflict candidate: {candidate_id}"
        )
    path = _contained_file(directory, candidate["audio"], "candidate WAV")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != candidate["audio_sha256"]:
        raise TerminalConflictReviewError("Terminal conflict WAV changed")
    return payload


def _review_document_header(value: object) -> TerminalConflictReviewDocument:
    if (
        not _is_review_document(value)
        or value.get("schema") != TERMINAL_CONFLICT_REVIEW_SCHEMA
        or value.get("schema_version") != TERMINAL_CONFLICT_REVIEW_VERSION
    ):
        raise TerminalConflictReviewError("Unsupported terminal conflict review")
    expected_fields = {
        "schema",
        "schema_version",
        "review_id",
        "source_reconciliation",
        "source_reconciliation_sha256",
        "source_report_id",
        "policy",
        "case_count",
        "candidate_count",
        "cases",
    }
    if set(value) != expected_fields:
        raise TerminalConflictReviewError("Terminal conflict review fields changed")
    review_id = _sha256(value["review_id"], "Terminal conflict review ID")
    body = {key: item for key, item in value.items() if key != "review_id"}
    if canonical_document_sha256(body) != review_id:
        raise TerminalConflictReviewError("Terminal conflict review identity changed")
    _sha256(value["source_reconciliation_sha256"], "Source reconciliation hash")
    _sha256(value["source_report_id"], "Source reconciliation report ID")
    source_reconciliation = _text(
        value["source_reconciliation"], "Source reconciliation path"
    )
    if not Path(source_reconciliation).is_absolute():
        raise TerminalConflictReviewError("Source reconciliation path must be absolute")
    if value["policy"] != {
        "candidate_order": "stable opaque digest order",
        "decision_scope": "one explicit winner or neither per exact queue ID",
        "workspace_mutation": "forbidden",
    }:
        raise TerminalConflictReviewError("Terminal conflict review policy changed")
    cases = value["cases"]
    if not isinstance(cases, list) or not cases:
        raise TerminalConflictReviewError("Terminal conflict review cases are empty")
    if value["case_count"] != len(cases):
        raise TerminalConflictReviewError("Terminal conflict case count changed")
    return value


def _candidate_identity(
    candidate: object,
    queue_id: str,
    identities: set[tuple[str, str]],
) -> tuple[str, str]:
    if not _is_review_candidate(candidate) or set(candidate) != {
        "candidate_id",
        "authority",
        "audio",
        "audio_sha256",
        "sample_rate",
        "sample_count",
        "source_authorities",
        "workspace_ids",
    }:
        raise TerminalConflictReviewError("Terminal conflict candidate is malformed")
    candidate_id = _sha256(candidate["candidate_id"], "Terminal conflict candidate ID")
    authority = candidate["authority"]
    if authority not in {"approved", "rejected"}:
        raise TerminalConflictReviewError(
            "Terminal conflict candidate authority is invalid"
        )
    digest = _sha256(candidate["audio_sha256"], "Terminal conflict candidate WAV hash")
    expected_id = canonical_document_sha256(
        {"queue_id": queue_id, "authority": authority, "audio_sha256": digest}
    )
    if candidate_id != expected_id or (authority, digest) in identities:
        raise TerminalConflictReviewError(
            "Terminal conflict candidate identity changed"
        )
    identities.add((authority, digest))
    return candidate_id, digest


def _validate_candidate_audio(candidate: object, root: Path, digest: str) -> None:
    if not _is_review_candidate(candidate):
        raise TerminalConflictReviewError("Terminal conflict candidate is malformed")
    audio = _contained_file(root, candidate["audio"], "candidate WAV")
    payload = audio.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise TerminalConflictReviewError("Terminal conflict WAV changed")
    try:
        info = probe_pcm16_mono_wav(audio)
    except Pcm16MonoWavError as error:
        raise TerminalConflictReviewError(str(error)) from error
    if (
        candidate["sample_rate"] != info.sample_rate
        or candidate["sample_count"] != info.sample_count
    ):
        raise TerminalConflictReviewError("Terminal conflict WAV metadata changed")


def _validate_source_authority(source: object) -> None:
    if not _is_source_authority(source) or set(source) != {
        "workspace_id",
        "state",
        "queue",
        "review_authority",
    }:
        raise TerminalConflictReviewError(
            "Terminal conflict source authority is malformed"
        )
    _text(source["workspace_id"], "Terminal conflict source workspace")
    state_path = Path(_text(source["state"], "Terminal conflict source state"))
    queue_path = Path(_text(source["queue"], "Terminal conflict source queue"))
    if not state_path.is_absolute() or not queue_path.is_absolute():
        raise TerminalConflictReviewError(
            "Terminal conflict source paths must be absolute"
        )
    review_authority = source["review_authority"]
    if not isinstance(review_authority, dict) or set(review_authority) != {
        "queue_sha256",
        "state_sha256",
        "item_sha256",
        "audio_sha256",
    }:
        raise TerminalConflictReviewError(
            "Terminal conflict review authority is malformed"
        )
    for key, authority_digest in review_authority.items():
        _sha256(authority_digest, f"Terminal conflict review authority {key}")


def _validate_candidate_sources(candidate: object) -> None:
    if not _is_review_candidate(candidate):
        raise TerminalConflictReviewError("Terminal conflict candidate is malformed")
    workspace_ids: object = candidate["workspace_ids"]
    if (
        not isinstance(workspace_ids, list)
        or not workspace_ids
        or workspace_ids != sorted(set(workspace_ids))
        or any(not isinstance(item, str) or not item for item in workspace_ids)
    ):
        raise TerminalConflictReviewError(
            "Terminal conflict candidate workspaces changed"
        )
    source_authorities: object = candidate["source_authorities"]
    if (
        not isinstance(source_authorities, list)
        or len(source_authorities) != len(workspace_ids)
        or any(not isinstance(item, dict) for item in source_authorities)
        or [item.get("workspace_id") for item in source_authorities] != workspace_ids
    ):
        raise TerminalConflictReviewError(
            "Terminal conflict source authorities changed"
        )
    for source in source_authorities:
        _validate_source_authority(source)


def _validate_review_case(
    case: object,
    root: Path,
    seen_cases: set[str],
    seen_queue_ids: set[str],
) -> int:
    if not _is_review_case(case) or set(case) != {
        "case_id",
        "queue_id",
        "line_id",
        "queue_record_sha256",
        "text_sha256",
        "speaker",
        "voice_character",
        "text",
        "candidates",
    }:
        raise TerminalConflictReviewError("Terminal conflict case is malformed")
    case_id = _sha256(case["case_id"], "Terminal conflict case ID")
    queue_id = _text(case["queue_id"], "Terminal conflict queue ID")
    if case_id in seen_cases or queue_id in seen_queue_ids:
        raise TerminalConflictReviewError("Terminal conflict case is duplicated")
    seen_cases.add(case_id)
    seen_queue_ids.add(queue_id)
    _text(case["line_id"], "Terminal conflict line ID")
    queue_record_sha256 = _sha256(
        case["queue_record_sha256"], "Terminal conflict queue-record hash"
    )
    text_sha256 = _sha256(case["text_sha256"], "Terminal conflict text hash")
    text = _text(case["text"], "Terminal conflict text")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
        raise TerminalConflictReviewError("Terminal conflict text changed")
    _text(case["speaker"], "Terminal conflict speaker")
    _text(case["voice_character"], "Terminal conflict voice character")
    candidates: object = case["candidates"]
    if not isinstance(candidates, list) or len(candidates) != 2:
        raise TerminalConflictReviewError(
            "Terminal conflict requires exactly two candidates"
        )
    candidate_ids: list[str] = []
    identities: set[tuple[str, str]] = set()
    for candidate in candidates:
        candidate_id, digest = _candidate_identity(candidate, queue_id, identities)
        candidate_ids.append(candidate_id)
        _validate_candidate_audio(candidate, root, digest)
        _validate_candidate_sources(candidate)
    expected_case_id = canonical_document_sha256(
        {
            "queue_id": queue_id,
            "queue_record_sha256": queue_record_sha256,
            "text_sha256": text_sha256,
            "candidate_ids": candidate_ids,
        }
    )
    if case_id != expected_case_id:
        raise TerminalConflictReviewError("Terminal conflict case identity changed")
    return len(candidates)


def validate_terminal_conflict_review_document(
    document: object, directory: str | Path
) -> TerminalConflictReviewDocument:
    value = _review_document_header(copy.deepcopy(document))
    root = Path(directory).resolve()
    cases = value["cases"]
    seen_cases: set[str] = set()
    seen_queue_ids: set[str] = set()
    candidate_count = 0
    for case in cases:
        candidate_count += _validate_review_case(case, root, seen_cases, seen_queue_ids)
    if value["candidate_count"] != candidate_count:
        raise TerminalConflictReviewError("Terminal conflict candidate count changed")
    return value


def load_terminal_conflict_review_progress(
    directory: str | Path,
) -> TerminalConflictReviewProgress:
    directory = _review_directory(directory)
    try:
        review_snapshot = capture_authority_file(
            directory / "review.json", "terminal conflict review"
        )
        review = validate_terminal_conflict_review_document(
            review_snapshot.json_document("terminal conflict review"), directory
        )
        progress_snapshot = capture_authority_file(
            directory / "progress.json", "terminal conflict progress"
        )
        progress = progress_snapshot.json_document("terminal conflict progress")
        validated = _validate_progress(progress, review)
        _assert_progress_carry_forward(validated, review)
        assert_authority_snapshot(review_snapshot, "terminal conflict review")
        assert_authority_snapshot(progress_snapshot, "terminal conflict progress")
    except AuthoringAuthorityError as error:
        raise TerminalConflictReviewError(str(error)) from error
    return validated


def record_terminal_conflict_decision(
    directory: str | Path,
    case_id: str,
    decision: str,
    *,
    overwrite: bool = False,
) -> TerminalConflictReviewProgress:
    """Atomically record one human winner without changing source workspaces."""
    directory = _review_directory(directory)
    with _progress_lock(directory):
        review_snapshot = capture_authority_file(
            directory / "review.json", "terminal conflict review"
        )
        review = validate_terminal_conflict_review_document(
            review_snapshot.json_document("terminal conflict review"), directory
        )
        case = next(
            (value for value in review["cases"] if value["case_id"] == case_id), None
        )
        if case is None:
            raise TerminalConflictReviewError(f"Unknown terminal conflict: {case_id}")
        allowed = {value["candidate_id"] for value in case["candidates"]}
        allowed.add(NEITHER_ACCEPTABLE)
        if decision not in allowed:
            raise TerminalConflictReviewError(
                "Terminal conflict decision is not a candidate or neither"
            )
        progress_path = directory / "progress.json"
        if progress_path.exists() or progress_path.is_symlink():
            progress_snapshot = capture_authority_file(
                progress_path, "terminal conflict progress"
            )
            progress = _validate_progress(
                progress_snapshot.json_document("terminal conflict progress"), review
            )
            _assert_progress_carry_forward(progress, review)
            original_progress = progress_snapshot.payload
        else:
            progress = {
                "schema": TERMINAL_CONFLICT_PROGRESS_SCHEMA,
                "schema_version": TERMINAL_CONFLICT_PROGRESS_VERSION,
                "review_id": review["review_id"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "decisions": [],
            }
            original_progress = None
        existing = next(
            (value for value in progress["decisions"] if value["case_id"] == case_id),
            None,
        )
        if existing is not None and not overwrite:
            raise TerminalConflictReviewError("Terminal conflict is already decided")
        now = datetime.now(timezone.utc).isoformat()
        replacement: TerminalConflictReviewDecision = {
            "case_id": case_id,
            "decision": decision,
            "reviewed_at": now,
        }
        if existing is None:
            progress["decisions"].append(replacement)
        else:
            progress["decisions"][progress["decisions"].index(existing)] = replacement
        progress["decisions"].sort(key=lambda value: value["case_id"])
        progress["updated_at"] = now
        _validate_progress(progress, review)
        _assert_progress_carry_forward(progress, review)
        assert_authority_snapshot(review_snapshot, "terminal conflict review")
        _assert_source_authorities(review)
        if (
            original_progress is not None
            and progress_path.read_bytes() != original_progress
        ):
            raise TerminalConflictReviewError(
                "Terminal conflict progress changed before save"
            )
        atomic_write_json(progress_path, progress, sort_keys=True)
        return load_terminal_conflict_review_progress(directory)


def carry_terminal_conflict_decisions(
    source_directory: str | Path, target_directory: str | Path
) -> TerminalConflictReviewProgress:
    """Carry content-identical decisions into a current-authority review.

    A completed decision belongs to the immutable candidate copies in the
    source review, not to the continued immutability of every unrelated item in
    its source workspace.  The target review independently binds the current
    workspace authorities; the carry ledger binds the exact predecessor review,
    progress and candidate identities that the operator actually heard.
    """
    source_directory = _review_directory(source_directory)
    target_directory = _review_directory(target_directory)
    if source_directory == target_directory:
        raise TerminalConflictReviewError(
            "Terminal conflict carry requires distinct review directories"
        )
    with _progress_lock(target_directory):
        target_progress = target_directory / "progress.json"
        if target_progress.exists() or target_progress.is_symlink():
            raise TerminalConflictReviewError(
                "Target terminal conflict review already has progress"
            )
        try:
            source_review_snapshot = capture_authority_file(
                source_directory / "review.json", "source terminal conflict review"
            )
            source_progress_snapshot = capture_authority_file(
                source_directory / "progress.json",
                "source terminal conflict progress",
            )
            target_review_snapshot = capture_authority_file(
                target_directory / "review.json", "target terminal conflict review"
            )
            source_review = validate_terminal_conflict_review_document(
                source_review_snapshot.json_document("source terminal conflict review"),
                source_directory,
            )
            source_progress = _validate_progress(
                source_progress_snapshot.json_document(
                    "source terminal conflict progress"
                ),
                source_review,
            )
            target_review = validate_terminal_conflict_review_document(
                target_review_snapshot.json_document("target terminal conflict review"),
                target_directory,
            )
        except AuthoringAuthorityError as error:
            raise TerminalConflictReviewError(str(error)) from error
        _assert_progress_carry_forward(source_progress, source_review)
        source_cases = {case["case_id"]: case for case in source_review["cases"]}
        target_cases = {case["case_id"]: case for case in target_review["cases"]}
        carried = []
        for decision in source_progress["decisions"]:
            case_id = decision["case_id"]
            source_case = source_cases.get(case_id)
            target_case = target_cases.get(case_id)
            if source_case is None or target_case is None:
                continue
            source_candidates = [
                candidate["candidate_id"] for candidate in source_case["candidates"]
            ]
            target_candidates = [
                candidate["candidate_id"] for candidate in target_case["candidates"]
            ]
            if source_candidates != target_candidates:
                continue
            carried.append(copy.deepcopy(decision))
        if not carried:
            raise TerminalConflictReviewError(
                "No content-identical terminal conflict decisions can be carried"
            )
        carried.sort(key=lambda value: value["case_id"])
        now = datetime.now(timezone.utc).isoformat()
        progress = {
            "schema": TERMINAL_CONFLICT_PROGRESS_SCHEMA,
            "schema_version": TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION,
            "review_id": target_review["review_id"],
            "updated_at": now,
            "decisions": carried,
            "carry_forward": {
                "source_review": str(source_review_snapshot.path),
                "source_review_sha256": source_review_snapshot.sha256,
                "source_progress": str(source_progress_snapshot.path),
                "source_progress_sha256": source_progress_snapshot.sha256,
                "source_review_id": source_review["review_id"],
                "case_ids": [decision["case_id"] for decision in carried],
            },
        }
        _validate_progress(progress, target_review)
        assert_authority_snapshot(
            source_review_snapshot, "source terminal conflict review"
        )
        assert_authority_snapshot(
            source_progress_snapshot, "source terminal conflict progress"
        )
        assert_authority_snapshot(
            target_review_snapshot, "target terminal conflict review"
        )
        _assert_source_authorities(target_review)
        if target_progress.exists() or target_progress.is_symlink():
            raise TerminalConflictReviewError(
                "Target terminal conflict progress appeared before carry"
            )
        atomic_write_json(target_progress, progress, sort_keys=True)
        return load_terminal_conflict_review_progress(target_directory)


def carry_approved_cohort_terminal_conflict_decisions(
    directory: str | Path,
) -> TerminalConflictReviewProgress:
    """Reuse exact human cohort approvals for matching current candidates.

    Rejections are intentionally not promoted: rejecting one cohort WAV does
    not establish that another historical candidate is acceptable.  Every
    selected candidate must be approved in the current state, carry the exact
    cohort sample assessment, and match the review's queue ID and WAV digest.
    Decisions are saved through the normal progress transaction, so a crash can
    leave only a valid prefix and a concurrent authority change still fails
    closed.
    """
    directory = _review_directory(directory)
    review = load_terminal_conflict_review_document(directory)
    _assert_source_authorities(review)
    if (directory / "progress.json").exists():
        progress = load_terminal_conflict_review_progress(directory)
    else:
        progress = None
    completed = (
        {decision["case_id"] for decision in progress["decisions"]}
        if progress is not None
        else set()
    )
    carried = []
    for case in review["cases"]:
        if case["case_id"] in completed:
            continue
        approved = [
            candidate
            for candidate in case["candidates"]
            if candidate["authority"] == "approved"
            and _candidate_has_exact_cohort_approval(case, candidate)
        ]
        if len(approved) > 1:
            raise TerminalConflictReviewError(
                f"Multiple cohort-approved candidates exist: {case['queue_id']}"
            )
        if not approved:
            continue
        progress = record_terminal_conflict_decision(
            directory,
            case["case_id"],
            approved[0]["candidate_id"],
        )
        completed.add(case["case_id"])
        carried.append(case["case_id"])
    if not carried:
        raise TerminalConflictReviewError(
            "No exact approved cohort decisions can be carried"
        )
    if progress is None:
        raise TerminalConflictReviewError(
            "No terminal conflict decisions were recorded"
        )
    return progress


def _candidate_has_exact_cohort_approval(
    case: TerminalConflictReviewCase, candidate: TerminalConflictReviewCandidate
) -> bool:
    for source in candidate["source_authorities"]:
        try:
            snapshot = capture_authority_file(
                source["state"], "cohort-approved terminal conflict state"
            )
            authority = source["review_authority"]
            if snapshot.sha256 != authority["state_sha256"]:
                continue
            state = snapshot.json_document("cohort-approved terminal conflict state")
            items = state.get("items")
            if not isinstance(items, dict):
                continue
            item = items.get(case["queue_id"])
            if (
                not isinstance(item, dict)
                or canonical_document_sha256(item) != authority["item_sha256"]
                or item.get("status") != "approved"
                or item.get("review_status") != "approved"
                or item.get("file_sha256") != candidate["audio_sha256"]
            ):
                continue
            cohort = item.get("cohort_review")
            if not isinstance(cohort, dict) or cohort.get("decision") not in {
                "accepted",
                "split",
            }:
                continue
            samples = cohort.get("reviewed_samples")
            assessments = cohort.get("sample_assessments")
            if (
                not isinstance(samples, list)
                or not any(
                    sample.get("queue_id") == case["queue_id"]
                    and sample.get("audio_sha256") == candidate["audio_sha256"]
                    for sample in samples
                    if isinstance(sample, dict)
                )
                or not isinstance(assessments, list)
                or not any(
                    assessment.get("queue_id") == case["queue_id"]
                    and assessment.get("assessment")
                    in (
                        {"heard", "acceptable"}
                        if cohort["decision"] == "accepted"
                        else {"acceptable"}
                    )
                    for assessment in assessments
                    if isinstance(assessment, dict)
                )
            ):
                continue
            if cohort["decision"] == "split":
                statuses = cohort.get("item_review_statuses")
                if not isinstance(statuses, list) or not any(
                    status.get("queue_id") == case["queue_id"]
                    and status.get("review_status") == "approved"
                    for status in statuses
                    if isinstance(status, dict)
                ):
                    continue
            assert_authority_snapshot(
                snapshot, "cohort-approved terminal conflict state"
            )
            return True
        except AuthoringAuthorityError as error:
            raise TerminalConflictReviewError(str(error)) from error
    return False


def validate_terminal_conflict_review_progress_document(
    progress: object, review: TerminalConflictReviewDocument
) -> TerminalConflictReviewProgress:
    """Return validated mutable decisions for an already validated review."""
    return _validate_progress(progress, review)


def assert_terminal_conflict_progress_carry_forward(
    progress: TerminalConflictReviewProgress, review: TerminalConflictReviewDocument
) -> None:
    """Recheck an optional predecessor decision ledger and its authorities."""
    _assert_progress_carry_forward(progress, review)


def assert_terminal_conflict_review_source_authorities(
    review: TerminalConflictReviewDocument,
) -> None:
    """Require every source state, queue, item and WAV to match the review."""
    _assert_source_authorities(review)


def _source_reconciliation(
    review: TerminalConflictReviewDocument,
) -> tuple[AuthoritySnapshot, JsonDocument, dict[str, JsonDocument]]:
    try:
        report_snapshot = capture_authority_file(
            review["source_reconciliation"], "source reconciliation"
        )
        report = load_authoring_reconciliation(report_snapshot.path).document
    except (AuthoringAuthorityError, AuthoringReconciliationError) as error:
        raise TerminalConflictReviewError(str(error)) from error
    if (
        report_snapshot.sha256 != review["source_reconciliation_sha256"]
        or report["report_id"] != review["source_report_id"]
    ):
        raise TerminalConflictReviewError(
            "Source reconciliation changed after conflict review publication"
        )
    workspace_records = {
        _text(value.get("workspace_id"), "Workspace ID"): value
        for value in _objects(report.get("workspaces"), "reconciliation workspaces")
    }
    return report_snapshot, report, workspace_records


def _assert_candidate_source_authority(
    case: TerminalConflictReviewCase,
    candidate: TerminalConflictReviewCandidate,
    source: TerminalConflictReviewSourceAuthority,
    workspace_records: dict[str, JsonDocument],
) -> None:
    workspace_id = source["workspace_id"]
    workspace_record = workspace_records.get(workspace_id)
    if workspace_record is None:
        raise TerminalConflictReviewError(
            "Terminal conflict workspace disappeared from reconciliation"
        )
    workspace = Path(
        _text(workspace_record.get("workspace"), "Workspace path")
    ).resolve()
    expected_state = (workspace / "generated-audio" / "generation-state.json").resolve()
    expected_queue = (workspace / "queue.jsonl").resolve()
    state_path = Path(source["state"])
    queue_path = Path(source["queue"])
    if state_path != expected_state or queue_path != expected_queue:
        raise TerminalConflictReviewError(
            f"Terminal conflict source paths changed: {workspace_id}"
        )
    try:
        authority = ReviewAuthority(**source["review_authority"])
        if (
            authority.state_sha256 != workspace_record["state_sha256"]
            or authority.queue_sha256 != workspace_record["queue_sha256"]
        ):
            raise TerminalConflictReviewError(
                f"Terminal conflict reconciliation authority changed: {workspace_id}"
            )
        payload = load_review_audio_bytes(
            state_path, queue_path, case["queue_id"], authority
        )
    except (BulkGenerationError, TypeError) as error:
        raise TerminalConflictReviewError(
            f"Terminal conflict authority changed: {workspace_id}"
        ) from error
    if hashlib.sha256(payload).hexdigest() != candidate["audio_sha256"]:
        raise TerminalConflictReviewError(
            f"Terminal conflict authority changed: {workspace_id}"
        )


def _assert_source_authorities(review: TerminalConflictReviewDocument) -> None:
    report_snapshot, _report, workspace_records = _source_reconciliation(review)
    for case in review["cases"]:
        for candidate in case["candidates"]:
            for source in candidate["source_authorities"]:
                _assert_candidate_source_authority(
                    case, candidate, source, workspace_records
                )
    assert_authority_snapshot(report_snapshot, "source reconciliation")


def _objects(value: object, label: str) -> list[JsonDocument]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TerminalConflictReviewError(f"{label.capitalize()} are malformed")
    return [item for item in value if isinstance(item, dict)]


def _progress_document(
    progress: object, review: TerminalConflictReviewDocument
) -> tuple[TerminalConflictReviewProgress, int]:
    value = copy.deepcopy(progress)
    version = value.get("schema_version") if isinstance(value, dict) else None
    required = {"schema", "schema_version", "review_id", "updated_at", "decisions"}
    if version == TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION:
        required.add("carry_forward")
    if (
        not _is_progress_document(value)
        or value.get("schema") != TERMINAL_CONFLICT_PROGRESS_SCHEMA
        or version not in SUPPORTED_TERMINAL_CONFLICT_PROGRESS_VERSIONS
        or value.get("review_id") != review["review_id"]
        or set(value) != required
    ):
        raise TerminalConflictReviewError("Terminal conflict progress is invalid")
    _aware_timestamp(value["updated_at"], "Terminal conflict progress timestamp")
    return value, version


def _validate_progress_decisions(
    value: TerminalConflictReviewProgress, review: TerminalConflictReviewDocument
) -> set[str]:
    cases = {item["case_id"]: item for item in review["cases"]}
    decisions: object = value["decisions"]
    if not isinstance(decisions, list):
        raise TerminalConflictReviewError("Terminal conflict decisions are invalid")
    seen: set[str] = set()
    for decision in decisions:
        if not _is_progress_decision(decision) or set(decision) != {
            "case_id",
            "decision",
            "reviewed_at",
        }:
            raise TerminalConflictReviewError("Terminal conflict decision is malformed")
        case_id = decision["case_id"]
        if case_id in seen or case_id not in cases:
            raise TerminalConflictReviewError(
                "Terminal conflict decision is duplicated"
            )
        seen.add(case_id)
        allowed = {item["candidate_id"] for item in cases[case_id]["candidates"]}
        allowed.add(NEITHER_ACCEPTABLE)
        if decision["decision"] not in allowed:
            raise TerminalConflictReviewError("Terminal conflict winner is invalid")
        _aware_timestamp(
            decision["reviewed_at"], "Terminal conflict decision timestamp"
        )
    if decisions != sorted(decisions, key=lambda item: item["case_id"]):
        raise TerminalConflictReviewError("Terminal conflict decisions are not sorted")
    return seen


def _validate_progress_carry(
    value: TerminalConflictReviewProgress, version: int, seen: set[str]
) -> None:
    if version != TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION:
        return
    carry: object = value["carry_forward"]
    if not isinstance(carry, dict) or set(carry) != {
        "source_review",
        "source_review_sha256",
        "source_progress",
        "source_progress_sha256",
        "source_review_id",
        "case_ids",
    }:
        raise TerminalConflictReviewError(
            "Terminal conflict carry-forward ledger is malformed"
        )
    for field in ("source_review", "source_progress"):
        path = carry.get(field)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise TerminalConflictReviewError(
                "Terminal conflict carry-forward path is invalid"
            )
    for field in ("source_review_sha256", "source_progress_sha256"):
        _sha256(carry.get(field), f"Terminal conflict carry-forward {field}")
    _sha256(carry.get("source_review_id"), "Terminal conflict source review ID")
    case_ids = carry.get("case_ids")
    if (
        not isinstance(case_ids, list)
        or not case_ids
        or case_ids != sorted(set(case_ids))
        or not set(case_ids).issubset(seen)
    ):
        raise TerminalConflictReviewError(
            "Terminal conflict carried case identities are invalid"
        )


def _validate_progress(
    progress: object, review: TerminalConflictReviewDocument
) -> TerminalConflictReviewProgress:
    value, version = _progress_document(progress, review)
    seen = _validate_progress_decisions(value, review)
    _validate_progress_carry(value, version, seen)
    return value


def _assert_progress_carry_forward(
    progress: TerminalConflictReviewProgress,
    review: TerminalConflictReviewDocument,
    seen: set[tuple[str, str]] | None = None,
) -> None:
    if progress.get("schema_version") != TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION:
        return
    carry = progress["carry_forward"]
    source_review_path = _text(
        carry["source_review"], "Terminal conflict carry-forward source review"
    )
    source_progress_path = _text(
        carry["source_progress"], "Terminal conflict carry-forward source progress"
    )
    key = (source_review_path, source_progress_path)
    observed = set() if seen is None else set(seen)
    if key in observed:
        raise TerminalConflictReviewError(
            "Terminal conflict carry-forward ledger contains a cycle"
        )
    observed.add(key)
    try:
        review_snapshot = capture_authority_file(
            source_review_path, "carried terminal conflict review"
        )
        progress_snapshot = capture_authority_file(
            source_progress_path, "carried terminal conflict progress"
        )
        if review_snapshot.sha256 != _sha256(
            carry["source_review_sha256"], "Carried review SHA-256"
        ) or progress_snapshot.sha256 != _sha256(
            carry["source_progress_sha256"], "Carried progress SHA-256"
        ):
            raise TerminalConflictReviewError(
                "Carried terminal conflict authority changed"
            )
        source_review = validate_terminal_conflict_review_document(
            review_snapshot.json_document("carried terminal conflict review"),
            review_snapshot.path.parent,
        )
        source_progress = _validate_progress(
            progress_snapshot.json_document("carried terminal conflict progress"),
            source_review,
        )
    except AuthoringAuthorityError as error:
        raise TerminalConflictReviewError(str(error)) from error
    if source_review["review_id"] != _sha256(
        carry["source_review_id"], "Terminal conflict source review ID"
    ):
        raise TerminalConflictReviewError(
            "Carried terminal conflict review identity changed"
        )
    source_cases = {case["case_id"]: case for case in source_review["cases"]}
    target_cases = {case["case_id"]: case for case in review["cases"]}
    source_decisions = {
        decision["case_id"]: decision for decision in source_progress["decisions"]
    }
    target_decisions = {
        decision["case_id"]: decision for decision in progress["decisions"]
    }
    for case_id in _text_list(carry["case_ids"], "Terminal conflict carried case IDs"):
        source_case = source_cases.get(case_id)
        target_case = target_cases.get(case_id)
        if (
            source_case is None
            or target_case is None
            or [candidate["candidate_id"] for candidate in source_case["candidates"]]
            != [candidate["candidate_id"] for candidate in target_case["candidates"]]
            or source_decisions.get(case_id) != target_decisions.get(case_id)
        ):
            raise TerminalConflictReviewError(
                "Carried terminal conflict decision identity changed"
            )
    _assert_progress_carry_forward(source_progress, source_review, observed)
    assert_authority_snapshot(review_snapshot, "carried terminal conflict review")
    assert_authority_snapshot(progress_snapshot, "carried terminal conflict progress")


def _review_directory(directory: str | Path) -> Path:
    argument = Path(directory).expanduser()
    if argument.is_symlink():
        raise TerminalConflictReviewError(
            "Terminal conflict review directory must not be a symlink"
        )
    try:
        resolved = argument.resolve()
    except OSError as error:
        raise TerminalConflictReviewError(
            f"Unable to resolve terminal conflict review directory: {error}"
        ) from error
    if not resolved.is_dir():
        raise TerminalConflictReviewError(
            f"Terminal conflict review directory is unavailable: {resolved}"
        )
    return resolved


def _new_progress_lease() -> _ProgressLease:
    return {
        "schema": PROGRESS_LEASE_SCHEMA,
        "schema_version": PROGRESS_LEASE_VERSION,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "process_started_at": process_started_at(os.getpid()),
        "lease_id": uuid.uuid4().hex,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def _is_stored_progress_lease(value: object) -> TypeGuard[_StoredProgressLease]:
    return (
        isinstance(value, dict)
        and value.get("schema") == PROGRESS_LEASE_SCHEMA
        and value.get("schema_version") == PROGRESS_LEASE_VERSION
        and isinstance(value.get("pid"), int)
        and value["pid"] > 0
        and isinstance(value.get("hostname"), str)
        and bool(value["hostname"])
        and isinstance(value.get("lease_id"), str)
        and bool(value["lease_id"])
    )


def _stored_progress_lease(path: Path) -> tuple[bytes, _StoredProgressLease]:
    try:
        payload = path.read_bytes()
        existing = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TerminalConflictReviewError(
            "Unrecognized terminal conflict progress lock blocks review"
        ) from error
    if not _is_stored_progress_lease(existing):
        raise TerminalConflictReviewError(
            "Unrecognized terminal conflict progress lock blocks review"
        )
    return payload, existing


def _stored_lease_is_live(existing: _StoredProgressLease) -> bool:
    if existing["hostname"] != socket.gethostname():
        raise TerminalConflictReviewError(
            "Another terminal conflict decision is being saved"
        )
    if not process_is_alive(existing["pid"]):
        return False
    recorded_start = existing.get("process_started_at")
    actual_start = process_started_at(existing["pid"])
    if recorded_start is None or actual_start is None:
        raise TerminalConflictReviewError(
            "Another terminal conflict decision is being saved"
        )
    return recorded_start == actual_start


def _recover_stale_progress_lock(directory: Path, path: Path) -> None:
    existing_payload, existing = _stored_progress_lease(path)
    if _stored_lease_is_live(existing):
        raise TerminalConflictReviewError(
            "Another terminal conflict decision is being saved"
        )
    interrupted = directory / (".progress.lock.interrupted-" + uuid.uuid4().hex)
    if path.read_bytes() != existing_payload:
        raise TerminalConflictReviewError(
            "Terminal conflict progress lock changed during recovery"
        )
    try:
        path.rename(interrupted)
    except OSError as error:
        raise TerminalConflictReviewError(
            "Unable to recover an interrupted terminal conflict save"
        ) from error


def _remove_progress_lock(path: Path, guard_path: Path, lease: _ProgressLease) -> None:
    try:
        with exclusive_advisory_lock(guard_path, blocking=True):
            if json.loads(path.read_text(encoding="utf-8")) == lease:
                path.unlink()
    except OSError, json.JSONDecodeError, AdvisoryLockBusyError:
        return


@contextmanager
def _progress_lock(directory: Path) -> Iterator[None]:
    path = directory / ".progress.lock"
    guard_path = directory / ".progress.lock.guard"
    lease = _new_progress_lease()
    try:
        with exclusive_advisory_lock(guard_path):
            if path.exists():
                _recover_stale_progress_lock(directory, path)
            try:
                write_json_document_no_replace(
                    path,
                    lease,
                    "terminal conflict progress lock",
                    error_type=TerminalConflictReviewError,
                )
            except TerminalConflictReviewError as error:
                if isinstance(error.__cause__, FileExistsError):
                    raise TerminalConflictReviewError(
                        "Another terminal conflict decision is being saved"
                    ) from error
                raise
    except AdvisoryLockBusyError as error:
        raise TerminalConflictReviewError(
            "Another terminal conflict decision is being saved"
        ) from error
    try:
        yield
    finally:
        _remove_progress_lock(path, guard_path, lease)


__all__ = [
    "NEITHER_ACCEPTABLE",
    "TERMINAL_CONFLICT_PROGRESS_SCHEMA",
    "TERMINAL_CONFLICT_PROGRESS_VERSION",
    "TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION",
    "SUPPORTED_TERMINAL_CONFLICT_PROGRESS_VERSIONS",
    "TERMINAL_CONFLICT_REVIEW_SCHEMA",
    "TERMINAL_CONFLICT_REVIEW_VERSION",
    "TerminalConflictReview",
    "TerminalConflictReviewError",
    "assert_terminal_conflict_review_source_authorities",
    "assert_terminal_conflict_progress_carry_forward",
    "carry_approved_cohort_terminal_conflict_decisions",
    "carry_terminal_conflict_decisions",
    "load_terminal_conflict_review",
    "load_terminal_conflict_candidate_audio",
    "load_terminal_conflict_review_document",
    "load_terminal_conflict_review_progress",
    "publish_terminal_conflict_review",
    "record_terminal_conflict_decision",
    "validate_terminal_conflict_review_document",
    "validate_terminal_conflict_review_progress_document",
]

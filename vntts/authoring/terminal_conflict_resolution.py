"""Immutable application evidence for completed terminal conflict decisions."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeAlias, TypedDict, TypeGuard

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav

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
from vntts.authoring.terminal_conflict_records import (
    require_terminal_conflict_directory,
    require_terminal_conflict_file,
    require_terminal_conflict_sha256,
    require_terminal_conflict_text,
    require_terminal_conflict_timestamp,
)
from vntts.authoring.terminal_conflict_review import (
    NEITHER_ACCEPTABLE,
    TerminalConflictReviewCase,
    TerminalConflictReviewDecision,
    TerminalConflictReviewDocument,
    TerminalConflictReviewError,
    TerminalConflictReviewProgress,
    assert_terminal_conflict_progress_carry_forward,
    assert_terminal_conflict_review_source_authorities,
    validate_terminal_conflict_review_document,
    validate_terminal_conflict_review_progress_document,
)

TERMINAL_CONFLICT_RESOLUTION_SCHEMA = "vntts.authoring-terminal-conflict-resolution"
TERMINAL_CONFLICT_RESOLUTION_VERSION = 1

JsonDocument: TypeAlias = dict[str, object]


class TerminalConflictResolutionRecord(TypedDict):
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


class TerminalConflictResolutionDocument(TypedDict):
    schema: str
    schema_version: int
    resolution_id: str
    source_review: str
    source_review_sha256: str
    source_progress: str
    source_progress_sha256: str
    source_review_id: str
    source_report_id: str
    policy: dict[str, str]
    case_count: int
    selected_count: int
    neither_count: int
    resolutions: list[TerminalConflictResolutionRecord]


def _is_resolution_document(
    value: object,
) -> TypeGuard[TerminalConflictResolutionDocument]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_resolution_record(
    value: object,
) -> TypeGuard[TerminalConflictResolutionRecord]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


class TerminalConflictResolutionError(RuntimeError):
    """Completed terminal conflict decisions cannot be published safely."""


@dataclass(frozen=True)
class _ResolutionInputs:
    review_snapshot: AuthoritySnapshot
    review: TerminalConflictReviewDocument
    progress_snapshot: AuthoritySnapshot
    progress: TerminalConflictReviewProgress


@dataclass(frozen=True)
class _ResolutionRecords:
    records: list[TerminalConflictResolutionRecord]
    selected_count: int
    neither_count: int
    selected_snapshots: list[AuthoritySnapshot]


@dataclass(frozen=True)
class _ResolutionAuthorityInputs:
    resolution_snapshot: AuthoritySnapshot
    resolution: TerminalConflictResolutionDocument
    review_snapshot: AuthoritySnapshot
    review: TerminalConflictReviewDocument
    progress_snapshot: AuthoritySnapshot
    progress: TerminalConflictReviewProgress


def _contained_file(root: str | Path, value: object, label: str) -> Path:
    return Path(
        require_terminal_conflict_file(
            root, value, label, error_type=TerminalConflictResolutionError
        )
    )


def _directory(value: str | Path, label: str) -> Path:
    return Path(
        require_terminal_conflict_directory(
            value, label, error_type=TerminalConflictResolutionError
        )
    )


def _text(value: object, label: str) -> str:
    return str(
        require_terminal_conflict_text(
            value, label, error_type=TerminalConflictResolutionError
        )
    )


def _sha256(value: object, label: str) -> str:
    return str(
        require_terminal_conflict_sha256(
            value, label, error_type=TerminalConflictResolutionError
        )
    )


def _aware_timestamp(value: object, label: str) -> object:
    return require_terminal_conflict_timestamp(
        value, label, error_type=TerminalConflictResolutionError
    )


@dataclass(frozen=True)
class TerminalConflictResolution:
    directory: Path
    resolution_id: str
    case_count: int
    selected_count: int
    neither_count: int
    created: bool = False

    @property
    def resolution(self) -> Path:
        return self.directory / "resolution.json"

    def to_dict(self) -> JsonDocument:
        return {
            "directory": str(self.directory),
            "resolution": str(self.resolution),
            "resolution_id": self.resolution_id,
            "case_count": self.case_count,
            "selected_count": self.selected_count,
            "neither_count": self.neither_count,
            "created": self.created,
        }


def publish_terminal_conflict_resolution(
    review_directory: str | Path, output_directory: str | Path
) -> TerminalConflictResolution:
    """Publish exact completed decisions without changing any source workspace."""
    review_root = _directory(review_directory, "terminal conflict review")
    output = Path(output_directory).expanduser().resolve()
    inputs = _load_resolution_inputs(review_root)
    decisions: dict[str, TerminalConflictReviewDecision] = {
        item["case_id"]: item for item in inputs.progress["decisions"]
    }
    if len(decisions) != inputs.review["case_count"]:
        raise TerminalConflictResolutionError(
            "Every terminal conflict requires a decision before resolution publication"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output_exists = output.exists() or output.is_symlink()
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        built = _build_resolution_records(
            inputs.review, decisions, review_root, staging
        )
        body = _resolution_body(inputs, built)
        resolution_id = canonical_document_sha256(body)
        document = {**body, "resolution_id": resolution_id}
        atomic_write_json(staging / "resolution.json", document, sort_keys=True)
        load_terminal_conflict_resolution(staging)

        _assert_resolution_publication_sources(inputs, built.selected_snapshots)
        if output_exists:
            existing = load_terminal_conflict_resolution(output)
            if existing.resolution_id != resolution_id:
                raise TerminalConflictResolutionError(
                    f"Terminal conflict resolution output has another identity: {output}"
                )
            return existing
        try:
            rename_directory_no_replace(staging, output)
        except (AtomicPublicationError, OSError) as error:
            raise TerminalConflictResolutionError(
                f"Unable to publish terminal conflict resolution: {error}"
            ) from error
    return TerminalConflictResolution(
        output,
        resolution_id,
        len(built.records),
        built.selected_count,
        built.neither_count,
        True,
    )


def _load_resolution_inputs(review_root: Path) -> _ResolutionInputs:
    try:
        review_snapshot = capture_authority_file(
            review_root / "review.json", "terminal conflict review"
        )
        review = validate_terminal_conflict_review_document(
            review_snapshot.json_document("terminal conflict review"), review_root
        )
        progress_snapshot = capture_authority_file(
            review_root / "progress.json", "terminal conflict progress"
        )
        progress = validate_terminal_conflict_review_progress_document(
            progress_snapshot.json_document("terminal conflict progress"), review
        )
        assert_terminal_conflict_progress_carry_forward(progress, review)
        assert_terminal_conflict_review_source_authorities(review)
    except (AuthoringAuthorityError, TerminalConflictReviewError) as error:
        raise TerminalConflictResolutionError(str(error)) from error
    return _ResolutionInputs(review_snapshot, review, progress_snapshot, progress)


def _build_resolution_records(
    review: TerminalConflictReviewDocument,
    decisions: dict[str, TerminalConflictReviewDecision],
    review_root: Path,
    staging: Path,
) -> _ResolutionRecords:
    records: list[TerminalConflictResolutionRecord] = []
    selected_snapshots: list[AuthoritySnapshot] = []
    selected_count = 0
    neither_count = 0
    for position, case in enumerate(review["cases"], start=1):
        saved = decisions[case["case_id"]]
        candidate_ids = [item["candidate_id"] for item in case["candidates"]]
        if saved["decision"] == NEITHER_ACCEPTABLE:
            neither_count += 1
            records.append(
                {
                    "case_id": case["case_id"],
                    "queue_id": case["queue_id"],
                    "line_id": case["line_id"],
                    "queue_record_sha256": case["queue_record_sha256"],
                    "text_sha256": case["text_sha256"],
                    "candidate_ids": candidate_ids,
                    "reviewed_at": saved["reviewed_at"],
                    "decision": NEITHER_ACCEPTABLE,
                    "selected_candidate_id": None,
                    "selected_authority": None,
                    "selected_audio": None,
                    "selected_audio_sha256": None,
                    "sample_rate": None,
                    "sample_count": None,
                }
            )
            continue
        candidate = next(
            (
                item
                for item in case["candidates"]
                if item["candidate_id"] == saved["decision"]
            ),
            None,
        )
        if candidate is None:
            raise TerminalConflictResolutionError(
                f"Terminal conflict decision is not a current candidate: {case['queue_id']}"
            )
        source = _contained_file(
            review_root, candidate["audio"], "selected candidate WAV"
        )
        try:
            snapshot = capture_authority_file(
                source,
                "selected terminal conflict WAV",
                root=review_root,
            )
        except AuthoringAuthorityError as error:
            raise TerminalConflictResolutionError(str(error)) from error
        if snapshot.sha256 != candidate["audio_sha256"]:
            raise TerminalConflictResolutionError(
                f"Selected terminal conflict WAV changed: {case['queue_id']}"
            )
        selected_snapshots.append(snapshot)
        relative = Path("audio") / f"{position:02d}.wav"
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(snapshot.payload)
        try:
            info = probe_pcm16_mono_wav(destination)
        except Pcm16MonoWavError as error:
            raise TerminalConflictResolutionError(str(error)) from error
        selected_count += 1
        records.append(
            {
                "case_id": case["case_id"],
                "queue_id": case["queue_id"],
                "line_id": case["line_id"],
                "queue_record_sha256": case["queue_record_sha256"],
                "text_sha256": case["text_sha256"],
                "candidate_ids": candidate_ids,
                "reviewed_at": saved["reviewed_at"],
                "decision": "selected_candidate",
                "selected_candidate_id": candidate["candidate_id"],
                "selected_authority": candidate["authority"],
                "selected_audio": relative.as_posix(),
                "selected_audio_sha256": snapshot.sha256,
                "sample_rate": info.sample_rate,
                "sample_count": info.sample_count,
            }
        )
    records.sort(key=lambda value: value["queue_id"])
    return _ResolutionRecords(
        records, selected_count, neither_count, selected_snapshots
    )


def _resolution_body(
    inputs: _ResolutionInputs, built: _ResolutionRecords
) -> JsonDocument:
    return {
        "schema": TERMINAL_CONFLICT_RESOLUTION_SCHEMA,
        "schema_version": TERMINAL_CONFLICT_RESOLUTION_VERSION,
        "source_review": str(inputs.review_snapshot.path),
        "source_review_sha256": inputs.review_snapshot.sha256,
        "source_progress": str(inputs.progress_snapshot.path),
        "source_progress_sha256": inputs.progress_snapshot.sha256,
        "source_review_id": inputs.review["review_id"],
        "source_report_id": inputs.review["source_report_id"],
        "policy": {
            "workspace_mutation": "forbidden",
            "historical_authority_suppression": "forbidden",
            "neither_result": "new repair hypothesis required",
        },
        "case_count": len(built.records),
        "selected_count": built.selected_count,
        "neither_count": built.neither_count,
        "resolutions": built.records,
    }


def _assert_resolution_publication_sources(
    inputs: _ResolutionInputs, selected_snapshots: list[AuthoritySnapshot]
) -> None:
    try:
        assert_authority_snapshot(inputs.review_snapshot, "terminal conflict review")
        assert_authority_snapshot(
            inputs.progress_snapshot, "terminal conflict progress"
        )
        for snapshot in selected_snapshots:
            assert_authority_snapshot(snapshot, "selected terminal conflict WAV")
        assert_terminal_conflict_review_source_authorities(inputs.review)
    except (AuthoringAuthorityError, TerminalConflictReviewError) as error:
        raise TerminalConflictResolutionError(str(error)) from error


def load_terminal_conflict_resolution(
    directory: str | Path,
) -> TerminalConflictResolution:
    """Load and fully validate one immutable resolution publication."""
    root = _directory(directory, "terminal conflict resolution")
    document = load_terminal_conflict_resolution_document(root)
    return TerminalConflictResolution(
        root,
        document["resolution_id"],
        document["case_count"],
        document["selected_count"],
        document["neither_count"],
        False,
    )


def load_terminal_conflict_resolution_document(
    directory: str | Path,
) -> TerminalConflictResolutionDocument:
    root = _directory(directory, "terminal conflict resolution")
    try:
        snapshot = capture_authority_file(
            root / "resolution.json", "terminal conflict resolution"
        )
        document = validate_terminal_conflict_resolution_document(
            snapshot.json_document("terminal conflict resolution"), root
        )
        assert_authority_snapshot(snapshot, "terminal conflict resolution")
    except AuthoringAuthorityError as error:
        raise TerminalConflictResolutionError(str(error)) from error
    return document


def assert_terminal_conflict_resolution_source_authorities(
    directory: str | Path,
) -> TerminalConflictResolutionDocument:
    """Recheck resolution, review, progress and every historical source CAS."""
    root = _directory(directory, "terminal conflict resolution")
    inputs = _load_resolution_authority_inputs(root)
    decisions: dict[str, TerminalConflictReviewDecision] = {
        item["case_id"]: item for item in inputs.progress["decisions"]
    }
    cases: dict[str, TerminalConflictReviewCase] = {
        item["case_id"]: item for item in inputs.review["cases"]
    }
    try:
        selected_snapshots = [
            snapshot
            for record in inputs.resolution["resolutions"]
            if (
                snapshot := _assert_resolution_record_source(
                    record, cases, decisions, root
                )
            )
            is not None
        ]
        _assert_resolution_authority_inputs(inputs, selected_snapshots)
    except (AuthoringAuthorityError, TerminalConflictReviewError) as error:
        raise TerminalConflictResolutionError(str(error)) from error
    return inputs.resolution


def _load_resolution_authority_inputs(root: Path) -> _ResolutionAuthorityInputs:
    try:
        resolution_snapshot = capture_authority_file(
            root / "resolution.json", "terminal conflict resolution"
        )
        resolution = validate_terminal_conflict_resolution_document(
            resolution_snapshot.json_document("terminal conflict resolution"), root
        )
        review_snapshot = capture_authority_file(
            resolution["source_review"], "terminal conflict source review"
        )
        progress_snapshot = capture_authority_file(
            resolution["source_progress"], "terminal conflict source progress"
        )
        if (
            review_snapshot.sha256 != resolution["source_review_sha256"]
            or progress_snapshot.sha256 != resolution["source_progress_sha256"]
        ):
            raise TerminalConflictResolutionError(
                "Terminal conflict resolution sources changed"
            )
        review = validate_terminal_conflict_review_document(
            review_snapshot.json_document("terminal conflict source review"),
            review_snapshot.path.parent,
        )
        progress = validate_terminal_conflict_review_progress_document(
            progress_snapshot.json_document("terminal conflict source progress"),
            review,
        )
        assert_terminal_conflict_progress_carry_forward(progress, review)
        if (
            review["review_id"] != resolution["source_review_id"]
            or review["source_report_id"] != resolution["source_report_id"]
        ):
            raise TerminalConflictResolutionError(
                "Terminal conflict resolution source identity changed"
            )
        assert_terminal_conflict_review_source_authorities(review)
    except (AuthoringAuthorityError, TerminalConflictReviewError) as error:
        raise TerminalConflictResolutionError(str(error)) from error
    return _ResolutionAuthorityInputs(
        resolution_snapshot,
        resolution,
        review_snapshot,
        review,
        progress_snapshot,
        progress,
    )


def _assert_resolution_record_source(
    record: TerminalConflictResolutionRecord,
    cases: dict[str, TerminalConflictReviewCase],
    decisions: dict[str, TerminalConflictReviewDecision],
    root: Path,
) -> AuthoritySnapshot | None:
    case = cases.get(record["case_id"])
    decision = decisions.get(record["case_id"])
    if (
        case is None
        or decision is None
        or case["queue_id"] != record["queue_id"]
        or case["line_id"] != record["line_id"]
        or case["queue_record_sha256"] != record["queue_record_sha256"]
        or case["text_sha256"] != record["text_sha256"]
        or [item["candidate_id"] for item in case["candidates"]]
        != record["candidate_ids"]
    ):
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution no longer matches its review"
        )
    if decision["reviewed_at"] != record["reviewed_at"]:
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution decision timestamp changed"
        )
    if decision["decision"] == NEITHER_ACCEPTABLE:
        if record["decision"] != NEITHER_ACCEPTABLE:
            raise TerminalConflictResolutionError(
                "Terminal conflict neither decision changed"
            )
        return None
    candidate = next(
        (
            item
            for item in case["candidates"]
            if item["candidate_id"] == decision["decision"]
        ),
        None,
    )
    if (
        candidate is None
        or record["decision"] != "selected_candidate"
        or record["selected_candidate_id"] != candidate["candidate_id"]
        or record["selected_authority"] != candidate["authority"]
        or record["selected_audio_sha256"] != candidate["audio_sha256"]
    ):
        raise TerminalConflictResolutionError(
            "Selected terminal conflict resolution changed"
        )
    selected_snapshot = capture_authority_file(
        _contained_file(root, record["selected_audio"], "selected resolution WAV"),
        "selected resolution WAV",
        root=root,
    )
    if selected_snapshot.sha256 != record["selected_audio_sha256"]:
        raise TerminalConflictResolutionError(
            "Selected terminal conflict resolution WAV changed"
        )
    return selected_snapshot


def _assert_resolution_authority_inputs(
    inputs: _ResolutionAuthorityInputs, selected_snapshots: list[AuthoritySnapshot]
) -> None:
    assert_authority_snapshot(
        inputs.resolution_snapshot, "terminal conflict resolution"
    )
    assert_authority_snapshot(inputs.review_snapshot, "terminal conflict source review")
    assert_authority_snapshot(
        inputs.progress_snapshot, "terminal conflict source progress"
    )
    for snapshot in selected_snapshots:
        assert_authority_snapshot(snapshot, "selected resolution WAV")


def validate_terminal_conflict_resolution_document(
    document: object, directory: str | Path
) -> TerminalConflictResolutionDocument:
    value = copy.deepcopy(document)
    value, raw_records, root = _validate_resolution_document_header(value, directory)
    seen_cases: set[str] = set()
    seen_queue_ids: set[str] = set()
    records: list[TerminalConflictResolutionRecord] = []
    selected = 0
    neither = 0
    selected_paths: set[str] = set()
    for raw_record in raw_records:
        record, candidate_ids, queue_id = _validate_resolution_record_identity(
            raw_record, seen_cases, seen_queue_ids
        )
        records.append(record)
        selected_audio = _validate_resolution_record_selection(
            record, candidate_ids, queue_id, root
        )
        if selected_audio is None:
            neither += 1
        else:
            selected += 1
            selected_paths.add(selected_audio)
    _validate_resolution_document_completion(
        value, records, root, selected, neither, selected_paths
    )
    return value


def _validate_resolution_document_header(
    value: object, directory: str | Path
) -> tuple[
    TerminalConflictResolutionDocument, list[TerminalConflictResolutionRecord], Path
]:
    fields = {
        "schema",
        "schema_version",
        "resolution_id",
        "source_review",
        "source_review_sha256",
        "source_progress",
        "source_progress_sha256",
        "source_review_id",
        "source_report_id",
        "policy",
        "case_count",
        "selected_count",
        "neither_count",
        "resolutions",
    }
    if (
        not _is_resolution_document(value)
        or set(value) != fields
        or value.get("schema") != TERMINAL_CONFLICT_RESOLUTION_SCHEMA
        or value.get("schema_version") != TERMINAL_CONFLICT_RESOLUTION_VERSION
    ):
        raise TerminalConflictResolutionError(
            "Unsupported terminal conflict resolution"
        )
    _validate_resolution_document_identity(value)
    raw_records = value["resolutions"]
    if not isinstance(raw_records, list) or not raw_records:
        raise TerminalConflictResolutionError("Terminal conflict resolutions are empty")
    if value["case_count"] != len(raw_records):
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution case count changed"
        )
    return value, raw_records, Path(directory).resolve()


def _validate_resolution_document_identity(
    value: TerminalConflictResolutionDocument,
) -> None:
    resolution_id = _sha256(value["resolution_id"], "Resolution ID")
    if (
        canonical_document_sha256(
            {key: item for key, item in value.items() if key != "resolution_id"}
        )
        != resolution_id
    ):
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution identity changed"
        )
    for field in ("source_review", "source_progress"):
        path = Path(_text(value[field], field.replace("_", " ").title()))
        if not path.is_absolute():
            raise TerminalConflictResolutionError(
                "Terminal conflict resolution source paths must be absolute"
            )
    for digest, label in (
        (value["source_review_sha256"], "Source Review Sha256"),
        (value["source_progress_sha256"], "Source Progress Sha256"),
        (value["source_review_id"], "Source Review Id"),
        (value["source_report_id"], "Source Report Id"),
    ):
        _sha256(digest, label)
    if value["policy"] != {
        "workspace_mutation": "forbidden",
        "historical_authority_suppression": "forbidden",
        "neither_result": "new repair hypothesis required",
    }:
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution policy changed"
        )


def _validate_resolution_record_identity(
    value: object,
    seen_cases: set[str],
    seen_queue_ids: set[str],
) -> tuple[TerminalConflictResolutionRecord, list[str], str]:
    expected_fields = {
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
    if not _is_resolution_record(value) or set(value) != expected_fields:
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution record is malformed"
        )
    record = value
    case_id = _sha256(record["case_id"], "Resolution case ID")
    queue_id = _text(record["queue_id"], "Resolution queue ID")
    if case_id in seen_cases or queue_id in seen_queue_ids:
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution is duplicated"
        )
    seen_cases.add(case_id)
    seen_queue_ids.add(queue_id)
    _text(record["line_id"], "Resolution line ID")
    _sha256(record["queue_record_sha256"], "Resolution queue-record hash")
    _sha256(record["text_sha256"], "Resolution text hash")
    candidate_ids = record["candidate_ids"]
    if (
        not isinstance(candidate_ids, list)
        or len(candidate_ids) < 2
        or len(candidate_ids) != len(set(candidate_ids))
    ):
        raise TerminalConflictResolutionError(
            "Terminal conflict candidate IDs are invalid"
        )
    for candidate_id in candidate_ids:
        _sha256(candidate_id, "Resolution candidate ID")
    expected_case_id = canonical_document_sha256(
        {
            "queue_id": queue_id,
            "queue_record_sha256": record["queue_record_sha256"],
            "text_sha256": record["text_sha256"],
            "candidate_ids": candidate_ids,
        }
    )
    if case_id != expected_case_id:
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution case identity changed"
        )
    _aware_timestamp(record["reviewed_at"], "Resolution review timestamp")
    return record, candidate_ids, queue_id


def _validate_resolution_record_selection(
    record: TerminalConflictResolutionRecord,
    candidate_ids: list[str],
    queue_id: str,
    root: Path,
) -> str | None:
    if record["decision"] == NEITHER_ACCEPTABLE:
        if any(
            record[field] is not None
            for field in (
                "selected_candidate_id",
                "selected_authority",
                "selected_audio",
                "selected_audio_sha256",
                "sample_rate",
                "sample_count",
            )
        ):
            raise TerminalConflictResolutionError(
                "Neither resolution must not select audio"
            )
        return None
    if record["decision"] != "selected_candidate":
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution decision is invalid"
        )
    selected_id = _sha256(record["selected_candidate_id"], "Selected candidate ID")
    if selected_id not in candidate_ids:
        raise TerminalConflictResolutionError(
            "Selected terminal conflict candidate is unavailable"
        )
    if record["selected_authority"] not in {"approved", "rejected"}:
        raise TerminalConflictResolutionError(
            "Selected terminal conflict authority is invalid"
        )
    digest = _sha256(record["selected_audio_sha256"], "Selected resolution WAV hash")
    expected_selected_id = canonical_document_sha256(
        {
            "queue_id": queue_id,
            "authority": record["selected_authority"],
            "audio_sha256": digest,
        }
    )
    if selected_id != expected_selected_id:
        raise TerminalConflictResolutionError(
            "Selected terminal conflict identity changed"
        )
    selected_audio = _text(record["selected_audio"], "Selected resolution WAV")
    audio = _contained_file(root, selected_audio, "selected WAV")
    if hashlib.sha256(audio.read_bytes()).hexdigest() != digest:
        raise TerminalConflictResolutionError("Selected terminal conflict WAV changed")
    try:
        info = probe_pcm16_mono_wav(audio)
    except Pcm16MonoWavError as error:
        raise TerminalConflictResolutionError(str(error)) from error
    if (
        record["sample_rate"] != info.sample_rate
        or record["sample_count"] != info.sample_count
    ):
        raise TerminalConflictResolutionError(
            "Selected terminal conflict WAV metadata changed"
        )
    return PurePosixPath(selected_audio).as_posix()


def _validate_resolution_document_completion(
    value: TerminalConflictResolutionDocument,
    records: list[TerminalConflictResolutionRecord],
    root: Path,
    selected: int,
    neither: int,
    selected_paths: set[str],
) -> None:
    if (
        value["selected_count"] != selected
        or value["neither_count"] != neither
        or selected + neither != len(records)
    ):
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution counts changed"
        )
    if records != sorted(records, key=lambda item: item["queue_id"]):
        raise TerminalConflictResolutionError(
            "Terminal conflict resolutions are not sorted"
        )
    inventory = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    expected_inventory = {"resolution.json", *selected_paths}
    if inventory != expected_inventory:
        raise TerminalConflictResolutionError(
            "Terminal conflict resolution inventory changed"
        )


__all__ = [
    "TERMINAL_CONFLICT_RESOLUTION_SCHEMA",
    "TERMINAL_CONFLICT_RESOLUTION_VERSION",
    "TerminalConflictResolution",
    "TerminalConflictResolutionError",
    "assert_terminal_conflict_resolution_source_authorities",
    "load_terminal_conflict_resolution",
    "load_terminal_conflict_resolution_document",
    "publish_terminal_conflict_resolution",
    "validate_terminal_conflict_resolution_document",
]

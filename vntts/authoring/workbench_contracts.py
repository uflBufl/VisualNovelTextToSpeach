"""Shared value contracts for the authoring workbench."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueItem,
)

from vntts.authoring.bulk_generation import ReviewAuthority

WORKSPACE_SCHEMA = "vntts.authoring-workspace"
WORKSPACE_VERSION = 1


class AuthoringWorkbenchError(RuntimeError):
    """A workspace or authoring action is unsafe or inconsistent."""


class AuthoringRuntimeStatus(str, Enum):
    READY = "ready"
    RUNNING_HERE = "running_here"
    RUNNING_EXTERNAL = "running_external"
    INTERRUPTED = "interrupted"
    NEEDS_REVIEW = "needs_review"
    NEEDS_ATTENTION = "needs_attention"
    COMPLETE = "complete"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WorkspaceCreationResult:
    directory: Path
    created: bool


@dataclass(frozen=True)
class _OutcomeMergeBase:
    directory: Path
    document: dict[str, object]
    workspace_sha256: str
    queue: VoiceGenerationQueue
    state: dict[str, object]
    state_sha256: str
    queue_sha256: str
    queue_by_id: dict[str, VoiceGenerationQueueItem]


@dataclass(frozen=True)
class _OutcomeMergeSource:
    directory: Path
    document: dict[str, object]
    workspace_sha256: str
    state: dict[str, object]
    state_sha256: str
    selected_ids: tuple[str, ...]
    selected_records: dict[str, dict[str, object]] | None


@dataclass
class _OutcomeMergeSources:
    items: dict[str, tuple[dict[str, object], dict[str, object]]]
    records: list[dict[str, object]]
    snapshots: list[tuple[Path, str]]
    audio: dict[str, tuple[Path, bytes, Path]]


@dataclass(frozen=True)
class ActiveAttempt:
    queue_id: str | None
    line_id: str | None
    speaker: str | None
    text: str | None
    phase: str | None
    attempt: int | None
    attempt_limit: int | None
    total_attempts: int | None
    seed: int | None
    started_at: str | None
    updated_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class WorkspaceSummary:
    directory: Path
    title: str
    runtime_status: AuthoringRuntimeStatus
    queue_items: int
    eligible: int
    pending: int
    generated: int
    approved: int
    rejected: int
    live_fallback: int
    omitted: int
    failed: int
    skipped_actions: int
    skipped_sound_effects: int
    recoverable_source_audio: int
    manual_review: int
    resolve_audio: int
    missing_voice: int | None
    blocked_reasons: tuple[str, ...]
    active: ActiveAttempt | None
    failure_reasons: tuple[tuple[str, int], ...]
    queue: Path
    output: Path
    state: Path | None
    voice_manifest: Path | None
    latest_line: str | None
    latest_text: str | None
    latest_status: str | None
    latest_updated_at: str | None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for field in ("directory", "queue", "output", "state", "voice_manifest"):
            value = payload[field]
            payload[field] = None if value is None else str(value)
        return payload


@dataclass(frozen=True)
class ReviewItem:
    queue_id: str
    line_id: str
    speaker: str
    voice_character: str
    text: str
    status: str
    review_status: str | None
    attempts: int
    seed: int | None
    last_error: str | None
    audio: Path | None
    collection_id: str | None = None
    authority: ReviewAuthority | None = None
    state: Path | None = None
    queue: Path | None = None
    duration_seconds: float | None = None
    words_per_minute: float | None = None
    peak: float | None = None
    technical_flags: tuple[str, ...] = ()
    pace_baseline_wpm: float | None = None
    pace_ratio: float | None = None
    pace_baseline_scope: str | None = None
    pace_advisories: tuple[str, ...] = ()
    failure_category: str | None = None
    internal_pause_seconds: float | None = None
    repair_strategy: str | None = None


@dataclass(frozen=True)
class GenerationReadiness:
    selected: int
    pending: int
    failed: int
    ready: int
    missing_voice: int | None
    blocked_reasons: tuple[str, ...]
    queue_ids: tuple[str, ...]


@dataclass(frozen=True)
class CollectionSelection:
    collection_ids: tuple[str, ...]
    collection_count: int
    story_records: int
    queue_items: int
    queue_ids: tuple[str, ...]
    readiness: GenerationReadiness


@dataclass(frozen=True)
class ImmutableHistoryTimestamp:
    kind: str
    instant: str
    display: str


@dataclass(frozen=True)
class WorkspaceCollection:
    collection_id: str
    title: str
    kind: str
    record_count: int


@dataclass(frozen=True)
class WorkspaceVoice:
    character: str
    speaker: str
    aliases: tuple[str, ...]
    references: tuple[Path, ...]


@dataclass(frozen=True)
class WorkbenchProjectionData:
    """One internally consistent workbench refresh projection."""

    summary: WorkspaceSummary
    reviews: tuple[ReviewItem, ...]
    workspace: dict[str, object]
    collections: tuple[WorkspaceCollection, ...]
    collection_selection: CollectionSelection
    history: tuple[ImmutableHistoryTimestamp, ...]
    voices: tuple[WorkspaceVoice, ...]
    _voice_controls: tuple[tuple[Path, str], ...]

    def verify_voice_controls(self) -> None:
        """Fail if a voice reference changed after the projection was built."""
        for path, digest in self._voice_controls:
            _read_bound_bytes(path, digest, "Voice reference snapshot")


@dataclass(frozen=True)
class _WorkbenchProjectionRead:
    """Validated input objects shared only by one projection build."""

    directory: Path
    workspace: dict[str, object]
    workspace_sha256: str
    queue_path: Path
    queue: VoiceGenerationQueue
    output: Path
    state_path: Path | None
    state: dict[str, object] | None
    state_sha256: str | None
    story: object
    voices: tuple[WorkspaceVoice, ...]
    voice_controls: tuple[tuple[Path, str], ...]


def _read_bound_bytes(path: str | Path, expected_sha256: str, label: str) -> bytes:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise AuthoringWorkbenchError(f"{label} is missing or unsafe")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise AuthoringWorkbenchError(f"Unable to read {label}: {error}") from error
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise AuthoringWorkbenchError(f"{label} was modified")
    return payload


__all__ = [
    "ActiveAttempt",
    "AuthoringRuntimeStatus",
    "AuthoringWorkbenchError",
    "CollectionSelection",
    "GenerationReadiness",
    "ImmutableHistoryTimestamp",
    "ReviewItem",
    "WorkbenchProjectionData",
    "WorkspaceCollection",
    "WorkspaceCreationResult",
    "WorkspaceSummary",
    "WorkspaceVoice",
    "WORKSPACE_SCHEMA",
    "WORKSPACE_VERSION",
    "_OutcomeMergeBase",
    "_OutcomeMergeSource",
    "_OutcomeMergeSources",
    "_WorkbenchProjectionRead",
    "_read_bound_bytes",
]

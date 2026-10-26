"""Resumable device-independent generation from shared voice queues."""

from __future__ import annotations

import copy
import hashlib
import importlib
import io
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias, TypedDict, TypeGuard

import numpy as np
from numpy.typing import NDArray
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.text_utils import slugify
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueItem,
)
from vntts_artifacts.voice_manifest import (
    VoiceManifestEntry,
    VoiceManifestError,
    normalize_character_name,
)

from vntts.audio_store import SharedAudioStore, SharedAudioStoreError, link_shared_audio
from vntts.authoring.audio_events import (
    audio_event_plan_for_record,
    requires_audio_event_composition,
)
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.failure_repair import (
    BOUNDED_SEED_RETRY,
    EDGE_SILENCE_TRIM,
    INLINE_PAUSE_MARKER,
    MAX_BOUNDED_TOTAL_ATTEMPTS,
    OFFLINE_FALLBACK_BACKEND,
    SENTENCE_BOUNDARY_SEGMENTATION,
    FailureRepairPolicy,
    FailureRepairPolicyError,
    inline_sentence_pause_prompt,
    render_sentence_segments,
    safe_sentence_segments,
    trim_excess_edge_silence,
)
from vntts.authoring.generation_failures import (
    IncompleteSynthesisError as IncompleteSynthesisError,
)
from vntts.authoring.generation_failures import _CohortCount as _CohortCount
from vntts.authoring.generation_failures import (
    _FailureReportRecord as _FailureReportRecord,
)
from vntts.authoring.generation_failures import (
    _GenerationFailureReport as _GenerationFailureReport,
)
from vntts.authoring.generation_failures import edge_silence_only as _edge_silence_only
from vntts.authoring.generation_failures import (
    failure_kind,
    failure_repair_history,
)
from vntts.authoring.generation_failures import failure_record as _failure_record
from vntts.authoring.generation_failures import (
    generation_failure_repair_plan as generation_failure_repair_plan,
)
from vntts.authoring.generation_failures import (
    generation_failure_report as generation_failure_report,
)
from vntts.authoring.generation_failures import (
    inline_pause_matches_failure as inline_pause_matches_failure,
)
from vntts.authoring.generation_failures import (
    normalized_failure_record as normalized_failure_record,
)
from vntts.authoring.generation_failures import (
    sentence_repair_matches_failure as sentence_repair_matches_failure,
)
from vntts.authoring.generation_failures import silence_durations as _silence_durations
from vntts.authoring.generation_fallback import (
    AUTOMATIC_RECOVERY_LIVE_FALLBACK_ACTIONS as AUTOMATIC_RECOVERY_LIVE_FALLBACK_ACTIONS,
)
from vntts.authoring.generation_fallback import (
    authorize_live_fallback as authorize_live_fallback,
)
from vntts.authoring.generation_lease import LEASE_SCHEMA as LEASE_SCHEMA
from vntts.authoring.generation_lease import LEASE_VERSION as LEASE_VERSION
from vntts.authoring.generation_lease import (
    BulkGenerationError,
    GenerationLease,
    archive_interrupted_artifact,
    process_is_alive,
    process_started_at,
)
from vntts.authoring.generation_lease import (
    BulkGenerationSourceChangedError as BulkGenerationSourceChangedError,
)
from vntts.authoring.generation_manifest import (
    RUNTIME_PROGRESS_MANIFEST_NAME,
    approved_manifest_entries,
    inspect_generated_wav_with_samples,
    snapshot_recorded_voices,
    validate_success_file,
    write_generated_manifest_from_state,
    write_runtime_progress_manifest_from_state,
)
from vntts.authoring.generation_manifest import AudioQuality as AudioQuality
from vntts.authoring.generation_manifest import RecordedVoice as RecordedVoice
from vntts.authoring.generation_manifest import (
    inspect_generated_wav as inspect_generated_wav,
)
from vntts.authoring.generation_review import ReviewAuthority as ReviewAuthority
from vntts.authoring.generation_review import ReviewCommit as ReviewCommit
from vntts.authoring.generation_review import assert_review_authorities
from vntts.authoring.generation_review import (
    generation_review_authorities as generation_review_authorities,
)
from vntts.authoring.generation_review import (
    generation_review_authority as generation_review_authority,
)
from vntts.authoring.generation_review import (
    load_review_audio_bytes as load_review_audio_bytes,
)
from vntts.authoring.generation_review import (
    review_generation_cohort as review_generation_cohort,
)
from vntts.authoring.generation_review import (
    review_generation_item as review_generation_item,
)
from vntts.authoring.generation_state import (
    AUTOMATIC_RECOVERY_LIVE_FALLBACK_EVIDENCE_SCHEMA as AUTOMATIC_RECOVERY_LIVE_FALLBACK_EVIDENCE_SCHEMA,
)
from vntts.authoring.generation_state import (
    FAILURE_KINDS as FAILURE_KINDS,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED as LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_AUTOMATIC_RECOVERY_VERSION as LIVE_FALLBACK_AUTOMATIC_RECOVERY_VERSION,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_EVIDENCE_SCHEMA as LIVE_FALLBACK_EVIDENCE_SCHEMA,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_EVIDENCE_VERSION as LIVE_FALLBACK_EVIDENCE_VERSION,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_HYPOTHESES_EXHAUSTED as LIVE_FALLBACK_HYPOTHESES_EXHAUSTED,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_MISSING_VOICE_EVIDENCE_VERSION as LIVE_FALLBACK_MISSING_VOICE_EVIDENCE_VERSION,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_REASONS as LIVE_FALLBACK_REASONS,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_REVIEW_EVIDENCE_VERSION as LIVE_FALLBACK_REVIEW_EVIDENCE_VERSION,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_SCHEMA as LIVE_FALLBACK_SCHEMA,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_VERSION as LIVE_FALLBACK_VERSION,
)
from vntts.authoring.generation_state import (
    MISSING_VOICE_LIVE_FALLBACK_EVIDENCE_SCHEMA as MISSING_VOICE_LIVE_FALLBACK_EVIDENCE_SCHEMA,
)
from vntts.authoring.generation_state import (
    STATE_SCHEMA as STATE_SCHEMA,
)
from vntts.authoring.generation_state import (
    STATE_VERSION as STATE_VERSION,
)
from vntts.authoring.generation_state import StateItems as StateItems
from vntts.authoring.generation_state import (
    completed_generation_attempts,
    generation_item_status,
    generation_state_file,
    load_stable_generation_queue,
    read_generation_state_document,
    write_generation_state,
)
from vntts.authoring.generation_state import (
    contained_state_path as _within,
)
from vntts.authoring.generation_state import (
    control_directory_digest as _control_directory_digest,
)
from vntts.authoring.generation_state import (
    generation_state_items as _state_items,
)
from vntts.authoring.generation_state import (
    load_generation_state as load_generation_state,
)
from vntts.authoring.generation_state import (
    provider_attempts as _provider_attempts,
)
from vntts.authoring.generation_state import (
    required_state_text as _required_text,
)
from vntts.authoring.generation_state import (
    state_integer as _integer,
)
from vntts.authoring.generation_state import (
    state_nonnegative_int as _nonnegative_int,
)
from vntts.authoring.generation_state import (
    validate_generation_state_document as _validate_state_document,
)
from vntts.authoring.generation_state import (
    validate_generation_state_document as validate_generation_state_document,
)
from vntts.authoring.generation_state import (
    validate_success_item as _validate_success_item,
)
from vntts.authoring.missing_voice_policy import (
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.publication import no_replace_destination
from vntts.authoring.silence_evidence import (
    SilenceFailureEvidenceError,
    publish_silence_failure_evidence,
)
from vntts.authoring.source_reference_bindings import queue_voice_overrides_sha256
from vntts.authoring.speech_quality import (
    LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION as LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION,
)
from vntts.authoring.speech_quality import (
    MAX_INTERNAL_SILENCE_SECONDS as MAX_INTERNAL_SILENCE_SECONDS,
)
from vntts.authoring.speech_quality import (
    MAX_LEADING_SILENCE_SECONDS as MAX_LEADING_SILENCE_SECONDS,
)
from vntts.authoring.speech_quality import (
    MAX_SILENCE_RATIO as MAX_SILENCE_RATIO,
)
from vntts.authoring.speech_quality import (
    MAX_TRAILING_SILENCE_SECONDS as MAX_TRAILING_SILENCE_SECONDS,
)
from vntts.authoring.speech_quality import (
    NOTABLE_SILENCE_SPAN_SECONDS,
    SpeechSilenceValidationError,
    inspect_generated_speech_samples,
)
from vntts.authoring.speech_quality import (
    SILENCE_DBFS as SILENCE_DBFS,
)
from vntts.authoring.speech_quality import (
    SILENCE_FRAME_MS as SILENCE_FRAME_MS,
)
from vntts.authoring.speech_quality import (
    SPEECH_QUALITY_ANALYSIS_VERSION as SPEECH_QUALITY_ANALYSIS_VERSION,
)
from vntts.authoring.speech_quality import (
    SpeechPauseDiagnosis as SpeechPauseDiagnosis,
)
from vntts.authoring.speech_quality import (
    SpeechQuality as SpeechQuality,
)
from vntts.authoring.speech_quality import (
    SpeechSilenceSpan as SpeechSilenceSpan,
)
from vntts.authoring.speech_quality import (
    inspect_generated_speech as inspect_generated_speech,
)
from vntts.authoring.speech_quality import (
    measure_generated_speech as measure_generated_speech,
)
from vntts.authoring.speech_quality import (
    text_failure_features as _text_failure_features,
)
from vntts.authoring.terminal_conflict_records import (
    TerminalConflictRecordError,
    validate_terminal_conflict_state_binding,
)
from vntts.authoring.workspace_foundation import (
    load_json_object,
)
from vntts.document_identity import file_sha256, sha256_file
from vntts.generation_database import (
    measure_generation_phase,
    measure_generation_run,
    require_registered_output,
)
from vntts.generation_database import state_path as generation_state_path
from vntts.json_types import decode_json
from vntts.json_types import is_json_object as _is_json_document
from vntts.path_safety import read_regular_bytes
from vntts.reading_diagnostics import record_diagnostic_event
from vntts.speech_presentation import speech_runtime_label
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCancellation,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.synthesis import (
    normalize_short_trailing_ellipsis as normalize_short_trailing_ellipsis,
)
from vntts.voices import (
    pocket_tts_preset_voices,
    synthesis_character_for_line,
    voice_manifest_entries_at_path,
)

NO_PROMPT_SHA256 = hashlib.sha256(b"").hexdigest()
PURE_SOUND_EFFECT_PATTERN = re.compile(r'^\s*["“”]?\*[^*]+\*["“”]?[.!?]?\s*$')


class _GenerationControl(TypedDict):
    role: str
    path: Path
    sha256: str
    kind: str
    files: list[dict[str, str]]


JsonDocument: TypeAlias = dict[str, object]
GenerationRenderer: TypeAlias = Callable[[SynthesisRequest], SynthesisChunkStream]
PrefetchedRender: TypeAlias = tuple[str, SynthesisRequest, Future[SynthesisResult]]


class _CapturedSilenceFailure(TypedDict):
    wav_payload: bytes
    queue_id: str
    line_id: str
    text: str
    text_sha256: str
    state_item: JsonDocument


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_generation_renderer(value: object) -> TypeGuard[GenerationRenderer]:
    return callable(value)


def _is_text_transform(value: object) -> TypeGuard[Callable[[str], str]]:
    return callable(value)


def _generation_text(value: object, label: str) -> str:
    parsed = _required_text(value, label)
    if not isinstance(parsed, str):
        raise BulkGenerationError(f"{label} must be non-empty text")
    return parsed


def _generation_integer(value: object, label: str) -> int:
    parsed = _integer(value, label)
    if not isinstance(parsed, int) or isinstance(parsed, bool):
        raise BulkGenerationError(f"{label} must be an integer")
    return parsed


class BulkGenerationProvenanceError(BulkGenerationError):
    """Typed render diagnostics contradict declared synthesis provenance."""


@dataclass(frozen=True)
class BulkGenerationResult:
    generated: int
    failed: int
    skipped_existing: int
    skipped_actions: int
    skipped_characters: int
    skipped_items: int
    cancelled: bool
    state: Path
    manifest: Path

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["state"] = str(self.state)
        payload["manifest"] = str(self.manifest)
        return payload


@dataclass(frozen=True)
class _GenerationExecutionResult:
    generated: int
    skipped_existing: int
    cancelled: bool
    captured_silence_failure: _CapturedSilenceFailure | None


@dataclass(frozen=True)
class _GenerationItemExecutionResult:
    generated: bool
    cancelled: bool
    captured_silence_failure: _CapturedSilenceFailure | None
    prefetched_render: PrefetchedRender | None


@dataclass(frozen=True)
class _GenerationFailureResult:
    last_error: str
    cancelled: bool
    captured_silence_failure: _CapturedSilenceFailure | None


@dataclass
class _GenerationAttempt:
    attempts: int
    provider_attempts: int
    attempts_by_provider: dict[str, int]
    run_attempts: int
    attempt_seed: int
    request_seed: int | None
    attempt_repair: JsonDocument | None
    partial: Path
    request: SynthesisRequest
    rendered: SynthesisResult | None = None


@dataclass(frozen=True)
class GenerationProgressItem:
    queue_id: str
    status: str | None
    line_id: str | None


@dataclass(frozen=True)
class GenerationProgressEvent:
    """Detached progress data; reset replaces all prior terminal projections."""

    queue_sha256: str
    active: object
    items: tuple[GenerationProgressItem, ...] = ()
    reset: bool = False


def _generation_progress_event(
    state: JsonDocument,
    items: StateItems,
    changed_items: tuple[str, ...] | None,
) -> GenerationProgressEvent:
    projected = []
    for queue_id in items if changed_items is None else changed_items:
        item = items.get(queue_id, {})
        status = generation_item_status(item)
        line_id = item.get("line_id")
        projected.append(
            GenerationProgressItem(
                queue_id,
                status if isinstance(status, str) else None,
                line_id if isinstance(line_id, str) and line_id else None,
            )
        )
    queue_sha256 = state["queue_sha256"]
    assert isinstance(queue_sha256, str)
    return GenerationProgressEvent(
        queue_sha256,
        copy.deepcopy(state.get("active")),
        tuple(projected),
        reset=changed_items is None,
    )


@dataclass(frozen=True)
class _GenerationExecutionContext:
    state_path: Path
    state: JsonDocument
    _items: StateItems
    output_directory: Path
    output_argument: Path
    audio_store: SharedAudioStore
    workspace_output_identity: object
    candidates: Sequence[VoiceGenerationQueueItem]
    queue_voice_overrides: dict[str, str]
    character_overrides: dict[str, str]
    repair_policy: FailureRepairPolicy
    policy: MissingVoicePolicy
    narrator_character: str | None
    synthesis_configuration: JsonDocument
    text_transform: Callable[[str], str] | None
    text_transform_id: str | None
    provider: str
    model: str
    generation_profile: str
    provenance_sha256: str
    backend: object
    render: GenerationRenderer
    controls: Sequence[_GenerationControl]
    lease: GenerationLease
    render_prefetch: ThreadPoolExecutor
    seed: int
    retries: int
    cancellation: SynthesisCancellation
    synthesis_cache_policy: SynthesisCachePolicy
    recorded_voices: dict[str, RecordedVoice]
    evidence_directory: Path | None
    regenerate_existing: bool
    approve_validated_audio: bool
    progress_callback: Callable[[JsonDocument], None] | None
    progress_observer: Callable[[GenerationProgressEvent], None] | None

    @property
    def items(self) -> StateItems:
        # Callbacks can retain, mutate or replace the live collection.
        return (
            self._items if self.progress_callback is None else _state_items(self.state)
        )


@dataclass(frozen=True)
class _GenerationConfiguration:
    limit: int | None
    retries: int
    seed: int
    provider: str
    model: str
    generation_profile: str
    synthesis_cache_policy: SynthesisCachePolicy
    policy: MissingVoicePolicy
    character_overrides: dict[str, str]
    projection_queue_ids: tuple[str, ...]
    repair_policy: FailureRepairPolicy
    text_transform: Callable[[str], str] | None
    text_transform_id: str | None
    render: GenerationRenderer


@dataclass(frozen=True)
class _GenerationInputs:
    queue_path: Path
    output_argument: Path
    output_directory: Path
    queue: VoiceGenerationQueue
    queue_sha256: str
    queue_voice_overrides: dict[str, str]
    selected_queue_ids: set[str] | None
    evidence_directory: Path | None


@dataclass
class _PreparedGenerationItem:
    item: VoiceGenerationQueueItem
    existing: JsonDocument
    queue_id: str
    repair_strategy: str | None
    repair_document: JsonDocument | None
    requested_voice: str
    voice: str
    synthesis_fallback: JsonDocument | None
    source_reference_binding: JsonDocument | None
    queue_annotations_sha256: str
    prompt_sha256: str
    synthesis_text: str
    synthesis_text_sha256: str
    relative: Path
    destination: Path
    attempts: int
    attempts_by_provider: dict[str, int]
    provider_attempts: int
    attempt_limit: int
    last_error: str | None


def sha256_control_path(path: str | Path) -> str:
    """Hash one immutable synthesis control file or complete directory tree."""
    try:
        path = Path(path).expanduser().resolve()
        if path.is_file():
            return file_sha256(path, error_type=OSError)
        if not path.is_dir():
            raise BulkGenerationError(f"Generation control does not exist: {path}")
        digest = hashlib.sha256()
        for candidate in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(path).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(bytes.fromhex(file_sha256(candidate, error_type=OSError)))
        return digest.hexdigest()
    except BulkGenerationError:
        raise
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read generation control {path}: {error}"
        ) from error


def is_spoken_queue_item(item: VoiceGenerationQueueItem | JsonDocument) -> bool:
    """Skip pure or inline audio events until a typed composition is approved."""
    document: object = item.document if hasattr(item, "document") else item
    if not _is_json_document(document):
        raise BulkGenerationError("Queue item document must be an object")
    if document.get("speakable") is False:
        return False
    text = str(document.get("text") or "")
    try:
        requires_composition = requires_audio_event_composition(document)
    except ValueError as error:
        raise BulkGenerationError(str(error)) from error
    return (
        not requires_composition and PURE_SOUND_EFFECT_PATTERN.fullmatch(text) is None
    )


def audio_event_spoken_projection(text: str) -> str:
    """Remove typed inline events while preserving the record's spoken text."""
    try:
        plan = audio_event_plan_for_record({"text": text})
    except ValueError as error:
        raise BulkGenerationError(str(error)) from error
    spoken_text = plan.get("spoken_text") if isinstance(plan, dict) else None
    if (
        not isinstance(plan, dict)
        or not plan.get("requires_composition")
        or not isinstance(spoken_text, str)
        or not spoken_text.strip()
        or spoken_text == text
    ):
        raise BulkGenerationError(
            "Audio-event spoken projection requires mixed speech and events"
        )
    if not isinstance(spoken_text, str):
        raise BulkGenerationError(
            "Audio-event spoken projection requires mixed speech and events"
        )
    return spoken_text


def _generation_voice_overrides(
    policy_document: object,
    synthesis_character_overrides: object,
    *,
    narrator_character: str | None,
) -> tuple[MissingVoicePolicy, dict[str, str]]:
    policy = _missing_voice_policy(policy_document)
    overrides = (
        {} if synthesis_character_overrides is None else synthesis_character_overrides
    )
    if not isinstance(overrides, dict):
        raise BulkGenerationError("Synthesis character overrides must be an object")
    normalized = _normalized_narrator_overrides(overrides, policy)
    if normalized or narrator_character is not None:
        _required_text(narrator_character, "Narrator character")
    return policy, normalized


def _missing_voice_policy(policy_document: object) -> MissingVoicePolicy:
    try:
        return (
            policy_document
            if isinstance(policy_document, MissingVoicePolicy)
            else MissingVoicePolicy.from_document(policy_document)
        )
    except MissingVoicePolicyError as error:
        raise BulkGenerationError(str(error)) from error


def _normalized_narrator_overrides(
    overrides: Mapping[object, object], policy: MissingVoicePolicy
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    source_names: dict[str, str] = {}
    for requested, effective in overrides.items():
        requested = _required_text(requested, "Requested synthesis character")
        effective = _required_text(effective, "Effective synthesis character")
        key = normalize_character_name(requested)
        if not key or key == "narrator":
            raise BulkGenerationError(
                "Narrator fallback overrides require a named non-Narrator role"
            )
        if normalize_character_name(effective) != "narrator":
            raise BulkGenerationError(
                "Missing-voice synthesis overrides may target only Narrator"
            )
        previous = source_names.get(key)
        if previous is not None and previous != requested:
            raise BulkGenerationError(
                "Synthesis override roles collide after normalization: "
                f"{previous!r}, {requested!r}"
            )
        if not policy.applies_to(requested):
            raise BulkGenerationError(
                f"Missing-voice policy does not authorize role {requested!r}"
            )
        source_names[key] = requested
        normalized[key] = "Narrator"
    return normalized


def _validated_queue_voice_overrides(
    value: object, queue: VoiceGenerationQueue
) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BulkGenerationError("Queue voice overrides must be an object")
    known = {item.queue_id for item in queue.items}
    parsed: dict[str, str] = {}
    for queue_id, character in value.items():
        queue_id = _required_text(queue_id, "Queue voice override ID")
        character = _required_text(character, f"Queue voice override {queue_id!r}")
        if queue_id not in known:
            raise BulkGenerationError(
                f"Queue voice override is absent from the bound queue: {queue_id}"
            )
        if normalize_character_name(character) == "narrator":
            raise BulkGenerationError(
                "Narrator routing must use the explicit missing-voice policy"
            )
        parsed[queue_id] = character
    return dict(sorted(parsed.items()))


def _synthesis_fallback_document(
    requested_voice: str,
    effective_voice: str,
    *,
    policy: MissingVoicePolicy,
    narrator_character: str | None,
) -> JsonDocument | None:
    if normalize_character_name(requested_voice) == normalize_character_name(
        effective_voice
    ):
        return None
    if normalize_character_name(effective_voice) != "narrator":
        raise BulkGenerationError("Only Narrator synthesis fallback is supported")
    if not policy.applies_to(requested_voice):
        raise BulkGenerationError(
            f"Missing-voice policy does not authorize role {requested_voice!r}"
        )
    return {
        "schema_version": 1,
        "kind": "missing_voice_to_narrator",
        "policy": policy.to_document(),
        "source_voice_character": requested_voice,
        "synthesis_voice_character": "Narrator",
        "narrator_character": _required_text(narrator_character, "Narrator character"),
    }


def _assert_missing_voice_overrides_match_manifest(
    controls: Sequence[_GenerationControl],
    character_overrides: Mapping[str, str],
    narrator_character: str | None,
    provider: str,
) -> None:
    if not character_overrides:
        return
    manifest_control = next(
        (value for value in controls if value["role"] == "voice_manifest"), None
    )
    if manifest_control is None or manifest_control["kind"] != "file":
        raise BulkGenerationError(
            "Narrator fallback requires a bound voice_manifest control"
        )
    entries = _fallback_manifest_entries(manifest_control)
    indexed = _voice_manifest_entries_by_name(entries)
    _assert_fallback_roles_have_no_references(character_overrides, indexed)
    _assert_fallback_narrator_has_reference(narrator_character, provider, indexed)


def _fallback_manifest_entries(
    control: _GenerationControl,
) -> Sequence[VoiceManifestEntry]:
    path = control["path"]
    try:
        payload = read_regular_bytes(path)
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read fallback voice manifest: {error}"
        ) from error
    if hashlib.sha256(payload).hexdigest() != control["sha256"]:
        raise BulkGenerationSourceChangedError(
            "Voice manifest changed while fallback roles were validated"
        )
    try:
        document = decode_json(payload.decode("utf-8"))
        return voice_manifest_entries_at_path(document, path)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
    ) as error:
        raise BulkGenerationError(str(error)) from error


def _voice_manifest_entries_by_name(
    entries: Sequence[VoiceManifestEntry],
) -> dict[str, VoiceManifestEntry]:
    indexed = {}
    for entry in entries:
        for name in (entry.character, entry.speaker, *entry.aliases):
            key = normalize_character_name(name)
            if key:
                indexed[key] = entry
    return indexed


def _assert_fallback_roles_have_no_references(
    character_overrides: Mapping[str, str], indexed: Mapping[str, VoiceManifestEntry]
) -> None:
    for requested in character_overrides:
        entry = indexed.get(requested)
        if entry is not None and entry.references:
            raise BulkGenerationError(
                f"Narrator fallback role {requested!r} still has configured references"
            )


def _assert_fallback_narrator_has_reference(
    narrator_character: str | None,
    provider: str,
    indexed: Mapping[str, VoiceManifestEntry],
) -> None:
    narrator = indexed.get(normalize_character_name(narrator_character))
    if narrator is None or not (
        narrator.references
        or provider == "pocket-tts"
        and narrator.speaker in pocket_tts_preset_voices
    ):
        raise BulkGenerationError(
            f"Narrator character {narrator_character!r} has no configured reference"
        )


def _validate_failure_repair_selection(
    policy: FailureRepairPolicy,
    selected_queue_ids: set[str] | None,
    state: JsonDocument,
    queue: VoiceGenerationQueue,
    *,
    provider: str,
) -> None:
    if policy.is_empty:
        return
    expected = set(policy.queue_ids)
    if selected_queue_ids != expected:
        raise BulkGenerationError("Failure-repair selection changed unexpectedly")
    queue_by_id = {item.queue_id: item for item in queue.items}
    for queue_id in policy.queue_ids:
        _validate_failure_repair_item(
            _generation_text(policy.strategy_for(queue_id), "Failure-repair strategy"),
            queue_id,
            _failed_repair_result(state, queue_id),
            queue_by_id[queue_id],
            provider,
        )


def _failed_repair_result(state: JsonDocument, queue_id: str) -> JsonDocument:
    result = _state_items(state).get(queue_id)
    if result is None or result.get("status") != "failed":
        raise BulkGenerationError(
            f"Failure repair requires a current failed outcome for {queue_id!r}"
        )
    if not isinstance(result.get("failure"), dict):
        raise BulkGenerationError(
            f"Failure repair requires typed failure provenance for {queue_id!r}"
        )
    return result


def _validate_failure_repair_item(
    strategy: str,
    queue_id: str,
    result: JsonDocument,
    item: VoiceGenerationQueueItem,
    provider: str,
) -> None:
    failure = normalized_failure_record(result, text=item.text)
    previous_repair = result.get("failure_repair")
    if generation_item_status(result) == "cancelled":
        if (
            not isinstance(previous_repair, dict)
            or previous_repair.get("strategy") != strategy
            or result.get("provider") != provider
            or _provider_repair_attempts(result, queue_id, provider)
            >= (
                1
                if strategy == OFFLINE_FALLBACK_BACKEND
                else MAX_BOUNDED_TOTAL_ATTEMPTS
            )
        ):
            raise BulkGenerationError(
                f"Cancelled repair cannot resume under changed or exhausted controls for {queue_id!r}"
            )
        return
    validators = {
        SENTENCE_BOUNDARY_SEGMENTATION: _validate_sentence_repair,
        EDGE_SILENCE_TRIM: _validate_edge_silence_repair,
        BOUNDED_SEED_RETRY: _validate_bounded_seed_repair,
        OFFLINE_FALLBACK_BACKEND: _validate_offline_fallback_repair,
        INLINE_PAUSE_MARKER: _validate_inline_pause_repair,
    }
    validator = validators.get(strategy)
    if validator is not None:
        validator(queue_id, result, item, failure, provider)


def _validate_sentence_repair(
    queue_id: str,
    result: JsonDocument,
    item: VoiceGenerationQueueItem,
    failure: JsonDocument,
    _provider: str,
) -> None:
    if SENTENCE_BOUNDARY_SEGMENTATION in failure_repair_history(result):
        raise BulkGenerationError(f"Sentence repair already failed for {queue_id!r}")
    if not _sentence_repair_matches_failure(failure, item.text):
        raise BulkGenerationError(
            f"Sentence repair no longer matches failure {queue_id!r}"
        )


def _validate_edge_silence_repair(
    queue_id: str,
    _result: JsonDocument,
    _item: VoiceGenerationQueueItem,
    failure: JsonDocument,
    _provider: str,
) -> None:
    quality = failure.get("speech_quality")
    if not isinstance(quality, dict):
        raise BulkGenerationError(
            f"Edge-silence repair requires typed speech metrics for {queue_id!r}"
        )
    if _silence_durations(failure) is None:
        raise BulkGenerationError(
            f"Edge-silence repair metrics are invalid for {queue_id!r}"
        )
    if not _edge_silence_only(failure):
        raise BulkGenerationError(
            f"Edge-silence repair no longer matches failure {queue_id!r}"
        )


def _provider_repair_attempts(
    result: JsonDocument,
    queue_id: str,
    provider: object,
    *,
    default_provider: object | None = None,
) -> int:
    attempts = _nonnegative_int(
        result.get("attempts", 0), f"State item {queue_id!r} attempts"
    )
    provider_key = _generation_text(provider, f"State item {queue_id!r} provider")
    return _provider_attempts(
        result,
        attempts,
        default_provider=provider if default_provider is None else default_provider,
    ).get(provider_key, 0)


def _validate_bounded_seed_repair(
    queue_id: str,
    result: JsonDocument,
    _item: VoiceGenerationQueueItem,
    failure: JsonDocument,
    provider: str,
) -> None:
    if (
        failure.get("kind") != "missed_eos_audio_limit"
        or result.get("provider") != provider
        or _provider_repair_attempts(result, queue_id, provider)
        >= MAX_BOUNDED_TOTAL_ATTEMPTS
    ):
        raise BulkGenerationError(
            f"Bounded seed repair no longer matches failure {queue_id!r}"
        )


def _validate_offline_fallback_repair(
    queue_id: str,
    result: JsonDocument,
    _item: VoiceGenerationQueueItem,
    _failure: JsonDocument,
    provider: str,
) -> None:
    carry = _offline_fallback_source(result)
    if (
        not isinstance(carry, dict)
        or carry.get("mode") != "failed-outcome"
        or carry.get("source_provider") == provider
        or _provider_repair_attempts(
            result,
            queue_id,
            provider,
            default_provider=result.get("provider"),
        )
        >= 1
    ):
        raise BulkGenerationError(
            "Offline fallback lacks a different bound source backend or its single attempt is exhausted for "
            f"{queue_id!r}"
        )


def _validate_inline_pause_repair(
    queue_id: str,
    result: JsonDocument,
    item: VoiceGenerationQueueItem,
    failure: JsonDocument,
    provider: str,
) -> None:
    if (
        not _inline_pause_matches_failure(failure, item.text)
        or result.get("provider") != provider
        or _provider_repair_attempts(result, queue_id, provider)
        >= MAX_BOUNDED_TOTAL_ATTEMPTS
    ):
        raise BulkGenerationError(
            f"Inline pause repair no longer matches failure {queue_id!r}"
        )


_failure_kind = failure_kind
_failure_repair_history = failure_repair_history
_sentence_repair_matches_failure = sentence_repair_matches_failure


_inline_pause_matches_failure = inline_pause_matches_failure


def _failure_repair_document(
    policy: FailureRepairPolicy,
    queue_id: str,
    text: str,
    *,
    existing: JsonDocument | None = None,
) -> JsonDocument | None:
    strategy = policy.strategy_for(queue_id)
    if strategy is None:
        return None
    document = {"schema_version": 1, "strategy": strategy}
    if strategy == SENTENCE_BOUNDARY_SEGMENTATION:
        segments = safe_sentence_segments(text)
        document.update(
            {
                "segments": list(segments),
                "segment_text_sha256": [
                    hashlib.sha256(value.encode("utf-8")).hexdigest()
                    for value in segments
                ],
                "pause_ms": policy.segment_pause_ms,
            }
        )
    elif strategy == INLINE_PAUSE_MARKER:
        prompt, marker_count = inline_sentence_pause_prompt(
            text, pause_ms=policy.inline_pause_ms
        )
        document.update(
            {
                "source_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "derived_prompt_sha256": hashlib.sha256(
                    prompt.encode("utf-8")
                ).hexdigest(),
                "pause_ms": policy.inline_pause_ms,
                "marker_count": marker_count,
            }
        )
    elif strategy == OFFLINE_FALLBACK_BACKEND:
        carry = _offline_fallback_source(existing)
        if not isinstance(carry, dict) or carry.get("mode") != "failed-outcome":
            raise BulkGenerationError(
                f"Offline fallback lacks source failure provenance for {queue_id!r}"
            )
        document["source_failure"] = copy.deepcopy(carry)
    return document


def _offline_fallback_source(result: object) -> JsonDocument | None:
    if not _is_json_document(result):
        return None
    carry = result.get("carry_forward")
    if _is_json_document(carry) and carry.get("mode") == "failed-outcome":
        return carry
    repair = result.get("failure_repair")
    source_failure = repair.get("source_failure") if _is_json_document(repair) else None
    if (
        _is_json_document(repair)
        and repair.get("strategy") == OFFLINE_FALLBACK_BACKEND
        and _is_json_document(source_failure)
    ):
        return source_failure
    return None


def _select_generation_candidates(
    queue: VoiceGenerationQueue,
    state: JsonDocument,
    *,
    include_prefer_source: bool,
    include_characters: Sequence[str] | None,
    selected_queue_ids: set[str] | None,
    item_filter: Callable[[VoiceGenerationQueueItem], bool] | None,
    limit: int | None,
    regenerate_existing: bool,
) -> tuple[list[VoiceGenerationQueueItem], int, int, int]:
    eligible_actions = {"generate"}
    if include_prefer_source:
        eligible_actions.add("prefer_source_audio")
    candidates = [item for item in queue.items if item.action in eligible_actions]
    skipped_actions = len(queue.items) - len(candidates)

    skipped_characters = 0
    if include_characters is not None:
        character_filter = set(include_characters)
        filtered = [
            item
            for item in candidates
            if synthesis_character_for_line(item.speaker, item.voice_character)
            in character_filter
        ]
        skipped_characters = len(candidates) - len(filtered)
        candidates = filtered

    skipped_items = 0
    if selected_queue_ids is not None:
        filtered = [item for item in candidates if item.queue_id in selected_queue_ids]
        skipped_items += len(candidates) - len(filtered)
        candidates = filtered
    if item_filter is not None:
        filtered = [item for item in candidates if item_filter(item)]
        skipped_items += len(candidates) - len(filtered)
        candidates = filtered
    if limit is not None:
        candidates = candidates[:limit]

    if regenerate_existing:
        items = _state_items(state)
        protected = [
            item.queue_id
            for item in candidates
            if items.get(item.queue_id, {}).get("status")
            in {"generated", "approved", "live_fallback"}
            and items[item.queue_id].get("review_status") != "pending_review"
        ]
        if protected:
            raise BulkGenerationError(
                "Regeneration cannot overwrite an approved or rejected decision: "
                + ", ".join(protected)
            )

    return candidates, skipped_actions, skipped_characters, skipped_items


def _skip_existing_generation_item(
    run: _GenerationExecutionContext,
    item: VoiceGenerationQueueItem,
    existing: JsonDocument,
) -> bool:
    queue_id = item.queue_id
    if existing.get("status") == "live_fallback":
        if run.regenerate_existing:
            raise BulkGenerationError(
                "Regeneration cannot overwrite a terminal live fallback "
                f"decision for {queue_id!r}"
            )
        return True
    if existing.get("status") in {"generated", "approved"}:
        _validate_success_item(
            queue_id,
            existing,
            run.output_directory,
            item,
        )
        if not run.regenerate_existing:
            source = run.output_directory / str(existing["path"])
            stored = run.audio_store.put(
                "wav-" + str(existing["file_sha256"]),
                source,
                expected_sha256=str(existing["file_sha256"]),
            )
            link_shared_audio(stored, source, str(existing["file_sha256"]))
            return True
        if existing.get("review_status") != "pending_review":
            raise BulkGenerationError(
                "Regeneration cannot overwrite an approved or rejected "
                f"decision for {queue_id!r}"
            )
    return False


def _generation_item_voice(
    run: _GenerationExecutionContext, item: VoiceGenerationQueueItem
) -> tuple[str, str, JsonDocument | None, JsonDocument | None]:
    queue_id = item.queue_id
    requested_voice = _required_text(
        synthesis_character_for_line(item.speaker, item.voice_character),
        f"Queue item {queue_id!r} voice",
    )
    voice = run.queue_voice_overrides.get(
        queue_id,
        run.character_overrides.get(
            normalize_character_name(requested_voice), requested_voice
        ),
    )
    synthesis_fallback = (
        None
        if queue_id in run.queue_voice_overrides
        else _synthesis_fallback_document(
            requested_voice,
            voice,
            policy=run.policy,
            narrator_character=run.narrator_character,
        )
    )
    source_reference_binding: JsonDocument | None = (
        {
            "schema_version": 1,
            "queue_id": queue_id,
            "source_voice_character": requested_voice,
            "synthesis_voice_character": voice,
            "queue_voice_overrides_sha256": run.synthesis_configuration[
                "queue_voice_overrides_sha256"
            ],
        }
        if queue_id in run.queue_voice_overrides
        else None
    )
    return requested_voice, voice, synthesis_fallback, source_reference_binding


def _generation_item_text(
    run: _GenerationExecutionContext,
    item: VoiceGenerationQueueItem,
    repair_strategy: str | None,
    repair_document: JsonDocument | None,
) -> str:
    queue_id = item.queue_id
    synthesis_text = (
        item.text if run.text_transform is None else run.text_transform(item.text)
    )
    if not isinstance(synthesis_text, str) or not synthesis_text.strip():
        raise BulkGenerationError(
            f"Text transform returned no speech for queue item {queue_id!r}"
        )
    if (
        repair_strategy == SENTENCE_BOUNDARY_SEGMENTATION
        and repair_document is not None
        and list(safe_sentence_segments(synthesis_text))
        != repair_document.get("segments")
    ):
        raise BulkGenerationError(
            "Sentence repair text transform changed authorized segments for "
            f"{queue_id!r}"
        )
    if repair_strategy == INLINE_PAUSE_MARKER and repair_document is not None:
        if synthesis_text != item.text:
            raise BulkGenerationError(
                "Inline pause comparison requires unchanged source text before "
                f"marker insertion for {queue_id!r}"
            )
        synthesis_text, marker_count = inline_sentence_pause_prompt(
            synthesis_text, pause_ms=run.repair_policy.inline_pause_ms
        )
        if (
            repair_document.get("marker_count") != marker_count
            or repair_document.get("derived_prompt_sha256")
            != hashlib.sha256(synthesis_text.encode("utf-8")).hexdigest()
        ):
            raise BulkGenerationError(
                f"Inline pause provenance changed for {queue_id!r}"
            )
    return synthesis_text


def _generation_item_attempts(
    run: _GenerationExecutionContext,
    existing: JsonDocument,
    repair_strategy: str | None,
) -> tuple[int, dict[str, int], int, int]:
    attempts = _nonnegative_int(existing.get("attempts", 0), "Attempts")
    default_attempt_provider = run.provider
    if (
        run.regenerate_existing
        and existing.get("status") == "failed"
        and not existing.get("provider")
        and not existing.get("synthesis_provenance_sha256")
    ):
        default_attempt_provider = "legacy-unbound"
    attempts_by_provider = _provider_attempts(
        existing, attempts, default_provider=default_attempt_provider
    )
    provider_attempts = attempts_by_provider.get(run.provider, 0)
    attempt_limit = run.retries + 1
    if repair_strategy in {BOUNDED_SEED_RETRY, INLINE_PAUSE_MARKER}:
        attempt_limit = min(
            attempt_limit,
            MAX_BOUNDED_TOTAL_ATTEMPTS - provider_attempts,
        )
    return (
        completed_generation_attempts(existing),
        attempts_by_provider,
        provider_attempts,
        attempt_limit,
    )


def _prepare_generation_item(
    run: _GenerationExecutionContext, item: VoiceGenerationQueueItem
) -> _PreparedGenerationItem | None:
    queue_id = item.queue_id
    existing = run.items.get(queue_id, {})
    repair_strategy = run.repair_policy.strategy_for(queue_id)
    repair_document = _failure_repair_document(
        run.repair_policy, queue_id, item.text, existing=existing
    )
    if _skip_existing_generation_item(run, item, existing):
        return None
    (
        requested_voice,
        voice,
        synthesis_fallback,
        source_reference_binding,
    ) = _generation_item_voice(run, item)
    synthesis_text = _generation_item_text(run, item, repair_strategy, repair_document)
    relative = _audio_relative_path(voice, queue_id)
    if run.workspace_output_identity is not None:
        _assert_workspace_output_identity(
            run.output_argument, run.workspace_output_identity
        )
    destination = _within(run.output_directory, relative, "Generated WAV")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        _archive_interrupted_artifact(run.output_directory, destination)
    attempts, attempts_by_provider, provider_attempts, attempt_limit = (
        _generation_item_attempts(run, existing, repair_strategy)
    )
    return _PreparedGenerationItem(
        item=item,
        existing=existing,
        queue_id=queue_id,
        repair_strategy=repair_strategy,
        repair_document=repair_document,
        requested_voice=requested_voice,
        voice=voice,
        synthesis_fallback=synthesis_fallback,
        source_reference_binding=source_reference_binding,
        queue_annotations_sha256=_canonical_sha256(
            item.document.get("prompt_adapters") or {}
        ),
        prompt_sha256=NO_PROMPT_SHA256,
        synthesis_text=synthesis_text,
        synthesis_text_sha256=hashlib.sha256(
            synthesis_text.encode("utf-8")
        ).hexdigest(),
        relative=relative,
        destination=destination,
        attempts=attempts,
        attempts_by_provider=attempts_by_provider,
        provider_attempts=provider_attempts,
        attempt_limit=attempt_limit,
        last_error=str(existing.get("last_error") or "") or None,
    )


def _next_generation_request(
    run: _GenerationExecutionContext, candidate: VoiceGenerationQueueItem
) -> SynthesisRequest | None:
    existing = run.items.get(candidate.queue_id, {})
    if existing.get("status") in {"generated", "approved", "live_fallback"}:
        return None
    requested = _required_text(
        synthesis_character_for_line(candidate.speaker, candidate.voice_character),
        f"Queue item {candidate.queue_id!r} voice",
    )
    voice = run.queue_voice_overrides.get(
        candidate.queue_id,
        run.character_overrides.get(normalize_character_name(requested), requested),
    )
    attempts = _nonnegative_int(existing.get("attempts", 0), "Attempts")
    provider_attempts = _provider_attempts(
        existing, attempts, default_provider=run.provider
    ).get(run.provider, 0)
    return SynthesisRequest(
        voice=voice,
        text=candidate.text,
        seed=run.seed + provider_attempts,
        generation_profile=run.generation_profile,
        cancellation=run.cancellation,
        cache_policy=run.synthesis_cache_policy,
    )


def _store_successful_generation_attempt(
    run: _GenerationExecutionContext,
    plan: _PreparedGenerationItem,
    *,
    attempts: int,
    attempts_by_provider: Mapping[str, int],
    attempt_seed: int,
    request_seed: int | None,
    attempt_repair: JsonDocument | None,
    file_sha256: str,
    quality: AudioQuality,
    speech_quality: SpeechQuality,
) -> None:
    item = plan.item
    value: JsonDocument = {
        "status": "approved" if run.approve_validated_audio else "generated",
        "review_status": "approved"
        if run.approve_validated_audio
        else "pending_review",
        "attempts": attempts,
        "attempts_by_provider": dict(sorted(attempts_by_provider.items())),
        "path": plan.relative.as_posix(),
        "line_id": item.line_id,
        "text_sha256": item.text_sha256,
        "file_sha256": file_sha256,
        "provider": run.provider,
        "model": run.model,
        "prompt_sha256": plan.prompt_sha256,
        "prompt_applied": False,
        "queue_annotations_sha256": plan.queue_annotations_sha256,
        "synthesis_text_sha256": plan.synthesis_text_sha256,
        "text_transform": run.text_transform_id,
        "synthesis_provenance_sha256": run.provenance_sha256,
        "synthesis_configuration": run.synthesis_configuration,
        "seed": attempt_seed,
        "seed_applied": request_seed is not None,
        "generation_profile": run.generation_profile,
        "speaker": item.speaker,
        "requested_voice_character": plan.requested_voice,
        "voice_character": plan.voice,
        "quality": asdict(quality),
        "speech_quality": asdict(speech_quality),
        "updated_at": _now(),
    }
    recorded_voice = run.recorded_voices.get(normalize_character_name(plan.voice))
    if recorded_voice is not None:
        value["vntts.recorded_voice"] = {
            "schema_version": 1,
            **recorded_voice,
            "audio_sha256": file_sha256,
            "synthesis_provenance_sha256": run.provenance_sha256,
            "provider": run.provider,
            "model": run.model,
            "voice_character": plan.voice,
        }
    if plan.synthesis_fallback is not None:
        value["synthesis_fallback"] = plan.synthesis_fallback
        value["narrator_character"] = run.narrator_character
    if plan.source_reference_binding is not None:
        value["source_reference_binding"] = plan.source_reference_binding
    if attempt_repair is not None:
        value["failure_repair"] = attempt_repair
    carry_forward = plan.existing.get("carry_forward")
    if (
        plan.repair_strategy
        in {
            SENTENCE_BOUNDARY_SEGMENTATION,
            INLINE_PAUSE_MARKER,
            BOUNDED_SEED_RETRY,
        }
        and _is_json_document(carry_forward)
        and carry_forward.get("mode") == "failed-outcome"
    ):
        value["carry_forward"] = copy.deepcopy(carry_forward)
    run.items[plan.queue_id] = value
    run.state["active"] = None
    run.lease.assert_owned()
    write_generation_state(
        run.state_path,
        run.state,
        changed_items=(plan.queue_id,),
        controls_unchanged=run.progress_callback is None,
    )


def _store_failed_generation_attempt(
    run: _GenerationExecutionContext,
    plan: _PreparedGenerationItem,
    *,
    error: Exception,
    completion: SynthesisCompletion | None,
    request: SynthesisRequest,
    partial: Path,
    attempts: int,
    attempts_by_provider: Mapping[str, int],
    attempt_seed: int,
    request_seed: int | None,
    attempt_repair: JsonDocument | None,
    run_attempts: int,
) -> _GenerationFailureResult:
    captured_partial: bytes | None = None
    carry_forward = plan.existing.get("carry_forward")
    if (
        run.evidence_directory is not None
        and isinstance(error, SpeechSilenceValidationError)
        and partial.is_file()
        and not partial.is_symlink()
    ):
        captured_partial = read_regular_bytes(partial)
    if partial.exists():
        partial.unlink()
    is_cancelled = (
        completion is SynthesisCompletion.CANCELLED or request.cancellation_requested()
    )
    last_error = str(error) or error.__class__.__name__
    if is_cancelled:
        attempts -= 1
        attempts_by_provider = dict(attempts_by_provider)
        attempts_by_provider[run.provider] -= 1
    value: JsonDocument = {
        "status": "failed",
        "attempts": attempts,
        "attempts_by_provider": dict(sorted(attempts_by_provider.items())),
        "seed": attempt_seed,
        "seed_applied": request_seed is not None,
        "last_error": last_error,
        "failure": _failure_record(
            error,
            text=plan.synthesis_text,
            completion=(SynthesisCompletion.CANCELLED if is_cancelled else completion),
            attempt_binding={
                "provider": run.provider,
                "model": run.model,
                "generation_profile": run.generation_profile,
                "seed": attempt_seed,
                "synthesis_provenance_sha256": run.provenance_sha256,
            },
        ),
        "provider": run.provider,
        "model": run.model,
        "generation_profile": run.generation_profile,
        "speaker": plan.item.speaker,
        "requested_voice_character": plan.requested_voice,
        "voice_character": plan.voice,
        "prompt_sha256": plan.prompt_sha256,
        "prompt_applied": False,
        "queue_annotations_sha256": plan.queue_annotations_sha256,
        "synthesis_text_sha256": plan.synthesis_text_sha256,
        "text_transform": run.text_transform_id,
        "synthesis_provenance_sha256": run.provenance_sha256,
        "synthesis_configuration": run.synthesis_configuration,
        "updated_at": _now(),
    }
    if is_cancelled:
        failure = value["failure"]
        assert isinstance(failure, dict)
        failure["attempt_counted"] = False
        previous: object = plan.existing
        if generation_item_status(plan.existing) == "cancelled":
            stored = plan.existing.get("failure")
            previous = (
                stored.get("previous_failure") if isinstance(stored, dict) else None
            )
        if isinstance(previous, dict) and previous.get("status") == "failed":
            failure["previous_failure"] = copy.deepcopy(previous)
    if plan.synthesis_fallback is not None:
        value["synthesis_fallback"] = plan.synthesis_fallback
        value["narrator_character"] = run.narrator_character
    if plan.source_reference_binding is not None:
        value["source_reference_binding"] = plan.source_reference_binding
    if attempt_repair is not None:
        value["failure_repair"] = attempt_repair
    carry_forward = plan.existing.get("carry_forward")
    if (
        plan.repair_strategy
        in {
            SENTENCE_BOUNDARY_SEGMENTATION,
            INLINE_PAUSE_MARKER,
            BOUNDED_SEED_RETRY,
        }
        and _is_json_document(carry_forward)
        and carry_forward.get("mode") == "failed-outcome"
    ):
        value["carry_forward"] = copy.deepcopy(carry_forward)
    run.items[plan.queue_id] = value
    if run_attempts < plan.attempt_limit and not is_cancelled:
        _write_active_phase(
            run.state_path,
            run.state,
            "retrying",
            last_error=last_error,
            changed_items=(plan.queue_id,),
            controls_unchanged=run.progress_callback is None,
        )
    else:
        run.state["active"] = None
        write_generation_state(
            run.state_path,
            run.state,
            changed_items=(plan.queue_id,),
            controls_unchanged=run.progress_callback is None,
        )
    captured: _CapturedSilenceFailure | None = None
    if captured_partial is not None:
        captured = {
            "wav_payload": captured_partial,
            "queue_id": plan.queue_id,
            "line_id": plan.item.line_id,
            "text": plan.item.text,
            "text_sha256": plan.item.text_sha256,
            "state_item": copy.deepcopy(value),
        }
    return _GenerationFailureResult(
        last_error=last_error,
        cancelled=is_cancelled,
        captured_silence_failure=captured,
    )


def _begin_generation_attempt(
    run: _GenerationExecutionContext,
    plan: _PreparedGenerationItem,
    *,
    attempts: int,
    provider_attempts: int,
    run_attempts: int,
    last_error: str | None,
) -> _GenerationAttempt:
    attempts += 1
    provider_attempts += 1
    plan.attempts_by_provider[run.provider] = provider_attempts
    run_attempts += 1
    attempt_seed = run.seed + provider_attempts - 1
    request_seed = None if run.provider == "pocket-tts" else attempt_seed
    attempt_repair: JsonDocument | None = None
    if plan.repair_document is not None:
        attempt_repair = copy.deepcopy(plan.repair_document)
        if plan.repair_strategy == SENTENCE_BOUNDARY_SEGMENTATION:
            segments = attempt_repair.get("segments")
            if not isinstance(segments, list):
                raise BulkGenerationError("Sentence repair segments are malformed")
            attempt_repair["planned_segment_seeds"] = [
                attempt_seed + index for index in range(len(segments))
            ]
    partial = plan.destination.with_suffix(".partial.wav")
    if partial.exists():
        _archive_interrupted_artifact(run.output_directory, partial)
    _write_active(
        run.state_path,
        run.state,
        plan.item,
        provider=run.provider,
        model=run.model,
        generation_profile=run.generation_profile,
        prompt_sha256=plan.prompt_sha256,
        queue_annotations_sha256=plan.queue_annotations_sha256,
        synthesis_text_sha256=plan.synthesis_text_sha256,
        text_transform_id=run.text_transform_id,
        synthesis_provenance_sha256=run.provenance_sha256,
        synthesis_configuration=run.synthesis_configuration,
        synthesis_voice_character=plan.voice,
        synthesis_fallback=plan.synthesis_fallback,
        source_reference_binding=plan.source_reference_binding,
        failure_repair=attempt_repair,
        phase="generating",
        runtime_status=speech_runtime_label(run.backend),
        runtime_worker_pid=_runtime_worker_pid(run.backend),
        attempt=run_attempts,
        attempt_limit=plan.attempt_limit,
        total_attempts=attempts,
        provider_attempt=provider_attempts,
        attempts_by_provider=plan.attempts_by_provider,
        seed=attempt_seed,
        seed_applied=request_seed is not None,
        started_at=_now(),
        last_error=last_error,
        controls_unchanged=run.progress_callback is None,
    )
    _notify_progress(run)
    return _GenerationAttempt(
        attempts=attempts,
        provider_attempts=provider_attempts,
        attempts_by_provider=plan.attempts_by_provider,
        run_attempts=run_attempts,
        attempt_seed=attempt_seed,
        request_seed=request_seed,
        attempt_repair=attempt_repair,
        partial=partial,
        request=SynthesisRequest(
            voice=plan.voice,
            text=plan.synthesis_text,
            seed=request_seed,
            generation_profile=run.generation_profile,
            cancellation=run.cancellation,
            cache_policy=run.synthesis_cache_policy,
        ),
    )


def _final_audio_key(
    run: _GenerationExecutionContext,
    request: SynthesisRequest,
    repair: JsonDocument | None = None,
) -> str | None:
    identity = getattr(run.backend, "synthesis_key", None)
    if not callable(identity):
        return None
    synthesis_key = identity(request)
    if synthesis_key is None:
        return None
    if not isinstance(synthesis_key, str) or not synthesis_key:
        raise BulkGenerationProvenanceError("Invalid synthesis identity")
    return _canonical_sha256(
        {
            "final_wav_version": 1,
            "synthesis_key": synthesis_key,
            "text_transform": run.text_transform_id,
            "repair": repair,
        }
    )


def _saved_audio_result(
    run: _GenerationExecutionContext, request: SynthesisRequest, source: Path
) -> SynthesisResult:
    quality, samples = inspect_generated_wav_with_samples(source)
    return SynthesisResult(
        pcm=np.asarray(samples, dtype=np.float32) / 32768.0,
        sample_rate=quality.sample_rate,
        completion=SynthesisCompletion.COMPLETE,
        limits=SynthesisLimits(None, None),
        timing=SynthesisTiming(0.0, 0.0),
        diagnostics=SynthesisDiagnostics(
            backend=run.provider,
            cache_source="shared-wav",
            generation_profile=request.generation_profile,
            seed=request.seed,
            chunk_count=1,
            sample_count=quality.sample_count,
        ),
    )


def _prefetch_next_generation(
    run: _GenerationExecutionContext,
    item_index: int,
    prefetched_render: PrefetchedRender | None,
) -> PrefetchedRender | None:
    if prefetched_render is not None or item_index + 1 >= len(run.candidates):
        return prefetched_render
    try:
        prepared_next = _next_generation_request(run, run.candidates[item_index + 1])
    except Exception:
        return None
    if prepared_next is None:
        return None

    # ponytail: one speculative render may finish after the current WAV fails
    # validation; add per-request cancellation only if waste is measured.
    def collect_prefetched(
        request: SynthesisRequest = prepared_next,
    ) -> SynthesisResult:
        key = _final_audio_key(run, request)
        if key is not None and run.synthesis_cache_policy is SynthesisCachePolicy.USE:
            saved = run.audio_store.get(key)
            if saved is not None:
                return _saved_audio_result(run, request, saved)
        return run.render(request).collect()

    return (
        run.candidates[item_index + 1].queue_id,
        prepared_next,
        run.render_prefetch.submit(collect_prefetched),
    )


def _render_and_publish_generation_attempt(
    run: _GenerationExecutionContext,
    item_index: int,
    plan: _PreparedGenerationItem,
    attempt: _GenerationAttempt,
    prefetched_render: PrefetchedRender | None,
    pipeline_enabled: bool,
) -> PrefetchedRender | None:
    final_key = _final_audio_key(run, attempt.request, plan.repair_document)
    saved = None
    if final_key is not None and run.synthesis_cache_policy is SynthesisCachePolicy.USE:
        with measure_generation_phase("cached-wav-lookup"):
            saved = run.audio_store.get(final_key)
    if saved is not None:
        with measure_generation_phase("saved-wav-reuse"):
            attempt.rendered = _saved_audio_result(run, attempt.request, saved)
            if prefetched_render is not None and prefetched_render[0] == plan.queue_id:
                prefetched_render[2].result()
                prefetched_render = None
    else:
        # Prefetch can overlap publication; this measures caller wait, not GPU compute.
        with measure_generation_phase("render-wait"):
            if prefetched_render is not None and prefetched_render[0] == plan.queue_id:
                prefetched_request, future = prefetched_render[1:]
                prefetched_render = None
                if prefetched_request != attempt.request:
                    raise BulkGenerationError(
                        "OpenMOSS prefetched request identity changed"
                    )
                attempt.rendered = future.result()
            elif plan.repair_strategy == SENTENCE_BOUNDARY_SEGMENTATION:
                attempt.rendered = render_sentence_segments(
                    run.render,
                    attempt.request,
                    safe_sentence_segments(plan.synthesis_text),
                    pause_ms=run.repair_policy.segment_pause_ms,
                )
            else:
                attempt.rendered = run.render(attempt.request).collect()
    rendered = attempt.rendered
    worker_pid = _runtime_worker_pid(run.backend)
    record_diagnostic_event(
        "generation",
        "generation-render-result",
        line_id=plan.item.line_id,
        process_id=os.getpid(),
        worker_pid=worker_pid,
        provider=rendered.diagnostics.backend,
        cache_source=rendered.diagnostics.cache_source,
        completion=rendered.completion.value,
        synthesis_ms=rendered.timing.total_ms,
        first_chunk_ms=rendered.timing.first_chunk_ms,
        seed=rendered.diagnostics.seed,
        sample_count=rendered.diagnostics.sample_count,
        chunk_count=rendered.diagnostics.chunk_count,
    )
    active = run.state.get("active")
    if _is_json_document(active):
        active["runtime_status"] = speech_runtime_label(run.backend)
        active["runtime_worker_pid"] = worker_pid
    _validate_render_result(attempt.rendered, attempt.request, run.provider)
    _assert_control_files_unchanged(run.controls)
    if run.workspace_output_identity is not None:
        _assert_workspace_output_identity(
            run.output_argument, run.workspace_output_identity
        )
    run.lease.assert_owned()
    _write_active_phase(
        run.state_path,
        run.state,
        "validating",
        controls_unchanged=run.progress_callback is None,
    )
    _notify_progress(run)
    if pipeline_enabled:
        prefetched_render = _prefetch_next_generation(
            run, item_index, prefetched_render
        )
    with measure_generation_phase("wav-quality"):
        output_pcm = _generated_mono_pcm(attempt.rendered.pcm)
        if plan.repair_strategy == EDGE_SILENCE_TRIM:
            trimmed = trim_excess_edge_silence(
                output_pcm,
                attempt.rendered.sample_rate,
                trigger_seconds=NOTABLE_SILENCE_SPAN_SECONDS,
            )
            output_pcm = trimmed.pcm
            attempt.attempt_repair = {
                **(attempt.attempt_repair or {}),
                "leading_trimmed_samples": trimmed.leading_trimmed_samples,
                "trailing_trimmed_samples": trimmed.trailing_trimmed_samples,
            }
        if saved is None:
            write_pcm16_wav(attempt.partial, output_pcm, attempt.rendered.sample_rate)
        else:
            link_shared_audio(saved, attempt.partial)
        payload = read_regular_bytes(attempt.partial)
        quality, samples = inspect_generated_wav_with_samples(io.BytesIO(payload))
        speech_quality = inspect_generated_speech_samples(
            samples,
            sample_rate=quality.sample_rate,
            duration_seconds=quality.sample_count / quality.sample_rate,
            text=plan.synthesis_text,
        )
        file_sha256 = hashlib.sha256(payload).hexdigest()
    _write_active_phase(
        run.state_path,
        run.state,
        "publishing",
        controls_unchanged=run.progress_callback is None,
    )
    _notify_progress(run)
    if run.workspace_output_identity is not None:
        _assert_workspace_output_identity(
            run.output_argument, run.workspace_output_identity
        )
    if attempt.request.cancellation_requested():
        raise BulkGenerationError("Generation cancelled by user")
    run.lease.assert_owned()
    with measure_generation_phase("wav-store"):
        stored = run.audio_store.put(
            final_key or "wav-" + file_sha256,
            attempt.partial,
            expected_sha256=file_sha256,
        )
        link_shared_audio(stored, plan.destination, file_sha256)
        attempt.partial.unlink()
    _store_successful_generation_attempt(
        run,
        plan,
        attempts=attempt.attempts,
        attempts_by_provider=attempt.attempts_by_provider,
        attempt_seed=attempt.attempt_seed,
        request_seed=attempt.request_seed,
        attempt_repair=attempt.attempt_repair,
        file_sha256=file_sha256,
        quality=quality,
        speech_quality=speech_quality,
    )
    record_diagnostic_event(
        "generation",
        "generation-published",
        line_id=plan.item.line_id,
        process_id=os.getpid(),
        source_sha256=file_sha256,
        sample_count=quality.sample_count,
        sample_rate=quality.sample_rate,
    )
    return prefetched_render


def _publish_runtime_progress(
    run: _GenerationExecutionContext, changed_items: tuple[str, ...]
) -> None:
    run.lease.assert_owned()
    with measure_generation_phase("progress-manifest"):
        write_runtime_progress_manifest_from_state(
            run.state,
            run.output_directory,
            run.output_directory / RUNTIME_PROGRESS_MANIFEST_NAME,
            # Every included WAV was validated before its atomic state update.
            # Playback independently verifies the file hash before use.
            validate_files=False,
        )
    _notify_progress(run, changed_items=changed_items)


@measure_generation_phase("progress-observer")
def _notify_progress(
    run: _GenerationExecutionContext, *, changed_items: tuple[str, ...] = ()
) -> None:
    if run.progress_callback is not None:
        run.progress_callback(run.state)
    if run.progress_observer is not None:
        run.progress_observer(
            _generation_progress_event(
                run.state,
                run.items,
                None if run.progress_callback is not None else changed_items,
            )
        )


def _execute_generation_item(
    run: _GenerationExecutionContext,
    item_index: int,
    plan: _PreparedGenerationItem,
    prefetched_render: PrefetchedRender | None,
    pipeline_enabled: bool,
) -> _GenerationItemExecutionResult:
    attempts = plan.attempts
    provider_attempts = plan.provider_attempts
    run_attempts = 0
    last_error = plan.last_error
    captured_silence_failure: _CapturedSilenceFailure | None = None

    while run_attempts < plan.attempt_limit:
        attempt = _begin_generation_attempt(
            run,
            plan,
            attempts=attempts,
            provider_attempts=provider_attempts,
            run_attempts=run_attempts,
            last_error=last_error,
        )
        attempts = attempt.attempts
        provider_attempts = attempt.provider_attempts
        run_attempts = attempt.run_attempts
        try:
            prefetched_render = _render_and_publish_generation_attempt(
                run,
                item_index,
                plan,
                attempt,
                prefetched_render,
                pipeline_enabled,
            )
        except (
            BulkGenerationSourceChangedError,
            BulkGenerationProvenanceError,
            SharedAudioStoreError,
        ):
            attempt.partial.unlink(missing_ok=True)
            raise
        except Exception as error:
            if run.state.get("active") is None:
                raise
            try:
                run.lease.assert_owned()
            except BulkGenerationError:
                attempt.partial.unlink(missing_ok=True)
                raise
            failure = _store_failed_generation_attempt(
                run,
                plan,
                error=error,
                completion=(
                    attempt.rendered.completion
                    if attempt.rendered is not None
                    else None
                ),
                request=attempt.request,
                partial=attempt.partial,
                attempts=attempt.attempts,
                attempts_by_provider=attempt.attempts_by_provider,
                attempt_seed=attempt.attempt_seed,
                request_seed=attempt.request_seed,
                attempt_repair=attempt.attempt_repair,
                run_attempts=run_attempts,
            )
            last_error = failure.last_error
            if failure.captured_silence_failure is not None:
                captured_silence_failure = failure.captured_silence_failure
            _publish_runtime_progress(run, (plan.queue_id,))
            if failure.cancelled:
                return _GenerationItemExecutionResult(
                    generated=False,
                    cancelled=True,
                    captured_silence_failure=captured_silence_failure,
                    prefetched_render=prefetched_render,
                )
        else:
            _publish_runtime_progress(run, (plan.queue_id,))
            return _GenerationItemExecutionResult(
                generated=True,
                cancelled=False,
                captured_silence_failure=captured_silence_failure,
                prefetched_render=prefetched_render,
            )
    return _GenerationItemExecutionResult(
        generated=False,
        cancelled=False,
        captured_silence_failure=captured_silence_failure,
        prefetched_render=prefetched_render,
    )


def _execute_generation_candidates(
    run: _GenerationExecutionContext,
) -> _GenerationExecutionResult:
    candidates = run.candidates
    generated = 0
    skipped_existing = 0
    cancelled = False
    captured_silence_failure: _CapturedSilenceFailure | None = None
    prefetched_render: PrefetchedRender | None = None
    pipeline_enabled = (
        run.provider == "moss-tts"
        and run.repair_policy.is_empty
        and run.text_transform is None
        and not run.regenerate_existing
    )

    for item_index, item in enumerate(candidates):
        plan = _prepare_generation_item(run, item)
        if plan is None:
            skipped_existing += 1
            continue
        item_result = _execute_generation_item(
            run, item_index, plan, prefetched_render, pipeline_enabled
        )
        prefetched_render = item_result.prefetched_render
        generated += int(item_result.generated)
        if item_result.captured_silence_failure is not None:
            captured_silence_failure = item_result.captured_silence_failure
        if item_result.cancelled:
            cancelled = True
            break

    return _GenerationExecutionResult(
        generated=generated,
        skipped_existing=skipped_existing,
        cancelled=cancelled,
        captured_silence_failure=captured_silence_failure,
    )


def _finalize_generation_run(
    *,
    queue_path: Path,
    queue_sha256: str,
    controls: Sequence[_GenerationControl],
    workspace_output_identity: object,
    output_argument: Path,
    lease: GenerationLease,
    state: JsonDocument,
    state_path: Path,
    manifest_path: Path,
    evidence_directory: Path | None,
    provenance_sha256: str,
    execution: _GenerationExecutionResult,
    skipped_actions: int,
    skipped_characters: int,
    skipped_items: int,
) -> BulkGenerationResult:
    _assert_sources_unchanged(queue_path, queue_sha256, controls)
    if workspace_output_identity is not None:
        _assert_workspace_output_identity(output_argument, workspace_output_identity)
    lease.assert_owned()
    with measure_generation_phase("final-manifest"):
        publish_generated_manifest(
            state_path, manifest_path=manifest_path, _lease_held=True
        )
    captured = execution.captured_silence_failure
    if captured is not None:
        if evidence_directory is None:
            raise BulkGenerationError("Silence-failure evidence directory is missing")
        try:
            publish_silence_failure_evidence(
                evidence_directory,
                captured["wav_payload"],
                {
                    "queue": str(queue_path),
                    "queue_sha256": queue_sha256,
                    "state": str(state_path),
                    "state_sha256": sha256_file(state_path),
                    "queue_id": captured["queue_id"],
                    "line_id": captured["line_id"],
                    "text": captured["text"],
                    "text_sha256": captured["text_sha256"],
                    "state_item": captured["state_item"],
                    "state_item_sha256": _canonical_sha256(captured["state_item"]),
                    "synthesis_controls_sha256": provenance_sha256,
                },
            )
        except (SilenceFailureEvidenceError, OSError) as error:
            raise BulkGenerationError(str(error)) from error
    failed = sum(
        generation_item_status(value) == "failed"
        for value in _state_items(state).values()
    )
    return BulkGenerationResult(
        generated=execution.generated,
        failed=failed,
        skipped_existing=execution.skipped_existing,
        skipped_actions=skipped_actions,
        skipped_characters=skipped_characters,
        skipped_items=skipped_items,
        cancelled=execution.cancelled,
        state=state_path,
        manifest=manifest_path,
    )


def _validated_generation_backend(
    backend: object,
    provider: str,
    model: str,
    retries: int,
    repair_policy: FailureRepairPolicy,
) -> GenerationRenderer:
    render = getattr(backend, "render", None)
    if not _is_generation_renderer(render):
        raise BulkGenerationError(
            "Generation backend must implement render(SynthesisRequest)"
        )
    backend_name = _required_text(
        getattr(backend, "name", provider), "Backend identity"
    )
    if repair_policy.offline_fallback_queue_ids and retries != 0:
        raise BulkGenerationError(
            "Offline fallback is a single backend-owned unseeded attempt; set retries to 0"
        )
    if provider == "pocket-tts" and retries != 0:
        raise BulkGenerationError(
            "Pocket TTS generation is unseeded and permits exactly one attempt; "
            "set retries to 0"
        )
    if repair_policy.inline_pause_queue_ids and (
        retries != 0 or provider != "moss-tts"
    ):
        raise BulkGenerationError(
            "Inline pause comparison requires moss-tts and exactly one attempt; "
            "set retries to 0"
        )
    if backend_name != provider:
        raise BulkGenerationError(
            f"Configured provider {provider!r} does not match backend {backend_name!r}"
        )
    backend_model = getattr(backend, "model_identity", None) or getattr(
        backend, "model_name", None
    )
    if backend_model is None:
        backend_model = backend_name
    if str(backend_model) != model:
        raise BulkGenerationError(
            f"Configured model {model!r} does not match backend model {backend_model!r}"
        )
    return render


def _prepare_generation_configuration(
    backend: object,
    *,
    provider: object,
    model: object,
    generation_profile: object,
    limit: object,
    retries: object,
    seed: object,
    synthesis_cache_policy: object,
    missing_voice_policy: object,
    synthesis_character_overrides: object,
    narrator_character: str | None,
    audio_event_spoken_projection_queue_ids: Sequence[object] | None,
    failure_repair_policy: object,
    text_transform: object,
    text_transform_id: object,
) -> _GenerationConfiguration:
    limit = _nonnegative_optional_int(limit, "Generation limit")
    retries = _nonnegative_optional_int(retries, "Retry count")
    if retries is None:
        raise BulkGenerationError("Retry count must be nonnegative")
    seed = _generation_integer(seed, "Base seed")
    provider = _generation_text(provider, "Provider")
    model = _generation_text(model, "Model")
    generation_profile = _generation_text(generation_profile, "Generation profile")
    try:
        synthesis_cache_policy = SynthesisCachePolicy(synthesis_cache_policy)
    except ValueError as error:
        raise BulkGenerationError(
            f"Unknown synthesis cache policy {synthesis_cache_policy!r}"
        ) from error
    policy, character_overrides = _generation_voice_overrides(
        missing_voice_policy,
        synthesis_character_overrides,
        narrator_character=narrator_character,
    )
    projection_queue_ids = tuple(
        sorted(
            {
                _generation_text(value, "Audio-event spoken projection queue ID")
                for value in (audio_event_spoken_projection_queue_ids or ())
            }
        )
    )
    try:
        repair_policy = (
            failure_repair_policy
            if isinstance(failure_repair_policy, FailureRepairPolicy)
            else FailureRepairPolicy.from_document(failure_repair_policy)
        )
    except FailureRepairPolicyError as error:
        raise BulkGenerationError(str(error)) from error
    if text_transform is not None and not _is_text_transform(text_transform):
        raise BulkGenerationError("Text transform must be callable")
    if text_transform is not None:
        text_transform_id = _generation_text(
            text_transform_id, "Text transform identity"
        )
    elif text_transform_id is not None:
        raise BulkGenerationError("Text transform identity requires a text transform")
    render = _validated_generation_backend(
        backend, provider, model, retries, repair_policy
    )
    return _GenerationConfiguration(
        limit,
        retries,
        seed,
        provider,
        model,
        generation_profile,
        synthesis_cache_policy,
        policy,
        character_overrides,
        projection_queue_ids,
        repair_policy,
        text_transform,
        text_transform_id,
        render,
    )


def _selected_generation_queue_ids(
    queue: VoiceGenerationQueue, include_queue_ids: Sequence[object] | None
) -> set[str] | None:
    if include_queue_ids is None:
        return None
    selected = {
        _generation_text(value, "Selected queue ID") for value in include_queue_ids
    }
    unknown = selected - {item.queue_id for item in queue.items}
    if unknown:
        raise BulkGenerationError(
            "Selected queue IDs are absent from the bound queue: "
            + ", ".join(sorted(unknown))
        )
    return selected


def _validate_audio_event_projection(
    queue: VoiceGenerationQueue,
    selected_queue_ids: set[str] | None,
    configuration: _GenerationConfiguration,
) -> None:
    projection_queue_ids = configuration.projection_queue_ids
    if not projection_queue_ids:
        return
    if (
        selected_queue_ids != set(projection_queue_ids)
        or not configuration.repair_policy.is_empty
        or configuration.text_transform_id != "audio-event-spoken-projection-v1"
        or configuration.text_transform is not audio_event_spoken_projection
    ):
        raise BulkGenerationError(
            "Audio-event spoken projection requires its exact queue-ID scope "
            "and canonical text transform"
        )
    queue_by_id = {item.queue_id: item for item in queue.items}
    for queue_id in projection_queue_ids:
        item = queue_by_id[queue_id]
        if item.action != "generate":
            raise BulkGenerationError(
                f"Audio-event projection item is not generated: {queue_id!r}"
            )
        plan = audio_event_plan_for_record(item)
        if plan is None or plan.get("spoken_text") != audio_event_spoken_projection(
            item.text
        ):
            raise BulkGenerationError(
                f"Audio-event projection plan changed for {queue_id!r}"
            )


def _silence_failure_evidence_directory(
    value: object,
    output_directory: Path,
    selected_queue_ids: set[str] | None,
    retries: int,
) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        raise BulkGenerationError("Silence-failure evidence requires a directory name")
    directory = Path(value).expanduser()
    if not directory.name or directory.name in {".", ".."}:
        raise BulkGenerationError("Silence-failure evidence requires a directory name")
    directory = no_replace_destination(directory)
    if directory.is_relative_to(output_directory):
        raise BulkGenerationError(
            "Silence-failure evidence must stay outside generated output"
        )
    if selected_queue_ids is None or len(selected_queue_ids) != 1 or retries != 0:
        raise BulkGenerationError(
            "Silence-failure evidence requires one exact queue ID and retries=0"
        )
    if directory.exists() or directory.is_symlink():
        raise BulkGenerationError(
            f"Silence-failure evidence destination already exists: {directory}"
        )
    return directory


def _prepare_generation_inputs(
    queue_path: str | Path,
    output_directory: str | Path,
    *,
    workspace_output_identity: object,
    queue_voice_overrides: object,
    include_queue_ids: Sequence[object] | None,
    include_characters: Sequence[str] | None,
    regenerate_existing: bool,
    silence_failure_evidence: object,
    configuration: _GenerationConfiguration,
) -> _GenerationInputs:
    queue_path = Path(queue_path).expanduser().resolve()
    output_argument = Path(output_directory).expanduser()
    if workspace_output_identity is not None:
        _assert_workspace_output_identity(output_argument, workspace_output_identity)
    output_directory = output_argument.resolve()
    queue, queue_sha256 = _load_stable_queue(queue_path)
    queue_voice_overrides = _validated_queue_voice_overrides(
        queue_voice_overrides, queue
    )
    selected_queue_ids = _selected_generation_queue_ids(queue, include_queue_ids)
    if not configuration.repair_policy.is_empty and selected_queue_ids != set(
        configuration.repair_policy.queue_ids
    ):
        raise BulkGenerationError(
            "Failure repair requires an exact --queue-id selection matching its policy"
        )
    _validate_audio_event_projection(queue, selected_queue_ids, configuration)
    evidence_directory = _silence_failure_evidence_directory(
        silence_failure_evidence,
        output_directory,
        selected_queue_ids,
        configuration.retries,
    )
    if (
        regenerate_existing
        and selected_queue_ids is None
        and include_characters is None
    ):
        raise BulkGenerationError(
            "Regenerating existing outcomes requires explicit queue IDs or characters"
        )
    return _GenerationInputs(
        queue_path,
        output_argument,
        output_directory,
        queue,
        queue_sha256,
        queue_voice_overrides,
        selected_queue_ids,
        evidence_directory,
    )


def run_bulk_generation(
    queue_path: str | Path,
    output_directory: str | Path,
    backend: object,
    *,
    provider: object,
    model: object,
    generation_profile: object = "stable",
    limit: object = None,
    retries: object = 2,
    include_prefer_source: bool = False,
    include_characters: Sequence[str] | None = None,
    include_queue_ids: Sequence[object] | None = None,
    regenerate_existing: bool = False,
    item_filter: Callable[[VoiceGenerationQueueItem], bool] | None = None,
    seed: object = 0,
    cancellation: SynthesisCancellation = None,
    control_files: Mapping[str, str | Path | tuple[str | Path, str]] | None = None,
    text_transform: Callable[[str], str] | None = None,
    text_transform_id: object = None,
    process_checker: Callable[[object], bool] | None = None,
    workspace_output_identity: object = None,
    synthesis_character_overrides: object = None,
    queue_voice_overrides: object = None,
    missing_voice_policy: object = None,
    narrator_character: str | None = None,
    failure_repair_policy: object = None,
    silence_failure_evidence: object = None,
    audio_event_spoken_projection_queue_ids: Sequence[object] | None = None,
    synthesis_cache_policy: object = SynthesisCachePolicy.BYPASS,
    audio_store_directory: str | Path | None = None,
    approve_validated_audio: bool = False,
    progress_callback: Callable[[JsonDocument], None] | None = None,
    progress_observer: Callable[[GenerationProgressEvent], None] | None = None,
) -> BulkGenerationResult:
    """Render selected queue items with no device playback and resumable state."""
    configuration = _prepare_generation_configuration(
        backend,
        provider=provider,
        model=model,
        generation_profile=generation_profile,
        limit=limit,
        retries=retries,
        seed=seed,
        synthesis_cache_policy=synthesis_cache_policy,
        missing_voice_policy=missing_voice_policy,
        synthesis_character_overrides=synthesis_character_overrides,
        narrator_character=narrator_character,
        audio_event_spoken_projection_queue_ids=(
            audio_event_spoken_projection_queue_ids
        ),
        failure_repair_policy=failure_repair_policy,
        text_transform=text_transform,
        text_transform_id=text_transform_id,
    )
    limit = configuration.limit
    retries = configuration.retries
    seed = configuration.seed
    provider = configuration.provider
    model = configuration.model
    generation_profile = configuration.generation_profile
    synthesis_cache_policy = configuration.synthesis_cache_policy
    policy = configuration.policy
    character_overrides = configuration.character_overrides
    projection_queue_ids = configuration.projection_queue_ids
    repair_policy = configuration.repair_policy
    text_transform = configuration.text_transform
    text_transform_id = configuration.text_transform_id
    render = configuration.render

    inputs = _prepare_generation_inputs(
        queue_path,
        output_directory,
        workspace_output_identity=workspace_output_identity,
        queue_voice_overrides=queue_voice_overrides,
        include_queue_ids=include_queue_ids,
        include_characters=include_characters,
        regenerate_existing=regenerate_existing,
        silence_failure_evidence=silence_failure_evidence,
        configuration=configuration,
    )
    queue_path = inputs.queue_path
    output_argument = inputs.output_argument
    output_directory = inputs.output_directory
    queue = inputs.queue
    queue_sha256 = inputs.queue_sha256
    queue_voice_overrides = inputs.queue_voice_overrides
    selected_queue_ids = inputs.selected_queue_ids
    evidence_directory = inputs.evidence_directory
    controls = _snapshot_control_files(control_files or {})
    recorded_voices = snapshot_recorded_voices(
        controls, narrator_character=narrator_character
    )
    _assert_missing_voice_overrides_match_manifest(
        controls, character_overrides, narrator_character, provider
    )
    control_records = [_stored_control(value) for value in controls]
    synthesis_configuration = {
        "missing_voice_policy": policy.to_document(),
        "synthesis_character_overrides": dict(sorted(character_overrides.items())),
        "failure_repair_policy": repair_policy.to_document(),
    }
    if projection_queue_ids:
        synthesis_configuration["audio_event_spoken_projection_queue_ids"] = list(
            projection_queue_ids
        )
    if queue_voice_overrides:
        synthesis_configuration["queue_voice_overrides_sha256"] = (
            queue_voice_overrides_sha256(queue_voice_overrides)
        )
    provenance_sha256 = _canonical_sha256(
        {
            "provider": provider,
            "model": model,
            "generation_profile": generation_profile,
            "text_transform": text_transform_id,
            **synthesis_configuration,
            "controls": [
                {"role": value["role"], "sha256": value["sha256"]} for value in controls
            ],
        }
    )
    state_path = generation_state_path(output_directory)
    manifest_path = output_directory / "manifest.json"
    output_directory.mkdir(parents=True, exist_ok=True)

    return _run_generation_execution(
        output_directory=output_directory,
        queue_sha256=queue_sha256,
        process_checker=process_checker,
        workspace_output_identity=workspace_output_identity,
        output_argument=output_argument,
        state_path=state_path,
        audio_store=SharedAudioStore(
            audio_store_directory or output_directory.parent / "audio-store"
        ),
        queue=queue,
        repair_policy=repair_policy,
        selected_queue_ids=selected_queue_ids,
        provenance_sha256=provenance_sha256,
        control_records=control_records,
        queue_path=queue_path,
        controls=controls,
        include_prefer_source=include_prefer_source,
        include_characters=include_characters,
        item_filter=item_filter,
        limit=limit,
        regenerate_existing=regenerate_existing,
        queue_voice_overrides=queue_voice_overrides,
        character_overrides=character_overrides,
        policy=policy,
        narrator_character=narrator_character,
        synthesis_configuration=synthesis_configuration,
        text_transform=text_transform,
        text_transform_id=text_transform_id,
        provider=provider,
        model=model,
        generation_profile=generation_profile,
        backend=backend,
        render=render,
        seed=seed,
        retries=retries,
        cancellation=cancellation,
        synthesis_cache_policy=synthesis_cache_policy,
        recorded_voices=recorded_voices,
        evidence_directory=evidence_directory,
        manifest_path=manifest_path,
        approve_validated_audio=approve_validated_audio,
        progress_callback=progress_callback,
        progress_observer=progress_observer,
    )


@measure_generation_run()
def _run_generation_execution(
    *,
    output_directory: Path,
    queue_sha256: str,
    process_checker: Callable[[object], bool] | None,
    workspace_output_identity: object,
    output_argument: Path,
    state_path: Path,
    audio_store: SharedAudioStore,
    queue: VoiceGenerationQueue,
    repair_policy: FailureRepairPolicy,
    selected_queue_ids: set[str] | None,
    provenance_sha256: str,
    control_records: list[JsonDocument],
    queue_path: Path,
    controls: Sequence[_GenerationControl],
    include_prefer_source: bool,
    include_characters: Sequence[str] | None,
    item_filter: Callable[[VoiceGenerationQueueItem], bool] | None,
    limit: int | None,
    regenerate_existing: bool,
    queue_voice_overrides: dict[str, str],
    character_overrides: dict[str, str],
    policy: MissingVoicePolicy,
    narrator_character: str | None,
    synthesis_configuration: JsonDocument,
    text_transform: Callable[[str], str] | None,
    text_transform_id: str | None,
    provider: str,
    model: str,
    generation_profile: str,
    backend: object,
    render: GenerationRenderer,
    seed: int,
    retries: int,
    cancellation: SynthesisCancellation,
    synthesis_cache_policy: SynthesisCachePolicy,
    recorded_voices: dict[str, RecordedVoice],
    evidence_directory: Path | None,
    manifest_path: Path,
    approve_validated_audio: bool,
    progress_callback: Callable[[JsonDocument], None] | None,
    progress_observer: Callable[[GenerationProgressEvent], None] | None,
) -> BulkGenerationResult:
    with (
        _GenerationLease(
            output_directory,
            queue_sha256,
            process_checker=process_checker or process_is_alive,
        ) as lease,
        ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="moss-generation-prefetch"
        ) as render_prefetch,
    ):
        _assert_generation_workspace_identity(
            workspace_output_identity, output_argument
        )
        lease.assert_owned()
        interrupted_job = lease.interrupted_process
        state = _load_or_create_state(state_path, output_directory, queue, queue_sha256)
        _validate_failure_repair_selection(
            repair_policy, selected_queue_ids, state, queue, provider=provider
        )
        _record_generation_controls(
            state, state_path, provenance_sha256, control_records
        )
        _record_interrupted_generation_job(state, state_path, interrupted_job)
        _reconcile_interrupted_attempt(state_path, state, queue)
        with measure_generation_phase("progress-observer"):
            if progress_callback is not None:
                progress_callback(state)
            if progress_observer is not None:
                progress_observer(
                    _generation_progress_event(state, _state_items(state), None)
                )
        state.setdefault("game", queue.metadata.get("game"))
        state.setdefault("language", queue.metadata.get("language"))
        candidates, skipped_actions, skipped_characters, skipped_items = (
            _select_generation_candidates(
                queue,
                state,
                include_prefer_source=include_prefer_source,
                include_characters=include_characters,
                selected_queue_ids=selected_queue_ids,
                item_filter=item_filter,
                limit=limit,
                regenerate_existing=regenerate_existing,
            )
        )
        execution = _execute_generation_candidates(
            _GenerationExecutionContext(
                state_path=state_path,
                state=state,
                _items=_state_items(state),
                output_directory=output_directory,
                output_argument=output_argument,
                audio_store=audio_store,
                workspace_output_identity=workspace_output_identity,
                candidates=candidates,
                queue_voice_overrides=queue_voice_overrides,
                character_overrides=character_overrides,
                repair_policy=repair_policy,
                policy=policy,
                narrator_character=narrator_character,
                synthesis_configuration=synthesis_configuration,
                text_transform=text_transform,
                text_transform_id=text_transform_id,
                provider=provider,
                model=model,
                generation_profile=generation_profile,
                provenance_sha256=provenance_sha256,
                backend=backend,
                render=render,
                controls=controls,
                lease=lease,
                render_prefetch=render_prefetch,
                seed=seed,
                retries=retries,
                cancellation=cancellation,
                synthesis_cache_policy=synthesis_cache_policy,
                recorded_voices=recorded_voices,
                evidence_directory=evidence_directory,
                regenerate_existing=regenerate_existing,
                approve_validated_audio=approve_validated_audio,
                progress_callback=progress_callback,
                progress_observer=progress_observer,
            )
        )
        return _finalize_generation_run(
            queue_path=queue_path,
            queue_sha256=queue_sha256,
            controls=controls,
            workspace_output_identity=workspace_output_identity,
            output_argument=output_argument,
            lease=lease,
            state=state,
            state_path=state_path,
            manifest_path=manifest_path,
            evidence_directory=evidence_directory,
            provenance_sha256=provenance_sha256,
            execution=execution,
            skipped_actions=skipped_actions,
            skipped_characters=skipped_characters,
            skipped_items=skipped_items,
        )


def _assert_generation_workspace_identity(
    identity: object, output_argument: Path
) -> None:
    if identity is not None:
        _assert_workspace_output_identity(output_argument, identity)


def _record_generation_controls(
    state: JsonDocument,
    state_path: Path,
    provenance_sha256: str,
    controls: list[JsonDocument],
) -> None:
    registry_value = state.get("synthesis_controls")
    if registry_value is None:
        registry: JsonDocument = {}
        state["synthesis_controls"] = registry
    elif _is_json_document(registry_value):
        registry = registry_value
    else:
        raise BulkGenerationProvenanceError(
            "Stored synthesis controls conflict with this run"
        )
    existing = registry.get(provenance_sha256)
    if existing is not None and existing != controls:
        # Relocated inputs retain their content identity and original evidence paths.
        if (
            not isinstance(existing, list)
            or any(not isinstance(control, dict) for control in existing)
            or [
                {key: value for key, value in control.items() if key != "path"}
                for control in existing
            ]
            != [
                {key: value for key, value in control.items() if key != "path"}
                for control in controls
            ]
        ):
            raise BulkGenerationProvenanceError(
                "Stored synthesis controls conflict with this run"
            )
    if existing is None:
        registry[provenance_sha256] = controls
        write_generation_state(state_path, state)


def _record_interrupted_generation_job(
    state: JsonDocument, state_path: Path, interrupted_job: JsonDocument | None
) -> None:
    if interrupted_job is None:
        return
    value = state.get("interrupted_processes")
    if value is None:
        processes: list[object] = []
        state["interrupted_processes"] = processes
    elif _is_object_list(value):
        processes = value
    else:
        raise BulkGenerationError("Interrupted processes are malformed")
    if not any(
        isinstance(item, dict)
        and item.get("lease_sha256") == interrupted_job["lease_sha256"]
        for item in processes
    ):
        processes.append(interrupted_job)
        write_generation_state(state_path, state)


def publish_generated_manifest(
    state_path: str | Path,
    *,
    manifest_path: str | Path | None = None,
    _lease_held: bool = False,
) -> Path:
    """Rebuild the approved-only manifest from authoritative state."""
    state_path = generation_state_file(state_path)
    output_directory = state_path.parent
    workspace_queue = state_path.parent.parent / "queue.jsonl"
    workspace_ledger = state_path.parent.parent / "workspace.json"
    if workspace_ledger.exists():
        if workspace_ledger.is_symlink() or not workspace_ledger.is_file():
            raise BulkGenerationError(
                "Workspace generation state requires a regular workspace ledger"
            )
        if workspace_queue.is_symlink() or not workspace_queue.is_file():
            raise BulkGenerationError(
                "Workspace generation state requires its canonical queue"
            )
        state = load_generation_state(state_path, workspace_queue)
    else:
        state = load_generation_state(state_path)
    if not _lease_held:
        with _GenerationLease(
            output_directory,
            _generation_text(state.get("queue_sha256"), "Queue SHA-256"),
            process_checker=process_is_alive,
        ):
            return publish_generated_manifest(
                state_path, manifest_path=manifest_path, _lease_held=True
            )
    manifest_path = Path(manifest_path or output_directory / "manifest.json").resolve()
    if manifest_path.parent != output_directory:
        raise BulkGenerationError(
            "Generated-audio manifest must stay in the state directory"
        )
    validate_authoring_publication_authority(state_path, state)
    _write_generated_manifest_from_state(state, output_directory, manifest_path)
    return manifest_path


_write_generated_manifest_from_state = write_generated_manifest_from_state


def validate_terminal_conflict_publication_authority(
    state_path: str | Path, state: JsonDocument
) -> None:
    """Bind marked state to one fully validated canonical workspace ledger."""
    marked = any(
        isinstance(result, dict) and "terminal_conflict_resolution" in result
        for result in _state_items(state).values()
    )
    if not marked:
        return
    state_path = generation_state_file(state_path)
    workspace_path = state_path.parent.parent / "workspace.json"
    if workspace_path.is_symlink() or not workspace_path.is_file():
        raise BulkGenerationError(
            "Terminal conflict state requires its canonical workspace ledger"
        )
    try:
        workbench = importlib.import_module("vntts.authoring.workbench")
        directory, workspace, _workspace_sha256 = workbench.load_workspace_authority(
            workspace_path.parent
        )
        canonical_state_path = (
            directory / "generated-audio" / "generation-state.sqlite3"
        ).resolve()
        if canonical_state_path != Path(state_path).resolve():
            raise BulkGenerationError(
                "Terminal conflict state is not the canonical workspace state"
            )
        current = load_generation_state(
            canonical_state_path,
            directory / "queue.jsonl",
        )
        if current != state:
            raise BulkGenerationError(
                "Terminal conflict state changed while publication was prepared"
            )
    except workbench.AuthoringWorkbenchError as error:
        raise BulkGenerationError(str(error)) from error
    try:
        validate_terminal_conflict_state_binding(
            state, workspace.get("terminal_conflict_merge")
        )
    except TerminalConflictRecordError as error:
        raise BulkGenerationError(str(error)) from error


def validate_authoring_publication_authority(
    state_path: str | Path, state: JsonDocument
) -> None:
    """Validate every reserved authoring provenance extension before projection."""
    validate_terminal_conflict_publication_authority(state_path, state)
    config_rebase = importlib.import_module("vntts.authoring.config_rebase")
    config_rebase.validate_config_rebase_publication_authority(state_path, state)


_validate_terminal_conflict_manifest_authority = (
    validate_authoring_publication_authority
)


_approved_manifest_entries = approved_manifest_entries


_assert_review_authorities = assert_review_authorities
_review_generation_cohort = review_generation_cohort


_GenerationLease = GenerationLease
_process_started_at = process_started_at


@dataclass(frozen=True)
class GenerationCancellationWitness:
    output: Path
    directory_identity: tuple[int, int]
    queue: Path
    queue_sha256: str
    lease: JsonDocument
    requested_at: str


def capture_generation_cancellation(
    output: Path, queue: Path, queue_sha256: str, process: subprocess.Popen[str]
) -> GenerationCancellationWitness | None:
    """Witness explicit Cancel only while this exact owned child is still live."""
    pid = getattr(process, "pid", None)
    if type(pid) is not int or pid <= 0 or process.poll() is not None:
        return None
    output = output.resolve()
    lease_path = output / ".generation-lease.json"
    if not lease_path.exists():
        return None
    lease = _load_json(lease_path, "generation lease")
    started = process_started_at(process.pid, timeout=0.5)
    if (
        lease.get("schema") != LEASE_SCHEMA
        or type(lease.get("schema_version")) is not int
        or lease.get("schema_version") != LEASE_VERSION
        or lease.get("pid") != process.pid
        or lease.get("hostname") != socket.gethostname()
        or lease.get("queue_sha256") != queue_sha256
        or not isinstance(lease.get("lease_id"), str)
        or re.fullmatch(r"[a-f0-9]{32}", str(lease["lease_id"])) is None
        or started is None
        or lease.get("process_started_at") != started
        or process.poll() is not None
    ):
        return None
    identity = output.stat()
    return GenerationCancellationWitness(
        output,
        (identity.st_dev, identity.st_ino),
        queue.resolve(),
        queue_sha256,
        lease,
        _now(),
    )


def reconcile_generation_cancellation(
    witness: GenerationCancellationWitness, process: subprocess.Popen[str]
) -> bool:
    """Reconcile a confirmed owned exit without taking authority from a new writer."""
    if process.pid != witness.lease["pid"] or process.poll() is None:
        raise BulkGenerationError("Cancelled generation child has not confirmed exit")
    if sys.platform != "win32" and process.returncode not in {
        -signal.SIGTERM,
        -signal.SIGKILL,
    }:
        return False
    identity = witness.output.stat()
    if (identity.st_dev, identity.st_ino) != witness.directory_identity:
        raise BulkGenerationError("Cancelled generation output identity changed")
    if not (witness.output / ".generation-lease.json").exists():
        return False
    with _GenerationLease(
        witness.output,
        witness.queue_sha256,
        process_checker=process_is_alive,
        expected_previous=witness.lease,
    ) as lease:
        identity = witness.output.stat()
        if (identity.st_dev, identity.st_ino) != witness.directory_identity:
            raise BulkGenerationError("Cancelled generation output identity changed")
        queue, queue_sha256 = load_stable_generation_queue(witness.queue)
        if queue_sha256 != witness.queue_sha256:
            raise BulkGenerationError("Cancelled generation queue changed")
        state_path = generation_state_path(witness.output)
        state = load_generation_state(state_path, witness.queue)
        lease.assert_owned()
        _assert_sources_unchanged(witness.queue, witness.queue_sha256, ())
        active = state.get("active")
        if _is_json_document(active):
            if active.get("generation_owner_pid") != process.pid:
                raise BulkGenerationError("Cancelled generation active owner changed")
            queue_id = _generation_text(active.get("queue_id"), "Cancelled queue ID")
            existing = _state_items(state).get(queue_id, {})
            if active.get("phase") != "retrying" and existing.get("status") not in {
                "generated",
                "approved",
            }:
                voice = _generation_text(
                    active.get("synthesis_voice_character"), "Cancelled synthesis voice"
                )
                audio = _within(
                    witness.output,
                    _audio_relative_path(voice, queue_id),
                    "Cancelled audio",
                )
                for artifact in (audio.with_suffix(".partial.wav"), audio):
                    if artifact.exists() or artifact.is_symlink():
                        _archive_interrupted_artifact(witness.output, artifact)
            _reconcile_interrupted_attempt(
                state_path,
                state,
                queue,
                cancellation={
                    "owner_pid": process.pid,
                    "owner_started_at": witness.lease["process_started_at"],
                    "lease_id": witness.lease["lease_id"],
                    "queue_sha256": witness.queue_sha256,
                    "requested_at": witness.requested_at,
                    "returncode": process.returncode,
                },
            )
        lease.assert_owned()
        publish_generated_manifest(state_path, _lease_held=True)
        write_runtime_progress_manifest_from_state(
            state, witness.output, witness.output / RUNTIME_PROGRESS_MANIFEST_NAME
        )
    return True


def _load_or_create_state(
    state_path: Path,
    output_directory: Path,
    queue: VoiceGenerationQueue,
    queue_sha256: str,
) -> JsonDocument:
    try:
        require_registered_output(output_directory)
    except (OSError, ValueError) as error:
        raise BulkGenerationError(str(error)) from error
    if state_path.is_file():
        state = read_generation_state_document(state_path)
        _validate_state_document(state, output_directory, queue, queue_sha256)
        return state
    state = {
        "schema": STATE_SCHEMA,
        "schema_version": STATE_VERSION,
        "queue_sha256": queue_sha256,
        "game": queue.metadata.get("game"),
        "language": queue.metadata.get("language"),
        "items": {},
        "active": None,
        "synthesis_controls": {},
    }
    write_generation_state(state_path, state)
    return state


_validate_success_file = validate_success_file


def _reconcile_interrupted_attempt(
    state_path: Path,
    state: JsonDocument,
    queue: VoiceGenerationQueue,
    *,
    cancellation: JsonDocument | None = None,
) -> None:
    active = state.get("active")
    if not _is_json_document(active):
        return
    queue_id = active.get("queue_id")
    if not isinstance(queue_id, str) or queue_id not in {
        item.queue_id for item in queue.items
    }:
        raise BulkGenerationError(
            "Interrupted attempt references an unknown queue item"
        )
    interrupted = dict(active)
    interrupted["detected_at"] = _now()
    if cancellation is not None:
        interrupted["cancellation"] = cancellation
        interrupted["attempt_counted"] = active.get("phase") == "retrying"
    interrupted_attempts = state.get("interrupted_attempts")
    if interrupted_attempts is None:
        interrupted_attempts = []
        state["interrupted_attempts"] = interrupted_attempts
    if not _is_object_list(interrupted_attempts):
        raise BulkGenerationError("Interrupted generation attempts are malformed")
    interrupted_attempts.append(interrupted)
    items = state.get("items")
    if not _is_json_document(items):
        raise BulkGenerationError("Generation state items are malformed")
    existing_value = items.get(queue_id, {})
    existing = existing_value if _is_json_document(existing_value) else {}
    if cancellation is not None and active.get("phase") == "retrying":
        if (
            generation_item_status(existing) != "failed"
            or existing.get("attempts") != active.get("total_attempts")
            or any(
                existing.get(field) != active.get(field)
                for field in (
                    "provider",
                    "model",
                    "generation_profile",
                    "seed",
                    "synthesis_provenance_sha256",
                )
            )
        ):
            raise BulkGenerationError(
                "Retrying cancellation has no matching completed failure"
            )
        state["active"] = None
        write_generation_state(state_path, state)
        return
    if existing.get("status") not in {"generated", "approved"}:
        attempts = max(
            _nonnegative_int(existing.get("attempts", 0), "Attempts"),
            _nonnegative_int(active.get("total_attempts", 0), "Active attempts"),
        )
        interrupted_result = {
            "status": "failed",
            "attempts": attempts,
            "seed": active.get("seed"),
            "last_error": "Interrupted generation attempt",
            "failure": {
                "schema_version": 1,
                "kind": "interrupted",
                "error_type": "InterruptedGenerationAttempt",
                "text_features": _text_failure_features(
                    active["text"] if isinstance(active.get("text"), str) else ""
                ),
            },
            "updated_at": _now(),
        }
        if cancellation is not None:
            attempts = _nonnegative_int(
                active.get("total_attempts"), "Cancelled attempts"
            )
            provider = _generation_text(active.get("provider"), "Cancelled provider")
            providers = _provider_attempts(active, attempts, default_provider=provider)
            if attempts < 1 or providers.get(provider, 0) < 1:
                raise BulkGenerationError(
                    "Cancelled generation attempt counts are unavailable"
                )
            attempts -= 1
            providers[provider] -= 1
            previous_total = _nonnegative_int(
                existing.get("attempts", 0), "Previous attempts"
            )
            previous_providers = _provider_attempts(
                existing, previous_total, default_provider=provider
            )
            if completed_generation_attempts(existing) > attempts or any(
                providers.get(name, 0) < count
                for name, count in previous_providers.items()
            ):
                raise BulkGenerationError(
                    "Cancelled generation cannot refund a completed failure"
                )
            failure: JsonDocument = {
                "schema_version": 1,
                "kind": "cancelled",
                "completion": SynthesisCompletion.CANCELLED.value,
                "error_type": "CancelledGenerationProcess",
                "attempt_counted": False,
                "text_features": _text_failure_features(
                    active["text"] if isinstance(active.get("text"), str) else ""
                ),
            }
            previous: object = existing
            if generation_item_status(existing) == "cancelled":
                stored = existing.get("failure")
                previous = (
                    stored.get("previous_failure") if isinstance(stored, dict) else None
                )
            if isinstance(previous, dict) and previous.get("status") == "failed":
                failure["previous_failure"] = copy.deepcopy(previous)
            interrupted_result.update(
                attempts=attempts,
                attempts_by_provider=providers,
                last_error="Generation cancelled by user",
                failure=failure,
            )
            if _is_json_document(existing.get("carry_forward")):
                interrupted_result["carry_forward"] = copy.deepcopy(
                    existing["carry_forward"]
                )
        for field in (
            "provider",
            "model",
            "generation_profile",
            "requested_voice_character",
            "synthesis_voice_character",
            "synthesis_fallback",
            "narrator_character",
            "prompt_sha256",
            "prompt_applied",
            "queue_annotations_sha256",
            "synthesis_text_sha256",
            "text_transform",
            "synthesis_provenance_sha256",
            "synthesis_configuration",
            "source_reference_binding",
            "failure_repair",
            "seed_applied",
        ):
            if field in active:
                interrupted_result[field] = active[field]
        if "synthesis_voice_character" in interrupted_result:
            interrupted_result["voice_character"] = interrupted_result.pop(
                "synthesis_voice_character"
            )
        items[queue_id] = interrupted_result
    state["active"] = None
    write_generation_state(state_path, state)


def _runtime_worker_pid(backend: object) -> int | None:
    # In-process generation has no separate worker; never label its owner as one.
    try:
        for name in ("process", "server"):
            process = getattr(backend, name, None)
            pid = getattr(process, "pid", None)
            poll = getattr(process, "poll", None)
            if type(pid) is int and pid > 0 and callable(poll) and poll() is None:
                return pid
    except Exception:
        pass
    return None


def _write_active(
    state_path: Path,
    state: JsonDocument,
    item: VoiceGenerationQueueItem,
    *,
    provider: str,
    model: str,
    generation_profile: str,
    prompt_sha256: str,
    queue_annotations_sha256: str,
    synthesis_text_sha256: str,
    text_transform_id: str | None,
    synthesis_provenance_sha256: str,
    synthesis_configuration: JsonDocument,
    synthesis_voice_character: str,
    synthesis_fallback: JsonDocument | None,
    source_reference_binding: JsonDocument | None,
    failure_repair: JsonDocument | None,
    phase: str,
    attempt: int,
    attempt_limit: int,
    total_attempts: int,
    provider_attempt: int,
    attempts_by_provider: Mapping[str, int],
    seed: int,
    seed_applied: bool,
    started_at: str,
    last_error: str | None,
    runtime_status: str | None = None,
    runtime_worker_pid: int | None = None,
    controls_unchanged: bool = False,
) -> None:
    active: JsonDocument = {
        "queue_id": item.queue_id,
        "line_id": item.line_id,
        "text_sha256": item.text_sha256,
        "speaker": item.speaker,
        "voice_character": item.voice_character,
        "requested_voice_character": synthesis_character_for_line(
            item.speaker, item.voice_character
        ),
        "synthesis_voice_character": synthesis_voice_character,
        "text": item.text,
        "phase": phase,
        "runtime_status": runtime_status,
        "generation_owner_pid": os.getpid(),
        "runtime_worker_pid": runtime_worker_pid,
        "attempt": attempt,
        "attempt_limit": attempt_limit,
        "total_attempts": total_attempts,
        "provider_attempt": provider_attempt,
        "attempts_by_provider": dict(sorted(attempts_by_provider.items())),
        "seed": seed,
        "seed_applied": seed_applied,
        "provider": provider,
        "model": model,
        "generation_profile": generation_profile,
        "prompt_sha256": prompt_sha256,
        "prompt_applied": False,
        "queue_annotations_sha256": queue_annotations_sha256,
        "synthesis_text_sha256": synthesis_text_sha256,
        "text_transform": text_transform_id,
        "synthesis_provenance_sha256": synthesis_provenance_sha256,
        "synthesis_configuration": synthesis_configuration,
        "started_at": started_at,
        "updated_at": _now(),
        "last_error": last_error,
    }
    state["active"] = active
    if synthesis_fallback is not None:
        active["synthesis_fallback"] = synthesis_fallback
        active["narrator_character"] = synthesis_fallback["narrator_character"]
    if source_reference_binding is not None:
        active["source_reference_binding"] = source_reference_binding
    if failure_repair is not None:
        active["failure_repair"] = failure_repair
    write_generation_state(
        state_path, state, changed_items=(), controls_unchanged=controls_unchanged
    )


def _write_active_phase(
    state_path: Path,
    state: JsonDocument,
    phase: str,
    *,
    last_error: str | None = None,
    changed_items: tuple[str, ...] = (),
    controls_unchanged: bool = False,
) -> None:
    active = state.get("active")
    if not _is_json_document(active):
        raise BulkGenerationError("Generation active attempt was lost")
    active["phase"] = phase
    active["updated_at"] = _now()
    if last_error is not None:
        active["last_error"] = last_error
    write_generation_state(
        state_path,
        state,
        changed_items=changed_items,
        controls_unchanged=controls_unchanged,
    )


def _validate_render_result(
    result: SynthesisResult, request: SynthesisRequest, provider: str
) -> None:
    if result.completion is not SynthesisCompletion.COMPLETE:
        raise IncompleteSynthesisError(result)
    if type(result.sample_rate) is not int or result.sample_rate <= 0:
        raise BulkGenerationError("Typed render returned an invalid sample rate")
    if (
        result.diagnostics.seed != request.seed
        or result.diagnostics.generation_profile != request.generation_profile
    ):
        raise BulkGenerationProvenanceError(
            "Typed render diagnostics do not match the request"
        )
    if result.diagnostics.backend != provider:
        raise BulkGenerationProvenanceError(
            "Typed render backend diagnostics do not match the configured provider"
        )


def generated_mono_pcm(pcm: object) -> NDArray[np.float32]:
    """Normalize typed renderer PCM without flattening channels into time."""
    samples = np.asarray(pcm, dtype=np.float32)
    if samples.ndim == 1:
        mono = samples
    elif samples.ndim == 2 and samples.shape[1] in {1, 2}:
        mono = samples[:, 0] if samples.shape[1] == 1 else samples.mean(axis=1)
    else:
        raise BulkGenerationError(
            "Typed render PCM must be frames or frames-by-one/two channels"
        )
    if not np.isfinite(mono).all():
        raise BulkGenerationError("Typed render PCM contains non-finite samples")
    return np.asarray(mono, dtype=np.float32)


_generated_mono_pcm = generated_mono_pcm


def _audio_relative_path(voice: str, queue_id: str) -> Path:
    voice_slug = slugify(voice)
    if not isinstance(voice_slug, str) or not voice_slug:
        raise BulkGenerationError(
            f"Voice cannot form a safe audio directory: {voice!r}"
        )
    digest = hashlib.sha256(queue_id.encode("utf-8")).hexdigest()[:24]
    return Path("audio") / voice_slug / f"{digest}.wav"


_archive_interrupted_artifact = archive_interrupted_artifact


_load_stable_queue = load_stable_generation_queue


def snapshot_generation_control_files(
    control_files: Mapping[str, str | Path | tuple[str | Path, str]],
) -> list[_GenerationControl]:
    if not isinstance(control_files, dict):
        raise BulkGenerationError(
            "Generation control files must be a role/path mapping"
        )
    snapshots: list[_GenerationControl] = []
    for role, configured in sorted(control_files.items()):
        role = _required_text(role, "Control-file role")
        expected = None
        path = configured
        if isinstance(configured, tuple) and len(configured) == 2:
            path, expected = configured
        path = Path(path).expanduser().resolve()
        try:
            digest = sha256_control_path(path)
        except OSError as error:
            raise BulkGenerationError(
                f"Unable to read generation control {role!r} {path}: {error}"
            ) from error
        if expected is not None and digest != expected:
            raise BulkGenerationSourceChangedError(
                f"Generation control {role!r} changed before the run started"
            )
        directory_files = _control_directory_files(path) if path.is_dir() else []
        if directory_files and _control_directory_digest(directory_files) != digest:
            raise BulkGenerationSourceChangedError(
                f"Generation control {role!r} changed while it was inventoried"
            )
        snapshots.append(
            {
                "role": role,
                "path": path,
                "sha256": digest,
                "kind": "directory" if path.is_dir() else "file",
                "files": directory_files,
            }
        )
    return snapshots


_snapshot_control_files = snapshot_generation_control_files


def _control_directory_files(path: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    try:
        candidates = sorted(path.rglob("*"), key=lambda value: value.as_posix())
        for candidate in candidates:
            if not candidate.is_file():
                continue
            records.append(
                {
                    "path": candidate.relative_to(path).as_posix(),
                    "sha256": file_sha256(candidate, error_type=OSError),
                }
            )
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to inventory generation control directory {path}: {error}"
        ) from error
    return records


def _stored_control(control: _GenerationControl) -> dict[str, object]:
    record: dict[str, object] = {
        "role": control["role"],
        "kind": control["kind"],
        "path": str(control["path"]),
        "sha256": control["sha256"],
    }
    if control["kind"] == "directory":
        record["files"] = list(control["files"])
    return record


def _assert_sources_unchanged(
    queue_path: Path, queue_sha256: str, controls: Sequence[_GenerationControl]
) -> None:
    try:
        current_queue = sha256_file(queue_path)
    except OSError as error:
        raise BulkGenerationSourceChangedError(
            f"Generation queue became unreadable during the run: {error}"
        ) from error
    if current_queue != queue_sha256:
        raise BulkGenerationSourceChangedError(
            "Generation queue changed during the run; state was not published"
        )
    _assert_control_files_unchanged(controls)


def _assert_workspace_output_identity(
    output_directory: str | Path, identity: object
) -> None:
    if not isinstance(identity, dict) or set(identity) != {"path", "device", "inode"}:
        raise BulkGenerationSourceChangedError("Workspace output identity is malformed")
    path = Path(output_directory).expanduser()
    absolute = Path(os.path.abspath(os.fspath(path)))
    expected_path = identity["path"]
    if not isinstance(expected_path, str):
        raise BulkGenerationSourceChangedError("Workspace output identity is malformed")
    expected = Path(expected_path)
    if absolute != expected or path.is_symlink():
        raise BulkGenerationSourceChangedError(
            "Workspace output directory changed or leaves its workspace"
        )
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise BulkGenerationSourceChangedError(
            f"Workspace output directory became unavailable: {error}"
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != identity["device"]
        or metadata.st_ino != identity["inode"]
    ):
        raise BulkGenerationSourceChangedError(
            "Workspace output directory identity changed"
        )


@measure_generation_phase("controls-validation")
def _assert_control_files_unchanged(controls: Sequence[_GenerationControl]) -> None:
    for control in controls:
        try:
            digest = sha256_control_path(control["path"])
        except (BulkGenerationError, OSError) as error:
            raise BulkGenerationSourceChangedError(
                f"Generation control {control['role']!r} became unreadable: {error}"
            ) from error
        if digest != control["sha256"]:
            raise BulkGenerationSourceChangedError(
                f"Generation control {control['role']!r} changed during the run"
            )


def _load_json(path: str | Path, description: str) -> dict[str, object]:
    return load_json_object(
        path,
        description,
        error_type=BulkGenerationError,
        object_label=description.capitalize(),
    )


_canonical_sha256 = canonical_document_sha256


def _nonnegative_optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    parsed = _nonnegative_int(value, label)
    if not isinstance(parsed, int) or isinstance(parsed, bool):
        raise BulkGenerationError(f"{label} must be nonnegative")
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

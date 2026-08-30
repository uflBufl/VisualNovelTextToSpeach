"""Resumable device-independent generation from shared voice queues."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.text_utils import slugify
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
)

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
from vntts.authoring.generation_lease import LEASE_SCHEMA as LEASE_SCHEMA
from vntts.authoring.generation_lease import LEASE_VERSION as LEASE_VERSION
from vntts.authoring.generation_lease import (
    BulkGenerationError,
    GenerationLease,
    archive_interrupted_artifact,
    process_is_alive,
    process_started_at,
)
from vntts.authoring.generation_manifest import AudioQuality as AudioQuality
from vntts.authoring.generation_manifest import (
    approved_manifest_entries,
    inspect_generated_wav,
    validate_success_file,
    write_generated_manifest_from_state,
)
from vntts.authoring.generation_state import (
    FAILURE_KINDS as FAILURE_KINDS,
)
from vntts.authoring.generation_state import (
    LEGACY_STATE_SCHEMA as LEGACY_STATE_SCHEMA,
)
from vntts.authoring.generation_state import (
    LEGACY_STATE_VERSION as LEGACY_STATE_VERSION,
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
from vntts.authoring.generation_state import (
    contained_state_path as _within,
)
from vntts.authoring.generation_state import (
    control_directory_digest as _control_directory_digest,
)
from vntts.authoring.generation_state import (
    load_stable_generation_queue,
)
from vntts.authoring.generation_state import (
    provider_attempts as _provider_attempts,
)
from vntts.authoring.generation_state import (
    required_state_text as _required_text,
)
from vntts.authoring.generation_state import (
    safe_state_relative_path as _safe_relative,
)
from vntts.authoring.generation_state import (
    state_integer as _integer,
)
from vntts.authoring.generation_state import (
    state_nonnegative_int as _nonnegative_int,
)
from vntts.authoring.generation_state import (
    validate_failure_record as _validate_failure_record,
)
from vntts.authoring.generation_state import (
    validate_failure_repair_record as _validate_failure_repair_record,
)
from vntts.authoring.generation_state import (
    validate_generation_state_document as _validate_state_document,
)
from vntts.authoring.generation_state import (
    validate_generation_state_document as validate_generation_state_document,
)
from vntts.authoring.generation_state import (
    validate_live_fallback_evidence as _validate_live_fallback_evidence,
)
from vntts.authoring.generation_state import (
    validate_seed_application as _validate_seed_application,
)
from vntts.authoring.generation_state import (
    validate_success_item as _validate_success_item,
)
from vntts.authoring.generation_state import (
    validate_synthesis_identity as _validate_synthesis_identity,
)
from vntts.authoring.missing_voice_policy import (
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.silence_evidence import publish_silence_failure_evidence
from vntts.authoring.source_reference_bindings import queue_voice_overrides_sha256
from vntts.authoring.speech_quality import (
    LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION as LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION,
)
from vntts.authoring.speech_quality import (
    MAX_INTERNAL_SILENCE_SECONDS,
    MAX_LEADING_SILENCE_SECONDS,
    MAX_TRAILING_SILENCE_SECONDS,
    SpeechSilenceValidationError,
    inspect_generated_speech,
)
from vntts.authoring.speech_quality import (
    MAX_SILENCE_RATIO as MAX_SILENCE_RATIO,
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
    measure_generated_speech as measure_generated_speech,
)
from vntts.authoring.speech_quality import (
    text_failure_features as _text_failure_features,
)
from vntts.authoring.terminal_conflict_records import (
    TerminalConflictRecordError,
    validate_terminal_conflict_state_binding,
)
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
    SynthesisRequest,
)
from vntts.voices import synthesis_character_for_line

NO_PROMPT_SHA256 = hashlib.sha256(b"").hexdigest()
PURE_SOUND_EFFECT_PATTERN = re.compile(r'^\s*["“”]?\*[^*]+\*["“”]?[.!?]?\s*$')
SHORT_TRAILING_ELLIPSIS_PATTERN = re.compile(
    r"^\s*(?P<spoken>[\w'’]+(?:\s+[\w'’]+)?)\s*(?:\.{3}|…)\s*$"
)


class BulkGenerationSourceChangedError(BulkGenerationError):
    """A queue or synthesis control changed during a generation snapshot."""


class BulkGenerationProvenanceError(BulkGenerationError):
    """Typed render diagnostics contradict declared synthesis provenance."""


class IncompleteSynthesisError(BulkGenerationError):
    """A typed renderer stopped without producing a publishable completion."""

    def __init__(self, result):
        self.result = result
        super().__init__(
            "Typed render completed as "
            f"{result.completion.value} "
            f"(sample_count={result.diagnostics.sample_count}, "
            f"chunk_count={result.diagnostics.chunk_count}, "
            f"max_audio_seconds={result.limits.max_audio_seconds}, "
            f"max_tokens={result.limits.max_tokens}); WAV was not published"
        )


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

    def to_dict(self):
        payload = asdict(self)
        payload["state"] = str(self.state)
        payload["manifest"] = str(self.manifest)
        return payload


@dataclass(frozen=True)
class ReviewAuthority:
    """Exact immutable inputs that one human review decision applies to."""

    queue_sha256: str
    state_sha256: str
    item_sha256: str
    audio_sha256: str


@dataclass(frozen=True)
class ReviewCommit:
    """Durable result of one compare-and-swap review decision."""

    queue_id: str
    status: str
    review_status: str
    updated_at: str
    authority: ReviewAuthority


def generation_review_authority(state_path, queue_id):
    """Snapshot one reviewable state item and its exact validated WAV."""
    state_path = Path(state_path).expanduser().resolve()
    state = load_generation_state(state_path)
    item = state.get("items", {}).get(queue_id)
    if not isinstance(item, dict) or item.get("status") not in {
        "generated",
        "approved",
    }:
        raise BulkGenerationError(f"Generated queue item does not exist: {queue_id}")
    relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
    audio = _within(state_path.parent, relative, "Generated WAV")
    _validate_success_file(queue_id, item, audio)
    return ReviewAuthority(
        queue_sha256=state["queue_sha256"],
        state_sha256=sha256_file(state_path),
        item_sha256=_canonical_sha256(item),
        audio_sha256=sha256_file(audio),
    )


def _assert_review_authority(
    state_path,
    queue_id,
    expected_authority,
    queue_path,
):
    if not isinstance(expected_authority, ReviewAuthority):
        raise BulkGenerationError("Review authority snapshot is invalid")
    state, item, audio_bytes = _load_review_snapshot(
        state_path,
        queue_id,
        expected_authority,
        queue_path,
        capture_audio=True,
    )
    actual = ReviewAuthority(
        queue_sha256=state["queue_sha256"],
        state_sha256=expected_authority.state_sha256,
        item_sha256=_canonical_sha256(item),
        audio_sha256=hashlib.sha256(audio_bytes).hexdigest(),
    )
    if actual != expected_authority:
        raise BulkGenerationError(
            "Review authority changed after the item was displayed; refresh before deciding"
        )
    return state, item, audio_bytes


def _assert_review_authorities(state_path, authorities, queue_path):
    """Validate one cohort against one shared state and queue snapshot."""
    if not isinstance(authorities, dict) or not authorities:
        raise BulkGenerationError("Cohort review authorities must be a non-empty map")
    if any(not isinstance(value, ReviewAuthority) for value in authorities.values()):
        raise BulkGenerationError("Cohort review authority snapshot is invalid")
    expected_state = {value.state_sha256 for value in authorities.values()}
    expected_queue = {value.queue_sha256 for value in authorities.values()}
    if len(expected_state) != 1 or len(expected_queue) != 1:
        raise BulkGenerationError("Cohort review authorities do not share one snapshot")
    state_sha256 = next(iter(expected_state))
    queue_sha256 = next(iter(expected_queue))
    state_path = Path(state_path).expanduser().resolve()
    queue_path = Path(queue_path).expanduser().resolve()
    try:
        state_payload = state_path.read_bytes()
        queue_payload = queue_path.read_bytes()
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read cohort review controls: {error}"
        ) from error
    if hashlib.sha256(state_payload).hexdigest() != state_sha256:
        raise BulkGenerationError(
            "Review authority changed after the cohort was displayed; refresh before deciding"
        )
    if hashlib.sha256(queue_payload).hexdigest() != queue_sha256:
        raise BulkGenerationError(
            "Review queue changed after the cohort was displayed; refresh before deciding"
        )
    try:
        state = json.loads(state_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BulkGenerationError(
            f"Unable to read generation state {state_path}: {error}"
        ) from error
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("items"), dict)
        or state.get("queue_sha256") != queue_sha256
    ):
        raise BulkGenerationError("Generation state items or queue identity changed")
    snapshots = {}
    audio_paths = {}
    for queue_id, authority in authorities.items():
        queue_id = _required_text(queue_id, "Cohort review queue ID")
        item = state["items"].get(queue_id)
        if not isinstance(item, dict) or item.get("status") not in {
            "generated",
            "approved",
        }:
            raise BulkGenerationError(
                f"Generated queue item does not exist: {queue_id}"
            )
        if _canonical_sha256(item) != authority.item_sha256:
            raise BulkGenerationError(
                "Review authority changed after the cohort was displayed; refresh before deciding"
            )
        relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
        audio = _within(state_path.parent, relative, "Generated WAV")
        try:
            audio_bytes = audio.read_bytes()
        except OSError as error:
            raise BulkGenerationError(
                f"Generated WAV is unreadable for {queue_id!r}: {error}"
            ) from error
        if hashlib.sha256(audio_bytes).hexdigest() != authority.audio_sha256:
            raise BulkGenerationError(
                "Review authority changed after the cohort was displayed; refresh before deciding"
            )
        snapshots[queue_id] = (item, audio_bytes)
        audio_paths[queue_id] = audio
    if (
        sha256_file(state_path) != state_sha256
        or sha256_file(queue_path) != queue_sha256
    ):
        raise BulkGenerationError(
            "Review authority changed while the cohort was being validated"
        )
    for queue_id, authority in authorities.items():
        if sha256_file(audio_paths[queue_id]) != authority.audio_sha256:
            raise BulkGenerationError(
                "Review authority changed while the cohort was being validated"
            )
    return state, snapshots


def _load_review_snapshot(
    state_path,
    queue_id,
    expected_authority,
    queue_path,
    *,
    capture_audio,
):
    """Revalidate only the exact state/item/WAV snapshot displayed by the UI."""
    if not isinstance(expected_authority, ReviewAuthority):
        raise BulkGenerationError("Review authority snapshot is invalid")
    state_path = Path(state_path).expanduser().resolve()
    queue_path = None if queue_path is None else Path(queue_path).expanduser().resolve()
    try:
        state_payload = state_path.read_bytes()
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read generation state {state_path}: {error}"
        ) from error
    if hashlib.sha256(state_payload).hexdigest() != expected_authority.state_sha256:
        raise BulkGenerationError(
            "Review authority changed after the item was displayed; refresh before deciding"
        )
    try:
        state = json.loads(state_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BulkGenerationError(
            f"Unable to read generation state {state_path}: {error}"
        ) from error
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        raise BulkGenerationError("Generation state items must be an object")
    if state.get("queue_sha256") != expected_authority.queue_sha256:
        raise BulkGenerationError(
            "Review authority changed after the item was displayed; refresh before deciding"
        )
    if queue_path is None or sha256_file(queue_path) != expected_authority.queue_sha256:
        raise BulkGenerationError(
            "Review queue changed after the item was displayed; refresh before deciding"
        )
    item = state["items"].get(queue_id)
    if not isinstance(item, dict) or item.get("status") not in {
        "generated",
        "approved",
    }:
        raise BulkGenerationError(f"Generated queue item does not exist: {queue_id}")
    if _canonical_sha256(item) != expected_authority.item_sha256:
        raise BulkGenerationError(
            "Review authority changed after the item was displayed; refresh before deciding"
        )
    relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
    audio = _within(state_path.parent, relative, "Generated WAV")
    try:
        audio_bytes = audio.read_bytes()
    except OSError as error:
        raise BulkGenerationError(
            f"Generated WAV is unreadable for {queue_id!r}: {error}"
        ) from error
    if hashlib.sha256(audio_bytes).hexdigest() != expected_authority.audio_sha256:
        raise BulkGenerationError(
            "Review authority changed after the item was displayed; refresh before deciding"
        )
    return state, item, audio_bytes if capture_audio else b""


def load_review_audio_bytes(state_path, queue_path, queue_id, expected_authority):
    """Read the exact selected WAV bytes without rescanning unrelated outcomes."""
    _state, _item, audio_bytes = _load_review_snapshot(
        state_path,
        queue_id,
        expected_authority,
        queue_path,
        capture_audio=True,
    )
    return audio_bytes


def sha256_control_path(path):
    """Hash one immutable synthesis control file or complete directory tree."""
    try:
        path = Path(path).expanduser().resolve()
        if path.is_file():
            return sha256_file(path)
        if not path.is_dir():
            raise BulkGenerationError(f"Generation control does not exist: {path}")
        digest = hashlib.sha256()
        for candidate in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(path).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(bytes.fromhex(sha256_file(candidate)))
        return digest.hexdigest()
    except BulkGenerationError:
        raise
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read generation control {path}: {error}"
        ) from error


def is_spoken_queue_item(item):
    """Skip pure or inline audio events until a typed composition is approved."""
    document = item.document if hasattr(item, "document") else item
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


def normalize_short_trailing_ellipsis(text):
    """Give one/two-word ellipses an audible terminal boundary for MOSS."""
    match = SHORT_TRAILING_ELLIPSIS_PATTERN.fullmatch(str(text or ""))
    return str(text) if match is None else match.group("spoken") + "."


def audio_event_spoken_projection(text):
    """Remove typed inline events while preserving the record's spoken text."""
    try:
        plan = audio_event_plan_for_record({"text": text})
    except ValueError as error:
        raise BulkGenerationError(str(error)) from error
    if (
        not isinstance(plan, dict)
        or not plan.get("requires_composition")
        or not isinstance(plan.get("spoken_text"), str)
        or not plan["spoken_text"].strip()
        or plan["spoken_text"] == text
    ):
        raise BulkGenerationError(
            "Audio-event spoken projection requires mixed speech and events"
        )
    return plan["spoken_text"]


def _failure_kind(error, completion=None):
    if completion is SynthesisCompletion.CANCELLED:
        return "cancelled"
    if isinstance(error, IncompleteSynthesisError):
        return (
            "cancelled"
            if error.result.completion is SynthesisCompletion.CANCELLED
            else "missed_eos_audio_limit"
        )
    if isinstance(error, SpeechSilenceValidationError):
        return "speech_silence"
    value = str(error or "").casefold()
    if "interrupted" in value:
        return "interrupted"
    if "reference" in value and (
        "missing" in value or "unavailable" in value or "no reference" in value
    ):
        return "reference_unavailable"
    if "limited" in value or " limit" in value or "before eos" in value:
        return "missed_eos_audio_limit"
    if "silence" in value:
        return "speech_silence"
    return "backend_error"


def _failure_record(error, *, text, completion=None, attempt_binding=None):
    record = {
        "schema_version": 1,
        "kind": _failure_kind(error, completion),
        "error_type": error.__class__.__name__,
        "text_features": _text_failure_features(text),
    }
    result = error.result if isinstance(error, IncompleteSynthesisError) else None
    if result is not None:
        sample_count = result.diagnostics.sample_count
        max_audio_seconds = result.limits.max_audio_seconds
        utilization = None
        if (
            isinstance(sample_count, int)
            and not isinstance(sample_count, bool)
            and isinstance(result.sample_rate, int)
            and not isinstance(result.sample_rate, bool)
            and result.sample_rate > 0
            and isinstance(max_audio_seconds, (int, float))
            and not isinstance(max_audio_seconds, bool)
            and max_audio_seconds > 0
        ):
            utilization = round(
                sample_count / result.sample_rate / max_audio_seconds,
                6,
            )
        record["completion"] = result.completion.value
        record["render"] = {
            "sample_count": sample_count,
            "chunk_count": result.diagnostics.chunk_count,
            "max_audio_seconds": max_audio_seconds,
            "max_tokens": result.limits.max_tokens,
            "audio_limit_utilization": utilization,
        }
    elif completion is not None:
        record["completion"] = completion.value
    if isinstance(error, SpeechSilenceValidationError):
        record["speech_quality"] = asdict(error.quality)
        record["silence_failures"] = list(error.failures)
        if error.diagnosis is not None:
            record["pause_diagnosis"] = asdict(error.diagnosis)
            record["pause_diagnosis"]["attempt_binding"] = copy.deepcopy(
                attempt_binding
            )
    return record


def normalized_failure_record(item, *, text=""):
    """Return a typed failure record, inferring legacy string-only outcomes."""
    stored = item.get("failure") if isinstance(item, dict) else None
    if isinstance(stored, dict) and stored.get("kind") in FAILURE_KINDS:
        return copy.deepcopy(stored)
    error = str(item.get("last_error") or "Unknown generation failure")
    return {
        "schema_version": 1,
        "kind": _failure_kind(error),
        "error_type": "LegacyStringFailure",
        "text_features": _text_failure_features(text),
        "inferred_from_legacy_error": True,
    }


def _generation_voice_overrides(
    policy_document, synthesis_character_overrides, *, narrator_character
):
    try:
        policy = (
            policy_document
            if isinstance(policy_document, MissingVoicePolicy)
            else MissingVoicePolicy.from_document(policy_document)
        )
    except MissingVoicePolicyError as error:
        raise BulkGenerationError(str(error)) from error
    if synthesis_character_overrides is None:
        synthesis_character_overrides = {}
    if not isinstance(synthesis_character_overrides, dict):
        raise BulkGenerationError("Synthesis character overrides must be an object")
    normalized = {}
    source_names = {}
    for requested, effective in synthesis_character_overrides.items():
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
    if normalized:
        _required_text(narrator_character, "Narrator character")
    elif narrator_character is not None:
        _required_text(narrator_character, "Narrator character")
    return policy, normalized


def _validated_queue_voice_overrides(value, queue):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BulkGenerationError("Queue voice overrides must be an object")
    known = {item.queue_id for item in queue.items}
    parsed = {}
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
    requested_voice, effective_voice, *, policy, narrator_character
):
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
    controls, character_overrides, *, narrator_character
):
    if not character_overrides:
        return
    manifest_control = next(
        (value for value in controls if value["role"] == "voice_manifest"), None
    )
    if manifest_control is None or manifest_control["kind"] != "file":
        raise BulkGenerationError(
            "Narrator fallback requires a bound voice_manifest control"
        )
    path = manifest_control["path"]
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read fallback voice manifest: {error}"
        ) from error
    if hashlib.sha256(payload).hexdigest() != manifest_control["sha256"]:
        raise BulkGenerationSourceChangedError(
            "Voice manifest changed while fallback roles were validated"
        )
    try:
        with TemporaryDirectory(prefix="vntts-voice-manifest-") as directory:
            snapshot = Path(directory) / "manifest.json"
            snapshot.write_bytes(payload)
            _manifest, entries = load_voice_manifest(snapshot)
    except (OSError, VoiceManifestError) as error:
        raise BulkGenerationError(str(error)) from error
    indexed = {}
    for entry in entries:
        for name in (entry.character, entry.speaker, *entry.aliases):
            key = normalize_character_name(name)
            if key:
                indexed[key] = entry
    for requested in character_overrides:
        entry = indexed.get(requested)
        if entry is not None and entry.references:
            raise BulkGenerationError(
                f"Narrator fallback role {requested!r} still has configured references"
            )
    narrator = indexed.get(normalize_character_name(narrator_character))
    if narrator is None or not narrator.references:
        raise BulkGenerationError(
            f"Narrator character {narrator_character!r} has no configured reference"
        )


def load_generation_state(state_path, queue_path=None):
    """Load either VNTTS-owned or preserved legacy state and verify its files."""
    state_path = Path(state_path).expanduser().resolve()
    state = _load_json(state_path, "generation state")
    queue = None
    queue_sha256 = state.get("queue_sha256")
    if queue_path is not None:
        queue_path = Path(queue_path).expanduser().resolve()
        try:
            queue = VoiceGenerationQueue.load(queue_path)
        except VoiceGenerationQueueError as error:
            raise BulkGenerationError(str(error)) from error
        queue_sha256 = sha256_file(queue_path)
    _validate_state_document(state, state_path.parent, queue, queue_sha256)
    return state


def generation_failure_report(state_path, queue_path):
    """Project current and legacy failures into stable, actionable cohorts."""
    state_path = Path(state_path).expanduser().resolve()
    queue_path = Path(queue_path).expanduser().resolve()
    state = load_generation_state(state_path, queue_path)
    try:
        queue = VoiceGenerationQueue.load(queue_path)
    except VoiceGenerationQueueError as error:
        raise BulkGenerationError(str(error)) from error
    queue_by_id = {item.queue_id: item for item in queue.items}
    records = []
    for queue_id, result in state["items"].items():
        if not isinstance(result, dict) or result.get("status") != "failed":
            continue
        item = queue_by_id[queue_id]
        requested = synthesis_character_for_line(item.speaker, item.voice_character)
        failure = normalized_failure_record(result, text=item.text)
        attempts = _nonnegative_int(
            result.get("attempts", 0), f"State item {queue_id!r} attempts"
        )
        records.append(
            {
                "queue_id": queue_id,
                "line_id": item.line_id,
                "speaker": item.speaker,
                "text": item.text,
                "requested_voice_character": result.get(
                    "requested_voice_character", requested
                ),
                "synthesis_voice_character": result.get("voice_character", requested),
                "provider": result.get("provider"),
                "model": result.get("model"),
                "generation_profile": result.get("generation_profile"),
                "synthesis_control_digest": result.get("synthesis_provenance_sha256"),
                "attempts": attempts,
                "attempts_by_provider": _provider_attempts(
                    result, attempts, default_provider=result.get("provider")
                ),
                "seed": result.get("seed"),
                "last_error": result.get("last_error"),
                "failure": failure,
                "failure_repair": copy.deepcopy(result.get("failure_repair")),
                "failure_repair_history": _failure_repair_history(result),
            }
        )
    records.sort(key=lambda value: value["queue_id"])

    def counts(key):
        grouped = {}
        for record in records:
            value = key(record)
            serialized = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            grouped.setdefault(serialized, {"value": value, "count": 0})["count"] += 1
        return sorted(
            grouped.values(),
            key=lambda value: (
                -value["count"],
                json.dumps(value["value"], sort_keys=True),
            ),
        )

    return {
        "schema": "vntts.authoring-generation-failure-report",
        "schema_version": 1,
        "state": str(state_path),
        "state_sha256": sha256_file(state_path),
        "queue": str(queue_path),
        "queue_sha256": state["queue_sha256"],
        "failure_count": len(records),
        "cohorts": {
            "kind": counts(lambda value: value["failure"]["kind"]),
            "role": counts(
                lambda value: {
                    "speaker": value["speaker"],
                    "requested_voice_character": value["requested_voice_character"],
                    "synthesis_voice_character": value["synthesis_voice_character"],
                }
            ),
            "backend": counts(
                lambda value: {
                    "provider": value["provider"],
                    "model": value["model"],
                    "generation_profile": value["generation_profile"],
                }
            ),
            "synthesis_control": counts(
                lambda value: value["synthesis_control_digest"]
            ),
            "attempt_seed": counts(
                lambda value: {
                    "attempts": value["attempts"],
                    "attempts_by_provider": value["attempts_by_provider"],
                    "seed": value["seed"],
                }
            ),
            "text_shape": counts(lambda value: value["failure"]["text_features"]),
            "limit_utilization": counts(lambda value: value["failure"].get("render")),
            "silence": counts(lambda value: value["failure"].get("speech_quality")),
        },
        "records": records,
    }


def generation_failure_repair_plan(state_path, queue_path):
    """Return deterministic exact-ID repair candidates without changing state."""
    report = generation_failure_report(state_path, queue_path)
    planned = []
    for record in report["records"]:
        failure = record["failure"]
        kind = failure["kind"]
        attempts = record["attempts"]
        previous_repair = record.get("failure_repair")
        previous_strategy = (
            previous_repair.get("strategy")
            if isinstance(previous_repair, dict)
            else None
        )
        repair_history = record.get("failure_repair_history", [])
        if not _failure_has_bound_synthesis_controls(record):
            action = "provenance_recovery_or_regeneration"
            reason = (
                "legacy failure lacks exact provider, model, generation profile or "
                "synthesis-control provenance; recover immutable evidence or regenerate "
                "under current controls before selecting a repair"
            )
        elif (
            previous_strategy == BOUNDED_SEED_RETRY and kind == "missed_eos_audio_limit"
        ):
            provider_attempts = record["attempts_by_provider"].get(
                record["provider"], attempts
            )
            if provider_attempts < MAX_BOUNDED_TOTAL_ATTEMPTS:
                action = "bounded_seed_retry"
                reason = "the current-provider bounded repair still has an attempt"
            else:
                action = "offline_fallback_backend"
                reason = "the current-provider bounded repair is exhausted"
        elif previous_strategy == SENTENCE_BOUNDARY_SEGMENTATION:
            if kind == "missed_eos_audio_limit":
                provider_attempts = record["attempts_by_provider"].get(
                    record["provider"], attempts
                )
                if provider_attempts < MAX_BOUNDED_TOTAL_ATTEMPTS:
                    action = "bounded_seed_retry"
                    reason = (
                        "sentence segmentation failed, but one bounded "
                        "current-provider attempt remains"
                    )
                else:
                    action = "offline_fallback_backend"
                    reason = (
                        "bounded sentence segmentation failed and current-provider "
                        "attempts are exhausted"
                    )
            elif kind == "speech_silence":
                action = "reference_comparison"
                reason = (
                    "segmented output still failed the speech-silence gate and "
                    "requires listening and reference audit"
                )
            else:
                action = "backend_diagnosis"
                reason = (
                    "sentence segmentation already failed with an unrelated typed "
                    "backend outcome"
                )
        elif kind == "missed_eos_audio_limit":
            provider_attempts = record["attempts_by_provider"].get(
                record["provider"], attempts
            )
            if len(safe_sentence_segments(record["text"])) >= 2:
                action = "sentence_boundary_segmentation"
                reason = "multiple complete sentence boundaries"
            elif provider_attempts < MAX_BOUNDED_TOTAL_ATTEMPTS:
                action = "bounded_seed_retry"
                reason = "fewer than three completed attempts for the current provider"
            else:
                action = "offline_fallback_backend"
                reason = "current-provider bounded attempts are exhausted"
        elif kind == "speech_silence":
            quality = failure.get("speech_quality")
            edge_only = bool(
                isinstance(quality, dict)
                and (
                    quality.get("leading_silence_seconds", 0)
                    > MAX_LEADING_SILENCE_SECONDS
                    or quality.get("trailing_silence_seconds", 0)
                    > MAX_TRAILING_SILENCE_SECONDS
                )
                and quality.get("longest_internal_silence_seconds", 0)
                <= MAX_INTERNAL_SILENCE_SECONDS
            )
            if (
                SENTENCE_BOUNDARY_SEGMENTATION in repair_history
                and _sentence_repair_matches_failure(failure, record["text"])
            ):
                action = "reference_comparison"
                reason = (
                    "speech-silence remained after an earlier sentence-boundary "
                    "repair and requires listening or reference audit"
                )
            elif _sentence_repair_matches_failure(failure, record["text"]):
                action = "sentence_boundary_segmentation"
                reason = "internal silence between multiple complete sentences"
            elif _inline_pause_matches_failure(failure, record["text"]):
                provider_attempts = record["attempts_by_provider"].get(
                    record["provider"], attempts
                )
                if provider_attempts < MAX_BOUNDED_TOTAL_ATTEMPTS:
                    action = "inline_pause_marker_comparison"
                    reason = (
                        "sentence-boundary silence cannot use safe independent "
                        "segments and bounded attempts remain"
                    )
                else:
                    action = "offline_fallback_backend"
                    reason = "bounded inline-pause attempts are exhausted"
            elif edge_only:
                action = "edge_silence_trim"
                reason = "only measured boundary silence exceeds the speech gate"
            else:
                action = "reference_comparison"
                reason = (
                    "internal or untyped silence requires listening and reference audit"
                )
        elif kind == "reference_unavailable":
            action = "reference_discovery"
            reason = "no exact synthesis reference was available"
        elif kind in {"cancelled", "interrupted"}:
            action = "safe_resume"
            reason = "the attempt did not reach a terminal synthesis result"
        else:
            action = "backend_diagnosis"
            reason = "the typed backend failure has no safe automatic repair"
        planned.append(
            {
                "queue_id": record["queue_id"],
                "line_id": record["line_id"],
                "speaker": record["speaker"],
                "requested_voice_character": record["requested_voice_character"],
                "synthesis_voice_character": record["synthesis_voice_character"],
                "failure_kind": kind,
                "attempts": attempts,
                "seed": record["seed"],
                "attempted_repair_strategy": previous_strategy,
                "action": action,
                "reason": reason,
            }
        )
    action_counts = {}
    for record in planned:
        action_counts[record["action"]] = action_counts.get(record["action"], 0) + 1
    return {
        "schema": "vntts.authoring-generation-failure-repair-plan",
        "schema_version": 1,
        "state": report["state"],
        "state_sha256": report["state_sha256"],
        "queue": report["queue"],
        "queue_sha256": report["queue_sha256"],
        "failure_count": report["failure_count"],
        "action_counts": dict(sorted(action_counts.items())),
        "records": planned,
    }


def _failure_repair_history(result):
    """Return newest-first repair strategies proven by one outcome chain."""
    observed = []

    def remember(value):
        if isinstance(value, str) and value and value not in observed:
            observed.append(value)

    repair = result.get("failure_repair")
    if isinstance(repair, dict):
        remember(repair.get("strategy"))
    carry = result.get("carry_forward")
    visited = set()
    while isinstance(carry, dict):
        digest = _canonical_sha256(carry)
        if digest in visited:
            break
        visited.add(digest)
        remember(carry.get("source_repair_strategy"))
        carry = carry.get("source_parent_carry_forward")
    return observed


def _failure_has_bound_synthesis_controls(record):
    for field in ("provider", "model", "generation_profile"):
        value = record.get(field)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            return False
    digest = record.get("synthesis_control_digest")
    return isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None


def _validate_failure_repair_selection(
    policy, selected_queue_ids, state, queue, *, provider
):
    if policy.is_empty:
        return
    expected = set(policy.queue_ids)
    if selected_queue_ids != expected:
        raise BulkGenerationError("Failure-repair selection changed unexpectedly")
    queue_by_id = {item.queue_id: item for item in queue.items}
    for queue_id in policy.queue_ids:
        result = state["items"].get(queue_id)
        if not isinstance(result, dict) or result.get("status") != "failed":
            raise BulkGenerationError(
                f"Failure repair requires a current failed outcome for {queue_id!r}"
            )
        if not isinstance(result.get("failure"), dict):
            raise BulkGenerationError(
                f"Failure repair requires typed failure provenance for {queue_id!r}"
            )
        item = queue_by_id[queue_id]
        failure = normalized_failure_record(result, text=item.text)
        strategy = policy.strategy_for(queue_id)
        if strategy == SENTENCE_BOUNDARY_SEGMENTATION:
            previous_repair = result.get("failure_repair")
            if (
                isinstance(previous_repair, dict)
                and previous_repair.get("strategy") == SENTENCE_BOUNDARY_SEGMENTATION
            ):
                raise BulkGenerationError(
                    f"Sentence repair already failed for {queue_id!r}"
                )
            if not _sentence_repair_matches_failure(failure, item.text):
                raise BulkGenerationError(
                    f"Sentence repair no longer matches failure {queue_id!r}"
                )
        elif strategy == EDGE_SILENCE_TRIM:
            quality = failure.get("speech_quality")
            if not isinstance(quality, dict):
                raise BulkGenerationError(
                    f"Edge-silence repair requires typed speech metrics for {queue_id!r}"
                )
            for field in (
                "leading_silence_seconds",
                "trailing_silence_seconds",
                "longest_internal_silence_seconds",
            ):
                value = quality.get(field)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not np.isfinite(value)
                    or value < 0
                ):
                    raise BulkGenerationError(
                        f"Edge-silence repair metrics are invalid for {queue_id!r}"
                    )
            edge_exceeded = (
                quality.get("leading_silence_seconds", 0) > MAX_LEADING_SILENCE_SECONDS
                or quality.get("trailing_silence_seconds", 0)
                > MAX_TRAILING_SILENCE_SECONDS
            )
            if (
                not edge_exceeded
                or quality.get("longest_internal_silence_seconds", 0)
                > MAX_INTERNAL_SILENCE_SECONDS
            ):
                raise BulkGenerationError(
                    f"Edge-silence repair no longer matches failure {queue_id!r}"
                )
        elif strategy == BOUNDED_SEED_RETRY:
            attempts = _nonnegative_int(
                result.get("attempts", 0), f"State item {queue_id!r} attempts"
            )
            provider_attempts = _provider_attempts(
                result, attempts, default_provider=provider
            ).get(provider, 0)
            if (
                failure.get("kind") != "missed_eos_audio_limit"
                or result.get("provider") != provider
                or provider_attempts >= MAX_BOUNDED_TOTAL_ATTEMPTS
            ):
                raise BulkGenerationError(
                    f"Bounded seed repair no longer matches failure {queue_id!r}"
                )
        elif strategy == OFFLINE_FALLBACK_BACKEND:
            carry = _offline_fallback_source(result)
            attempts = _nonnegative_int(
                result.get("attempts", 0), f"State item {queue_id!r} attempts"
            )
            provider_attempts = _provider_attempts(
                result, attempts, default_provider=result.get("provider")
            ).get(provider, 0)
            if (
                not isinstance(carry, dict)
                or carry.get("mode") != "failed-outcome"
                or carry.get("source_provider") == provider
                or provider_attempts >= 1
            ):
                raise BulkGenerationError(
                    "Offline fallback lacks a different bound source backend or "
                    f"its single attempt is exhausted for {queue_id!r}"
                )
        elif strategy == INLINE_PAUSE_MARKER:
            attempts = _nonnegative_int(
                result.get("attempts", 0), f"State item {queue_id!r} attempts"
            )
            provider_attempts = _provider_attempts(
                result, attempts, default_provider=provider
            ).get(provider, 0)
            if (
                not _inline_pause_matches_failure(failure, item.text)
                or result.get("provider") != provider
                or provider_attempts >= MAX_BOUNDED_TOTAL_ATTEMPTS
            ):
                raise BulkGenerationError(
                    f"Inline pause repair no longer matches failure {queue_id!r}"
                )


def sentence_repair_matches_failure(failure, text):
    if len(safe_sentence_segments(text)) < 2:
        return False
    if failure.get("kind") == "missed_eos_audio_limit":
        return True
    if failure.get("kind") != "speech_silence":
        return False
    quality = failure.get("speech_quality")
    if not isinstance(quality, dict):
        return False
    values = {
        field: quality.get(field)
        for field in (
            "leading_silence_seconds",
            "trailing_silence_seconds",
            "longest_internal_silence_seconds",
        )
    }
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not np.isfinite(value)
        or value < 0
        for value in values.values()
    ):
        return False
    return bool(
        values["longest_internal_silence_seconds"] > MAX_INTERNAL_SILENCE_SECONDS
        and values["leading_silence_seconds"] <= MAX_LEADING_SILENCE_SECONDS
        and values["trailing_silence_seconds"] <= MAX_TRAILING_SILENCE_SECONDS
    )


_sentence_repair_matches_failure = sentence_repair_matches_failure


def inline_pause_matches_failure(failure, text):
    if failure.get("kind") != "speech_silence":
        return False
    try:
        _prompt, marker_count = inline_sentence_pause_prompt(text)
    except ValueError:
        return False
    quality = failure.get("speech_quality")
    if not isinstance(quality, dict) or marker_count < 1:
        return False
    values = {
        field: quality.get(field)
        for field in (
            "leading_silence_seconds",
            "trailing_silence_seconds",
            "longest_internal_silence_seconds",
        )
    }
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not np.isfinite(value)
        or value < 0
        for value in values.values()
    ):
        return False
    return bool(
        values["longest_internal_silence_seconds"] > MAX_INTERNAL_SILENCE_SECONDS
        and values["leading_silence_seconds"] <= MAX_LEADING_SILENCE_SECONDS
        and values["trailing_silence_seconds"] <= MAX_TRAILING_SILENCE_SECONDS
    )


_inline_pause_matches_failure = inline_pause_matches_failure


def _failure_repair_document(policy, queue_id, text, *, existing=None):
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


def _offline_fallback_source(result):
    if not isinstance(result, dict):
        return None
    carry = result.get("carry_forward")
    if isinstance(carry, dict) and carry.get("mode") == "failed-outcome":
        return carry
    repair = result.get("failure_repair")
    if (
        isinstance(repair, dict)
        and repair.get("strategy") == OFFLINE_FALLBACK_BACKEND
        and isinstance(repair.get("source_failure"), dict)
    ):
        return repair["source_failure"]
    return None


def run_bulk_generation(
    queue_path,
    output_directory,
    backend,
    *,
    provider,
    model,
    generation_profile="stable",
    limit=None,
    retries=2,
    include_prefer_source=False,
    include_characters=None,
    include_queue_ids=None,
    regenerate_existing=False,
    item_filter=None,
    seed=0,
    cancellation=None,
    control_files=None,
    text_transform=None,
    text_transform_id=None,
    process_checker=None,
    workspace_output_identity=None,
    synthesis_character_overrides=None,
    queue_voice_overrides=None,
    missing_voice_policy=None,
    narrator_character=None,
    failure_repair_policy=None,
    silence_failure_evidence=None,
    audio_event_spoken_projection_queue_ids=None,
):
    """Render selected queue items with no device playback and resumable state."""
    limit = _nonnegative_optional_int(limit, "Generation limit")
    retries = _nonnegative_int(retries, "Retry count")
    seed = _integer(seed, "Base seed")
    provider = _required_text(provider, "Provider")
    model = _required_text(model, "Model")
    generation_profile = _required_text(generation_profile, "Generation profile")
    policy, character_overrides = _generation_voice_overrides(
        missing_voice_policy,
        synthesis_character_overrides,
        narrator_character=narrator_character,
    )
    projection_queue_ids = tuple(
        sorted(
            {
                _required_text(value, "Audio-event spoken projection queue ID")
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
    if text_transform is not None and not callable(text_transform):
        raise BulkGenerationError("Text transform must be callable")
    if text_transform is not None:
        text_transform_id = _required_text(text_transform_id, "Text transform identity")
    elif text_transform_id is not None:
        raise BulkGenerationError("Text transform identity requires a text transform")
    render = getattr(backend, "render", None)
    if not callable(render):
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

    queue_path = Path(queue_path).expanduser().resolve()
    output_argument = Path(output_directory).expanduser()
    if workspace_output_identity is not None:
        _assert_workspace_output_identity(output_argument, workspace_output_identity)
    output_directory = output_argument.resolve()
    queue, queue_sha256 = _load_stable_queue(queue_path)
    queue_voice_overrides = _validated_queue_voice_overrides(
        queue_voice_overrides, queue
    )
    selected_queue_ids = None
    if include_queue_ids is not None:
        selected_queue_ids = {
            _required_text(value, "Selected queue ID") for value in include_queue_ids
        }
        known_queue_ids = {item.queue_id for item in queue.items}
        unknown_queue_ids = selected_queue_ids - known_queue_ids
        if unknown_queue_ids:
            raise BulkGenerationError(
                "Selected queue IDs are absent from the bound queue: "
                + ", ".join(sorted(unknown_queue_ids))
            )
    if not repair_policy.is_empty and selected_queue_ids != set(
        repair_policy.queue_ids
    ):
        raise BulkGenerationError(
            "Failure repair requires an exact --queue-id selection matching its policy"
        )
    if projection_queue_ids:
        if (
            selected_queue_ids != set(projection_queue_ids)
            or not repair_policy.is_empty
            or text_transform_id != "audio-event-spoken-projection-v1"
            or text_transform is not audio_event_spoken_projection
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
            projected = audio_event_spoken_projection(item.text)
            plan = audio_event_plan_for_record(item)
            if plan.get("spoken_text") != projected:
                raise BulkGenerationError(
                    f"Audio-event projection plan changed for {queue_id!r}"
                )
    evidence_directory = None
    if silence_failure_evidence is not None:
        evidence_directory = Path(silence_failure_evidence).expanduser()
        if not evidence_directory.name or evidence_directory.name in {".", ".."}:
            raise BulkGenerationError(
                "Silence-failure evidence requires a directory name"
            )
        if not evidence_directory.is_absolute():
            evidence_directory = Path.cwd() / evidence_directory
        evidence_directory = (
            evidence_directory.parent.resolve() / evidence_directory.name
        )
        try:
            evidence_directory.relative_to(output_directory)
        except ValueError:
            pass
        else:
            raise BulkGenerationError(
                "Silence-failure evidence must stay outside generated output"
            )
        if selected_queue_ids is None or len(selected_queue_ids) != 1 or retries != 0:
            raise BulkGenerationError(
                "Silence-failure evidence requires one exact queue ID and retries=0"
            )
        if evidence_directory.exists() or evidence_directory.is_symlink():
            raise BulkGenerationError(
                f"Silence-failure evidence destination already exists: {evidence_directory}"
            )
    if (
        regenerate_existing
        and selected_queue_ids is None
        and include_characters is None
    ):
        raise BulkGenerationError(
            "Regenerating existing outcomes requires explicit queue IDs or characters"
        )
    controls = _snapshot_control_files(control_files or {})
    _assert_missing_voice_overrides_match_manifest(
        controls,
        character_overrides,
        narrator_character=narrator_character,
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
    state_path = output_directory / "generation-state.json"
    manifest_path = output_directory / "manifest.json"
    output_directory.mkdir(parents=True, exist_ok=True)

    with _GenerationLease(
        output_directory,
        queue_sha256,
        process_checker=process_checker or process_is_alive,
    ) as lease:
        if workspace_output_identity is not None:
            _assert_workspace_output_identity(
                output_argument, workspace_output_identity
            )
        interrupted_job = _guard_job_process(
            output_directory, process_checker or process_is_alive
        )
        state = _load_or_create_state(state_path, output_directory, queue, queue_sha256)
        _validate_failure_repair_selection(
            repair_policy,
            selected_queue_ids,
            state,
            queue,
            provider=provider,
        )
        if state["schema"] == STATE_SCHEMA:
            registry = state.setdefault("synthesis_controls", {})
            existing_controls = registry.get(provenance_sha256)
            if existing_controls is not None and existing_controls != control_records:
                raise BulkGenerationProvenanceError(
                    "Stored synthesis controls conflict with this run"
                )
            if existing_controls is None:
                registry[provenance_sha256] = control_records
                atomic_write_json(state_path, state, sort_keys=True)
        if interrupted_job is not None:
            interrupted_processes = state.setdefault("interrupted_processes", [])
            if not any(
                isinstance(value, dict)
                and value.get("job_sha256") == interrupted_job["job_sha256"]
                for value in interrupted_processes
            ):
                interrupted_processes.append(interrupted_job)
                atomic_write_json(state_path, state, sort_keys=True)
        _reconcile_interrupted_attempt(state_path, state, queue)
        state.setdefault("game", queue.metadata.get("game"))
        state.setdefault("language", queue.metadata.get("language"))

        eligible_actions = {"generate"}
        if include_prefer_source:
            eligible_actions.add("prefer_source_audio")
        candidates = [item for item in queue.items if item.action in eligible_actions]
        skipped_actions = len(queue.items) - len(candidates)
        character_filter = (
            None if include_characters is None else set(include_characters)
        )
        skipped_characters = 0
        if character_filter is not None:
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
            filtered = [
                item for item in candidates if item.queue_id in selected_queue_ids
            ]
            skipped_items += len(candidates) - len(filtered)
            candidates = filtered
        if item_filter is not None:
            filtered = [item for item in candidates if item_filter(item)]
            skipped_items += len(candidates) - len(filtered)
            candidates = filtered
        if limit is not None:
            candidates = candidates[:limit]

        if regenerate_existing:
            protected = [
                item.queue_id
                for item in candidates
                if state["items"].get(item.queue_id, {}).get("status")
                in {"generated", "approved", "live_fallback"}
                and state["items"][item.queue_id].get("review_status")
                != "pending_review"
            ]
            if protected:
                raise BulkGenerationError(
                    "Regeneration cannot overwrite an approved or rejected decision: "
                    + ", ".join(protected)
                )

        generated = 0
        skipped_existing = 0
        cancelled = False
        captured_silence_failure = None
        for item in candidates:
            queue_id = item.queue_id
            existing = state["items"].get(queue_id, {})
            repair_strategy = repair_policy.strategy_for(queue_id)
            repair_document = _failure_repair_document(
                repair_policy, queue_id, item.text, existing=existing
            )
            if existing.get("status") == "live_fallback":
                if regenerate_existing:
                    raise BulkGenerationError(
                        "Regeneration cannot overwrite a terminal live fallback "
                        f"decision for {queue_id!r}"
                    )
                skipped_existing += 1
                continue
            if existing.get("status") in {"generated", "approved"}:
                _validate_success_item(
                    queue_id,
                    existing,
                    output_directory,
                    item,
                    state_schema=state["schema"],
                )
                if not regenerate_existing:
                    skipped_existing += 1
                    continue
                if existing.get("review_status") != "pending_review":
                    raise BulkGenerationError(
                        "Regeneration cannot overwrite an approved or rejected "
                        f"decision for {queue_id!r}"
                    )

            requested_voice = _required_text(
                synthesis_character_for_line(item.speaker, item.voice_character),
                f"Queue item {queue_id!r} voice",
            )
            voice = queue_voice_overrides.get(
                queue_id,
                character_overrides.get(
                    normalize_character_name(requested_voice), requested_voice
                ),
            )
            synthesis_fallback = (
                None
                if queue_id in queue_voice_overrides
                else _synthesis_fallback_document(
                    requested_voice,
                    voice,
                    policy=policy,
                    narrator_character=narrator_character,
                )
            )
            source_reference_binding = (
                {
                    "schema_version": 1,
                    "queue_id": queue_id,
                    "source_voice_character": requested_voice,
                    "synthesis_voice_character": voice,
                    "queue_voice_overrides_sha256": synthesis_configuration[
                        "queue_voice_overrides_sha256"
                    ],
                }
                if queue_id in queue_voice_overrides
                else None
            )
            queue_annotations_sha256 = _canonical_sha256(
                item.document.get("prompt_adapters") or {}
            )
            prompt_sha256 = NO_PROMPT_SHA256
            synthesis_text = (
                item.text if text_transform is None else text_transform(item.text)
            )
            if not isinstance(synthesis_text, str) or not synthesis_text.strip():
                raise BulkGenerationError(
                    f"Text transform returned no speech for queue item {queue_id!r}"
                )
            if repair_strategy == INLINE_PAUSE_MARKER:
                if synthesis_text != item.text:
                    raise BulkGenerationError(
                        "Inline pause comparison requires unchanged source text before "
                        f"marker insertion for {queue_id!r}"
                    )
                synthesis_text, marker_count = inline_sentence_pause_prompt(
                    synthesis_text, pause_ms=repair_policy.inline_pause_ms
                )
                if (
                    repair_document.get("marker_count") != marker_count
                    or repair_document.get("derived_prompt_sha256")
                    != hashlib.sha256(synthesis_text.encode("utf-8")).hexdigest()
                ):
                    raise BulkGenerationError(
                        f"Inline pause provenance changed for {queue_id!r}"
                    )
            synthesis_text_sha256 = hashlib.sha256(
                synthesis_text.encode("utf-8")
            ).hexdigest()
            relative = _audio_relative_path(voice, queue_id)
            if workspace_output_identity is not None:
                _assert_workspace_output_identity(
                    output_argument, workspace_output_identity
                )
            destination = _within(output_directory, relative, "Generated WAV")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                _archive_interrupted_artifact(output_directory, destination)
            attempts = _nonnegative_int(existing.get("attempts", 0), "Attempts")
            default_attempt_provider = provider
            if (
                regenerate_existing
                and existing.get("status") == "failed"
                and not existing.get("provider")
                and not existing.get("synthesis_provenance_sha256")
            ):
                default_attempt_provider = "legacy-unbound"
            attempts_by_provider = _provider_attempts(
                existing, attempts, default_provider=default_attempt_provider
            )
            provider_attempts = attempts_by_provider.get(provider, 0)
            run_attempts = 0
            attempt_limit = retries + 1
            if repair_strategy in {BOUNDED_SEED_RETRY, INLINE_PAUSE_MARKER}:
                attempt_limit = min(
                    attempt_limit,
                    MAX_BOUNDED_TOTAL_ATTEMPTS - provider_attempts,
                )
            last_error = str(existing.get("last_error") or "") or None
            while run_attempts < attempt_limit:
                attempts += 1
                provider_attempts += 1
                attempts_by_provider[provider] = provider_attempts
                run_attempts += 1
                attempt_seed = seed + provider_attempts - 1
                request_seed = None if provider == "pocket-tts" else attempt_seed
                attempt_repair = None
                if repair_document is not None:
                    attempt_repair = copy.deepcopy(repair_document)
                    if repair_strategy == SENTENCE_BOUNDARY_SEGMENTATION:
                        attempt_repair["planned_segment_seeds"] = [
                            attempt_seed + index
                            for index in range(len(attempt_repair["segments"]))
                        ]
                started_at = _now()
                partial = destination.with_suffix(".partial.wav")
                if partial.exists():
                    _archive_interrupted_artifact(output_directory, partial)
                _write_active(
                    state_path,
                    state,
                    item,
                    provider=provider,
                    model=model,
                    generation_profile=generation_profile,
                    prompt_sha256=prompt_sha256,
                    queue_annotations_sha256=queue_annotations_sha256,
                    synthesis_text_sha256=synthesis_text_sha256,
                    text_transform_id=text_transform_id,
                    synthesis_provenance_sha256=provenance_sha256,
                    synthesis_configuration=synthesis_configuration,
                    synthesis_voice_character=voice,
                    synthesis_fallback=synthesis_fallback,
                    source_reference_binding=source_reference_binding,
                    failure_repair=attempt_repair,
                    phase="generating",
                    attempt=run_attempts,
                    attempt_limit=attempt_limit,
                    total_attempts=attempts,
                    provider_attempt=provider_attempts,
                    attempts_by_provider=attempts_by_provider,
                    seed=attempt_seed,
                    seed_applied=request_seed is not None,
                    started_at=started_at,
                    last_error=last_error,
                )
                request = SynthesisRequest(
                    voice=voice,
                    text=synthesis_text,
                    seed=request_seed,
                    generation_profile=generation_profile,
                    cancellation=cancellation,
                    cache_policy=SynthesisCachePolicy.BYPASS,
                )
                try:
                    if repair_strategy == SENTENCE_BOUNDARY_SEGMENTATION:
                        rendered = render_sentence_segments(
                            render,
                            request,
                            safe_sentence_segments(synthesis_text),
                            pause_ms=repair_policy.segment_pause_ms,
                        )
                    else:
                        rendered = render(request).collect()
                    _validate_render_result(rendered, request, provider)
                    _assert_control_files_unchanged(controls)
                    if workspace_output_identity is not None:
                        _assert_workspace_output_identity(
                            output_argument, workspace_output_identity
                        )
                    lease.assert_owned()
                    _write_active_phase(state_path, state, "validating")
                    output_pcm = _generated_mono_pcm(rendered.pcm)
                    if repair_strategy == EDGE_SILENCE_TRIM:
                        trimmed = trim_excess_edge_silence(
                            output_pcm, rendered.sample_rate
                        )
                        output_pcm = trimmed.pcm
                        attempt_repair = {
                            **attempt_repair,
                            "leading_trimmed_samples": trimmed.leading_trimmed_samples,
                            "trailing_trimmed_samples": trimmed.trailing_trimmed_samples,
                        }
                    write_pcm16_wav(partial, output_pcm, rendered.sample_rate)
                    quality = inspect_generated_wav(partial)
                    speech_quality = inspect_generated_speech(
                        partial, text=synthesis_text
                    )
                    file_sha256 = sha256_file(partial)
                    _write_active_phase(state_path, state, "publishing")
                    if workspace_output_identity is not None:
                        _assert_workspace_output_identity(
                            output_argument, workspace_output_identity
                        )
                    os.replace(partial, destination)
                    state["items"][queue_id] = {
                        "status": "generated",
                        "review_status": "pending_review",
                        "attempts": attempts,
                        "attempts_by_provider": dict(
                            sorted(attempts_by_provider.items())
                        ),
                        "path": relative.as_posix(),
                        "line_id": item.line_id,
                        "text_sha256": item.text_sha256,
                        "file_sha256": file_sha256,
                        "provider": provider,
                        "model": model,
                        "prompt_sha256": prompt_sha256,
                        "prompt_applied": False,
                        "queue_annotations_sha256": queue_annotations_sha256,
                        "synthesis_text_sha256": synthesis_text_sha256,
                        "text_transform": text_transform_id,
                        "synthesis_provenance_sha256": provenance_sha256,
                        "synthesis_configuration": synthesis_configuration,
                        "seed": attempt_seed,
                        "seed_applied": request_seed is not None,
                        "generation_profile": generation_profile,
                        "speaker": item.speaker,
                        "requested_voice_character": requested_voice,
                        "voice_character": voice,
                        "quality": asdict(quality),
                        "speech_quality": asdict(speech_quality),
                        "updated_at": _now(),
                    }
                    if synthesis_fallback is not None:
                        state["items"][queue_id]["synthesis_fallback"] = (
                            synthesis_fallback
                        )
                        state["items"][queue_id]["narrator_character"] = (
                            narrator_character
                        )
                    if source_reference_binding is not None:
                        state["items"][queue_id]["source_reference_binding"] = (
                            source_reference_binding
                        )
                    if attempt_repair is not None:
                        state["items"][queue_id]["failure_repair"] = attempt_repair
                    if (
                        repair_strategy
                        in {
                            SENTENCE_BOUNDARY_SEGMENTATION,
                            INLINE_PAUSE_MARKER,
                            BOUNDED_SEED_RETRY,
                        }
                        and isinstance(existing.get("carry_forward"), dict)
                        and existing["carry_forward"].get("mode") == "failed-outcome"
                    ):
                        state["items"][queue_id]["carry_forward"] = copy.deepcopy(
                            existing["carry_forward"]
                        )
                    state["active"] = None
                    atomic_write_json(state_path, state, sort_keys=True)
                    generated += 1
                    break
                except (
                    BulkGenerationSourceChangedError,
                    BulkGenerationProvenanceError,
                ):
                    if partial.exists():
                        partial.unlink()
                    raise
                except Exception as error:
                    captured_partial = None
                    if (
                        evidence_directory is not None
                        and isinstance(error, SpeechSilenceValidationError)
                        and partial.is_file()
                        and not partial.is_symlink()
                    ):
                        captured_partial = partial.read_bytes()
                    if partial.exists():
                        partial.unlink()
                    completion = (
                        getattr(rendered, "completion", None)
                        if "rendered" in locals()
                        else None
                    )
                    is_cancelled = (
                        completion is SynthesisCompletion.CANCELLED
                        or request.cancellation_requested()
                    )
                    last_error = str(error) or error.__class__.__name__
                    state["items"][queue_id] = {
                        "status": "failed",
                        "attempts": attempts,
                        "attempts_by_provider": dict(
                            sorted(attempts_by_provider.items())
                        ),
                        "seed": attempt_seed,
                        "seed_applied": request_seed is not None,
                        "last_error": last_error,
                        "failure": _failure_record(
                            error,
                            text=synthesis_text,
                            completion=(
                                SynthesisCompletion.CANCELLED
                                if is_cancelled
                                else completion
                            ),
                            attempt_binding={
                                "provider": provider,
                                "model": model,
                                "generation_profile": generation_profile,
                                "seed": attempt_seed,
                                "synthesis_provenance_sha256": provenance_sha256,
                            },
                        ),
                        "provider": provider,
                        "model": model,
                        "generation_profile": generation_profile,
                        "speaker": item.speaker,
                        "requested_voice_character": requested_voice,
                        "voice_character": voice,
                        "prompt_sha256": prompt_sha256,
                        "prompt_applied": False,
                        "queue_annotations_sha256": queue_annotations_sha256,
                        "synthesis_text_sha256": synthesis_text_sha256,
                        "text_transform": text_transform_id,
                        "synthesis_provenance_sha256": provenance_sha256,
                        "synthesis_configuration": synthesis_configuration,
                        "updated_at": _now(),
                    }
                    if synthesis_fallback is not None:
                        state["items"][queue_id]["synthesis_fallback"] = (
                            synthesis_fallback
                        )
                        state["items"][queue_id]["narrator_character"] = (
                            narrator_character
                        )
                    if source_reference_binding is not None:
                        state["items"][queue_id]["source_reference_binding"] = (
                            source_reference_binding
                        )
                    if attempt_repair is not None:
                        state["items"][queue_id]["failure_repair"] = attempt_repair
                    if (
                        repair_strategy
                        in {
                            SENTENCE_BOUNDARY_SEGMENTATION,
                            INLINE_PAUSE_MARKER,
                            BOUNDED_SEED_RETRY,
                        }
                        and isinstance(existing.get("carry_forward"), dict)
                        and existing["carry_forward"].get("mode") == "failed-outcome"
                    ):
                        state["items"][queue_id]["carry_forward"] = copy.deepcopy(
                            existing["carry_forward"]
                        )
                    if run_attempts < attempt_limit and not is_cancelled:
                        _write_active_phase(
                            state_path, state, "retrying", last_error=last_error
                        )
                    else:
                        state["active"] = None
                        atomic_write_json(state_path, state, sort_keys=True)
                    if captured_partial is not None:
                        captured_silence_failure = {
                            "wav_payload": captured_partial,
                            "queue_id": queue_id,
                            "line_id": item.line_id,
                            "text": item.text,
                            "text_sha256": item.text_sha256,
                            "state_item": copy.deepcopy(state["items"][queue_id]),
                        }
                    if is_cancelled:
                        cancelled = True
                        break
                    if run_attempts >= attempt_limit:
                        break
                finally:
                    if "rendered" in locals():
                        del rendered
            if cancelled:
                break

        _assert_sources_unchanged(queue_path, queue_sha256, controls)
        if workspace_output_identity is not None:
            _assert_workspace_output_identity(
                output_argument, workspace_output_identity
            )
        lease.assert_owned()
        publish_generated_manifest(
            state_path, manifest_path=manifest_path, _lease_held=True
        )
        if captured_silence_failure is not None:
            publish_silence_failure_evidence(
                evidence_directory,
                captured_silence_failure["wav_payload"],
                {
                    "queue": str(queue_path),
                    "queue_sha256": queue_sha256,
                    "state": str(state_path),
                    "state_sha256": sha256_file(state_path),
                    "queue_id": captured_silence_failure["queue_id"],
                    "line_id": captured_silence_failure["line_id"],
                    "text": captured_silence_failure["text"],
                    "text_sha256": captured_silence_failure["text_sha256"],
                    "state_item": captured_silence_failure["state_item"],
                    "state_item_sha256": _canonical_sha256(
                        captured_silence_failure["state_item"]
                    ),
                    "synthesis_controls_sha256": provenance_sha256,
                },
            )
        failed = sum(
            value.get("status") == "failed" for value in state["items"].values()
        )
        return BulkGenerationResult(
            generated=generated,
            failed=failed,
            skipped_existing=skipped_existing,
            skipped_actions=skipped_actions,
            skipped_characters=skipped_characters,
            skipped_items=skipped_items,
            cancelled=cancelled,
            state=state_path,
            manifest=manifest_path,
        )


def publish_generated_manifest(state_path, *, manifest_path=None, _lease_held=False):
    """Rebuild the approved-only manifest from authoritative state."""
    state_path = Path(state_path).expanduser().resolve()
    output_directory = state_path.parent
    state = load_generation_state(state_path)
    if not _lease_held:
        with _GenerationLease(
            output_directory,
            state["queue_sha256"],
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


def validate_terminal_conflict_publication_authority(state_path, state):
    """Bind marked state to one fully validated canonical workspace ledger."""
    marked = any(
        isinstance(result, dict) and "terminal_conflict_resolution" in result
        for result in state.get("items", {}).values()
    )
    if not marked:
        return
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
            directory / "generated-audio" / "generation-state.json"
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


def validate_authoring_publication_authority(state_path, state):
    """Validate every reserved authoring provenance extension before projection."""
    validate_terminal_conflict_publication_authority(state_path, state)
    config_rebase = importlib.import_module("vntts.authoring.config_rebase")
    config_rebase.validate_config_rebase_publication_authority(state_path, state)


_validate_terminal_conflict_manifest_authority = (
    validate_authoring_publication_authority
)


_approved_manifest_entries = approved_manifest_entries


def review_generation_item(
    state_path,
    queue_id,
    decision,
    *,
    expected_authority=None,
    queue_path=None,
):
    """Persist approval/rejection, then rebuild the derived manifest."""
    if decision not in {"approved", "rejected"}:
        raise BulkGenerationError("Review decision must be approved or rejected")
    state_path = Path(state_path).expanduser().resolve()
    if expected_authority is None:
        initial = load_generation_state(state_path)
    else:
        initial, _item, _audio_bytes = _load_review_snapshot(
            state_path,
            queue_id,
            expected_authority,
            queue_path,
            capture_audio=False,
        )
    with _GenerationLease(
        state_path.parent,
        initial["queue_sha256"],
        process_checker=process_is_alive,
    ) as lease:
        return _review_generation_item_locked(
            state_path,
            queue_id,
            decision,
            expected_authority=expected_authority,
            queue_path=queue_path,
            lease=lease,
        )


def review_generation_cohort(
    state_path,
    queue_path,
    authorities,
    decision,
    *,
    provenance,
):
    """Commit one exact cohort decision in a single state transaction."""
    if not isinstance(authorities, dict) or not authorities:
        raise BulkGenerationError("Cohort review authorities must be a non-empty map")
    if isinstance(decision, str):
        decisions = {queue_id: decision for queue_id in authorities}
    elif isinstance(decision, dict):
        decisions = dict(decision)
    else:
        raise BulkGenerationError(
            "Cohort review decision must be approved, rejected, or an exact item map"
        )
    if set(decisions) != set(authorities) or any(
        value not in {"approved", "rejected", "pending_review"}
        for value in decisions.values()
    ):
        raise BulkGenerationError(
            "Cohort review item decisions must bind every authority to approved, "
            "rejected, or unchanged pending review"
        )
    if set(decisions.values()) == {"pending_review"}:
        raise BulkGenerationError("Cohort review decision does not change any item")
    if not isinstance(provenance, dict):
        raise BulkGenerationError("Cohort review provenance must be an object")
    state_path = Path(state_path).expanduser().resolve()
    queue_path = Path(queue_path).expanduser().resolve()
    authority_values = list(authorities.values())
    if any(not isinstance(value, ReviewAuthority) for value in authority_values):
        raise BulkGenerationError("Cohort review authority snapshot is invalid")
    if len({value.queue_sha256 for value in authority_values}) != 1:
        raise BulkGenerationError("Cohort review queue authorities do not match")
    if len({value.state_sha256 for value in authority_values}) != 1:
        raise BulkGenerationError("Cohort review state authorities do not match")
    with _GenerationLease(
        state_path.parent,
        authority_values[0].queue_sha256,
        process_checker=process_is_alive,
    ) as lease:
        state, snapshots = _assert_review_authorities(
            state_path, authorities, queue_path
        )
        for queue_id, (item, _audio) in snapshots.items():
            if (item.get("status"), item.get("review_status")) != (
                "generated",
                "pending_review",
            ):
                raise BulkGenerationError(
                    f"Cohort item is no longer pending review: {queue_id}"
                )
        proposed = copy.deepcopy(state)
        updated_at = _now()
        for queue_id, authority in authorities.items():
            item_decision = decisions[queue_id]
            if item_decision == "pending_review":
                continue
            proposed_item = proposed["items"][queue_id]
            proposed_item["review_status"] = item_decision
            proposed_item["status"] = (
                "approved" if item_decision == "approved" else "generated"
            )
            proposed_item["updated_at"] = updated_at
            proposed_item["cohort_review"] = {
                **copy.deepcopy(provenance),
                "projection_review_status": item_decision,
                "target_audio_sha256": authority.audio_sha256,
            }
        manifest_path = state_path.parent / "manifest.json"
        entries = _approved_manifest_entries(
            proposed,
            state_path.parent,
            validate_files=False,
        )
        _validate_cohort_approved_wavs(proposed, state_path.parent, decisions)
        transaction_id = secrets.token_hex(16)
        staged_state = state_path.with_name(f".{state_path.name}.{transaction_id}.tmp")
        staged_manifest = manifest_path.with_name(
            f".{manifest_path.name}.{transaction_id}.tmp"
        )
        staged_conservative_manifest = manifest_path.with_name(
            f".{manifest_path.name}.{transaction_id}.conservative.tmp"
        )
        is_mixed = len(set(decisions.values())) > 1
        try:
            atomic_write_json(staged_state, proposed, sort_keys=True)
            proposed_state_sha256 = sha256_file(staged_state)
            _write_generated_manifest_from_state(
                proposed,
                state_path.parent,
                staged_manifest,
                entries=entries,
                validate_files=False,
            )
            if is_mixed:
                conservative_entries = _approved_manifest_entries(
                    state,
                    state_path.parent,
                    validate_files=False,
                )
                _write_generated_manifest_from_state(
                    state,
                    state_path.parent,
                    staged_conservative_manifest,
                    entries=conservative_entries,
                    validate_files=False,
                )
            _assert_review_authorities(state_path, authorities, queue_path)
            validated_entries = _approved_manifest_entries(
                proposed,
                state_path.parent,
                validate_files=False,
            )
            if validated_entries != entries:
                raise BulkGenerationError(
                    "Approved cohort manifest authority changed before the final commit"
                )
            if sha256_file(state_path) != authority_values[0].state_sha256:
                raise BulkGenerationError(
                    "Cohort review state changed before the final commit"
                )
            if sha256_file(queue_path) != authority_values[0].queue_sha256:
                raise BulkGenerationError(
                    "Cohort review queue changed before the final commit"
                )
            lease.assert_owned()
            if is_mixed:
                # A mixed projection first publishes the conservative manifest,
                # then the exact per-item state, then its approved-only manifest.
                # A crash or ownership loss at either boundary can omit a new
                # approval temporarily, but can never publish a rejected WAV.
                try:
                    os.replace(staged_conservative_manifest, manifest_path)
                    lease.assert_owned()
                    _assert_review_authorities(state_path, authorities, queue_path)
                    lease.assert_owned()
                    os.replace(staged_state, state_path)
                    lease.assert_owned()
                    if sha256_file(state_path) != proposed_state_sha256:
                        raise BulkGenerationError(
                            "Mixed cohort state changed before manifest publication"
                        )
                    if sha256_file(queue_path) != authority_values[0].queue_sha256:
                        raise BulkGenerationError(
                            "Cohort review queue changed before manifest publication"
                        )
                    _validate_cohort_approved_wavs(
                        proposed, state_path.parent, decisions
                    )
                    current_entries = _approved_manifest_entries(
                        proposed,
                        state_path.parent,
                        validate_files=False,
                    )
                    if current_entries != entries:
                        raise BulkGenerationError(
                            "Mixed cohort manifest authority changed before publication"
                        )
                    lease.assert_owned()
                    os.replace(staged_manifest, manifest_path)
                except OSError as error:
                    raise BulkGenerationError(
                        "Mixed cohort review did not fully commit; the published "
                        f"manifest remains fail-closed: {error}"
                    ) from error
            elif next(iter(decisions.values())) == "rejected":
                # Revocation must reach the derived manifest before authority is
                # committed. A later state failure leaves a conservative
                # pending item, never a rejected WAV that is still published.
                try:
                    os.replace(staged_manifest, manifest_path)
                    lease.assert_owned()
                    os.replace(staged_state, state_path)
                except OSError as error:
                    raise BulkGenerationError(
                        "Cohort rejection did not fully commit; the published "
                        f"manifest remains fail-closed: {error}"
                    ) from error
            else:
                # Approval becomes authoritative before it is projected. If the
                # projection fails, the manifest remains conservative.
                try:
                    os.replace(staged_state, state_path)
                except OSError as error:
                    raise BulkGenerationError(
                        f"Unable to save cohort review decision: {error}"
                    ) from error
                try:
                    lease.assert_owned()
                    _validate_cohort_approved_wavs(
                        proposed, state_path.parent, decisions
                    )
                except BulkGenerationError as error:
                    raise BulkGenerationError(
                        "Cohort review decision was saved, but manifest rebuild was "
                        f"blocked: {error}"
                    ) from error
                try:
                    os.replace(staged_manifest, manifest_path)
                except OSError as error:
                    raise BulkGenerationError(
                        "Cohort review decision was saved, but manifest rebuild failed: "
                        f"{error}"
                    ) from error
        finally:
            for staged in (
                staged_state,
                staged_manifest,
                staged_conservative_manifest,
            ):
                staged.unlink(missing_ok=True)
        lease.mark_committed()
    committed_state_sha256 = sha256_file(state_path)
    return tuple(
        ReviewCommit(
            queue_id=queue_id,
            status=proposed["items"][queue_id]["status"],
            review_status=decisions[queue_id],
            updated_at=updated_at,
            authority=ReviewAuthority(
                queue_sha256=proposed["queue_sha256"],
                state_sha256=committed_state_sha256,
                item_sha256=_canonical_sha256(proposed["items"][queue_id]),
                audio_sha256=authority.audio_sha256,
            ),
        )
        for queue_id, authority in authorities.items()
        if decisions[queue_id] != "pending_review"
    )


def _validate_cohort_approved_wavs(state, output_directory, decisions):
    """Validate only WAVs whose approval is introduced by this transaction."""
    for queue_id, decision in decisions.items():
        if decision != "approved":
            continue
        item = state["items"][queue_id]
        relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
        audio = _within(output_directory, relative, "Generated WAV")
        _validate_success_file(queue_id, item, audio)


_review_generation_cohort = review_generation_cohort


def authorize_live_fallback(
    state_path,
    queue_path,
    queue_id,
    *,
    reason,
    provider="pocket-tts",
    model,
    generation_profile="default",
    evidence_workspaces=(),
    evidence_reviews=(),
):
    """Record one exact terminal live-Pocket decision without publishing audio."""
    if reason not in LIVE_FALLBACK_REASONS:
        raise BulkGenerationError("Live fallback reason is unsupported")
    if provider != "pocket-tts":
        raise BulkGenerationError("Live fallback currently requires Pocket TTS")
    provider = _required_text(provider, "Live fallback provider")
    model = _required_text(model, "Live fallback model")
    generation_profile = _required_text(
        generation_profile, "Live fallback generation profile"
    )
    if model != "pocket-tts" or generation_profile != "default":
        raise BulkGenerationError(
            "Live fallback requires the exact Pocket TTS default model/profile"
        )
    state_path = Path(state_path).expanduser().resolve()
    queue_path = Path(queue_path).expanduser().resolve()
    queue, queue_sha256 = _load_stable_queue(queue_path)
    queue_by_id = {item.queue_id: item for item in queue.items}
    queue_item = queue_by_id.get(queue_id)
    if queue_item is None:
        raise BulkGenerationError(f"Unknown live fallback queue item: {queue_id!r}")
    with _GenerationLease(
        state_path.parent,
        queue_sha256,
        process_checker=process_is_alive,
    ) as lease:
        try:
            state_payload = state_path.read_bytes()
            state = json.loads(state_payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BulkGenerationError(
                f"Unable to read generation state {state_path}: {error}"
            ) from error
        if not isinstance(state, dict):
            raise BulkGenerationError("Generation state must be a JSON object")
        state_sha256 = hashlib.sha256(state_payload).hexdigest()
        _validate_state_document(state, state_path.parent, queue, queue_sha256)
        existing = state["items"].get(queue_id)
        _validate_live_fallback_source(existing, queue_item, reason)
        if (
            reason == LIVE_FALLBACK_HYPOTHESES_EXHAUSTED
            and state.get("active") is not None
        ):
            raise BulkGenerationError(
                "Exhausted-hypothesis fallback requires an inactive base workspace"
            )
        previous_sha256 = None if existing is None else _canonical_sha256(existing)
        evidence = None
        evidence_sources = ()
        if reason == LIVE_FALLBACK_HYPOTHESES_EXHAUSTED:
            if tuple(evidence_workspaces) and tuple(evidence_reviews):
                raise BulkGenerationError(
                    "Live fallback cannot mix repair-workspace and render-review evidence"
                )
            if tuple(evidence_reviews):
                evidence, evidence_sources = _capture_render_review_fallback_evidence(
                    state_path,
                    queue_path,
                    queue_sha256,
                    queue_item,
                    existing,
                    evidence_reviews,
                )
            else:
                evidence, evidence_sources = _capture_live_fallback_evidence(
                    state_path,
                    queue_path,
                    queue_sha256,
                    queue_item,
                    existing,
                    evidence_workspaces,
                )
        elif tuple(evidence_workspaces) or tuple(evidence_reviews):
            raise BulkGenerationError(
                "Live fallback evidence sources require the "
                "generation_hypotheses_exhausted reason"
            )
        requested = synthesis_character_for_line(
            queue_item.speaker, queue_item.voice_character
        )
        decision = {
            "schema": LIVE_FALLBACK_SCHEMA,
            "schema_version": (
                LIVE_FALLBACK_REVIEW_EVIDENCE_VERSION
                if isinstance(evidence, dict) and evidence.get("schema_version") == 2
                else (
                    LIVE_FALLBACK_EVIDENCE_VERSION
                    if evidence is not None
                    else LIVE_FALLBACK_VERSION
                )
            ),
            "reason": reason,
            "provider": provider,
            "model": model,
            "generation_profile": generation_profile,
            "queue_id": queue_id,
            "line_id": queue_item.line_id,
            "text_sha256": queue_item.text_sha256,
            "speaker": queue_item.speaker,
            "requested_voice_character": requested,
            "previous_result_sha256": previous_sha256,
            "decided_at": _now(),
        }
        if evidence is not None:
            decision["evidence"] = evidence
        proposed = copy.deepcopy(state)
        proposed_item = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        if reason == "generated_audio_rejected":
            proposed_item["live_fallback"] = decision
            proposed_item["updated_at"] = _now()
        else:
            for field in (
                "path",
                "file_sha256",
                "quality",
                "speech_quality",
                "review_status",
            ):
                proposed_item.pop(field, None)
            proposed_item.update(
                {
                    "status": "live_fallback",
                    "review_status": "live_fallback",
                    "attempts": _nonnegative_int(
                        proposed_item.get("attempts", 0), "Live fallback attempts"
                    ),
                    "line_id": queue_item.line_id,
                    "text_sha256": queue_item.text_sha256,
                    "speaker": queue_item.speaker,
                    "requested_voice_character": requested,
                    "voice_character": proposed_item.get("voice_character", requested),
                    "live_fallback": decision,
                    "updated_at": _now(),
                }
            )
        proposed["items"][queue_id] = proposed_item
        _validate_state_document(proposed, state_path.parent, queue, queue_sha256)
        entries = _approved_manifest_entries(proposed, state_path.parent)
        transaction_id = secrets.token_hex(16)
        staged_state = state_path.with_name(f".{state_path.name}.{transaction_id}.tmp")
        manifest_path = state_path.parent / "manifest.json"
        staged_manifest = manifest_path.with_name(
            f".{manifest_path.name}.{transaction_id}.tmp"
        )
        try:
            atomic_write_json(staged_state, proposed, sort_keys=True)
            _write_generated_manifest_from_state(
                proposed,
                state_path.parent,
                staged_manifest,
                entries=entries,
            )
            if sha256_file(queue_path) != queue_sha256:
                raise BulkGenerationSourceChangedError(
                    "Generation queue changed before live fallback commit"
                )
            if sha256_file(state_path) != state_sha256:
                raise BulkGenerationSourceChangedError(
                    "Generation state changed before live fallback commit"
                )
            for source_path, source_sha256 in evidence_sources:
                if (
                    not source_path.is_file()
                    or sha256_file(source_path) != source_sha256
                ):
                    raise BulkGenerationSourceChangedError(
                        "Live fallback evidence changed before commit"
                    )
            for source_path, _source_sha256 in evidence_sources:
                if source_path.name != "generation-state.json":
                    continue
                output = source_path.parent
                if any(output.rglob("*.partial.wav")):
                    raise BulkGenerationSourceChangedError(
                        "Live fallback evidence became active before commit"
                    )
            _validate_state_document(proposed, state_path.parent, queue, queue_sha256)
            lease.assert_owned()
            os.replace(staged_state, state_path)
            try:
                lease.assert_owned()
            except BulkGenerationError as error:
                raise BulkGenerationError(
                    "Live fallback was saved, but manifest rebuild was blocked: "
                    f"{error}"
                ) from error
            os.replace(staged_manifest, manifest_path)
            lease.mark_committed()
        finally:
            for path in (staged_state, staged_manifest):
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass
    return decision


def _validate_live_fallback_source(existing, queue_item, reason):
    if reason == "reference_unavailable_after_audit":
        if existing is not None and (
            existing.get("status") != "failed"
            or not isinstance(existing.get("failure"), dict)
            or existing["failure"].get("kind") != "reference_unavailable"
        ):
            raise BulkGenerationError(
                "Reference-unavailable fallback requires no result or an exact "
                "reference-unavailable failure"
            )
        return
    if not isinstance(existing, dict):
        raise BulkGenerationError("Live fallback requires an existing outcome")
    if reason == LIVE_FALLBACK_HYPOTHESES_EXHAUSTED:
        if (
            existing.get("status") != "failed"
            or existing.get("provider") != "moss-tts"
            or not isinstance(existing.get("failure"), dict)
            or existing["failure"].get("kind")
            not in {"missed_eos_audio_limit", "speech_silence"}
        ):
            raise BulkGenerationError(
                "Exhausted-hypothesis fallback requires an exact typed failed "
                "MOSS current-control outcome"
            )
        return
    if reason == "generated_audio_rejected":
        if (existing.get("status"), existing.get("review_status")) != (
            "generated",
            "rejected",
        ):
            raise BulkGenerationError(
                "Rejected-audio fallback requires a reviewed rejected WAV"
            )
        return
    repair = existing.get("failure_repair")
    if (
        existing.get("status") != "failed"
        or existing.get("provider") != "pocket-tts"
        or not isinstance(existing.get("failure"), dict)
        or not isinstance(repair, dict)
        or repair.get("strategy") != OFFLINE_FALLBACK_BACKEND
    ):
        raise BulkGenerationError(
            "Offline-exhausted live fallback requires a typed failed Pocket fallback"
        )


def _capture_live_fallback_evidence(
    state_path,
    queue_path,
    queue_sha256,
    queue_item,
    existing,
    evidence_workspaces,
):
    values = tuple(Path(value).expanduser().resolve() for value in evidence_workspaces)
    if not values:
        raise BulkGenerationError(
            "Exhausted-hypothesis fallback requires an evidence workspace"
        )
    if len(values) != len(set(values)):
        raise BulkGenerationError("Live fallback evidence workspace is duplicated")
    base_root = state_path.parent.parent
    base_workspace_path = base_root / "workspace.json"
    try:
        base_workspace_payload = base_workspace_path.read_bytes()
        base_workspace = json.loads(base_workspace_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BulkGenerationError(
            f"Unable to read live fallback base workspace: {error}"
        ) from error
    _base_workspace_id, base_import = _live_fallback_workspace_import(
        base_workspace, "base"
    )
    if queue_path != base_root / "queue.jsonl":
        raise BulkGenerationError(
            "Live fallback queue must belong to its base workspace"
        )
    base_result_sha256 = _canonical_sha256(existing)
    hypotheses = []
    snapshots = []
    for directory in values:
        if directory == base_root:
            raise BulkGenerationError(
                "Live fallback evidence must differ from the current workspace"
            )
        workspace_path = directory / "workspace.json"
        source_queue_path = directory / "queue.jsonl"
        source_state_path = directory / "generated-audio" / "generation-state.json"
        try:
            workspace_payload = workspace_path.read_bytes()
            source_queue_payload = source_queue_path.read_bytes()
            source_state_payload = source_state_path.read_bytes()
            workspace = json.loads(workspace_payload.decode("utf-8"))
            source_state = json.loads(source_state_payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BulkGenerationError(
                f"Unable to read live fallback evidence workspace {directory}: {error}"
            ) from error
        workspace_id, source_import = _live_fallback_workspace_import(
            workspace, "evidence"
        )
        if source_import != base_import:
            raise BulkGenerationError(
                "Live fallback evidence immutable import differs from its base"
            )
        source_queue_sha256 = hashlib.sha256(source_queue_payload).hexdigest()
        if source_queue_sha256 != queue_sha256:
            raise BulkGenerationError(
                "Live fallback evidence queue differs from its base"
            )
        source_queue, stable_queue_sha256 = _load_stable_queue(source_queue_path)
        if stable_queue_sha256 != source_queue_sha256:
            raise BulkGenerationSourceChangedError(
                "Live fallback evidence queue changed while it was read"
            )
        if (
            (source_state.get("schema"), source_state.get("schema_version"))
            not in {
                (STATE_SCHEMA, STATE_VERSION),
                (LEGACY_STATE_SCHEMA, LEGACY_STATE_VERSION),
            }
            or source_state.get("queue_sha256") != source_queue_sha256
            or not isinstance(source_state.get("items"), dict)
        ):
            raise BulkGenerationError(
                "Live fallback evidence generation state is malformed"
            )
        if source_state.get("active") is not None or any(
            source_state_path.parent.rglob("*.partial.wav")
        ):
            raise BulkGenerationError(
                "Live fallback evidence workspace is active or incomplete"
            )
        source_result = source_state["items"].get(queue_item.queue_id)
        if (
            not isinstance(source_result, dict)
            or source_result.get("status") != "failed"
        ):
            raise BulkGenerationError(
                "Live fallback evidence must contain the exact failed queue item"
            )
        source_queue_item = next(
            item for item in source_queue.items if item.queue_id == queue_item.queue_id
        )
        _validate_failure_record(
            source_result.get("failure"),
            queue_item.queue_id,
            result=source_result,
        )
        _validate_synthesis_identity(
            source_result, queue_item.queue_id, source_queue_item
        )
        _validate_failure_repair_record(
            source_result, queue_item.queue_id, source_queue_item
        )
        _validate_seed_application(source_result, queue_item.queue_id)
        repair = source_result.get("failure_repair")
        carry = source_result.get("carry_forward")
        if (
            not isinstance(repair, dict)
            or repair.get("strategy") != SENTENCE_BOUNDARY_SEGMENTATION
            or not isinstance(carry, dict)
            or carry.get("source_item_sha256") != base_result_sha256
        ):
            raise BulkGenerationError(
                "Live fallback evidence must be a completed sentence-boundary "
                "repair carried from the exact current item"
            )
        workspace_sha256 = hashlib.sha256(workspace_payload).hexdigest()
        state_sha256 = hashlib.sha256(source_state_payload).hexdigest()
        result_sha256 = _canonical_sha256(source_result)
        hypotheses.append(
            {
                "workspace_id": workspace_id,
                "workspace_sha256": workspace_sha256,
                "state_sha256": state_sha256,
                "queue_sha256": source_queue_sha256,
                "result_sha256": result_sha256,
                "strategy": SENTENCE_BOUNDARY_SEGMENTATION,
                "result": copy.deepcopy(source_result),
            }
        )
        snapshots.extend(
            (
                (workspace_path, workspace_sha256),
                (source_queue_path, source_queue_sha256),
                (source_state_path, state_sha256),
            )
        )
    hypotheses.sort(key=lambda value: (value["workspace_id"], value["result_sha256"]))
    evidence = {
        "schema": LIVE_FALLBACK_EVIDENCE_SCHEMA,
        "schema_version": 1,
        "queue_sha256": queue_sha256,
        "base_result_sha256": base_result_sha256,
        "hypotheses": hypotheses,
    }
    _validate_live_fallback_evidence(evidence, base_result_sha256)
    snapshots.extend(
        (
            (
                base_workspace_path,
                hashlib.sha256(base_workspace_payload).hexdigest(),
            ),
            (queue_path, queue_sha256),
        )
    )
    return evidence, tuple(snapshots)


def _capture_render_review_fallback_evidence(
    state_path,
    queue_path,
    queue_sha256,
    queue_item,
    existing,
    evidence_reviews,
):
    from vntts.authoring.render_hypothesis_records import (
        RenderHypothesisRecordError,
        load_render_hypothesis_record,
    )

    values = tuple(Path(value).expanduser().resolve() for value in evidence_reviews)
    if not values:
        raise BulkGenerationError(
            "Exhausted-hypothesis fallback requires a render review"
        )
    if len(values) != len(set(values)):
        raise BulkGenerationError("Live fallback render review is duplicated")
    base_root = state_path.parent.parent
    if queue_path != base_root / "queue.jsonl":
        raise BulkGenerationError(
            "Live fallback queue must belong to its base workspace"
        )
    base_result_sha256 = _canonical_sha256(existing)
    hypotheses = []
    snapshots = [(queue_path, queue_sha256)]
    for directory in values:
        try:
            record = load_render_hypothesis_record(directory)
        except RenderHypothesisRecordError as error:
            raise BulkGenerationError(
                f"Unable to read live fallback render review {directory}: {error}"
            ) from error
        review = record.review
        decision = record.decision
        if (
            not isinstance(decision, dict)
            or decision.get("decision") != "need_different"
        ):
            raise BulkGenerationError(
                "Live fallback render review must be terminal need_different"
            )
        if (
            review.get("queue_id") != queue_item.queue_id
            or review.get("line_id") != queue_item.line_id
            or review.get("text_sha256") != queue_item.text_sha256
        ):
            raise BulkGenerationError(
                "Live fallback render review queue identity changed"
            )
        source_files = tuple(
            (snapshot.path, snapshot.sha256) for snapshot in record.snapshots
        )
        hypotheses.append(
            {
                "kind": "render_hypothesis_review",
                "review_id": review["review_id"],
                "review_sha256": record.review_snapshot.sha256,
                "review_document_sha256": _canonical_sha256(review),
                "decision_sha256": record.decision_snapshot.sha256,
                "decision_document_sha256": _canonical_sha256(decision),
                "comparison_sha256": review["comparison_sha256"],
                "arm_report_sha256": review["arm_report_sha256"],
                "reference_sha256": review["reference_sha256"],
                "result_sha256": review["result_sha256"],
                "decision": "need_different",
                "review": review,
                "decision_document": decision,
            }
        )
        snapshots.extend(source_files)
    hypotheses.sort(key=lambda value: (value["kind"], value["review_id"]))
    evidence = {
        "schema": LIVE_FALLBACK_EVIDENCE_SCHEMA,
        "schema_version": 2,
        "queue_sha256": queue_sha256,
        "base_result_sha256": base_result_sha256,
        "hypotheses": hypotheses,
    }
    _validate_live_fallback_evidence(evidence, base_result_sha256)
    return evidence, tuple(snapshots)


def _live_fallback_workspace_import(workspace, label):
    if (
        not isinstance(workspace, dict)
        or workspace.get("schema") != "vntts.authoring-workspace"
        or workspace.get("schema_version") != 1
        or not isinstance(workspace.get("workspace_id"), str)
        or not workspace["workspace_id"].startswith("resume-")
        or not isinstance(workspace.get("source"), dict)
        or not isinstance(workspace["source"].get("import_id"), str)
        or not workspace["source"]["import_id"]
    ):
        raise BulkGenerationError(
            f"Live fallback {label} workspace authority is malformed"
        )
    return workspace["workspace_id"], workspace["source"]["import_id"]


def _review_generation_item_locked(
    state_path,
    queue_id,
    decision,
    *,
    expected_authority=None,
    queue_path=None,
    lease=None,
):
    if expected_authority is None:
        state = load_generation_state(state_path)
    else:
        state, _displayed_item, _audio_bytes = _load_review_snapshot(
            state_path,
            queue_id,
            expected_authority,
            queue_path,
            capture_audio=False,
        )
    item = state.get("items", {}).get(queue_id)
    if not isinstance(item, dict) or item.get("status") not in {
        "generated",
        "approved",
    }:
        raise BulkGenerationError(f"Generated queue item does not exist: {queue_id}")
    relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
    audio = _within(state_path.parent, relative, "Generated WAV")
    if expected_authority is None:
        _validate_success_file(queue_id, item, audio)
    if expected_authority is not None:
        _assert_review_authority(
            state_path,
            queue_id,
            expected_authority,
            queue_path,
        )
    if lease is not None:
        lease.assert_owned()
    proposed = copy.deepcopy(state)
    proposed_item = proposed["items"][queue_id]
    proposed_item["review_status"] = decision
    proposed_item["status"] = "approved" if decision == "approved" else "generated"
    proposed_item["updated_at"] = _now()
    manifest_path = state_path.parent / "manifest.json"
    entries = _approved_manifest_entries(
        proposed,
        state_path.parent,
        validate_files=expected_authority is None,
    )
    transaction_id = secrets.token_hex(16)
    staged_state = state_path.with_name(f".{state_path.name}.{transaction_id}.tmp")
    staged_manifest = manifest_path.with_name(
        f".{manifest_path.name}.{transaction_id}.tmp"
    )
    try:
        atomic_write_json(staged_state, proposed, sort_keys=True)
        _write_generated_manifest_from_state(
            proposed,
            state_path.parent,
            staged_manifest,
            entries=entries,
            validate_files=expected_authority is None,
        )
        if expected_authority is not None:
            _assert_review_authority(
                state_path,
                queue_id,
                expected_authority,
                queue_path,
            )
        if lease is not None:
            lease.assert_owned()
        try:
            os.replace(staged_state, state_path)
        except OSError as error:
            raise BulkGenerationError(
                f"Unable to save review decision: {error}"
            ) from error
        try:
            if lease is not None:
                try:
                    lease.assert_owned()
                except BulkGenerationError as error:
                    raise BulkGenerationError(
                        "Review decision was saved, but manifest rebuild was blocked: "
                        f"{error}"
                    ) from error
            os.replace(staged_manifest, manifest_path)
        except OSError as error:
            raise BulkGenerationError(
                f"Review decision was saved, but manifest rebuild failed: {error}"
            ) from error
    finally:
        for staged in (staged_state, staged_manifest):
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    if lease is not None:
        lease.mark_committed()
    committed_item = proposed["items"][queue_id]
    if expected_authority is None:
        return proposed
    return ReviewCommit(
        queue_id=queue_id,
        status=committed_item["status"],
        review_status=committed_item["review_status"],
        updated_at=committed_item["updated_at"],
        authority=ReviewAuthority(
            queue_sha256=proposed["queue_sha256"],
            state_sha256=sha256_file(state_path),
            item_sha256=_canonical_sha256(committed_item),
            audio_sha256=expected_authority.audio_sha256,
        ),
    )


_GenerationLease = GenerationLease
_process_started_at = process_started_at


def _load_or_create_state(state_path, output_directory, queue, queue_sha256):
    if state_path.is_file():
        state = _load_json(state_path, "generation state")
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
    atomic_write_json(state_path, state, sort_keys=True)
    return state


_validate_success_file = validate_success_file


def _reconcile_interrupted_attempt(state_path, state, queue):
    active = state.get("active")
    if not isinstance(active, dict):
        return
    queue_id = active.get("queue_id")
    if queue_id not in {item.queue_id for item in queue.items}:
        raise BulkGenerationError(
            "Interrupted attempt references an unknown queue item"
        )
    interrupted = dict(active)
    interrupted["detected_at"] = _now()
    state.setdefault("interrupted_attempts", []).append(interrupted)
    existing = state["items"].get(queue_id, {})
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
                "text_features": _text_failure_features(active.get("text") or ""),
            },
            "updated_at": _now(),
        }
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
        state["items"][queue_id] = interrupted_result
    state["active"] = None
    atomic_write_json(state_path, state, sort_keys=True)


def _write_active(
    state_path,
    state,
    item,
    *,
    provider,
    model,
    generation_profile,
    prompt_sha256,
    queue_annotations_sha256,
    synthesis_text_sha256,
    text_transform_id,
    synthesis_provenance_sha256,
    synthesis_configuration,
    synthesis_voice_character,
    synthesis_fallback,
    source_reference_binding,
    failure_repair,
    phase,
    attempt,
    attempt_limit,
    total_attempts,
    provider_attempt,
    attempts_by_provider,
    seed,
    seed_applied,
    started_at,
    last_error,
):
    state["active"] = {
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
    if synthesis_fallback is not None:
        state["active"]["synthesis_fallback"] = synthesis_fallback
        state["active"]["narrator_character"] = synthesis_fallback["narrator_character"]
    if source_reference_binding is not None:
        state["active"]["source_reference_binding"] = source_reference_binding
    if failure_repair is not None:
        state["active"]["failure_repair"] = failure_repair
    atomic_write_json(state_path, state, sort_keys=True)


def _write_active_phase(state_path, state, phase, *, last_error=None):
    active = state.get("active")
    if not isinstance(active, dict):
        raise BulkGenerationError("Generation active attempt was lost")
    active["phase"] = phase
    active["updated_at"] = _now()
    if last_error is not None:
        active["last_error"] = last_error
    atomic_write_json(state_path, state, sort_keys=True)


def _validate_render_result(result, request, provider):
    if result.completion is not SynthesisCompletion.COMPLETE:
        raise IncompleteSynthesisError(result)
    if not isinstance(result.sample_rate, int) or result.sample_rate <= 0:
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


def generated_mono_pcm(pcm):
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
    return mono


_generated_mono_pcm = generated_mono_pcm


def _guard_job_process(output_directory, process_checker):
    job_path = output_directory.parent / "job.json"
    if not job_path.is_file():
        return None
    job = _load_json(job_path, "pregeneration job")
    if job.get("status") == "running" and process_checker(job.get("pid")):
        raise BulkGenerationError(
            f"Pregeneration job is active in another process with PID {job.get('pid')}"
        )
    if job.get("status") != "running":
        return None
    return {
        "job": str(job_path.resolve()),
        "job_sha256": sha256_file(job_path),
        "pid": job.get("pid"),
        "recorded_status": "running",
        "detected_status": "interrupted",
        "detected_at": _now(),
    }


def _audio_relative_path(voice, queue_id):
    voice_slug = slugify(voice)
    if not voice_slug:
        raise BulkGenerationError(
            f"Voice cannot form a safe audio directory: {voice!r}"
        )
    digest = hashlib.sha256(queue_id.encode("utf-8")).hexdigest()[:24]
    return Path("audio") / voice_slug / f"{digest}.wav"


_archive_interrupted_artifact = archive_interrupted_artifact


_load_stable_queue = load_stable_generation_queue


def snapshot_generation_control_files(control_files):
    if not isinstance(control_files, dict):
        raise BulkGenerationError(
            "Generation control files must be a role/path mapping"
        )
    snapshots = []
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
        directory_files = _control_directory_files(path) if path.is_dir() else ()
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
    return tuple(snapshots)


_snapshot_control_files = snapshot_generation_control_files


def _control_directory_files(path):
    records = []
    try:
        candidates = sorted(path.rglob("*"), key=lambda value: value.as_posix())
        for candidate in candidates:
            if not candidate.is_file():
                continue
            records.append(
                {
                    "path": candidate.relative_to(path).as_posix(),
                    "sha256": sha256_file(candidate),
                }
            )
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to inventory generation control directory {path}: {error}"
        ) from error
    return tuple(records)


def _stored_control(control):
    record = {
        "role": control["role"],
        "kind": control["kind"],
        "path": str(control["path"]),
        "sha256": control["sha256"],
    }
    if control["kind"] == "directory":
        record["files"] = list(control["files"])
    return record


def _assert_sources_unchanged(queue_path, queue_sha256, controls):
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


def _assert_workspace_output_identity(output_directory, identity):
    if not isinstance(identity, dict) or set(identity) != {"path", "device", "inode"}:
        raise BulkGenerationSourceChangedError("Workspace output identity is malformed")
    path = Path(output_directory).expanduser()
    absolute = Path(os.path.abspath(os.fspath(path)))
    expected = Path(identity["path"])
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


def _assert_control_files_unchanged(controls):
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


def _load_json(path, description):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BulkGenerationError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise BulkGenerationError(f"{description.capitalize()} must be a JSON object")
    return value


_canonical_sha256 = canonical_document_sha256


def _nonnegative_optional_int(value, label):
    if value is None:
        return None
    return _nonnegative_int(value, label)


def _now():
    return datetime.now(timezone.utc).isoformat()

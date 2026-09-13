"""Workspace inspection, readiness and generation command projections."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import SupportsIndex, SupportsInt

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    load_story_index_document,
)
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    VoiceGenerationQueueItem,
)
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    validate_voice_manifest,
)

import vntts.authoring.legacy_import as legacy_import
from vntts.authoring.audio_event_workspace import (
    AUDIO_EVENT_PROVIDER,
)
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    JsonDocument,
    ReviewAuthority,
    StateItems,
    is_spoken_queue_item,
    load_generation_state,
    normalized_failure_record,
    sentence_repair_matches_failure,
)
from vntts.authoring.failure_repair import (
    FailureRepairPolicy,
    FailureRepairPolicyError,
)
from vntts.authoring.generation_lease import (
    LEASE_SCHEMA,
    LEASE_VERSION,
    process_is_alive,
    process_started_at,
)
from vntts.authoring.missing_voice_policy import (
    NARRATOR_ALL_UNRESOLVED,
    NARRATOR_ROLES,
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
)
from vntts.authoring.speech_quality import (
    SPEECH_QUALITY_ANALYSIS_VERSION,
    measure_generated_speech_bytes,
)
from vntts.authoring.workbench_contracts import (
    ActiveAttempt,
    AuthoringRuntimeStatus,
    AuthoringWorkbenchError,
    CollectionSelection,
    GenerationReadiness,
    ImmutableHistoryTimestamp,
    ReviewItem,
    WorkbenchProjectionData,
    WorkspaceCollection,
    WorkspaceSummary,
    WorkspaceVoice,
    _read_bound_bytes,
    _WorkbenchProjectionRead,
)
from vntts.authoring.workspace_authority import (
    WorkspaceDocument,
    _load_bound_workspace_queue,
    _load_json,
    _load_json_snapshot,
    _load_workspace,
    _load_workspace_identity,
    _parse_history_timestamp,
    _require_sha256,
    _required_text,
    _safe_relative,
    _validate_import_history,
    _within,
    _workspace_failure_repair_policy,
    _workspace_run_config_with_policy,
    load_workspace_authority,
)
from vntts.authoring.workspace_config import (
    workspace_audio_event_spoken_projection_queue_ids,
    workspace_queue_sha256,
)
from vntts.authoring.workspace_creation import (
    _failure_reference_runtime_binding,
    _optional_text,
    _selected_voice_manifest,
    _workspace_missing_voice_policy,
    default_workspaces_root,
)
from vntts.authoring.workspace_state import (
    cached_workspace_generation_state,
    shared_workspace_state_reads,
)
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    synthesis_character_for_line,
)

REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS = 1.2
PACE_MINIMUM_WORDS = 5
PACE_MINIMUM_LENGTH_BUCKET_SAMPLES = 3
PACE_MINIMUM_VOICE_SAMPLES = 5
PACE_SLOW_RELATIVE_RATIO = 0.80
PACE_SLOW_MINIMUM_DELTA_WPM = 20.0
_IMPORT_ID_PATTERN = re.compile(r"legacy-[0-9a-f]{24}")


def generation_failure_category(error: object, *, text: str = "") -> str:
    """Collapse volatile backend diagnostics into actionable failure cohorts."""
    if isinstance(error, dict):
        failure = normalized_failure_record(error, text=text)
        kind = failure.get("kind")
        if not isinstance(kind, str):
            return "other generation failure"
        if (
            kind == "speech_silence"
            and text
            and sentence_repair_matches_failure(failure, text)
        ):
            return "Long sentence-boundary pause"
        return {
            "missed_eos_audio_limit": "audio limit / missed EOS",
            "speech_silence": "speech silence",
            "reference_unavailable": "reference unavailable",
            "cancelled": "cancelled",
            "interrupted": "interrupted",
            "backend_error": "other generation failure",
        }[kind]
    value = str(error or "").casefold()
    if "limited" in value or " limit" in value:
        return "audio limit / missed EOS"
    if "silence" in value:
        return "speech silence"
    return "other generation failure"


def _review_internal_pause_seconds(
    result: JsonDocument, *, failed: bool
) -> float | None:
    source = (
        normalized_failure_record(result).get("speech_quality")
        if failed
        else result.get("speech_quality")
    )
    if not isinstance(source, dict):
        return None
    value = source.get("longest_internal_silence_seconds")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return float(value)


def _inspection_state_items(state: Mapping[str, object]) -> StateItems:
    items = state.get("items")
    if not isinstance(items, dict) or any(
        not isinstance(queue_id, str) or not isinstance(value, dict)
        for queue_id, value in items.items()
    ):
        raise AuthoringWorkbenchError("Generation state items are malformed")
    return {queue_id: value for queue_id, value in items.items()}


@lru_cache(maxsize=2048)
def discover_imports(import_root: str | Path | None = None) -> tuple[Path, ...]:
    root = (
        Path(import_root or legacy_import.default_import_root()).expanduser().resolve()
    )
    if not root.is_dir():
        return ()
    results = []
    seen = set()
    for manifest_path in sorted(root.glob("legacy-*/import.json")):
        try:
            if manifest_path.is_symlink() or manifest_path.parent.is_symlink():
                continue
            directory = manifest_path.parent.resolve()
            directory.relative_to(root)
            if not _IMPORT_ID_PATTERN.fullmatch(directory.name):
                continue
            key = directory.name.casefold()
            if key in seen:
                continue
            manifest = _load_json(manifest_path, "legacy import")
            if (
                manifest.get("schema") == legacy_import.IMPORT_SCHEMA
                and manifest.get("schema_version")
                in legacy_import.SUPPORTED_IMPORT_SCHEMA_VERSIONS
                and (manifest_path.parent / "queue.jsonl").is_file()
            ):
                _validate_import_history(manifest)
                results.append(directory)
                seen.add(key)
        except AuthoringWorkbenchError, ValueError:
            continue
    return tuple(results)


def discover_workspaces(workspaces_root: str | Path | None = None) -> tuple[Path, ...]:
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    if not root.is_dir():
        return ()
    results = []
    seen = set()
    for path in sorted(root.glob("*/workspace.json"), reverse=True):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                continue
            directory = path.parent.resolve()
            directory.relative_to(root)
            if not re.fullmatch(r"resume-[0-9a-f]{24}-[0-9a-f]{16}", directory.name):
                continue
            key = directory.name.casefold()
            if key in seen:
                continue
            _load_workspace_identity(directory)
            results.append(directory)
            seen.add(key)
        except AuthoringWorkbenchError, ValueError:
            continue
    return tuple(results)


def inspect_workspace(
    workspace_directory: str | Path,
    *,
    voice_manifest: str | Path | None = None,
    local_process_id: int | None = None,
    local_process_started_at: str | None = None,
    process_checker: Callable[[int], bool] = process_is_alive,
    process_start_checker: Callable[[int], str | None] = process_started_at,
) -> WorkspaceSummary:
    directory, workspace = _load_workspace(workspace_directory)
    queue_path = _within(
        directory, _safe_relative(workspace["queue"], "Queue"), "Queue"
    )
    output = _within(directory, _safe_relative(workspace["output"], "Output"), "Output")
    try:
        queue = VoiceGenerationQueue.load(queue_path)
    except VoiceGenerationQueueError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    state_path = output / "generation-state.json"
    state = None
    if state_path.is_file():
        try:
            state = load_generation_state(state_path, queue_path)
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error

    return _inspect_workspace_from_read(
        directory,
        workspace,
        queue_path,
        output,
        queue,
        state_path if state_path.is_file() else None,
        state,
        voice_manifest=voice_manifest,
        local_process_id=local_process_id,
        local_process_started_at=local_process_started_at,
        process_checker=process_checker,
        process_start_checker=process_start_checker,
    )


def _inspect_workspace_from_read(
    directory: Path,
    workspace: JsonDocument,
    queue_path: Path,
    output: Path,
    queue: VoiceGenerationQueue,
    state_path: Path | None,
    state: JsonDocument | None,
    *,
    voice_manifest: str | Path | None = None,
    local_process_id: int | None = None,
    local_process_started_at: str | None = None,
    process_checker: Callable[[int], bool] = process_is_alive,
    process_start_checker: Callable[[int], str | None] = process_started_at,
) -> WorkspaceSummary:
    state_items = {} if state is None else _inspection_state_items(state)

    audio_event_config = workspace.get("audio_event_composition")
    audio_event_ids = (
        {audio_event_config["queue_id"]}
        if isinstance(audio_event_config, dict)
        else set()
    )
    omission_config = workspace.get("audio_event_omission")
    if isinstance(omission_config, dict) and isinstance(
        omission_config.get("items"), list
    ):
        audio_event_ids.update(
            item["queue_id"]
            for item in omission_config["items"]
            if isinstance(item, dict) and isinstance(item.get("queue_id"), str)
        )
    candidates = [item for item in queue.items if item.action == "generate"]
    recoverable_source_audio = sum(
        item.action == "prefer_source_audio" and item.queue_id not in audio_event_ids
        for item in queue.items
    )
    manual_review = sum(
        item.action == "manual_review" and item.queue_id not in audio_event_ids
        for item in queue.items
    )
    resolve_audio = sum(
        item.action == "resolve_audio" and item.queue_id not in audio_event_ids
        for item in queue.items
    )
    spoken = [item for item in candidates if is_spoken_queue_item(item)]
    spoken_ids = {item.queue_id for item in spoken}
    reviewable_ids = spoken_ids | audio_event_ids
    relevant = {
        queue_id: value
        for queue_id, value in state_items.items()
        if queue_id in reviewable_ids
    }
    approved_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if value.get("status") == "approved"
        and value.get("review_status") == "approved"
    }
    rejected_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if value.get("status") == "generated"
        and value.get("review_status") == "rejected"
    }
    generated_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if value.get("status") == "generated"
        and value.get("review_status") == "pending_review"
    }
    failed_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if value.get("status") == "failed"
    }
    live_fallback_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if isinstance(value.get("live_fallback"), dict)
    }
    omitted_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if isinstance(value.get("audio_event_omission"), dict)
    }
    completed_ids = (
        approved_ids
        | rejected_ids
        | generated_ids
        | failed_ids
        | live_fallback_ids
        | omitted_ids
    )
    selected_voice_manifest = _selected_voice_manifest(
        directory, workspace, voice_manifest
    )
    missing_voice_ids, blocked_reasons = _voice_readiness(
        workspace,
        spoken,
        completed_ids,
        selected_voice_manifest,
        directory=directory,
    )
    blocked_reasons = (*_workspace_control_reasons(workspace), *blocked_reasons)
    pending_ids = reviewable_ids - completed_ids - missing_voice_ids
    active = _active_attempt(state.get("active") if state else None, spoken_ids)
    runtime_status = _runtime_status(
        output,
        active,
        len(pending_ids),
        len(generated_ids),
        len(failed_ids),
        len(missing_voice_ids),
        blocked_reasons,
        queue_sha256=sha256_file(queue_path),
        local_process_id=local_process_id,
        local_process_started_at=local_process_started_at,
        process_checker=process_checker,
        process_start_checker=process_start_checker,
    )
    failures = Counter(
        str(value.get("last_error") or "Unknown failure")
        for value in relevant.values()
        if value.get("status") == "failed"
    )
    latest_line, latest_text, latest_status, latest_updated_at = _latest_outcome(
        queue, relevant
    )
    return WorkspaceSummary(
        directory=directory,
        title=_required_text(workspace.get("title"), "Workspace title"),
        runtime_status=runtime_status,
        queue_items=len(queue.items),
        eligible=len(reviewable_ids),
        pending=len(pending_ids),
        generated=len(generated_ids),
        approved=len(approved_ids),
        rejected=len(rejected_ids),
        live_fallback=len(live_fallback_ids),
        omitted=len(omitted_ids),
        failed=len(failed_ids),
        skipped_actions=(
            len(queue.items)
            - len(candidates)
            - recoverable_source_audio
            - manual_review
            - resolve_audio
        ),
        skipped_sound_effects=len(candidates)
        - len(spoken)
        - len(audio_event_ids & {item.queue_id for item in candidates}),
        recoverable_source_audio=recoverable_source_audio,
        manual_review=manual_review,
        resolve_audio=resolve_audio,
        missing_voice=(
            len(missing_voice_ids) if selected_voice_manifest is not None else None
        ),
        blocked_reasons=blocked_reasons,
        active=active,
        failure_reasons=tuple(failures.most_common()),
        queue=queue_path,
        output=output,
        state=state_path,
        voice_manifest=selected_voice_manifest,
        latest_line=latest_line,
        latest_text=latest_text,
        latest_status=latest_status,
        latest_updated_at=latest_updated_at,
    )


def _review_technical_metrics(
    result: JsonDocument,
    text: str,
    *,
    projected_speech_quality: JsonDocument | None = None,
) -> tuple[float | None, float | None, float | None, tuple[str, ...]]:
    quality = result.get("quality")
    if not isinstance(quality, dict):
        return None, None, None, ()
    duration = quality.get("duration_seconds")
    peak = quality.get("peak")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = None
    else:
        duration = float(duration)
    peak = float(peak) if isinstance(peak, (int, float)) else None
    is_audio_event = result.get("provider") == AUDIO_EVENT_PROVIDER
    speech_quality = (
        projected_speech_quality
        if projected_speech_quality is not None
        else result.get("speech_quality")
    )
    speech_quality = (
        {}
        if is_audio_event
        else speech_quality
        if isinstance(speech_quality, dict)
        else {}
    )
    word_count = 0 if is_audio_event else _pace_word_count(text)
    leading_silence = speech_quality.get("leading_silence_seconds")
    trailing_silence = speech_quality.get("trailing_silence_seconds")
    trimmed_seconds = sum(
        float(value)
        for value in (leading_silence, trailing_silence)
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )
    audible_duration = (
        None if duration is None else max(0.0, duration - trimmed_seconds)
    )
    words_per_minute = (
        None
        if is_audio_event or not audible_duration
        else float(word_count * 60 / audible_duration)
    )
    internal_silence = speech_quality.get("longest_internal_silence_seconds")
    flags = []
    if peak is not None and peak >= 0.98:
        flags.append("near clipping")
    if (
        isinstance(internal_silence, (int, float))
        and internal_silence >= REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS
    ):
        flags.append("notable pause")
    return duration, words_per_minute, peak, tuple(flags)


def _pace_word_count(text: str) -> int:
    return len(re.findall(r"[\w’'-]+", str(text or ""), flags=re.UNICODE))


def _pace_length_bucket(word_count: int) -> str:
    if word_count <= 9:
        return "short"
    if word_count <= 20:
        return "medium"
    return "long"


def _pace_voice_key(item: ReviewItem) -> str:
    return str(item.voice_character or item.speaker or "").strip().casefold()


def _annotate_pace_advisories(
    records: Sequence[ReviewItem],
) -> tuple[ReviewItem, ...]:
    """Project relative slow-pace outliers without changing review authority."""
    eligible = [
        item
        for item in records
        if item.words_per_minute is not None
        and item.words_per_minute > 0
        and _pace_word_count(item.text) >= PACE_MINIMUM_WORDS
    ]
    by_voice: dict[str, list[float]] = {}
    by_voice_and_length: dict[tuple[str, str], list[float]] = {}
    for item in eligible:
        words_per_minute = item.words_per_minute
        if words_per_minute is None:
            continue
        voice = _pace_voice_key(item)
        length = _pace_length_bucket(_pace_word_count(item.text))
        by_voice.setdefault(voice, []).append(words_per_minute)
        by_voice_and_length.setdefault((voice, length), []).append(words_per_minute)

    annotated = []
    for item in records:
        word_count = _pace_word_count(item.text)
        voice = _pace_voice_key(item)
        length = _pace_length_bucket(word_count)
        same_length = by_voice_and_length.get((voice, length), ())
        same_voice = by_voice.get(voice, ())
        baseline = None
        scope = None
        if word_count >= PACE_MINIMUM_WORDS and item.words_per_minute is not None:
            if len(same_length) >= PACE_MINIMUM_LENGTH_BUCKET_SAMPLES:
                baseline = float(median(same_length))
                scope = f"same voice/{length} lines"
            elif len(same_voice) >= PACE_MINIMUM_VOICE_SAMPLES:
                baseline = float(median(same_voice))
                scope = "same voice/all eligible lengths"
        advisories: tuple[str, ...] = ()
        ratio = None
        words_per_minute = item.words_per_minute
        if baseline is not None and baseline > 0 and words_per_minute is not None:
            ratio = float(words_per_minute / baseline)
            if (
                ratio <= PACE_SLOW_RELATIVE_RATIO
                and baseline - words_per_minute >= PACE_SLOW_MINIMUM_DELTA_WPM
            ):
                advisories = (
                    f"slow relative outlier {words_per_minute:.0f} WPM "
                    f"vs {baseline:.0f} WPM {scope} median",
                )
        annotated.append(
            replace(
                item,
                pace_baseline_wpm=baseline,
                pace_ratio=ratio,
                pace_baseline_scope=scope,
                pace_advisories=advisories,
            )
        )
    return tuple(annotated)


def _corrected_legacy_speech_quality(
    audio_path: str | Path, expected_sha256: str
) -> JsonDocument:
    """Re-measure one legacy WAV from digest-bound bytes for review attention."""
    path = Path(audio_path)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise AuthoringWorkbenchError(
            f"Unable to read generated WAV for review metrics: {error}"
        ) from error
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise AuthoringWorkbenchError(
            "Generated WAV changed while review metrics were being projected"
        )
    try:
        return asdict(
            measure_generated_speech_bytes(
                content,
                analysis_version=SPEECH_QUALITY_ANALYSIS_VERSION,
            )
        )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _review_voice_character(
    item: VoiceGenerationQueueItem, result: JsonDocument
) -> str:
    return str(
        result.get("voice_character")
        or synthesis_character_for_line(item.speaker, item.voice_character)
    )


def _normalize_review_queue_ids(queue_ids: object) -> set[str] | None:
    if queue_ids is None:
        return None
    if not isinstance(queue_ids, (list, tuple, set, frozenset)):
        raise AuthoringWorkbenchError("Review queue IDs must be a collection")
    selected = set()
    for queue_id in queue_ids:
        if not isinstance(queue_id, str) or not queue_id:
            raise AuthoringWorkbenchError("Review queue ID must be non-empty text")
        if queue_id in selected:
            raise AuthoringWorkbenchError(f"Review queue ID is duplicated: {queue_id}")
        selected.add(queue_id)
    return selected


def list_review_items(
    workspace_directory: str | Path, queue_ids: object = None
) -> tuple[ReviewItem, ...]:
    selected_queue_ids = _normalize_review_queue_ids(queue_ids)
    directory, workspace = _load_workspace(workspace_directory)
    queue_path = _within(
        directory, _safe_relative(workspace["queue"], "Queue"), "Queue"
    )
    output = _within(
        directory,
        _safe_relative(workspace["output"], "Output"),
        "Output",
    )
    state_path = output / "generation-state.json"
    if not state_path.is_file():
        return ()
    queue = _load_bound_workspace_queue(directory, workspace)
    story = _load_bound_story_document(directory, workspace)
    state_sha256 = sha256_file(state_path)
    state = load_generation_state(state_path, queue_path)
    if sha256_file(state_path) != state_sha256:
        raise AuthoringWorkbenchError(
            "Generation state changed while review rows were being projected"
        )
    return _list_review_items_from_read(
        queue,
        story,
        state_path,
        state,
        state_sha256,
        queue_path,
        output,
        selected_queue_ids=selected_queue_ids,
    )


def _list_review_items_from_read(
    queue: VoiceGenerationQueue,
    story: StoryIndexDocument,
    state_path: Path,
    state: JsonDocument,
    state_sha256: str,
    queue_path: Path,
    output: Path,
    *,
    selected_queue_ids: set[str] | None = None,
) -> tuple[ReviewItem, ...]:
    collection_by_record = {
        (record.line_id, record.text_sha256): collection.collection_id
        for collection in story.collections
        for record in story.records_for_collection(collection.collection_id)
    }
    records: list[ReviewItem] = []
    for item in queue.items:
        if selected_queue_ids is not None and item.queue_id not in selected_queue_ids:
            continue
        result = _inspection_state_items(state).get(item.queue_id)
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "unknown")
        if status not in {"generated", "approved", "failed"}:
            continue
        audio = None
        if result.get("path"):
            audio = _within(
                output,
                _safe_relative(result["path"], "Generated audio"),
                "Generated audio",
            )
        stored_speech_quality = result.get("speech_quality")
        projected_speech_quality = None
        if audio is not None and (
            not isinstance(stored_speech_quality, dict)
            or "analysis_version" not in stored_speech_quality
        ):
            projected_speech_quality = _corrected_legacy_speech_quality(
                str(audio), str(result.get("file_sha256") or "")
            )
        duration, words_per_minute, peak, technical_flags = _review_technical_metrics(
            result,
            item.text,
            projected_speech_quality=projected_speech_quality,
        )
        repair = result.get("failure_repair")
        records.append(
            ReviewItem(
                queue_id=item.queue_id,
                line_id=item.line_id,
                speaker=item.speaker,
                voice_character=_review_voice_character(item, result),
                text=item.text,
                status=status,
                review_status=_optional_text(result.get("review_status")),
                attempts=_integer(result.get("attempts") or 0, "Review attempts"),
                seed=_optional_integer(result.get("seed")),
                last_error=_optional_text(result.get("last_error")),
                audio=audio,
                collection_id=collection_by_record.get(
                    (item.line_id, item.text_sha256)
                ),
                authority=(
                    ReviewAuthority(
                        queue_sha256=str(
                            _required_text(state.get("queue_sha256"), "Queue SHA-256")
                        ),
                        state_sha256=state_sha256,
                        item_sha256=hashlib.sha256(
                            json.dumps(
                                result,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                        audio_sha256=str(result["file_sha256"]),
                    )
                    if status in {"generated", "approved"}
                    else None
                ),
                state=state_path,
                queue=queue_path,
                duration_seconds=duration,
                words_per_minute=words_per_minute,
                peak=peak,
                technical_flags=technical_flags,
                failure_category=(
                    generation_failure_category(result, text=item.text)
                    if status == "failed"
                    else None
                ),
                internal_pause_seconds=_review_internal_pause_seconds(
                    result, failed=status == "failed"
                ),
                repair_strategy=_optional_text(
                    repair.get("strategy") if isinstance(repair, dict) else None
                ),
            )
        )
    if selected_queue_ids is not None:
        projected = {record.queue_id for record in records}
        missing = sorted(selected_queue_ids - projected)
        if missing:
            raise AuthoringWorkbenchError(
                f"Requested review outcomes are unavailable: {missing}"
            )
    return _annotate_pace_advisories(records)


def inspect_generation_readiness(
    workspace_directory: str | Path,
    *,
    queue_ids: Sequence[str] | None = None,
    regenerate_existing: bool = False,
) -> GenerationReadiness:
    if regenerate_existing and queue_ids is None:
        raise AuthoringWorkbenchError(
            "Workspace regeneration requires explicit queue IDs"
        )
    summary = inspect_workspace(workspace_directory)
    loaded_directory, loaded_workspace = _load_workspace(workspace_directory)
    projection_ids = set(
        workspace_audio_event_spoken_projection_queue_ids(
            loaded_workspace, error_type=AuthoringWorkbenchError
        )
    )
    queue = VoiceGenerationQueue.load(summary.queue)
    state_items: StateItems = {}
    state: JsonDocument | None = None
    if summary.state is not None:
        state = load_generation_state(summary.state, summary.queue)
        state_items = _inspection_state_items(state)
    control_workspace = _load_workspace(workspace_directory)[1]
    return _inspect_generation_readiness_from_read(
        loaded_directory,
        loaded_workspace,
        summary,
        queue,
        state,
        queue_ids=queue_ids,
        regenerate_existing=regenerate_existing,
        projection_ids=projection_ids,
        state_items=state_items,
        control_workspace=control_workspace,
    )


def _inspect_generation_readiness_from_read(
    directory: Path,
    workspace: JsonDocument,
    summary: WorkspaceSummary,
    queue: VoiceGenerationQueue,
    state: JsonDocument | None,
    *,
    queue_ids: Sequence[str] | None = None,
    regenerate_existing: bool = False,
    projection_ids: Iterable[str] | None = None,
    state_items: StateItems | None = None,
    control_workspace: JsonDocument | None = None,
) -> GenerationReadiness:
    if regenerate_existing and queue_ids is None:
        raise AuthoringWorkbenchError(
            "Workspace regeneration requires explicit queue IDs"
        )
    projection_ids = (
        set(
            workspace_audio_event_spoken_projection_queue_ids(
                workspace, error_type=AuthoringWorkbenchError
            )
        )
        if projection_ids is None
        else set(projection_ids)
    )
    if state_items is None:
        state_items = {} if state is None else _inspection_state_items(state)
    control_workspace = workspace if control_workspace is None else control_workspace
    selected = _selected_queue_ids(queue, queue_ids)
    candidates, pending, failed = _generation_candidates(
        queue,
        selected,
        state_items,
        projection_ids,
        regenerate_existing=regenerate_existing,
    )
    manifest = summary.voice_manifest
    missing, reasons = _voice_readiness(
        workspace,
        candidates,
        set(),
        manifest,
        directory=directory,
    )
    reasons = (
        *_workspace_control_reasons(control_workspace),
        *reasons,
    )
    if not candidates:
        scope = (
            "pending, failed, or regenerable pending-review"
            if regenerate_existing
            else "pending or failed"
        )
        reasons = (f"No {scope} queue items are selected",)
    return GenerationReadiness(
        selected=len(candidates),
        pending=pending,
        failed=failed,
        ready=len(candidates) - len(missing) if manifest is not None else 0,
        missing_voice=len(missing) if manifest is not None else None,
        blocked_reasons=reasons,
        queue_ids=tuple(item.queue_id for item in candidates),
    )


def _selected_queue_ids(
    queue: VoiceGenerationQueue, queue_ids: Iterable[object] | None
) -> set[str] | None:
    if queue_ids is None:
        return None
    selected = {_required_text(value, "Queue ID") for value in queue_ids}
    unknown = selected - {item.queue_id for item in queue.items}
    if unknown:
        raise AuthoringWorkbenchError(
            "Selected queue IDs are absent from the workspace queue: "
            + ", ".join(sorted(unknown))
        )
    return selected


def _generation_candidates(
    queue: VoiceGenerationQueue,
    selected: set[str] | None,
    state_items: Mapping[str, object],
    projection_ids: set[str],
    *,
    regenerate_existing: bool,
) -> tuple[list[VoiceGenerationQueueItem], int, int]:
    candidates: list[VoiceGenerationQueueItem] = []
    pending = 0
    failed = 0
    for item in queue.items:
        if selected is not None and item.queue_id not in selected:
            continue
        if item.action != "generate" or not (
            is_spoken_queue_item(item) or item.queue_id in projection_ids
        ):
            continue
        result = state_items.get(item.queue_id)
        status = result.get("status") if isinstance(result, dict) else None
        review_status = (
            result.get("review_status") if isinstance(result, dict) else None
        )
        if status == "failed":
            failed += 1
            candidates.append(item)
        elif status is None:
            pending += 1
            candidates.append(item)
        elif (
            regenerate_existing
            and status == "generated"
            and review_status == "pending_review"
        ):
            candidates.append(item)
    return candidates, pending, failed


def inspect_collection_selection(
    workspace_directory: str | Path,
    *,
    collection_ids: Iterable[str] | None = None,
) -> CollectionSelection:
    """Map declared story collections to exact immutable queue identities."""
    directory, workspace = _load_workspace(workspace_directory)
    document = _load_bound_story_document(directory, workspace)
    queue = _load_bound_workspace_queue(directory, workspace)
    selected, record_keys, queue_ids = _collection_selection_scope(
        document, queue, collection_ids
    )
    readiness = inspect_generation_readiness(
        workspace_directory,
        queue_ids=queue_ids,
    )
    return _collection_selection_from_scope(selected, record_keys, queue_ids, readiness)


def _collection_selection_scope(
    document: StoryIndexDocument,
    queue: VoiceGenerationQueue,
    collection_ids: Iterable[str] | None,
) -> tuple[tuple[str, ...], set[tuple[str, str]], tuple[str, ...]]:
    declared = tuple(collection.collection_id for collection in document.collections)
    if collection_ids is None:
        selected = declared
    else:
        requested = {_required_text(value, "Collection ID") for value in collection_ids}
        unknown = requested - set(declared)
        if unknown:
            raise AuthoringWorkbenchError(
                "Selected collection IDs are absent from the story index: "
                + ", ".join(sorted(unknown))
            )
        selected = tuple(value for value in declared if value in requested)
    record_keys = {
        (record.line_id, record.text_sha256)
        for collection_id in selected
        for record in document.records_for_collection(collection_id)
    }
    queue_ids = tuple(
        item.queue_id
        for item in queue.items
        if (item.line_id, item.text_sha256) in record_keys
    )
    return selected, record_keys, queue_ids


def _collection_selection_from_scope(
    selected: tuple[str, ...],
    record_keys: set[tuple[str, str]],
    queue_ids: tuple[str, ...],
    readiness: GenerationReadiness,
) -> CollectionSelection:
    return CollectionSelection(
        collection_ids=selected,
        collection_count=len(selected),
        story_records=len(record_keys),
        queue_items=len(queue_ids),
        queue_ids=queue_ids,
        readiness=readiness,
    )


def list_workspace_collections(
    workspace_directory: str | Path,
) -> tuple[WorkspaceCollection, ...]:
    directory, workspace = _load_workspace(workspace_directory)
    document = _load_bound_story_document(directory, workspace)
    return _workspace_collections_from_document(document)


def _workspace_collections_from_document(
    document: StoryIndexDocument,
) -> tuple[WorkspaceCollection, ...]:
    return tuple(
        WorkspaceCollection(
            collection_id=collection.collection_id,
            title=collection.title,
            kind=collection.kind,
            record_count=len(document.records_for_collection(collection.collection_id)),
        )
        for collection in document.collections
    )


def workspace_voice_snapshot(
    workspace_directory: str | Path,
) -> tuple[WorkspaceVoice, ...]:
    """Load exact hash-bound voice tokens without trusting cached resolved paths."""
    directory, workspace = _load_workspace(workspace_directory)
    return _workspace_voice_snapshot_from_read(directory, workspace)


def _workspace_voice_snapshot_from_read(
    directory: Path, workspace: JsonDocument
) -> tuple[WorkspaceVoice, ...]:
    voices, _controls = _workspace_voice_projection_from_read(
        directory, workspace, verify_controls=True
    )
    return voices


def _workspace_voice_projection_from_read(
    directory: Path, workspace: JsonDocument, *, verify_controls: bool
) -> tuple[tuple[WorkspaceVoice, ...], tuple[tuple[Path, str], ...]]:
    voice = workspace.get("voice_manifest")
    if not isinstance(voice, dict):
        return (), ()
    manifest_path = _within(
        directory,
        _safe_relative(voice.get("path"), "Voice manifest snapshot"),
        "Voice manifest snapshot",
    )
    payload = _read_bound_bytes(
        manifest_path,
        _require_sha256(voice.get("sha256"), "Voice manifest snapshot SHA-256"),
        "Voice manifest snapshot",
    )
    try:
        document = json.loads(payload.decode("utf-8"))
        entries = validate_voice_manifest(document)
    except (UnicodeDecodeError, json.JSONDecodeError, VoiceManifestError) as error:
        raise AuthoringWorkbenchError(
            f"Workspace voice manifest snapshot is invalid: {error}"
        ) from error
    controls = {}
    for control in voice.get("controls", []):
        if not isinstance(control, dict):
            raise AuthoringWorkbenchError("Workspace voice control is malformed")
        path = _within(
            directory,
            _safe_relative(control.get("path"), "Voice reference snapshot"),
            "Voice reference snapshot",
        )
        controls[path] = _require_sha256(
            control.get("sha256"), "Voice reference snapshot SHA-256"
        )
    values = []
    used = set()
    for entry in entries:
        references = []
        for value in entry.references:
            relative = _safe_relative(value, "Voice reference")
            path = _within(manifest_path.parent, relative, "Voice reference")
            expected = controls.get(path)
            if expected is None:
                raise AuthoringWorkbenchError(
                    f"Voice reference is absent from workspace controls: {value!r}"
                )
            if verify_controls:
                _read_bound_bytes(path, expected, "Voice reference snapshot")
            references.append(path)
            used.add(path)
        values.append(
            WorkspaceVoice(
                character=entry.character,
                speaker=entry.speaker,
                aliases=entry.aliases,
                references=tuple(references),
            )
        )
    if used != set(controls):
        raise AuthoringWorkbenchError(
            "Workspace voice control inventory does not match the manifest snapshot"
        )
    return tuple(values), tuple(controls.items())


def _load_bound_story_document(
    directory: Path, workspace: JsonDocument
) -> StoryIndexDocument:
    story = workspace.get("story_index")
    if not isinstance(story, dict):
        raise AuthoringWorkbenchError(
            "Collection selection requires a snapshotted story index"
        )
    path = _within(
        directory,
        _safe_relative(story.get("path"), "Story index snapshot"),
        "Story index snapshot",
    )
    payload = _read_bound_bytes(
        path,
        _require_sha256(story.get("sha256"), "Story index snapshot SHA-256"),
        "Story index snapshot",
    )
    with tempfile.TemporaryDirectory(prefix="vntts-story-snapshot-") as temporary:
        snapshot = Path(temporary) / "story-index.jsonl"
        snapshot.write_bytes(payload)
        try:
            return load_story_index_document(snapshot)
        except StoryIndexError as error:
            raise AuthoringWorkbenchError(str(error)) from error


def immutable_history_timestamps(
    workspace_directory: str | Path,
) -> tuple[ImmutableHistoryTimestamp, ...]:
    """Return friendly timestamps from immutable source and workspace records."""
    directory, workspace = _load_workspace(workspace_directory)
    return _immutable_history_timestamps_from_read(directory, workspace)


def _immutable_history_timestamps_from_read(
    directory: Path, workspace: JsonDocument
) -> tuple[ImmutableHistoryTimestamp, ...]:
    source = workspace.get("source")
    if not isinstance(source, Mapping):
        raise AuthoringWorkbenchError("Workspace source is malformed")
    snapshot_name = _required_text(source.get("snapshot"), "Workspace import snapshot")
    import_sha256 = _required_text(
        source.get("import_sha256"), "Workspace import SHA-256"
    )
    snapshot, snapshot_sha256, _payload = _load_json_snapshot(
        directory / snapshot_name,
        "workspace import snapshot",
    )
    if snapshot_sha256 != import_sha256:
        raise AuthoringWorkbenchError("Workspace import snapshot was modified")
    legacy_job = snapshot.get("legacy_job")
    candidates: list[tuple[str, object]] = []
    if isinstance(legacy_job, dict):
        candidates.extend(
            (
                ("Source created", legacy_job.get("created_at")),
                ("Source updated", legacy_job.get("updated_at")),
            )
        )
    candidates.append(("Imported", snapshot.get("imported_at")))
    candidates.append(("Workspace created", workspace.get("created_at")))
    values: list[tuple[datetime, str, ImmutableHistoryTimestamp]] = []
    for kind, value in candidates:
        parsed = _parse_history_timestamp(value)
        if parsed is None:
            continue
        utc = parsed.astimezone(timezone.utc)
        values.append(
            (
                utc,
                kind,
                ImmutableHistoryTimestamp(
                    kind=kind,
                    instant=utc.isoformat(),
                    display=f"{kind}: {utc:%Y-%m-%d %H:%M:%S} UTC",
                ),
            )
        )
    return tuple(value for _instant, _kind, value in sorted(values))


def _load_workbench_projection_read(
    workspace_directory: str | Path, *, load_projection_details: bool = True
) -> _WorkbenchProjectionRead:
    with shared_workspace_state_reads():
        return _load_workbench_projection_read_scoped(
            workspace_directory, load_projection_details=load_projection_details
        )


def _load_workbench_projection_read_scoped(
    workspace_directory: str | Path,
    *,
    load_projection_details: bool,
) -> _WorkbenchProjectionRead:
    directory, workspace, workspace_sha256 = load_workspace_authority(
        workspace_directory
    )
    queue_path = _within(
        directory, _safe_relative(workspace["queue"], "Queue"), "Queue"
    )
    output = _within(directory, _safe_relative(workspace["output"], "Output"), "Output")
    cached_state = cached_workspace_generation_state(directory, workspace)
    queue = (
        cached_state[0]
        if cached_state is not None
        else _load_bound_workspace_queue(directory, workspace)
    )
    state_path = output / "generation-state.json"
    state = None
    state_sha256 = None
    if state_path.is_file():
        if cached_state is not None:
            state, state_sha256 = cached_state[1], cached_state[3]
        else:
            state_sha256 = sha256_file(state_path)
            try:
                state = load_generation_state(state_path, queue_path)
            except BulkGenerationError as error:
                raise AuthoringWorkbenchError(str(error)) from error
        if sha256_file(state_path) != state_sha256:
            raise AuthoringWorkbenchError(
                "Generation state changed while review rows were being projected"
            )
    queue_sha256 = workspace_queue_sha256(workspace, error_type=AuthoringWorkbenchError)
    if sha256_file(queue_path) != queue_sha256:
        raise AuthoringWorkbenchError("Workspace queue was modified")
    story = (
        _load_bound_story_document(directory, workspace)
        if load_projection_details
        else None
    )
    voices, voice_controls = (
        _workspace_voice_projection_from_read(
            directory, workspace, verify_controls=False
        )
        if load_projection_details
        else ((), ())
    )
    if sha256_file(directory / "workspace.json") != workspace_sha256:
        raise AuthoringWorkbenchError("Workspace authority changed while it was loaded")
    return _WorkbenchProjectionRead(
        directory=directory,
        workspace=workspace,
        workspace_sha256=workspace_sha256,
        queue_path=queue_path,
        queue=queue,
        output=output,
        state_path=state_path if state is not None else None,
        state=state,
        state_sha256=state_sha256,
        story=story,
        voices=voices,
        voice_controls=voice_controls,
    )


def load_workbench_projection_data(
    workspace_directory: str | Path,
    selected_collection_ids: Iterable[str] | None = None,
    *,
    local_process_id: int | None = None,
    local_process_started_at: str | None = None,
) -> WorkbenchProjectionData:
    """Build one full UI projection from one bounded authority read."""
    read = _load_workbench_projection_read(workspace_directory)
    summary = _inspect_workspace_from_read(
        read.directory,
        read.workspace,
        read.queue_path,
        read.output,
        read.queue,
        read.state_path,
        read.state,
        local_process_id=local_process_id,
        local_process_started_at=local_process_started_at,
    )
    reviews = (
        ()
        if read.state_path is None or read.state is None or read.state_sha256 is None
        else _list_review_items_from_read(
            read.queue,
            read.story,
            read.state_path,
            read.state,
            read.state_sha256,
            read.queue_path,
            read.output,
        )
    )
    collections = _workspace_collections_from_document(read.story)
    declared = tuple(value.collection_id for value in collections)
    if selected_collection_ids is None:
        selected = declared
    else:
        requested = set(selected_collection_ids)
        selected = tuple(value for value in declared if value in requested)
    selected, record_keys, queue_ids = _collection_selection_scope(
        read.story, read.queue, selected
    )
    readiness = _inspect_generation_readiness_from_read(
        read.directory,
        read.workspace,
        summary,
        read.queue,
        read.state,
        queue_ids=queue_ids,
    )
    collection_selection = _collection_selection_from_scope(
        selected, record_keys, queue_ids, readiness
    )
    history = _immutable_history_timestamps_from_read(read.directory, read.workspace)
    return WorkbenchProjectionData(
        summary=summary,
        reviews=tuple(reviews),
        workspace=read.workspace,
        collections=collections,
        collection_selection=collection_selection,
        history=history,
        voices=read.voices,
        _voice_controls=read.voice_controls,
    )


def generation_command(
    workspace_directory: str | Path,
    *,
    backend: str | None = None,
    voice_manifest: str | Path | None = None,
    model: str | None = None,
    generation_profile: str | None = None,
    narrator_character: str | None = None,
    retries: int = 2,
    seed: int = 0,
    include_prefer_source: bool = False,
    queue_ids: Sequence[str] | None = None,
    regenerate_existing: bool = False,
) -> tuple[str, ...]:
    if include_prefer_source:
        raise AuthoringWorkbenchError(
            "Recoverable source-audio generation requires an explicit preflight policy"
        )
    if regenerate_existing and queue_ids is None:
        raise AuthoringWorkbenchError(
            "Workspace regeneration requires explicit queue IDs"
        )
    read = _load_workbench_projection_read(
        workspace_directory, load_projection_details=False
    )
    directory, workspace = read.directory, read.workspace
    policy, repair_policy, projection_ids, queue_ids = _generation_scope(
        workspace, queue_ids, retries
    )
    backend, model, generation_profile = _generation_run_config(
        workspace,
        backend=backend,
        model=model,
        generation_profile=generation_profile,
    )
    summary = _inspect_workspace_from_read(
        directory,
        workspace,
        read.queue_path,
        read.output,
        read.queue,
        read.state_path,
        read.state,
        voice_manifest=voice_manifest,
    )
    readiness = _inspect_generation_readiness_from_read(
        directory,
        workspace,
        summary,
        read.queue,
        read.state,
        queue_ids=queue_ids,
        regenerate_existing=regenerate_existing,
    )
    if readiness.blocked_reasons:
        raise AuthoringWorkbenchError("; ".join(readiness.blocked_reasons))
    manifest = summary.voice_manifest
    if manifest is None:
        raise AuthoringWorkbenchError("Select an existing voice manifest")
    if backend not in {"pocket-tts", "chatterbox-nano", "moss-tts"}:
        raise AuthoringWorkbenchError(f"Unsupported generation backend: {backend!r}")
    configured_narrator = _configured_narrator(workspace, narrator_character)
    command = _base_generation_command(
        directory,
        summary,
        manifest,
        backend,
        configured_narrator,
        retries,
        seed,
    )
    _append_generation_options(
        command,
        model=model,
        generation_profile=generation_profile,
        policy=policy,
        repair_policy=repair_policy,
        projection_ids=projection_ids,
        queue_ids=queue_ids,
        regenerate_existing=regenerate_existing,
    )
    return tuple(command)


def _generation_scope(
    workspace: dict[str, object], queue_ids: Sequence[str] | None, retries: int
) -> tuple[
    MissingVoicePolicy,
    FailureRepairPolicy,
    tuple[str, ...],
    Sequence[str] | None,
]:
    policy = _workspace_missing_voice_policy(workspace)
    repair_policy = _workspace_failure_repair_policy(workspace)
    projection_ids = workspace_audio_event_spoken_projection_queue_ids(
        workspace, error_type=AuthoringWorkbenchError
    )
    if not repair_policy.is_empty:
        queue_ids = _bound_queue_ids(
            queue_ids,
            repair_policy.queue_ids,
            "Generation queue IDs differ from workspace failure-repair policy",
        )
    if projection_ids:
        queue_ids = _bound_queue_ids(
            queue_ids,
            projection_ids,
            "Generation queue IDs differ from workspace audio-event projections",
        )
    if repair_policy.offline_fallback_queue_ids and retries != 0:
        raise AuthoringWorkbenchError(
            "Offline fallback is a single backend-owned unseeded attempt; set retries to 0"
        )
    return policy, repair_policy, projection_ids, queue_ids


def _bound_queue_ids(
    queue_ids: Sequence[str] | None,
    required_ids: Sequence[str],
    mismatch_message: str,
) -> Sequence[str]:
    if queue_ids is None:
        return required_ids
    if set(queue_ids) != set(required_ids):
        raise AuthoringWorkbenchError(mismatch_message)
    return queue_ids


def _generation_run_config(
    workspace: dict[str, object],
    *,
    backend: str | None,
    model: str | None,
    generation_profile: str | None,
) -> tuple[str, str | None, str | None]:
    run_config = workspace["run_config"]
    if not isinstance(run_config, dict):
        raise AuthoringWorkbenchError("Workspace run config must be an object")
    configured_backend = _optional_text(run_config.get("backend"))
    configured_model = _optional_text(run_config.get("model"))
    configured_profile = _optional_text(run_config.get("generation_profile"))
    if configured_backend is None:
        raise AuthoringWorkbenchError(
            "Create a config-addressed workspace with a generation backend"
        )
    if backend is not None and backend != configured_backend:
        raise AuthoringWorkbenchError(
            "Generation backend differs from workspace config"
        )
    if model is not None and model != configured_model:
        raise AuthoringWorkbenchError("Generation model differs from workspace config")
    if generation_profile is not None and generation_profile != configured_profile:
        raise AuthoringWorkbenchError(
            "Generation profile differs from workspace config"
        )
    return configured_backend, configured_model, configured_profile


def _configured_narrator(
    workspace: dict[str, object], narrator_character: str | None
) -> str:
    configured = workspace.get("narrator_character")
    if narrator_character is not None and narrator_character != configured:
        raise AuthoringWorkbenchError(
            "Persist the narrator selection in workspace configuration before generation"
        )
    return str(_required_text(configured, "Narrator character"))


def _base_generation_command(
    directory: Path,
    summary: WorkspaceSummary,
    manifest: Path,
    backend: str,
    configured_narrator: str,
    retries: int,
    seed: int,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vntts.authoring.cli",
        "generate",
        "--workspace",
        str(directory),
        "--queue",
        str(summary.queue),
        "--output",
        str(summary.output),
        "--voice-manifest",
        str(manifest.resolve()),
        "--backend",
        backend,
        "--narrator-character",
        configured_narrator,
        "--retries",
        str(_nonnegative_integer(retries, "Retries")),
        "--seed",
        str(_integer(seed, "Seed")),
    ]


def _append_generation_options(
    command: list[str],
    *,
    model: str | None,
    generation_profile: str | None,
    policy: MissingVoicePolicy,
    repair_policy: FailureRepairPolicy,
    projection_ids: Sequence[str],
    queue_ids: Sequence[str] | None,
    regenerate_existing: bool,
) -> None:
    if model:
        command.extend(("--model", str(model)))
    if generation_profile:
        command.extend(("--generation-profile", generation_profile))
    _append_missing_voice_options(command, policy)
    _append_repair_options(command, repair_policy)
    _append_queue_options(command, projection_ids, queue_ids, regenerate_existing)


def _append_missing_voice_options(
    command: list[str], policy: MissingVoicePolicy
) -> None:
    if policy.mode == NARRATOR_ALL_UNRESOLVED:
        command.append("--narrator-fallback-all")
    elif policy.mode == NARRATOR_ROLES:
        for role in policy.roles:
            command.extend(("--narrator-fallback-role", role))


def _append_repair(command: list[str], flag: str, queue_ids: Iterable[str]) -> None:
    for queue_id in queue_ids:
        command.extend((flag, queue_id))


def _append_repair_options(
    command: list[str], repair_policy: FailureRepairPolicy
) -> None:
    _append_repair(
        command, "--sentence-segment-failed", repair_policy.sentence_segment_queue_ids
    )
    _append_repair(
        command, "--trim-edge-silence-failed", repair_policy.edge_silence_queue_ids
    )
    _append_repair(
        command, "--bounded-seed-failed", repair_policy.bounded_seed_retry_queue_ids
    )
    _append_repair(
        command, "--offline-fallback-failed", repair_policy.offline_fallback_queue_ids
    )
    _append_repair(
        command, "--inline-pause-failed", repair_policy.inline_pause_queue_ids
    )
    if repair_policy.segment_pause_ms != 180:
        command.extend(("--segment-pause-ms", str(repair_policy.segment_pause_ms)))
    if repair_policy.inline_pause_ms != 180:
        command.extend(("--inline-pause-ms", str(repair_policy.inline_pause_ms)))


def _append_queue_options(
    command: list[str],
    projection_ids: Sequence[str],
    queue_ids: Sequence[str] | None,
    regenerate_existing: bool,
) -> None:
    _append_repair(command, "--audio-event-spoken-projection", projection_ids)
    if queue_ids is not None:
        _append_repair(
            command,
            "--queue-id",
            (_required_text(queue_id, "Queue ID") for queue_id in queue_ids),
        )
    if regenerate_existing:
        command.append("--regenerate-existing")


def generation_control_bindings(
    workspace_directory: str | Path,
    *,
    queue: str | Path,
    output: str | Path,
    voice_manifest: str | Path,
    backend: str,
    model: str | None,
    generation_profile: str | None,
    narrator_character: str,
    missing_voice_policy: object = None,
    failure_repair_policy: object = None,
    audio_event_spoken_projection_queue_ids: Iterable[object] | None = None,
) -> dict[Path, str]:
    directory, workspace = _load_workspace(workspace_directory)
    selected_manifest = _selected_voice_manifest(directory, workspace)
    selected_manifest = _validate_generation_paths(
        directory,
        queue=queue,
        output=output,
        voice_manifest=voice_manifest,
        selected_manifest=selected_manifest,
    )
    policy = _missing_voice_policy(missing_voice_policy)
    repair_policy = _failure_repair_policy(failure_repair_policy)
    run_config = workspace["run_config"]
    expected: dict[str, object] = {
        "backend": backend,
        "model": model,
        "generation_profile": generation_profile,
        "missing_voice_policy": policy.to_document(),
        "failure_repair_policy": repair_policy.to_document(),
    }
    projection_ids = _generation_projection_ids(audio_event_spoken_projection_queue_ids)
    if projection_ids:
        expected["audio_event_spoken_projection_queue_ids"] = list(projection_ids)
    if _workspace_run_config_with_policy(run_config) != expected:
        raise AuthoringWorkbenchError("Generation run config differs from workspace")
    if narrator_character != workspace["narrator_character"]:
        raise AuthoringWorkbenchError(
            "Narrator selection differs from workspace config"
        )
    return _generation_voice_control_bindings(directory, workspace, selected_manifest)


def _validate_generation_paths(
    directory: Path,
    *,
    queue: str | Path,
    output: str | Path,
    voice_manifest: str | Path,
    selected_manifest: Path | None,
) -> Path:
    if Path(queue).expanduser().resolve() != (directory / "queue.jsonl").resolve():
        raise AuthoringWorkbenchError("Generation queue differs from workspace config")
    if Path(output).expanduser().resolve() != (directory / "generated-audio").resolve():
        raise AuthoringWorkbenchError("Generation output differs from workspace config")
    if selected_manifest is None or (
        Path(voice_manifest).expanduser().resolve() != selected_manifest
    ):
        raise AuthoringWorkbenchError(
            "Generation voice manifest differs from workspace config"
        )
    return selected_manifest


def _missing_voice_policy(value: object) -> MissingVoicePolicy:
    try:
        return (
            value
            if isinstance(value, MissingVoicePolicy)
            else MissingVoicePolicy.from_document(value)
        )
    except MissingVoicePolicyError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _failure_repair_policy(value: object) -> FailureRepairPolicy:
    try:
        return (
            value
            if isinstance(value, FailureRepairPolicy)
            else FailureRepairPolicy.from_document(value)
        )
    except FailureRepairPolicyError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _generation_projection_ids(values: Iterable[object] | None) -> tuple[str, ...]:
    queue_ids = tuple(
        sorted(
            _required_text(value, "Audio-event spoken projection queue ID")
            for value in (values or ())
        )
    )
    if len(queue_ids) != len(set(queue_ids)):
        raise AuthoringWorkbenchError(
            "Audio-event spoken projection queue IDs must be unique"
        )
    return queue_ids


def _generation_voice_control_bindings(
    directory: Path, workspace: dict[str, object], selected_manifest: Path
) -> dict[Path, str]:
    voice = workspace["voice_manifest"]
    if not isinstance(voice, dict):
        raise AuthoringWorkbenchError("Workspace voice manifest must be an object")
    bindings = {
        selected_manifest: _required_text(voice.get("sha256"), "Voice manifest SHA-256")
    }
    for control in voice["controls"]:
        path = _within(
            directory,
            _safe_relative(control["path"], "Voice reference snapshot"),
            "Voice reference snapshot",
        )
        bindings[path] = control["sha256"]
    runtime_binding = _failure_reference_runtime_binding(directory, workspace)
    if runtime_binding is not None:
        bindings.update(runtime_binding.controls)
    return bindings


def generation_output_identity(workspace_directory: str | Path) -> dict[str, str | int]:
    directory, _workspace = _load_workspace(workspace_directory)
    output = directory / "generated-audio"
    metadata = output.stat(follow_symlinks=False)
    return {
        "path": str(output),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


def _active_attempt(value: object, eligible_ids: set[str]) -> ActiveAttempt | None:
    if not isinstance(value, dict) or value.get("queue_id") not in eligible_ids:
        return None
    return ActiveAttempt(
        queue_id=_optional_text(value.get("queue_id")),
        line_id=_optional_text(value.get("line_id")),
        speaker=_optional_text(value.get("speaker") or value.get("voice_character")),
        text=_optional_text(value.get("text")),
        phase=_optional_text(value.get("phase")),
        attempt=_optional_integer(value.get("attempt")),
        attempt_limit=_optional_integer(value.get("attempt_limit")),
        total_attempts=_optional_integer(value.get("total_attempts")),
        seed=_optional_integer(value.get("seed")),
        started_at=_optional_text(value.get("started_at")),
        updated_at=_optional_text(value.get("updated_at")),
        last_error=_optional_text(value.get("last_error")),
    )


def _runtime_status(
    output: Path,
    active: ActiveAttempt | None,
    pending: int,
    review_pending: int,
    failed: int,
    missing_voice: int | None,
    blocked_reasons: Sequence[str],
    *,
    queue_sha256: str,
    local_process_id: int | None,
    local_process_started_at: str | None,
    process_checker: Callable[[int], bool],
    process_start_checker: Callable[[int], str | None],
) -> AuthoringRuntimeStatus:
    lease_path = output / ".generation-lease.json"
    if lease_path.is_file():
        return _leased_runtime_status(
            lease_path,
            queue_sha256=queue_sha256,
            local_process_id=local_process_id,
            local_process_started_at=local_process_started_at,
            process_checker=process_checker,
            process_start_checker=process_start_checker,
        )
    return _unleased_runtime_status(
        active,
        pending,
        review_pending,
        failed,
        missing_voice,
        blocked_reasons,
    )


def _leased_runtime_status(
    lease_path: Path,
    *,
    queue_sha256: str,
    local_process_id: int | None,
    local_process_started_at: str | None,
    process_checker: Callable[[int], bool],
    process_start_checker: Callable[[int], str | None],
) -> AuthoringRuntimeStatus:
    try:
        lease = _load_json(lease_path, "generation lease")
    except AuthoringWorkbenchError:
        return AuthoringRuntimeStatus.BLOCKED
    if (
        lease.get("schema") != LEASE_SCHEMA
        or lease.get("schema_version") != LEASE_VERSION
        or lease.get("queue_sha256") != queue_sha256
    ):
        return AuthoringRuntimeStatus.BLOCKED
    pid = lease.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return AuthoringRuntimeStatus.BLOCKED
    if lease.get("hostname") not in {None, socket.gethostname()}:
        return AuthoringRuntimeStatus.RUNNING_EXTERNAL
    if not process_checker(pid):
        return AuthoringRuntimeStatus.INTERRUPTED
    recorded_start = lease.get("process_started_at")
    actual_start = process_start_checker(pid)
    if (
        recorded_start is not None
        and actual_start is not None
        and actual_start != recorded_start
    ):
        return AuthoringRuntimeStatus.INTERRUPTED
    if (
        local_process_id is not None
        and pid == int(local_process_id)
        and (recorded_start is None or local_process_started_at == recorded_start)
    ):
        return AuthoringRuntimeStatus.RUNNING_HERE
    return AuthoringRuntimeStatus.RUNNING_EXTERNAL


def _unleased_runtime_status(
    active: ActiveAttempt | None,
    pending: int,
    review_pending: int,
    failed: int,
    missing_voice: int | None,
    blocked_reasons: Sequence[str],
) -> AuthoringRuntimeStatus:
    if active is not None:
        return AuthoringRuntimeStatus.INTERRUPTED
    if review_pending:
        return AuthoringRuntimeStatus.NEEDS_REVIEW
    if failed:
        return AuthoringRuntimeStatus.NEEDS_ATTENTION
    if blocked_reasons:
        return AuthoringRuntimeStatus.BLOCKED
    if pending:
        return AuthoringRuntimeStatus.READY
    if missing_voice:
        return AuthoringRuntimeStatus.NEEDS_ATTENTION
    return AuthoringRuntimeStatus.COMPLETE


def _voice_readiness(
    workspace: WorkspaceDocument,
    spoken: Iterable[VoiceGenerationQueueItem],
    completed_ids: set[str],
    manifest_path: str | Path | None,
    *,
    directory: Path | None = None,
) -> tuple[set[str], tuple[str, ...]]:
    if manifest_path is None:
        return set(), ("Select an existing voice manifest",)
    registry, queue_overrides = _load_voice_routing(manifest_path)
    if directory is None:
        directory = Path(manifest_path).expanduser().resolve().parents[2]
    registry, queue_overrides = _extend_voice_routing(
        directory, workspace, registry, queue_overrides
    )
    narrator = str(workspace.get("narrator_character") or "Narrator")
    policy = _workspace_missing_voice_policy(workspace)
    narrator_ready = not _voice_missing(registry.resolve(narrator))
    missing = set()
    for item in spoken:
        if item.queue_id in completed_ids:
            continue
        requested_character = synthesis_character_for_line(
            item.speaker, item.voice_character
        )
        character = queue_overrides.get(item.queue_id) or (
            narrator if requested_character == "Narrator" else requested_character
        )
        voice_missing = _voice_missing(
            registry.resolve(character or item.speaker or "")
        )
        if voice_missing and policy.applies_to(requested_character) and narrator_ready:
            continue
        if voice_missing:
            missing.add(item.queue_id)
    if missing:
        return missing, (
            f"Voice references are missing or unsafe for {len(missing)} queued line(s)",
        )
    return missing, ()


def _load_voice_routing(
    manifest_path: str | Path,
) -> tuple[CharacterVoiceRegistry, dict[str, str]]:
    try:
        registry = CharacterVoiceRegistry.from_file(manifest_path)
        document, entries = load_voice_manifest(manifest_path, allow_legacy=False)
        overrides = queue_voice_overrides_from_manifest(document, voices=entries)
    except (SourceReferenceBindingError, VoiceManifestError, OSError) as error:
        raise AuthoringWorkbenchError(
            f"Unable to load voice manifest: {error}"
        ) from error
    return registry, overrides


def _extend_voice_routing(
    directory: Path,
    workspace: WorkspaceDocument,
    registry: CharacterVoiceRegistry,
    queue_overrides: dict[str, str],
) -> tuple[CharacterVoiceRegistry, dict[str, str]]:
    runtime_binding = _failure_reference_runtime_binding(directory, workspace)
    if runtime_binding is None:
        return registry, queue_overrides
    try:
        registry = CharacterVoiceRegistry(
            (*registry.unique_voices(), *runtime_binding.voices)
        )
    except VoiceManifestError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    return registry, {
        **queue_overrides,
        **runtime_binding.queue_voice_overrides,
    }


def _voice_missing(voice: CharacterVoice | None) -> bool:
    return (
        voice is None
        or not voice.references
        or any(not reference.is_file() for reference in voice.references)
    )


def inspect_voice_readiness(
    workspace: Mapping[str, object],
    spoken: Iterable[VoiceGenerationQueueItem],
    completed_ids: set[str],
    manifest_path: str | Path | None,
    *,
    directory: Path | None = None,
) -> tuple[set[str], tuple[str, ...]]:
    """Project exact missing-voice IDs through the workbench policy."""
    return _voice_readiness(
        dict(workspace),
        spoken,
        completed_ids,
        manifest_path,
        directory=directory,
    )


def _workspace_control_reasons(workspace: Mapping[str, object]) -> tuple[str, ...]:
    run_config = workspace.get("run_config", {})
    if not isinstance(run_config, Mapping):
        run_config = {}
    missing = [
        label
        for field, label in (
            ("backend", "generation backend"),
            ("model", "generation model"),
            ("generation_profile", "generation profile"),
        )
        if not _optional_text(run_config.get(field))
    ]
    if not missing:
        return ()
    return ("Workspace requires " + ", ".join(missing),)


def _latest_outcome(
    queue: VoiceGenerationQueue, relevant: Mapping[str, JsonDocument]
) -> tuple[str | None, str | None, str | None, str | None]:
    if not relevant:
        return None, None, None, None
    queue_by_id = {item.queue_id: item for item in queue.items}
    queue_id, value = max(
        relevant.items(), key=lambda pair: str(pair[1].get("updated_at") or "")
    )
    item = queue_by_id.get(queue_id)
    return (
        None if item is None else item.line_id,
        None if item is None else item.text,
        str(value.get("review_status") or value.get("status") or "unknown"),
        _optional_text(value.get("updated_at")),
    )


def _optional_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise AuthoringWorkbenchError(f"{label} must be an integer")
    if not isinstance(value, (str, bytes, bytearray, SupportsInt, SupportsIndex)):
        raise AuthoringWorkbenchError(f"{label} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise AuthoringWorkbenchError(f"{label} must be an integer") from error


def _nonnegative_integer(value: object, label: str) -> int:
    result = _integer(value, label)
    if result < 0:
        raise AuthoringWorkbenchError(f"{label} must not be negative")
    return result


__all__ = [
    "_active_attempt",
    "_annotate_pace_advisories",
    "_collection_selection_from_scope",
    "_collection_selection_scope",
    "_corrected_legacy_speech_quality",
    "_immutable_history_timestamps_from_read",
    "_inspect_generation_readiness_from_read",
    "_inspect_workspace_from_read",
    "_integer",
    "_latest_outcome",
    "_list_review_items_from_read",
    "_load_bound_story_document",
    "_load_workbench_projection_read",
    "_load_workbench_projection_read_scoped",
    "_nonnegative_integer",
    "_normalize_review_queue_ids",
    "_optional_integer",
    "_pace_length_bucket",
    "_pace_voice_key",
    "_pace_word_count",
    "_review_internal_pause_seconds",
    "_review_technical_metrics",
    "_review_voice_character",
    "_runtime_status",
    "_voice_readiness",
    "_workspace_collections_from_document",
    "_workspace_control_reasons",
    "_workspace_voice_projection_from_read",
    "_workspace_voice_snapshot_from_read",
    "discover_imports",
    "discover_workspaces",
    "generation_command",
    "generation_control_bindings",
    "generation_failure_category",
    "generation_output_identity",
    "immutable_history_timestamps",
    "inspect_collection_selection",
    "inspect_generation_readiness",
    "inspect_voice_readiness",
    "inspect_workspace",
    "list_review_items",
    "list_workspace_collections",
    "load_workbench_projection_data",
    "workspace_voice_snapshot",
]

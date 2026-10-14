"""Failure outcome records, reporting and deterministic recovery planning."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import TypeAlias, TypedDict

from vntts.authoring.authority import canonical_document_sha256 as _canonical_sha256
from vntts.authoring.failure_repair import (
    BOUNDED_SEED_RETRY,
    MAX_BOUNDED_TOTAL_ATTEMPTS,
    SENTENCE_BOUNDARY_SEGMENTATION,
    inline_sentence_pause_prompt,
    safe_sentence_segments,
)
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_state import FAILURE_KINDS, load_stable_generation_queue
from vntts.authoring.generation_state import generation_state_items as _state_items
from vntts.authoring.generation_state import provider_attempts as _provider_attempts
from vntts.authoring.generation_state import required_state_text as _generation_text
from vntts.authoring.generation_state import state_nonnegative_int as _nonnegative_int
from vntts.authoring.generation_state import (
    validate_generation_state_document as _validate_state_document,
)
from vntts.authoring.speech_quality import (
    MAX_INTERNAL_SILENCE_SECONDS,
    MAX_LEADING_SILENCE_SECONDS,
    MAX_SILENCE_RATIO,
    MAX_TRAILING_SILENCE_SECONDS,
    NOTABLE_SILENCE_SPAN_SECONDS,
    SpeechSilenceValidationError,
)
from vntts.authoring.speech_quality import (
    text_failure_features as _text_failure_features,
)
from vntts.authoring.workspace_foundation import load_json_object_snapshot
from vntts.document_identity import is_lowercase_sha256
from vntts.json_types import is_json_object as _is_json_document
from vntts.numeric_values import optional_finite_number
from vntts.synthesis import SynthesisCompletion, SynthesisResult
from vntts.voices import synthesis_character_for_line

JsonDocument: TypeAlias = dict[str, object]


class _FailureReportRecord(TypedDict):
    queue_id: str
    line_id: str
    speaker: str
    text: str
    requested_voice_character: object
    synthesis_voice_character: object
    provider: object
    model: object
    generation_profile: object
    synthesis_control_digest: object
    attempts: int
    attempts_by_provider: dict[str, int]
    seed: object
    last_error: object
    failure: JsonDocument
    failure_repair: object
    failure_repair_history: list[str]


class _CohortCount(TypedDict):
    value: object
    count: int


class _GenerationFailureReport(TypedDict):
    schema: str
    schema_version: int
    state: str
    state_sha256: str
    queue: str
    queue_sha256: str
    failure_count: int
    cohorts: dict[str, list[_CohortCount]]
    records: list[_FailureReportRecord]


class IncompleteSynthesisError(BulkGenerationError):
    """A typed renderer stopped without producing a publishable completion."""

    __module__ = "vntts.authoring.bulk_generation"

    def __init__(self, result: SynthesisResult) -> None:
        self.result = result
        super().__init__(
            "Typed render completed as "
            f"{result.completion.value} "
            f"(sample_count={result.diagnostics.sample_count}, "
            f"chunk_count={result.diagnostics.chunk_count}, "
            f"max_audio_seconds={result.limits.max_audio_seconds}, "
            f"max_tokens={result.limits.max_tokens}); WAV was not published"
        )


def failure_kind(error: object, completion: SynthesisCompletion | None = None) -> str:
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


def failure_record(
    error: object,
    *,
    text: str,
    completion: SynthesisCompletion | None = None,
    attempt_binding: object | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "kind": failure_kind(error, completion),
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
            pause_diagnosis: JsonDocument = asdict(error.diagnosis)
            pause_diagnosis["attempt_binding"] = copy.deepcopy(attempt_binding)
            record["pause_diagnosis"] = pause_diagnosis
    return record


def normalized_failure_record(item: object, *, text: str = "") -> JsonDocument:
    """Return a typed failure record, inferring legacy string-only outcomes."""
    document = item if _is_json_document(item) else {}
    stored = document.get("failure")
    if _is_json_document(stored) and stored.get("kind") in FAILURE_KINDS:
        return copy.deepcopy(stored)
    error = str(document.get("last_error") or "Unknown generation failure")
    return {
        "schema_version": 1,
        "kind": failure_kind(error),
        "error_type": "LegacyStringFailure",
        "text_features": _text_failure_features(text),
        "inferred_from_legacy_error": True,
    }


def generation_failure_report(
    state_path: str | Path, queue_path: str | Path
) -> _GenerationFailureReport:
    """Project current and legacy failures into stable, actionable cohorts."""
    state_path = Path(state_path).expanduser().resolve()
    queue_path = Path(queue_path).expanduser().resolve()
    queue, queue_sha256 = load_stable_generation_queue(queue_path)
    state_document, state_sha256, _payload = load_json_object_snapshot(
        state_path, "generation state", error_type=BulkGenerationError
    )
    state = _validate_state_document(
        state_document, state_path.parent, queue, queue_sha256
    )
    queue_by_id = {item.queue_id: item for item in queue.items}
    records: list[_FailureReportRecord] = []
    for queue_id, result in _state_items(state).items():
        if result.get("status") != "failed":
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
                "failure_repair_history": failure_repair_history(result),
            }
        )
    records.sort(key=lambda value: value["queue_id"])

    def counts(
        key: Callable[[_FailureReportRecord], object],
    ) -> list[_CohortCount]:
        grouped: dict[str, _CohortCount] = {}
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
        "state_sha256": state_sha256,
        "queue": str(queue_path),
        "queue_sha256": _generation_text(state.get("queue_sha256"), "Queue SHA-256"),
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


def generation_failure_repair_plan(
    state_path: str | Path, queue_path: str | Path
) -> JsonDocument:
    """Return deterministic exact-ID repair candidates without changing state."""
    report = generation_failure_report(state_path, queue_path)
    planned: list[JsonDocument] = []
    for record in report["records"]:
        action, reason, previous_strategy = _failure_repair_action(record)
        planned.append(
            {
                "queue_id": record["queue_id"],
                "line_id": record["line_id"],
                "speaker": record["speaker"],
                "requested_voice_character": record["requested_voice_character"],
                "synthesis_voice_character": record["synthesis_voice_character"],
                "failure_kind": record["failure"]["kind"],
                "provider": record["provider"],
                "attempts": record["attempts"],
                "seed": record["seed"],
                "attempted_repair_strategy": previous_strategy,
                "action": action,
                "reason": reason,
            }
        )
    action_counts: dict[str, int] = {}
    for planned_record in planned:
        action = _generation_text(planned_record.get("action"), "Repair action")
        action_counts[action] = action_counts.get(action, 0) + 1
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


def _failure_repair_action(record: _FailureReportRecord) -> tuple[str, str, object]:
    previous_repair = record.get("failure_repair")
    previous_strategy = (
        previous_repair.get("strategy") if isinstance(previous_repair, dict) else None
    )
    if not _failure_has_bound_synthesis_controls(record):
        return (
            "provenance_recovery_or_regeneration",
            "legacy failure lacks exact provider, model, generation profile or synthesis-control provenance; recover immutable evidence or regenerate under current controls before selecting a repair",
            previous_strategy,
        )
    action, reason = _bound_failure_repair_action(record, previous_strategy)
    return action, reason, previous_strategy


def _bound_failure_repair_action(
    record: _FailureReportRecord, previous_strategy: object
) -> tuple[str, str]:
    kind = record["failure"]["kind"]
    if previous_strategy == BOUNDED_SEED_RETRY and kind == "missed_eos_audio_limit":
        return _bounded_repair_action(
            record,
            "the current-provider bounded repair still has an attempt",
            "the current-provider bounded repair is exhausted",
        )
    if previous_strategy == SENTENCE_BOUNDARY_SEGMENTATION:
        return _segmented_failure_repair_action(record, kind)
    if kind == "missed_eos_audio_limit":
        return _bounded_repair_action(
            record,
            "fewer than three completed attempts for the current provider",
            "current-provider bounded attempts are exhausted",
        )
    if kind == "speech_silence":
        return _silence_failure_repair_action(record)
    if kind == "reference_unavailable":
        return "reference_discovery", "no exact synthesis reference was available"
    if kind in {"cancelled", "interrupted"}:
        return "safe_resume", "the attempt did not reach a terminal synthesis result"
    return "backend_diagnosis", "the typed backend failure has no safe automatic repair"


def _bounded_repair_action(
    record: _FailureReportRecord,
    available: str,
    exhausted: str,
    *,
    action: str = "bounded_seed_retry",
) -> tuple[str, str]:
    attempts = record["attempts_by_provider"].get(_failure_report_provider(record), 0)
    return (
        (action, available)
        if attempts < MAX_BOUNDED_TOTAL_ATTEMPTS
        else ("offline_fallback_backend", exhausted)
    )


def _segmented_failure_repair_action(
    record: _FailureReportRecord, kind: object
) -> tuple[str, str]:
    if kind == "missed_eos_audio_limit":
        return _bounded_repair_action(
            record,
            "sentence segmentation failed, but one bounded current-provider attempt remains",
            "bounded sentence segmentation failed and current-provider attempts are exhausted",
        )
    if kind == "speech_silence":
        return (
            "reference_comparison",
            "segmented output still failed the speech-silence gate and requires listening and reference audit",
        )
    return (
        "backend_diagnosis",
        "sentence segmentation already failed with an unrelated typed backend outcome",
    )


def _silence_failure_repair_action(record: _FailureReportRecord) -> tuple[str, str]:
    failure, text = record["failure"], record["text"]
    if SENTENCE_BOUNDARY_SEGMENTATION in record.get(
        "failure_repair_history", []
    ) and sentence_repair_matches_failure(failure, text):
        return (
            "reference_comparison",
            "speech-silence remained after an earlier sentence-boundary repair and requires listening or reference audit",
        )
    if sentence_repair_matches_failure(failure, text):
        return (
            "sentence_boundary_segmentation",
            "internal silence between multiple complete sentences",
        )
    if inline_pause_matches_failure(failure, text):
        return _bounded_repair_action(
            record,
            "sentence-boundary silence cannot use safe independent segments and bounded attempts remain",
            "bounded inline-pause attempts are exhausted",
            action="inline_pause_marker_comparison",
        )
    if edge_silence_only(failure):
        return (
            "edge_silence_trim",
            "only measured boundary silence exceeds the speech gate",
        )
    return (
        "reference_comparison",
        "internal or untyped silence requires listening and reference audit",
    )


def edge_silence_only(failure: JsonDocument) -> bool:
    quality = failure.get("speech_quality")
    durations = silence_durations(failure)
    if not isinstance(quality, dict) or durations is None:
        return False
    leading, trailing, longest_internal = durations
    silence_ratio = optional_finite_number(quality.get("silence_ratio"))
    return bool(
        (
            leading > MAX_LEADING_SILENCE_SECONDS
            or trailing > MAX_TRAILING_SILENCE_SECONDS
            or silence_ratio is not None
            and silence_ratio > MAX_SILENCE_RATIO
            and max(leading, trailing) >= NOTABLE_SILENCE_SPAN_SECONDS
        )
        and longest_internal <= MAX_INTERNAL_SILENCE_SECONDS
    )


def failure_repair_history(result: JsonDocument) -> list[str]:
    """Return newest-first repair strategies proven by one outcome chain."""
    observed: list[str] = []

    def remember(value: object) -> None:
        if isinstance(value, str) and value and value not in observed:
            observed.append(value)

    repair = result.get("failure_repair")
    if _is_json_document(repair):
        remember(repair.get("strategy"))
    carry = result.get("carry_forward")
    visited: set[str] = set()
    while _is_json_document(carry):
        digest = _canonical_sha256(carry)
        if digest in visited:
            break
        visited.add(digest)
        remember(carry.get("source_repair_strategy"))
        carry = carry.get("source_parent_carry_forward")
    return observed


def _failure_has_bound_synthesis_controls(record: Mapping[str, object]) -> bool:
    for field in ("provider", "model", "generation_profile"):
        value = record.get(field)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            return False
    digest = record.get("synthesis_control_digest")
    return is_lowercase_sha256(digest)


def _failure_report_provider(record: _FailureReportRecord) -> str:
    provider = record["provider"]
    return provider if isinstance(provider, str) else "legacy-unbound"


def silence_durations(
    failure: JsonDocument,
) -> tuple[float, float, float] | None:
    quality = failure.get("speech_quality")
    if not isinstance(quality, dict):
        return None

    def duration(name: str) -> float | None:
        value = optional_finite_number(quality.get(name))
        return value if value is not None and value >= 0 else None

    leading = duration("leading_silence_seconds")
    trailing = duration("trailing_silence_seconds")
    longest_internal = duration("longest_internal_silence_seconds")
    if leading is None or trailing is None or longest_internal is None:
        return None
    return leading, trailing, longest_internal


def sentence_repair_matches_failure(failure: JsonDocument, text: str) -> bool:
    if len(safe_sentence_segments(text)) < 2:
        return False
    if failure.get("kind") == "missed_eos_audio_limit":
        return True
    if failure.get("kind") != "speech_silence":
        return False
    durations = silence_durations(failure)
    if durations is None:
        return False
    leading, trailing, longest_internal = durations
    return bool(
        longest_internal > MAX_INTERNAL_SILENCE_SECONDS
        and leading <= MAX_LEADING_SILENCE_SECONDS
        and trailing <= MAX_TRAILING_SILENCE_SECONDS
    )


def inline_pause_matches_failure(failure: JsonDocument, text: str) -> bool:
    if failure.get("kind") != "speech_silence":
        return False
    try:
        _prompt, marker_count = inline_sentence_pause_prompt(text)
    except ValueError:
        return False
    durations = silence_durations(failure)
    if durations is None or marker_count < 1:
        return False
    leading, trailing, longest_internal = durations
    return bool(
        longest_internal > MAX_INTERNAL_SILENCE_SECONDS
        and leading <= MAX_LEADING_SILENCE_SECONDS
        and trailing <= MAX_TRAILING_SILENCE_SECONDS
    )

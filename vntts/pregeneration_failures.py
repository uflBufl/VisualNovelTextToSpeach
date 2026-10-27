"""Read-only failure explanations and on-demand diagnostic audio for Stories."""

from __future__ import annotations

import json
from dataclasses import dataclass

from vntts import generation_database
from vntts.authoring.failed_audio import failed_audio_path, read_failed_audio
from vntts.authoring.generation_failures import normalized_failure_record
from vntts.authoring.generation_state import (
    STATE_SCHEMA,
    STATE_VERSION,
    load_stable_generation_queue,
)
from vntts.authoring.speech_quality import (
    MAX_INTERNAL_SILENCE_SECONDS,
    MAX_LEADING_SILENCE_SECONDS,
    MAX_SILENCE_RATIO,
    MAX_TRAILING_SILENCE_SECONDS,
)
from vntts.numeric_values import optional_finite_number
from vntts.pregeneration_queue import PregenerationInput


@dataclass(frozen=True)
class PreparationFailure:
    queue_id: str
    line_id: str
    speaker: str
    text: str
    reason: str
    details: str
    audio_status: str
    audio: object


def inspect_preparation_failures(
    generation_input: PregenerationInput,
) -> tuple[PreparationFailure, ...]:
    path = generation_database.state_path(generation_input.output_directory)
    if not path.exists() and not path.is_symlink():
        return ()
    queue, digest = load_stable_generation_queue(generation_input.queue)
    if digest != generation_input.queue_sha256:
        raise ValueError("Preparation queue changed")
    by_id = {item.queue_id: item for item in queue.items}
    state = generation_database.read_state(path, allowed_queue_ids=frozenset(by_id))
    if (
        state.get("schema") != STATE_SCHEMA
        or type(state.get("schema_version")) is not int
        or state.get("schema_version") != STATE_VERSION
        or state.get("queue_sha256") != digest
    ):
        raise ValueError("Preparation state does not match the queue")
    items = state["items"]
    assert isinstance(items, dict)
    failures = []
    for item in queue.items:
        result = items.get(item.queue_id)
        if not isinstance(result, dict) or result.get("status") not in {
            "failed",
            "live_fallback",
        }:
            continue
        if result.get("status") == "live_fallback" and not (
            result.get("failure") or result.get("last_error")
        ):
            continue
        while True:
            for name in ("queue_id", "line_id", "text_sha256", "speaker", "text"):
                if name in result and result[name] != getattr(item, name):
                    raise ValueError(
                        f"Preparation failure {name} differs from the queue"
                    )
            failure = normalized_failure_record(result, text=item.text)
            if failure.get("kind") != "cancelled":
                break
            previous = failure.get("previous_failure")
            if not isinstance(previous, dict):
                result = {}
                break
            result = previous
        if not result:
            continue
        audio = result.get("failure_audio")
        audio_status = "unavailable"
        if isinstance(audio, dict) and audio.get("created") is False:
            audio_status = "not_created"
        else:
            try:
                failed_audio_path(
                    generation_input.output_directory, item.queue_id, audio
                )
            except OSError, ValueError:
                pass
            else:
                audio_status = "available"
        details = str(result.get("last_error") or "")
        render = failure.get("render")
        if isinstance(render, dict):
            details += ("\n" if details else "") + json.dumps(render, sort_keys=True)
        failures.append(
            PreparationFailure(
                item.queue_id,
                item.line_id,
                item.speaker,
                item.text,
                _failure_reason(failure),
                details,
                audio_status,
                audio,
            )
        )
    return tuple(failures)


def load_preparation_failure_audio(
    generation_input: PregenerationInput, failure: PreparationFailure
) -> bytes:
    current = next(
        (
            value
            for value in inspect_preparation_failures(generation_input)
            if value.queue_id == failure.queue_id
        ),
        None,
    )
    if current != failure or current.audio_status != "available":
        raise ValueError("Failed audio is no longer available for this failure")
    return read_failed_audio(
        generation_input.output_directory, failure.queue_id, failure.audio
    )


def _failure_reason(failure: dict[str, object]) -> str:
    kind = failure.get("kind")
    if kind == "speech_silence":
        quality = failure.get("speech_quality")
        exceeded = []
        if isinstance(quality, dict):
            for name, label, threshold in (
                (
                    "leading_silence_seconds",
                    "Leading silence",
                    MAX_LEADING_SILENCE_SECONDS,
                ),
                (
                    "trailing_silence_seconds",
                    "Trailing silence",
                    MAX_TRAILING_SILENCE_SECONDS,
                ),
                (
                    "longest_internal_silence_seconds",
                    "Internal pause",
                    MAX_INTERNAL_SILENCE_SECONDS,
                ),
                ("silence_ratio", "Silence", MAX_SILENCE_RATIO),
            ):
                value = optional_finite_number(quality.get(name))
                if value is not None and value > threshold:
                    measured, limit = (
                        (f"{value * 100:g}%", f"{threshold * 100:g}%")
                        if name == "silence_ratio"
                        else (f"{value:g} s", f"{threshold:g} s")
                    )
                    exceeded.append(f"{label}: {measured} (limit {limit})")
        return "; ".join(exceeded) or "Audio did not pass the pause check"
    return {
        "missed_eos_audio_limit": "Generation reached a limit before finishing the phrase",
        "reference_unavailable": "The voice reference was missing or unavailable",
        "interrupted": "Generation was interrupted before finishing the phrase",
    }.get(str(kind), "The speech engine could not generate this phrase")

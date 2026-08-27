"""Typed, provenance-safe plans for inline non-verbal audio events."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass

AUDIO_EVENT_PLAN_FIELD = "vntts.authoring.audio_event_plan"
AUDIO_EVENT_PLAN_SCHEMA = "vntts.authoring-audio-event-plan"
AUDIO_EVENT_PLAN_VERSION = 1
STORY_AUDIO_CUES_FIELD = "story_audio_cues"

_MISSING = object()
_CUE_STATUS_MAP = {
    "unchecked": "unknown",
    "installed": "available",
    "no_audio": "absent",
    "configured_unavailable": "unavailable",
    "unresolved": "unknown",
}
_CUE_INTEGER_FIELDS = ("parameter_code_1", "parameter_code_3", "parameter_code_6")
_CUE_NUMBER_FIELDS = (
    "localized_parameter_2",
    "scalar_parameter_4",
    "localized_parameter_5",
)

_STAGE_EVENT_PATTERN = re.compile(r"\*(?P<label>[^*\n]+)\*")
_TSK_PATTERN = re.compile(r"^\s*(?P<token>tsk)(?:[.!…]+)?\s*$", re.IGNORECASE)
_EVENT_DEFINITIONS = {
    "gasp": ("human-gasp", "sound-effect-model-candidate"),
    "gasps": ("human-gasp", "sound-effect-model-candidate"),
    "gurgle": ("human-gurgle", "sound-effect-model-candidate"),
    "gurgles": ("human-gurgle", "sound-effect-model-candidate"),
    "tsk": ("tongue-click", "tts-pronunciation-candidate"),
}


@dataclass(frozen=True)
class AudioEventPlan:
    """Canonical text split into speech and ordered non-verbal events."""

    canonical_text: str
    spoken_text: str
    events: tuple[dict, ...]

    @property
    def requires_composition(self):
        return bool(self.events)

    def to_document(self, *, story_audio_cues=_MISSING):
        body = {
            "schema": AUDIO_EVENT_PLAN_SCHEMA,
            "schema_version": AUDIO_EVENT_PLAN_VERSION,
            "canonical_text_sha256": _sha256(self.canonical_text),
            "spoken_text": self.spoken_text,
            "spoken_text_sha256": _sha256(self.spoken_text),
            "event_count": len(self.events),
            "events": [dict(value) for value in self.events],
            "requires_composition": self.requires_composition,
        }
        if story_audio_cues is not _MISSING:
            cues = validate_story_audio_cues(story_audio_cues)
            body["story_audio_cue_count"] = len(cues)
            body["story_audio_cues_sha256"] = _canonical_sha256(cues)
        return {**body, "plan_sha256": _canonical_sha256(body)}


def plan_inline_audio_events(text):
    """Return a deterministic plan without changing canonical story text."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Audio-event source text must be non-empty text")
    matches = list(_STAGE_EVENT_PATTERN.finditer(text))
    events = []
    for index, match in enumerate(matches, start=1):
        label = " ".join(match.group("label").split())
        normalized = label.casefold()
        kind, policy = _EVENT_DEFINITIONS.get(
            normalized, ("unsupported-stage-direction", "unsupported")
        )
        events.append(
            {
                "event_index": index,
                "source": match.group(0),
                "label": label,
                "kind": kind,
                "synthesis_policy": policy,
                "start": match.start(),
                "end": match.end(),
            }
        )
    spoken_text = _normalize_spoken_text(_STAGE_EVENT_PATTERN.sub(" ", text))
    if not events:
        tsk = _TSK_PATTERN.fullmatch(text)
        if tsk is not None:
            events.append(
                {
                    "event_index": 1,
                    "source": text.strip(),
                    "label": tsk.group("token"),
                    "kind": "tongue-click",
                    "synthesis_policy": "tts-pronunciation-candidate",
                    "start": len(text) - len(text.lstrip()),
                    "end": len(text.rstrip()),
                }
            )
            spoken_text = ""
    return AudioEventPlan(text, spoken_text, tuple(events))


def audio_event_plan_document(text, *, story_audio_cues=_MISSING):
    """Return an additive queue document only when composition is required."""
    if story_audio_cues is not _MISSING:
        story_audio_cues = validate_story_audio_cues(story_audio_cues)
    plan = plan_inline_audio_events(text)
    return (
        plan.to_document(story_audio_cues=story_audio_cues)
        if plan.requires_composition
        else None
    )


def audio_event_plan_for_record(value):
    """Plan from exact record text and optional producer-owned story cues."""
    document = value.document if hasattr(value, "document") else value
    if not isinstance(document, dict):
        raise ValueError("Audio-event source record must be an object")
    text = document.get("text")
    if STORY_AUDIO_CUES_FIELD in document:
        return audio_event_plan_document(
            text,
            story_audio_cues=document[STORY_AUDIO_CUES_FIELD],
        )
    return audio_event_plan_document(text)


def requires_audio_event_composition(value):
    """Recognize current plans and legacy queue text without trusting extensions."""
    document = value.document if hasattr(value, "document") else value
    if isinstance(document, dict):
        recorded = document.get(AUDIO_EVENT_PLAN_FIELD)
        expected = audio_event_plan_for_record(document)
        if recorded is not None and recorded != expected:
            raise ValueError("Recorded audio-event plan does not match canonical text")
        return expected is not None
    return audio_event_plan_document(value) is not None


def validate_story_audio_cues(value):
    """Validate extractor cue provenance without assigning event semantics."""
    if not isinstance(value, (list, tuple)):
        raise ValueError("story_audio_cues must be a list")
    cues = []
    for expected_index, source in enumerate(value, start=1):
        if not isinstance(source, dict):
            raise ValueError(f"story_audio_cues[{expected_index}] must be an object")
        cue = dict(source)
        if cue.get("cue_index") != expected_index:
            raise ValueError(
                "story_audio_cues must have consecutive source-order indices"
            )
        audio_id = cue.get("source_audio_id")
        if not isinstance(audio_id, str) or not audio_id.isdecimal():
            raise ValueError(
                f"story_audio_cues[{expected_index}] source_audio_id is invalid"
            )
        for field in _CUE_INTEGER_FIELDS:
            field_value = cue.get(field)
            if not isinstance(field_value, int) or isinstance(field_value, bool):
                raise ValueError(
                    f"story_audio_cues[{expected_index}] {field} is invalid"
                )
        for field in _CUE_NUMBER_FIELDS:
            field_value = cue.get(field)
            if (
                not isinstance(field_value, (int, float))
                or isinstance(field_value, bool)
                or not math.isfinite(field_value)
            ):
                raise ValueError(
                    f"story_audio_cues[{expected_index}] {field} is invalid"
                )
        status = cue.get("audio_status")
        if status not in _CUE_STATUS_MAP:
            raise ValueError(
                f"story_audio_cues[{expected_index}] audio_status is invalid"
            )
        if cue.get("source_audio_status") != _CUE_STATUS_MAP[status]:
            raise ValueError(
                f"story_audio_cues[{expected_index}] source_audio_status is inconsistent"
            )
        reason = cue.get("audio_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                f"story_audio_cues[{expected_index}] audio_reason is invalid"
            )
        for field in ("source_event", "source_bank"):
            field_value = cue.get(field)
            if field_value is not None and (
                not isinstance(field_value, str) or not field_value.strip()
            ):
                raise ValueError(
                    f"story_audio_cues[{expected_index}] {field} is invalid"
                )
        media_ids = _cue_media_ids(cue, expected_index, "source_media_ids")
        available_ids = _cue_media_ids(cue, expected_index, "available_media_ids")
        if not set(available_ids).issubset(media_ids):
            raise ValueError(
                f"story_audio_cues[{expected_index}] available media is not declared"
            )
        if status == "installed" and (
            not cue.get("source_event")
            or not cue.get("source_bank")
            or not media_ids
            or not available_ids
        ):
            raise ValueError(
                f"story_audio_cues[{expected_index}] installed provenance is incomplete"
            )
        cues.append(cue)
    return tuple(cues)


def _cue_media_ids(cue, cue_index, field):
    value = cue.get(field)
    if not isinstance(value, list) or any(
        not isinstance(media_id, int) or isinstance(media_id, bool)
        for media_id in value
    ):
        raise ValueError(f"story_audio_cues[{cue_index}] {field} is invalid")
    if len(set(value)) != len(value):
        raise ValueError(f"story_audio_cues[{cue_index}] {field} contains duplicates")
    return tuple(value)


def _normalize_spoken_text(text):
    value = " ".join(text.split())
    return re.sub(r"\s+([,.;:!?])", r"\1", value)


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "AUDIO_EVENT_PLAN_FIELD",
    "AUDIO_EVENT_PLAN_SCHEMA",
    "AUDIO_EVENT_PLAN_VERSION",
    "STORY_AUDIO_CUES_FIELD",
    "AudioEventPlan",
    "audio_event_plan_document",
    "audio_event_plan_for_record",
    "plan_inline_audio_events",
    "requires_audio_event_composition",
    "validate_story_audio_cues",
]

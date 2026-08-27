"""Typed, provenance-safe plans for inline non-verbal audio events."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

AUDIO_EVENT_PLAN_FIELD = "vntts.authoring.audio_event_plan"
AUDIO_EVENT_PLAN_SCHEMA = "vntts.authoring-audio-event-plan"
AUDIO_EVENT_PLAN_VERSION = 1

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

    def to_document(self):
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


def audio_event_plan_document(text):
    """Return an additive queue document only when composition is required."""
    plan = plan_inline_audio_events(text)
    return plan.to_document() if plan.requires_composition else None


def requires_audio_event_composition(value):
    """Recognize current plans and legacy queue text without trusting extensions."""
    document = value.document if hasattr(value, "document") else value
    if isinstance(document, dict):
        text = document.get("text")
        recorded = document.get(AUDIO_EVENT_PLAN_FIELD)
        expected = audio_event_plan_document(text)
        if recorded is not None and recorded != expected:
            raise ValueError("Recorded audio-event plan does not match canonical text")
        return expected is not None
    return audio_event_plan_document(value) is not None


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
    "AudioEventPlan",
    "audio_event_plan_document",
    "plan_inline_audio_events",
    "requires_audio_event_composition",
]

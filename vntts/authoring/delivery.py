"""Explicit, provenance-marked delivery annotation policy for authoring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Mapping

from vntts.document_identity import canonical_document_sha256

DELIVERY_ANNOTATION_VERSION = 1
LEGACY_ENGLISH_POLICY = "legacy-english-heuristic-v1"
PRESERVE_DELIVERY_POLICY = "preserve"

_WORD_PATTERN = re.compile(r"[A-Za-z']+")
_EMOTION_TERMS = {
    "joy": {
        "glad",
        "happy",
        "laugh",
        "wonderful",
        "great",
        "love",
        "delighted",
        "smile",
    },
    "sadness": {"sad", "sorry", "lost", "alone", "grief", "cry", "miss", "regret"},
    "anger": {"angry", "hate", "damn", "fool", "idiot", "revenge", "furious", "stop"},
    "fear": {
        "afraid",
        "fear",
        "scared",
        "danger",
        "run",
        "help",
        "terrified",
        "monster",
    },
    "surprise": {"what", "really", "impossible", "suddenly", "unexpected", "wait"},
    "contemplation": {
        "perhaps",
        "maybe",
        "wonder",
        "think",
        "remember",
        "understand",
        "why",
    },
}
_ANNOTATION_FIELDS = ("annotation_version", "emotion", "delivery", "prompt_adapters")
_CONTENT_FIELDS = ("emotion", "delivery", "prompt_adapters")


class DeliveryAnnotationError(RuntimeError):
    """A source record cannot be annotated without losing provenance."""


@dataclass(frozen=True)
class DeliveryPolicyApplication:
    record: dict[str, object]
    origin: Literal["none", "policy", "source_complete", "source_partial"]
    provenance: dict[str, object] | None = None

    @property
    def policy_generated(self):
        return self.origin == "policy"


def annotate_delivery(
    text,
    *,
    speaker="Narrator",
    previous_text=None,
    next_text=None,
    kind="dialogue",
):
    """Return the exact deterministic extractor v1 English heuristic output."""
    text = _required_exact_text(text, "text")
    speaker = _required_exact_text(speaker, "speaker")
    previous_text = _optional_text(previous_text, "previous_text")
    next_text = _optional_text(next_text, "next_text")
    kind = _required_exact_text(kind, "kind")

    lowered = text.casefold()
    words = set(_WORD_PATTERN.findall(lowered))
    scores = {emotion: len(words & terms) for emotion, terms in _EMOTION_TERMS.items()}
    cues = []
    if "!" in text:
        scores["surprise"] += 1
        cues.append("exclamation")
    if "..." in text or "…" in text:
        scores["contemplation"] += 1
        cues.append("ellipsis")
    if text.count("?") >= 2:
        scores["surprise"] += 1
        cues.append("repeated_question")
    letters = [character for character in text if character.isalpha()]
    if (
        len(letters) >= 8
        and sum(character.isupper() for character in letters) / len(letters) > 0.7
    ):
        scores["anger"] += 2
        cues.append("uppercase_emphasis")
    if any(token in lowered for token in ("*sob", "*cry", "tears")):
        scores["sadness"] += 2
        cues.append("sad_stage_direction")
    if any(token in lowered for token in ("*laugh", "haha", "hehe")):
        scores["joy"] += 2
        cues.append("laugh_stage_direction")

    primary, score = max(scores.items(), key=lambda item: (item[1], item[0]))
    if score == 0:
        primary = "contemplation" if kind == "narration" else "neutral"
    confidence = min(0.95, 0.45 + (score * 0.15)) if score else 0.35
    pace = (
        "slow"
        if primary in {"sadness", "contemplation"} or "ellipsis" in cues
        else "medium"
    )
    if primary in {"anger", "fear", "surprise"} and "ellipsis" not in cues:
        pace = "fast"
    energy = "high" if primary in {"anger", "fear", "surprise", "joy"} else "low"
    if primary == "neutral":
        energy = "medium"
    volume = (
        "loud" if "uppercase_emphasis" in cues or text.count("!") >= 2 else "normal"
    )
    tone = {
        "joy": "warm and buoyant",
        "sadness": "soft and restrained",
        "anger": "tense and forceful",
        "fear": "uneasy and urgent",
        "surprise": "alert and reactive",
        "contemplation": "reflective and measured",
        "neutral": "natural and conversational",
    }[primary]
    context = " ".join(
        value for value in (previous_text, next_text) if value is not None
    )
    context_words = set(_WORD_PATTERN.findall(context.casefold()))
    context_emotions = [
        emotion for emotion, terms in _EMOTION_TERMS.items() if context_words & terms
    ]
    if context_emotions:
        cues.append(f"context:{context_emotions[0]}")

    generic_prompt = (
        f"Perform as {speaker}. Emotion: {primary}. Tone: {tone}. "
        f"Pace: {pace}. Energy: {energy}. Volume: {volume}."
    )
    exaggeration = 0.7 if energy == "high" else 0.45 if energy == "medium" else 0.3
    return {
        "annotation_version": DELIVERY_ANNOTATION_VERSION,
        "emotion": {
            "primary": primary,
            "confidence": round(confidence, 2),
            "cues": cues,
        },
        "delivery": {
            "pace": pace,
            "energy": energy,
            "volume": volume,
            "tone": tone,
        },
        "prompt_adapters": {
            "generic": generic_prompt,
            "chatterbox": {
                "prompt": generic_prompt,
                "exaggeration": exaggeration,
                "cfg_weight": 0.45 if primary in {"anger", "fear"} else 0.5,
            },
            "cosyvoice": {"instruct": generic_prompt},
            "fish_speech": {"text_prompt": generic_prompt},
        },
    }


def apply_delivery_policy(
    record: Mapping[str, object], policy=PRESERVE_DELIVERY_POLICY
):
    """Return a copy with source annotations preserved or explicit policy output."""
    if not isinstance(record, Mapping):
        raise DeliveryAnnotationError("record must be a mapping")
    policy = PRESERVE_DELIVERY_POLICY if policy is None else str(policy).strip()
    if policy not in {PRESERVE_DELIVERY_POLICY, LEGACY_ENGLISH_POLICY}:
        raise DeliveryAnnotationError(f"Unsupported delivery policy: {policy!r}")

    result = dict(record)
    present = {field for field in _ANNOTATION_FIELDS if field in result}
    complete = all(
        field in result and isinstance(result[field], dict) for field in _CONTENT_FIELDS
    ) and (
        isinstance(result.get("annotation_version"), int)
        and not isinstance(result.get("annotation_version"), bool)
        and result["annotation_version"] == DELIVERY_ANNOTATION_VERSION
    )
    if present:
        return DeliveryPolicyApplication(
            result, "source_complete" if complete else "source_partial"
        )
    if policy == PRESERVE_DELIVERY_POLICY:
        return DeliveryPolicyApplication(result, "none")

    inputs = {
        "text": _required_exact_text(result.get("text"), "record text"),
        "speaker": _required_exact_text(result.get("speaker"), "record speaker"),
        "previous_text": _optional_text(
            result.get("previous_text"), "record previous_text"
        ),
        "next_text": _optional_text(result.get("next_text"), "record next_text"),
        "kind": _required_exact_text(result.get("kind") or "dialogue", "record kind"),
    }
    result.update(
        annotate_delivery(
            inputs["text"],
            speaker=inputs["speaker"],
            previous_text=inputs["previous_text"],
            next_text=inputs["next_text"],
            kind=inputs["kind"],
        )
    )
    provenance = {
        "origin": "policy",
        "policy": LEGACY_ENGLISH_POLICY,
        "policy_version": DELIVERY_ANNOTATION_VERSION,
        "input_sha256": canonical_document_sha256(inputs),
        "generated_fields": list(_ANNOTATION_FIELDS),
    }
    return DeliveryPolicyApplication(result, "policy", provenance)


def _required_exact_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise DeliveryAnnotationError(f"{label} must be non-empty text")
    return value


def _optional_text(value, label):
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeliveryAnnotationError(f"{label} must be text or null")
    return value

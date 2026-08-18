"""Validate exact queue-to-voice bindings carried by a voice manifest."""

from __future__ import annotations

import hashlib
import json

from vntts_artifacts.voice_manifest import normalize_character_name

SOURCE_REFERENCE_BINDINGS_FIELD = "vntts.authoring.source_reference_bindings"
SOURCE_REFERENCE_BINDINGS_SCHEMA = "vntts.authoring-source-reference-bindings"
SOURCE_REFERENCE_BINDINGS_VERSION = 1


class SourceReferenceBindingError(ValueError):
    """A source-reference voice binding is malformed or inconsistent."""


def queue_voice_overrides_from_manifest(document, *, queue_ids=None, voices=()):
    """Return validated exact queue overrides from a voice-manifest document."""
    value = document.get(SOURCE_REFERENCE_BINDINGS_FIELD)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourceReferenceBindingError("Source-reference bindings must be an object")
    if (
        value.get("schema") != SOURCE_REFERENCE_BINDINGS_SCHEMA
        or value.get("schema_version") != SOURCE_REFERENCE_BINDINGS_VERSION
    ):
        raise SourceReferenceBindingError("Unsupported source-reference binding schema")
    plan_sha256 = value.get("source_reference_plan_sha256")
    if not _is_sha256(plan_sha256):
        raise SourceReferenceBindingError(
            "Source-reference bindings require the exact plan SHA-256"
        )
    quality_review_sha256 = value.get("source_reference_quality_review_sha256")
    if quality_review_sha256 is not None and not _is_sha256(quality_review_sha256):
        raise SourceReferenceBindingError(
            "Source-reference bindings quality-review SHA-256 is invalid"
        )
    variants = value.get("selected_variants")
    if not isinstance(variants, list) or not variants:
        raise SourceReferenceBindingError(
            "Source-reference bindings require selected variants"
        )
    seen_variants = set()
    selected_voices = set()
    for index, variant in enumerate(variants):
        if not isinstance(variant, dict):
            raise SourceReferenceBindingError(
                f"Source-reference variant {index} must be an object"
            )
        variant_id = _text(variant.get("variant_id"), "Source-reference variant ID")
        character = _text(
            variant.get("voice_character"), "Source-reference variant voice"
        )
        if variant_id in seen_variants:
            raise SourceReferenceBindingError(
                f"Duplicate source-reference variant: {variant_id}"
            )
        seen_variants.add(variant_id)
        normalized_character = normalize_character_name(character)
        if not normalized_character:
            raise SourceReferenceBindingError(
                f"Source-reference variant has invalid voice: {variant_id}"
            )
        if normalized_character in selected_voices:
            raise SourceReferenceBindingError(
                f"Duplicate source-reference variant voice: {character}"
            )
        selected_voices.add(normalized_character)
    overrides = value.get("queue_voice_overrides")
    if not isinstance(overrides, dict) or not overrides:
        raise SourceReferenceBindingError(
            "Source-reference queue voice overrides must be a non-empty object"
        )
    known_queue_ids = None if queue_ids is None else set(queue_ids)
    known_voices = {
        normalize_character_name(voice.character): voice.character for voice in voices
    }
    parsed = {}
    for queue_id, character in overrides.items():
        queue_id = _text(queue_id, "Source-reference queue ID")
        character = _text(character, f"Source-reference queue {queue_id!r} voice")
        if queue_id in parsed:
            raise SourceReferenceBindingError(
                f"Duplicate source-reference queue ID: {queue_id}"
            )
        if known_queue_ids is not None and queue_id not in known_queue_ids:
            raise SourceReferenceBindingError(
                f"Source-reference queue ID is absent from the queue: {queue_id}"
            )
        if known_voices and normalize_character_name(character) not in known_voices:
            raise SourceReferenceBindingError(
                f"Source-reference voice is absent from the manifest: {character}"
            )
        if normalize_character_name(character) not in selected_voices:
            raise SourceReferenceBindingError(
                "Source-reference queue mapping targets a voice that was not "
                f"explicitly selected: {character}"
            )
        parsed[queue_id] = character
    mapped_voices = {
        normalize_character_name(character) for character in parsed.values()
    }
    unused_voices = selected_voices - mapped_voices
    if unused_voices:
        raise SourceReferenceBindingError(
            "Selected source-reference variants must each bind at least one queue item"
        )
    declared_sha256 = value.get("queue_voice_overrides_sha256")
    calculated_sha256 = queue_voice_overrides_sha256(parsed)
    if declared_sha256 != calculated_sha256:
        raise SourceReferenceBindingError(
            "Source-reference queue voice override checksum is inconsistent"
        )
    return parsed


def queue_voice_overrides_sha256(overrides):
    rendered = json.dumps(
        dict(sorted(overrides.items())),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise SourceReferenceBindingError(f"{label} must be non-empty text")
    return value.strip()


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "SOURCE_REFERENCE_BINDINGS_FIELD",
    "SOURCE_REFERENCE_BINDINGS_SCHEMA",
    "SOURCE_REFERENCE_BINDINGS_VERSION",
    "SourceReferenceBindingError",
    "queue_voice_overrides_from_manifest",
    "queue_voice_overrides_sha256",
]

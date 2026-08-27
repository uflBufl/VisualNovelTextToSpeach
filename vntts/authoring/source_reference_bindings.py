"""Validate exact queue-to-voice bindings carried by a voice manifest."""

from __future__ import annotations

import hashlib
import json

from vntts_artifacts.voice_manifest import normalize_character_name

SOURCE_REFERENCE_BINDINGS_FIELD = "vntts.authoring.source_reference_bindings"
SOURCE_REFERENCE_BINDINGS_SCHEMA = "vntts.authoring-source-reference-bindings"
SOURCE_REFERENCE_BINDINGS_VERSION = 1
SOURCE_REFERENCE_BINDINGS_MULTI_VERSION = 2
SOURCE_REFERENCE_BINDINGS_RETIREMENT_VERSION = 3
SUPPORTED_SOURCE_REFERENCE_BINDINGS_VERSIONS = frozenset(
    {
        SOURCE_REFERENCE_BINDINGS_VERSION,
        SOURCE_REFERENCE_BINDINGS_MULTI_VERSION,
        SOURCE_REFERENCE_BINDINGS_RETIREMENT_VERSION,
    }
)
SOURCE_REFERENCE_RETIREMENT_REASONS = frozenset({"real_story_quality_failure"})


class SourceReferenceBindingError(ValueError):
    """A source-reference voice binding is malformed or inconsistent."""


def queue_voice_overrides_from_manifest(document, *, queue_ids=None, voices=()):
    """Return validated exact queue overrides from a voice-manifest document."""
    value = document.get(SOURCE_REFERENCE_BINDINGS_FIELD)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourceReferenceBindingError("Source-reference bindings must be an object")
    version = value.get("schema_version")
    if (
        value.get("schema") != SOURCE_REFERENCE_BINDINGS_SCHEMA
        or version not in SUPPORTED_SOURCE_REFERENCE_BINDINGS_VERSIONS
    ):
        raise SourceReferenceBindingError("Unsupported source-reference binding schema")
    if version == SOURCE_REFERENCE_BINDINGS_VERSION:
        plan_sha256s = {
            _required_sha256(
                value.get("source_reference_plan_sha256"),
                "Source-reference bindings plan SHA-256",
            )
        }
        quality_review_sha256 = value.get("source_reference_quality_review_sha256")
        if quality_review_sha256 is not None and not _is_sha256(quality_review_sha256):
            raise SourceReferenceBindingError(
                "Source-reference bindings quality-review SHA-256 is invalid"
            )
    else:
        _required_sha256(
            value.get("predecessor_manifest_sha256"),
            "Multi-plan source-reference predecessor manifest SHA-256",
        )
        sources = value.get("sources")
        if not isinstance(sources, list) or not sources:
            raise SourceReferenceBindingError(
                "Multi-plan source-reference bindings require source ledgers"
            )
        plan_sha256s = set()
        for source in sources:
            if not isinstance(source, dict) or set(source) != {
                "source_reference_plan_sha256",
                "source_reference_quality_review_sha256",
            }:
                raise SourceReferenceBindingError(
                    "Source-reference binding source ledger is invalid"
                )
            plan_sha256 = _required_sha256(
                source.get("source_reference_plan_sha256"),
                "Source-reference binding source plan SHA-256",
            )
            _required_sha256(
                source.get("source_reference_quality_review_sha256"),
                "Source-reference binding source quality-review SHA-256",
            )
            if plan_sha256 in plan_sha256s:
                raise SourceReferenceBindingError(
                    "Source-reference binding source plans must be distinct"
                )
            plan_sha256s.add(plan_sha256)
        retired_variants = value.get("retired_variants")
        if version == SOURCE_REFERENCE_BINDINGS_RETIREMENT_VERSION:
            _validate_retired_variants(retired_variants, plan_sha256s)
        elif retired_variants is not None:
            raise SourceReferenceBindingError(
                "Retired variants require source-reference binding schema version 3"
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
        if version in {
            SOURCE_REFERENCE_BINDINGS_MULTI_VERSION,
            SOURCE_REFERENCE_BINDINGS_RETIREMENT_VERSION,
        }:
            variant_plan_sha256 = _required_sha256(
                variant.get("source_reference_plan_sha256"),
                "Source-reference variant plan SHA-256",
            )
            if variant_plan_sha256 not in plan_sha256s:
                raise SourceReferenceBindingError(
                    "Source-reference variant references an unknown source plan"
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
    if version == SOURCE_REFERENCE_BINDINGS_RETIREMENT_VERSION:
        retired = value["retired_variants"]
        retired_voices = {
            normalize_character_name(record["voice_character"]) for record in retired
        }
        if known_voices and not retired_voices.issubset(known_voices):
            raise SourceReferenceBindingError(
                "Retired source-reference voice is absent from the manifest"
            )
        if retired_voices & selected_voices:
            raise SourceReferenceBindingError(
                "A source-reference voice cannot be selected and retired"
            )
        retired_queue_ids = {
            queue_id for record in retired for queue_id in record["queue_ids"]
        }
        if retired_queue_ids & set(parsed):
            raise SourceReferenceBindingError(
                "Retired source-reference queue IDs must not remain active"
            )
    declared_sha256 = value.get("queue_voice_overrides_sha256")
    calculated_sha256 = queue_voice_overrides_sha256(parsed)
    if declared_sha256 != calculated_sha256:
        raise SourceReferenceBindingError(
            "Source-reference queue voice override checksum is inconsistent"
        )
    return parsed


def retired_source_reference_variants_from_manifest(document):
    """Return validated inactive variant records from a version-3 manifest."""
    value = document.get(SOURCE_REFERENCE_BINDINGS_FIELD)
    if value is None:
        return ()
    version = value.get("schema_version") if isinstance(value, dict) else None
    if version != SOURCE_REFERENCE_BINDINGS_RETIREMENT_VERSION:
        return ()
    sources = value.get("sources")
    plan_sha256s = {
        source.get("source_reference_plan_sha256")
        for source in sources
        if isinstance(source, dict)
    }
    retired = value.get("retired_variants")
    _validate_retired_variants(retired, plan_sha256s)
    return tuple(dict(record) for record in retired)


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


def _required_sha256(value, label):
    if not _is_sha256(value):
        raise SourceReferenceBindingError(f"{label} is invalid")
    return value


def _validate_retired_variants(records, plan_sha256s):
    if not isinstance(records, list) or not records:
        raise SourceReferenceBindingError(
            "Retired source-reference bindings require retired variants"
        )
    observed_variants = []
    observed_queue_ids = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "variant_id",
            "source_reference_plan_sha256",
            "voice_character",
            "reference_sha256",
            "queue_ids",
            "reason",
        }:
            raise SourceReferenceBindingError(
                "Retired source-reference variant record is malformed"
            )
        variant_id = _text(record.get("variant_id"), "Retired variant ID")
        plan_sha256 = _required_sha256(
            record.get("source_reference_plan_sha256"),
            "Retired variant source plan SHA-256",
        )
        if plan_sha256 not in plan_sha256s:
            raise SourceReferenceBindingError(
                "Retired variant references an unknown source plan"
            )
        _text(record.get("voice_character"), "Retired variant voice")
        _required_sha256(
            record.get("reference_sha256"),
            "Retired variant reference SHA-256",
        )
        reason = record.get("reason")
        if reason not in SOURCE_REFERENCE_RETIREMENT_REASONS:
            raise SourceReferenceBindingError("Retired variant reason is unsupported")
        queue_ids = record.get("queue_ids")
        if (
            not isinstance(queue_ids, list)
            or not queue_ids
            or queue_ids != sorted(set(queue_ids))
        ):
            raise SourceReferenceBindingError(
                "Retired variant queue IDs are not canonical"
            )
        for queue_id in queue_ids:
            _text(queue_id, "Retired variant queue ID")
            if queue_id in observed_queue_ids:
                raise SourceReferenceBindingError(
                    "Retired source-reference variants overlap queue IDs"
                )
            observed_queue_ids.add(queue_id)
        observed_variants.append(variant_id)
    if observed_variants != sorted(set(observed_variants)):
        raise SourceReferenceBindingError(
            "Retired source-reference variants are not canonical"
        )


__all__ = [
    "SOURCE_REFERENCE_BINDINGS_FIELD",
    "SOURCE_REFERENCE_BINDINGS_SCHEMA",
    "SOURCE_REFERENCE_BINDINGS_VERSION",
    "SOURCE_REFERENCE_BINDINGS_MULTI_VERSION",
    "SOURCE_REFERENCE_BINDINGS_RETIREMENT_VERSION",
    "SOURCE_REFERENCE_RETIREMENT_REASONS",
    "SUPPORTED_SOURCE_REFERENCE_BINDINGS_VERSIONS",
    "SourceReferenceBindingError",
    "queue_voice_overrides_from_manifest",
    "queue_voice_overrides_sha256",
    "retired_source_reference_variants_from_manifest",
]

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
MISSING_VOICE_REUSE_BINDING_FIELD = "vntts.authoring.missing_voice_reuse"
MISSING_VOICE_REUSE_BINDING_SCHEMA = "vntts.authoring-missing-voice-reuse-binding"
MISSING_VOICE_REUSE_BINDING_VERSION = 1
MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION = 2
SUPPORTED_MISSING_VOICE_REUSE_BINDING_VERSIONS = frozenset(
    {
        MISSING_VOICE_REUSE_BINDING_VERSION,
        MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION,
    }
)
KNOWN_ROLE_REUSE_BINDING_FIELD = "vntts.authoring.known_role_reuse"
KNOWN_ROLE_REUSE_BINDING_SCHEMA = "vntts.authoring-known-role-reuse-binding"
KNOWN_ROLE_REUSE_BINDING_VERSION = 1
KNOWN_ROLE_REUSE_AUTHORITY = (
    "Explicit exact-role reuse binding. Existing approved audio remains authoritative; "
    "absent and rejected targets use the selected existing character voice."
)


class SourceReferenceBindingError(ValueError):
    """A source-reference voice binding is malformed or inconsistent."""


def queue_voice_overrides_from_manifest(document, *, queue_ids=None, voices=()):
    """Return all validated exact queue overrides from a voice manifest."""
    # Callers commonly pass a generator over the queue. Both independent
    # binding layers must validate against the same complete identity set.
    queue_ids = None if queue_ids is None else tuple(queue_ids)
    source_overrides = _source_reference_overrides_from_manifest(
        document, queue_ids=queue_ids, voices=voices
    )
    reuse_overrides = _missing_voice_reuse_overrides_from_manifest(
        document, queue_ids=queue_ids, voices=voices
    )
    known_role_overrides = _known_role_reuse_overrides_from_manifest(
        document, queue_ids=queue_ids, voices=voices
    )
    overlap = set(source_overrides).intersection(reuse_overrides)
    if overlap:
        reuse = document.get(MISSING_VOICE_REUSE_BINDING_FIELD)
        controls = reuse.get("source_failed_state_item_sha256s", {})
        if (
            reuse.get("target_mode") != "failed"
            or not isinstance(controls, dict)
            or not set(reuse_overrides).issubset(controls)
            or any(not _is_sha256(value) for value in controls.values())
        ):
            raise SourceReferenceBindingError(
                "Source-reference and missing-voice reuse bindings overlap queue IDs: "
                + ", ".join(sorted(overlap))
            )
    overlap = set(source_overrides).intersection(known_role_overrides)
    if overlap:
        authority = document.get(KNOWN_ROLE_REUSE_BINDING_FIELD)
        controls = authority.get("source_rejected_state_item_sha256s", {})
        if (
            not isinstance(controls, dict)
            or not overlap.issubset(controls)
            or any(not _is_sha256(controls[queue_id]) for queue_id in overlap)
        ):
            raise SourceReferenceBindingError(
                "Source-reference and known-role reuse bindings overlap queue IDs: "
                + ", ".join(sorted(overlap))
            )
    overlap = set(reuse_overrides).intersection(known_role_overrides)
    if overlap:
        raise SourceReferenceBindingError(
            "Missing-voice and known-role reuse bindings overlap queue IDs: "
            + ", ".join(sorted(overlap))
        )
    return {**source_overrides, **reuse_overrides, **known_role_overrides}


def _known_role_reuse_overrides_from_manifest(document, *, queue_ids=None, voices=()):
    value = document.get(KNOWN_ROLE_REUSE_BINDING_FIELD)
    if value is None:
        return {}
    fields = {
        "schema",
        "schema_version",
        "mode",
        "source_voice_manifest_sha256",
        "source_workspace_id",
        "source_workspace_sha256",
        "source_state_sha256",
        "queue_sha256",
        "source_character",
        "reuse_voice_character",
        "reuse_reference_sha256s",
        "unresolved_authority",
        "retired_variants",
        "targets",
        "preserved_approved_queue_ids",
        "source_rejected_state_item_sha256s",
        "queue_voice_overrides",
        "queue_voice_overrides_sha256",
        "authority",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema") != KNOWN_ROLE_REUSE_BINDING_SCHEMA
        or value.get("schema_version") != KNOWN_ROLE_REUSE_BINDING_VERSION
        or value.get("mode") != "explicit_role_reuse"
        or value.get("authority") != KNOWN_ROLE_REUSE_AUTHORITY
    ):
        raise SourceReferenceBindingError("Known-role reuse binding is malformed")
    for field in (
        "source_voice_manifest_sha256",
        "source_workspace_sha256",
        "source_state_sha256",
        "queue_sha256",
    ):
        _required_sha256(value.get(field), f"Known-role reuse {field}")
    _text(value.get("source_workspace_id"), "Known-role reuse source workspace ID")
    source_character = _text(
        value.get("source_character"), "Known-role reuse source character"
    )
    reuse_character = _text(
        value.get("reuse_voice_character"), "Known-role reuse selected voice"
    )
    if normalize_character_name(source_character) == normalize_character_name(
        reuse_character
    ):
        raise SourceReferenceBindingError(
            "Known-role reuse source and selected voice must differ"
        )
    references = value.get("reuse_reference_sha256s")
    if (
        not isinstance(references, list)
        or not references
        or references != sorted(set(references))
    ):
        raise SourceReferenceBindingError(
            "Known-role reuse reference hashes are not canonical"
        )
    for digest in references:
        _required_sha256(digest, "Known-role reuse reference SHA-256")

    unresolved = value.get("unresolved_authority")
    unresolved_fields = {
        "bundle_id",
        "bundle_sha256",
        "decision_id",
        "decision_sha256",
        "plan_id",
        "cohort_ids",
        "queue_ids",
    }
    if not isinstance(unresolved, dict) or set(unresolved) != unresolved_fields:
        raise SourceReferenceBindingError(
            "Known-role unresolved authority is malformed"
        )
    for field in (
        "bundle_id",
        "bundle_sha256",
        "decision_id",
        "decision_sha256",
        "plan_id",
    ):
        _required_sha256(unresolved.get(field), f"Known-role unresolved {field}")
    cohort_ids = unresolved.get("cohort_ids")
    unresolved_ids = unresolved.get("queue_ids")
    if (
        not isinstance(cohort_ids, list)
        or not cohort_ids
        or cohort_ids != sorted(set(cohort_ids))
        or any(not _is_sha256(value) for value in cohort_ids)
        or not isinstance(unresolved_ids, list)
        or not unresolved_ids
        or unresolved_ids != sorted(set(unresolved_ids))
    ):
        raise SourceReferenceBindingError(
            "Known-role unresolved scope is not canonical"
        )

    rejected = value.get("source_rejected_state_item_sha256s")
    if (
        not isinstance(rejected, dict)
        or list(rejected) != sorted(rejected)
        or any(not _is_sha256(digest) for digest in rejected.values())
    ):
        raise SourceReferenceBindingError(
            "Known-role rejected-item authority is malformed"
        )
    targets = value.get("targets")
    if not isinstance(targets, list) or not targets:
        raise SourceReferenceBindingError("Known-role reuse targets are empty")
    target_ids = []
    absent_ids = []
    rejected_ids = []
    for target in targets:
        target_fields = {
            "queue_id",
            "line_id",
            "text_sha256",
            "speaker",
            "declared_voice_character",
            "source_state",
            "source_state_item_sha256",
        }
        if not isinstance(target, dict) or set(target) != target_fields:
            raise SourceReferenceBindingError("Known-role reuse target is malformed")
        queue_id = _text(target.get("queue_id"), "Known-role reuse queue ID")
        _text(target.get("line_id"), "Known-role reuse line ID")
        _required_sha256(target.get("text_sha256"), "Known-role reuse text SHA-256")
        _text(target.get("speaker"), "Known-role reuse speaker")
        declared = _text(
            target.get("declared_voice_character"),
            "Known-role reuse declared voice",
        )
        if normalize_character_name(declared) != normalize_character_name(
            source_character
        ):
            raise SourceReferenceBindingError(
                "Known-role reuse target has a different declared voice"
            )
        source_state = target.get("source_state")
        state_digest = target.get("source_state_item_sha256")
        if source_state == "absent":
            if state_digest is not None:
                raise SourceReferenceBindingError(
                    "Absent known-role target has a state-item hash"
                )
            absent_ids.append(queue_id)
        elif source_state == "rejected":
            if rejected.get(queue_id) != state_digest or not _is_sha256(state_digest):
                raise SourceReferenceBindingError(
                    "Rejected known-role target lacks exact state authority"
                )
            rejected_ids.append(queue_id)
        else:
            raise SourceReferenceBindingError(
                "Known-role reuse target source state is unsupported"
            )
        target_ids.append(queue_id)
    if (
        target_ids != sorted(set(target_ids))
        or absent_ids != unresolved_ids
        or rejected_ids != sorted(rejected)
    ):
        raise SourceReferenceBindingError(
            "Known-role target inventory disagrees with its authorities"
        )

    retired = value.get("retired_variants")
    if not isinstance(retired, list):
        raise SourceReferenceBindingError("Known-role retired variants must be a list")
    retired_ids = []
    retired_queue_ids = set()
    for record in retired:
        if not isinstance(record, dict) or set(record) != {
            "variant_id",
            "record_sha256",
            "queue_ids",
        }:
            raise SourceReferenceBindingError(
                "Known-role retired-variant authority is malformed"
            )
        retired_ids.append(_text(record.get("variant_id"), "Retired variant ID"))
        _required_sha256(record.get("record_sha256"), "Retired variant SHA-256")
        queue_scope = record.get("queue_ids")
        if (
            not isinstance(queue_scope, list)
            or not queue_scope
            or queue_scope != sorted(set(queue_scope))
        ):
            raise SourceReferenceBindingError(
                "Known-role retired-variant queue scope is malformed"
            )
        retired_queue_ids.update(queue_scope)
    if retired_ids != sorted(set(retired_ids)) or not retired_queue_ids.issubset(
        rejected
    ):
        raise SourceReferenceBindingError(
            "Known-role retired variants are not bound to rejected targets"
        )

    approved = value.get("preserved_approved_queue_ids")
    if (
        not isinstance(approved, list)
        or approved != sorted(set(approved))
        or set(approved).intersection(target_ids)
    ):
        raise SourceReferenceBindingError(
            "Known-role preserved-approved scope is malformed"
        )
    overrides = value.get("queue_voice_overrides")
    if (
        not isinstance(overrides, dict)
        or list(overrides) != target_ids
        or any(
            normalize_character_name(character)
            != normalize_character_name(reuse_character)
            for character in overrides.values()
        )
        or value.get("queue_voice_overrides_sha256")
        != queue_voice_overrides_sha256(overrides)
    ):
        raise SourceReferenceBindingError("Known-role reuse overrides are inconsistent")
    known_queue_ids = None if queue_ids is None else set(queue_ids)
    if known_queue_ids is not None and not set(target_ids).issubset(known_queue_ids):
        raise SourceReferenceBindingError(
            "Known-role reuse queue ID is absent from the queue"
        )
    known_voices = {
        normalize_character_name(voice.character): voice.character for voice in voices
    }
    if known_voices and normalize_character_name(reuse_character) not in known_voices:
        raise SourceReferenceBindingError(
            "Known-role selected voice is absent from the manifest"
        )
    return dict(overrides)


def _source_reference_overrides_from_manifest(document, *, queue_ids=None, voices=()):
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


def _missing_voice_reuse_overrides_from_manifest(
    document, *, queue_ids=None, voices=()
):
    value = document.get(MISSING_VOICE_REUSE_BINDING_FIELD)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourceReferenceBindingError(
            "Missing-voice reuse binding must be an object"
        )
    version = value.get("schema_version")
    mode = value.get("mode")
    if (
        value.get("schema") != MISSING_VOICE_REUSE_BINDING_SCHEMA
        or version not in SUPPORTED_MISSING_VOICE_REUSE_BINDING_VERSIONS
        or (version, mode)
        not in {
            (MISSING_VOICE_REUSE_BINDING_VERSION, "comparison_sample_only"),
            (MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION, "approved_cohort_reuse"),
        }
    ):
        raise SourceReferenceBindingError(
            "Unsupported missing-voice reuse binding schema"
        )
    _required_sha256(value.get("plan_id"), "Missing-voice reuse plan ID")
    _required_sha256(
        value.get("source_voice_manifest_sha256"),
        "Missing-voice reuse source manifest SHA-256",
    )
    _text(
        value.get("source_workspace_id"),
        "Missing-voice reuse source workspace ID",
    )
    _required_sha256(
        value.get("source_workspace_sha256"),
        "Missing-voice reuse source workspace SHA-256",
    )
    if mode == "comparison_sample_only":
        _required_sha256(value.get("candidate_id"), "Missing-voice reuse candidate ID")
        candidate = _text(
            value.get("candidate_voice_character"),
            "Missing-voice reuse candidate voice",
        )
        references = value.get("candidate_reference_sha256s")
        if not isinstance(references, list) or not references:
            raise SourceReferenceBindingError(
                "Missing-voice reuse candidate references are empty"
            )
        for reference in references:
            _required_sha256(
                reference, "Missing-voice reuse candidate reference SHA-256"
            )
        candidates = {normalize_character_name(candidate): candidate}
    else:
        candidates = _validate_approved_reuse_authority(value)
    cohort_ids = value.get("cohort_ids")
    if (
        not isinstance(cohort_ids, list)
        or not cohort_ids
        or cohort_ids != sorted(set(cohort_ids))
        or any(not _is_sha256(cohort_id) for cohort_id in cohort_ids)
    ):
        raise SourceReferenceBindingError(
            "Missing-voice reuse cohort IDs are not canonical"
        )
    known_queue_ids = None if queue_ids is None else set(queue_ids)
    known_voices = {
        normalize_character_name(voice.character): voice.character for voice in voices
    }
    if known_voices and not set(candidates).issubset(known_voices):
        raise SourceReferenceBindingError(
            "A selected missing-voice reuse candidate is absent from the manifest"
        )
    overrides = value.get("queue_voice_overrides")
    if not isinstance(overrides, dict) or (
        mode == "comparison_sample_only" and not overrides
    ):
        raise SourceReferenceBindingError(
            "Missing-voice reuse overrides must be a non-empty object"
        )
    parsed = {}
    for queue_id, character in overrides.items():
        queue_id = _text(queue_id, "Missing-voice reuse queue ID")
        character = _text(character, f"Missing-voice reuse queue {queue_id!r} voice")
        if known_queue_ids is not None and queue_id not in known_queue_ids:
            raise SourceReferenceBindingError(
                f"Missing-voice reuse queue ID is absent from the queue: {queue_id}"
            )
        if normalize_character_name(character) not in candidates:
            raise SourceReferenceBindingError(
                "Missing-voice reuse override targets an unselected candidate voice"
            )
        parsed[queue_id] = character
    if value.get("queue_voice_overrides_sha256") != queue_voice_overrides_sha256(
        parsed
    ):
        raise SourceReferenceBindingError(
            "Missing-voice reuse override checksum is inconsistent"
        )
    authority = value.get("authority")
    expected_authorities = (
        {
            "Comparison-only exact sample bindings. This authority does not bind the "
            "remaining cohort or approve generated audio."
        }
        if mode == "comparison_sample_only"
        else {
            "Human-reviewed exact cohort reuse binding. Neither decisions bind no voice.",
            "Exact cohort reuse binding. Candidate choices require human review; "
            "cohorts with no selectable candidate are deterministically unresolved. "
            "Neither decisions bind no voice.",
        }
    )
    if authority not in expected_authorities:
        raise SourceReferenceBindingError(
            "Missing-voice reuse authority statement is invalid"
        )
    return parsed


def _validate_approved_reuse_authority(value):
    for field, label in (
        ("review_bundle_id", "Missing-voice reuse review bundle ID"),
        ("review_bundle_sha256", "Missing-voice reuse review bundle SHA-256"),
        ("review_session_sha256", "Missing-voice reuse review session SHA-256"),
        ("blind_key_sha256", "Missing-voice reuse blind-key SHA-256"),
    ):
        _required_sha256(value.get(field), label)
    selected = value.get("selected_candidates")
    if not isinstance(selected, list):
        raise SourceReferenceBindingError(
            "Approved missing-voice selected candidates must be a list"
        )
    candidates = {}
    candidate_ids = []
    for record in selected:
        if not isinstance(record, dict) or set(record) != {
            "candidate_id",
            "voice_character",
            "reference_sha256s",
        }:
            raise SourceReferenceBindingError(
                "Approved missing-voice selected candidate is malformed"
            )
        candidate_id = _required_sha256(
            record.get("candidate_id"), "Approved missing-voice candidate ID"
        )
        character = _text(
            record.get("voice_character"), "Approved missing-voice candidate voice"
        )
        references = record.get("reference_sha256s")
        if not isinstance(references, list) or not references:
            raise SourceReferenceBindingError(
                "Approved missing-voice candidate references are empty"
            )
        for reference in references:
            _required_sha256(
                reference, "Approved missing-voice candidate reference SHA-256"
            )
        normalized = normalize_character_name(character)
        if normalized in candidates:
            raise SourceReferenceBindingError(
                "Approved missing-voice candidate voices are duplicated"
            )
        candidates[normalized] = character
        candidate_ids.append(candidate_id)
    if candidate_ids != sorted(set(candidate_ids)):
        raise SourceReferenceBindingError(
            "Approved missing-voice candidates are not canonical"
        )
    decisions = value.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise SourceReferenceBindingError("Approved missing-voice decisions are empty")
    observed_cohorts = []
    observed_queue_ids = set()
    used_candidate_ids = set()
    selected_by_id = {
        record["candidate_id"]: normalize_character_name(record["voice_character"])
        for record in selected
    }
    expected_overrides = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            raise SourceReferenceBindingError(
                "Approved missing-voice decision is malformed"
            )
        cohort_id = _required_sha256(
            decision.get("cohort_id"), "Approved missing-voice cohort ID"
        )
        queue_ids = decision.get("queue_ids")
        if (
            not isinstance(queue_ids, list)
            or not queue_ids
            or queue_ids != sorted(set(queue_ids))
        ):
            raise SourceReferenceBindingError(
                "Approved missing-voice decision queue IDs are not canonical"
            )
        if observed_queue_ids.intersection(queue_ids):
            raise SourceReferenceBindingError(
                "Approved missing-voice decisions overlap queue IDs"
            )
        observed_queue_ids.update(queue_ids)
        outcome = decision.get("decision")
        origin = decision.get("review_decision_origin", "human_review")
        if origin not in {"human_review", "automatic_no_complete_candidate"}:
            raise SourceReferenceBindingError(
                "Missing-voice review decision origin is unsupported"
            )
        origin_field = (
            {"review_decision_origin"}
            if "review_decision_origin" in decision
            else set()
        )
        if outcome == "neither":
            if set(decision) != {
                "cohort_id",
                "decision",
                "queue_ids",
                *origin_field,
            }:
                raise SourceReferenceBindingError(
                    "Neither missing-voice decision is malformed"
                )
        elif outcome == "candidate":
            if (
                set(decision)
                != {
                    "cohort_id",
                    "decision",
                    "candidate_id",
                    "voice_character",
                    "queue_ids",
                    *origin_field,
                }
                or origin != "human_review"
            ):
                raise SourceReferenceBindingError(
                    "Selected missing-voice decision is malformed"
                )
            candidate_id = _required_sha256(
                decision.get("candidate_id"), "Selected missing-voice candidate ID"
            )
            voice = _text(
                decision.get("voice_character"), "Selected missing-voice voice"
            )
            if selected_by_id.get(candidate_id) != normalize_character_name(voice):
                raise SourceReferenceBindingError(
                    "Selected missing-voice decision references an unknown candidate"
                )
            used_candidate_ids.add(candidate_id)
            expected_overrides.update({queue_id: voice for queue_id in queue_ids})
        else:
            raise SourceReferenceBindingError(
                "Approved missing-voice decision outcome is unsupported"
            )
        observed_cohorts.append(cohort_id)
    if observed_cohorts != sorted(set(observed_cohorts)):
        raise SourceReferenceBindingError(
            "Approved missing-voice decisions are not canonical"
        )
    if observed_cohorts != value.get("cohort_ids"):
        raise SourceReferenceBindingError(
            "Approved missing-voice decisions disagree with declared cohorts"
        )
    target_mode = value.get("target_mode")
    controls = value.get("source_failed_state_item_sha256s")
    if target_mode == "failed":
        if (
            not isinstance(controls, dict)
            or set(controls) != observed_queue_ids
            or any(not _is_sha256(digest) for digest in controls.values())
        ):
            raise SourceReferenceBindingError(
                "Failed missing-voice decisions lack exact source-item authority"
            )
    elif target_mode is not None or controls is not None:
        raise SourceReferenceBindingError(
            "Missing-voice target-mode authority is invalid"
        )
    if used_candidate_ids != set(selected_by_id):
        raise SourceReferenceBindingError(
            "Approved missing-voice selected candidates must each bind a cohort"
        )
    if value.get("queue_voice_overrides") != expected_overrides:
        raise SourceReferenceBindingError(
            "Approved missing-voice decisions disagree with queue overrides"
        )
    return candidates


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
    "MISSING_VOICE_REUSE_BINDING_FIELD",
    "MISSING_VOICE_REUSE_BINDING_SCHEMA",
    "MISSING_VOICE_REUSE_BINDING_VERSION",
    "MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION",
    "SUPPORTED_MISSING_VOICE_REUSE_BINDING_VERSIONS",
    "SUPPORTED_SOURCE_REFERENCE_BINDINGS_VERSIONS",
    "SourceReferenceBindingError",
    "queue_voice_overrides_from_manifest",
    "queue_voice_overrides_sha256",
    "retired_source_reference_variants_from_manifest",
]

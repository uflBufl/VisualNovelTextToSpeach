"""Checksum-bound portrait similarity suggestions and human-approved aliases."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import TypeAlias, TypedDict

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.cohort_review import (
    CohortReviewError,
    _load_document,
    _write_document_no_replace,
)
from vntts.authoring.source_reference_quality import (
    SourceReferenceQualityError,
    load_source_reference_quality_review,
)
from vntts.authoring.workspace_foundation import contained_regular_file

PORTRAIT_ALIAS_PLAN_SCHEMA = "vntts.authoring-portrait-alias-plan"
PORTRAIT_ALIAS_PLAN_VERSION = 1
PORTRAIT_ALIAS_DECISION_SCHEMA = "vntts.authoring-portrait-alias-decision"
PORTRAIT_ALIAS_DECISION_VERSION = 1
DEFAULT_MAX_DHASH_DISTANCE = 6
MAX_DHASH_DISTANCE = 12
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

JsonObject: TypeAlias = dict[str, object]


class _PortraitVariant(TypedDict):
    variant_id: str
    character: str
    portrait: object
    source_bank: str
    portrait_image_sha256: str
    dhash: str


class _PortraitAliasSuggestionBody(TypedDict):
    character: str
    source_bank: str
    dhash_distance: int
    variants: list[_PortraitVariant]


class _PortraitAliasSuggestion(_PortraitAliasSuggestionBody):
    suggestion_id: str


class _PortraitAliasPlanBody(TypedDict):
    schema: str
    schema_version: int
    source_quality_review: str
    source_quality_review_sha256: str
    max_dhash_distance: int
    eligible_variant_count: int
    suggestion_count: int
    suggestions: list[_PortraitAliasSuggestion]


class _PortraitAliasPlanDocument(_PortraitAliasPlanBody):
    plan_id: str


class _PortraitAliasIdentityBody(TypedDict):
    character: str
    source_bank: str
    variants: list[_PortraitVariant]


class _PortraitAliasIdentity(_PortraitAliasIdentityBody):
    identity_id: str


class _PortraitAliasDecisionBody(TypedDict):
    schema: str
    schema_version: int
    plan_id: str
    accepted_suggestion_ids: list[str]
    identity_count: int
    identities: list[_PortraitAliasIdentity]


class _PortraitAliasDecisionDocument(_PortraitAliasDecisionBody):
    decision_id: str


class PortraitAliasError(RuntimeError):
    """Portrait aliases cannot be proven from the supplied immutable evidence."""


@dataclass(frozen=True)
class PortraitAliasPlan:
    plan_id: str
    document: _PortraitAliasPlanDocument

    def to_dict(self) -> _PortraitAliasPlanDocument:
        return copy.deepcopy(self.document)


@dataclass(frozen=True)
class PortraitAliasDecision:
    decision_id: str
    document: _PortraitAliasDecisionDocument

    def to_dict(self) -> _PortraitAliasDecisionDocument:
        return copy.deepcopy(self.document)


def build_portrait_alias_plan(
    quality_review_path: str | Path,
    *,
    max_dhash_distance: int = DEFAULT_MAX_DHASH_DISTANCE,
) -> PortraitAliasPlan:
    """Suggest same-character expression aliases without granting authority."""
    if (
        not isinstance(max_dhash_distance, int)
        or isinstance(max_dhash_distance, bool)
        or not 0 <= max_dhash_distance <= MAX_DHASH_DISTANCE
    ):
        raise PortraitAliasError(
            f"Portrait dHash distance must be an integer from 0 to {MAX_DHASH_DISTANCE}"
        )
    path = Path(quality_review_path).expanduser().resolve()
    payload = _read(path, "source-reference quality review")
    source_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        review = load_source_reference_quality_review(path)
    except SourceReferenceQualityError as error:
        raise PortraitAliasError(str(error)) from error
    variants: list[_PortraitVariant] = []
    review_variants = review.get("variants")
    if not isinstance(review_variants, list):
        raise PortraitAliasError("Source-reference quality review variants are invalid")
    for card in review_variants:
        decision = card.get("decision")
        portrait_image = card.get("portrait_image")
        if (
            not isinstance(decision, dict)
            or decision.get("decision") != "accept"
            or not isinstance(portrait_image, dict)
        ):
            continue
        image_path = _contained_file(path.parent, portrait_image.get("image"))
        image_payload = _read(image_path, f"portrait {card['variant_id']}")
        image_sha256 = hashlib.sha256(image_payload).hexdigest()
        if image_sha256 != portrait_image.get("image_sha256"):
            raise PortraitAliasError(f"Portrait image changed: {card['variant_id']}")
        variant_id = _required_text(card.get("variant_id"), "quality variant ID")
        variants.append(
            {
                "variant_id": variant_id,
                "character": _required_text(card.get("character"), "quality character"),
                "portrait": card["portrait"],
                "source_bank": _required_text(
                    card.get("source_bank"), "quality source bank"
                ),
                "portrait_image_sha256": image_sha256,
                "dhash": _dhash(image_payload, variant_id),
            }
        )
    suggestions: list[_PortraitAliasSuggestion] = []
    for first, second in combinations(variants, 2):
        if (
            first["character"].casefold() != second["character"].casefold()
            or first["source_bank"].casefold() != second["source_bank"].casefold()
            or first["portrait"] == second["portrait"]
        ):
            continue
        distance = _hamming(first["dhash"], second["dhash"])
        if distance > max_dhash_distance:
            continue
        members = sorted(
            (copy.deepcopy(first), copy.deepcopy(second)),
            key=lambda value: value["variant_id"],
        )
        suggestion_body: _PortraitAliasSuggestionBody = {
            "character": members[0]["character"],
            "source_bank": members[0]["source_bank"],
            "dhash_distance": distance,
            "variants": members,
        }
        suggestions.append(
            {
                "suggestion_id": canonical_document_sha256(suggestion_body),
                **suggestion_body,
            }
        )
    suggestions.sort(key=lambda value: value["suggestion_id"])
    body: _PortraitAliasPlanBody = {
        "schema": PORTRAIT_ALIAS_PLAN_SCHEMA,
        "schema_version": PORTRAIT_ALIAS_PLAN_VERSION,
        "source_quality_review": str(path),
        "source_quality_review_sha256": source_sha256,
        "max_dhash_distance": max_dhash_distance,
        "eligible_variant_count": len(variants),
        "suggestion_count": len(suggestions),
        "suggestions": suggestions,
    }
    document: _PortraitAliasPlanDocument = {
        "schema": body["schema"],
        "schema_version": body["schema_version"],
        "source_quality_review": body["source_quality_review"],
        "source_quality_review_sha256": body["source_quality_review_sha256"],
        "max_dhash_distance": body["max_dhash_distance"],
        "eligible_variant_count": body["eligible_variant_count"],
        "suggestion_count": body["suggestion_count"],
        "suggestions": body["suggestions"],
        "plan_id": canonical_document_sha256(body),
    }
    if (
        hashlib.sha256(_read(path, "source-reference quality review")).hexdigest()
        != source_sha256
    ):
        raise PortraitAliasError(
            "Source-reference quality review changed while aliases were planned"
        )
    return PortraitAliasPlan(document["plan_id"], document)


def write_portrait_alias_plan(plan: PortraitAliasPlan, output: str | Path) -> Path:
    if not isinstance(plan, PortraitAliasPlan):
        raise PortraitAliasError("Portrait alias plan is invalid")
    try:
        return Path(
            _write_document_no_replace(output, plan.document, "portrait alias plan")
        )
    except CohortReviewError as error:
        raise PortraitAliasError(str(error)) from error


def load_portrait_alias_plan(path: str | Path) -> PortraitAliasPlan:
    path = Path(path).expanduser().resolve()
    try:
        loaded_document = _load_document(path, "portrait alias plan")
    except CohortReviewError as error:
        raise PortraitAliasError(str(error)) from error
    document = _validate_plan_shape(loaded_document)
    rebuilt = build_portrait_alias_plan(
        document["source_quality_review"],
        max_dhash_distance=document["max_dhash_distance"],
    )
    if rebuilt.document != document:
        raise PortraitAliasError("Portrait alias plan no longer matches its evidence")
    return PortraitAliasPlan(document["plan_id"], document)


def build_portrait_alias_decision(
    plan: PortraitAliasPlan, accepted_suggestion_ids: object
) -> PortraitAliasDecision:
    """Record explicit human authority over exact suggested pairs."""
    if not isinstance(plan, PortraitAliasPlan):
        raise PortraitAliasError("Portrait alias plan is invalid")
    accepted = _distinct_texts(accepted_suggestion_ids, "Accepted suggestion IDs")
    available = {
        suggestion["suggestion_id"]: suggestion
        for suggestion in plan.document["suggestions"]
    }
    unknown = sorted(set(accepted) - set(available))
    if unknown:
        raise PortraitAliasError(
            "Accepted portrait alias suggestion is absent: " + ", ".join(unknown)
        )
    if not accepted:
        raise PortraitAliasError("At least one portrait alias suggestion is required")
    groups = _connected_alias_groups([available[value] for value in accepted])
    body: _PortraitAliasDecisionBody = {
        "schema": PORTRAIT_ALIAS_DECISION_SCHEMA,
        "schema_version": PORTRAIT_ALIAS_DECISION_VERSION,
        "plan_id": plan.plan_id,
        "accepted_suggestion_ids": list(accepted),
        "identity_count": len(groups),
        "identities": groups,
    }
    document: _PortraitAliasDecisionDocument = {
        "schema": body["schema"],
        "schema_version": body["schema_version"],
        "plan_id": body["plan_id"],
        "accepted_suggestion_ids": body["accepted_suggestion_ids"],
        "identity_count": body["identity_count"],
        "identities": body["identities"],
        "decision_id": canonical_document_sha256(body),
    }
    return PortraitAliasDecision(document["decision_id"], document)


def write_portrait_alias_decision(
    decision: PortraitAliasDecision, output: str | Path
) -> Path:
    if not isinstance(decision, PortraitAliasDecision):
        raise PortraitAliasError("Portrait alias decision is invalid")
    try:
        return Path(
            _write_document_no_replace(
                output, decision.document, "portrait alias decision"
            )
        )
    except CohortReviewError as error:
        raise PortraitAliasError(str(error)) from error


def load_portrait_alias_decision(
    path: str | Path, plan: PortraitAliasPlan
) -> PortraitAliasDecision:
    if not isinstance(plan, PortraitAliasPlan):
        raise PortraitAliasError("Portrait alias plan is invalid")
    try:
        document = _load_document(path, "portrait alias decision")
    except CohortReviewError as error:
        raise PortraitAliasError(str(error)) from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != PORTRAIT_ALIAS_DECISION_SCHEMA
        or document.get("schema_version") != PORTRAIT_ALIAS_DECISION_VERSION
        or document.get("plan_id") != plan.plan_id
    ):
        raise PortraitAliasError("Unsupported portrait alias decision")
    rebuilt = build_portrait_alias_decision(
        plan, document.get("accepted_suggestion_ids")
    )
    if rebuilt.document != document:
        raise PortraitAliasError("Portrait alias decision is inconsistent")
    return rebuilt


def portrait_identity_by_variant(decision: PortraitAliasDecision) -> dict[str, str]:
    if not isinstance(decision, PortraitAliasDecision):
        raise PortraitAliasError("Portrait alias decision is invalid")
    return {
        variant["variant_id"]: identity["identity_id"]
        for identity in decision.document["identities"]
        for variant in identity["variants"]
    }


def _connected_alias_groups(
    suggestions: Sequence[_PortraitAliasSuggestion],
) -> list[_PortraitAliasIdentity]:
    variants: dict[str, _PortraitVariant] = {}
    adjacency: dict[str, set[str]] = {}
    for suggestion in suggestions:
        first, second = suggestion["variants"]
        for variant in (first, second):
            variants[variant["variant_id"]] = variant
            adjacency.setdefault(variant["variant_id"], set())
        adjacency[first["variant_id"]].add(second["variant_id"])
        adjacency[second["variant_id"]].add(first["variant_id"])
    groups: list[_PortraitAliasIdentity] = []
    remaining: set[str] = set(adjacency)
    while remaining:
        pending: list[str] = [min(remaining)]
        component: set[str] = set()
        while pending:
            variant_id = pending.pop()
            if variant_id in component:
                continue
            component.add(variant_id)
            pending.extend(adjacency[variant_id] - component)
        remaining -= component
        members = [variants[value] for value in sorted(component)]
        characters = {value["character"].casefold() for value in members}
        banks = {value["source_bank"].casefold() for value in members}
        if len(characters) != 1 or len(banks) != 1:
            raise PortraitAliasError("Portrait alias group crosses a voice identity")
        identity_body: _PortraitAliasIdentityBody = {
            "character": members[0]["character"],
            "source_bank": members[0]["source_bank"],
            "variants": members,
        }
        groups.append(
            {"identity_id": canonical_document_sha256(identity_body), **identity_body}
        )
    groups.sort(key=lambda value: value["identity_id"])
    return groups


def _validate_plan_shape(document: JsonObject) -> _PortraitAliasPlanDocument:
    source_quality_review = document.get("source_quality_review")
    source_quality_review_sha256 = document.get("source_quality_review_sha256")
    max_dhash_distance = document.get("max_dhash_distance")
    eligible_variant_count = document.get("eligible_variant_count")
    suggestion_count = document.get("suggestion_count")
    raw_suggestions = document.get("suggestions")
    plan_id = document.get("plan_id")
    schema = document.get("schema")
    schema_version = document.get("schema_version")
    if (
        schema != PORTRAIT_ALIAS_PLAN_SCHEMA
        or schema_version != PORTRAIT_ALIAS_PLAN_VERSION
        or not isinstance(source_quality_review, str)
        or not isinstance(source_quality_review_sha256, str)
        or not isinstance(max_dhash_distance, int)
        or not isinstance(eligible_variant_count, int)
        or not isinstance(suggestion_count, int)
        or not isinstance(raw_suggestions, list)
        or not isinstance(plan_id, str)
    ):
        raise PortraitAliasError("Unsupported portrait alias plan")
    suggestions: list[_PortraitAliasSuggestion] = []
    for value in raw_suggestions:
        if not isinstance(value, dict):
            raise PortraitAliasError("Unsupported portrait alias plan")
        variants = value.get("variants")
        if (
            not isinstance(value.get("suggestion_id"), str)
            or not isinstance(value.get("character"), str)
            or not isinstance(value.get("source_bank"), str)
            or not isinstance(value.get("dhash_distance"), int)
            or not isinstance(variants, list)
        ):
            raise PortraitAliasError("Unsupported portrait alias plan")
        typed_variants: list[_PortraitVariant] = []
        for variant in variants:
            if (
                not isinstance(variant, dict)
                or not isinstance(variant.get("variant_id"), str)
                or not isinstance(variant.get("character"), str)
                or not isinstance(variant.get("source_bank"), str)
                or not isinstance(variant.get("portrait_image_sha256"), str)
                or not isinstance(variant.get("dhash"), str)
                or "portrait" not in variant
            ):
                raise PortraitAliasError("Unsupported portrait alias plan")
            typed_variants.append(
                {
                    "variant_id": variant["variant_id"],
                    "character": variant["character"],
                    "portrait": variant["portrait"],
                    "source_bank": variant["source_bank"],
                    "portrait_image_sha256": variant["portrait_image_sha256"],
                    "dhash": variant["dhash"],
                }
            )
        suggestions.append(
            {
                "suggestion_id": value["suggestion_id"],
                "character": value["character"],
                "source_bank": value["source_bank"],
                "dhash_distance": value["dhash_distance"],
                "variants": typed_variants,
            }
        )
    return {
        "schema": schema,
        "schema_version": schema_version,
        "source_quality_review": source_quality_review,
        "source_quality_review_sha256": source_quality_review_sha256,
        "max_dhash_distance": max_dhash_distance,
        "eligible_variant_count": eligible_variant_count,
        "suggestion_count": suggestion_count,
        "suggestions": suggestions,
        "plan_id": plan_id,
    }


def _dhash(payload: bytes, label: str) -> str:
    image = QImage.fromData(payload) if payload.startswith(PNG_SIGNATURE) else QImage()
    if image.isNull():
        raise PortraitAliasError(f"Portrait image is not a valid PNG: {label}")
    scaled = image.convertToFormat(QImage.Format.Format_Grayscale8).scaled(
        9,
        8,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | int(
                scaled.pixelColor(x, y).red() > scaled.pixelColor(x + 1, y).red()
            )
    return f"{bits:016x}"


def _hamming(first: str, second: str) -> int:
    return (int(first, 16) ^ int(second, 16)).bit_count()


def _contained_file(root: str | Path, relative: object) -> Path:
    return Path(
        contained_regular_file(
            root, relative, "portrait image", error_type=PortraitAliasError
        )
    )


def _read(path: str | Path, label: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as error:
        raise PortraitAliasError(f"Unable to read {label} {path}: {error}") from error


def _distinct_texts(values: object, label: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise PortraitAliasError(f"{label} must be a collection")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise PortraitAliasError(f"{label} must contain non-empty text")
        value = value.strip()
        if value in seen:
            raise PortraitAliasError(f"{label} must be distinct")
        seen.add(value)
        result.append(value)
    return tuple(sorted(result))


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortraitAliasError(f"{label} must be non-empty text")
    return value


__all__ = [
    "DEFAULT_MAX_DHASH_DISTANCE",
    "PORTRAIT_ALIAS_DECISION_SCHEMA",
    "PORTRAIT_ALIAS_DECISION_VERSION",
    "PORTRAIT_ALIAS_PLAN_SCHEMA",
    "PORTRAIT_ALIAS_PLAN_VERSION",
    "PortraitAliasDecision",
    "PortraitAliasError",
    "PortraitAliasPlan",
    "build_portrait_alias_decision",
    "build_portrait_alias_plan",
    "load_portrait_alias_decision",
    "load_portrait_alias_plan",
    "portrait_identity_by_variant",
    "write_portrait_alias_decision",
    "write_portrait_alias_plan",
]

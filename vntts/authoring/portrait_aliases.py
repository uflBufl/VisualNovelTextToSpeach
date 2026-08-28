"""Checksum-bound portrait similarity suggestions and human-approved aliases."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QByteArray, Qt
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

PORTRAIT_ALIAS_PLAN_SCHEMA = "vntts.authoring-portrait-alias-plan"
PORTRAIT_ALIAS_PLAN_VERSION = 1
PORTRAIT_ALIAS_DECISION_SCHEMA = "vntts.authoring-portrait-alias-decision"
PORTRAIT_ALIAS_DECISION_VERSION = 1
DEFAULT_MAX_DHASH_DISTANCE = 6
MAX_DHASH_DISTANCE = 12


class PortraitAliasError(RuntimeError):
    """Portrait aliases cannot be proven from the supplied immutable evidence."""


@dataclass(frozen=True)
class PortraitAliasPlan:
    plan_id: str
    document: dict

    def to_dict(self):
        return copy.deepcopy(self.document)


@dataclass(frozen=True)
class PortraitAliasDecision:
    decision_id: str
    document: dict

    def to_dict(self):
        return copy.deepcopy(self.document)


def build_portrait_alias_plan(
    quality_review_path,
    *,
    max_dhash_distance=DEFAULT_MAX_DHASH_DISTANCE,
):
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
    variants = []
    for card in review["variants"]:
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
        variants.append(
            {
                "variant_id": card["variant_id"],
                "character": card["character"],
                "portrait": card["portrait"],
                "source_bank": card["source_bank"],
                "portrait_image_sha256": image_sha256,
                "dhash": _dhash(image_payload, card["variant_id"]),
            }
        )
    suggestions = []
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
        body = {
            "character": members[0]["character"],
            "source_bank": members[0]["source_bank"],
            "dhash_distance": distance,
            "variants": members,
        }
        suggestions.append({"suggestion_id": canonical_document_sha256(body), **body})
    suggestions.sort(key=lambda value: value["suggestion_id"])
    body = {
        "schema": PORTRAIT_ALIAS_PLAN_SCHEMA,
        "schema_version": PORTRAIT_ALIAS_PLAN_VERSION,
        "source_quality_review": str(path),
        "source_quality_review_sha256": source_sha256,
        "max_dhash_distance": max_dhash_distance,
        "eligible_variant_count": len(variants),
        "suggestion_count": len(suggestions),
        "suggestions": suggestions,
    }
    document = {**body, "plan_id": canonical_document_sha256(body)}
    if (
        hashlib.sha256(_read(path, "source-reference quality review")).hexdigest()
        != source_sha256
    ):
        raise PortraitAliasError(
            "Source-reference quality review changed while aliases were planned"
        )
    return PortraitAliasPlan(document["plan_id"], document)


def write_portrait_alias_plan(plan, output):
    if not isinstance(plan, PortraitAliasPlan):
        raise PortraitAliasError("Portrait alias plan is invalid")
    try:
        return _write_document_no_replace(output, plan.document, "portrait alias plan")
    except CohortReviewError as error:
        raise PortraitAliasError(str(error)) from error


def load_portrait_alias_plan(path):
    path = Path(path).expanduser().resolve()
    try:
        document = _load_document(path, "portrait alias plan")
    except CohortReviewError as error:
        raise PortraitAliasError(str(error)) from error
    _validate_plan_shape(document)
    rebuilt = build_portrait_alias_plan(
        document["source_quality_review"],
        max_dhash_distance=document["max_dhash_distance"],
    )
    if rebuilt.document != document:
        raise PortraitAliasError("Portrait alias plan no longer matches its evidence")
    return PortraitAliasPlan(document["plan_id"], document)


def build_portrait_alias_decision(plan, accepted_suggestion_ids):
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
    body = {
        "schema": PORTRAIT_ALIAS_DECISION_SCHEMA,
        "schema_version": PORTRAIT_ALIAS_DECISION_VERSION,
        "plan_id": plan.plan_id,
        "accepted_suggestion_ids": list(accepted),
        "identity_count": len(groups),
        "identities": groups,
    }
    document = {**body, "decision_id": canonical_document_sha256(body)}
    return PortraitAliasDecision(document["decision_id"], document)


def write_portrait_alias_decision(decision, output):
    if not isinstance(decision, PortraitAliasDecision):
        raise PortraitAliasError("Portrait alias decision is invalid")
    try:
        return _write_document_no_replace(
            output, decision.document, "portrait alias decision"
        )
    except CohortReviewError as error:
        raise PortraitAliasError(str(error)) from error


def load_portrait_alias_decision(path, plan):
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


def portrait_identity_by_variant(decision):
    if not isinstance(decision, PortraitAliasDecision):
        raise PortraitAliasError("Portrait alias decision is invalid")
    return {
        variant["variant_id"]: identity["identity_id"]
        for identity in decision.document["identities"]
        for variant in identity["variants"]
    }


def _connected_alias_groups(suggestions):
    variants = {}
    adjacency = {}
    for suggestion in suggestions:
        first, second = suggestion["variants"]
        for value in (first, second):
            variants[value["variant_id"]] = value
            adjacency.setdefault(value["variant_id"], set())
        adjacency[first["variant_id"]].add(second["variant_id"])
        adjacency[second["variant_id"]].add(first["variant_id"])
    groups = []
    remaining = set(adjacency)
    while remaining:
        pending = [min(remaining)]
        component = set()
        while pending:
            value = pending.pop()
            if value in component:
                continue
            component.add(value)
            pending.extend(adjacency[value] - component)
        remaining -= component
        members = [variants[value] for value in sorted(component)]
        characters = {value["character"].casefold() for value in members}
        banks = {value["source_bank"].casefold() for value in members}
        if len(characters) != 1 or len(banks) != 1:
            raise PortraitAliasError("Portrait alias group crosses a voice identity")
        identity_body = {
            "character": members[0]["character"],
            "source_bank": members[0]["source_bank"],
            "variants": members,
        }
        groups.append(
            {"identity_id": canonical_document_sha256(identity_body), **identity_body}
        )
    groups.sort(key=lambda value: value["identity_id"])
    return groups


def _validate_plan_shape(document):
    if (
        not isinstance(document, dict)
        or document.get("schema") != PORTRAIT_ALIAS_PLAN_SCHEMA
        or document.get("schema_version") != PORTRAIT_ALIAS_PLAN_VERSION
        or not isinstance(document.get("source_quality_review"), str)
        or not isinstance(document.get("max_dhash_distance"), int)
        or not isinstance(document.get("suggestions"), list)
    ):
        raise PortraitAliasError("Unsupported portrait alias plan")


def _dhash(payload, label):
    image = QImage.fromData(QByteArray(payload), "PNG")
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


def _hamming(first, second):
    return (int(first, 16) ^ int(second, 16)).bit_count()


def _contained_file(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise PortraitAliasError("Portrait image path is invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PortraitAliasError("Portrait image path is invalid")
    root = Path(root).resolve()
    path = (root / Path(*pure.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise PortraitAliasError("Portrait image leaves its review root") from error
    if not path.is_file() or path.is_symlink():
        raise PortraitAliasError(f"Portrait image is missing: {path}")
    return path


def _read(path, label):
    try:
        return Path(path).read_bytes()
    except OSError as error:
        raise PortraitAliasError(f"Unable to read {label} {path}: {error}") from error


def _distinct_texts(values, label):
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise PortraitAliasError(f"{label} must be a collection")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise PortraitAliasError(f"{label} must contain non-empty text")
        value = value.strip()
        if value in seen:
            raise PortraitAliasError(f"{label} must be distinct")
        seen.add(value)
        result.append(value)
    return tuple(sorted(result))


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

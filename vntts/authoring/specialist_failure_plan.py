"""Checksum-bound next actions for terminal specialist repair failures."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.cohort_review import (
    CohortReviewError,
    _load_document,
    _write_document_no_replace,
)
from vntts.authoring.failure_repair import (
    INLINE_PAUSE_MARKER,
    MAX_BOUNDED_TOTAL_ATTEMPTS,
    OFFLINE_FALLBACK_BACKEND,
    SENTENCE_BOUNDARY_SEGMENTATION,
)
from vntts.document_identity import is_lowercase_sha256

SPECIALIST_FAILURE_PLAN_SCHEMA = "vntts.authoring-specialist-failure-plan"
SPECIALIST_FAILURE_PLAN_VERSION = 1
REFERENCE_OR_LIVE = "reference_comparison_or_live_fallback"
SENTENCE_REPAIR_RETRY = "sentence_boundary_retry"
JsonObject = dict[str, object]


@dataclass(frozen=True)
class SpecialistFailurePlan:
    plan_id: str
    document: JsonObject

    def to_dict(self) -> JsonObject:
        return dict(self.document)


def build_specialist_failure_plan(
    workspace_directories: Iterable[str | Path],
) -> SpecialistFailurePlan:
    paths = tuple(
        sorted({Path(value).resolve() for value in workspace_directories}, key=str)
    )
    if not paths:
        raise CohortReviewError("A specialist failure plan requires a workspace")
    sources: list[JsonObject] = []
    items: list[JsonObject] = []
    seen: set[str] = set()
    snapshots: list[tuple[Path, str]] = []
    for workspace in paths:
        source, workspace_items, captured = _capture_workspace(workspace, seen)
        sources.append(source)
        items.extend(workspace_items)
        snapshots.extend(captured)
    items.sort(key=lambda value: _text_field(value, "queue_id"))
    body = _build_plan_body(sources, items)
    for path, digest in snapshots:
        try:
            if sha256_file(path) != digest:
                raise CohortReviewError(
                    f"Specialist source changed during planning: {path}"
                )
        except OSError as error:
            raise CohortReviewError(str(error)) from error
    plan_id = canonical_document_sha256(body)
    return SpecialistFailurePlan(plan_id, _validated({**body, "plan_id": plan_id}))


def _capture_workspace(
    workspace: Path, seen: set[str]
) -> tuple[JsonObject, list[JsonObject], list[tuple[Path, str]]]:
    configuration_path = workspace / "workspace.json"
    state_path = workspace / "generated-audio/generation-state.json"
    queue_path = workspace / "queue.jsonl"
    configuration_payload = _read(configuration_path, "workspace configuration")
    state_payload = _read(state_path, "generation state")
    queue_payload = _read(queue_path, "generation queue")
    configuration = _decode(configuration_payload, "workspace configuration")
    state = _decode(state_payload, "generation state")
    queue = _queue_records(queue_payload)
    selected = _selected_failure_ids(configuration)
    state_items = _object_field(state, "items", "generation state items")
    items: list[JsonObject] = []
    for queue_id in selected:
        result = state_items.get(queue_id)
        if result is not None and not isinstance(result, dict):
            raise CohortReviewError(
                f"Specialist generation result is invalid: {queue_id}"
            )
        if not isinstance(result, dict) or result.get("status") != "failed":
            continue
        if queue_id in seen:
            raise CohortReviewError(f"Specialist failure is duplicated: {queue_id}")
        seen.add(queue_id)
        record = queue.get(queue_id)
        if not isinstance(record, dict):
            raise CohortReviewError(f"Specialist queue item disappeared: {queue_id}")
        items.append(
            _project_failed_item(workspace, configuration, record, result, queue_id)
        )
    source = {
        "workspace": str(workspace),
        "workspace_id": configuration.get("workspace_id"),
        "config_fingerprint": configuration.get("config_fingerprint"),
        "state_sha256": hashlib.sha256(state_payload).hexdigest(),
        "queue_sha256": hashlib.sha256(queue_payload).hexdigest(),
        "failed_item_count": len(items),
    }
    snapshots: list[tuple[Path, str]] = []
    for path, payload, label in (
        (configuration_path, configuration_payload, "workspace configuration"),
        (state_path, state_payload, "generation state"),
        (queue_path, queue_payload, "generation queue"),
    ):
        if _read(path, label) != payload:
            raise CohortReviewError(f"Specialist {label} changed during planning")
        snapshots.append((path, hashlib.sha256(payload).hexdigest()))
    return source, items, snapshots


def _project_failed_item(
    workspace: Path,
    configuration: JsonObject,
    record: JsonObject,
    result: JsonObject,
    queue_id: str,
) -> JsonObject:
    failure = result.get("failure")
    repair = result.get("failure_repair")
    if not isinstance(failure, dict) or not isinstance(repair, dict):
        raise CohortReviewError(
            f"Specialist failure evidence is incomplete: {queue_id}"
        )
    action, rationale = _next_action(result, repair, failure)
    text = _text_field(record, "text")
    text_features = failure.get("text_features")
    text_features = text_features if isinstance(text_features, dict) else {}
    item: JsonObject = {
        "workspace": str(workspace),
        "workspace_id": configuration.get("workspace_id"),
        "queue_id": queue_id,
        "line_id": record.get("line_id"),
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_speaker": record.get("speaker"),
        "effective_voice": result.get("voice_character"),
        "provider": result.get("provider"),
        "model": result.get("model"),
        "generation_profile": result.get("generation_profile"),
        "repair_strategy": repair.get("strategy"),
        "failure": failure,
        "result_sha256": canonical_document_sha256(result),
        "text_shape": {
            "sentence_boundary_count": text_features.get("sentence_boundary_count"),
            "word_count": text_features.get("word_count"),
            "has_ellipsis": bool(text_features.get("ellipsis_count")),
        },
        "next_action": action,
        "rationale": rationale,
    }
    item["cluster_key"] = _cluster_key(item)
    return item


def _cluster_key(item: JsonObject) -> str:
    fields = (
        "provider",
        "model",
        "generation_profile",
        "effective_voice",
        "repair_strategy",
        "text_shape",
        "next_action",
    )
    failure = item.get("failure")
    if not isinstance(failure, dict) or any(field not in item for field in fields):
        raise CohortReviewError("Specialist cluster evidence is invalid")
    return canonical_document_sha256(
        {key: item[key] for key in fields}
        | {
            "failure_kind": failure.get("kind"),
            "completion": failure.get("completion"),
            "error_type": failure.get("error_type"),
        }
    )


def _build_plan_body(sources: list[JsonObject], items: list[JsonObject]) -> JsonObject:
    grouped: defaultdict[str, list[str]] = defaultdict(list)
    next_actions: dict[str, str] = {}
    for item in items:
        cluster_key = _text_field(item, "cluster_key")
        grouped[cluster_key].append(_text_field(item, "queue_id"))
        if cluster_key not in next_actions:
            next_actions[cluster_key] = _text_field(item, "next_action")
    clusters = [
        {
            "cluster_key": key,
            "item_count": len(queue_ids),
            "queue_ids": sorted(queue_ids),
            "next_action": next_actions[key],
        }
        for key, queue_ids in sorted(grouped.items())
    ]
    body = {
        "schema": SPECIALIST_FAILURE_PLAN_SCHEMA,
        "schema_version": SPECIALIST_FAILURE_PLAN_VERSION,
        "source_count": len(sources),
        "item_count": len(items),
        "cluster_count": len(clusters),
        "action_counts": {
            action: sum(item["next_action"] == action for item in items)
            for action in (
                SENTENCE_REPAIR_RETRY,
                OFFLINE_FALLBACK_BACKEND,
                REFERENCE_OR_LIVE,
            )
        },
        "sources": sources,
        "clusters": clusters,
        "items": items,
    }
    return body


def write_specialist_failure_plan(
    plan: SpecialistFailurePlan | object, output_path: str | Path
) -> Path:
    document = _validated(plan)
    return Path(
        _write_document_no_replace(output_path, document, "specialist failure plan")
    )


def load_specialist_failure_plan(path: str | Path) -> SpecialistFailurePlan:
    document = _validated(_load_document(path, "specialist failure plan"))
    return SpecialistFailurePlan(_text_field(document, "plan_id"), document)


def _next_action(
    result: JsonObject, repair: JsonObject, failure: JsonObject
) -> tuple[str, str]:
    strategy = _text_field(repair, "strategy")
    providers = result.get("attempts_by_provider")
    providers = providers if isinstance(providers, dict) else {}
    if (
        strategy == INLINE_PAUSE_MARKER
        and failure.get("kind") == "speech_silence"
        and _attempt_count(providers.get(_text_field(result, "provider")))
        >= MAX_BOUNDED_TOTAL_ATTEMPTS
        and not _attempt_count(providers.get("pocket-tts"))
    ):
        return (
            OFFLINE_FALLBACK_BACKEND,
            "Inline-pause repair exhausted three primary attempts; one unseeded Pocket attempt remains bounded",
        )
    if (
        strategy == SENTENCE_BOUNDARY_SEGMENTATION
        and failure.get("kind") == "missed_eos_audio_limit"
        and failure.get("completion") == "limited"
        and not _attempt_count(providers.get("pocket-tts"))
    ):
        attempts = result.get("attempts")
        if (
            attempts is not None
            and _attempt_count(attempts) < MAX_BOUNDED_TOTAL_ATTEMPTS
        ):
            return (
                SENTENCE_REPAIR_RETRY,
                "Sentence repair has fewer than three cumulative MOSS attempts; one exact retry remains before Pocket",
            )
        return (
            OFFLINE_FALLBACK_BACKEND,
            "Sentence repair is terminal under MOSS; one unseeded Pocket attempt remains bounded",
        )
    if strategy in {SENTENCE_BOUNDARY_SEGMENTATION, OFFLINE_FALLBACK_BACKEND}:
        return (
            REFERENCE_OR_LIVE,
            "A complete render failed speech quality or Pocket is already terminal; compare a verified reference or retain live fallback",
        )
    raise CohortReviewError(
        "Specialist failure has no evidence-backed next action: "
        f"{strategy!r}/{failure.get('kind')!r}"
    )


def _validated(plan: SpecialistFailurePlan | object) -> JsonObject:
    document = plan.document if isinstance(plan, SpecialistFailurePlan) else plan
    if (
        not isinstance(document, dict)
        or document.get("schema") != SPECIALIST_FAILURE_PLAN_SCHEMA
    ):
        raise CohortReviewError("Unsupported specialist failure plan schema")
    if (
        type(document.get("schema_version")) is not int
        or document.get("schema_version") != SPECIALIST_FAILURE_PLAN_VERSION
    ):
        raise CohortReviewError("Unsupported specialist failure plan version")
    claimed = document.get("plan_id")
    actual = canonical_document_sha256(
        {key: value for key, value in document.items() if key != "plan_id"}
    )
    if claimed != actual:
        raise CohortReviewError("Specialist failure plan identity changed")
    items = document.get("items")
    if (
        not isinstance(items, list)
        or type(document.get("item_count")) is not int
        or document.get("item_count") != len(items)
    ):
        raise CohortReviewError("Specialist failure plan item count is invalid")
    sources = document.get("sources")
    if (
        not isinstance(sources, list)
        or type(document.get("source_count")) is not int
        or document.get("source_count") != len(sources)
    ):
        raise CohortReviewError("Specialist failure plan source count is invalid")
    clusters = document.get("clusters")
    if (
        not isinstance(clusters, list)
        or type(document.get("cluster_count")) is not int
        or document.get("cluster_count") != len(clusters)
    ):
        raise CohortReviewError("Specialist failure plan cluster count is invalid")
    if (
        any(
            not isinstance(source, dict)
            or type(source.get("failed_item_count")) is not int
            for source in sources
        )
        or sum(source["failed_item_count"] for source in sources)
        != document["item_count"]
    ):
        raise CohortReviewError("Specialist failure plan source counts are invalid")
    if any(
        not isinstance(cluster, dict)
        or not isinstance(cluster.get("queue_ids"), list)
        or type(cluster.get("item_count")) is not int
        or cluster["item_count"] != len(cluster["queue_ids"])
        for cluster in clusters
    ):
        raise CohortReviewError(
            "Specialist failure plan cluster item counts are invalid"
        )
    action_counts = document.get("action_counts")
    if (
        not isinstance(action_counts, dict)
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in action_counts.values()
        )
        or sum(action_counts.values()) != document["item_count"]
    ):
        raise CohortReviewError("Specialist failure plan action counts are invalid")
    _validate_plan_membership(sources, items, clusters, action_counts)
    return document


def _validate_plan_membership(
    sources: list[object],
    items: list[object],
    clusters: list[object],
    action_counts: JsonObject,
) -> None:
    validated_items: list[JsonObject] = []
    for item in items:
        if not isinstance(item, dict):
            raise CohortReviewError("Specialist failure plan item is invalid")
        for field in ("queue_id", "workspace", "workspace_id", "next_action"):
            _text_field(item, field)
        text = _text_field(item, "text")
        if (
            item.get("cluster_key") != _cluster_key(item)
            or item.get("text_sha256")
            != hashlib.sha256(text.encode("utf-8")).hexdigest()
            or item["next_action"]
            not in {SENTENCE_REPAIR_RETRY, OFFLINE_FALLBACK_BACKEND, REFERENCE_OR_LIVE}
        ):
            raise CohortReviewError("Specialist failure plan item evidence is invalid")
        validated_items.append(item)
    queue_ids = [_text_field(item, "queue_id") for item in validated_items]
    if queue_ids != sorted(set(queue_ids)):
        raise CohortReviewError(
            "Specialist failure plan item IDs must be unique and sorted"
        )
    _validate_source_membership(sources, validated_items)
    expected = _build_plan_body([], validated_items)
    declared_clusters = [
        {
            field: cluster.get(field)
            for field in ("cluster_key", "item_count", "queue_ids", "next_action")
        }
        for cluster in clusters
        if isinstance(cluster, dict)
    ]
    if declared_clusters != expected["clusters"]:
        raise CohortReviewError("Specialist failure plan cluster membership is invalid")
    if action_counts != expected["action_counts"]:
        raise CohortReviewError("Specialist failure plan actions differ from items")


def _validate_source_membership(sources: list[object], items: list[JsonObject]) -> None:
    remaining = Counter(
        (_text_field(item, "workspace"), _text_field(item, "workspace_id"))
        for item in items
    )
    seen: set[tuple[str, str]] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise CohortReviewError("Specialist failure plan source is invalid")
        identity = (
            _text_field(source, "workspace"),
            _text_field(source, "workspace_id"),
        )
        if identity in seen:
            raise CohortReviewError("Specialist failure plan source is duplicated")
        seen.add(identity)
        for field in ("config_fingerprint", "state_sha256", "queue_sha256"):
            digest = source.get(field)
            if not isinstance(digest, str) or not is_lowercase_sha256(digest):
                raise CohortReviewError(f"Specialist source {field} is invalid")
        if source.get("failed_item_count") != remaining.pop(identity, 0):
            raise CohortReviewError(
                "Specialist failure plan source membership is invalid"
            )
    if remaining:
        raise CohortReviewError("Specialist failure plan items have an unknown source")


def _read(path: str | Path, label: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as error:
        raise CohortReviewError(
            f"Unable to read specialist {label}: {error}"
        ) from error


def _decode(payload: bytes, label: str) -> JsonObject:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(
            f"Unable to decode specialist {label}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise CohortReviewError(f"Specialist {label} must be an object")
    return document


def _queue_records(payload: bytes) -> dict[str, JsonObject]:
    try:
        rows = [json.loads(value) for value in payload.decode("utf-8").splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(
            f"Unable to decode specialist queue: {error}"
        ) from error
    records: dict[str, JsonObject] = {}
    for value in rows[1:]:
        if not isinstance(value, dict):
            raise CohortReviewError("Specialist queue record is invalid")
        queue_id = _text_field(value, "queue_id")
        if queue_id in records:
            raise CohortReviewError(f"Specialist queue ID is duplicated: {queue_id}")
        records[queue_id] = value
    return records


def _text_field(document: JsonObject, field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise CohortReviewError(f"Specialist {field} is invalid")
    return value


def _attempt_count(value: object) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CohortReviewError(
            "Specialist attempt count must be a non-negative integer"
        )
    return value


def _selected_failure_ids(configuration: JsonObject) -> list[str]:
    carry = configuration.get("carry_forward")
    selected = carry.get("failed_queue_ids") if isinstance(carry, dict) else None
    if (
        not isinstance(selected, list)
        or not selected
        or any(not isinstance(queue_id, str) for queue_id in selected)
    ):
        raise CohortReviewError("Specialist workspace has no exact failed selection")
    return selected


def _object_field(document: JsonObject, field: str, label: str) -> JsonObject:
    value = document.get(field)
    if not isinstance(value, dict):
        raise CohortReviewError(f"Specialist {label} are invalid")
    return value

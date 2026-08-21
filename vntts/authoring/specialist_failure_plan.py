"""Checksum-bound next actions for terminal specialist repair failures."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from vntts.authoring.bulk_generation import _canonical_sha256
from vntts.authoring.cohort_review import (
    CohortReviewError,
    _load_document,
    _write_document_no_replace,
)
from vntts.authoring.failure_repair import (
    OFFLINE_FALLBACK_BACKEND,
    SENTENCE_BOUNDARY_SEGMENTATION,
)

SPECIALIST_FAILURE_PLAN_SCHEMA = "vntts.authoring-specialist-failure-plan"
SPECIALIST_FAILURE_PLAN_VERSION = 1
REFERENCE_OR_LIVE = "reference_comparison_or_live_fallback"


@dataclass(frozen=True)
class SpecialistFailurePlan:
    plan_id: str
    document: dict

    def to_dict(self):
        return dict(self.document)


def build_specialist_failure_plan(workspace_directories):
    paths = tuple(
        sorted({Path(value).resolve() for value in workspace_directories}, key=str)
    )
    if not paths:
        raise CohortReviewError("A specialist failure plan requires a workspace")
    sources = []
    items = []
    seen = set()
    for workspace in paths:
        configuration_path = workspace / "workspace.json"
        state_path = workspace / "generated-audio/generation-state.json"
        queue_path = workspace / "queue.jsonl"
        configuration_payload = _read(configuration_path, "workspace configuration")
        state_payload = _read(state_path, "generation state")
        queue_payload = _read(queue_path, "generation queue")
        configuration = _decode(configuration_payload, "workspace configuration")
        state = _decode(state_payload, "generation state")
        queue = _queue_records(queue_payload)
        carry = (
            configuration.get("carry_forward")
            if isinstance(configuration, dict)
            else None
        )
        selected = carry.get("failed_queue_ids") if isinstance(carry, dict) else None
        if not isinstance(selected, list) or not selected:
            raise CohortReviewError(
                "Specialist workspace has no exact failed selection"
            )
        source_item_count = 0
        for queue_id in selected:
            result = (
                state.get("items", {}).get(queue_id)
                if isinstance(state, dict)
                else None
            )
            if not isinstance(result, dict) or result.get("status") != "failed":
                continue
            if queue_id in seen:
                raise CohortReviewError(f"Specialist failure is duplicated: {queue_id}")
            seen.add(queue_id)
            record = queue.get(queue_id)
            if not isinstance(record, dict):
                raise CohortReviewError(
                    f"Specialist queue item disappeared: {queue_id}"
                )
            failure = result.get("failure")
            repair = result.get("failure_repair")
            if not isinstance(failure, dict) or not isinstance(repair, dict):
                raise CohortReviewError(
                    f"Specialist failure evidence is incomplete: {queue_id}"
                )
            action, rationale = _next_action(result, repair, failure)
            text = str(record.get("text") or "")
            text_features = failure.get("text_features")
            text_features = text_features if isinstance(text_features, dict) else {}
            item = {
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
                "result_sha256": _canonical_sha256(result),
                "text_shape": {
                    "sentence_boundary_count": text_features.get(
                        "sentence_boundary_count"
                    ),
                    "word_count": text_features.get("word_count"),
                    "has_ellipsis": bool(text_features.get("ellipsis_count")),
                },
                "next_action": action,
                "rationale": rationale,
            }
            item["cluster_key"] = _canonical_sha256(
                {
                    key: item[key]
                    for key in (
                        "provider",
                        "model",
                        "generation_profile",
                        "effective_voice",
                        "repair_strategy",
                        "text_shape",
                        "next_action",
                    )
                }
                | {
                    "failure_kind": failure.get("kind"),
                    "completion": failure.get("completion"),
                    "error_type": failure.get("error_type"),
                }
            )
            items.append(item)
            source_item_count += 1
        sources.append(
            {
                "workspace": str(workspace),
                "workspace_id": configuration.get("workspace_id"),
                "config_fingerprint": configuration.get("config_fingerprint"),
                "state_sha256": hashlib.sha256(state_payload).hexdigest(),
                "queue_sha256": hashlib.sha256(queue_payload).hexdigest(),
                "failed_item_count": source_item_count,
            }
        )
        for path, payload, label in (
            (configuration_path, configuration_payload, "workspace configuration"),
            (state_path, state_payload, "generation state"),
            (queue_path, queue_payload, "generation queue"),
        ):
            if _read(path, label) != payload:
                raise CohortReviewError(f"Specialist {label} changed during planning")
    items.sort(key=lambda value: value["queue_id"])
    grouped = defaultdict(list)
    for item in items:
        grouped[item["cluster_key"]].append(item["queue_id"])
    clusters = [
        {
            "cluster_key": key,
            "item_count": len(queue_ids),
            "queue_ids": sorted(queue_ids),
            "next_action": next(
                item["next_action"] for item in items if item["cluster_key"] == key
            ),
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
            for action in (OFFLINE_FALLBACK_BACKEND, REFERENCE_OR_LIVE)
        },
        "sources": sources,
        "clusters": clusters,
        "items": items,
    }
    plan_id = _canonical_sha256(body)
    return SpecialistFailurePlan(plan_id, {**body, "plan_id": plan_id})


def write_specialist_failure_plan(plan, output_path):
    document = _validated(plan)
    return _write_document_no_replace(output_path, document, "specialist failure plan")


def load_specialist_failure_plan(path):
    document = _validated(_load_document(path, "specialist failure plan"))
    return SpecialistFailurePlan(document["plan_id"], document)


def _next_action(result, repair, failure):
    strategy = repair.get("strategy")
    providers = result.get("attempts_by_provider")
    providers = providers if isinstance(providers, dict) else {}
    if strategy == SENTENCE_BOUNDARY_SEGMENTATION and not providers.get("pocket-tts"):
        return (
            OFFLINE_FALLBACK_BACKEND,
            "Sentence repair is terminal under MOSS; one unseeded Pocket attempt remains bounded",
        )
    if strategy == OFFLINE_FALLBACK_BACKEND:
        return (
            REFERENCE_OR_LIVE,
            "Pocket fallback is already terminal; compare a verified reference or retain live fallback",
        )
    raise CohortReviewError(
        "Specialist failure has no evidence-backed next action: "
        f"{strategy!r}/{failure.get('kind')!r}"
    )


def _validated(plan):
    document = plan.document if isinstance(plan, SpecialistFailurePlan) else plan
    if (
        not isinstance(document, dict)
        or document.get("schema") != SPECIALIST_FAILURE_PLAN_SCHEMA
    ):
        raise CohortReviewError("Unsupported specialist failure plan schema")
    if document.get("schema_version") != SPECIALIST_FAILURE_PLAN_VERSION:
        raise CohortReviewError("Unsupported specialist failure plan version")
    claimed = document.get("plan_id")
    actual = _canonical_sha256(
        {key: value for key, value in document.items() if key != "plan_id"}
    )
    if claimed != actual:
        raise CohortReviewError("Specialist failure plan identity changed")
    if document.get("item_count") != len(document.get("items", [])):
        raise CohortReviewError("Specialist failure plan item count is invalid")
    if sum(document.get("action_counts", {}).values()) != document["item_count"]:
        raise CohortReviewError("Specialist failure plan action counts are invalid")
    return document


def _read(path, label):
    try:
        return Path(path).read_bytes()
    except OSError as error:
        raise CohortReviewError(
            f"Unable to read specialist {label}: {error}"
        ) from error


def _decode(payload, label):
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(
            f"Unable to decode specialist {label}: {error}"
        ) from error


def _queue_records(payload):
    try:
        rows = [json.loads(value) for value in payload.decode("utf-8").splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(
            f"Unable to decode specialist queue: {error}"
        ) from error
    return {
        value["queue_id"]: value
        for value in rows[1:]
        if isinstance(value, dict) and isinstance(value.get("queue_id"), str)
    }

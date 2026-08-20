"""Exact read-only disposition plans for provenance-unbound pending WAVs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from vntts.authoring.cohort_review import (
    CohortReviewError,
    build_cohort_review_plan,
)
from vntts.authoring.workbench import AuthoringWorkbenchError, list_review_items

PENDING_RESOLUTION_PLAN_SCHEMA = "vntts.authoring-pending-resolution-plan"
PENDING_RESOLUTION_PLAN_VERSION = 1
RECOVER_OR_REGENERATE = "provenance_recovery_or_regeneration"


class PendingResolutionError(CohortReviewError):
    """Pending review outcomes cannot be dispositioned from exact authority."""


@dataclass(frozen=True)
class PendingResolutionPlan:
    """One immutable read-only plan for outcomes excluded from cohort review."""

    plan_id: str
    document: dict

    def to_dict(self):
        return dict(self.document)


def build_pending_resolution_plan(workspace_directory):
    """Bind every cohort-blocked pending WAV to a conservative next action."""
    cohort_plan = build_cohort_review_plan(workspace_directory)
    try:
        projected = list_review_items(workspace_directory)
    except AuthoringWorkbenchError as error:
        raise PendingResolutionError(str(error)) from error
    by_queue_id = {item.queue_id: item for item in projected}
    records = []
    seen = set()
    for blocked in cohort_plan.document["blocked_items"]:
        queue_id = blocked["queue_id"]
        if queue_id in seen:
            raise PendingResolutionError(
                f"Pending resolution queue ID is duplicated: {queue_id!r}"
            )
        seen.add(queue_id)
        item = by_queue_id.get(queue_id)
        if item is None:
            raise PendingResolutionError(
                f"Pending review item disappeared while planning: {queue_id!r}"
            )
        authority = item.authority
        if (
            item.status != "generated"
            or item.review_status != "pending_review"
            or authority is None
        ):
            raise PendingResolutionError(
                f"Blocked item is no longer pending exact-WAV review: {queue_id!r}"
            )
        if authority.state_sha256 != cohort_plan.document["state_sha256"]:
            raise PendingResolutionError(
                "Generation state changed while pending resolution was being planned"
            )
        if authority.queue_sha256 != cohort_plan.document["queue_sha256"]:
            raise PendingResolutionError(
                "Generation queue changed while pending resolution was being planned"
            )
        records.append(
            {
                "queue_id": queue_id,
                "line_id": item.line_id,
                "text_sha256": hashlib.sha256(item.text.encode("utf-8")).hexdigest(),
                "item_sha256": authority.item_sha256,
                "audio_sha256": authority.audio_sha256,
                "blocker": blocked["reason"],
                "action": RECOVER_OR_REGENERATE,
            }
        )
    records.sort(key=lambda value: value["queue_id"])
    body = {
        "schema": PENDING_RESOLUTION_PLAN_SCHEMA,
        "schema_version": PENDING_RESOLUTION_PLAN_VERSION,
        "workspace_id": cohort_plan.document["workspace_id"],
        "workspace_config_fingerprint": cohort_plan.document[
            "workspace_config_fingerprint"
        ],
        "queue_sha256": cohort_plan.document["queue_sha256"],
        "state_sha256": cohort_plan.document["state_sha256"],
        "blocked_pending_count": len(records),
        "action_counts": ({RECOVER_OR_REGENERATE: len(records)} if records else {}),
        "records": records,
    }
    plan_id = _canonical_sha256(body)
    document = {**body, "plan_id": plan_id}
    return PendingResolutionPlan(plan_id, document)


def _canonical_sha256(document):
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

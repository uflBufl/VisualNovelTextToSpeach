"""Exact read-only disposition plans for provenance-unbound pending WAVs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

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


def write_pending_resolution_plan(plan, output_path):
    """Publish one validated plan atomically without replacing another file."""
    document = _validated_plan_document(plan)
    path = Path(output_path).expanduser().resolve()
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as error:
        raise PendingResolutionError(
            f"Pending resolution plan output exists: {path}"
        ) from error
    except OSError as error:
        raise PendingResolutionError(
            f"Unable to publish pending resolution plan {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def load_pending_resolution_plan(path):
    """Load and fully validate one immutable pending-resolution plan."""
    path = Path(path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PendingResolutionError(
            f"Unable to read pending resolution plan {path}: {error}"
        ) from error
    validated = _validated_plan_document(document)
    return PendingResolutionPlan(validated["plan_id"], validated)


def _validated_plan_document(plan):
    document = plan.document if isinstance(plan, PendingResolutionPlan) else plan
    if not isinstance(document, dict):
        raise PendingResolutionError("Pending resolution plan must be an object")
    required = {
        "schema",
        "schema_version",
        "workspace_id",
        "workspace_config_fingerprint",
        "queue_sha256",
        "state_sha256",
        "blocked_pending_count",
        "action_counts",
        "records",
        "plan_id",
    }
    if set(document) != required:
        raise PendingResolutionError("Pending resolution plan fields are invalid")
    if document.get("schema") != PENDING_RESOLUTION_PLAN_SCHEMA:
        raise PendingResolutionError("Pending resolution plan schema is unsupported")
    if document.get("schema_version") != PENDING_RESOLUTION_PLAN_VERSION:
        raise PendingResolutionError("Pending resolution plan version is unsupported")
    _required_text(document.get("workspace_id"), "Workspace ID")
    for field, label in (
        ("workspace_config_fingerprint", "Workspace config fingerprint"),
        ("queue_sha256", "Queue SHA-256"),
        ("state_sha256", "State SHA-256"),
        ("plan_id", "Plan ID"),
    ):
        _required_sha256(document.get(field), label)
    records = document.get("records")
    if not isinstance(records, list):
        raise PendingResolutionError("Pending resolution records must be a list")
    canonical = [_validated_record(value) for value in records]
    queue_ids = [value["queue_id"] for value in canonical]
    if queue_ids != sorted(queue_ids) or len(set(queue_ids)) != len(queue_ids):
        raise PendingResolutionError(
            "Pending resolution queue IDs must be unique and sorted"
        )
    if document.get("blocked_pending_count") != len(canonical):
        raise PendingResolutionError("Pending resolution count is inconsistent")
    expected_counts = {RECOVER_OR_REGENERATE: len(canonical)} if canonical else {}
    if document.get("action_counts") != expected_counts:
        raise PendingResolutionError("Pending resolution action counts are invalid")
    actual_id = _canonical_sha256(
        {key: value for key, value in document.items() if key != "plan_id"}
    )
    if actual_id != document["plan_id"]:
        raise PendingResolutionError("Pending resolution plan identity is invalid")
    return document


def _validated_record(record):
    if not isinstance(record, dict) or set(record) != {
        "queue_id",
        "line_id",
        "text_sha256",
        "item_sha256",
        "audio_sha256",
        "blocker",
        "action",
    }:
        raise PendingResolutionError("Pending resolution record fields are invalid")
    for field, label in (("queue_id", "Queue ID"), ("line_id", "Line ID")):
        _required_text(record.get(field), label)
    for field, label in (
        ("text_sha256", "Text SHA-256"),
        ("item_sha256", "Item SHA-256"),
        ("audio_sha256", "Audio SHA-256"),
    ):
        _required_sha256(record.get(field), label)
    _required_text(record.get("blocker"), "Pending resolution blocker")
    if record.get("action") != RECOVER_OR_REGENERATE:
        raise PendingResolutionError("Pending resolution action is unsupported")
    return record


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise PendingResolutionError(f"{label} must be non-empty text")
    return value


def _required_sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PendingResolutionError(f"{label} must be lowercase SHA-256")
    return value


def _canonical_sha256(document):
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

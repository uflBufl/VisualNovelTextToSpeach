"""Immutable exact-ID plans for provenance-unbound failed generations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from vntts.authoring.authority import (
    canonical_document_sha256,
    write_json_document_no_replace,
)
from vntts.authoring.bulk_generation import (
    generation_failure_repair_plan,
    load_generation_state,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    generation_command,
    load_workspace_authority,
)
from vntts.document_identity import is_lowercase_sha256

FAILURE_REGENERATION_PLAN_SCHEMA = "vntts.authoring-failure-regeneration-plan"
FAILURE_REGENERATION_PLAN_VERSION = 1
REGENERATE_UNBOUND_FAILURE = "provenance_recovery_or_regeneration"


class FailureRegenerationRecord(TypedDict):
    queue_id: str
    line_id: str
    item_sha256: str
    failure_kind: str
    attempts: int
    seed: int
    action: str


class FailureRegenerationDocument(TypedDict):
    schema: str
    schema_version: int
    workspace_id: str
    workspace_config_fingerprint: str
    queue_sha256: str
    state_sha256: str
    failure_count: int
    records: list[FailureRegenerationRecord]
    plan_id: str


class FailureRegenerationError(RuntimeError):
    """A legacy-failure regeneration plan lost its exact authority."""


@dataclass(frozen=True)
class FailureRegenerationPlan:
    plan_id: str
    document: FailureRegenerationDocument

    def to_dict(self) -> dict[str, object]:
        return dict(self.document)


@dataclass(frozen=True)
class FailureRegenerationCommand:
    batch_id: str
    batch_index: int
    batch_count: int
    queue_ids: tuple[str, ...]
    command: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "batch_index": self.batch_index,
            "batch_count": self.batch_count,
            "queue_ids": list(self.queue_ids),
            "command": list(self.command),
        }


def build_failure_regeneration_plan(
    workspace_directory: str | Path,
) -> FailureRegenerationPlan:
    """Bind every current provenance-unbound failure without changing state."""
    try:
        directory, workspace, _workspace_sha256 = load_workspace_authority(
            workspace_directory
        )
        queue_path = directory / "queue.jsonl"
        state_path = directory / "generated-audio/generation-state.json"
        repair = generation_failure_repair_plan(state_path, queue_path)
        state = load_generation_state(state_path, queue_path)
    except (AuthoringWorkbenchError, OSError, ValueError) as error:
        raise FailureRegenerationError(str(error)) from error
    records: list[FailureRegenerationRecord] = []
    state_items = _object_field(state, "items", "Generation state items")
    for planned in _object_list(repair.get("records"), "Failure repair records"):
        if planned.get("action") != REGENERATE_UNBOUND_FAILURE:
            continue
        queue_id = _required_text(planned.get("queue_id"), "Queue ID")
        item = state_items.get(queue_id)
        if not isinstance(item, dict) or item.get("status") != "failed":
            raise FailureRegenerationError(
                f"Failure regeneration item changed while planning: {queue_id!r}"
            )
        records.append(
            {
                "queue_id": queue_id,
                "line_id": _required_text(planned.get("line_id"), "Line ID"),
                "item_sha256": canonical_document_sha256(item),
                "failure_kind": _required_text(
                    planned.get("failure_kind"), "Failure kind"
                ),
                "attempts": _required_int(
                    planned.get("attempts"), "Failure attempts", minimum=0
                ),
                "seed": _required_int(planned.get("seed"), "Failure seed"),
                "action": REGENERATE_UNBOUND_FAILURE,
            }
        )
    records.sort(key=lambda value: value["queue_id"])
    body: dict[str, object] = {
        "schema": FAILURE_REGENERATION_PLAN_SCHEMA,
        "schema_version": FAILURE_REGENERATION_PLAN_VERSION,
        "workspace_id": workspace["workspace_id"],
        "workspace_config_fingerprint": workspace["config_fingerprint"],
        "queue_sha256": repair["queue_sha256"],
        "state_sha256": repair["state_sha256"],
        "failure_count": len(records),
        "records": records,
    }
    plan_id = canonical_document_sha256(body)
    return FailureRegenerationPlan(
        plan_id,
        _validated_plan_document({**body, "plan_id": plan_id}),
    )


def build_failure_regeneration_command(
    workspace_directory: str | Path,
    plan: FailureRegenerationPlan | object,
    *,
    batch_index: int,
    batch_size: int = 10,
) -> FailureRegenerationCommand:
    """Return one bounded exact-ID argv if the full plan is still current."""
    document = _validated_plan_document(plan)
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or not 1 <= batch_size <= 25
    ):
        raise FailureRegenerationError("Failure batch size must be 1 to 25")
    if (
        not isinstance(batch_index, int)
        or isinstance(batch_index, bool)
        or batch_index < 1
    ):
        raise FailureRegenerationError("Failure batch index must be positive")
    current = build_failure_regeneration_plan(workspace_directory)
    if current.document != document:
        raise FailureRegenerationError(
            "Workspace failure authority changed after the plan was published"
        )
    if not document["records"]:
        raise FailureRegenerationError("Failure regeneration plan has no items")
    batch_count = (len(document["records"]) + batch_size - 1) // batch_size
    if batch_index > batch_count:
        raise FailureRegenerationError(f"Failure batch index exceeds {batch_count}")
    start = (batch_index - 1) * batch_size
    queue_ids = tuple(
        record["queue_id"] for record in document["records"][start : start + batch_size]
    )
    try:
        command = generation_command(
            workspace_directory,
            queue_ids=queue_ids,
            regenerate_existing=True,
            retries=0,
            seed=0,
        )
    except AuthoringWorkbenchError as error:
        raise FailureRegenerationError(str(error)) from error
    identity = {
        "plan_id": document["plan_id"],
        "batch_index": batch_index,
        "batch_size": batch_size,
        "queue_ids": list(queue_ids),
    }
    return FailureRegenerationCommand(
        batch_id=canonical_document_sha256(identity),
        batch_index=batch_index,
        batch_count=batch_count,
        queue_ids=queue_ids,
        command=tuple(command),
    )


def write_failure_regeneration_plan(
    plan: FailureRegenerationPlan | object, output_path: str | Path
) -> Path:
    document = _validated_plan_document(plan)
    return Path(
        write_json_document_no_replace(
            output_path,
            document,
            "failure regeneration plan",
            error_type=FailureRegenerationError,
        )
    )


def load_failure_regeneration_plan(path: str | Path) -> FailureRegenerationPlan:
    path = Path(path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FailureRegenerationError(
            f"Unable to read failure regeneration plan {path}: {error}"
        ) from error
    validated = _validated_plan_document(document)
    return FailureRegenerationPlan(validated["plan_id"], validated)


def _validated_plan_document(
    plan: FailureRegenerationPlan | object,
) -> FailureRegenerationDocument:
    document = plan.document if isinstance(plan, FailureRegenerationPlan) else plan
    required = {
        "schema",
        "schema_version",
        "workspace_id",
        "workspace_config_fingerprint",
        "queue_sha256",
        "state_sha256",
        "failure_count",
        "records",
        "plan_id",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise FailureRegenerationError("Failure regeneration plan fields are invalid")
    if (
        document.get("schema") != FAILURE_REGENERATION_PLAN_SCHEMA
        or document.get("schema_version") != FAILURE_REGENERATION_PLAN_VERSION
    ):
        raise FailureRegenerationError(
            "Failure regeneration plan schema is unsupported"
        )
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
        raise FailureRegenerationError("Failure regeneration records must be a list")
    canonical = [_validated_record(record) for record in records]
    queue_ids = [record["queue_id"] for record in canonical]
    if queue_ids != sorted(queue_ids) or len(queue_ids) != len(set(queue_ids)):
        raise FailureRegenerationError(
            "Failure regeneration queue IDs must be unique and sorted"
        )
    if document.get("failure_count") != len(canonical):
        raise FailureRegenerationError("Failure regeneration count is inconsistent")
    actual = canonical_document_sha256(
        {key: value for key, value in document.items() if key != "plan_id"}
    )
    if actual != document["plan_id"]:
        raise FailureRegenerationError("Failure regeneration plan identity is invalid")
    failure_count = document.get("failure_count")
    if not isinstance(failure_count, int) or isinstance(failure_count, bool):
        raise FailureRegenerationError("Failure regeneration count is invalid")
    return {
        "schema": FAILURE_REGENERATION_PLAN_SCHEMA,
        "schema_version": FAILURE_REGENERATION_PLAN_VERSION,
        "workspace_id": _required_text(document.get("workspace_id"), "Workspace ID"),
        "workspace_config_fingerprint": _required_sha256(
            document.get("workspace_config_fingerprint"),
            "Workspace config fingerprint",
        ),
        "queue_sha256": _required_sha256(document.get("queue_sha256"), "Queue SHA-256"),
        "state_sha256": _required_sha256(document.get("state_sha256"), "State SHA-256"),
        "failure_count": failure_count,
        "records": canonical,
        "plan_id": _required_sha256(document.get("plan_id"), "Plan ID"),
    }


def _validated_record(record: object) -> FailureRegenerationRecord:
    required = {
        "queue_id",
        "line_id",
        "item_sha256",
        "failure_kind",
        "attempts",
        "seed",
        "action",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise FailureRegenerationError("Failure regeneration record is malformed")
    _required_text(record.get("queue_id"), "Queue ID")
    _required_text(record.get("line_id"), "Line ID")
    _required_text(record.get("failure_kind"), "Failure kind")
    _required_sha256(record.get("item_sha256"), "Item SHA-256")
    attempts = record.get("attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise FailureRegenerationError("Failure attempts must be non-negative")
    seed = record.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise FailureRegenerationError("Failure seed must be an integer")
    if record.get("action") != REGENERATE_UNBOUND_FAILURE:
        raise FailureRegenerationError("Failure regeneration action is unsupported")
    return {
        "queue_id": _required_text(record.get("queue_id"), "Queue ID"),
        "line_id": _required_text(record.get("line_id"), "Line ID"),
        "item_sha256": _required_sha256(record.get("item_sha256"), "Item SHA-256"),
        "failure_kind": _required_text(record.get("failure_kind"), "Failure kind"),
        "attempts": attempts,
        "seed": seed,
        "action": REGENERATE_UNBOUND_FAILURE,
    }


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise FailureRegenerationError(f"{label} must be non-empty text")
    return value


def _required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not is_lowercase_sha256(value):
        raise FailureRegenerationError(f"{label} must be lowercase SHA-256")
    return value


def _required_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or (minimum is not None and value < minimum)
    ):
        raise FailureRegenerationError(f"{label} is invalid")
    return value


def _object_field(
    document: dict[str, object], field: str, label: str
) -> dict[str, object]:
    value = document.get(field)
    if not isinstance(value, dict):
        raise FailureRegenerationError(f"{label} are invalid")
    return value


def _object_list(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise FailureRegenerationError(f"{label} are invalid")
    return value

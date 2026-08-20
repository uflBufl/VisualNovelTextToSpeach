"""Immutable exact-ID plans for provenance-unbound failed generations."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vntts.authoring.bulk_generation import (
    _canonical_sha256,
    generation_failure_repair_plan,
    load_generation_state,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    _load_workspace,
    generation_command,
)

FAILURE_REGENERATION_PLAN_SCHEMA = "vntts.authoring-failure-regeneration-plan"
FAILURE_REGENERATION_PLAN_VERSION = 1
REGENERATE_UNBOUND_FAILURE = "provenance_recovery_or_regeneration"


class FailureRegenerationError(RuntimeError):
    """A legacy-failure regeneration plan lost its exact authority."""


@dataclass(frozen=True)
class FailureRegenerationPlan:
    plan_id: str
    document: dict

    def to_dict(self):
        return dict(self.document)


@dataclass(frozen=True)
class FailureRegenerationCommand:
    batch_id: str
    batch_index: int
    batch_count: int
    queue_ids: tuple[str, ...]
    command: tuple[str, ...]

    def to_dict(self):
        return {
            "batch_id": self.batch_id,
            "batch_index": self.batch_index,
            "batch_count": self.batch_count,
            "queue_ids": list(self.queue_ids),
            "command": list(self.command),
        }


def build_failure_regeneration_plan(workspace_directory):
    """Bind every current provenance-unbound failure without changing state."""
    try:
        directory, workspace = _load_workspace(workspace_directory)
        queue_path = directory / "queue.jsonl"
        state_path = directory / "generated-audio/generation-state.json"
        repair = generation_failure_repair_plan(state_path, queue_path)
        state = load_generation_state(state_path, queue_path)
    except (AuthoringWorkbenchError, OSError, ValueError) as error:
        raise FailureRegenerationError(str(error)) from error
    records = []
    for planned in repair["records"]:
        if planned["action"] != REGENERATE_UNBOUND_FAILURE:
            continue
        queue_id = planned["queue_id"]
        item = state["items"].get(queue_id)
        if not isinstance(item, dict) or item.get("status") != "failed":
            raise FailureRegenerationError(
                f"Failure regeneration item changed while planning: {queue_id!r}"
            )
        records.append(
            {
                "queue_id": queue_id,
                "line_id": planned["line_id"],
                "item_sha256": _canonical_sha256(item),
                "failure_kind": planned["failure_kind"],
                "attempts": planned["attempts"],
                "seed": planned["seed"],
                "action": REGENERATE_UNBOUND_FAILURE,
            }
        )
    records.sort(key=lambda value: value["queue_id"])
    body = {
        "schema": FAILURE_REGENERATION_PLAN_SCHEMA,
        "schema_version": FAILURE_REGENERATION_PLAN_VERSION,
        "workspace_id": workspace["workspace_id"],
        "workspace_config_fingerprint": workspace["config_fingerprint"],
        "queue_sha256": repair["queue_sha256"],
        "state_sha256": repair["state_sha256"],
        "failure_count": len(records),
        "records": records,
    }
    plan_id = _canonical_sha256(body)
    return FailureRegenerationPlan(plan_id, {**body, "plan_id": plan_id})


def build_failure_regeneration_command(
    workspace_directory,
    plan,
    *,
    batch_index,
    batch_size=10,
):
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
        batch_id=_canonical_sha256(identity),
        batch_index=batch_index,
        batch_count=batch_count,
        queue_ids=queue_ids,
        command=tuple(command),
    )


def write_failure_regeneration_plan(plan, output_path):
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
        raise FailureRegenerationError(
            f"Failure regeneration plan output exists: {path}"
        ) from error
    except OSError as error:
        raise FailureRegenerationError(
            f"Unable to publish failure regeneration plan {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def load_failure_regeneration_plan(path):
    path = Path(path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FailureRegenerationError(
            f"Unable to read failure regeneration plan {path}: {error}"
        ) from error
    validated = _validated_plan_document(document)
    return FailureRegenerationPlan(validated["plan_id"], validated)


def _validated_plan_document(plan):
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
    actual = _canonical_sha256(
        {key: value for key, value in document.items() if key != "plan_id"}
    )
    if actual != document["plan_id"]:
        raise FailureRegenerationError("Failure regeneration plan identity is invalid")
    return document


def _validated_record(record):
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
    return record


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise FailureRegenerationError(f"{label} must be non-empty text")
    return value


def _required_sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FailureRegenerationError(f"{label} must be lowercase SHA-256")
    return value

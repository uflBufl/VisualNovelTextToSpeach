"""Carry exact non-playable failed controls onto an additive workspace config."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    _canonical_sha256,
    load_generation_state,
    process_is_alive,
)
from vntts.authoring.config_rebase import _route_reference_identity
from vntts.authoring.publication import generation_publication_leases
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    _failure_reference_runtime_binding,
    _workspace_queue_voice_overrides,
    _workspace_voice_registry,
    load_workspace_authority,
)

FAILED_CONTROL_CARRY_SCHEMA = "vntts.authoring-failed-control-carry"
FAILED_CONTROL_CARRY_VERSION = 1
FAILED_CONTROL_CARRY_FILENAME = "failed-control-carry.json"


class FailedControlCarryError(RuntimeError):
    """Failed controls cannot be carried without preserving their exact route."""


@dataclass(frozen=True)
class FailedControlCarryResult:
    target_workspace: Path
    report: Path
    created: bool
    carry_id: str
    item_count: int

    def to_dict(self):
        return {
            "target_workspace": str(self.target_workspace),
            "report": str(self.report),
            "created": self.created,
            "carry_id": self.carry_id,
            "item_count": self.item_count,
        }


def carry_failed_controls(source_workspace, target_workspace, queue_ids):
    """Copy exact failed state items when an additive config keeps their route."""
    requested = _queue_ids(queue_ids)
    try:
        source_directory, source, source_workspace_sha256 = load_workspace_authority(
            source_workspace
        )
        target_directory, target, target_workspace_sha256 = load_workspace_authority(
            target_workspace
        )
    except AuthoringWorkbenchError as error:
        raise FailedControlCarryError(str(error)) from error
    if source_directory == target_directory:
        raise FailedControlCarryError(
            "Failed-control carry requires distinct workspaces"
        )
    if source["source"]["import_id"] != target["source"]["import_id"]:
        raise FailedControlCarryError("Failed-control workspaces use different imports")
    source_queue = source_directory / "queue.jsonl"
    target_queue = target_directory / "queue.jsonl"
    try:
        source_queue_payload = source_queue.read_bytes()
        target_queue_payload = target_queue.read_bytes()
    except OSError as error:
        raise FailedControlCarryError(str(error)) from error
    if source_queue_payload != target_queue_payload:
        raise FailedControlCarryError(
            "Failed-control carry queues are not byte-identical"
        )
    queue_sha256 = sha256_file(source_queue)
    source_output = source_directory / "generated-audio"
    target_output = target_directory / "generated-audio"
    report_path = target_output / FAILED_CONTROL_CARRY_FILENAME
    try:
        with generation_publication_leases(
            ((source_output, queue_sha256), (target_output, queue_sha256)),
            process_checker=process_is_alive,
        ):
            source_directory, source, source_workspace_sha256 = (
                load_workspace_authority(source_directory)
            )
            target_directory, target, target_workspace_sha256 = (
                load_workspace_authority(target_directory)
            )
            source_state_path = source_output / "generation-state.json"
            target_state_path = target_output / "generation-state.json"
            source_state = load_generation_state(source_state_path, source_queue)
            target_state = load_generation_state(target_state_path, target_queue)
            if (
                source_state.get("active") is not None
                or target_state.get("active") is not None
            ):
                raise FailedControlCarryError(
                    "Failed-control carry authority has an active attempt"
                )
            queue = VoiceGenerationQueue.load(source_queue)
            queue_by_id = {item.queue_id: item for item in queue.items}
            source_registry = _workspace_voice_registry(source_directory, source)
            target_registry = _workspace_voice_registry(target_directory, target)
            source_overrides = _workspace_queue_voice_overrides(
                source_directory, source
            )
            target_overrides = _workspace_queue_voice_overrides(
                target_directory, target
            )
            source_failure_binding = _failure_reference_runtime_binding(
                source_directory, source
            )
            target_failure_binding = _failure_reference_runtime_binding(
                target_directory, target
            )
            records = []
            proposed = copy.deepcopy(target_state)
            base = copy.deepcopy(target_state)
            for queue_id in requested:
                queue_item = queue_by_id.get(queue_id)
                result = source_state["items"].get(queue_id)
                target_result = target_state["items"].get(queue_id)
                if queue_item is None:
                    raise FailedControlCarryError(
                        f"Failed-control queue ID is absent: {queue_id}"
                    )
                if not isinstance(result, dict) or result.get("status") != "failed":
                    raise FailedControlCarryError(
                        f"Failed-control source item is not failed: {queue_id}"
                    )
                if (
                    result.get("path") is not None
                    or result.get("file_sha256") is not None
                ):
                    raise FailedControlCarryError(
                        f"Failed-control source unexpectedly publishes a WAV: {queue_id}"
                    )
                source_route = _route_reference_identity(
                    source_registry,
                    source,
                    source_overrides,
                    queue_item,
                    result=result,
                    failure_reference_binding=source_failure_binding,
                )
                target_route = _route_reference_identity(
                    target_registry,
                    target,
                    target_overrides,
                    queue_item,
                    source_result=result,
                    failure_reference_binding=target_failure_binding,
                )
                if source_route != target_route:
                    raise FailedControlCarryError(
                        "Failed-control target changes the effective reference for "
                        f"{queue_id!r}"
                    )
                if target_result is not None and target_result != result:
                    raise FailedControlCarryError(
                        f"Failed-control target item is already different: {queue_id}"
                    )
                proposed["items"][queue_id] = copy.deepcopy(result)
                base["items"].pop(queue_id, None)
                records.append(
                    {
                        "queue_id": queue_id,
                        "source_item_sha256": _canonical_sha256(result),
                        "effective_voice_character": source_route[0],
                        "reference_sha256s": list(source_route[1]),
                    }
                )
            body = {
                "schema": FAILED_CONTROL_CARRY_SCHEMA,
                "schema_version": FAILED_CONTROL_CARRY_VERSION,
                "source_workspace_id": source["workspace_id"],
                "source_workspace_sha256": source_workspace_sha256,
                "source_state_sha256": sha256_file(source_state_path),
                "target_workspace_id": target["workspace_id"],
                "target_workspace_sha256": target_workspace_sha256,
                "queue_sha256": queue_sha256,
                "target_base_state_id": _canonical_sha256(base),
                "target_result_state_id": _canonical_sha256(proposed),
                "items": records,
                "authority": (
                    "Exact non-playable failed controls only. This carry adds no "
                    "audio, generation attempt, review decision or voice binding."
                ),
            }
            report = {**body, "carry_id": _canonical_sha256(body)}
            if report_path.is_symlink():
                raise FailedControlCarryError(
                    "Failed-control carry report must not be a symbolic link"
                )
            if report_path.exists():
                _validate_existing_report(report_path, report, proposed)
                return _result(target_directory, report_path, report, created=False)
            if any(
                target_state["items"].get(queue_id) is not None
                and target_state["items"].get(queue_id)
                != source_state["items"][queue_id]
                for queue_id in requested
            ):
                raise FailedControlCarryError(
                    "Failed-control target changed before publication"
                )
            atomic_write_json(target_state_path, proposed, sort_keys=True)
            load_generation_state(target_state_path, target_queue)
            atomic_write_json(report_path, report, sort_keys=True)
            _validate_existing_report(report_path, report, proposed)
            return _result(target_directory, report_path, report, created=True)
    except (
        AuthoringWorkbenchError,
        BulkGenerationError,
        OSError,
        ValueError,
    ) as error:
        raise FailedControlCarryError(str(error)) from error


def _validate_existing_report(report_path, expected, expected_state):
    try:
        observed = json.loads(report_path.read_text(encoding="utf-8"))
        state_path = report_path.parent / "generation-state.json"
        observed_state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FailedControlCarryError(str(error)) from error
    if observed != expected:
        raise FailedControlCarryError("Existing failed-control carry report differs")
    if observed_state != expected_state:
        raise FailedControlCarryError("Carried failed-control state changed")


def _queue_ids(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise FailedControlCarryError("Failed-control queue IDs must be non-empty")
    queue_ids = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise FailedControlCarryError("Failed-control queue ID is invalid")
        queue_ids.append(value.strip())
    if len(queue_ids) != len(set(queue_ids)):
        raise FailedControlCarryError("Failed-control queue IDs must be distinct")
    return tuple(sorted(queue_ids))


def _result(target, report_path, report, *, created):
    return FailedControlCarryResult(
        Path(target).resolve(),
        Path(report_path).resolve(),
        created,
        report["carry_id"],
        len(report["items"]),
    )


__all__ = [
    "FAILED_CONTROL_CARRY_FILENAME",
    "FAILED_CONTROL_CARRY_SCHEMA",
    "FAILED_CONTROL_CARRY_VERSION",
    "FailedControlCarryError",
    "FailedControlCarryResult",
    "carry_failed_controls",
]

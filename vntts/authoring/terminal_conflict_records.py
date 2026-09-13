"""Low-level wire validation for terminal-conflict workspace provenance."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from vntts.authoring.workspace_foundation import contained_regular_file
from vntts.document_identity import is_lowercase_sha256

TERMINAL_CONFLICT_MERGE_SCHEMA = "vntts.authoring-terminal-conflict-workspace-merge"
TERMINAL_CONFLICT_MERGE_VERSION = 1

_ITEM_FIELDS = {
    "source_workspace_id",
    "source_state_sha256",
    "source_item_sha256",
    "audio_sha256",
    "status",
    "review_status",
    "selected_candidate_id",
    "next_action",
}
_LEDGER_ITEM_FIELDS = {"queue_id", *_ITEM_FIELDS}
_WORKSPACE_PATTERN = re.compile(r"resume-[0-9a-f]{24}-[0-9a-f]{16}")


class TerminalConflictItemProvenance(TypedDict):
    """One persisted selected terminal-conflict outcome."""

    source_workspace_id: str
    source_state_sha256: str
    source_item_sha256: str
    audio_sha256: str
    status: str
    review_status: str
    selected_candidate_id: str
    next_action: str


class TerminalConflictWorkspaceLedger(TerminalConflictItemProvenance):
    """One selected outcome bound into a merged workspace."""

    queue_id: str


class TerminalConflictWorkspaceSource(TypedDict):
    """One workspace contributing selected terminal-conflict outcomes."""

    workspace_id: str
    config_fingerprint: str
    state_sha256: str
    terminal_item_count: int


class TerminalConflictRecordError(ValueError):
    """Terminal-conflict item provenance is malformed or unbound."""


def require_terminal_conflict_text(
    value: object,
    label: str,
    *,
    error_type: type[Exception] = TerminalConflictRecordError,
    message: str | None = None,
) -> str:
    """Require canonical non-empty terminal-conflict text."""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise error_type(message or f"{label} must be non-empty text")
    return value


def require_terminal_conflict_sha256(
    value: object,
    label: str,
    *,
    error_type: type[Exception] = TerminalConflictRecordError,
    message: str | None = None,
) -> str:
    """Require one lowercase hexadecimal SHA-256 value."""
    if not isinstance(value, str) or not is_lowercase_sha256(value):
        raise error_type(message or f"{label} must be lowercase SHA-256")
    return value


def require_terminal_conflict_timestamp(
    value: object,
    label: str,
    *,
    error_type: type[Exception] = TerminalConflictRecordError,
) -> datetime:
    """Require one timezone-aware ISO timestamp."""
    try:
        parsed = datetime.fromisoformat(
            require_terminal_conflict_text(value, label, error_type=error_type)
        )
    except ValueError as error:
        raise error_type(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise error_type(f"{label} requires a timezone")
    return parsed


def require_terminal_conflict_file(
    root: str | Path,
    value: object,
    label: str,
    *,
    error_type: type[Exception] = TerminalConflictRecordError,
) -> Path:
    """Resolve one symlink-free contained terminal-conflict file."""
    return Path(contained_regular_file(root, value, label, error_type=error_type))


def require_terminal_conflict_directory(
    value: str | Path,
    label: str,
    *,
    error_type: type[Exception] = TerminalConflictRecordError,
) -> Path:
    """Resolve one existing non-symlink terminal-conflict directory."""
    argument = Path(value).expanduser()
    if argument.is_symlink():
        raise error_type(f"{label.title()} must not be a symlink")
    root = argument.resolve()
    if not root.is_dir():
        raise error_type(f"{label.title()} is unavailable: {root}")
    return root


def is_terminal_review_outcome(result: object) -> bool:
    """Return whether a state item is approved or explicitly rejected."""
    return isinstance(result, dict) and (
        result.get("status"),
        result.get("review_status"),
    ) in {
        ("approved", "approved"),
        ("generated", "rejected"),
    }


def validate_terminal_conflict_item_provenance(
    value: object,
) -> TerminalConflictItemProvenance:
    """Validate one state-item provenance record without workspace I/O."""
    if not isinstance(value, dict) or set(value) != _ITEM_FIELDS:
        raise TerminalConflictRecordError(
            "Terminal conflict state-item provenance is malformed"
        )
    workspace_id = _text(value.get("source_workspace_id"), "source workspace ID")
    if _WORKSPACE_PATTERN.fullmatch(workspace_id) is None:
        raise TerminalConflictRecordError(
            "Terminal conflict source workspace identity is invalid"
        )
    for field, label in (
        ("source_state_sha256", "source state SHA-256"),
        ("source_item_sha256", "source item SHA-256"),
        ("audio_sha256", "audio SHA-256"),
        ("selected_candidate_id", "selected candidate ID"),
    ):
        _sha256(value.get(field), label)
    if value.get("status") == "approved" and value.get("review_status") == "approved":
        status = "approved"
        review_status = "approved"
        expected_action = "apply_selected_approved_outcome"
    elif (
        value.get("status") == "generated" and value.get("review_status") == "rejected"
    ):
        status = "generated"
        review_status = "rejected"
        expected_action = "retain_explicit_rejection"
    else:
        expected_action = None
    if expected_action is None or value.get("next_action") != expected_action:
        raise TerminalConflictRecordError(
            "Terminal conflict state-item status/action is inconsistent"
        )
    return {
        "source_workspace_id": workspace_id,
        "source_state_sha256": _sha256(
            value.get("source_state_sha256"), "source state SHA-256"
        ),
        "source_item_sha256": _sha256(
            value.get("source_item_sha256"), "source item SHA-256"
        ),
        "audio_sha256": _sha256(value.get("audio_sha256"), "audio SHA-256"),
        "status": status,
        "review_status": review_status,
        "selected_candidate_id": _sha256(
            value.get("selected_candidate_id"), "selected candidate ID"
        ),
        "next_action": _text(value.get("next_action"), "next action"),
    }


def validate_terminal_conflict_state_binding(
    state: Mapping[str, object], merge: object
) -> dict[str, object]:
    """Require an exact merge-ledger record for every marked state item."""
    if (
        not isinstance(merge, dict)
        or merge.get("schema") != TERMINAL_CONFLICT_MERGE_SCHEMA
        or merge.get("schema_version") != TERMINAL_CONFLICT_MERGE_VERSION
        or not isinstance(merge.get("items"), list)
    ):
        raise TerminalConflictRecordError(
            "Terminal conflict workspace merge ledger is missing or malformed"
        )
    items = state.get("items") if isinstance(state, dict) else None
    if not isinstance(items, dict):
        raise TerminalConflictRecordError("Generation state items are malformed")
    expected = {}
    for ledger in merge["items"]:
        if not isinstance(ledger, dict) or set(ledger) != _LEDGER_ITEM_FIELDS:
            raise TerminalConflictRecordError(
                "Terminal conflict workspace item ledger is malformed"
            )
        queue_id = _text(ledger.get("queue_id"), "queue ID")
        if queue_id in expected:
            raise TerminalConflictRecordError(
                "Terminal conflict workspace item ledger is duplicated"
            )
        provenance = {key: ledger[key] for key in _ITEM_FIELDS}
        expected[queue_id] = validate_terminal_conflict_item_provenance(provenance)
    observed = {
        queue_id: result["terminal_conflict_resolution"]
        for queue_id, result in items.items()
        if isinstance(result, dict) and "terminal_conflict_resolution" in result
    }
    if set(observed) != set(expected):
        raise TerminalConflictRecordError(
            "Terminal conflict state items do not match the workspace ledger"
        )
    for queue_id, provenance in observed.items():
        checked = validate_terminal_conflict_item_provenance(provenance)
        if checked != expected[queue_id]:
            raise TerminalConflictRecordError(
                f"Terminal conflict state-item provenance changed for {queue_id!r}"
            )
    return copy.deepcopy(merge)


def _text(value: object, label: str) -> str:
    return require_terminal_conflict_text(
        value,
        label,
        message=f"Terminal conflict {label} is invalid",
    )


def _sha256(value: object, label: str) -> str:
    return require_terminal_conflict_sha256(
        value,
        label,
        message=f"Terminal conflict {label} is invalid",
    )


__all__ = [
    "TERMINAL_CONFLICT_MERGE_SCHEMA",
    "TERMINAL_CONFLICT_MERGE_VERSION",
    "TerminalConflictItemProvenance",
    "TerminalConflictWorkspaceLedger",
    "TerminalConflictWorkspaceSource",
    "TerminalConflictRecordError",
    "is_terminal_review_outcome",
    "require_terminal_conflict_directory",
    "require_terminal_conflict_file",
    "require_terminal_conflict_sha256",
    "require_terminal_conflict_text",
    "require_terminal_conflict_timestamp",
    "validate_terminal_conflict_item_provenance",
    "validate_terminal_conflict_state_binding",
]

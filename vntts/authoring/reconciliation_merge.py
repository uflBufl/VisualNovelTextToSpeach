"""Apply exact terminal outcomes selected by an immutable reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from vntts.authoring.reconciliation import load_authoring_reconciliation
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    merge_reconciled_workspace_outcomes,
)
from vntts.authoring.workbench_contracts import WorkspaceCreationResult

JsonObject = dict[str, object]


class _ReconciliationSelection(TypedDict):
    report_id: str
    base: dict[str, object]
    sources: dict[Path, dict[str, dict[str, object]]]


def merge_reconciled_terminal_outcomes(
    base_workspace: str | Path,
    reconciliation: str | Path,
    workspaces_root: str | Path | None = None,
) -> WorkspaceCreationResult:
    """Create a successor from only exact terminal sources selected by a report."""
    report = load_authoring_reconciliation(reconciliation).document
    base_path = Path(base_workspace).expanduser().resolve()
    if report["primary_workspace_id"] != base_path.name:
        raise AuthoringWorkbenchError(
            "Reconciliation primary workspace differs from the requested base"
        )
    workspace_by_id = {
        _text(value.get("workspace_id"), "Reconciliation workspace ID"): value
        for value in _objects(report.get("workspaces"), "reconciliation workspaces")
    }
    base_report = workspace_by_id.get(base_path.name)
    if (
        base_report is None
        or Path(_text(base_report.get("workspace"), "Reconciliation workspace"))
        .expanduser()
        .resolve()
        != base_path
    ):
        raise AuthoringWorkbenchError(
            "Reconciliation primary workspace path differs from the requested base"
        )
    selected: dict[Path, dict[str, dict[str, object]]] = {}
    for action in _objects(report.get("actions"), "reconciliation actions"):
        if (
            action.get("action") != "terminal_merge_required"
            or action.get("workspace_id") != base_path.name
        ):
            continue
        source = _object(action.get("terminal_source"), "terminal source")
        if source["authority"] == "explicit_fallback":
            raise AuthoringWorkbenchError(
                "Reconciled explicit fallback requires a dedicated fallback merge"
            )
        source_report = workspace_by_id[
            _text(source.get("workspace_id"), "Terminal source workspace ID")
        ]
        source_path = Path(
            _text(source_report.get("workspace"), "Terminal source workspace")
        ).resolve()
        queue_id = _text(action.get("queue_id"), "Reconciliation queue ID")
        selected.setdefault(source_path, {})[queue_id] = {
            "action": action,
            "source": source,
            "workspace": source_report,
        }
    if not selected:
        raise AuthoringWorkbenchError(
            "Reconciliation has no exact terminal outcomes for its primary workspace"
        )
    selection: _ReconciliationSelection = {
        "report_id": _text(report.get("report_id"), "Reconciliation report ID"),
        "base": base_report,
        "sources": selected,
    }
    return merge_reconciled_workspace_outcomes(
        base_path,
        tuple(sorted(selected, key=str)),
        selection,
        workspaces_root,
    )


def _object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise AuthoringWorkbenchError(f"{label.capitalize()} is malformed")
    return value


def _objects(value: object, label: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AuthoringWorkbenchError(f"{label.capitalize()} are malformed")
    return [item for item in value if isinstance(item, dict)]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AuthoringWorkbenchError(f"{label} is malformed")
    return value


__all__ = ["merge_reconciled_terminal_outcomes"]

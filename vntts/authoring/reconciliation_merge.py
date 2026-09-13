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
    workspace_by_id = {value["workspace_id"]: value for value in report["workspaces"]}
    base_report = workspace_by_id.get(base_path.name)
    if base_report is None or Path(base_report["workspace"]).resolve() != base_path:
        raise AuthoringWorkbenchError(
            "Reconciliation primary workspace path differs from the requested base"
        )
    selected: dict[Path, dict[str, dict[str, object]]] = {}
    for action in report["actions"]:
        if (
            action.get("action") != "terminal_merge_required"
            or action.get("workspace_id") != base_path.name
        ):
            continue
        source = action["terminal_source"]
        if source["authority"] == "explicit_fallback":
            raise AuthoringWorkbenchError(
                "Reconciled explicit fallback requires a dedicated fallback merge"
            )
        source_report = workspace_by_id[source["workspace_id"]]
        source_path = Path(source_report["workspace"]).resolve()
        selected.setdefault(source_path, {})[action["queue_id"]] = {
            "action": action,
            "source": source,
            "workspace": source_report,
        }
    if not selected:
        raise AuthoringWorkbenchError(
            "Reconciliation has no exact terminal outcomes for its primary workspace"
        )
    selection: _ReconciliationSelection = {
        "report_id": report["report_id"],
        "base": base_report,
        "sources": selected,
    }
    return merge_reconciled_workspace_outcomes(
        base_path,
        tuple(sorted(selected, key=str)),
        selection,
        workspaces_root,
    )


__all__ = ["merge_reconciled_terminal_outcomes"]

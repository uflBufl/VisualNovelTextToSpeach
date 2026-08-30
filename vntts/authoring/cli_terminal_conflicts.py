"""Terminal-conflict resolution command family for the authoring CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.terminal_conflict_resolution import (
    TerminalConflictResolutionError as _TerminalConflictResolutionError,
)
from vntts.authoring.terminal_conflict_resolution import (
    publish_terminal_conflict_resolution,
)
from vntts.authoring.terminal_conflict_review import (
    TerminalConflictReviewError as _TerminalConflictReviewError,
)
from vntts.authoring.terminal_conflict_review import (
    carry_approved_cohort_terminal_conflict_decisions,
    carry_terminal_conflict_decisions,
)
from vntts.authoring.terminal_conflict_successor import (
    TerminalConflictSuccessorError as _TerminalConflictSuccessorError,
)
from vntts.authoring.terminal_conflict_successor import (
    publish_terminal_conflict_successor,
)
from vntts.authoring.terminal_conflict_workspace import (
    merge_terminal_conflict_resolution,
)
from vntts.authoring.workbench import default_workspaces_root

TerminalConflictResolutionError = _TerminalConflictResolutionError
TerminalConflictReviewError = _TerminalConflictReviewError
TerminalConflictSuccessorError = _TerminalConflictSuccessorError

COMMANDS = frozenset(
    {
        "terminal-conflict-resolution",
        "terminal-conflict-carry",
        "terminal-conflict-cohort-carry",
        "terminal-conflict-successor",
        "terminal-conflict-merge",
    }
)


def configure_parsers(subparsers) -> None:
    resolution = subparsers.add_parser(
        "terminal-conflict-resolution",
        help="Publish immutable completed terminal-conflict decisions",
    )
    resolution.add_argument("review_directory", type=Path)
    resolution.add_argument("output", type=Path)
    carry = subparsers.add_parser(
        "terminal-conflict-carry",
        help="Carry unchanged completed decisions into a refreshed review",
    )
    carry.add_argument("source_review_directory", type=Path)
    carry.add_argument("target_review_directory", type=Path)
    cohort_carry = subparsers.add_parser(
        "terminal-conflict-cohort-carry",
        help="Carry exact approved cohort decisions into a current conflict review",
    )
    cohort_carry.add_argument("review_directory", type=Path)
    successor = subparsers.add_parser(
        "terminal-conflict-successor",
        help="Publish a resolution-aware reconciliation successor",
    )
    successor.add_argument("reconciliation", type=Path)
    successor.add_argument("resolution_directory", type=Path)
    successor.add_argument("output", type=Path)
    merge = subparsers.add_parser(
        "terminal-conflict-merge",
        help="Create a config-addressed workspace from terminal decisions",
    )
    merge.add_argument("base_workspace", type=Path)
    merge.add_argument("successor_directory", type=Path)
    merge.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "terminal-conflict-resolution":
        result = publish_terminal_conflict_resolution(
            arguments.review_directory, arguments.output
        )
        payload = result.to_dict()
    elif arguments.command == "terminal-conflict-carry":
        payload = carry_terminal_conflict_decisions(
            arguments.source_review_directory,
            arguments.target_review_directory,
        )
    elif arguments.command == "terminal-conflict-cohort-carry":
        payload = carry_approved_cohort_terminal_conflict_decisions(
            arguments.review_directory
        )
    elif arguments.command == "terminal-conflict-successor":
        result = publish_terminal_conflict_successor(
            arguments.reconciliation,
            arguments.resolution_directory,
            arguments.output,
        )
        payload = result.to_dict()
    elif arguments.command == "terminal-conflict-merge":
        result = merge_terminal_conflict_resolution(
            arguments.base_workspace,
            arguments.successor_directory,
            arguments.workspaces_root,
        )
        payload = {"directory": str(result.directory), "created": result.created}
    else:
        raise ValueError(
            f"Unsupported terminal-conflict command: {arguments.command!r}"
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0

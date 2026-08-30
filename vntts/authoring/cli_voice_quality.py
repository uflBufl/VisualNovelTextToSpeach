"""Reusable voice quality and repair commands for the authoring CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.cohort_review import (
    load_cohort_review_decision,
    load_cohort_review_plan,
)
from vntts.authoring.voice_quality_gate import (
    VoiceQualityGateError as _VoiceQualityGateError,
)
from vntts.authoring.voice_quality_gate import (
    build_voice_quality_gate,
    inspect_voice_quality_gate,
    load_voice_quality_gate,
    write_voice_quality_gate,
)
from vntts.authoring.voice_repair_comparison import (
    VoiceRepairComparisonError as _VoiceRepairComparisonError,
)
from vntts.authoring.voice_repair_comparison import (
    build_voice_repair_candidate_command,
    build_voice_repair_comparison_plan,
    load_voice_repair_comparison_plan,
    prepare_voice_repair_candidate_workspace,
    write_voice_repair_comparison_plan,
)
from vntts.authoring.workbench import default_workspaces_root

VoiceQualityGateError = _VoiceQualityGateError
VoiceRepairComparisonError = _VoiceRepairComparisonError

COMMANDS = frozenset(
    {
        "voice-quality-gate",
        "voice-quality-check",
        "voice-repair-comparison-plan",
        "voice-repair-candidate-workspace",
        "voice-repair-candidate-command",
    }
)


def configure_parsers(subparsers) -> None:
    gate = subparsers.add_parser(
        "voice-quality-gate",
        help="Publish a reusable accepted voice-control quality gate",
    )
    gate.add_argument("workspace", type=Path)
    gate.add_argument("plan", type=Path)
    gate.add_argument("decision", type=Path)
    gate.add_argument("--output", type=Path, required=True)
    check = subparsers.add_parser(
        "voice-quality-check",
        help="Compare one later pending item with a reusable voice-quality gate",
    )
    check.add_argument("gate", type=Path)
    check.add_argument("workspace", type=Path)
    check.add_argument("queue_id")
    repair = subparsers.add_parser(
        "voice-repair-comparison-plan",
        help="Plan a checksum-bound profile comparison for one unresolved voice",
    )
    repair.add_argument("workspace", type=Path)
    repair.add_argument("character")
    repair.add_argument(
        "--generation-profile",
        action="append",
        dest="generation_profiles",
        help="Bounded profile to compare; repeat for each candidate",
    )
    repair.add_argument("--output", type=Path, required=True)
    workspace = subparsers.add_parser(
        "voice-repair-candidate-workspace",
        help="Create one self-contained candidate workspace from an immutable plan",
    )
    workspace.add_argument("plan", type=Path)
    workspace.add_argument("candidate_id")
    workspace.add_argument("import_directory", type=Path)
    workspace.add_argument("--inputs-root", type=Path, required=True)
    workspace.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    command = subparsers.add_parser(
        "voice-repair-candidate-command",
        help="Validate and print one exact sample-only generation command",
    )
    command.add_argument("plan", type=Path)
    command.add_argument("candidate_id")
    command.add_argument("workspace", type=Path)


def _print_document(value) -> None:
    print(json.dumps(value.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "voice-quality-gate":
        gate = build_voice_quality_gate(
            arguments.workspace,
            load_cohort_review_plan(arguments.plan),
            load_cohort_review_decision(arguments.decision),
        )
        write_voice_quality_gate(gate, arguments.output)
        _print_document(gate)
        return 0
    if arguments.command == "voice-quality-check":
        result = inspect_voice_quality_gate(
            load_voice_quality_gate(arguments.gate),
            arguments.workspace,
            arguments.queue_id,
        )
        _print_document(result)
        return 0
    if arguments.command == "voice-repair-comparison-plan":
        plan = build_voice_repair_comparison_plan(
            arguments.workspace,
            arguments.character,
            generation_profiles=(
                ("stable", "natural")
                if arguments.generation_profiles is None
                else tuple(arguments.generation_profiles)
            ),
        )
        write_voice_repair_comparison_plan(plan, arguments.output)
        _print_document(plan)
        return 0
    if arguments.command == "voice-repair-candidate-workspace":
        result = prepare_voice_repair_candidate_workspace(
            load_voice_repair_comparison_plan(arguments.plan),
            arguments.candidate_id,
            arguments.import_directory,
            arguments.inputs_root,
            arguments.workspaces_root,
        )
        _print_document(result)
        return 0
    if arguments.command == "voice-repair-candidate-command":
        command = build_voice_repair_candidate_command(
            load_voice_repair_comparison_plan(arguments.plan),
            arguments.candidate_id,
            arguments.workspace,
        )
        print(json.dumps({"command": list(command)}, indent=2, sort_keys=True))
        return 0
    raise ValueError(f"Unsupported voice-quality command: {arguments.command!r}")

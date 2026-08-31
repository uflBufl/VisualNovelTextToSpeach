"""Immutable workspace-transition command family for the authoring CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.config_rebase import rebase_workspace_config
from vntts.authoring.experimental_composite_voice import (
    publish_experimental_composite_voice_input,
)
from vntts.authoring.failed_control_carry import carry_failed_controls
from vntts.authoring.failed_prompt_hypothesis import (
    publish_failed_prompt_hypothesis_selection,
)
from vntts.authoring.known_role_live_fallback import (
    create_known_role_live_fallback_workspace,
)
from vntts.authoring.reviewed_rejection_fallback import (
    create_reviewed_rejection_fallback_workspace,
)
from vntts.authoring.reviewed_waveform_publication import (
    create_reviewed_waveform_publication_workspace,
)
from vntts.authoring.workbench import default_workspaces_root

COMMANDS = frozenset(
    {
        "known-role-live-fallback",
        "reviewed-waveform-publication",
        "reviewed-rejection-live-fallback",
        "rebase-workspace-config",
        "experimental-composite-voice-input",
        "carry-failed-controls",
        "failed-prompt-hypothesis-selection",
    }
)


def configure_parsers(subparsers) -> None:
    known_role_fallback = subparsers.add_parser(
        "known-role-live-fallback",
        help="Route exact exhausted lines to a bound known-role Pocket voice",
    )
    known_role_fallback.add_argument("base_workspace", type=Path)
    known_role_fallback.add_argument(
        "--evidence",
        action="append",
        nargs=2,
        metavar=("QUEUE_ID", "FAILED_WORKSPACE"),
        required=True,
    )
    known_role_fallback.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )

    reviewed_waveforms = subparsers.add_parser(
        "reviewed-waveform-publication",
        help="Migrate exact approved WAVs without inventing synthesis controls",
    )
    reviewed_waveforms.add_argument("base_workspace", type=Path)
    reviewed_waveforms.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )

    reviewed_rejections = subparsers.add_parser(
        "reviewed-rejection-live-fallback",
        help="Route exact rejected WAV identities through Pocket live synthesis",
    )
    reviewed_rejections.add_argument("base_workspace", type=Path)
    reviewed_rejections.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )

    config_rebase = subparsers.add_parser(
        "rebase-workspace-config",
        help="Carry exact terminal decisions onto one additive immutable config",
    )
    config_rebase.add_argument("source_workspace", type=Path)
    config_rebase.add_argument("target_workspace", type=Path)
    config_rebase.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )

    experimental_composite = subparsers.add_parser(
        "experimental-composite-voice-input",
        help="Publish a comparison-only exact-bank composite manifest voice",
    )
    experimental_composite.add_argument("source_manifest", type=Path)
    experimental_composite.add_argument("composite_directory", type=Path)
    experimental_composite.add_argument("quality_review", type=Path)
    experimental_composite.add_argument("voice_character")
    experimental_composite.add_argument("output_directory", type=Path)

    failed_control_carry = subparsers.add_parser(
        "carry-failed-controls",
        help="Carry exact non-playable failures onto an additive workspace config",
    )
    failed_control_carry.add_argument("source_workspace", type=Path)
    failed_control_carry.add_argument("target_workspace", type=Path)
    failed_control_carry.add_argument(
        "--queue-id", action="append", required=True, dest="queue_ids"
    )

    failed_prompt_selection = subparsers.add_parser(
        "failed-prompt-hypothesis-selection",
        help="Import a completed prompt comparison without approving speech",
    )
    failed_prompt_selection.add_argument("plan", type=Path)
    failed_prompt_selection.add_argument("session", type=Path)
    failed_prompt_selection.add_argument("output", type=Path)


def _print_workspace(result) -> None:
    print(
        json.dumps(
            {"directory": str(result.directory), "created": result.created},
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "known-role-live-fallback":
        _print_workspace(
            create_known_role_live_fallback_workspace(
                arguments.base_workspace,
                arguments.evidence,
                arguments.workspaces_root,
            )
        )
        return 0
    if arguments.command == "reviewed-waveform-publication":
        _print_workspace(
            create_reviewed_waveform_publication_workspace(
                arguments.base_workspace,
                arguments.workspaces_root,
            )
        )
        return 0
    if arguments.command == "reviewed-rejection-live-fallback":
        _print_workspace(
            create_reviewed_rejection_fallback_workspace(
                arguments.base_workspace,
                arguments.workspaces_root,
            )
        )
        return 0
    if arguments.command == "rebase-workspace-config":
        _print_workspace(
            rebase_workspace_config(
                arguments.source_workspace,
                arguments.target_workspace,
                arguments.workspaces_root,
            )
        )
        return 0
    if arguments.command == "experimental-composite-voice-input":
        result = publish_experimental_composite_voice_input(
            arguments.source_manifest,
            arguments.composite_directory,
            arguments.quality_review,
            arguments.voice_character,
            arguments.output_directory,
        )
        print(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
        return 0
    if arguments.command == "carry-failed-controls":
        result = carry_failed_controls(
            arguments.source_workspace,
            arguments.target_workspace,
            arguments.queue_ids,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "failed-prompt-hypothesis-selection":
        result = publish_failed_prompt_hypothesis_selection(
            arguments.plan, arguments.session, arguments.output
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    raise AssertionError(f"Unhandled workspace-transition command: {arguments.command}")

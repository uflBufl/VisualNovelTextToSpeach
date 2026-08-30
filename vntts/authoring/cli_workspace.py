"""Core immutable-workspace command family for the authoring CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.cli_generation_options import (
    add_failure_repair_arguments,
    add_missing_voice_policy_arguments,
    failure_repair_policy,
    missing_voice_policy,
)
from vntts.authoring.explicit_fallback_merge import merge_explicit_live_fallbacks
from vntts.authoring.reconciliation_merge import merge_reconciled_terminal_outcomes
from vntts.authoring.workbench import (
    create_resume_workspace,
    default_workspaces_root,
    merge_workspace_outcomes,
)

COMMANDS = frozenset(
    {
        "create-workspace",
        "merge-workspace-outcomes",
        "merge-reconciled-outcomes",
        "merge-explicit-fallbacks",
    }
)


def configure_parsers(subparsers) -> None:
    workspace = subparsers.add_parser(
        "create-workspace",
        help="Create an immutable config-addressed resume workspace",
    )
    workspace.add_argument("import_directory", type=Path)
    workspace.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    workspace.add_argument("--story-index", type=Path)
    workspace.add_argument("--voice-manifest", type=Path)
    workspace.add_argument(
        "--generation-queue",
        type=Path,
        help="Use one strict additive queue successor instead of the imported queue",
    )
    workspace.add_argument(
        "--audio-event-spoken-projection",
        action="append",
        dest="audio_event_spoken_projection_queue_ids",
        help=(
            "Pregenerate only the spoken projection of this exact mixed audio-event "
            "queue item; repeat for multiple IDs"
        ),
    )
    workspace.add_argument("--narrator-character")
    workspace.add_argument(
        "--backend", choices=("pocket-tts", "chatterbox-nano", "moss-tts")
    )
    workspace.add_argument("--model")
    workspace.add_argument("--generation-profile")
    workspace.add_argument("--carry-forward-from", type=Path)
    workspace.add_argument(
        "--carry-forward-character", action="append", dest="carry_forward_characters"
    )
    workspace.add_argument(
        "--offline-fallback-authority",
        action="append",
        dest="offline_fallback_authorities",
        type=Path,
        help=(
            "Canonical automatic-unresolved decision authorizing the exact "
            "selected Pocket fallback items; repeat for multiple artifacts"
        ),
    )
    add_missing_voice_policy_arguments(workspace)
    add_failure_repair_arguments(workspace)
    merge = subparsers.add_parser(
        "merge-workspace-outcomes",
        help="Create a successor from exact reviewed repair outcomes",
    )
    merge.add_argument("base_workspace", type=Path)
    merge.add_argument(
        "--source-workspace",
        action="append",
        dest="source_workspaces",
        type=Path,
        required=True,
    )
    merge.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    reconciled_merge = subparsers.add_parser(
        "merge-reconciled-outcomes",
        help="Create a successor from exact terminal outcomes in a reconciliation",
    )
    reconciled_merge.add_argument("base_workspace", type=Path)
    reconciled_merge.add_argument("reconciliation", type=Path)
    reconciled_merge.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    fallback_merge = subparsers.add_parser(
        "merge-explicit-fallbacks",
        help="Compose exact standalone live-fallback decisions into a successor",
    )
    fallback_merge.add_argument("base_workspace", type=Path)
    fallback_merge.add_argument("source_workspace", type=Path)
    fallback_merge.add_argument(
        "--queue-id", action="append", dest="queue_ids", required=True
    )
    fallback_merge.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )


def _print_workspace_result(result, **extra) -> None:
    print(
        json.dumps(
            {
                "directory": str(result.directory),
                "created": result.created,
                **extra,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "create-workspace":
        missing_policy = missing_voice_policy(arguments)
        repair_policy = failure_repair_policy(arguments)
        result = create_resume_workspace(
            arguments.import_directory,
            arguments.workspaces_root,
            story_index=arguments.story_index,
            voice_manifest=arguments.voice_manifest,
            narrator_character=arguments.narrator_character,
            backend=arguments.backend,
            model=arguments.model,
            generation_profile=arguments.generation_profile,
            missing_voice_policy=missing_policy,
            failure_repair_policy=repair_policy,
            carry_forward_from=arguments.carry_forward_from,
            carry_forward_characters=arguments.carry_forward_characters,
            offline_fallback_authorities=arguments.offline_fallback_authorities,
            generation_queue=arguments.generation_queue,
            audio_event_spoken_projection_queue_ids=(
                arguments.audio_event_spoken_projection_queue_ids
            ),
        )
        _print_workspace_result(
            result,
            missing_voice_policy=missing_policy.to_document(),
            failure_repair_policy=repair_policy.to_document(),
        )
        return 0
    if arguments.command == "merge-workspace-outcomes":
        result = merge_workspace_outcomes(
            arguments.base_workspace,
            arguments.source_workspaces,
            arguments.workspaces_root,
        )
    elif arguments.command == "merge-reconciled-outcomes":
        result = merge_reconciled_terminal_outcomes(
            arguments.base_workspace,
            arguments.reconciliation,
            arguments.workspaces_root,
        )
    else:
        result = merge_explicit_live_fallbacks(
            arguments.base_workspace,
            arguments.source_workspace,
            arguments.queue_ids,
            arguments.workspaces_root,
        )
    _print_workspace_result(result)
    return 0

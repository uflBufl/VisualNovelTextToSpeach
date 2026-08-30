"""Audio-event review and workspace command family for the authoring CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.audio_event_composition import (
    AudioEventCompositionError as _AudioEventCompositionError,
)
from vntts.authoring.audio_event_composition import (
    load_audio_event_composition,
    publish_audio_event_composition,
    record_audio_event_composition_decision,
)
from vntts.authoring.audio_event_omission import (
    create_audio_event_omission_workspace,
)
from vntts.authoring.audio_event_projection_fallback import (
    create_audio_event_projection_fallback_workspace,
)
from vntts.authoring.audio_event_review import (
    AudioEventReviewError as _AudioEventReviewError,
)
from vntts.authoring.audio_event_review import (
    load_audio_event_review,
    publish_source_audio_event_review,
    record_audio_event_review_decision,
)
from vntts.authoring.workbench import (
    create_audio_event_composition_workspace,
    default_workspaces_root,
)

AudioEventCompositionError = _AudioEventCompositionError
AudioEventReviewError = _AudioEventReviewError

WORKSPACE_COMMANDS = frozenset(
    {
        "audio-event-omission",
        "audio-event-projection-fallback",
    }
)
REVIEW_COMMANDS = frozenset(
    {
        "audio-event-review-publish",
        "audio-event-review-decide",
        "audio-event-review-status",
        "audio-event-composition-publish",
        "audio-event-composition-decide",
        "audio-event-composition-status",
        "audio-event-composition-workspace",
    }
)
COMMANDS = WORKSPACE_COMMANDS | REVIEW_COMMANDS


def configure_workspace_parsers(subparsers) -> None:
    event_omission = subparsers.add_parser(
        "audio-event-omission",
        help="Omit exact pure events with no validated audio source",
    )
    event_omission.add_argument("base_workspace", type=Path)
    event_omission.add_argument(
        "--queue-id", action="append", dest="queue_ids", required=True
    )
    event_omission.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    event_projection = subparsers.add_parser(
        "audio-event-projection-fallback",
        help="Route only spoken text from exact mixed audio-event lines",
    )
    event_projection.add_argument("base_workspace", type=Path)
    event_projection.add_argument(
        "--queue-id", action="append", dest="queue_ids", required=True
    )
    event_projection.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )


def configure_review_parsers(subparsers) -> None:
    audio_event_publish = subparsers.add_parser(
        "audio-event-review-publish",
        help="Publish one immutable source-backed non-verbal event review",
    )
    audio_event_publish.add_argument("queue", type=Path)
    audio_event_publish.add_argument("queue_id")
    audio_event_publish.add_argument("source_story_index", type=Path)
    audio_event_publish.add_argument("audio", type=Path)
    audio_event_publish.add_argument("--output", type=Path, required=True)
    audio_event_publish.add_argument("--source-line-id", required=True)
    audio_event_publish.add_argument("--source-speaker", required=True)
    audio_event_publish.add_argument("--source-event", required=True)
    audio_event_publish.add_argument("--source-bank", required=True)
    audio_event_publish.add_argument("--source-media-id", type=int, required=True)
    audio_event_publish.add_argument("--source-audio-id", required=True)
    audio_event_decide = subparsers.add_parser(
        "audio-event-review-decide",
        help="Record one terminal accept/reject audio-event decision",
    )
    audio_event_decide.add_argument("directory", type=Path)
    audio_event_decide.add_argument("decision", choices=("accept", "reject"))
    audio_event_status = subparsers.add_parser(
        "audio-event-review-status",
        help="Validate and inspect one audio-event review",
    )
    audio_event_status.add_argument("directory", type=Path)
    audio_event_composition_publish = subparsers.add_parser(
        "audio-event-composition-publish",
        help="Publish one exact accepted event-only production composition",
    )
    audio_event_composition_publish.add_argument("review", type=Path)
    audio_event_composition_publish.add_argument("--output", type=Path, required=True)
    audio_event_composition_decide = subparsers.add_parser(
        "audio-event-composition-decide",
        help="Approve or reject one exact production event composition",
    )
    audio_event_composition_decide.add_argument("directory", type=Path)
    audio_event_composition_decide.add_argument(
        "decision", choices=("approved", "rejected")
    )
    audio_event_composition_status = subparsers.add_parser(
        "audio-event-composition-status",
        help="Validate and inspect one event-only production composition",
    )
    audio_event_composition_status.add_argument("directory", type=Path)
    audio_event_workspace = subparsers.add_parser(
        "audio-event-composition-workspace",
        help="Create a reviewable successor from one approved event composition",
    )
    audio_event_workspace.add_argument("base_workspace", type=Path)
    audio_event_workspace.add_argument("composition", type=Path)
    audio_event_workspace.add_argument("--workspaces-root", type=Path)


def _print_workspace_result(result) -> None:
    print(
        json.dumps(
            {
                "directory": str(result.directory),
                "created": result.created,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "audio-event-omission":
        _print_workspace_result(
            create_audio_event_omission_workspace(
                arguments.base_workspace,
                arguments.queue_ids,
                arguments.workspaces_root,
            )
        )
        return 0
    if arguments.command == "audio-event-projection-fallback":
        _print_workspace_result(
            create_audio_event_projection_fallback_workspace(
                arguments.base_workspace,
                arguments.queue_ids,
                arguments.workspaces_root,
            )
        )
        return 0
    if arguments.command == "audio-event-review-publish":
        result = publish_source_audio_event_review(
            arguments.queue,
            arguments.queue_id,
            arguments.source_story_index,
            arguments.audio,
            arguments.output,
            source_line_id=arguments.source_line_id,
            source_speaker=arguments.source_speaker,
            source_event=arguments.source_event,
            source_bank=arguments.source_bank,
            source_media_id=arguments.source_media_id,
            source_audio_id=arguments.source_audio_id,
        )
    elif arguments.command == "audio-event-review-decide":
        result = record_audio_event_review_decision(
            arguments.directory, arguments.decision
        )
    elif arguments.command == "audio-event-review-status":
        result = load_audio_event_review(arguments.directory)
    elif arguments.command == "audio-event-composition-publish":
        result = publish_audio_event_composition(arguments.review, arguments.output)
    elif arguments.command == "audio-event-composition-decide":
        result = record_audio_event_composition_decision(
            arguments.directory, arguments.decision
        )
    elif arguments.command == "audio-event-composition-status":
        result = load_audio_event_composition(arguments.directory)
    elif arguments.command == "audio-event-composition-workspace":
        result = create_audio_event_composition_workspace(
            arguments.base_workspace,
            arguments.composition,
            arguments.workspaces_root,
        )
        print(
            json.dumps(
                {
                    "created": result.created,
                    "workspace": str(result.directory),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    else:
        raise ValueError(f"Unsupported audio-event command: {arguments.command!r}")
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0

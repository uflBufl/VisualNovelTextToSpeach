"""Generation-queue command family for the authoring CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.delivery import LEGACY_ENGLISH_POLICY, PRESERVE_DELIVERY_POLICY
from vntts.authoring.queue_builder import (
    inspect_generation_queue,
    publish_generation_queue,
)
from vntts.authoring.queue_extension import publish_additive_generation_queue

COMMANDS = frozenset({"preflight-queue", "build-queue", "extend-queue"})


def configure_parsers(subparsers) -> None:
    extension = subparsers.add_parser(
        "extend-queue", help="Publish a strict additive generation-queue successor"
    )
    extension.add_argument("base_queue", type=Path)
    extension.add_argument("extension_queue", type=Path)
    extension.add_argument("--output", type=Path, required=True)
    for command, help_text in (
        ("preflight-queue", "Summarize a collection-driven generation queue"),
        ("build-queue", "Publish a validated collection-driven generation queue"),
    ):
        queue = subparsers.add_parser(command, help=help_text)
        queue.add_argument("--story-index", type=Path, required=True)
        queue.add_argument("--voice-manifest", type=Path, required=True)
        queue.add_argument(
            "--collection",
            action="append",
            dest="collection_ids",
            help="Include one declared collection; repeat to include more",
        )
        queue.add_argument(
            "--unknown-action",
            choices=("resolve_audio", "manual_review"),
            help="Required policy when a selected source-audio status is unknown",
        )
        queue.add_argument(
            "--delivery-policy",
            choices=(PRESERVE_DELIVERY_POLICY, LEGACY_ENGLISH_POLICY),
            default=PRESERVE_DELIVERY_POLICY,
            help="Preserve source annotations or opt into the legacy English heuristic",
        )
        queue.add_argument(
            "--partial-source-audio-only",
            action="store_true",
            help="Include only available cues whose displayed text needs continuation",
        )
        if command == "build-queue":
            queue.add_argument("--output", type=Path, required=True)


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "extend-queue":
        output = publish_additive_generation_queue(
            arguments.base_queue, arguments.extension_queue, arguments.output
        )
        print(json.dumps({"output": str(output)}, indent=2, sort_keys=True))
        return 0
    plan = inspect_generation_queue(
        arguments.story_index,
        arguments.voice_manifest,
        collection_ids=None
        if arguments.collection_ids is None
        else tuple(arguments.collection_ids),
        unknown_action=arguments.unknown_action,
        delivery_policy=arguments.delivery_policy,
        partial_source_audio_only=arguments.partial_source_audio_only,
    )
    payload = {"summary": plan.summary.to_dict()}
    if arguments.command == "build-queue":
        output = publish_generation_queue(plan, arguments.output)
        payload["output"] = str(output)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0

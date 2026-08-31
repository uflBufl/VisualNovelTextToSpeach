"""Final game-pack publication command family."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from vntts.authoring.game_pack import publish_final_game_pack

COMMANDS = frozenset({"publish-pack"})


def _producer_record(value):
    name, separator, producer_version = value.partition("=")
    if not separator or not name.strip() or not producer_version.strip():
        raise argparse.ArgumentTypeError("producer must use NAME=VERSION")
    return {"name": name.strip(), "version": producer_version.strip()}


def _vntts_version():
    try:
        return version("visual-novel-text-to-speech")
    except PackageNotFoundError:
        return "0.1.0"


def configure_parsers(subparsers) -> None:
    pack = subparsers.add_parser(
        "publish-pack", help="Atomically publish a fully verified final game pack"
    )
    pack.add_argument("--state", type=Path, required=True)
    pack.add_argument("--queue", type=Path, required=True)
    pack.add_argument("--story-index", type=Path, required=True)
    pack.add_argument("--voice-manifest", type=Path, required=True)
    pack.add_argument(
        "--live-sequence-plan",
        type=Path,
        help="Exact checksum-bound live sequence plan to ship as a version-2 pack",
    )
    pack.add_argument(
        "--source-audio-semantic-evidence",
        type=Path,
        help="Exact authoring-only semantic evidence bound by the selected story index",
    )
    pack.add_argument(
        "--failure-reference-binding",
        type=Path,
        help="Exact immutable selected-reference binding used by mixed provenance state",
    )
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--game-id")
    pack.add_argument("--game-version", required=True)
    pack.add_argument(
        "--producer",
        action="append",
        type=_producer_record,
        help="Producer identity as NAME=VERSION; repeat for upstream producers",
    )


def handle(arguments: argparse.Namespace) -> int:
    producers = arguments.producer or [
        {"name": "visual-novel-text-to-speech", "version": _vntts_version()}
    ]
    result = publish_final_game_pack(
        arguments.output,
        state_path=arguments.state,
        queue_path=arguments.queue,
        story_index_path=arguments.story_index,
        voice_manifest_path=arguments.voice_manifest,
        live_sequence_plan_path=arguments.live_sequence_plan,
        source_audio_semantic_evidence_path=arguments.source_audio_semantic_evidence,
        failure_reference_binding_path=arguments.failure_reference_binding,
        game_id=arguments.game_id,
        game_version=arguments.game_version,
        producers=producers,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0

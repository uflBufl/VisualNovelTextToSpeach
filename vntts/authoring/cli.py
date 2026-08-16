"""Command-line entry point for offline authoring workflows."""

import argparse
import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts import StoryIndexError, VoiceGenerationQueueError
from vntts_artifacts.voice_manifest import VoiceManifestError, load_voice_manifest

from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    is_spoken_queue_item,
    load_generation_state,
    normalize_short_trailing_ellipsis,
    publish_generated_manifest,
    review_generation_item,
    run_bulk_generation,
    sha256_control_path,
)
from vntts.authoring.game_pack import FinalGamePackError, publish_final_game_pack
from vntts.authoring.legacy_import import (
    LegacyAuthoringImportError,
    default_import_root,
    default_legacy_jobs_root,
    discover_legacy_jobs,
    import_legacy_job,
    import_standalone_generation,
    inspect_standalone_generation,
)
from vntts.authoring.listening_import import (
    ListeningImportError,
    import_listening_session,
    inspect_listening_session,
)
from vntts.authoring.queue_builder import (
    GenerationQueueBuildError,
    inspect_generation_queue,
    publish_generation_queue,
)
from vntts.tts_benchmark import create_backend
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


def _load_stable_voice_registry(manifest_path):
    manifest_path = Path(manifest_path).expanduser().resolve()
    try:
        payload = manifest_path.read_bytes()
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read voice manifest {manifest_path}: {error}"
        ) from error
    digest = hashlib.sha256(payload).hexdigest()
    with TemporaryDirectory() as directory:
        snapshot = Path(directory) / "manifest.json"
        snapshot.write_bytes(payload)
        _document, entries = load_voice_manifest(snapshot)
    voices = [
        CharacterVoice(
            character=entry.character,
            speaker=entry.speaker,
            aliases=entry.aliases,
            references=tuple(
                (manifest_path.parent / reference).resolve()
                for reference in entry.references
            ),
        )
        for entry in entries
    ]
    return CharacterVoiceRegistry(voices), digest


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


def create_parser():
    parser = argparse.ArgumentParser(
        description="VNTTS offline pregeneration authoring"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover = subparsers.add_parser(
        "discover-legacy",
        help="Inspect Reverse: 1999 pregeneration jobs without changing them",
    )
    discover.add_argument("--jobs-root", type=Path, default=default_legacy_jobs_root())
    migrate = subparsers.add_parser(
        "import-legacy",
        help="Validate and non-destructively import one legacy job",
    )
    migrate.add_argument("job_directory", type=Path)
    migrate.add_argument("--destination-root", type=Path, default=default_import_root())
    inspect_standalone = subparsers.add_parser(
        "inspect-standalone",
        help="Validate one explicitly paired standalone queue and output",
    )
    inspect_standalone.add_argument("--queue", type=Path, required=True)
    inspect_standalone.add_argument("--output", type=Path, required=True)
    import_standalone = subparsers.add_parser(
        "import-standalone",
        help="Non-destructively import one explicitly paired queue and output",
    )
    import_standalone.add_argument("--queue", type=Path, required=True)
    import_standalone.add_argument("--output", type=Path, required=True)
    import_standalone.add_argument(
        "--destination-root", type=Path, default=default_import_root()
    )
    inspect_listening = subparsers.add_parser(
        "inspect-listening",
        help="Validate one selected legacy blind-listening session",
    )
    inspect_listening.add_argument("session_directory", type=Path)
    import_listening = subparsers.add_parser(
        "import-listening",
        help="Non-destructively preserve one selected blind-listening session",
    )
    import_listening.add_argument("session_directory", type=Path)
    import_listening.add_argument(
        "--destination-root", type=Path, default=default_import_root()
    )
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
        if command == "build-queue":
            queue.add_argument("--output", type=Path, required=True)
    generate = subparsers.add_parser(
        "generate", help="Resume typed device-independent generation from a queue"
    )
    generate.add_argument("--queue", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--voice-manifest", type=Path, required=True)
    generate.add_argument(
        "--backend",
        required=True,
        choices=("pocket-tts", "chatterbox-nano", "moss-tts"),
    )
    generate.add_argument("--model")
    generate.add_argument(
        "--narrator-character",
        default="Narrator",
        help="Manifest character whose first reference voices queue Narrator lines",
    )
    generate.add_argument("--generation-profile")
    generate.add_argument("--limit", type=int)
    generate.add_argument("--retries", type=int, default=2)
    generate.add_argument("--seed", type=int, default=0)
    generate.add_argument("--include-prefer-source", action="store_true")
    generate.add_argument("--character", action="append", dest="characters")
    review = subparsers.add_parser(
        "review", help="Approve or reject one generated queue item"
    )
    review.add_argument("--state", type=Path, required=True)
    review.add_argument("queue_id")
    review.add_argument("decision", choices=("approved", "rejected"))
    publish = subparsers.add_parser(
        "publish", help="Rebuild the approved-only manifest from generation state"
    )
    publish.add_argument("--state", type=Path, required=True)
    status = subparsers.add_parser("status", help="Inspect resumable generation state")
    status.add_argument("--state", type=Path, required=True)
    status.add_argument("--queue", type=Path)
    pack = subparsers.add_parser(
        "publish-pack", help="Atomically publish a fully verified final game pack"
    )
    pack.add_argument("--state", type=Path, required=True)
    pack.add_argument("--queue", type=Path, required=True)
    pack.add_argument("--story-index", type=Path, required=True)
    pack.add_argument("--voice-manifest", type=Path, required=True)
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--game-id")
    pack.add_argument("--game-version", required=True)
    pack.add_argument(
        "--producer",
        action="append",
        type=_producer_record,
        help="Producer identity as NAME=VERSION; repeat for upstream producers",
    )
    return parser


def main(argv=None):
    arguments = create_parser().parse_args(argv)
    if arguments.command == "discover-legacy":
        candidates = discover_legacy_jobs(arguments.jobs_root)
        print(
            json.dumps(
                [
                    {
                        "job_directory": str(candidate.job_directory),
                        "kind": candidate.kind,
                        "title": candidate.title,
                        "status": candidate.status,
                        "queue_items": candidate.queue_items,
                        "generated_items": candidate.generated_items,
                        "compatible": candidate.compatible,
                        "compatibility_error": candidate.compatibility_error,
                        "diagnostics": list(candidate.diagnostics),
                    }
                    for candidate in candidates
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    try:
        if arguments.command == "generate":
            voice_manifest = arguments.voice_manifest.expanduser().resolve()
            registry, voice_manifest_sha256 = _load_stable_voice_registry(
                voice_manifest
            )
            control_files = {"voice_manifest": (voice_manifest, voice_manifest_sha256)}
            for index, reference in enumerate(
                sorted(
                    {
                        path.resolve()
                        for voice in registry.unique_voices()
                        for path in voice.references
                    },
                    key=str,
                ),
                start=1,
            ):
                control_files[f"voice_reference:{index:04d}"] = (
                    reference,
                    sha256_control_path(reference),
                )
            if arguments.model:
                model_path = Path(arguments.model).expanduser()
                if model_path.exists():
                    model_path = model_path.resolve()
                    control_files["model_artifact"] = (
                        model_path,
                        sha256_control_path(model_path),
                    )
            narrator_voice = registry.resolve(arguments.narrator_character)
            narrator_reference = (
                narrator_voice.references[0]
                if narrator_voice is not None and narrator_voice.references
                else None
            )
            if arguments.backend == "moss-tts" and narrator_reference is None:
                raise BulkGenerationError(
                    f"Narrator voice {arguments.narrator_character!r} has no reference"
                )

            def ready_spoken_item(item):
                if not is_spoken_queue_item(item):
                    return False
                if item.voice_character == "Narrator":
                    return narrator_reference is not None
                voice = registry.resolve(item.voice_character or item.speaker or "")
                return voice is not None and bool(voice.references)

            with TemporaryDirectory() as cache_directory:
                backend = create_backend(
                    arguments.backend,
                    registry,
                    cache_directory,
                    model_name=arguments.model,
                    narrator_reference=narrator_reference,
                )
                try:
                    profile = arguments.generation_profile or getattr(
                        backend, "generation_profile", "stable"
                    )
                    model = arguments.model or str(
                        getattr(backend, "model_name", arguments.backend)
                    )
                    result = run_bulk_generation(
                        arguments.queue,
                        arguments.output,
                        backend,
                        provider=arguments.backend,
                        model=model,
                        generation_profile=profile,
                        limit=arguments.limit,
                        retries=arguments.retries,
                        include_prefer_source=arguments.include_prefer_source,
                        include_characters=arguments.characters,
                        item_filter=ready_spoken_item,
                        seed=arguments.seed,
                        control_files=control_files,
                        text_transform=(
                            normalize_short_trailing_ellipsis
                            if arguments.backend == "moss-tts"
                            else None
                        ),
                        text_transform_id=(
                            "short-trailing-ellipsis-v1"
                            if arguments.backend == "moss-tts"
                            else None
                        ),
                    )
                finally:
                    stop = getattr(backend, "stop", None)
                    if callable(stop):
                        stop()
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "review":
            review_generation_item(
                arguments.state, arguments.queue_id, arguments.decision
            )
            print(
                json.dumps(
                    {
                        "queue_id": arguments.queue_id,
                        "decision": arguments.decision,
                        "state": str(arguments.state.expanduser().resolve()),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "publish":
            manifest = publish_generated_manifest(arguments.state)
            print(json.dumps({"manifest": str(manifest)}, indent=2, sort_keys=True))
            return 0
        if arguments.command == "status":
            state = load_generation_state(arguments.state, arguments.queue)
            counts = {"failed": 0, "generated": 0, "approved": 0}
            for item in state["items"].values():
                counts[item["status"]] += 1
            print(
                json.dumps(
                    {
                        **counts,
                        "active": state.get("active"),
                        "queue_sha256": state["queue_sha256"],
                        "schema": state["schema"],
                        "state": str(arguments.state.expanduser().resolve()),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "publish-pack":
            producers = arguments.producer or [
                {
                    "name": "visual-novel-text-to-speech",
                    "version": _vntts_version(),
                }
            ]
            result = publish_final_game_pack(
                arguments.output,
                state_path=arguments.state,
                queue_path=arguments.queue,
                story_index_path=arguments.story_index,
                voice_manifest_path=arguments.voice_manifest,
                game_id=arguments.game_id,
                game_version=arguments.game_version,
                producers=producers,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command in {"preflight-queue", "build-queue"}:
            plan = inspect_generation_queue(
                arguments.story_index,
                arguments.voice_manifest,
                collection_ids=None
                if arguments.collection_ids is None
                else tuple(arguments.collection_ids),
                unknown_action=arguments.unknown_action,
            )
            payload = {"summary": plan.summary.to_dict()}
            if arguments.command == "build-queue":
                output = publish_generation_queue(plan, arguments.output)
                payload["output"] = str(output)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if arguments.command == "inspect-standalone":
            plan = inspect_standalone_generation(arguments.queue, arguments.output)
            print(json.dumps(plan.summary, indent=2, sort_keys=True))
            return 0
        if arguments.command == "import-standalone":
            result = import_standalone_generation(
                arguments.queue,
                arguments.output,
                arguments.destination_root,
            )
        elif arguments.command == "inspect-listening":
            inspection = inspect_listening_session(arguments.session_directory)
            print(
                json.dumps(
                    {
                        "session_directory": str(inspection.session_directory),
                        "trial_count": inspection.trial_count,
                        "completed_count": inspection.completed_count,
                        "audio_count": inspection.audio_count,
                        "report_present": inspection.report_present,
                        "logical_identity": inspection.logical_identity,
                        "source_fingerprint": inspection.source_fingerprint,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        elif arguments.command == "import-listening":
            result = import_listening_session(
                arguments.session_directory,
                arguments.destination_root,
            )
        else:
            result = import_legacy_job(
                arguments.job_directory,
                arguments.destination_root,
            )
    except (
        GenerationQueueBuildError,
        BulkGenerationError,
        FinalGamePackError,
        LegacyAuthoringImportError,
        ListeningImportError,
        StoryIndexError,
        VoiceGenerationQueueError,
        VoiceManifestError,
    ) as error:
        create_parser().error(str(error))
    print(
        json.dumps(
            {
                "destination": str(result.destination),
                "created": result.created,
                "summary": result.manifest["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0

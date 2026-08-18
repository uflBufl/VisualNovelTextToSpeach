"""Command-line entry point for offline authoring workflows."""

import argparse
import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts import (
    StoryIndexError,
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)
from vntts_artifacts.voice_manifest import VoiceManifestError, load_voice_manifest

from vntts.authoring.bulk_generation import (
    LIVE_FALLBACK_REASONS,
    BulkGenerationError,
    authorize_live_fallback,
    generation_failure_repair_plan,
    generation_failure_report,
    is_spoken_queue_item,
    load_generation_state,
    normalize_short_trailing_ellipsis,
    publish_generated_manifest,
    review_generation_item,
    run_bulk_generation,
    sha256_control_path,
)
from vntts.authoring.delivery import (
    LEGACY_ENGLISH_POLICY,
    PRESERVE_DELIVERY_POLICY,
    DeliveryAnnotationError,
    apply_delivery_policy,
)
from vntts.authoring.failure_repair import (
    FailureRepairPolicy,
    FailureRepairPolicyError,
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
from vntts.authoring.missing_voice_policy import (
    NARRATOR_ALL_UNRESOLVED,
    NARRATOR_ROLES,
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.queue_builder import (
    GenerationQueueBuildError,
    inspect_generation_queue,
    publish_generation_queue,
)
from vntts.authoring.reference_selection import (
    ReferenceSelectionError,
    inspect_voice_reference_candidates,
    select_voice_reference,
)
from vntts.authoring.source_reference_review import (
    SourceReferenceReviewError,
    import_source_reference_review,
    publish_source_reference_evaluation,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    create_resume_workspace,
    default_workspaces_root,
    generation_control_bindings,
    generation_output_identity,
)
from vntts.tts_benchmark import create_backend
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    synthesis_character_for_line,
)


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


def _generation_missing_voice_policy(arguments):
    try:
        if arguments.narrator_fallback_all:
            return MissingVoicePolicy(NARRATOR_ALL_UNRESOLVED)
        if arguments.narrator_fallback_roles:
            return MissingVoicePolicy(
                NARRATOR_ROLES, tuple(arguments.narrator_fallback_roles)
            )
        return MissingVoicePolicy()
    except MissingVoicePolicyError as error:
        raise BulkGenerationError(str(error)) from error


def _add_missing_voice_policy_arguments(parser):
    fallback = parser.add_mutually_exclusive_group()
    fallback.add_argument(
        "--narrator-fallback-role",
        action="append",
        dest="narrator_fallback_roles",
        help=(
            "Use Narrator only when this exact requested role still has no "
            "configured reference; repeat for multiple roles"
        ),
    )
    fallback.add_argument(
        "--narrator-fallback-all",
        action="store_true",
        help="Use Narrator for every still-unresolved named role in this exact queue",
    )


def _generation_failure_repair_policy(arguments):
    try:
        return FailureRepairPolicy(
            tuple(arguments.sentence_segment_failed or ()),
            tuple(arguments.trim_edge_silence_failed or ()),
            arguments.segment_pause_ms,
            tuple(arguments.bounded_seed_failed or ()),
            tuple(arguments.offline_fallback_failed or ()),
        )
    except FailureRepairPolicyError as error:
        raise BulkGenerationError(str(error)) from error


def _add_failure_repair_arguments(parser):
    parser.add_argument(
        "--sentence-segment-failed",
        action="append",
        help="Repair this exact current missed-EOS failure at safe sentence boundaries",
    )
    parser.add_argument(
        "--trim-edge-silence-failed",
        action="append",
        help="Repair this exact current edge-only silence failure before validation",
    )
    parser.add_argument(
        "--bounded-seed-failed",
        action="append",
        help="Retry this exact current missed-EOS failure up to three total attempts",
    )
    parser.add_argument(
        "--offline-fallback-failed",
        action="append",
        help=(
            "Generate this exact carried exhausted backend failure with the "
            "config-addressed Pocket TTS fallback"
        ),
    )
    parser.add_argument(
        "--segment-pause-ms",
        type=int,
        default=180,
        help="Bounded silence inserted only between authorized sentence segments",
    )


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
        queue.add_argument(
            "--delivery-policy",
            choices=(PRESERVE_DELIVERY_POLICY, LEGACY_ENGLISH_POLICY),
            default=PRESERVE_DELIVERY_POLICY,
            help="Preserve source annotations or opt into the legacy English heuristic",
        )
        if command == "build-queue":
            queue.add_argument("--output", type=Path, required=True)
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
    _add_missing_voice_policy_arguments(workspace)
    _add_failure_repair_arguments(workspace)
    generate = subparsers.add_parser(
        "generate", help="Resume typed device-independent generation from a queue"
    )
    generate.add_argument("--queue", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument(
        "--workspace",
        type=Path,
        help="Canonical config-addressed workspace that binds this child run",
    )
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
    _add_missing_voice_policy_arguments(generate)
    _add_failure_repair_arguments(generate)
    generate.add_argument("--limit", type=int)
    generate.add_argument("--retries", type=int, default=2)
    generate.add_argument("--seed", type=int, default=0)
    generate.add_argument("--include-prefer-source", action="store_true")
    generate.add_argument("--character", action="append", dest="characters")
    generate.add_argument(
        "--regenerate-existing",
        action="store_true",
        help=(
            "Re-render existing pending-review outcomes only within an explicit "
            "--character or --queue-id scope; approved/rejected items remain protected"
        ),
    )
    generate.add_argument(
        "--queue-id",
        action="append",
        dest="queue_ids",
        help="Generate only one exact queue ID; repeat for a focused retry",
    )
    review = subparsers.add_parser(
        "review", help="Approve or reject one generated queue item"
    )
    review.add_argument("--state", type=Path, required=True)
    review.add_argument("queue_id")
    review.add_argument("decision", choices=("approved", "rejected"))
    live_fallback = subparsers.add_parser(
        "live-fallback",
        help="Authorize exact terminal Pocket live synthesis for one queue item",
    )
    live_fallback.add_argument("--state", type=Path, required=True)
    live_fallback.add_argument("--queue", type=Path, required=True)
    live_fallback.add_argument("queue_id")
    live_fallback.add_argument(
        "--reason", choices=tuple(sorted(LIVE_FALLBACK_REASONS)), required=True
    )
    live_fallback.add_argument("--provider", default="pocket-tts")
    live_fallback.add_argument("--model", required=True)
    live_fallback.add_argument("--generation-profile", default="default")
    publish = subparsers.add_parser(
        "publish", help="Rebuild the approved-only manifest from generation state"
    )
    publish.add_argument("--state", type=Path, required=True)
    status = subparsers.add_parser("status", help="Inspect resumable generation state")
    status.add_argument("--state", type=Path, required=True)
    status.add_argument("--queue", type=Path)
    failures = subparsers.add_parser(
        "failure-report",
        help="Group failed generation outcomes into stable typed cohorts",
    )
    failures.add_argument("--state", type=Path, required=True)
    failures.add_argument("--queue", type=Path, required=True)
    repairs = subparsers.add_parser(
        "failure-repair-plan",
        help="Plan exact-ID bounded repairs without changing generation state",
    )
    repairs.add_argument("--state", type=Path, required=True)
    repairs.add_argument("--queue", type=Path, required=True)
    references = subparsers.add_parser(
        "reference-report",
        help="Inspect immutable objective metrics for one character's references",
    )
    references.add_argument("--voice-manifest", type=Path, required=True)
    references.add_argument("--character", required=True)
    select_reference = subparsers.add_parser(
        "select-reference",
        help="Publish a no-overwrite manifest with one explicit first reference",
    )
    select_reference.add_argument("--voice-manifest", type=Path, required=True)
    select_reference.add_argument("--character", required=True)
    select_reference.add_argument("--reference-number", type=int, required=True)
    select_reference.add_argument("--output", type=Path, required=True)
    import_reference_review = subparsers.add_parser(
        "import-reference-review",
        help="Publish an immutable variant-aware plan from extractor decisions",
    )
    import_reference_review.add_argument("--report", type=Path, required=True)
    import_reference_review.add_argument("--review", type=Path, required=True)
    import_reference_review.add_argument("--story-index", type=Path, required=True)
    import_reference_review.add_argument("--output", type=Path, required=True)
    reference_evaluation = subparsers.add_parser(
        "build-reference-evaluation",
        help="Publish fixed-corpus inputs for accepted source-reference variants",
    )
    reference_evaluation.add_argument("--plan", type=Path, required=True)
    reference_evaluation.add_argument("--output", type=Path, required=True)
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
    annotate = subparsers.add_parser(
        "annotate-delivery",
        help="Print one provenance-marked legacy English delivery annotation",
    )
    annotate.add_argument("--text", required=True)
    annotate.add_argument("--speaker", default="Narrator")
    annotate.add_argument("--previous-text")
    annotate.add_argument("--next-text")
    annotate.add_argument("--kind", default="dialogue")
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
        if arguments.command == "annotate-delivery":
            application = apply_delivery_policy(
                {
                    "text": arguments.text,
                    "speaker": arguments.speaker,
                    "previous_text": arguments.previous_text,
                    "next_text": arguments.next_text,
                    "kind": arguments.kind,
                },
                LEGACY_ENGLISH_POLICY,
            )
            print(
                json.dumps(
                    {
                        "annotation": {
                            key: application.record[key]
                            for key in (
                                "annotation_version",
                                "emotion",
                                "delivery",
                                "prompt_adapters",
                            )
                        },
                        "provenance": application.provenance,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "create-workspace":
            missing_voice_policy = _generation_missing_voice_policy(arguments)
            failure_repair_policy = _generation_failure_repair_policy(arguments)
            result = create_resume_workspace(
                arguments.import_directory,
                arguments.workspaces_root,
                story_index=arguments.story_index,
                voice_manifest=arguments.voice_manifest,
                narrator_character=arguments.narrator_character,
                backend=arguments.backend,
                model=arguments.model,
                generation_profile=arguments.generation_profile,
                missing_voice_policy=missing_voice_policy,
                failure_repair_policy=failure_repair_policy,
                carry_forward_from=arguments.carry_forward_from,
                carry_forward_characters=arguments.carry_forward_characters,
            )
            print(
                json.dumps(
                    {
                        "directory": str(result.directory),
                        "created": result.created,
                        "missing_voice_policy": missing_voice_policy.to_document(),
                        "failure_repair_policy": failure_repair_policy.to_document(),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "generate":
            missing_voice_policy = _generation_missing_voice_policy(arguments)
            failure_repair_policy = _generation_failure_repair_policy(arguments)
            voice_manifest = arguments.voice_manifest.expanduser().resolve()
            expected_workspace_controls = None
            workspace_output_identity = None
            if arguments.workspace is not None:
                try:
                    expected_workspace_controls = generation_control_bindings(
                        arguments.workspace,
                        queue=arguments.queue,
                        output=arguments.output,
                        voice_manifest=voice_manifest,
                        backend=arguments.backend,
                        model=arguments.model,
                        generation_profile=arguments.generation_profile,
                        narrator_character=arguments.narrator_character,
                        missing_voice_policy=missing_voice_policy,
                        failure_repair_policy=failure_repair_policy,
                    )
                    workspace_output_identity = generation_output_identity(
                        arguments.workspace
                    )
                except AuthoringWorkbenchError as error:
                    raise BulkGenerationError(str(error)) from error
            registry, voice_manifest_sha256 = _load_stable_voice_registry(
                voice_manifest
            )
            try:
                policy_queue = VoiceGenerationQueue.load(arguments.queue)
            except VoiceGenerationQueueError as error:
                raise BulkGenerationError(str(error)) from error
            synthesis_character_overrides = {}
            for item in policy_queue.items:
                requested = synthesis_character_for_line(
                    item.speaker, item.voice_character
                )
                voice = registry.resolve(requested)
                if (
                    requested != "Narrator"
                    and (
                        voice is None
                        or not voice.references
                        or any(
                            not reference.is_file() for reference in voice.references
                        )
                    )
                    and missing_voice_policy.applies_to(requested)
                ):
                    synthesis_character_overrides[requested] = "Narrator"
            if (
                expected_workspace_controls is not None
                and expected_workspace_controls.get(voice_manifest)
                != voice_manifest_sha256
            ):
                raise BulkGenerationError(
                    "Workspace voice manifest changed before backend construction"
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
                reference_sha256 = sha256_control_path(reference)
                if (
                    expected_workspace_controls is not None
                    and expected_workspace_controls.get(reference) != reference_sha256
                ):
                    raise BulkGenerationError(
                        f"Workspace voice reference changed: {reference}"
                    )
                control_files[f"voice_reference:{index:04d}"] = (
                    reference,
                    reference_sha256,
                )
            if expected_workspace_controls is not None:
                observed_paths = {
                    Path(value[0]).resolve() for value in control_files.values()
                }
                if observed_paths != set(expected_workspace_controls):
                    raise BulkGenerationError(
                        "Workspace voice control inventory differs from the manifest"
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
            if narrator_reference is not None:
                control_files[f"narrator_selection:{arguments.narrator_character}"] = (
                    narrator_reference,
                    sha256_control_path(narrator_reference),
                )
            if arguments.backend == "moss-tts" and narrator_reference is None:
                raise BulkGenerationError(
                    f"Narrator voice {arguments.narrator_character!r} has no reference"
                )

            def ready_spoken_item(item):
                if not is_spoken_queue_item(item):
                    return False
                requested = synthesis_character_for_line(
                    item.speaker, item.voice_character
                )
                character = synthesis_character_overrides.get(requested, requested)
                if character == "Narrator":
                    return narrator_reference is not None
                voice = registry.resolve(character)
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
                        include_queue_ids=arguments.queue_ids,
                        regenerate_existing=arguments.regenerate_existing,
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
                        workspace_output_identity=workspace_output_identity,
                        synthesis_character_overrides=synthesis_character_overrides,
                        missing_voice_policy=missing_voice_policy.to_document(),
                        narrator_character=arguments.narrator_character,
                        failure_repair_policy=failure_repair_policy.to_document(),
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
        if arguments.command == "live-fallback":
            decision = authorize_live_fallback(
                arguments.state,
                arguments.queue,
                arguments.queue_id,
                reason=arguments.reason,
                provider=arguments.provider,
                model=arguments.model,
                generation_profile=arguments.generation_profile,
            )
            print(json.dumps(decision, indent=2, sort_keys=True))
            return 0
        if arguments.command == "publish":
            manifest = publish_generated_manifest(arguments.state)
            print(json.dumps({"manifest": str(manifest)}, indent=2, sort_keys=True))
            return 0
        if arguments.command == "status":
            state = load_generation_state(arguments.state, arguments.queue)
            counts = {
                "failed": 0,
                "generated": 0,
                "approved": 0,
                "live_fallback": 0,
            }
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
        if arguments.command == "failure-report":
            print(
                json.dumps(
                    generation_failure_report(arguments.state, arguments.queue),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "failure-repair-plan":
            print(
                json.dumps(
                    generation_failure_repair_plan(arguments.state, arguments.queue),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "reference-report":
            print(
                json.dumps(
                    inspect_voice_reference_candidates(
                        arguments.voice_manifest, arguments.character
                    ),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "select-reference":
            result = select_voice_reference(
                arguments.voice_manifest,
                arguments.character,
                arguments.reference_number,
                arguments.output,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "import-reference-review":
            result = import_source_reference_review(
                arguments.report,
                arguments.review,
                arguments.story_index,
                arguments.output,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "build-reference-evaluation":
            result = publish_source_reference_evaluation(
                arguments.plan, arguments.output
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
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
                delivery_policy=arguments.delivery_policy,
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
        AuthoringWorkbenchError,
        GenerationQueueBuildError,
        BulkGenerationError,
        DeliveryAnnotationError,
        FinalGamePackError,
        LegacyAuthoringImportError,
        ListeningImportError,
        ReferenceSelectionError,
        SourceReferenceReviewError,
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


if __name__ == "__main__":
    raise SystemExit(main())

"""Generation, state and bounded-repair command family."""

from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from vntts_artifacts import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    VoiceGenerationQueueItem,
)
from vntts_artifacts.voice_manifest import VoiceManifestEntry, load_voice_manifest

from vntts.authoring.bulk_generation import (
    BulkGenerationResult,
    audio_event_spoken_projection,
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
from vntts.authoring.cli_generation_options import (
    add_failure_repair_arguments,
    add_missing_voice_policy_arguments,
    failure_repair_policy,
    missing_voice_policy,
)
from vntts.authoring.failure_regeneration import (
    build_failure_regeneration_command,
    build_failure_regeneration_plan,
    load_failure_regeneration_plan,
    write_failure_regeneration_plan,
)
from vntts.authoring.failure_repair import FailureRepairPolicy
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_state import LIVE_FALLBACK_REASONS
from vntts.authoring.missing_voice_policy import MissingVoicePolicy
from vntts.authoring.pending_resolution import (
    build_pending_regeneration_command,
    build_pending_resolution_plan,
    load_pending_resolution_plan,
    write_pending_resolution_plan,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
)
from vntts.authoring.specialist_failure_plan import (
    build_specialist_failure_plan,
    write_specialist_failure_plan,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    FailureReferenceRuntimeBinding,
    failure_reference_runtime_binding,
    generation_control_bindings,
    generation_output_identity,
)
from vntts.speech_backend_runtime import shutdown_speech_backend
from vntts.tts_benchmark import create_backend
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    pocket_tts_preset_voices,
    synthesis_character_for_line,
)

COMMANDS = frozenset(
    {
        "generate",
        "review",
        "live-fallback",
        "publish",
        "status",
        "failure-report",
        "specialist-failure-plan",
        "failure-repair-plan",
        "pending-resolution-plan",
        "pending-regeneration-command",
        "failure-regeneration-plan",
        "failure-regeneration-command",
    }
)


def configure_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    _configure_generate_parser(subparsers)
    _configure_review_parser(subparsers)
    _configure_live_fallback_parser(subparsers)
    _configure_state_parsers(subparsers)
    _configure_regeneration_parsers(subparsers)


def _configure_generate_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
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
        "--cache-directory",
        type=Path,
        help=(
            "Persistent content-addressed synthesis cache; omitted for an "
            "isolated temporary authoring cache"
        ),
    )
    generate.add_argument(
        "--narrator-character",
        default="Narrator",
        help="Manifest character whose first reference voices queue Narrator lines",
    )
    generate.add_argument("--generation-profile")
    generate.add_argument(
        "--allow-gated-model-access",
        action="store_true",
        help="Permit authenticated Pocket TTS voice-cloning model access",
    )
    add_missing_voice_policy_arguments(generate)
    add_failure_repair_arguments(generate)
    generate.add_argument("--limit", type=int)
    generate.add_argument("--retries", type=int, default=2)
    generate.add_argument("--seed", type=int, default=0)
    generate.add_argument(
        "--capture-silence-failure",
        type=Path,
        help=(
            "Publish one selected, one-attempt speech-silence rejection as "
            "non-reviewable checksum-bound evidence outside generated output"
        ),
    )
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
    generate.add_argument(
        "--audio-event-spoken-projection",
        action="append",
        dest="audio_event_spoken_projection_queue_ids",
        help=(
            "Synthesize only spoken text from this exact mixed audio-event item; "
            "repeat for multiple IDs"
        ),
    )


def _configure_review_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    review = subparsers.add_parser(
        "review", help="Approve or reject one generated queue item"
    )
    review.add_argument("--state", type=Path, required=True)
    review.add_argument("queue_id")
    review.add_argument("decision", choices=("approved", "rejected"))


def _configure_live_fallback_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
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
    live_fallback.add_argument(
        "--evidence-workspace",
        action="append",
        default=[],
        type=Path,
        help=(
            "Bind one immutable failed repair workspace; repeat for "
            "generation_hypotheses_exhausted"
        ),
    )
    live_fallback.add_argument(
        "--evidence-review",
        action="append",
        default=[],
        type=Path,
        help=(
            "Bind one terminal need_different render-hypothesis review; repeat "
            "for generation_hypotheses_exhausted"
        ),
    )


def _configure_state_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
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

    specialist_failures = subparsers.add_parser(
        "specialist-failure-plan",
        help="Cluster terminal repair failures into checksum-bound next actions",
    )
    specialist_failures.add_argument(
        "--workspace", action="append", type=Path, required=True
    )
    specialist_failures.add_argument("--output", type=Path)

    repairs = subparsers.add_parser(
        "failure-repair-plan",
        help="Plan exact-ID bounded repairs without changing generation state",
    )
    repairs.add_argument("--state", type=Path, required=True)
    repairs.add_argument("--queue", type=Path, required=True)


def _configure_regeneration_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    pending_resolution = subparsers.add_parser(
        "pending-resolution-plan",
        help="Bind cohort-blocked pending WAVs to conservative next actions",
    )
    pending_resolution.add_argument("workspace", type=Path)
    pending_resolution.add_argument(
        "--output",
        type=Path,
        help="Publish the immutable plan without replacing an existing file",
    )

    pending_regeneration = subparsers.add_parser(
        "pending-regeneration-command",
        help="Print one bounded exact-ID command from a current pending plan",
    )
    pending_regeneration.add_argument("workspace", type=Path)
    pending_regeneration.add_argument("plan", type=Path)
    pending_regeneration.add_argument("--batch-index", type=int, required=True)
    pending_regeneration.add_argument("--batch-size", type=int, default=10)

    failure_regeneration = subparsers.add_parser(
        "failure-regeneration-plan",
        help="Bind provenance-unbound failures to exact-ID regeneration",
    )
    failure_regeneration.add_argument("workspace", type=Path)
    failure_regeneration.add_argument("--output", type=Path)

    failure_command = subparsers.add_parser(
        "failure-regeneration-command",
        help="Print one bounded exact-ID command from a current failure plan",
    )
    failure_command.add_argument("workspace", type=Path)
    failure_command.add_argument("plan", type=Path)
    failure_command.add_argument("--batch-index", type=int, required=True)
    failure_command.add_argument("--batch-size", type=int, default=10)


def _load_stable_voice_registry(
    manifest_path: str | Path,
) -> tuple[
    CharacterVoiceRegistry, str, dict[str, object], tuple[VoiceManifestEntry, ...]
]:
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
        document, entries = load_voice_manifest(snapshot)
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
    return CharacterVoiceRegistry(voices), digest, document, entries


def _workspace_generation_controls(
    arguments: argparse.Namespace,
    missing_policy: MissingVoicePolicy,
    repair_policy: FailureRepairPolicy,
    voice_manifest: Path,
    projection_ids: tuple[object, ...],
) -> tuple[dict[Path, str] | None, dict[str, str | int] | None]:
    if arguments.workspace is None:
        return None, None
    try:
        return (
            generation_control_bindings(
                arguments.workspace,
                queue=arguments.queue,
                output=arguments.output,
                voice_manifest=voice_manifest,
                backend=arguments.backend,
                model=arguments.model,
                generation_profile=arguments.generation_profile,
                narrator_character=arguments.narrator_character,
                missing_voice_policy=missing_policy,
                failure_repair_policy=repair_policy,
                audio_event_spoken_projection_queue_ids=projection_ids,
            ),
            generation_output_identity(arguments.workspace),
        )
    except AuthoringWorkbenchError as error:
        raise BulkGenerationError(str(error)) from error


def _generation_inputs(
    arguments: argparse.Namespace,
    voice_manifest: Path,
) -> tuple[
    CharacterVoiceRegistry,
    str,
    dict[str, object],
    tuple[VoiceManifestEntry, ...],
    FailureReferenceRuntimeBinding | None,
    VoiceGenerationQueue,
]:
    registry, manifest_sha256, manifest_document, manifest_entries = (
        _load_stable_voice_registry(voice_manifest)
    )
    runtime_binding = (
        failure_reference_runtime_binding(arguments.workspace)
        if arguments.workspace is not None
        else None
    )
    if runtime_binding is not None:
        registry = CharacterVoiceRegistry(
            (*registry.unique_voices(), *runtime_binding.voices)
        )
    try:
        policy_queue = VoiceGenerationQueue.load(arguments.queue)
    except VoiceGenerationQueueError as error:
        raise BulkGenerationError(str(error)) from error
    return (
        registry,
        manifest_sha256,
        manifest_document,
        manifest_entries,
        runtime_binding,
        policy_queue,
    )


def _queue_voice_overrides(
    manifest_document: dict[str, object],
    manifest_entries: tuple[VoiceManifestEntry, ...],
    policy_queue: VoiceGenerationQueue,
    runtime_binding: FailureReferenceRuntimeBinding | None,
) -> dict[str, str]:
    try:
        overrides = queue_voice_overrides_from_manifest(
            manifest_document,
            queue_ids=(item.queue_id for item in policy_queue.items),
            voices=manifest_entries,
        )
    except SourceReferenceBindingError as error:
        raise BulkGenerationError(str(error)) from error
    if runtime_binding is not None:
        overrides.update(runtime_binding.queue_voice_overrides)
    return overrides


def _synthesis_character_overrides(
    policy_queue: VoiceGenerationQueue,
    registry: CharacterVoiceRegistry,
    missing_policy: MissingVoicePolicy,
) -> dict[str, str]:
    overrides = {}
    for item in policy_queue.items:
        requested = synthesis_character_for_line(item.speaker, item.voice_character)
        voice = registry.resolve(requested)
        if (
            requested != "Narrator"
            and (
                voice is None
                or not voice.references
                or any(not reference.is_file() for reference in voice.references)
            )
            and missing_policy.applies_to(requested)
        ):
            overrides[requested] = "Narrator"
    return overrides


def _add_voice_reference_controls(
    control_files: dict[str, tuple[Path, str]],
    registry: CharacterVoiceRegistry,
    expected_workspace_controls: dict[Path, str] | None,
) -> None:
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
            raise BulkGenerationError(f"Workspace voice reference changed: {reference}")
        control_files[f"voice_reference:{index:04d}"] = (reference, reference_sha256)


def _add_runtime_binding_controls(
    control_files: dict[str, tuple[Path, str]],
    runtime_binding: FailureReferenceRuntimeBinding | None,
) -> None:
    if runtime_binding is None:
        return
    binding_path = (runtime_binding.directory / "binding.json").resolve()
    control_files["failure_reference_binding"] = (
        binding_path,
        runtime_binding.controls[binding_path],
    )
    for index, path in enumerate(
        sorted(
            (path for path in runtime_binding.controls if path != binding_path), key=str
        ),
        start=1,
    ):
        control_files[f"failure_reference_selected:{index:04d}"] = (
            path,
            runtime_binding.controls[path],
        )


def _generation_control_files(
    arguments: argparse.Namespace,
    registry: CharacterVoiceRegistry,
    runtime_binding: FailureReferenceRuntimeBinding | None,
    expected_workspace_controls: dict[Path, str] | None,
    voice_manifest: Path,
    manifest_sha256: str,
) -> dict[str, tuple[Path, str]]:
    if (
        expected_workspace_controls is not None
        and expected_workspace_controls.get(voice_manifest) != manifest_sha256
    ):
        raise BulkGenerationError(
            "Workspace voice manifest changed before backend construction"
        )
    control_files = {"voice_manifest": (voice_manifest, manifest_sha256)}
    _add_voice_reference_controls(control_files, registry, expected_workspace_controls)
    _add_runtime_binding_controls(control_files, runtime_binding)
    if expected_workspace_controls is not None:
        observed_paths = {Path(value[0]).resolve() for value in control_files.values()}
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
    return control_files


def _narrator_reference(
    arguments: argparse.Namespace,
    registry: CharacterVoiceRegistry,
    control_files: dict[str, tuple[Path, str]],
) -> Path | str | None:
    narrator_voice = registry.resolve(arguments.narrator_character)
    reference = (
        narrator_voice.references[0]
        if narrator_voice is not None and narrator_voice.references
        else (
            narrator_voice.speaker
            if arguments.backend == "pocket-tts"
            and narrator_voice is not None
            and narrator_voice.speaker in pocket_tts_preset_voices
            else "alba"
            if arguments.backend == "pocket-tts" and narrator_voice is None
            else None
        )
    )
    if narrator_voice is not None and narrator_voice.references:
        narrator_reference_path = narrator_voice.references[0]
        control_files[f"narrator_selection:{arguments.narrator_character}"] = (
            narrator_reference_path,
            sha256_control_path(narrator_reference_path),
        )
    if arguments.backend == "moss-tts" and reference is None:
        raise BulkGenerationError(
            f"Narrator voice {arguments.narrator_character!r} has no reference"
        )
    return reference


def _ready_spoken_item(
    item: VoiceGenerationQueueItem,
    *,
    backend: str,
    registry: CharacterVoiceRegistry,
    narrator_reference: Path | str | None,
    projection_ids: tuple[object, ...],
    synthesis_overrides: dict[str, str],
    queue_overrides: dict[str, str],
) -> bool:
    if not (is_spoken_queue_item(item) or item.queue_id in set(projection_ids)):
        return False
    requested = synthesis_character_for_line(item.speaker, item.voice_character)
    character = queue_overrides.get(
        item.queue_id,
        synthesis_overrides.get(requested, requested),
    )
    if character == "Narrator":
        return narrator_reference is not None
    voice = registry.resolve(character)
    return voice is not None and (
        bool(voice.references)
        or (backend == "pocket-tts" and voice.speaker in pocket_tts_preset_voices)
    )


def _generation_text_transform(
    backend: str,
    projection_ids: tuple[object, ...],
) -> tuple[Callable[[str], str] | None, str | None]:
    if projection_ids:
        return audio_event_spoken_projection, "audio-event-spoken-projection-v1"
    if backend == "moss-tts":
        return normalize_short_trailing_ellipsis, "short-trailing-ellipsis-v1"
    return None, None


def _run_bulk_generation(
    arguments: argparse.Namespace,
    backend_factory: Callable[..., object],
    cancellation: object,
    startup_progress: object,
    registry: CharacterVoiceRegistry,
    policy_queue: VoiceGenerationQueue,
    narrator_reference: Path | str | None,
    control_files: dict[str, tuple[Path, str]],
    workspace_output_identity: dict[str, str | int] | None,
    projection_ids: tuple[object, ...],
    synthesis_overrides: dict[str, str],
    queue_overrides: dict[str, str],
    missing_policy: MissingVoicePolicy,
    repair_policy: FailureRepairPolicy,
) -> BulkGenerationResult:
    cache_context = (
        nullcontext(arguments.cache_directory.expanduser().resolve())
        if arguments.cache_directory is not None
        else TemporaryDirectory()
    )
    with cache_context as cache_directory:
        Path(cache_directory).mkdir(parents=True, exist_ok=True)
        backend = backend_factory(
            arguments.backend,
            registry,
            cache_directory,
            model_name=arguments.model,
            narrator_reference=narrator_reference,
            allow_gated_model_access=arguments.allow_gated_model_access,
            persistent_audio_cache_max_entries=max(512, len(policy_queue.items) * 2),
            startup_cancellation=cancellation,
            startup_progress=startup_progress,
        )
        try:
            profile = arguments.generation_profile or getattr(
                backend, "generation_profile", "stable"
            )
            model = str(
                getattr(backend, "model_identity", None)
                or getattr(backend, "model_name", None)
                or arguments.backend
            )
            text_transform, text_transform_id = _generation_text_transform(
                arguments.backend, projection_ids
            )
            return run_bulk_generation(
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
                item_filter=lambda item: _ready_spoken_item(
                    item,
                    backend=arguments.backend,
                    registry=registry,
                    narrator_reference=narrator_reference,
                    projection_ids=projection_ids,
                    synthesis_overrides=synthesis_overrides,
                    queue_overrides=queue_overrides,
                ),
                seed=arguments.seed,
                control_files=control_files,
                text_transform=text_transform,
                text_transform_id=text_transform_id,
                workspace_output_identity=workspace_output_identity,
                synthesis_character_overrides=synthesis_overrides,
                queue_voice_overrides=queue_overrides,
                missing_voice_policy=missing_policy.to_document(),
                narrator_character=arguments.narrator_character,
                failure_repair_policy=repair_policy.to_document(),
                silence_failure_evidence=arguments.capture_silence_failure,
                audio_event_spoken_projection_queue_ids=projection_ids,
                synthesis_cache_policy=(
                    "refresh"
                    if arguments.cache_directory is not None
                    and arguments.regenerate_existing
                    else "use"
                    if arguments.cache_directory is not None
                    else "bypass"
                ),
                cancellation=cancellation,
            )
        finally:
            shutdown_speech_backend(backend)


def run_generation(
    arguments: argparse.Namespace,
    *,
    backend_factory: Callable[..., object] | None = None,
    cancellation: object = None,
    startup_progress: object = None,
) -> BulkGenerationResult:
    missing_policy = missing_voice_policy(arguments)
    repair_policy = failure_repair_policy(arguments)
    projection_ids = tuple(arguments.audio_event_spoken_projection_queue_ids or ())
    voice_manifest = arguments.voice_manifest.expanduser().resolve()
    expected_controls, workspace_output_identity = _workspace_generation_controls(
        arguments, missing_policy, repair_policy, voice_manifest, projection_ids
    )
    (
        registry,
        manifest_sha256,
        manifest_document,
        manifest_entries,
        runtime_binding,
        policy_queue,
    ) = _generation_inputs(arguments, voice_manifest)
    queue_overrides = _queue_voice_overrides(
        manifest_document, manifest_entries, policy_queue, runtime_binding
    )
    synthesis_overrides = _synthesis_character_overrides(
        policy_queue, registry, missing_policy
    )
    control_files = _generation_control_files(
        arguments,
        registry,
        runtime_binding,
        expected_controls,
        voice_manifest,
        manifest_sha256,
    )
    narrator_reference = _narrator_reference(arguments, registry, control_files)
    return _run_bulk_generation(
        arguments,
        backend_factory or create_backend,
        cancellation,
        startup_progress,
        registry,
        policy_queue,
        narrator_reference,
        control_files,
        workspace_output_identity,
        projection_ids,
        synthesis_overrides,
        queue_overrides,
        missing_policy,
        repair_policy,
    )


def _generate(arguments: argparse.Namespace) -> int:
    result = run_generation(arguments)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


def _handle_review(arguments: argparse.Namespace) -> int:
    review_generation_item(arguments.state, arguments.queue_id, arguments.decision)
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


def _handle_live_fallback(arguments: argparse.Namespace) -> int:
    decision = authorize_live_fallback(
        arguments.state,
        arguments.queue,
        arguments.queue_id,
        reason=arguments.reason,
        provider=arguments.provider,
        model=arguments.model,
        generation_profile=arguments.generation_profile,
        evidence_workspaces=arguments.evidence_workspace,
        evidence_reviews=arguments.evidence_review,
    )
    print(json.dumps(decision, indent=2, sort_keys=True))
    return 0


def _handle_publish(arguments: argparse.Namespace) -> int:
    manifest = publish_generated_manifest(arguments.state)
    print(json.dumps({"manifest": str(manifest)}, indent=2, sort_keys=True))
    return 0


def _handle_status(arguments: argparse.Namespace) -> int:
    state = load_generation_state(arguments.state, arguments.queue)
    items = state.get("items")
    if not isinstance(items, dict) or not all(
        isinstance(queue_id, str) and isinstance(item, dict)
        for queue_id, item in items.items()
    ):
        raise BulkGenerationError("Generation state items are malformed")
    counts: dict[str, int] = {
        "failed": 0,
        "generated": 0,
        "approved": 0,
        "omitted": 0,
    }
    for item in items.values():
        status = item.get("status")
        if isinstance(status, str) and status != "live_fallback":
            counts[status] += 1
    counts["live_fallback"] = sum(
        isinstance(item.get("live_fallback"), dict) for item in items.values()
    )
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


def _print_document(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _handle_failure_report(arguments: argparse.Namespace) -> int:
    _print_document(generation_failure_report(arguments.state, arguments.queue))
    return 0


def _handle_specialist_failure_plan(arguments: argparse.Namespace) -> int:
    plan = build_specialist_failure_plan(arguments.workspace)
    if arguments.output is not None:
        write_specialist_failure_plan(plan, arguments.output)
    _print_document(plan.to_dict())
    return 0


def _handle_failure_repair_plan(arguments: argparse.Namespace) -> int:
    _print_document(generation_failure_repair_plan(arguments.state, arguments.queue))
    return 0


def _handle_pending_resolution_plan(arguments: argparse.Namespace) -> int:
    plan = build_pending_resolution_plan(arguments.workspace)
    if arguments.output is not None:
        write_pending_resolution_plan(plan, arguments.output)
    _print_document(plan.to_dict())
    return 0


def _handle_pending_regeneration_command(arguments: argparse.Namespace) -> int:
    command = build_pending_regeneration_command(
        arguments.workspace,
        load_pending_resolution_plan(arguments.plan),
        batch_index=arguments.batch_index,
        batch_size=arguments.batch_size,
    )
    _print_document(command.to_dict())
    return 0


def _handle_failure_regeneration_plan(arguments: argparse.Namespace) -> int:
    plan = build_failure_regeneration_plan(arguments.workspace)
    if arguments.output is not None:
        write_failure_regeneration_plan(plan, arguments.output)
    _print_document(plan.to_dict())
    return 0


def _handle_failure_regeneration_command(arguments: argparse.Namespace) -> int:
    command = build_failure_regeneration_command(
        arguments.workspace,
        load_failure_regeneration_plan(arguments.plan),
        batch_index=arguments.batch_index,
        batch_size=arguments.batch_size,
    )
    _print_document(command.to_dict())
    return 0


COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "generate": _generate,
    "review": _handle_review,
    "live-fallback": _handle_live_fallback,
    "publish": _handle_publish,
    "status": _handle_status,
    "failure-report": _handle_failure_report,
    "specialist-failure-plan": _handle_specialist_failure_plan,
    "failure-repair-plan": _handle_failure_repair_plan,
    "pending-resolution-plan": _handle_pending_resolution_plan,
    "pending-regeneration-command": _handle_pending_regeneration_command,
    "failure-regeneration-plan": _handle_failure_regeneration_plan,
    "failure-regeneration-command": _handle_failure_regeneration_command,
}


def handle(arguments: argparse.Namespace) -> int:
    handler = COMMAND_HANDLERS.get(arguments.command)
    if handler is None:
        raise AssertionError(f"Unhandled generation command: {arguments.command}")
    return handler(arguments)

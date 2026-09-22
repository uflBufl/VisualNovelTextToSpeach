"""Carry exact terminal audio decisions across an additive authoring config."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts import VoiceGenerationQueue, VoiceGenerationQueueItem
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import VoiceManifestError, load_voice_manifest

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    process_is_alive,
    synthesis_character_for_line,
)
from vntts.authoring.generation_lease import GenerationLease
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.missing_voice_policy import MissingVoicePolicy
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.queue_extension import (
    QueueExtensionError,
    validate_additive_generation_queue,
)
from vntts.authoring.source_reference_bindings import (
    KNOWN_ROLE_REUSE_BINDING_FIELD,
    SourceReferenceBindingError,
    retired_source_reference_variants_from_manifest,
)
from vntts.authoring.terminal_conflict_records import is_terminal_review_outcome
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    WorkspaceCreationResult,
    contained_workspace_path,
    default_workspaces_root,
    load_workspace_authority,
    load_workspace_json,
    read_workspace_file_bytes,
    require_workspace_sha256,
    safe_workspace_relative_path,
)
from vntts.authoring.workspace_config import (
    selected_voice_manifest_path,
    workspace_config_fingerprint,
    workspace_missing_voice_policy,
)
from vntts.authoring.workspace_voice_runtime import (
    FailureReferenceRuntimeBinding,
    load_failure_reference_runtime_binding,
    load_workspace_queue_voice_overrides,
    load_workspace_voice_registry,
)
from vntts.voices import CharacterVoiceRegistry

CONFIG_REBASE_SCHEMA = "vntts.authoring-workspace-config-rebase"
CONFIG_REBASE_VERSION = 4
SUPPORTED_CONFIG_REBASE_VERSIONS = frozenset({1, 2, 3, CONFIG_REBASE_VERSION})
REBASE_CARRIED_TERMINAL = "carried_terminal"
REBASE_PENDING_KNOWN_ROLE_REUSE = "pending_after_known_role_reuse"
_WORKFLOW_FIELDS = {
    "carry_forward",
    "outcome_merge",
    "terminal_conflict_resolution",
    "config_rebase",
}
JsonObject = dict[str, object]
Route = tuple[str, tuple[str, ...]]
Snapshots = list[tuple[Path, str]]


@dataclass(frozen=True)
class _RebaseSelection:
    source_directory: Path
    source_document: JsonObject
    source_workspace_sha256: str
    source_import_id: str
    source_queue: Path
    source_queue_payload: bytes
    source_queue_sha256: str
    source_output: Path
    target_directory: Path
    target_document: JsonObject
    target_workspace_sha256: str
    target_import_id: str
    target_queue: Path
    target_queue_payload: bytes
    target_queue_sha256: str
    target_output: Path


@dataclass(frozen=True)
class _RebaseState:
    source_path: Path
    source_payload: bytes
    source_sha256: str
    source: JsonObject
    target_path: Path
    target_payload: bytes
    target_sha256: str
    target: JsonObject


@dataclass(frozen=True)
class _RebaseRouteSelection:
    source_registry: CharacterVoiceRegistry
    target_registry: CharacterVoiceRegistry
    source_overrides: Mapping[str, str]
    target_overrides: Mapping[str, str]
    source_failure_binding: FailureReferenceRuntimeBinding | None
    source_policy: MissingVoicePolicy
    target_policy: MissingVoicePolicy
    target_reference_sha256s: set[str]
    target_voice: Path | None
    retired_variants: Sequence[JsonObject]
    known_role_reuse: object


@dataclass(frozen=True)
class _ValidatedRebaseLedger:
    document: JsonObject
    version: int
    source_queue_sha256: str
    target_queue_sha256: str


@dataclass(frozen=True)
class _RebaseValidationContext:
    directory: Path
    queue_path: Path
    source_root: Path
    source_items: JsonObject
    state_items: JsonObject
    target_reference_sha256s: set[str]
    retired_variants: Sequence[JsonObject]
    known_role_reuse: object


def rebase_workspace_config(
    source_workspace: str | Path,
    target_workspace: str | Path,
    workspaces_root: str | Path | None = None,
) -> WorkspaceCreationResult:
    """Publish a successor with source terminal WAVs and target immutable config."""
    selection = _select_rebase_authorities(source_workspace, target_workspace)
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        with generation_publication_leases(
            (
                (selection.source_output, selection.source_queue_sha256),
                (selection.target_output, selection.target_queue_sha256),
            ),
            process_checker=process_is_alive,
        ) as leases:
            return _publish_rebase_workspace(selection, root, leases)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _select_rebase_authorities(
    source_workspace: str | Path, target_workspace: str | Path
) -> _RebaseSelection:
    source_directory, source_document, source_workspace_sha256 = (
        load_workspace_authority(source_workspace)
    )
    target_directory, target_document, target_workspace_sha256 = (
        load_workspace_authority(target_workspace)
    )
    source_document = _object(source_document, "Config rebase source workspace")
    target_document = _object(target_document, "Config rebase target workspace")
    source_import_id = _workspace_import_id(source_document, "source")
    target_import_id = _workspace_import_id(target_document, "target")
    if source_directory == target_directory:
        raise AuthoringWorkbenchError("Config rebase requires distinct workspaces")
    if source_import_id != target_import_id:
        raise AuthoringWorkbenchError("Config rebase workspaces use different imports")
    source_queue = source_directory / "queue.jsonl"
    target_queue = target_directory / "queue.jsonl"
    source_queue_payload = read_workspace_file_bytes(
        source_queue, "config rebase source queue"
    )
    target_queue_payload = read_workspace_file_bytes(
        target_queue, "config rebase target queue"
    )
    source_queue_sha256 = hashlib.sha256(source_queue_payload).hexdigest()
    target_queue_sha256 = hashlib.sha256(target_queue_payload).hexdigest()
    _validate_rebase_queue_successor(
        source_queue, target_queue, source_queue_payload, target_queue_payload
    )
    return _RebaseSelection(
        source_directory,
        source_document,
        source_workspace_sha256,
        source_import_id,
        source_queue,
        source_queue_payload,
        source_queue_sha256,
        source_directory / "generated-audio",
        target_directory,
        target_document,
        target_workspace_sha256,
        target_import_id,
        target_queue,
        target_queue_payload,
        target_queue_sha256,
        target_directory / "generated-audio",
    )


def _workspace_import_id(workspace: Mapping[str, object], label: str) -> str:
    source = _object(workspace.get("source"), f"Config rebase {label} import")
    return _text(source.get("import_id"), f"Config rebase {label} import ID")


def _validate_rebase_queue_successor(
    source_queue: Path,
    target_queue: Path,
    source_payload: bytes,
    target_payload: bytes,
) -> None:
    if source_payload == target_payload:
        return
    try:
        validate_additive_generation_queue(target_queue, base_queue=source_queue)
    except QueueExtensionError as error:
        raise AuthoringWorkbenchError(
            f"Config rebase target queue is not an exact additive successor: {error}"
        ) from error


def _publish_rebase_workspace(
    selection: _RebaseSelection, root: Path, leases: Sequence[GenerationLease]
) -> WorkspaceCreationResult:
    _assert_workspace_authority_snapshots(selection)
    composition = _rebase_audio_event_composition(
        selection.source_document, selection.target_document
    )
    state = _load_rebase_state(selection)
    routes = _select_rebase_routes(selection, composition)
    projected_state, records = _project_terminal_state(selection, state, routes)
    rebase = _rebase_ledger(selection, state, routes.target_voice, records)
    fingerprint = _rebase_config_fingerprint(selection, rebase, composition)
    workspace_id = _rebase_workspace_id(selection.target_import_id, fingerprint)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Config rebase destination"
    )
    with staged_directory(root, prefix=".config-rebase-staging-") as staging:
        workspace, snapshots = _stage_rebase_workspace(
            staging,
            selection,
            state,
            projected_state,
            records,
            rebase,
            composition,
            workspace_id,
            fingerprint,
        )
        _validate_staged_rebase(staging, workspace, projected_state, snapshots, leases)
        return _publish_staged_rebase(staging, destination, rebase, leases)


def _assert_workspace_authority_snapshots(selection: _RebaseSelection) -> None:
    for directory, digest, label in (
        (selection.source_directory, selection.source_workspace_sha256, "source"),
        (selection.target_directory, selection.target_workspace_sha256, "target"),
    ):
        if sha256_file(directory / "workspace.json") != digest:
            raise AuthoringWorkbenchError(
                f"Config rebase {label} workspace changed before publication"
            )


def _load_rebase_state(selection: _RebaseSelection) -> _RebaseState:
    source_path = selection.source_output / "generation-state.json"
    target_path = selection.target_output / "generation-state.json"
    source_payload = read_workspace_file_bytes(
        source_path, "config rebase source state"
    )
    target_payload = read_workspace_file_bytes(
        target_path, "config rebase target state"
    )
    source = load_generation_state(source_path, selection.source_queue)
    target = load_generation_state(target_path, selection.target_queue)
    if source.get("active") is not None or target.get("active") is not None:
        raise AuthoringWorkbenchError("Config rebase authority has an active attempt")
    if any(selection.source_output.rglob("*.partial.wav")) or any(
        selection.target_output.rglob("*.partial.wav")
    ):
        raise AuthoringWorkbenchError("Config rebase source is incomplete")
    return _RebaseState(
        source_path,
        source_payload,
        hashlib.sha256(source_payload).hexdigest(),
        source,
        target_path,
        target_payload,
        hashlib.sha256(target_payload).hexdigest(),
        target,
    )


def _select_rebase_routes(
    selection: _RebaseSelection, composition: JsonObject | None
) -> _RebaseRouteSelection:
    source_registry = load_workspace_voice_registry(
        selection.source_directory,
        selection.source_document,
        error_type=AuthoringWorkbenchError,
    )
    target_registry = load_workspace_voice_registry(
        selection.target_directory,
        selection.target_document,
        error_type=AuthoringWorkbenchError,
    )
    source_overrides = load_workspace_queue_voice_overrides(
        selection.source_directory,
        selection.source_document,
        error_type=AuthoringWorkbenchError,
    )
    target_overrides = load_workspace_queue_voice_overrides(
        selection.target_directory,
        selection.target_document,
        error_type=AuthoringWorkbenchError,
    )
    source_binding = load_failure_reference_runtime_binding(
        selection.source_directory,
        selection.source_document,
        error_type=AuthoringWorkbenchError,
    )
    references = {
        sha256_file(reference)
        for voice in target_registry.unique_voices()
        for reference in voice.references
    }
    if composition is not None:
        references.add(
            require_workspace_sha256(
                composition.get("final_audio_sha256"),
                "Config rebase audio-event composition WAV SHA-256",
            )
        )
    source_policy = workspace_missing_voice_policy(
        selection.source_document, error_type=AuthoringWorkbenchError
    )
    target_policy = workspace_missing_voice_policy(
        selection.target_document, error_type=AuthoringWorkbenchError
    )
    target_voice = selected_voice_manifest_path(
        selection.target_directory,
        selection.target_document,
        error_type=AuthoringWorkbenchError,
    )
    retired_variants, known_role_reuse = _target_voice_rebase_controls(target_voice)
    return _RebaseRouteSelection(
        source_registry,
        target_registry,
        source_overrides,
        target_overrides,
        source_binding,
        source_policy,
        target_policy,
        references,
        target_voice,
        retired_variants,
        known_role_reuse,
    )


def _target_voice_rebase_controls(
    path: Path | None,
) -> tuple[Sequence[JsonObject], object]:
    try:
        document, _entries = load_voice_manifest(path, allow_legacy=False)
        return (
            retired_source_reference_variants_from_manifest(document),
            document.get(KNOWN_ROLE_REUSE_BINDING_FIELD),
        )
    except (VoiceManifestError, SourceReferenceBindingError) as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _project_terminal_state(
    selection: _RebaseSelection,
    state: _RebaseState,
    routes: _RebaseRouteSelection,
) -> tuple[JsonObject, list[JsonObject]]:
    queue = _load_json_queue(selection.source_queue)
    source_items = _object(
        state.source.get("items"), "Config rebase source state items"
    )
    projected_state = _object(
        copy.deepcopy(state.target), "Config rebase projected state"
    )
    projected_items = _object(
        projected_state.get("items"), "Config rebase projected state items"
    )
    projected_state["items"] = projected_items
    records: list[JsonObject] = []
    for queue_id, value in sorted(source_items.items()):
        if not isinstance(value, dict):
            continue
        result = _object(value, f"Config rebase source item {queue_id!r}")
        if not is_terminal_review_outcome(result):
            continue
        record, projected = _project_terminal_record(
            selection, routes, queue, queue_id, result
        )
        _apply_projected_terminal(projected_items, queue_id, record, projected)
        records.append(record)
    if not records:
        raise AuthoringWorkbenchError(
            "Config rebase source has no terminal review outcomes"
        )
    return projected_state, records


def _project_terminal_record(
    selection: _RebaseSelection,
    routes: _RebaseRouteSelection,
    queue: Mapping[str, VoiceGenerationQueueItem],
    queue_id: str,
    result: JsonObject,
) -> tuple[JsonObject, JsonObject]:
    queue_item = queue.get(queue_id)
    if queue_item is None:
        raise AuthoringWorkbenchError(
            f"Config rebase source item is absent from queue: {queue_id}"
        )
    source_route, target_route = _projected_routes(
        selection, routes, queue_item, result
    )
    if not set(source_route[1]).issubset(routes.target_reference_sha256s):
        raise AuthoringWorkbenchError(
            f"Config rebase target omits source reference bytes for {queue_id!r}"
        )
    source_item_sha256 = canonical_document_sha256(result)
    route_status = _target_route_status(
        queue_id,
        result.get("status"),
        result.get("review_status"),
        source_route,
        target_route,
        routes.retired_variants,
        routes.known_role_reuse,
        source_item_sha256,
    )
    audio_sha256 = _source_item_audio_sha256(selection.source_output, queue_id, result)
    source_live_fallback = result.get("live_fallback")
    projected = _project_source_item(
        result, exclude_live_fallback=source_live_fallback is not None
    )
    record = _terminal_rebase_record(
        queue_id,
        result,
        projected,
        audio_sha256,
        source_route,
        target_route,
        route_status,
        routes.known_role_reuse,
        source_item_sha256,
    )
    projected["config_rebase"] = {
        key: value for key, value in record.items() if key != "queue_id"
    }
    if source_live_fallback is not None:
        projected["live_fallback"] = copy.deepcopy(source_live_fallback)
    return record, projected


def _projected_routes(
    selection: _RebaseSelection,
    routes: _RebaseRouteSelection,
    queue_item: VoiceGenerationQueueItem,
    result: JsonObject,
) -> tuple[Route, Route]:
    requested = synthesis_character_for_line(
        queue_item.speaker, queue_item.voice_character
    )
    source_route = _route_reference_identity(
        routes.source_registry,
        selection.source_document,
        routes.source_overrides,
        queue_item,
        result=result,
        failure_reference_binding=routes.source_failure_binding,
    )
    target_route = _route_reference_identity(
        routes.target_registry,
        selection.target_document,
        routes.target_overrides,
        queue_item,
        source_result=result,
        failure_reference_binding=routes.source_failure_binding,
        allow_missing=True,
    )
    policies_differ = routes.target_policy.applies_to(
        requested
    ) != routes.source_policy.applies_to(requested)
    if policies_differ and not set(source_route[1]) & set(target_route[1]):
        raise AuthoringWorkbenchError(
            "Config rebase changes fallback reference bytes for "
            f"terminal item {queue_item.queue_id!r}"
        )
    return source_route, target_route


def _source_item_audio_sha256(
    source_output: Path, queue_id: str, result: Mapping[str, object]
) -> str:
    relative = safe_workspace_relative_path(
        result.get("path"), f"Config rebase item {queue_id!r} WAV"
    )
    audio = contained_workspace_path(
        source_output, relative, "Config rebase source WAV"
    )
    payload = read_workspace_file_bytes(audio, "config rebase source WAV")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != require_workspace_sha256(
        result.get("file_sha256"), f"Config rebase item {queue_id!r} WAV SHA-256"
    ):
        raise AuthoringWorkbenchError(
            f"Config rebase source WAV changed for {queue_id!r}"
        )
    return digest


def _terminal_rebase_record(
    queue_id: str,
    result: JsonObject,
    projected: JsonObject,
    audio_sha256: str,
    source_route: Route,
    target_route: Route,
    route_status: str,
    known_role_reuse: object,
    source_item_sha256: str,
) -> JsonObject:
    successor_state = (
        REBASE_PENDING_KNOWN_ROLE_REUSE
        if _known_role_reuse_requeues_rejection(
            queue_id,
            result.get("status"),
            result.get("review_status"),
            target_route,
            route_status,
            known_role_reuse,
            source_item_sha256,
        )
        else REBASE_CARRIED_TERMINAL
    )
    return {
        "queue_id": queue_id,
        "source_item_sha256": source_item_sha256,
        "projected_item_sha256": canonical_document_sha256(projected),
        "audio_sha256": audio_sha256,
        "status": result["status"],
        "review_status": result["review_status"],
        "source_effective_character": source_route[0],
        "target_effective_character": target_route[0],
        "source_reference_sha256s": list(source_route[1]),
        "target_reference_sha256s": list(target_route[1]),
        "target_route_status": route_status,
        "successor_state": successor_state,
    }


def _apply_projected_terminal(
    projected_items: JsonObject,
    queue_id: str,
    record: JsonObject,
    projected: JsonObject,
) -> None:
    if record["successor_state"] == REBASE_PENDING_KNOWN_ROLE_REUSE:
        projected_items.pop(queue_id, None)
        return
    projected_items[queue_id] = projected


def _rebase_ledger(
    selection: _RebaseSelection,
    state: _RebaseState,
    target_voice: Path | None,
    records: list[JsonObject],
) -> JsonObject:
    source_voice = selected_voice_manifest_path(
        selection.source_directory,
        selection.source_document,
        error_type=AuthoringWorkbenchError,
    )
    return {
        "schema": CONFIG_REBASE_SCHEMA,
        "schema_version": CONFIG_REBASE_VERSION,
        "source_workspace_id": _text(
            selection.source_document.get("workspace_id"),
            "Config rebase source workspace ID",
        ),
        "source_workspace_sha256": selection.source_workspace_sha256,
        "source_state_sha256": state.source_sha256,
        "source_voice_manifest_sha256": sha256_file(source_voice),
        "target_workspace_id": _text(
            selection.target_document.get("workspace_id"),
            "Config rebase target workspace ID",
        ),
        "target_workspace_sha256": selection.target_workspace_sha256,
        "target_state_sha256": state.target_sha256,
        "target_voice_manifest_sha256": sha256_file(target_voice),
        "source_queue_sha256": selection.source_queue_sha256,
        "target_queue_sha256": selection.target_queue_sha256,
        "items": records,
    }


def _rebase_config_fingerprint(
    selection: _RebaseSelection, rebase: JsonObject, composition: JsonObject | None
) -> str:
    target = selection.target_document
    return workspace_config_fingerprint(
        selection.target_import_id,
        target.get("story_index"),
        target.get("voice_manifest"),
        _text(target.get("narrator_character"), "Config rebase narrator character"),
        target["run_config"],
        target.get("carry_forward"),
        target.get("outcome_merge"),
        target.get("failure_reference_binding"),
        target.get("terminal_conflict_merge"),
        config_rebase=rebase,
        audio_event_composition=composition,
        explicit_fallback_merge=target.get("explicit_fallback_merge"),
        known_role_live_fallback=target.get("known_role_live_fallback"),
        audio_event_omission=target.get("audio_event_omission"),
        audio_event_projection_fallback=target.get("audio_event_projection_fallback"),
        reviewed_waveform_publication=target.get("reviewed_waveform_publication"),
        reviewed_rejection_live_fallback=target.get("reviewed_rejection_live_fallback"),
        queue_extension=target.get("queue_extension"),
    )


def _rebase_workspace_id(target_import_id: str, fingerprint: str) -> str:
    return "resume-" + target_import_id.removeprefix("legacy-") + f"-{fingerprint[:16]}"


def _stage_rebase_workspace(
    staging: Path,
    selection: _RebaseSelection,
    state: _RebaseState,
    projected_state: JsonObject,
    records: Sequence[JsonObject],
    rebase: JsonObject,
    composition: JsonObject | None,
    workspace_id: str,
    fingerprint: str,
) -> tuple[JsonObject, Snapshots]:
    snapshots = _rebase_snapshots(selection, state)
    _stage_rebase_inputs(staging, selection, composition, snapshots)
    _stage_rebase_provenance(staging, selection, state, records, snapshots)
    _stage_rebase_output(staging, selection, projected_state, records, snapshots)
    workspace = _rebase_workspace_document(
        selection.target_document, composition, workspace_id, rebase, fingerprint
    )
    atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
    return workspace, snapshots


def _rebase_snapshots(selection: _RebaseSelection, state: _RebaseState) -> Snapshots:
    return [
        (
            selection.source_directory / "workspace.json",
            selection.source_workspace_sha256,
        ),
        (state.source_path, state.source_sha256),
        (selection.source_queue, selection.source_queue_sha256),
        (
            selection.target_directory / "workspace.json",
            selection.target_workspace_sha256,
        ),
        (state.target_path, state.target_sha256),
    ]


def _stage_rebase_inputs(
    staging: Path,
    selection: _RebaseSelection,
    composition: JsonObject | None,
    snapshots: Snapshots,
) -> None:
    _copy_tree(selection.target_directory / "inputs", staging / "inputs", snapshots)
    if (
        composition is not None
        and selection.target_document.get("audio_event_composition") is None
    ):
        _copy_audio_event_composition_inputs(
            selection.source_directory, staging, composition, snapshots
        )
    _copy_tree(
        selection.target_directory / "provenance", staging / "provenance", snapshots
    )
    (staging / "queue.jsonl").write_bytes(selection.target_queue_payload)
    snapshots.append((selection.target_queue, selection.target_queue_sha256))


def _stage_rebase_provenance(
    staging: Path,
    selection: _RebaseSelection,
    state: _RebaseState,
    records: Sequence[JsonObject],
    snapshots: Snapshots,
) -> None:
    source_root = staging / "provenance" / "config-rebase" / "source-root"
    _copy_tree(selection.source_directory / "inputs", source_root / "inputs", snapshots)
    (source_root / "queue.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "queue.jsonl").write_bytes(selection.source_queue_payload)
    (source_root / "workspace.json").write_bytes(
        read_workspace_file_bytes(
            selection.source_directory / "workspace.json", "source workspace"
        )
    )
    source_audio = source_root / "generated-audio"
    source_audio.mkdir(parents=True)
    (source_audio / "generation-state.json").write_bytes(state.source_payload)
    _stage_pending_history(
        source_audio, selection.source_output, state.source, records, snapshots
    )
    target_root = staging / "provenance" / "config-rebase" / "target-root"
    target_root.mkdir(parents=True)
    (target_root / "workspace.json").write_bytes(
        read_workspace_file_bytes(
            selection.target_directory / "workspace.json", "target workspace"
        )
    )
    (target_root / "generation-state.json").write_bytes(state.target_payload)


def _stage_pending_history(
    destination: Path,
    source_output: Path,
    source_state: Mapping[str, object],
    records: Sequence[JsonObject],
    snapshots: Snapshots,
) -> None:
    source_items = _object(
        source_state.get("items"), "Config rebase source state items"
    )
    for record in records:
        if record["successor_state"] != REBASE_PENDING_KNOWN_ROLE_REUSE:
            continue
        queue_id = _text(record.get("queue_id"), "Config rebase queue ID")
        item = _object(
            source_items[queue_id], f"Config rebase source item {queue_id!r}"
        )
        relative = safe_workspace_relative_path(
            item.get("path"),
            f"Config rebase pending-history {record['queue_id']!r} WAV",
        )
        source = contained_workspace_path(
            source_output, relative, "Config rebase pending-history WAV"
        )
        target = contained_workspace_path(
            destination, relative, "Config rebase pending-history WAV"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            read_workspace_file_bytes(source, "config rebase pending-history WAV")
        )
        snapshots.append(
            (source, _text(record.get("audio_sha256"), "Config rebase audio SHA-256"))
        )


def _stage_rebase_output(
    staging: Path,
    selection: _RebaseSelection,
    projected_state: JsonObject,
    records: Sequence[JsonObject],
    snapshots: Snapshots,
) -> None:
    output = staging / "generated-audio"
    output.mkdir()
    rebased_ids = {
        _text(record.get("queue_id"), "Config rebase queue ID") for record in records
    }
    _copy_projected_audio(
        output,
        projected_state,
        selection.source_output,
        selection.target_output,
        rebased_ids,
        snapshots,
    )
    atomic_write_json(output / "generation-state.json", projected_state, sort_keys=True)
    write_generated_manifest_from_state(
        projected_state, output, output / "manifest.json"
    )


def _copy_projected_audio(
    output: Path,
    projected_state: Mapping[str, object],
    source_output: Path,
    target_output: Path,
    rebased_ids: set[str],
    snapshots: Snapshots,
) -> None:
    path_owners: dict[str, str] = {}
    projected_items = _object(
        projected_state.get("items"), "Config rebase projected state items"
    )
    for queue_id, value in projected_items.items():
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            continue
        result = _object(value, f"Config rebase projected item {queue_id!r}")
        _copy_projected_audio_item(
            output,
            source_output if queue_id in rebased_ids else target_output,
            queue_id,
            result,
            path_owners,
            snapshots,
        )


def _copy_projected_audio_item(
    output: Path,
    authority_output: Path,
    queue_id: str,
    result: JsonObject,
    path_owners: dict[str, str],
    snapshots: Snapshots,
) -> None:
    relative = safe_workspace_relative_path(
        result["path"], f"Config rebase state item {queue_id!r} WAV"
    )
    previous = path_owners.setdefault(relative.as_posix(), queue_id)
    if previous != queue_id:
        raise AuthoringWorkbenchError(
            f"Config rebase WAV path collides with {previous!r}"
        )
    source = contained_workspace_path(
        authority_output, relative, "Config rebase state WAV"
    )
    payload = read_workspace_file_bytes(source, "config rebase state WAV")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != require_workspace_sha256(
        result.get("file_sha256"), f"Config rebase state item {queue_id!r} WAV SHA-256"
    ):
        raise AuthoringWorkbenchError(
            f"Config rebase state WAV changed for {queue_id!r}"
        )
    target = contained_workspace_path(output, relative, "Config rebase output WAV")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    snapshots.append((source, digest))


def _rebase_workspace_document(
    target: JsonObject,
    composition: JsonObject | None,
    workspace_id: str,
    rebase: JsonObject,
    fingerprint: str,
) -> JsonObject:
    workspace = copy.deepcopy(target)
    for field in (
        "carry_forward",
        "outcome_merge",
        "terminal_conflict_merge",
        "failure_reference_binding",
    ):
        workspace.pop(field, None)
    if composition is not None:
        workspace["audio_event_composition"] = copy.deepcopy(composition)
    workspace.update(
        {
            "workspace_id": workspace_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config_rebase": rebase,
            "config_fingerprint": fingerprint,
        }
    )
    return workspace


def _validate_staged_rebase(
    staging: Path,
    workspace: JsonObject,
    projected_state: JsonObject,
    snapshots: Snapshots,
    leases: Sequence[GenerationLease],
) -> None:
    validate_config_rebase_workspace(staging, workspace, projected_state)
    output = staging / "generated-audio"
    load_generation_state(output / "generation-state.json", staging / "queue.jsonl")
    _assert_rebase_snapshots(snapshots)
    for lease in leases:
        lease.assert_owned()


def _assert_rebase_snapshots(snapshots: Sequence[tuple[Path, str]]) -> None:
    for path, digest in snapshots:
        if not path.is_file() or sha256_file(path) != digest:
            raise AuthoringWorkbenchError(
                f"Config rebase source changed during publication: {path}"
            )


def _publish_staged_rebase(
    staging: Path,
    destination: Path,
    rebase: JsonObject,
    leases: Sequence[GenerationLease],
) -> WorkspaceCreationResult:
    if destination.exists():
        _assert_rebase_destination(destination, rebase)
        return WorkspaceCreationResult(destination, False)
    try:
        rename_directory_no_replace(staging, destination)
    except (AtomicPublicationError, OSError) as error:
        if destination.exists() and _matches_rebase_destination(destination, rebase):
            _mark_rebase_leases_committed(leases)
            return WorkspaceCreationResult(destination, False)
        raise AuthoringWorkbenchError(
            f"Unable to publish config rebase workspace: {error}"
        ) from error
    _mark_rebase_leases_committed(leases)
    return WorkspaceCreationResult(destination, True)


def _assert_rebase_destination(destination: Path, rebase: JsonObject) -> None:
    if not _matches_rebase_destination(destination, rebase):
        raise AuthoringWorkbenchError(
            "Config rebase destination contains different authority"
        )


def _matches_rebase_destination(destination: Path, rebase: JsonObject) -> bool:
    _directory, existing, _digest = load_workspace_authority(destination)
    return existing.get("config_rebase") == rebase


def _mark_rebase_leases_committed(leases: Sequence[GenerationLease]) -> None:
    for lease in leases:
        lease.mark_committed()


def validate_config_rebase_workspace(
    directory: str | Path,
    workspace: Mapping[str, object],
    state: JsonObject | None = None,
) -> None:
    """Validate self-contained config-rebase authority and exact item projection."""
    _validate_config_rebase_workspace(directory, workspace, state)
    return


def _validate_config_rebase_workspace(
    directory: str | Path,
    workspace: Mapping[str, object],
    state: JsonObject | None,
) -> None:
    ledger = _validated_rebase_ledger(workspace)
    if ledger is None:
        return
    context = _rebase_validation_context(
        Path(directory).resolve(), workspace, ledger, state
    )
    records = ledger.document.get("items")
    if not isinstance(records, list) or not records:
        raise AuthoringWorkbenchError("Config rebase item ledger is empty")
    extensions = _rebase_later_extensions(workspace)
    observed_ids = [
        _validate_rebase_record(record, ledger, context, extensions)
        for record in records
    ]
    _validate_rebase_record_ledger(observed_ids, context.state_items)


def _validated_rebase_ledger(
    workspace: Mapping[str, object],
) -> _ValidatedRebaseLedger | None:
    rebase_value = workspace.get("config_rebase")
    if rebase_value is None:
        return None
    if not isinstance(rebase_value, dict):
        raise AuthoringWorkbenchError("Workspace config rebase ledger is malformed")
    rebase = _object(rebase_value, "Workspace config rebase ledger")
    version = rebase.get("schema_version")
    required = _rebase_ledger_fields(version)
    if (
        set(rebase) != required
        or rebase.get("schema") != CONFIG_REBASE_SCHEMA
        or version not in SUPPORTED_CONFIG_REBASE_VERSIONS
    ):
        raise AuthoringWorkbenchError("Workspace config rebase ledger is malformed")
    source_queue_sha256, target_queue_sha256 = _validate_rebase_ledger_digests(
        rebase, version
    )
    return _ValidatedRebaseLedger(
        rebase, int(version), source_queue_sha256, target_queue_sha256
    )


def _rebase_ledger_fields(version: object) -> set[str]:
    fields = {
        "schema",
        "schema_version",
        "source_workspace_id",
        "source_workspace_sha256",
        "source_state_sha256",
        "source_voice_manifest_sha256",
        "target_workspace_id",
        "target_workspace_sha256",
        "target_state_sha256",
        "target_voice_manifest_sha256",
        "items",
    }
    if version == 4:
        return fields | {"source_queue_sha256", "target_queue_sha256"}
    return fields | {"queue_sha256"}


def _validate_rebase_ledger_digests(
    rebase: Mapping[str, object], version: object
) -> tuple[str, str]:
    fields = [
        "source_workspace_sha256",
        "source_state_sha256",
        "source_voice_manifest_sha256",
        "target_workspace_sha256",
        "target_state_sha256",
        "target_voice_manifest_sha256",
    ]
    queue_fields = (
        ("source_queue_sha256", "target_queue_sha256")
        if version == 4
        else ("queue_sha256", "queue_sha256")
    )
    for field in [*fields, *queue_fields]:
        require_workspace_sha256(rebase.get(field), f"Config rebase {field}")
    return (
        require_workspace_sha256(
            rebase[queue_fields[0]], f"Config rebase {queue_fields[0]}"
        ),
        require_workspace_sha256(
            rebase[queue_fields[1]], f"Config rebase {queue_fields[1]}"
        ),
    )


def _rebase_validation_context(
    directory: Path,
    workspace: Mapping[str, object],
    ledger: _ValidatedRebaseLedger,
    state: JsonObject | None,
) -> _RebaseValidationContext:
    queue_path = directory / "queue.jsonl"
    if sha256_file(queue_path) != ledger.target_queue_sha256:
        raise AuthoringWorkbenchError("Config rebase queue was modified")
    source_root = directory / "provenance" / "config-rebase" / "source-root"
    target_root = directory / "provenance" / "config-rebase" / "target-root"
    source_workspace, source_queue, source_state = _rebase_source_snapshot_paths(
        source_root
    )
    target_workspace, target_state = _rebase_target_snapshot_paths(target_root)
    _validate_rebase_snapshots(
        ledger.document,
        source_workspace,
        source_queue,
        source_state,
        target_workspace,
        target_state,
        ledger.source_queue_sha256,
    )
    source_document = load_workspace_json(
        source_workspace, "config rebase source workspace"
    )
    target_document = load_workspace_json(
        target_workspace, "config rebase target workspace"
    )
    _validate_rebase_workspace_identity(
        ledger.document, source_document, target_document
    )
    _validate_rebase_additive_queue(ledger, queue_path, source_queue)
    references, retired_variants, known_role_reuse = _validate_rebase_voice_authority(
        directory, workspace, source_root, source_document, ledger.document
    )
    source_items = _object(
        load_workspace_json(source_state, "config rebase source state").get("items"),
        "Config rebase source state items",
    )
    if state is None:
        state = load_generation_state(
            directory / "generated-audio" / "generation-state.json", queue_path
        )
    state_items = _object(state.get("items"), "Config rebase state items")
    return _RebaseValidationContext(
        directory,
        queue_path,
        source_root,
        source_items,
        state_items,
        references,
        retired_variants,
        known_role_reuse,
    )


def _rebase_source_snapshot_paths(root: Path) -> tuple[Path, Path, Path]:
    return (
        root / "workspace.json",
        root / "queue.jsonl",
        root / "generated-audio" / "generation-state.json",
    )


def _rebase_target_snapshot_paths(root: Path) -> tuple[Path, Path]:
    return root / "workspace.json", root / "generation-state.json"


def _validate_rebase_snapshots(
    rebase: Mapping[str, object],
    source_workspace: Path,
    source_queue: Path,
    source_state: Path,
    target_workspace: Path,
    target_state: Path,
    source_queue_sha256: str,
) -> None:
    snapshots = (
        (source_workspace, rebase["source_workspace_sha256"], "source workspace"),
        (source_state, rebase["source_state_sha256"], "source state"),
        (target_workspace, rebase["target_workspace_sha256"], "target workspace"),
        (target_state, rebase["target_state_sha256"], "target state"),
        (source_queue, source_queue_sha256, "source queue"),
    )
    for path, digest, label in snapshots:
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise AuthoringWorkbenchError(f"Config rebase {label} snapshot changed")


def _validate_rebase_workspace_identity(
    rebase: Mapping[str, object],
    source: Mapping[str, object],
    target: Mapping[str, object],
) -> None:
    if (
        source.get("workspace_id") != rebase["source_workspace_id"]
        or target.get("workspace_id") != rebase["target_workspace_id"]
    ):
        raise AuthoringWorkbenchError("Config rebase workspace identity changed")


def _validate_rebase_additive_queue(
    ledger: _ValidatedRebaseLedger, queue_path: Path, source_queue: Path
) -> None:
    if ledger.version != 4 or ledger.source_queue_sha256 == ledger.target_queue_sha256:
        return
    try:
        validate_additive_generation_queue(queue_path, base_queue=source_queue)
    except QueueExtensionError as error:
        raise AuthoringWorkbenchError(
            f"Config rebase additive queue changed: {error}"
        ) from error


def _validate_rebase_voice_authority(
    directory: Path,
    workspace: Mapping[str, object],
    source_root: Path,
    source_document: Mapping[str, object],
    rebase: Mapping[str, object],
) -> tuple[set[str], Sequence[JsonObject], object]:
    source_voice = selected_voice_manifest_path(
        source_root, source_document, error_type=AuthoringWorkbenchError
    )
    selected_voice = selected_voice_manifest_path(
        directory, workspace, error_type=AuthoringWorkbenchError
    )
    if (
        sha256_file(source_voice) != rebase["source_voice_manifest_sha256"]
        or sha256_file(selected_voice) != rebase["target_voice_manifest_sha256"]
    ):
        raise AuthoringWorkbenchError("Config rebase voice authority changed")
    retired_variants, known_role_reuse = _target_voice_rebase_controls(selected_voice)
    registry = load_workspace_voice_registry(
        directory, workspace, error_type=AuthoringWorkbenchError
    )
    references = {
        sha256_file(reference)
        for voice in registry.unique_voices()
        for reference in voice.references
    }
    composition = workspace.get("audio_event_composition")
    if isinstance(composition, dict):
        references.add(
            require_workspace_sha256(
                composition.get("final_audio_sha256"),
                "Config rebase audio-event composition WAV SHA-256",
            )
        )
    return references, retired_variants, known_role_reuse


def _rebase_later_extensions(workspace: Mapping[str, object]) -> dict[str, JsonObject]:
    extensions: dict[str, JsonObject] = {}
    _collect_rebase_extension(
        extensions, workspace.get("outcome_merge"), "outcome_merge"
    )
    _collect_rebase_extension(
        extensions,
        workspace.get("terminal_conflict_merge"),
        "terminal_conflict_resolution",
    )
    return extensions


def _collect_rebase_extension(
    extensions: dict[str, JsonObject], value: object, field: str
) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return
    for item in value["items"]:
        if isinstance(item, dict) and isinstance(item.get("queue_id"), str):
            extensions.setdefault(item["queue_id"], {})[field] = {
                key: item_value for key, item_value in item.items() if key != "queue_id"
            }


def _validate_rebase_record(
    record_value: object,
    ledger: _ValidatedRebaseLedger,
    context: _RebaseValidationContext,
    extensions: Mapping[str, JsonObject],
) -> str:
    record = _validated_rebase_record(record_value, ledger.version)
    queue_id = _text(record.get("queue_id"), "Config rebase queue identity")
    _validate_rebase_record_digests(record)
    _validate_rebase_record_references(
        record, ledger.version, context.target_reference_sha256s, queue_id
    )
    source_route, target_route = _validate_rebase_record_routes(record, queue_id)
    route_status = _validate_rebase_route_status(
        record, queue_id, source_route, target_route, context
    )
    successor_state = _validate_rebase_successor_state(
        record, queue_id, target_route, route_status, context.known_role_reuse
    )
    source_item = _validate_rebase_source_projection(
        record, queue_id, context.source_items
    )
    _validate_rebase_current_projection(
        record, queue_id, successor_state, source_item, context, extensions
    )
    return queue_id


def _validated_rebase_record(value: object, version: int) -> JsonObject:
    fields = {
        "queue_id",
        "source_item_sha256",
        "projected_item_sha256",
        "audio_sha256",
        "status",
        "review_status",
        "source_effective_character",
        "target_effective_character",
        "source_reference_sha256s",
        "target_reference_sha256s",
    }
    if version >= 2:
        fields.add("target_route_status")
    if version >= 3:
        fields.add("successor_state")
    if not isinstance(value, dict) or set(value) != fields:
        raise AuthoringWorkbenchError("Config rebase item record is malformed")
    return _object(value, "Config rebase item record")


def _validate_rebase_record_digests(record: Mapping[str, object]) -> None:
    for field in ("source_item_sha256", "projected_item_sha256", "audio_sha256"):
        require_workspace_sha256(record.get(field), f"Config rebase item {field}")


def _validate_rebase_record_references(
    record: Mapping[str, object],
    version: int,
    target_references: set[str],
    queue_id: str,
) -> None:
    for field in ("source_reference_sha256s", "target_reference_sha256s"):
        values = record.get(field)
        if not isinstance(values, list) or values != sorted(set(values)):
            raise AuthoringWorkbenchError(
                f"Config rebase item {field} is not canonical"
            )
        if not values and (field == "source_reference_sha256s" or version < 2):
            raise AuthoringWorkbenchError(
                f"Config rebase item {field} is not canonical"
            )
        for value in values:
            require_workspace_sha256(value, f"Config rebase item {field}")
    source = record["source_reference_sha256s"]
    if not isinstance(source, list):
        raise AuthoringWorkbenchError(
            "Config rebase item source_reference_sha256s is not canonical"
        )
    if not set(source).issubset(target_references):
        raise AuthoringWorkbenchError(
            f"Config rebase target omits source reference bytes for {queue_id!r}"
        )


def _validate_rebase_record_routes(
    record: Mapping[str, object], queue_id: str
) -> tuple[Route, Route]:
    routes: list[Route] = []
    for character_field, references_field in (
        ("source_effective_character", "source_reference_sha256s"),
        ("target_effective_character", "target_reference_sha256s"),
    ):
        character = record.get(character_field)
        references = record.get(references_field)
        if not isinstance(character, str) or not character.strip():
            raise AuthoringWorkbenchError(
                f"Config rebase item {character_field} is invalid"
            )
        if not isinstance(references, list) or not all(
            isinstance(value, str) for value in references
        ):
            raise AuthoringWorkbenchError(
                f"Config rebase item {references_field} is not canonical"
            )
        routes.append((character, tuple(references)))
    return routes[0], routes[1]


def _validate_rebase_route_status(
    record: Mapping[str, object],
    queue_id: str,
    source_route: Route,
    target_route: Route,
    context: _RebaseValidationContext,
) -> str:
    expected = _target_route_status(
        queue_id,
        record.get("status"),
        record.get("review_status"),
        source_route,
        target_route,
        context.retired_variants,
        context.known_role_reuse,
        record["source_item_sha256"],
    )
    if record.get("target_route_status", "active") != expected:
        raise AuthoringWorkbenchError(
            f"Config rebase target route status is invalid for {queue_id!r}"
        )
    return expected


def _validate_rebase_successor_state(
    record: Mapping[str, object],
    queue_id: str,
    target_route: Route,
    route_status: str,
    known_role_reuse: object,
) -> str:
    expected = (
        REBASE_PENDING_KNOWN_ROLE_REUSE
        if _known_role_reuse_requeues_rejection(
            queue_id,
            record.get("status"),
            record.get("review_status"),
            target_route,
            route_status,
            known_role_reuse,
            record["source_item_sha256"],
        )
        else REBASE_CARRIED_TERMINAL
    )
    if record.get("successor_state", REBASE_CARRIED_TERMINAL) != expected:
        raise AuthoringWorkbenchError(
            f"Config rebase successor state is invalid for {queue_id!r}"
        )
    return expected


def _validate_rebase_source_projection(
    record: Mapping[str, object], queue_id: str, source_items: Mapping[str, object]
) -> JsonObject:
    source_item = source_items.get(queue_id)
    if (
        not isinstance(source_item, dict)
        or canonical_document_sha256(source_item) != record["source_item_sha256"]
    ):
        raise AuthoringWorkbenchError(
            f"Config rebase source item changed for {queue_id!r}"
        )
    source = _object(source_item, f"Config rebase source item {queue_id!r}")
    projected = _project_source_item(
        source, exclude_live_fallback=source.get("live_fallback") is not None
    )
    if canonical_document_sha256(projected) != record["projected_item_sha256"]:
        raise AuthoringWorkbenchError(
            f"Config rebase projected source changed for {queue_id!r}"
        )
    return source


def _validate_rebase_current_projection(
    record: Mapping[str, object],
    queue_id: str,
    successor_state: str,
    source_item: JsonObject,
    context: _RebaseValidationContext,
    extensions: Mapping[str, JsonObject],
) -> None:
    current = context.state_items.get(queue_id)
    extension = {key: value for key, value in record.items() if key != "queue_id"}
    if successor_state == REBASE_PENDING_KNOWN_ROLE_REUSE:
        _validate_pending_rebase_history(
            record, queue_id, current, source_item, context.source_root
        )
        return
    if not isinstance(current, dict):
        raise AuthoringWorkbenchError(
            f"Config rebase state item changed for {queue_id!r}"
        )
    current_item = _object(current, f"Config rebase state item {queue_id!r}")
    source_fallback = source_item.get("live_fallback")
    if source_fallback is not None:
        _validate_carried_live_fallback(
            record, queue_id, current_item, source_item, extension, context.directory
        )
        return
    if current_item.get("live_fallback") is not None:
        _validate_rejected_live_fallback(
            record, queue_id, current_item, source_item, extension, context.directory
        )
        return
    _validate_rebased_terminal_item(
        record,
        queue_id,
        current_item,
        source_item,
        extension,
        extensions.get(queue_id, {}),
        context.directory,
    )


def _validate_pending_rebase_history(
    record: Mapping[str, object],
    queue_id: str,
    current: object,
    source_item: Mapping[str, object],
    source_root: Path,
) -> None:
    if isinstance(current, dict) and "config_rebase" in current:
        raise AuthoringWorkbenchError(
            f"Config rebase pending item retained terminal history for {queue_id!r}"
        )
    historical = contained_workspace_path(
        source_root / "generated-audio",
        safe_workspace_relative_path(
            source_item.get("path"), "Config rebase pending-history WAV"
        ),
        "Config rebase pending-history WAV",
    )
    if (
        historical.is_symlink()
        or not historical.is_file()
        or sha256_file(historical) != record["audio_sha256"]
    ):
        raise AuthoringWorkbenchError(
            f"Config rebase pending-history WAV changed for {queue_id!r}"
        )


def _validate_carried_live_fallback(
    record: Mapping[str, object],
    queue_id: str,
    current: JsonObject,
    source_item: JsonObject,
    extension: JsonObject,
    directory: Path,
) -> None:
    expected = _project_source_item(source_item, exclude_live_fallback=True)
    expected["config_rebase"] = extension
    expected["live_fallback"] = copy.deepcopy(source_item["live_fallback"])
    if current != expected:
        raise AuthoringWorkbenchError(
            f"Config rebase carried live fallback changed for {queue_id!r}"
        )
    _validate_rebase_current_audio(record, queue_id, current, directory)


def _validate_rejected_live_fallback(
    record: Mapping[str, object],
    queue_id: str,
    current: JsonObject,
    source_item: JsonObject,
    extension: JsonObject,
    directory: Path,
) -> None:
    expected_base = _project_source_item(source_item, exclude_live_fallback=True)
    expected_base["config_rebase"] = extension
    fallback = current.get("live_fallback")
    if (
        not isinstance(fallback, dict)
        or fallback.get("reason") != "generated_audio_rejected"
        or fallback.get("previous_result_sha256")
        != canonical_document_sha256(expected_base)
    ):
        raise AuthoringWorkbenchError(
            f"Config rebase live fallback base changed for {queue_id!r}"
        )
    current_without_fallback = copy.deepcopy(current)
    current_without_fallback.pop("live_fallback")
    if "updated_at" in expected_base:
        current_without_fallback["updated_at"] = expected_base["updated_at"]
    else:
        current_without_fallback.pop("updated_at", None)
    if current_without_fallback != expected_base:
        raise AuthoringWorkbenchError(
            f"Config rebase item projection changed for {queue_id!r}"
        )
    _validate_rebase_current_audio(record, queue_id, current, directory)


def _validate_rebased_terminal_item(
    record: Mapping[str, object],
    queue_id: str,
    current: JsonObject,
    source_item: JsonObject,
    extension: JsonObject,
    overlays: JsonObject,
    directory: Path,
) -> None:
    if overlays and all(current.get(key) == value for key, value in overlays.items()):
        observed = current.get("config_rebase")
        if observed is not None and observed != extension:
            raise AuthoringWorkbenchError(
                f"Config rebase state item changed for {queue_id!r}"
            )
        return
    if current.get("config_rebase") != extension:
        raise AuthoringWorkbenchError(
            f"Config rebase state item changed for {queue_id!r}"
        )
    if _terminal_authority_changed(record, source_item, current):
        raise AuthoringWorkbenchError(
            f"Config rebase terminal authority changed for {queue_id!r}"
        )
    current_without_extension = copy.deepcopy(current)
    current_without_extension.pop("config_rebase", None)
    if (
        canonical_document_sha256(current_without_extension)
        != record["projected_item_sha256"]
    ):
        raise AuthoringWorkbenchError(
            f"Config rebase item projection changed for {queue_id!r}"
        )
    _validate_rebase_current_audio(record, queue_id, current, directory)


def _terminal_authority_changed(
    record: Mapping[str, object],
    source_item: Mapping[str, object],
    current: Mapping[str, object],
) -> bool:
    return any(
        value != record[field]
        for value, field in (
            (source_item.get("status"), "status"),
            (source_item.get("review_status"), "review_status"),
            (current.get("status"), "status"),
            (current.get("review_status"), "review_status"),
        )
    )


def _validate_rebase_current_audio(
    record: Mapping[str, object],
    queue_id: str,
    current: Mapping[str, object],
    directory: Path,
) -> None:
    audio = contained_workspace_path(
        directory / "generated-audio",
        safe_workspace_relative_path(current.get("path"), "Config rebase WAV"),
        "Config rebase WAV",
    )
    if not audio.is_file() or sha256_file(audio) != record["audio_sha256"]:
        raise AuthoringWorkbenchError(f"Config rebase WAV changed for {queue_id!r}")


def _validate_rebase_record_ledger(
    observed_ids: list[str], state_items: Mapping[str, object]
) -> None:
    if observed_ids != sorted(set(observed_ids)):
        raise AuthoringWorkbenchError("Config rebase item ledger is not canonical")
    marked = sorted(
        queue_id
        for queue_id, item in state_items.items()
        if isinstance(item, dict) and "config_rebase" in item
    )
    expected = sorted(
        queue_id
        for queue_id in observed_ids
        if _is_rebase_state_item(state_items.get(queue_id))
    )
    if marked != expected:
        raise AuthoringWorkbenchError("Config rebase state ledger is incomplete")


def _is_rebase_state_item(value: object) -> bool:
    return isinstance(value, dict) and "config_rebase" in value


def validate_config_rebase_publication_authority(
    state_path: str | Path, state: JsonObject
) -> None:
    """Bind marked state to one validated canonical config-rebase workspace."""
    state_items = _object(state.get("items"), "Config rebase state items")
    marked = any(
        isinstance(item, dict) and "config_rebase" in item
        for item in state_items.values()
    )
    if not marked:
        return
    state_path = Path(state_path).expanduser().resolve()
    workspace_path = state_path.parent.parent / "workspace.json"
    if workspace_path.is_symlink() or not workspace_path.is_file():
        raise BulkGenerationError(
            "Config rebase state requires its canonical workspace ledger"
        )
    try:
        directory, workspace, _workspace_sha256 = load_workspace_authority(
            workspace_path.parent
        )
        canonical_state_path = (
            directory / "generated-audio" / "generation-state.json"
        ).resolve()
        if canonical_state_path != state_path:
            raise BulkGenerationError(
                "Config rebase state is not the canonical workspace state"
            )
        current = load_generation_state(
            canonical_state_path,
            directory / "queue.jsonl",
        )
        if current != state:
            raise BulkGenerationError(
                "Config rebase state changed while publication was prepared"
            )
        if workspace.get("config_rebase") is None:
            raise BulkGenerationError(
                "Config rebase state requires its canonical workspace ledger"
            )
        validate_config_rebase_workspace(directory, workspace, current)
    except AuthoringWorkbenchError as error:
        raise BulkGenerationError(str(error)) from error


def _project_source_item(
    result: Mapping[str, object], *, exclude_live_fallback: bool = False
) -> JsonObject:
    projected = dict(copy.deepcopy(result))
    for field in _WORKFLOW_FIELDS:
        projected.pop(field, None)
    if exclude_live_fallback:
        projected.pop("live_fallback", None)
    return projected


def _retired_route_for_queue(
    records: Sequence[JsonObject], queue_id: str, source_route: Route
) -> JsonObject | None:
    character, reference_sha256s = source_route
    for record in records:
        queue_ids = record.get("queue_ids")
        if (
            isinstance(queue_ids, list)
            and queue_id in queue_ids
            and record["voice_character"] == character
            and record["reference_sha256"] in reference_sha256s
        ):
            return record
    return None


def _target_route_status(
    queue_id: str,
    status: object,
    review_status: object,
    source_route: Route,
    target_route: Route,
    retired_variants: Sequence[JsonObject],
    known_role_reuse: object = None,
    source_item_sha256: object = None,
) -> str:
    if set(source_route[1]).issubset(target_route[1]):
        return "active"
    if (
        status == "generated"
        and review_status == "rejected"
        and _retired_route_for_queue(retired_variants, queue_id, source_route)
        is not None
    ):
        return "retired_rejected"
    controls = (
        known_role_reuse.get("source_rejected_state_item_sha256s", {})
        if isinstance(known_role_reuse, dict)
        else {}
    )
    expected_references = (
        known_role_reuse.get("reuse_reference_sha256s")
        if isinstance(known_role_reuse, dict)
        else None
    )
    if (
        isinstance(known_role_reuse, dict)
        and status == "generated"
        and review_status == "rejected"
        and controls.get(queue_id) == source_item_sha256
        and target_route[0] == known_role_reuse.get("reuse_voice_character")
        and isinstance(expected_references, list)
        and list(target_route[1]) == expected_references
    ):
        return "known_role_reuse_rejected"
    raise AuthoringWorkbenchError(
        f"Config rebase changes the effective reference for terminal item {queue_id!r}"
    )


def _known_role_reuse_requeues_rejection(
    queue_id: str,
    status: object,
    review_status: object,
    target_route: Route,
    route_status: str,
    known_role_reuse: object,
    source_item_sha256: object,
) -> bool:
    """Return true only for an exact rejected item authorized for a new voice."""
    if (
        not isinstance(known_role_reuse, dict)
        or status != "generated"
        or review_status != "rejected"
        or route_status not in {"known_role_reuse_rejected", "retired_rejected"}
    ):
        return False
    controls = known_role_reuse.get("source_rejected_state_item_sha256s")
    expected_references = known_role_reuse.get("reuse_reference_sha256s")
    overrides = known_role_reuse.get("queue_voice_overrides")
    return (
        isinstance(controls, dict)
        and controls.get(queue_id) == source_item_sha256
        and isinstance(overrides, dict)
        and overrides.get(queue_id) == known_role_reuse.get("reuse_voice_character")
        and target_route[0] == known_role_reuse.get("reuse_voice_character")
        and isinstance(expected_references, list)
        and list(target_route[1]) == expected_references
    )


def _route_reference_identity(
    registry: CharacterVoiceRegistry,
    workspace: Mapping[str, object],
    overrides: Mapping[str, str],
    queue_item: VoiceGenerationQueueItem,
    result: JsonObject | None = None,
    *,
    source_result: JsonObject | None = None,
    failure_reference_binding: FailureReferenceRuntimeBinding | None = None,
    allow_missing: bool = False,
) -> Route:
    route = _audio_event_reference_route(result, source_result)
    if route is not None:
        return route
    result_for_failure = result if result is not None else source_result
    failure_route = _failure_reference_route(
        failure_reference_binding, queue_item, result_for_failure
    )
    if failure_route is not None and result is not None:
        return failure_route[0]
    requested = (
        failure_route[1]
        if failure_route is not None
        else synthesis_character_for_line(
            queue_item.speaker, queue_item.voice_character
        )
    )
    prior_route = _prior_config_rebase_target_route(result)
    if prior_route is not None:
        return prior_route
    character = _resolved_route_character(
        result, overrides, queue_item.queue_id, requested, workspace
    )
    return _voice_reference_route(
        registry, workspace, character, requested, allow_missing
    )


def _audio_event_reference_route(
    result: JsonObject | None, source_result: JsonObject | None
) -> Route | None:
    event = result if result is not None else source_result
    composition = (
        event.get("audio_event_composition") if isinstance(event, dict) else None
    )
    if (
        not isinstance(event, dict)
        or event.get("provider") != "original-game-audio-event"
        or not isinstance(composition, dict)
    ):
        return None
    return "Audio Event", (
        require_workspace_sha256(
            composition.get("final_audio_sha256"),
            "Config rebase audio-event result WAV SHA-256",
        ),
    )


def _resolved_route_character(
    result: JsonObject | None,
    overrides: Mapping[str, str],
    queue_id: str,
    requested: str,
    workspace: Mapping[str, object],
) -> str:
    result_character = (
        result.get("voice_character") if isinstance(result, dict) else None
    )
    character = result_character or overrides.get(queue_id) or requested
    if character == "Narrator":
        character = workspace.get("narrator_character")
    return _text(character, "Config rebase voice character")


def _voice_reference_route(
    registry: CharacterVoiceRegistry,
    workspace: Mapping[str, object],
    character: str,
    requested: str,
    allow_missing: bool,
) -> Route:
    voice = registry.resolve(character)
    if voice is None or not voice.references:
        policy = workspace_missing_voice_policy(
            workspace, error_type=AuthoringWorkbenchError
        )
        if policy.applies_to(requested):
            character = _text(
                workspace.get("narrator_character"),
                "Config rebase narrator character",
            )
            voice = registry.resolve(character)
    if voice is None or not voice.references:
        if allow_missing:
            return character, ()
        raise AuthoringWorkbenchError(
            f"Config rebase voice references are missing for {character!r}"
        )
    digests = tuple(sorted(sha256_file(reference) for reference in voice.references))
    return character, digests


def _failure_reference_route(
    binding: FailureReferenceRuntimeBinding | None,
    queue_item: VoiceGenerationQueueItem,
    result: object,
) -> tuple[Route, str] | None:
    if not isinstance(result, dict):
        return None
    queue_id = queue_item.queue_id
    source_binding = result.get("source_reference_binding")
    if not isinstance(source_binding, dict):
        return None
    required = {
        "schema_version",
        "queue_id",
        "source_voice_character",
        "synthesis_voice_character",
        "queue_voice_overrides_sha256",
    }
    synthetic_character = source_binding.get("synthesis_voice_character")
    runtime_character = (
        None if binding is None else binding.queue_voice_overrides.get(queue_id)
    )
    historical_references = _historical_failure_reference_digests(
        result, synthetic_character
    )
    if runtime_character is None and historical_references is None:
        return None
    if (
        set(source_binding) != required
        or source_binding.get("schema_version") != 1
        or source_binding.get("queue_id") != queue_id
        or not isinstance(synthetic_character, str)
        or not synthetic_character.strip()
        or (runtime_character is not None and synthetic_character != runtime_character)
        or result.get("voice_character") != synthetic_character
    ):
        raise AuthoringWorkbenchError(
            f"Config rebase failure-reference binding changed for {queue_id!r}"
        )
    requested = source_binding.get("source_voice_character")
    if not isinstance(requested, str) or not requested.strip():
        raise AuthoringWorkbenchError(
            f"Config rebase failure-reference source voice is invalid for {queue_id!r}"
        )
    digests: tuple[str, ...]
    if runtime_character is not None and binding is not None:
        voices = [
            voice for voice in binding.voices if voice.character == synthetic_character
        ]
        if len(voices) != 1 or not voices[0].references:
            raise AuthoringWorkbenchError(
                f"Config rebase failure-reference controls changed for {queue_id!r}"
            )
        digests = tuple(
            sorted(sha256_file(reference) for reference in voices[0].references)
        )
        if historical_references is not None and digests != historical_references:
            raise AuthoringWorkbenchError(
                f"Config rebase failure-reference history changed for {queue_id!r}"
            )
    else:
        if historical_references is None:
            raise AuthoringWorkbenchError(
                f"Config rebase failure-reference history is absent for {queue_id!r}"
            )
        digests = historical_references
    return (synthetic_character, digests), requested.strip()


def _historical_failure_reference_digests(
    result: Mapping[str, object], synthetic_character: object
) -> tuple[str, ...] | None:
    repair = result.get("failure_repair")
    if (
        not isinstance(repair, dict)
        or repair.get("strategy") != "offline_fallback_backend"
    ):
        return None
    source = repair.get("source_failure")
    voice = source.get("source_voice_reference") if isinstance(source, dict) else None
    if (
        not isinstance(voice, dict)
        or voice.get("character") != synthetic_character
        or not isinstance(voice.get("references"), list)
        or not voice["references"]
    ):
        return None
    return tuple(
        sorted(
            require_workspace_sha256(
                digest, "Config rebase historical failure reference SHA-256"
            )
            for digest in voice["references"]
        )
    )


def _prior_config_rebase_target_route(result: object) -> Route | None:
    """Return the effective route owned by an immediately preceding rebase.

    The complete preceding item, including its earlier source provenance, is
    still bound by ``source_item_sha256`` in the new ledger.  Each successor
    therefore records the preceding workspace's effective target route rather
    than flattening or re-resolving a historical synthesis character that may
    no longer be active in the selected manifest.  A retired rejection keeps
    its exact source route because its intentionally empty target route is not
    active synthesis authority and must be revalidated against retirement.
    """
    if not isinstance(result, dict):
        return None
    rebase = result.get("config_rebase")
    if not isinstance(rebase, dict):
        return None
    retired = rebase.get("target_route_status") in {
        "retired_rejected",
        "known_role_reuse_rejected",
    }
    character_field = (
        "source_effective_character" if retired else "target_effective_character"
    )
    references_field = (
        "source_reference_sha256s" if retired else "target_reference_sha256s"
    )
    character = rebase.get(character_field)
    values = rebase.get(references_field)
    if not isinstance(character, str) or not character.strip():
        raise AuthoringWorkbenchError(
            "Prior config rebase target character is malformed"
        )
    if not isinstance(values, list) or not values or values != sorted(set(values)):
        raise AuthoringWorkbenchError(
            "Prior config rebase target references are malformed"
        )
    digests = tuple(
        require_workspace_sha256(value, "Prior config rebase target reference SHA-256")
        for value in values
    )
    return character, digests


def _rebase_audio_event_composition(
    source_workspace: Mapping[str, object], target_workspace: Mapping[str, object]
) -> JsonObject | None:
    source = source_workspace.get("audio_event_composition")
    target = target_workspace.get("audio_event_composition")
    if source is not None and not isinstance(source, dict):
        raise AuthoringWorkbenchError(
            "Config rebase source audio-event composition is malformed"
        )
    if target is not None and not isinstance(target, dict):
        raise AuthoringWorkbenchError(
            "Config rebase target audio-event composition is malformed"
        )
    if source is not None and target is not None and source != target:
        raise AuthoringWorkbenchError(
            "Config rebase audio-event composition authorities conflict"
        )
    return copy.deepcopy(target if target is not None else source)


def _copy_audio_event_composition_inputs(
    source_directory: Path,
    staging: Path,
    composition: Mapping[str, object],
    snapshots: Snapshots,
) -> None:
    paths = _audio_event_composition_paths(composition)
    if not paths:
        raise AuthoringWorkbenchError(
            "Config rebase audio-event composition has no input paths"
        )
    roots = _audio_event_composition_roots(source_directory, paths)
    for root in sorted(roots):
        _copy_audio_event_composition_root(source_directory, staging, root, snapshots)


def _audio_event_composition_paths(composition: Mapping[str, object]) -> list[str]:
    return sorted(
        value
        for key, value in composition.items()
        if (key == "path" or key.endswith("_path")) and isinstance(value, str) and value
    )


def _audio_event_composition_roots(
    source_directory: Path, paths: Sequence[str]
) -> set[Path]:
    roots: set[Path] = set()
    for value in paths:
        relative = safe_workspace_relative_path(
            value, "Config rebase audio-event composition input"
        )
        if len(relative.parts) < 3 or relative.parts[0] != "inputs":
            raise AuthoringWorkbenchError(
                "Config rebase audio-event composition input leaves inputs"
            )
        source_file = contained_workspace_path(
            source_directory,
            relative,
            "Config rebase audio-event composition source",
        )
        if source_file.is_symlink() or not source_file.is_file():
            raise AuthoringWorkbenchError(
                f"Config rebase audio-event composition input is missing: {value}"
            )
        roots.add(Path(*relative.parts[:2]))
    return roots


def _copy_audio_event_composition_root(
    source_directory: Path, staging: Path, relative_root: Path, snapshots: Snapshots
) -> None:
    source_root = contained_workspace_path(
        source_directory,
        relative_root,
        "Config rebase audio-event composition source root",
    )
    if source_root.is_symlink() or not source_root.is_dir():
        raise AuthoringWorkbenchError(
            "Config rebase audio-event composition source root is invalid"
        )
    for source in sorted(source_root.rglob("*")):
        _copy_audio_event_composition_file(
            source, source_root, staging, relative_root, snapshots
        )


def _copy_audio_event_composition_file(
    source: Path,
    source_root: Path,
    staging: Path,
    relative_root: Path,
    snapshots: Snapshots,
) -> None:
    if source.is_symlink():
        raise AuthoringWorkbenchError(
            "Config rebase audio-event composition contains a symlink"
        )
    if source.is_dir():
        return
    relative = relative_root / source.relative_to(source_root)
    payload = read_workspace_file_bytes(source, "config rebase audio-event composition")
    digest = hashlib.sha256(payload).hexdigest()
    destination = contained_workspace_path(
        staging, relative, "Config rebase audio-event composition target"
    )
    _write_audio_event_composition_input(destination, payload, digest)
    snapshots.append((source, digest))


def _write_audio_event_composition_input(
    destination: Path, payload: bytes, digest: str
) -> None:
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise AuthoringWorkbenchError(
                "Config rebase audio-event composition target is unsafe"
            )
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise AuthoringWorkbenchError(
                "Config rebase audio-event composition input conflicts"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)


def _copy_tree(source: Path, destination: Path, snapshots: Snapshots) -> None:
    if source.is_symlink() or not source.is_dir():
        raise AuthoringWorkbenchError(f"Config rebase input tree is invalid: {source}")
    destination.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise AuthoringWorkbenchError(
                f"Config rebase input tree contains a symlink: {path}"
            )
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        payload = read_workspace_file_bytes(path, "config rebase input")
        digest = hashlib.sha256(payload).hexdigest()
        target = contained_workspace_path(destination, relative, "Config rebase input")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        snapshots.append((path, digest))


def _object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AuthoringWorkbenchError(f"{label} is malformed")
    return {key: item for key, item in value.items()}


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthoringWorkbenchError(f"{label} is invalid")
    return value


def _load_json_queue(path: str | Path) -> dict[str, VoiceGenerationQueueItem]:
    try:
        queue = VoiceGenerationQueue.load(path)
    except Exception as error:
        raise AuthoringWorkbenchError(str(error)) from error
    return {item.queue_id: item for item in queue.items}


__all__ = [
    "CONFIG_REBASE_SCHEMA",
    "CONFIG_REBASE_VERSION",
    "SUPPORTED_CONFIG_REBASE_VERSIONS",
    "rebase_workspace_config",
    "validate_config_rebase_publication_authority",
    "validate_config_rebase_workspace",
]

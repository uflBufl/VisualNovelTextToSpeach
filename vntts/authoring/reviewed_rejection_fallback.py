"""Publish explicit live routes for exact already-rejected generated WAVs."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import cast

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueueItem
from vntts_artifacts.voice_manifest import (
    VoiceManifestEntry,
    load_voice_manifest,
    normalize_character_name,
)

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    process_is_alive,
)
from vntts.authoring.bulk_generation import (
    _state_items as _generation_state_items,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_REVIEWED_REJECTION_VERSION,
    LIVE_FALLBACK_SCHEMA,
    REVIEWED_REJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.source_reference_bindings import (
    queue_voice_overrides_from_manifest,
)
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
    validate_workspace_provenance_extensions,
)
from vntts.authoring.workspace_config import (
    selected_voice_manifest_path,
    workspace_config_fingerprint,
)
from vntts.authoring.workspace_foundation import (
    copy_generation_wavs,
    copy_workspace_tree_snapshot,
)
from vntts.authoring.workspace_state import load_stable_workspace_generation_state
from vntts.voices import synthesis_character_for_line

_copy_base_wavs = partial(
    copy_generation_wavs,
    target_label="Reviewed-rejection WAV",
    error_type=AuthoringWorkbenchError,
)

SCHEMA = "vntts.authoring-reviewed-rejection-live-fallback-batch"
SCHEMA_VERSION = 1
REASON = "generated_audio_rejected"


@dataclass(frozen=True)
class _RejectionSelection:
    base_directory: Path
    base_document: dict[str, object]
    base_workspace_sha256: str
    state: dict[str, object]
    state_sha256: str
    queue_path: Path
    queue_sha256: str
    import_id: str
    narrator_character: str
    voice_sha256: str
    ledgers: list[dict[str, object]]


@dataclass(frozen=True)
class _RejectionIdentity:
    batch: dict[str, object]
    config_fingerprint: str
    root: Path
    destination: Path
    workspace_id: str


@dataclass
class _RejectionStaging:
    output: Path
    snapshots: list[tuple[Path, str]]
    target_state: dict[str, object]


def create_reviewed_rejection_fallback_workspace(
    base_workspace: str | Path,
    workspaces_root: str | Path | None = None,
) -> WorkspaceCreationResult:
    """Route every exact rejected result without an existing fallback."""
    selection = _select_rejection_fallback(base_workspace)
    identity = _rejection_identity(selection, workspaces_root)
    existing = _existing_rejection_workspace(identity)
    if existing is not None:
        return existing
    try:
        with staged_directory(identity.root, prefix=".reviewed-rejection-") as staging:
            staged = _stage_rejection_workspace(selection, staging)
            workspace = _mutate_rejection_state(selection, identity, staged)
            _validate_staged_rejection_workspace(staging, staged, workspace)
            _publish_rejection_workspace(selection, identity, staging, staged.snapshots)
    except (BulkGenerationError, OSError, ValueError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    return WorkspaceCreationResult(identity.destination, True)


def _select_rejection_fallback(base_workspace: str | Path) -> _RejectionSelection:
    base_directory, base_document, base_workspace_sha256 = load_workspace_authority(
        base_workspace
    )
    queue, state, _payload, state_sha256 = load_stable_workspace_generation_state(
        base_directory,
        base_document,
        "reviewed-rejection fallback base",
        error_type=AuthoringWorkbenchError,
    )
    if state.get("active") is not None:
        raise AuthoringWorkbenchError("Reviewed-rejection fallback base is active")
    state_items = _generation_state_items(state)
    import_id, narrator_character = _workspace_creation_fields(base_document)
    queue_path = base_directory / "queue.jsonl"
    queue_sha256 = sha256_file(queue_path)
    queue_by_id = {item.queue_id: item for item in queue.items}
    voice_path = selected_voice_manifest_path(
        base_directory, base_document, error_type=AuthoringWorkbenchError
    )
    if voice_path is None:
        raise AuthoringWorkbenchError(
            "Reviewed-rejection fallback requires a selected voice manifest"
        )
    voice_sha256 = sha256_file(voice_path)
    try:
        overrides, voice_entries = _rejection_voice_overrides(voice_path, queue_by_id)
    except (OSError, TypeError, ValueError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    reference_sha256s = _voice_reference_sha256s(voice_path, voice_entries)

    ledgers = []
    for queue_id, base_result in sorted(state_items.items()):
        ledger = _rejection_ledger(
            queue_id, base_result, queue_by_id, overrides, reference_sha256s
        )
        if ledger is not None:
            ledgers.append(ledger)
    if not ledgers:
        raise AuthoringWorkbenchError(
            "Reviewed-rejection fallback base has no unresolved rejected WAVs"
        )
    return _RejectionSelection(
        base_directory,
        base_document,
        base_workspace_sha256,
        state,
        state_sha256,
        queue_path,
        queue_sha256,
        import_id,
        narrator_character,
        voice_sha256,
        ledgers,
    )


def _rejection_ledger(
    queue_id: str,
    base_result: Mapping[str, object],
    queue_by_id: Mapping[str, VoiceGenerationQueueItem],
    overrides: Mapping[str, str],
    reference_sha256s: Mapping[str, list[str]],
) -> dict[str, object] | None:
    if (
        base_result.get("status") != "generated"
        or base_result.get("review_status") != "rejected"
        or isinstance(base_result.get("live_fallback"), dict)
    ):
        return None
    queue_item = queue_by_id.get(queue_id)
    if queue_item is None:
        raise AuthoringWorkbenchError(
            f"Reviewed-rejection queue ID is unavailable: {queue_id!r}"
        )
    rebase = base_result.get("config_rebase")
    if isinstance(rebase, dict):
        if (
            rebase.get("target_route_status") != "active"
            or not isinstance(rebase.get("target_effective_character"), str)
            or not rebase["target_effective_character"].strip()
            or not isinstance(rebase.get("target_reference_sha256s"), list)
            or not rebase["target_reference_sha256s"]
        ):
            raise AuthoringWorkbenchError(
                f"Reviewed-rejection config route is invalid: {queue_id!r}"
            )
        route_source = "config_rebase"
        synthesis_character = rebase["target_effective_character"]
        references = sorted(set(rebase["target_reference_sha256s"]))
    else:
        route_source = "voice_manifest"
        synthesis_character, references = _manifest_route(
            queue_item, base_result, overrides, reference_sha256s
        )
    return {
        "queue_id": queue_id,
        "line_id": queue_item.line_id,
        "text_sha256": queue_item.text_sha256,
        "speaker": queue_item.speaker,
        "base_result_sha256": canonical_document_sha256(base_result),
        "synthesis_character": synthesis_character,
        "route_source": route_source,
        "route_reference_sha256s": references,
    }


def _rejection_identity(
    selection: _RejectionSelection, workspaces_root: str | Path | None
) -> _RejectionIdentity:
    base_document = selection.base_document
    batch_body = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "reason": REASON,
        "base_workspace_id": base_document["workspace_id"],
        "base_workspace_path": "inputs/reviewed-rejection/base-workspace.json",
        "base_workspace_sha256": selection.base_workspace_sha256,
        "base_state_path": "inputs/reviewed-rejection/base-generation-state.json",
        "base_state_sha256": selection.state_sha256,
        "queue_sha256": selection.queue_sha256,
        "voice_manifest_sha256": selection.voice_sha256,
        "items": selection.ledgers,
    }
    batch = {**batch_body, "batch_id": canonical_document_sha256(batch_body)}
    config_fingerprint = workspace_config_fingerprint(
        selection.import_id,
        base_document.get("story_index"),
        base_document.get("voice_manifest"),
        selection.narrator_character,
        base_document["run_config"],
        base_document.get("carry_forward"),
        base_document.get("outcome_merge"),
        base_document.get("failure_reference_binding"),
        base_document.get("terminal_conflict_merge"),
        base_document.get("config_rebase"),
        base_document.get("audio_event_composition"),
        base_document.get("explicit_fallback_merge"),
        base_document.get("known_role_live_fallback"),
        base_document.get("audio_event_omission"),
        base_document.get("audio_event_projection_fallback"),
        base_document.get("reviewed_waveform_publication"),
        batch,
        queue_extension=base_document.get("queue_extension"),
    )
    workspace_id = f"resume-{selection.import_id.removeprefix('legacy-')}-{config_fingerprint[:16]}"
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Reviewed-rejection fallback destination"
    )
    return _RejectionIdentity(
        batch, config_fingerprint, root, destination, workspace_id
    )


def _existing_rejection_workspace(
    identity: _RejectionIdentity,
) -> WorkspaceCreationResult | None:
    if identity.destination.exists():
        _directory, existing, _sha256 = load_workspace_authority(identity.destination)
        if existing.get("reviewed_rejection_live_fallback") != identity.batch:
            raise AuthoringWorkbenchError(
                "Reviewed-rejection fallback destination conflicts"
            )
        return WorkspaceCreationResult(identity.destination, False)
    return None


def _stage_rejection_workspace(
    selection: _RejectionSelection, staging: Path
) -> _RejectionStaging:
    snapshots = [
        (selection.base_directory / "workspace.json", selection.base_workspace_sha256),
        (
            selection.base_directory / "generated-audio/generation-state.json",
            selection.state_sha256,
        ),
        (selection.queue_path, selection.queue_sha256),
    ]
    for tree_name in ("provenance", "inputs"):
        copy_workspace_tree_snapshot(
            selection.base_directory / tree_name,
            staging / tree_name,
            snapshots,
            error_type=AuthoringWorkbenchError,
        )
    inputs = staging / "inputs/reviewed-rejection"
    inputs.mkdir(parents=True)
    (inputs / "base-workspace.json").write_bytes(
        read_workspace_file_bytes(
            selection.base_directory / "workspace.json",
            "reviewed-rejection base workspace",
        )
    )
    (inputs / "base-generation-state.json").write_bytes(
        read_workspace_file_bytes(
            selection.base_directory / "generated-audio/generation-state.json",
            "reviewed-rejection base state",
        )
    )
    (staging / "queue.jsonl").write_bytes(
        read_workspace_file_bytes(selection.queue_path, "reviewed-rejection queue")
    )
    output = staging / "generated-audio"
    output.mkdir()
    target_state = copy.deepcopy(selection.state)
    _copy_base_wavs(selection.base_directory, output, selection.state, snapshots)
    return _RejectionStaging(output, snapshots, target_state)


def _mutate_rejection_state(
    selection: _RejectionSelection,
    identity: _RejectionIdentity,
    staged: _RejectionStaging,
) -> dict[str, object]:
    target_items = _generation_state_items(staged.target_state)
    state_items = _generation_state_items(selection.state)
    decided_at = datetime.now(timezone.utc).isoformat()
    for ledger in selection.ledgers:
        queue_id = ledger["queue_id"]
        base_result = state_items[queue_id]
        evidence = {
            "schema": REVIEWED_REJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA,
            "schema_version": 1,
            "batch_id": identity.batch["batch_id"],
            "base_workspace_id": selection.base_document["workspace_id"],
            "base_workspace_sha256": selection.base_workspace_sha256,
            "base_state_sha256": selection.state_sha256,
            "queue_sha256": selection.queue_sha256,
            "voice_manifest_sha256": selection.voice_sha256,
            "queue_id": queue_id,
            "base_result_sha256": ledger["base_result_sha256"],
            "base_result": copy.deepcopy(base_result),
            "source_character": ledger["speaker"],
            "synthesis_character": ledger["synthesis_character"],
            "route_source": ledger["route_source"],
            "route_reference_sha256s": ledger["route_reference_sha256s"],
        }
        decision = {
            "schema": LIVE_FALLBACK_SCHEMA,
            "schema_version": LIVE_FALLBACK_REVIEWED_REJECTION_VERSION,
            "reason": REASON,
            "provider": "pocket-tts",
            "model": "pocket-tts",
            "generation_profile": "default",
            "queue_id": queue_id,
            "line_id": ledger["line_id"],
            "text_sha256": ledger["text_sha256"],
            "speaker": ledger["speaker"],
            "requested_voice_character": ledger["synthesis_character"],
            "previous_result_sha256": ledger["base_result_sha256"],
            "decided_at": decided_at,
            "evidence": evidence,
        }
        projected = copy.deepcopy(base_result)
        projected["live_fallback"] = decision
        projected["updated_at"] = decided_at
        target_items[queue_id] = projected
    staged.target_state["active"] = None
    atomic_write_json(
        staged.output / "generation-state.json", staged.target_state, sort_keys=True
    )
    write_generated_manifest_from_state(
        staged.target_state, staged.output, staged.output / "manifest.json"
    )
    workspace = copy.deepcopy(selection.base_document)
    workspace.update(
        {
            "workspace_id": identity.workspace_id,
            "reviewed_rejection_live_fallback": copy.deepcopy(identity.batch),
            "config_fingerprint": identity.config_fingerprint,
        }
    )
    atomic_write_json(
        staged.output.parent / "workspace.json", workspace, sort_keys=True
    )
    return workspace


def _validate_staged_rejection_workspace(
    staging: Path, staged: _RejectionStaging, workspace: dict[str, object]
) -> None:
    import_snapshot = load_workspace_json(
        staging / "provenance/import.json", "reviewed-rejection import"
    )
    validate_workspace_provenance_extensions(staging, workspace, import_snapshot)
    load_generation_state(
        staged.output / "generation-state.json", staging / "queue.jsonl"
    )


def _publish_rejection_workspace(
    selection: _RejectionSelection,
    identity: _RejectionIdentity,
    staging: Path,
    snapshots: list[tuple[Path, str]],
) -> None:
    try:
        with generation_publication_leases(
            ((selection.base_directory / "generated-audio", selection.queue_sha256),),
            process_checker=process_is_alive,
        ) as leases:
            if any(
                (selection.base_directory / "generated-audio").rglob("*.partial.wav")
            ):
                raise AuthoringWorkbenchError(
                    "Reviewed-rejection fallback base became active"
                )
            for path, digest in snapshots:
                if not path.is_file() or sha256_file(path) != digest:
                    raise AuthoringWorkbenchError(
                        "Reviewed-rejection authority changed before publication"
                    )
            leases[0].assert_owned()
            try:
                rename_directory_no_replace(staging, identity.destination)
            except (AtomicPublicationError, OSError) as error:
                raise AuthoringWorkbenchError(
                    f"Unable to publish reviewed-rejection workspace: {error}"
                ) from error
            leases[0].mark_committed()
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def validate_reviewed_rejection_fallback_workspace(
    directory: str | Path, workspace: Mapping[str, object]
) -> None:
    """Validate the self-contained rejected-result route batch."""
    batch = workspace.get("reviewed_rejection_live_fallback")
    if batch is None:
        return
    batch = _validated_rejection_batch(batch)
    root = Path(directory)
    _validate_rejection_authority_snapshots(root, batch)
    base_state = load_workspace_json(
        root / cast(str, batch["base_state_path"]), "reviewed-rejection base state"
    )
    queue, state, _payload, _state_sha256 = load_stable_workspace_generation_state(
        root,
        workspace,
        "reviewed-rejection fallback workspace",
        error_type=AuthoringWorkbenchError,
    )
    if sha256_file(root / "queue.jsonl") != batch["queue_sha256"]:
        raise AuthoringWorkbenchError("Reviewed-rejection queue changed")
    manifest_path = selected_voice_manifest_path(
        root, workspace, error_type=AuthoringWorkbenchError
    )
    if (
        manifest_path is None
        or sha256_file(manifest_path) != batch["voice_manifest_sha256"]
    ):
        raise AuthoringWorkbenchError("Reviewed-rejection voice manifest changed")
    queue_by_id = {item.queue_id: item for item in queue.items}
    try:
        overrides, voice_entries = _rejection_voice_overrides(
            manifest_path, queue_by_id
        )
    except (OSError, TypeError, ValueError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    reference_sha256s = _voice_reference_sha256s(manifest_path, voice_entries)
    base_items = _generation_state_items(base_state)
    state_items = _generation_state_items(state)
    expected = _expected_rejection_queue_ids(base_items)
    observed = [
        _validate_rejection_ledger(
            ledger,
            batch,
            queue_by_id,
            base_items,
            state_items,
            overrides,
            reference_sha256s,
        )
        for ledger in cast(list[object], batch.get("items", []))
    ]
    if not observed or observed != expected:
        raise AuthoringWorkbenchError(
            "Reviewed-rejection fallback item coverage changed"
        )
    _validate_rejection_state(
        workspace, base_state, state, base_items, state_items, set(observed)
    )


def _validated_rejection_batch(value: object) -> dict[str, object]:
    batch = value
    fields = {
        "schema",
        "schema_version",
        "batch_id",
        "reason",
        "base_workspace_id",
        "base_workspace_path",
        "base_workspace_sha256",
        "base_state_path",
        "base_state_sha256",
        "queue_sha256",
        "voice_manifest_sha256",
        "items",
    }
    if (
        not isinstance(batch, dict)
        or set(batch) != fields
        or batch.get("schema") != SCHEMA
        or batch.get("schema_version") != SCHEMA_VERSION
        or batch.get("reason") != REASON
        or batch.get("batch_id")
        != canonical_document_sha256(
            {key: value for key, value in batch.items() if key != "batch_id"}
        )
    ):
        raise AuthoringWorkbenchError("Reviewed-rejection fallback batch is malformed")
    for field in (
        "batch_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "voice_manifest_sha256",
    ):
        require_workspace_sha256(batch.get(field), f"Reviewed-rejection {field}")
    return batch


def _validate_rejection_authority_snapshots(
    root: Path, batch: dict[str, object]
) -> None:
    for path_field, hash_field, label in (
        ("base_workspace_path", "base_workspace_sha256", "base workspace"),
        ("base_state_path", "base_state_sha256", "base state"),
    ):
        source = contained_workspace_path(
            root,
            safe_workspace_relative_path(
                batch.get(path_field), f"Reviewed-rejection {label}"
            ),
            f"Reviewed-rejection {label}",
        )
        if not source.is_file() or sha256_file(source) != batch.get(hash_field):
            raise AuthoringWorkbenchError(
                f"Reviewed-rejection {label} authority changed"
            )


def _expected_rejection_queue_ids(
    base_items: Mapping[str, dict[str, object]],
) -> list[str]:
    return sorted(
        queue_id
        for queue_id, result in base_items.items()
        if isinstance(result, dict)
        and result.get("status") == "generated"
        and result.get("review_status") == "rejected"
        and not isinstance(result.get("live_fallback"), dict)
    )


def _validate_rejection_ledger(
    ledger: object,
    batch: Mapping[str, object],
    queue_by_id: Mapping[str, VoiceGenerationQueueItem],
    base_items: Mapping[str, dict[str, object]],
    state_items: Mapping[str, dict[str, object]],
    overrides: Mapping[str, str],
    reference_sha256s: Mapping[str, list[str]],
) -> str:
    ledger_fields = {
        "queue_id",
        "line_id",
        "text_sha256",
        "speaker",
        "base_result_sha256",
        "synthesis_character",
        "route_source",
        "route_reference_sha256s",
    }
    if not isinstance(ledger, dict) or set(ledger) != ledger_fields:
        raise AuthoringWorkbenchError("Reviewed-rejection fallback item is malformed")
    queue_id = _required_text(ledger.get("queue_id"), "Reviewed-rejection queue ID")
    base_result = base_items.get(queue_id)
    result = state_items.get(queue_id)
    decision = result.get("live_fallback") if isinstance(result, dict) else None
    evidence = decision.get("evidence") if isinstance(decision, dict) else None
    queue_item = queue_by_id.get(queue_id)
    if (
        queue_item is None
        or not isinstance(base_result, dict)
        or canonical_document_sha256(base_result) != ledger.get("base_result_sha256")
        or ledger.get("line_id") != queue_item.line_id
        or ledger.get("text_sha256") != queue_item.text_sha256
        or ledger.get("speaker") != queue_item.speaker
        or not isinstance(evidence, dict)
        or evidence.get("base_result") != base_result
        or evidence.get("batch_id") != batch.get("batch_id")
        or evidence.get("synthesis_character") != ledger.get("synthesis_character")
        or evidence.get("route_source") != ledger.get("route_source")
        or evidence.get("route_reference_sha256s")
        != ledger.get("route_reference_sha256s")
    ):
        raise AuthoringWorkbenchError(
            f"Reviewed-rejection result changed for {queue_id!r}"
        )
    if ledger["route_source"] == "voice_manifest":
        character, references = _manifest_route(
            queue_item,
            base_result,
            overrides,
            reference_sha256s,
        )
        if (
            character != ledger["synthesis_character"]
            or references != ledger["route_reference_sha256s"]
        ):
            raise AuthoringWorkbenchError(
                f"Reviewed-rejection manifest route changed for {queue_id!r}"
            )
    return queue_id


def _validate_rejection_state(
    workspace: Mapping[str, object],
    base_state: Mapping[str, object],
    state: Mapping[str, object],
    base_items: Mapping[str, dict[str, object]],
    state_items: Mapping[str, dict[str, object]],
    observed_set: set[str],
) -> None:
    state_metadata = {key: value for key, value in state.items() if key != "items"}
    if workspace.get("reviewed_waveform_publication") is not None:
        state_metadata.pop("reviewed_waveform_publication", None)
    if state_metadata != {
        key: value for key, value in base_state.items() if key != "items"
    }:
        raise AuthoringWorkbenchError("Reviewed-rejection state metadata changed")
    downstream_queue_ids = _downstream_overlay_queue_ids(workspace)
    for queue_id, result in state_items.items():
        base_result = base_items.get(queue_id)
        if queue_id not in observed_set:
            if queue_id in downstream_queue_ids:
                continue
            if result != base_result:
                raise AuthoringWorkbenchError(
                    f"Reviewed-rejection unrelated result changed for {queue_id!r}"
                )
            continue
        if base_result is None:
            raise AuthoringWorkbenchError(
                f"Reviewed-rejection base result is unavailable for {queue_id!r}"
            )
        projected = copy.deepcopy(result)
        projected.pop("live_fallback", None)
        if "updated_at" in base_result:
            projected["updated_at"] = base_result["updated_at"]
        else:
            projected.pop("updated_at", None)
        if projected != base_result:
            raise AuthoringWorkbenchError(
                f"Reviewed-rejection base result changed for {queue_id!r}"
            )


def _voice_reference_sha256s(
    voice_path: Path, entries: Sequence[VoiceManifestEntry]
) -> dict[str, list[str]]:
    result = {}
    for entry in entries:
        digests = []
        for relative in entry.references:
            source = contained_workspace_path(
                voice_path.parent,
                safe_workspace_relative_path(relative, "Voice reference"),
                "Voice reference",
            )
            if not source.is_file():
                raise AuthoringWorkbenchError("Voice reference is unavailable")
            digests.append(sha256_file(source))
        for name in (entry.character, *entry.aliases):
            result[normalize_character_name(name)] = sorted(set(digests))
    return result


def _rejection_voice_overrides(
    voice_path: Path, queue_by_id: Mapping[str, VoiceGenerationQueueItem]
) -> tuple[Mapping[str, str], Sequence[VoiceManifestEntry]]:
    voice_document, voice_entries = load_voice_manifest(voice_path, allow_legacy=False)
    overrides = queue_voice_overrides_from_manifest(
        voice_document, queue_ids=queue_by_id, voices=voice_entries
    )
    return overrides, voice_entries


def _manifest_route(
    queue_item: VoiceGenerationQueueItem,
    result: Mapping[str, object],
    overrides: Mapping[str, str],
    reference_sha256s: Mapping[str, list[str]],
) -> tuple[str, list[str]]:
    requested = synthesis_character_for_line(
        queue_item.speaker, queue_item.voice_character
    )
    expected = overrides.get(queue_item.queue_id)
    if expected is None:
        fallback = result.get("synthesis_fallback")
        expected = (
            fallback.get("synthesis_voice_character")
            if isinstance(fallback, dict)
            else requested
        )
    synthesis_character = result.get("voice_character")
    if not isinstance(synthesis_character, str) or synthesis_character != expected:
        raise AuthoringWorkbenchError(
            f"Reviewed-rejection manifest route changed: {queue_item.queue_id!r}"
        )
    references = reference_sha256s.get(
        normalize_character_name(synthesis_character), []
    )
    if not references:
        raise AuthoringWorkbenchError(
            "Reviewed-rejection voice references are unavailable: "
            f"{queue_item.queue_id!r}"
        )
    return synthesis_character, references


def _downstream_overlay_queue_ids(workspace: Mapping[str, object]) -> set[str]:
    result: set[str] = set()
    for field in ("explicit_fallback_merge", "audio_event_omission"):
        config = workspace.get(field)
        items = config.get("items") if isinstance(config, dict) else None
        if not isinstance(items, list):
            continue
        result.update(
            item["queue_id"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("queue_id"), str)
        )
    return result


def _workspace_creation_fields(
    workspace: Mapping[str, object],
) -> tuple[str, str]:
    source = workspace.get("source")
    import_id = source.get("import_id") if isinstance(source, dict) else None
    narrator = workspace.get("narrator_character")
    if not isinstance(import_id, str) or not isinstance(narrator, str):
        raise AuthoringWorkbenchError("Reviewed-rejection fallback base is malformed")
    return import_id, narrator


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthoringWorkbenchError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "create_reviewed_rejection_fallback_workspace",
    "validate_reviewed_rejection_fallback_workspace",
]

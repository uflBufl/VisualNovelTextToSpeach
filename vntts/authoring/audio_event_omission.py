"""Publish exact pure audio-event omissions as immutable workspace successors."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from vntts.authoring.audio_events import audio_event_plan_for_record
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
    AUDIO_EVENT_OMISSION_REASON,
    AUDIO_EVENT_OMISSION_SCHEMA,
    AUDIO_EVENT_OMISSION_VERSION,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
    rename_directory_no_replace,
    staged_directory,
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
from vntts.authoring.workspace_config import workspace_config_fingerprint
from vntts.authoring.workspace_foundation import (
    copy_generation_wavs,
    copy_workspace_tree_snapshot,
)
from vntts.authoring.workspace_state import load_stable_workspace_generation_state

_copy_base_wavs = partial(
    copy_generation_wavs,
    target_label="Audio-event omission WAV",
    error_type=AuthoringWorkbenchError,
)

SCHEMA = "vntts.authoring-audio-event-omission-batch"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class _OmissionSelection:
    base_directory: Path
    base_document: dict[str, object]
    base_workspace_sha256: str
    queue: VoiceGenerationQueue
    state: dict[str, object]
    state_sha256: str
    queue_path: Path
    queue_sha256: str
    import_id: str
    narrator_character: str
    items: list[dict[str, object]]


@dataclass(frozen=True)
class _OmissionIdentity:
    batch: dict[str, object]
    config_fingerprint: str
    workspace_id: str
    root: Path
    destination: Path


def create_audio_event_omission_workspace(
    base_workspace: str | Path,
    queue_ids: Iterable[object],
    workspaces_root: str | Path | None = None,
) -> WorkspaceCreationResult:
    """Terminalize exact absent pure-event lines without producing audio."""
    selection = _select_omission_items(base_workspace, queue_ids)
    identity = _omission_identity(selection, workspaces_root)
    existing = _existing_omission_workspace(identity)
    if existing is not None:
        return existing
    snapshots = _authority_snapshots(selection)
    try:
        with staged_directory(
            identity.root, prefix=".audio-event-omission-"
        ) as staging:
            output = _stage_omission_workspace(staging, selection, snapshots)
            target_state = _omitted_state(selection, identity)
            workspace = _write_staged_omission_workspace(
                staging, output, selection, identity, target_state
            )
            _validate_staged_omission_workspace(staging, output, workspace)
            _publish_staged_omission_workspace(staging, selection, identity, snapshots)
    except (BulkGenerationError, OSError, ValueError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    return WorkspaceCreationResult(identity.destination, True)


def _select_omission_items(
    base_workspace: str | Path, queue_ids: Iterable[object]
) -> _OmissionSelection:
    base_directory, base_document, base_workspace_sha256 = load_workspace_authority(
        base_workspace
    )
    selected = tuple(sorted(str(value) for value in queue_ids))
    if not selected or len(selected) != len(set(selected)):
        raise AuthoringWorkbenchError(
            "Audio-event omission requires unique exact queue IDs"
        )
    queue, state, _payload, state_sha256 = load_stable_workspace_generation_state(
        base_directory,
        base_document,
        "audio-event omission base",
        error_type=AuthoringWorkbenchError,
    )
    if state.get("active") is not None:
        raise AuthoringWorkbenchError("Audio-event omission base is active")
    state_items = _generation_state_items(state)
    source = base_document.get("source")
    import_id = source.get("import_id") if isinstance(source, dict) else None
    narrator_character = base_document.get("narrator_character")
    if not isinstance(import_id, str) or not isinstance(narrator_character, str):
        raise AuthoringWorkbenchError("Audio-event omission base is malformed")
    queue_path = base_directory / "queue.jsonl"
    queue_sha256 = sha256_file(queue_path)
    queue_by_id = {item.queue_id: item for item in queue.items}
    items = []
    for queue_id in selected:
        queue_item = queue_by_id.get(queue_id)
        if queue_item is None or queue_item.action != "generate":
            raise AuthoringWorkbenchError(
                f"Audio-event omission queue ID is unavailable: {queue_id!r}"
            )
        if state_items.get(queue_id) is not None:
            raise AuthoringWorkbenchError(
                f"Audio-event omission base item is not absent: {queue_id!r}"
            )
        try:
            plan = audio_event_plan_for_record(queue_item)
        except ValueError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        if (
            not isinstance(plan, dict)
            or not plan.get("requires_composition")
            or plan.get("spoken_text") != ""
            or not isinstance(plan.get("events"), list)
            or not plan["events"]
        ):
            raise AuthoringWorkbenchError(
                f"Audio-event omission requires a pure event: {queue_id!r}"
            )
        items.append(
            {
                "queue_id": queue_id,
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "speaker": queue_item.speaker,
                "plan_sha256": plan["plan_sha256"],
                "spoken_text_sha256": plan["spoken_text_sha256"],
            }
        )

    return _OmissionSelection(
        base_directory=base_directory,
        base_document=base_document,
        base_workspace_sha256=base_workspace_sha256,
        queue=queue,
        state=state,
        state_sha256=state_sha256,
        queue_path=queue_path,
        queue_sha256=queue_sha256,
        import_id=import_id,
        narrator_character=narrator_character,
        items=items,
    )


def _omission_identity(
    selection: _OmissionSelection, workspaces_root: str | Path | None
) -> _OmissionIdentity:
    batch_body = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "reason": AUDIO_EVENT_OMISSION_REASON,
        "base_workspace_id": selection.base_document["workspace_id"],
        "base_workspace_path": "inputs/audio-event-omission/base-workspace.json",
        "base_workspace_sha256": selection.base_workspace_sha256,
        "base_state_path": "inputs/audio-event-omission/base-generation-state.json",
        "base_state_sha256": selection.state_sha256,
        "queue_sha256": selection.queue_sha256,
        "items": selection.items,
    }
    batch_id = canonical_document_sha256(batch_body)
    batch = {**batch_body, "batch_id": batch_id}
    config_fingerprint = workspace_config_fingerprint(
        selection.import_id,
        selection.base_document.get("story_index"),
        selection.base_document.get("voice_manifest"),
        selection.narrator_character,
        selection.base_document["run_config"],
        selection.base_document.get("carry_forward"),
        selection.base_document.get("outcome_merge"),
        selection.base_document.get("failure_reference_binding"),
        selection.base_document.get("terminal_conflict_merge"),
        selection.base_document.get("config_rebase"),
        selection.base_document.get("audio_event_composition"),
        selection.base_document.get("explicit_fallback_merge"),
        selection.base_document.get("known_role_live_fallback"),
        batch,
        selection.base_document.get("audio_event_projection_fallback"),
        selection.base_document.get("reviewed_waveform_publication"),
        selection.base_document.get("reviewed_rejection_live_fallback"),
        queue_extension=selection.base_document.get("queue_extension"),
    )
    workspace_id = f"resume-{selection.import_id.removeprefix('legacy-')}-{config_fingerprint[:16]}"
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Audio-event omission destination"
    )
    return _OmissionIdentity(batch, config_fingerprint, workspace_id, root, destination)


def _existing_omission_workspace(
    identity: _OmissionIdentity,
) -> WorkspaceCreationResult | None:
    if identity.destination.exists():
        _directory, existing, _sha256 = load_workspace_authority(identity.destination)
        if existing.get("audio_event_omission") != identity.batch:
            raise AuthoringWorkbenchError("Audio-event omission destination conflicts")
        return WorkspaceCreationResult(identity.destination, False)
    return None


def _authority_snapshots(selection: _OmissionSelection) -> list[tuple[Path, str]]:
    return [
        (selection.base_directory / "workspace.json", selection.base_workspace_sha256),
        (
            selection.base_directory / "generated-audio/generation-state.json",
            selection.state_sha256,
        ),
        (selection.queue_path, selection.queue_sha256),
    ]


def _stage_omission_workspace(
    staging: Path,
    selection: _OmissionSelection,
    snapshots: list[tuple[Path, str]],
) -> Path:
    for tree_name in ("provenance", "inputs"):
        copy_workspace_tree_snapshot(
            selection.base_directory / tree_name,
            staging / tree_name,
            snapshots,
            error_type=AuthoringWorkbenchError,
        )
    omission_inputs = staging / "inputs/audio-event-omission"
    omission_inputs.mkdir(parents=True)
    (omission_inputs / "base-workspace.json").write_bytes(
        read_workspace_file_bytes(
            selection.base_directory / "workspace.json",
            "audio-event omission base workspace",
        )
    )
    (omission_inputs / "base-generation-state.json").write_bytes(
        read_workspace_file_bytes(
            selection.base_directory / "generated-audio/generation-state.json",
            "audio-event omission base state",
        )
    )
    (staging / "queue.jsonl").write_bytes(
        read_workspace_file_bytes(selection.queue_path, "audio-event omission queue")
    )
    output = staging / "generated-audio"
    output.mkdir()
    _copy_base_wavs(selection.base_directory, output, selection.state, snapshots)
    return output


def _omitted_state(
    selection: _OmissionSelection, identity: _OmissionIdentity
) -> dict[str, object]:
    target_state = copy.deepcopy(selection.state)
    target_items = _generation_state_items(target_state)
    decided_at = datetime.now(timezone.utc).isoformat()
    authority = {
        "batch_id": identity.batch["batch_id"],
        "base_workspace_id": selection.base_document["workspace_id"],
        "base_workspace_sha256": selection.base_workspace_sha256,
        "base_state_sha256": selection.state_sha256,
        "queue_sha256": selection.queue_sha256,
    }
    for ledger in selection.items:
        queue_id = ledger["queue_id"]
        decision = {
            "schema": AUDIO_EVENT_OMISSION_SCHEMA,
            "schema_version": AUDIO_EVENT_OMISSION_VERSION,
            "reason": AUDIO_EVENT_OMISSION_REASON,
            **ledger,
            "decided_at": decided_at,
            "authority": copy.deepcopy(authority),
        }
        target_items[queue_id] = {
            "status": "omitted",
            "review_status": "omitted",
            "attempts": 0,
            "line_id": ledger["line_id"],
            "text_sha256": ledger["text_sha256"],
            "speaker": ledger["speaker"],
            "audio_event_omission": decision,
            "updated_at": decided_at,
        }
    target_state["active"] = None
    return target_state


def _write_staged_omission_workspace(
    staging: Path,
    output: Path,
    selection: _OmissionSelection,
    identity: _OmissionIdentity,
    target_state: dict[str, object],
) -> dict[str, object]:
    atomic_write_json(output / "generation-state.json", target_state, sort_keys=True)
    write_generated_manifest_from_state(target_state, output, output / "manifest.json")
    workspace = copy.deepcopy(selection.base_document)
    workspace.update(
        {
            "workspace_id": identity.workspace_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "audio_event_omission": identity.batch,
            "config_fingerprint": identity.config_fingerprint,
        }
    )
    atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
    return workspace


def _validate_staged_omission_workspace(
    staging: Path, output: Path, workspace: dict[str, object]
) -> None:
    import_snapshot = load_workspace_json(
        staging / "provenance/import.json", "audio-event omission import"
    )
    validate_workspace_provenance_extensions(staging, workspace, import_snapshot)
    load_generation_state(output / "generation-state.json", staging / "queue.jsonl")


def _publish_staged_omission_workspace(
    staging: Path,
    selection: _OmissionSelection,
    identity: _OmissionIdentity,
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
                raise AuthoringWorkbenchError("Audio-event omission base became active")
            for path, digest in snapshots:
                if not path.is_file() or sha256_file(path) != digest:
                    raise AuthoringWorkbenchError(
                        "Audio-event omission authority changed before publication"
                    )
            leases[0].assert_owned()
            try:
                rename_directory_no_replace(staging, identity.destination)
            except (AtomicPublicationError, OSError) as error:
                raise AuthoringWorkbenchError(
                    f"Unable to publish audio-event omission workspace: {error}"
                ) from error
            leases[0].mark_committed()
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def validate_audio_event_omission_workspace(
    directory: str | Path, workspace: Mapping[str, object]
) -> None:
    """Validate the self-contained exact omission authority."""
    batch = _validated_omission_batch(workspace)
    if batch is None:
        return
    root = Path(directory)
    _validate_omission_authority_files(root, batch)
    queue, state, _payload, _state_sha256 = load_stable_workspace_generation_state(
        root,
        workspace,
        "audio-event omission workspace",
        error_type=AuthoringWorkbenchError,
    )
    if sha256_file(root / "queue.jsonl") != batch["queue_sha256"]:
        raise AuthoringWorkbenchError("Audio-event omission queue changed")
    _validate_omission_items(queue, state, batch)


def _validated_omission_batch(
    workspace: Mapping[str, object],
) -> dict[str, object] | None:
    batch = workspace.get("audio_event_omission")
    if batch is None:
        return None
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
        "items",
    }
    if (
        not isinstance(batch, dict)
        or set(batch) != fields
        or batch.get("schema") != SCHEMA
        or batch.get("schema_version") != SCHEMA_VERSION
        or batch.get("reason") != AUDIO_EVENT_OMISSION_REASON
        or batch.get("batch_id")
        != canonical_document_sha256(
            {key: value for key, value in batch.items() if key != "batch_id"}
        )
    ):
        raise AuthoringWorkbenchError("Audio-event omission batch is malformed")
    for field in (
        "batch_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
    ):
        require_workspace_sha256(batch.get(field), f"Audio-event omission {field}")
    return batch


def _validate_omission_authority_files(root: Path, batch: Mapping[str, object]) -> None:
    for path_field, hash_field, label in (
        ("base_workspace_path", "base_workspace_sha256", "base workspace"),
        ("base_state_path", "base_state_sha256", "base state"),
    ):
        relative = safe_workspace_relative_path(
            batch.get(path_field), f"Audio-event omission {label}"
        )
        source = contained_workspace_path(
            root, relative, f"Audio-event omission {label}"
        )
        if not source.is_file() or sha256_file(source) != batch[hash_field]:
            raise AuthoringWorkbenchError(
                f"Audio-event omission {label} authority changed"
            )


def _validate_omission_items(
    queue: VoiceGenerationQueue,
    state: dict[str, object],
    batch: Mapping[str, object],
) -> None:
    state_items = _generation_state_items(state)
    queue_by_id = {item.queue_id: item for item in queue.items}
    items = batch.get("items")
    if not isinstance(items, list) or not items:
        raise AuthoringWorkbenchError("Audio-event omission item ledger is empty")
    observed: list[str] = []
    for ledger in items:
        ledger_fields = {
            "queue_id",
            "line_id",
            "text_sha256",
            "speaker",
            "plan_sha256",
            "spoken_text_sha256",
        }
        if not isinstance(ledger, dict) or set(ledger) != ledger_fields:
            raise AuthoringWorkbenchError("Audio-event omission item is malformed")
        queue_id = ledger.get("queue_id")
        item = queue_by_id.get(queue_id) if isinstance(queue_id, str) else None
        result = state_items.get(queue_id) if isinstance(queue_id, str) else None
        decision = (
            result.get("audio_event_omission") if isinstance(result, dict) else None
        )
        if (
            not isinstance(queue_id, str)
            or item is None
            or not isinstance(decision, dict)
        ):
            raise AuthoringWorkbenchError(
                f"Audio-event omission result changed for {queue_id!r}"
            )
        plan = audio_event_plan_for_record(item)
        expected = {
            "schema": AUDIO_EVENT_OMISSION_SCHEMA,
            "schema_version": AUDIO_EVENT_OMISSION_VERSION,
            "reason": AUDIO_EVENT_OMISSION_REASON,
            **ledger,
            "decided_at": decision.get("decided_at"),
            "authority": {
                "batch_id": batch["batch_id"],
                "base_workspace_id": batch["base_workspace_id"],
                "base_workspace_sha256": batch["base_workspace_sha256"],
                "base_state_sha256": batch["base_state_sha256"],
                "queue_sha256": batch["queue_sha256"],
            },
        }
        if (
            decision != expected
            or plan is None
            or plan.get("spoken_text") != ""
            or plan.get("plan_sha256") != ledger["plan_sha256"]
        ):
            raise AuthoringWorkbenchError(
                f"Audio-event omission result changed for {queue_id!r}"
            )
        observed.append(queue_id)
    if observed != sorted(set(observed)):
        raise AuthoringWorkbenchError("Audio-event omission items are not canonical")


__all__ = [
    "create_audio_event_omission_workspace",
    "validate_audio_event_omission_workspace",
]

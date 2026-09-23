"""Migrate exact approved WAVs that predate synthesis-control inventory."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueItem,
)
from vntts_artifacts.voice_manifest import load_voice_manifest, normalize_character_name

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.generation_state import (
    REVIEWED_WAVEFORM_PUBLICATION_REASON,
    REVIEWED_WAVEFORM_PUBLICATION_SCHEMA,
    REVIEWED_WAVEFORM_PUBLICATION_VERSION,
)
from vntts.authoring.publication import (
    publish_single_base_successor,
    staged_directory,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    WorkspaceCreationResult,
    contained_workspace_path,
    default_workspaces_root,
    load_workspace_authority,
    load_workspace_json,
    require_workspace_sha256,
    safe_workspace_relative_path,
    validate_workspace_provenance_extensions,
)
from vntts.authoring.workspace_config import (
    selected_voice_manifest_path,
    workspace_id_for_config,
    workspace_successor_config_fingerprint,
)
from vntts.authoring.workspace_foundation import (
    stage_single_base_successor,
)
from vntts.authoring.workspace_state import load_stable_workspace_generation_state


def create_reviewed_waveform_publication_workspace(
    base_workspace: str | Path,
    workspaces_root: str | Path | None = None,
) -> WorkspaceCreationResult:
    """Authorize packaging every exact already-approved base waveform."""
    base_directory, base_document, base_workspace_sha256 = load_workspace_authority(
        base_workspace
    )
    queue, state, _payload, state_sha256 = load_stable_workspace_generation_state(
        base_directory,
        base_document,
        "reviewed-waveform publication base",
        error_type=AuthoringWorkbenchError,
    )
    if state.get("active") is not None:
        raise AuthoringWorkbenchError("Reviewed-waveform publication base is active")
    if state.get("reviewed_waveform_publication") is not None:
        raise AuthoringWorkbenchError(
            "Reviewed-waveform publication base is already migrated"
        )
    queue_path = base_directory / "queue.jsonl"
    queue_sha256 = sha256_file(queue_path)
    queue_by_id = {item.queue_id: item for item in queue.items}
    voice_path = selected_voice_manifest_path(
        base_directory, base_document, error_type=AuthoringWorkbenchError
    )
    if voice_path is None:
        raise AuthoringWorkbenchError(
            "Reviewed-waveform publication requires a selected voice manifest"
        )
    voice_sha256 = sha256_file(voice_path)
    story_binding = base_document.get("story_index")
    if not isinstance(story_binding, dict):
        raise AuthoringWorkbenchError(
            "Reviewed-waveform publication requires a selected story index"
        )
    story_path = contained_workspace_path(
        base_directory,
        safe_workspace_relative_path(story_binding.get("path"), "Selected story index"),
        "Selected story index",
    )
    story_sha256 = sha256_file(story_path)
    if story_sha256 != require_workspace_sha256(
        story_binding.get("sha256"), "Selected story index SHA-256"
    ):
        raise AuthoringWorkbenchError("Selected story index changed")
    narrator = cast(str, base_document["narrator_character"])
    narrator_reference_sha256s = _character_reference_sha256s(voice_path, narrator)
    state_items = _state_items(state)
    ledgers = _approved_waveform_ledgers(base_directory, state_items, queue_by_id)
    if not ledgers:
        raise AuthoringWorkbenchError(
            "Reviewed-waveform publication base has no approved WAVs"
        )
    batch_body = {
        "schema": REVIEWED_WAVEFORM_PUBLICATION_SCHEMA,
        "schema_version": REVIEWED_WAVEFORM_PUBLICATION_VERSION,
        "reason": REVIEWED_WAVEFORM_PUBLICATION_REASON,
        "publication_scope": "exact_reviewed_waveform",
        "synthesis_reproducibility": False,
        "base_workspace_id": base_document["workspace_id"],
        "base_workspace_path": "inputs/reviewed-waveform/base-workspace.json",
        "base_workspace_sha256": base_workspace_sha256,
        "base_state_path": "inputs/reviewed-waveform/base-generation-state.json",
        "base_state_sha256": state_sha256,
        "queue_sha256": queue_sha256,
        "selected_story_index_sha256": story_sha256,
        "selected_voice_manifest_sha256": voice_sha256,
        "narrator_character": narrator,
        "narrator_reference_sha256s": narrator_reference_sha256s,
        "items": ledgers,
    }
    batch = {**batch_body, "batch_id": canonical_document_sha256(batch_body)}
    import_id = _reviewed_waveform_import_id(base_document)
    config_fingerprint = workspace_successor_config_fingerprint(
        base_document,
        import_id,
        overlays={"reviewed_waveform_publication": batch},
    )
    workspace_id = workspace_id_for_config(import_id, config_fingerprint)
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Reviewed-waveform publication destination"
    )
    if destination.exists():
        _directory, existing, _sha256 = load_workspace_authority(destination)
        if existing.get("reviewed_waveform_publication") != batch:
            raise AuthoringWorkbenchError(
                "Reviewed-waveform publication destination conflicts"
            )
        return WorkspaceCreationResult(destination, False)

    snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (base_directory / "generated-audio/generation-state.json", state_sha256),
        (queue_path, queue_sha256),
    ]
    try:
        with staged_directory(root, prefix=".reviewed-waveform-") as staging:
            output = _stage_reviewed_waveform_workspace(
                staging, base_directory, queue_path, state, snapshots
            )
            target_state = _published_waveform_state(state, batch)
            workspace = _write_staged_reviewed_waveform_workspace(
                staging,
                output,
                target_state,
                base_document,
                workspace_id,
                batch,
                config_fingerprint,
            )
            _validate_staged_reviewed_waveform_workspace(staging, output, workspace)
            _publish_staged_reviewed_waveform_workspace(
                staging, base_directory, queue_sha256, destination, snapshots
            )
    except (BulkGenerationError, OSError, ValueError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    return WorkspaceCreationResult(destination, True)


def _approved_waveform_ledgers(
    base_directory: Path,
    state_items: Mapping[str, dict[str, object]],
    queue_by_id: Mapping[str, VoiceGenerationQueueItem],
) -> list[dict[str, object]]:
    ledgers = []
    for queue_id, result in sorted(state_items.items()):
        if result.get("status") != "approved":
            continue
        if result.get("review_status") != "approved":
            raise AuthoringWorkbenchError(
                f"Approved waveform has an invalid review state: {queue_id!r}"
            )
        queue_item = queue_by_id.get(queue_id)
        if queue_item is None:
            raise AuthoringWorkbenchError(
                f"Approved waveform queue ID is unavailable: {queue_id!r}"
            )
        relative = safe_workspace_relative_path(
            result.get("path"), f"Approved waveform {queue_id!r} path"
        )
        source = contained_workspace_path(
            base_directory / "generated-audio", relative, "Approved waveform"
        )
        if not source.is_file() or sha256_file(source) != require_workspace_sha256(
            result.get("file_sha256"), f"Approved waveform {queue_id!r} SHA-256"
        ):
            raise AuthoringWorkbenchError(f"Approved waveform changed for {queue_id!r}")
        rebase = result.get("config_rebase")
        if isinstance(rebase, dict):
            if (
                rebase.get("status") != "approved"
                or rebase.get("review_status") != "approved"
                or rebase.get("target_route_status") != "active"
                or rebase.get("audio_sha256") != result["file_sha256"]
                or not isinstance(rebase.get("target_effective_character"), str)
                or not isinstance(rebase.get("target_reference_sha256s"), list)
                or not rebase["target_reference_sha256s"]
            ):
                raise AuthoringWorkbenchError(
                    f"Approved waveform config-rebase route is invalid: {queue_id!r}"
                )
            route = {
                "source": "config_rebase",
                "status": "active",
                "effective_character": rebase["target_effective_character"],
                "reference_sha256s": sorted(set(rebase["target_reference_sha256s"])),
            }
        else:
            route = {
                "source": "historical_reviewed_waveform",
                "status": "not_reproducible",
                "effective_character": "unknown",
                "reference_sha256s": [],
            }
        ledgers.append(
            {
                "queue_id": queue_id,
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "speaker": queue_item.speaker,
                "path": relative.as_posix(),
                "file_sha256": result["file_sha256"],
                "base_result_sha256": canonical_document_sha256(result),
                "base_result": copy.deepcopy(result),
                "route": route,
            }
        )
    return ledgers


def _stage_reviewed_waveform_workspace(
    staging: Path,
    base_directory: Path,
    queue_path: Path,
    state: Mapping[str, object],
    snapshots: list[tuple[Path, str]],
) -> Path:
    return stage_single_base_successor(
        staging,
        base_directory,
        queue_path,
        state,
        snapshots,
        input_name="reviewed-waveform",
        label="reviewed-waveform",
        wav_label="Reviewed-waveform WAV",
        error_type=AuthoringWorkbenchError,
    )


def _published_waveform_state(
    state: dict[str, object], batch: dict[str, object]
) -> dict[str, object]:
    target_state = copy.deepcopy(state)
    target_state["reviewed_waveform_publication"] = copy.deepcopy(batch)
    target_state["active"] = None
    return target_state


def _write_staged_reviewed_waveform_workspace(
    staging: Path,
    output: Path,
    target_state: dict[str, object],
    base_document: dict[str, object],
    workspace_id: str,
    batch: dict[str, object],
    config_fingerprint: str,
) -> dict[str, object]:
    atomic_write_json(output / "generation-state.json", target_state, sort_keys=True)
    write_generated_manifest_from_state(target_state, output, output / "manifest.json")
    workspace = copy.deepcopy(base_document)
    workspace.update(
        {
            "workspace_id": workspace_id,
            "reviewed_waveform_publication": copy.deepcopy(batch),
            "config_fingerprint": config_fingerprint,
        }
    )
    atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
    return workspace


def _reviewed_waveform_import_id(base_document: Mapping[str, object]) -> str:
    source_document = base_document.get("source")
    if not isinstance(source_document, dict) or not isinstance(
        source_document.get("import_id"), str
    ):
        raise AuthoringWorkbenchError("Reviewed-waveform source authority is malformed")
    return source_document["import_id"]


def _validate_staged_reviewed_waveform_workspace(
    staging: Path, output: Path, workspace: dict[str, object]
) -> None:
    import_snapshot = load_workspace_json(
        staging / "provenance/import.json", "reviewed-waveform import"
    )
    validate_workspace_provenance_extensions(staging, workspace, import_snapshot)
    load_generation_state(output / "generation-state.json", staging / "queue.jsonl")


def _publish_staged_reviewed_waveform_workspace(
    staging: Path,
    base_directory: Path,
    queue_sha256: str,
    destination: Path,
    snapshots: list[tuple[Path, str]],
) -> None:
    try:
        publish_single_base_successor(
            staging,
            destination,
            base_directory,
            queue_sha256,
            snapshots,
            label="Reviewed-waveform publication",
            publish_label="reviewed-waveform",
            error_type=AuthoringWorkbenchError,
        )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def validate_reviewed_waveform_publication_workspace(
    directory: str | Path, workspace: Mapping[str, object]
) -> None:
    """Validate snapshots and exact result equality for one migration."""
    batch = workspace.get("reviewed_waveform_publication")
    if batch is None:
        return
    if not isinstance(batch, dict):
        raise AuthoringWorkbenchError(
            "Reviewed-waveform publication authority is malformed"
        )
    root = Path(directory)
    base_state = _reviewed_waveform_base_state(root, batch)
    queue, state, _payload, _state_sha256 = load_stable_workspace_generation_state(
        root,
        workspace,
        "reviewed-waveform publication workspace",
        error_type=AuthoringWorkbenchError,
    )
    _validate_reviewed_waveform_state(root, batch, queue, state, base_state)


def _reviewed_waveform_base_state(
    root: Path, batch: Mapping[str, object]
) -> dict[str, object]:
    for path_field, hash_field, label in (
        ("base_workspace_path", "base_workspace_sha256", "base workspace"),
        ("base_state_path", "base_state_sha256", "base state"),
    ):
        relative = safe_workspace_relative_path(
            batch.get(path_field), f"Reviewed-waveform {label}"
        )
        source = contained_workspace_path(root, relative, f"Reviewed-waveform {label}")
        if not source.is_file() or sha256_file(source) != batch.get(hash_field):
            raise AuthoringWorkbenchError(
                f"Reviewed-waveform {label} authority changed"
            )
    return load_workspace_json(
        contained_workspace_path(
            root,
            safe_workspace_relative_path(
                batch["base_state_path"], "Reviewed-waveform base state"
            ),
            "Reviewed-waveform base state",
        ),
        "reviewed-waveform base state",
    )


def _validate_reviewed_waveform_state(
    root: Path,
    batch: Mapping[str, object],
    queue: VoiceGenerationQueue,
    state: dict[str, object],
    base_state: dict[str, object],
) -> None:
    if state.get("reviewed_waveform_publication") != batch:
        raise AuthoringWorkbenchError("Reviewed-waveform state authority changed")
    if sha256_file(root / "queue.jsonl") != batch.get("queue_sha256"):
        raise AuthoringWorkbenchError("Reviewed-waveform queue changed")
    queue_ids = {item.queue_id for item in queue.items}
    base_items = _state_items(base_state)
    state_items = _state_items(state)
    for ledger in cast(list[dict[str, object]], batch["items"]):
        queue_id = cast(str, ledger["queue_id"])
        if (
            queue_id not in queue_ids
            or base_items.get(queue_id) != ledger["base_result"]
            or state_items.get(queue_id) != ledger["base_result"]
        ):
            raise AuthoringWorkbenchError(
                f"Reviewed-waveform result changed for {queue_id!r}"
            )


def _character_reference_sha256s(voice_path: Path, character: str) -> list[str]:
    try:
        _document, entries = load_voice_manifest(voice_path)
    except Exception as error:
        raise AuthoringWorkbenchError(str(error)) from error
    normalized = normalize_character_name(character)
    matches = [
        entry
        for entry in entries
        if normalized
        in {
            normalize_character_name(entry.character),
            *(normalize_character_name(a) for a in entry.aliases),
        }
    ]
    if len(matches) != 1:
        raise AuthoringWorkbenchError(
            f"Selected narrator {character!r} is not unique in the voice manifest"
        )
    digests = []
    for relative in matches[0].references:
        reference = contained_workspace_path(
            voice_path.parent,
            safe_workspace_relative_path(relative, "Narrator voice reference"),
            "Narrator voice reference",
        )
        if not reference.is_file():
            raise AuthoringWorkbenchError("Narrator voice reference is unavailable")
        digests.append(sha256_file(reference))
    if not digests:
        raise AuthoringWorkbenchError("Selected narrator has no voice references")
    return sorted(set(digests))


def _state_items(state: Mapping[str, object]) -> dict[str, dict[str, object]]:
    items = state.get("items")
    if not isinstance(items, dict) or any(
        not isinstance(queue_id, str) or not isinstance(result, dict)
        for queue_id, result in items.items()
    ):
        raise AuthoringWorkbenchError(
            "Reviewed-waveform generation items are malformed"
        )
    return items


__all__ = [
    "create_reviewed_waveform_publication_workspace",
    "validate_reviewed_waveform_publication_workspace",
]

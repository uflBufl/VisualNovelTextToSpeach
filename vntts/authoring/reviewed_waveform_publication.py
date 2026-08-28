"""Migrate exact approved WAVs that predate synthesis-control inventory."""

from __future__ import annotations

import copy
import hashlib
import shutil
import tempfile
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import load_voice_manifest, normalize_character_name

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    process_is_alive,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.generation_state import (
    REVIEWED_WAVEFORM_PUBLICATION_REASON,
    REVIEWED_WAVEFORM_PUBLICATION_SCHEMA,
    REVIEWED_WAVEFORM_PUBLICATION_VERSION,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
    rename_directory_no_replace,
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
from vntts.authoring.workspace_foundation import copy_workspace_tree_snapshot
from vntts.authoring.workspace_state import load_stable_workspace_generation_state


def create_reviewed_waveform_publication_workspace(
    base_workspace,
    workspaces_root=None,
):
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
    narrator = base_document.get("narrator_character")
    narrator_reference_sha256s = _character_reference_sha256s(voice_path, narrator)
    ledgers = []
    for queue_id, result in sorted(state["items"].items()):
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
    config_fingerprint = workspace_config_fingerprint(
        base_document["source"]["import_id"],
        base_document.get("story_index"),
        base_document.get("voice_manifest"),
        base_document["narrator_character"],
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
        batch,
    )
    workspace_id = (
        f"resume-{base_document['source']['import_id'].removeprefix('legacy-')}-"
        f"{config_fingerprint[:16]}"
    )
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

    staging = Path(tempfile.mkdtemp(prefix=".reviewed-waveform-", dir=root)).resolve()
    snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (base_directory / "generated-audio/generation-state.json", state_sha256),
        (queue_path, queue_sha256),
    ]
    try:
        for tree_name in ("provenance", "inputs"):
            copy_workspace_tree_snapshot(
                base_directory / tree_name,
                staging / tree_name,
                snapshots,
                error_type=AuthoringWorkbenchError,
            )
        publication_inputs = staging / "inputs/reviewed-waveform"
        publication_inputs.mkdir(parents=True)
        (publication_inputs / "base-workspace.json").write_bytes(
            read_workspace_file_bytes(
                base_directory / "workspace.json",
                "reviewed-waveform base workspace",
            )
        )
        (publication_inputs / "base-generation-state.json").write_bytes(
            read_workspace_file_bytes(
                base_directory / "generated-audio/generation-state.json",
                "reviewed-waveform base state",
            )
        )
        (staging / "queue.jsonl").write_bytes(
            read_workspace_file_bytes(queue_path, "reviewed-waveform queue")
        )
        output = staging / "generated-audio"
        output.mkdir()
        target_state = copy.deepcopy(state)
        target_state["reviewed_waveform_publication"] = copy.deepcopy(batch)
        target_state["active"] = None
        _copy_base_wavs(base_directory, output, state, snapshots)
        atomic_write_json(
            output / "generation-state.json", target_state, sort_keys=True
        )
        write_generated_manifest_from_state(
            target_state, output, output / "manifest.json"
        )
        workspace = copy.deepcopy(base_document)
        workspace.update(
            {
                "workspace_id": workspace_id,
                "reviewed_waveform_publication": copy.deepcopy(batch),
                "config_fingerprint": config_fingerprint,
            }
        )
        atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
        import_snapshot = load_workspace_json(
            staging / "provenance/import.json", "reviewed-waveform import"
        )
        validate_workspace_provenance_extensions(staging, workspace, import_snapshot)
        load_generation_state(output / "generation-state.json", staging / "queue.jsonl")
        try:
            with generation_publication_leases(
                ((base_directory / "generated-audio", queue_sha256),),
                process_checker=process_is_alive,
            ) as leases:
                if any((base_directory / "generated-audio").rglob("*.partial.wav")):
                    raise AuthoringWorkbenchError(
                        "Reviewed-waveform publication base became active"
                    )
                for path, digest in snapshots:
                    if not path.is_file() or sha256_file(path) != digest:
                        raise AuthoringWorkbenchError(
                            "Reviewed-waveform authority changed before publication"
                        )
                leases[0].assert_owned()
                try:
                    rename_directory_no_replace(staging, destination)
                except (AtomicPublicationError, OSError) as error:
                    raise AuthoringWorkbenchError(
                        f"Unable to publish reviewed-waveform workspace: {error}"
                    ) from error
                leases[0].mark_committed()
                staging = None
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
    except (BulkGenerationError, OSError, ValueError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return WorkspaceCreationResult(destination, True)


def validate_reviewed_waveform_publication_workspace(directory, workspace):
    """Validate snapshots and exact result equality for one migration."""
    batch = workspace.get("reviewed_waveform_publication")
    if batch is None:
        return
    root = Path(directory)
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
    base_state = load_workspace_json(
        contained_workspace_path(
            root,
            safe_workspace_relative_path(
                batch["base_state_path"], "Reviewed-waveform base state"
            ),
            "Reviewed-waveform base state",
        ),
        "reviewed-waveform base state",
    )
    queue, state, _payload, _state_sha256 = load_stable_workspace_generation_state(
        root,
        workspace,
        "reviewed-waveform publication workspace",
        error_type=AuthoringWorkbenchError,
    )
    if state.get("reviewed_waveform_publication") != batch:
        raise AuthoringWorkbenchError("Reviewed-waveform state authority changed")
    if sha256_file(root / "queue.jsonl") != batch.get("queue_sha256"):
        raise AuthoringWorkbenchError("Reviewed-waveform queue changed")
    queue_ids = {item.queue_id for item in queue.items}
    for ledger in batch["items"]:
        queue_id = ledger["queue_id"]
        if (
            queue_id not in queue_ids
            or base_state.get("items", {}).get(queue_id) != ledger["base_result"]
            or state["items"].get(queue_id) != ledger["base_result"]
        ):
            raise AuthoringWorkbenchError(
                f"Reviewed-waveform result changed for {queue_id!r}"
            )


def _character_reference_sha256s(voice_path, character):
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


def _copy_base_wavs(base_directory, output, state, snapshots):
    owners = {}
    for queue_id, result in state["items"].items():
        if not isinstance(result, dict) or not isinstance(result.get("path"), str):
            continue
        relative = safe_workspace_relative_path(
            result["path"], f"Base generation item {queue_id!r} path"
        )
        owner = owners.setdefault(relative.as_posix(), queue_id)
        if owner != queue_id:
            raise AuthoringWorkbenchError(f"Base WAV path collides with {owner!r}")
        source = contained_workspace_path(
            base_directory / "generated-audio", relative, "Base generation WAV"
        )
        payload = read_workspace_file_bytes(source, "base generation WAV")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != require_workspace_sha256(
            result.get("file_sha256"), f"Base item {queue_id!r} WAV SHA-256"
        ):
            raise AuthoringWorkbenchError(f"Base WAV changed for {queue_id!r}")
        target = contained_workspace_path(output, relative, "Reviewed-waveform WAV")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        snapshots.append((source, digest))


__all__ = [
    "create_reviewed_waveform_publication_workspace",
    "validate_reviewed_waveform_publication_workspace",
]

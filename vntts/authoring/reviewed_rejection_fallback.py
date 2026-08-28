"""Publish explicit live routes for exact already-rejected generated WAVs."""

from __future__ import annotations

import copy
import hashlib
import shutil
import tempfile
from datetime import datetime, timezone
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
    LIVE_FALLBACK_REVIEWED_REJECTION_VERSION,
    LIVE_FALLBACK_SCHEMA,
    REVIEWED_REJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
    rename_directory_no_replace,
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
from vntts.authoring.workspace_foundation import copy_workspace_tree_snapshot
from vntts.authoring.workspace_state import load_stable_workspace_generation_state

SCHEMA = "vntts.authoring-reviewed-rejection-live-fallback-batch"
SCHEMA_VERSION = 1
REASON = "generated_audio_rejected"


def create_reviewed_rejection_fallback_workspace(
    base_workspace,
    workspaces_root=None,
):
    """Route every exact rejected result without an existing fallback."""
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
        voice_document, voice_entries = load_voice_manifest(
            voice_path, allow_legacy=False
        )
        overrides = queue_voice_overrides_from_manifest(
            voice_document, queue_ids=queue_by_id, voices=voice_entries
        )
    except (OSError, TypeError, ValueError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    reference_sha256s = _voice_reference_sha256s(voice_path, voice_entries)

    ledgers = []
    for queue_id, base_result in sorted(state["items"].items()):
        if (
            base_result.get("status") != "generated"
            or base_result.get("review_status") != "rejected"
            or isinstance(base_result.get("live_fallback"), dict)
        ):
            continue
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
            synthesis_character = base_result.get("voice_character")
            if overrides.get(queue_id) != synthesis_character:
                raise AuthoringWorkbenchError(
                    f"Reviewed-rejection manifest route changed: {queue_id!r}"
                )
            references = reference_sha256s.get(
                normalize_character_name(synthesis_character), []
            )
            if not references:
                raise AuthoringWorkbenchError(
                    f"Reviewed-rejection voice references are unavailable: {queue_id!r}"
                )
        ledgers.append(
            {
                "queue_id": queue_id,
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "speaker": queue_item.speaker,
                "base_result_sha256": canonical_document_sha256(base_result),
                "synthesis_character": synthesis_character,
                "route_source": route_source,
                "route_reference_sha256s": references,
            }
        )
    if not ledgers:
        raise AuthoringWorkbenchError(
            "Reviewed-rejection fallback base has no unresolved rejected WAVs"
        )
    batch_body = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "reason": REASON,
        "base_workspace_id": base_document["workspace_id"],
        "base_workspace_path": "inputs/reviewed-rejection/base-workspace.json",
        "base_workspace_sha256": base_workspace_sha256,
        "base_state_path": "inputs/reviewed-rejection/base-generation-state.json",
        "base_state_sha256": state_sha256,
        "queue_sha256": queue_sha256,
        "voice_manifest_sha256": voice_sha256,
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
        base_document.get("reviewed_waveform_publication"),
        batch,
    )
    workspace_id = (
        f"resume-{base_document['source']['import_id'].removeprefix('legacy-')}-"
        f"{config_fingerprint[:16]}"
    )
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Reviewed-rejection fallback destination"
    )
    if destination.exists():
        _directory, existing, _sha256 = load_workspace_authority(destination)
        if existing.get("reviewed_rejection_live_fallback") != batch:
            raise AuthoringWorkbenchError(
                "Reviewed-rejection fallback destination conflicts"
            )
        return WorkspaceCreationResult(destination, False)

    staging = Path(tempfile.mkdtemp(prefix=".reviewed-rejection-", dir=root)).resolve()
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
        inputs = staging / "inputs/reviewed-rejection"
        inputs.mkdir(parents=True)
        (inputs / "base-workspace.json").write_bytes(
            read_workspace_file_bytes(
                base_directory / "workspace.json", "reviewed-rejection base workspace"
            )
        )
        (inputs / "base-generation-state.json").write_bytes(
            read_workspace_file_bytes(
                base_directory / "generated-audio/generation-state.json",
                "reviewed-rejection base state",
            )
        )
        (staging / "queue.jsonl").write_bytes(
            read_workspace_file_bytes(queue_path, "reviewed-rejection queue")
        )
        output = staging / "generated-audio"
        output.mkdir()
        target_state = copy.deepcopy(state)
        _copy_base_wavs(base_directory, output, state, snapshots)
        decided_at = datetime.now(timezone.utc).isoformat()
        for ledger in ledgers:
            queue_id = ledger["queue_id"]
            base_result = state["items"][queue_id]
            evidence = {
                "schema": REVIEWED_REJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA,
                "schema_version": 1,
                "batch_id": batch["batch_id"],
                "base_workspace_id": base_document["workspace_id"],
                "base_workspace_sha256": base_workspace_sha256,
                "base_state_sha256": state_sha256,
                "queue_sha256": queue_sha256,
                "voice_manifest_sha256": voice_sha256,
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
            target_state["items"][queue_id] = projected
        target_state["active"] = None
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
                "reviewed_rejection_live_fallback": copy.deepcopy(batch),
                "config_fingerprint": config_fingerprint,
            }
        )
        atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
        import_snapshot = load_workspace_json(
            staging / "provenance/import.json", "reviewed-rejection import"
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
                        "Reviewed-rejection fallback base became active"
                    )
                for path, digest in snapshots:
                    if not path.is_file() or sha256_file(path) != digest:
                        raise AuthoringWorkbenchError(
                            "Reviewed-rejection authority changed before publication"
                        )
                leases[0].assert_owned()
                try:
                    rename_directory_no_replace(staging, destination)
                except (AtomicPublicationError, OSError) as error:
                    raise AuthoringWorkbenchError(
                        f"Unable to publish reviewed-rejection workspace: {error}"
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


def validate_reviewed_rejection_fallback_workspace(directory, workspace):
    """Validate the self-contained rejected-result route batch."""
    batch = workspace.get("reviewed_rejection_live_fallback")
    if batch is None:
        return
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
    root = Path(directory)
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
    base_state = load_workspace_json(
        root / batch["base_state_path"], "reviewed-rejection base state"
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
        voice_document, voice_entries = load_voice_manifest(
            manifest_path, allow_legacy=False
        )
        overrides = queue_voice_overrides_from_manifest(
            voice_document,
            queue_ids=queue_by_id,
            voices=voice_entries,
        )
    except (OSError, TypeError, ValueError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    reference_sha256s = _voice_reference_sha256s(manifest_path, voice_entries)
    expected = sorted(
        queue_id
        for queue_id, result in base_state.get("items", {}).items()
        if isinstance(result, dict)
        and result.get("status") == "generated"
        and result.get("review_status") == "rejected"
        and not isinstance(result.get("live_fallback"), dict)
    )
    observed = []
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
    for ledger in batch.get("items", []):
        if not isinstance(ledger, dict) or set(ledger) != ledger_fields:
            raise AuthoringWorkbenchError(
                "Reviewed-rejection fallback item is malformed"
            )
        queue_id = ledger.get("queue_id")
        base_result = base_state.get("items", {}).get(queue_id)
        result = state["items"].get(queue_id)
        decision = result.get("live_fallback") if isinstance(result, dict) else None
        evidence = decision.get("evidence") if isinstance(decision, dict) else None
        queue_item = queue_by_id.get(queue_id)
        if (
            queue_item is None
            or not isinstance(base_result, dict)
            or canonical_document_sha256(base_result)
            != ledger.get("base_result_sha256")
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
            character = ledger["synthesis_character"]
            if (
                overrides.get(queue_id) != character
                or reference_sha256s.get(normalize_character_name(character))
                != ledger["route_reference_sha256s"]
            ):
                raise AuthoringWorkbenchError(
                    f"Reviewed-rejection manifest route changed for {queue_id!r}"
                )
        observed.append(queue_id)
    if not observed or observed != expected:
        raise AuthoringWorkbenchError(
            "Reviewed-rejection fallback item coverage changed"
        )
    if {key: value for key, value in state.items() if key != "items"} != {
        key: value for key, value in base_state.items() if key != "items"
    }:
        raise AuthoringWorkbenchError("Reviewed-rejection state metadata changed")
    observed_set = set(observed)
    for queue_id, result in state["items"].items():
        base_result = base_state["items"].get(queue_id)
        if queue_id not in observed_set:
            if result != base_result:
                raise AuthoringWorkbenchError(
                    f"Reviewed-rejection unrelated result changed for {queue_id!r}"
                )
            continue
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


def _voice_reference_sha256s(voice_path, entries):
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
        target = contained_workspace_path(output, relative, "Reviewed-rejection WAV")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        snapshots.append((source, digest))


__all__ = [
    "create_reviewed_rejection_fallback_workspace",
    "validate_reviewed_rejection_fallback_workspace",
]

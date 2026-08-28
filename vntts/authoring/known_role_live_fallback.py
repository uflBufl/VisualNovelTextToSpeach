"""Publish exact routed live fallbacks from immutable failed evidence."""

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
from vntts.authoring.failure_repair import FailureRepairPolicy
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.generation_state import (
    KNOWN_ROLE_LIVE_FALLBACK_EVIDENCE_SCHEMA,
    LIVE_FALLBACK_HYPOTHESES_EXHAUSTED,
    LIVE_FALLBACK_KNOWN_ROLE_EVIDENCE_VERSION,
    LIVE_FALLBACK_SCHEMA,
)
from vntts.authoring.missing_voice_policy import MissingVoicePolicy
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
    rename_directory_no_replace,
)
from vntts.authoring.source_reference_bindings import (
    KNOWN_ROLE_REUSE_BINDING_FIELD,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
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
from vntts.authoring.workspace_foundation import copy_workspace_tree_snapshot
from vntts.authoring.workspace_state import load_stable_workspace_generation_state

SCHEMA = "vntts.authoring-known-role-live-fallback-batch"
SCHEMA_VERSION = 1


def create_known_role_live_fallback_workspace(
    base_workspace,
    evidence_pairs,
    workspaces_root=None,
):
    """Publish live Pocket routing for exact absent IDs with failed evidence."""
    base_directory, base_document, base_workspace_sha256 = load_workspace_authority(
        base_workspace
    )
    pairs = tuple(
        (str(queue_id), Path(path).expanduser().resolve())
        for queue_id, path in evidence_pairs
    )
    if not pairs or len({queue_id for queue_id, _path in pairs}) != len(pairs):
        raise AuthoringWorkbenchError(
            "Known-role live fallback requires unique exact evidence pairs"
        )
    pairs = tuple(sorted(pairs, key=lambda value: value[0]))
    base_queue, base_state, _payload, base_state_sha256 = (
        load_stable_workspace_generation_state(
            base_directory,
            base_document,
            "known-role fallback base",
            error_type=AuthoringWorkbenchError,
        )
    )
    if base_state.get("active") is not None:
        raise AuthoringWorkbenchError("Known-role fallback base is active")
    base_queue_path = base_directory / "queue.jsonl"
    queue_sha256 = sha256_file(base_queue_path)
    queue_by_id = {item.queue_id: item for item in base_queue.items}
    manifest_relative = safe_workspace_relative_path(
        base_document["voice_manifest"]["path"], "Known-role voice manifest"
    )
    manifest_path = contained_workspace_path(
        base_directory, manifest_relative, "Known-role voice manifest"
    )
    manifest = load_workspace_json(manifest_path, "known-role voice manifest")
    _metadata, voices = load_voice_manifest(manifest_path, allow_legacy=False)
    all_overrides = queue_voice_overrides_from_manifest(
        manifest, queue_ids=queue_by_id, voices=voices
    )
    combined_override_sha256 = queue_voice_overrides_sha256(all_overrides)
    route = manifest.get(KNOWN_ROLE_REUSE_BINDING_FIELD)
    if not isinstance(route, dict):
        raise AuthoringWorkbenchError("Known-role reuse authority is unavailable")
    route_sha256 = canonical_document_sha256(route)
    source_character = _required_text(
        route.get("source_character"), "Known-role source character"
    )
    synthesis_character = _required_text(
        route.get("reuse_voice_character"), "Known-role synthesis character"
    )
    manifest_sha256 = sha256_file(manifest_path)

    evidence_sources = {}
    ledgers = []
    for queue_id, evidence_directory in pairs:
        queue_item = queue_by_id.get(queue_id)
        if queue_item is None or queue_item.action != "generate":
            raise AuthoringWorkbenchError(
                f"Known-role fallback queue ID is unavailable: {queue_id!r}"
            )
        if base_state["items"].get(queue_id) is not None:
            raise AuthoringWorkbenchError(
                f"Known-role fallback base item is not absent: {queue_id!r}"
            )
        effective = all_overrides.get(queue_id)
        requested = queue_item.voice_character or queue_item.speaker
        if normalize_character_name(requested) != normalize_character_name(
            source_character
        ) or normalize_character_name(effective or "") != normalize_character_name(
            synthesis_character
        ):
            raise AuthoringWorkbenchError(
                f"Known-role fallback route does not cover {queue_id!r}"
            )
        source_directory, source_document, source_workspace_sha256 = (
            load_workspace_authority(evidence_directory)
        )
        if source_directory == base_directory:
            raise AuthoringWorkbenchError(
                "Known-role fallback evidence must differ from its base"
            )
        if source_document["source"] != base_document["source"]:
            raise AuthoringWorkbenchError(
                "Known-role fallback evidence has another immutable import"
            )
        source_queue, source_state, _source_payload, source_state_sha256 = (
            load_stable_workspace_generation_state(
                source_directory,
                source_document,
                "known-role fallback evidence",
                error_type=AuthoringWorkbenchError,
            )
        )
        source_queue_path = source_directory / "queue.jsonl"
        if (
            sha256_file(source_queue_path) != queue_sha256
            or source_queue.metadata != base_queue.metadata
            or [item.document for item in source_queue.items]
            != [item.document for item in base_queue.items]
        ):
            raise AuthoringWorkbenchError(
                "Known-role fallback evidence queue differs from its base"
            )
        if source_state.get("active") is not None:
            raise AuthoringWorkbenchError("Known-role fallback evidence is active")
        source_item = source_state["items"].get(queue_id)
        if (
            not isinstance(source_item, dict)
            or source_item.get("status") != "failed"
            or source_item.get("review_status") is not None
            or normalize_character_name(
                source_item.get("requested_voice_character", "")
            )
            != normalize_character_name(source_character)
            or normalize_character_name(source_item.get("voice_character", ""))
            != normalize_character_name(synthesis_character)
            or "path" in source_item
            or "file_sha256" in source_item
        ):
            raise AuthoringWorkbenchError(
                f"Known-role fallback evidence is not an exact routed failure: {queue_id!r}"
            )
        ledger = {
            "queue_id": queue_id,
            "evidence_workspace_id": source_document["workspace_id"],
            "evidence_workspace_sha256": source_workspace_sha256,
            "evidence_config_fingerprint": source_document["config_fingerprint"],
            "evidence_state_sha256": source_state_sha256,
            "evidence_item_sha256": canonical_document_sha256(source_item),
            "evidence_item": copy.deepcopy(source_item),
        }
        ledgers.append(ledger)
        evidence_sources[source_directory] = {
            "workspace": source_workspace_sha256,
            "state": source_state_sha256,
            "queue": queue_sha256,
        }

    batch_body = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "base_workspace_id": base_document["workspace_id"],
        "base_workspace_sha256": base_workspace_sha256,
        "base_state_sha256": base_state_sha256,
        "queue_sha256": queue_sha256,
        "voice_manifest_sha256": manifest_sha256,
        "route_binding_sha256": route_sha256,
        "queue_voice_overrides_sha256": combined_override_sha256,
        "source_character": source_character,
        "synthesis_character": synthesis_character,
        "items": ledgers,
    }
    batch_id = canonical_document_sha256(batch_body)
    batch = {**batch_body, "batch_id": batch_id}
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
        batch,
        base_document.get("audio_event_omission"),
        base_document.get("audio_event_projection_fallback"),
        base_document.get("reviewed_waveform_publication"),
    )
    workspace_id = (
        f"resume-{base_document['source']['import_id'].removeprefix('legacy-')}-"
        f"{config_fingerprint[:16]}"
    )
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Known-role fallback destination"
    )
    staging = Path(tempfile.mkdtemp(prefix=".known-role-fallback-", dir=root)).resolve()
    base_snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (base_directory / "generated-audio/generation-state.json", base_state_sha256),
        (base_queue_path, queue_sha256),
        (manifest_path, manifest_sha256),
    ]
    evidence_snapshots = []
    for directory, digests in evidence_sources.items():
        evidence_snapshots.extend(
            (
                (directory / "workspace.json", digests["workspace"]),
                (directory / "generated-audio/generation-state.json", digests["state"]),
                (directory / "queue.jsonl", digests["queue"]),
            )
        )
    try:
        for tree_name in ("provenance", "inputs"):
            copy_workspace_tree_snapshot(
                base_directory / tree_name,
                staging / tree_name,
                base_snapshots,
                error_type=AuthoringWorkbenchError,
            )
        (staging / "queue.jsonl").write_bytes(
            read_workspace_file_bytes(base_queue_path, "known-role fallback queue")
        )
        output = staging / "generated-audio"
        output.mkdir()
        target_state = copy.deepcopy(base_state)
        _copy_base_wavs(base_directory, output, base_state, base_snapshots)
        decided_at = datetime.now(timezone.utc).isoformat()
        synthesis_configuration = _synthesis_configuration(
            base_document["run_config"], combined_override_sha256
        )
        for ledger in ledgers:
            queue_id = ledger["queue_id"]
            queue_item = queue_by_id[queue_id]
            evidence = {
                "schema": KNOWN_ROLE_LIVE_FALLBACK_EVIDENCE_SCHEMA,
                "schema_version": 1,
                "batch_id": batch_id,
                "queue_id": queue_id,
                "voice_manifest_sha256": manifest_sha256,
                "route_binding_sha256": route_sha256,
                "queue_voice_overrides_sha256": combined_override_sha256,
                "source_character": source_character,
                "synthesis_character": synthesis_character,
                **{
                    key: value
                    for key, value in ledger.items()
                    if key != "queue_id" and key != "evidence_config_fingerprint"
                },
            }
            decision = {
                "schema": LIVE_FALLBACK_SCHEMA,
                "schema_version": LIVE_FALLBACK_KNOWN_ROLE_EVIDENCE_VERSION,
                "reason": LIVE_FALLBACK_HYPOTHESES_EXHAUSTED,
                "provider": "pocket-tts",
                "model": "pocket-tts",
                "generation_profile": "default",
                "queue_id": queue_id,
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "speaker": queue_item.speaker,
                "requested_voice_character": synthesis_character,
                "previous_result_sha256": None,
                "decided_at": decided_at,
                "evidence": evidence,
            }
            target_state["items"][queue_id] = {
                "status": "live_fallback",
                "review_status": "live_fallback",
                "attempts": 0,
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "speaker": queue_item.speaker,
                "requested_voice_character": source_character,
                "voice_character": synthesis_character,
                "synthesis_configuration": copy.deepcopy(synthesis_configuration),
                "source_reference_binding": {
                    "schema_version": 1,
                    "queue_id": queue_id,
                    "source_voice_character": source_character,
                    "synthesis_voice_character": synthesis_character,
                    "queue_voice_overrides_sha256": combined_override_sha256,
                },
                "live_fallback": decision,
                "updated_at": decided_at,
            }
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
                "created_at": datetime.now(timezone.utc).isoformat(),
                "known_role_live_fallback": batch,
                "config_fingerprint": config_fingerprint,
            }
        )
        atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
        import_snapshot = load_workspace_json(
            staging / "provenance/import.json", "known-role fallback import"
        )
        validate_workspace_provenance_extensions(staging, workspace, import_snapshot)
        load_generation_state(output / "generation-state.json", staging / "queue.jsonl")
        lease_directories = [
            (base_directory / "generated-audio", queue_sha256),
            *(
                (directory / "generated-audio", queue_sha256)
                for directory in evidence_sources
            ),
        ]
        try:
            with generation_publication_leases(
                lease_directories, process_checker=process_is_alive
            ) as held_leases:
                if any(
                    any((directory / "generated-audio").rglob("*.partial.wav"))
                    for directory in (base_directory, *evidence_sources)
                ):
                    raise AuthoringWorkbenchError(
                        "Known-role fallback authority became active"
                    )
                for path, digest in (*base_snapshots, *evidence_snapshots):
                    if not path.is_file() or sha256_file(path) != digest:
                        raise AuthoringWorkbenchError(
                            "Known-role fallback authority changed before publication"
                        )
                for lease in held_leases:
                    lease.assert_owned()
                if destination.exists():
                    _directory, existing, _sha256 = load_workspace_authority(
                        destination
                    )
                    if existing.get("known_role_live_fallback") != batch:
                        raise AuthoringWorkbenchError(
                            "Known-role fallback destination conflicts"
                        )
                    return WorkspaceCreationResult(destination, False)
                try:
                    rename_directory_no_replace(staging, destination)
                except (AtomicPublicationError, OSError) as error:
                    if destination.exists():
                        _directory, existing, _sha256 = load_workspace_authority(
                            destination
                        )
                        if existing.get("known_role_live_fallback") == batch:
                            for lease in held_leases:
                                lease.mark_committed()
                            return WorkspaceCreationResult(destination, False)
                    raise AuthoringWorkbenchError(
                        f"Unable to publish known-role fallback workspace: {error}"
                    ) from error
                for lease in held_leases:
                    lease.mark_committed()
                staging = None
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
    except (BulkGenerationError, OSError, ValueError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return WorkspaceCreationResult(destination, True)


def validate_known_role_live_fallback_workspace(directory, workspace):
    """Validate the self-contained routed fallback batch."""
    batch = workspace.get("known_role_live_fallback")
    if batch is None:
        return
    common = {
        "schema",
        "schema_version",
        "batch_id",
        "base_workspace_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "voice_manifest_sha256",
        "route_binding_sha256",
        "queue_voice_overrides_sha256",
        "source_character",
        "synthesis_character",
        "items",
    }
    if (
        not isinstance(batch, dict)
        or set(batch) != common
        or batch.get("schema") != SCHEMA
        or batch.get("schema_version") != SCHEMA_VERSION
        or batch.get("batch_id")
        != canonical_document_sha256(
            {key: value for key, value in batch.items() if key != "batch_id"}
        )
    ):
        raise AuthoringWorkbenchError("Known-role fallback batch is malformed")
    for field in (
        "batch_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "voice_manifest_sha256",
        "route_binding_sha256",
        "queue_voice_overrides_sha256",
    ):
        require_workspace_sha256(batch.get(field), f"Known-role fallback {field}")
    root = Path(directory)
    manifest_path = contained_workspace_path(
        root,
        safe_workspace_relative_path(
            workspace["voice_manifest"]["path"], "Known-role fallback manifest"
        ),
        "Known-role fallback manifest",
    )
    manifest = load_workspace_json(manifest_path, "known-role fallback manifest")
    _metadata, voices = load_voice_manifest(manifest_path, allow_legacy=False)
    try:
        queue = load_generation_state(
            root / "generated-audio/generation-state.json", root / "queue.jsonl"
        )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    queue_document = load_stable_workspace_generation_state(
        root,
        workspace,
        "known-role fallback workspace",
        error_type=AuthoringWorkbenchError,
    )[0]
    queue_by_id = {item.queue_id: item for item in queue_document.items}
    overrides = queue_voice_overrides_from_manifest(
        manifest, queue_ids=queue_by_id, voices=voices
    )
    route = manifest.get(KNOWN_ROLE_REUSE_BINDING_FIELD)
    if (
        sha256_file(manifest_path) != batch["voice_manifest_sha256"]
        or not isinstance(route, dict)
        or canonical_document_sha256(route) != batch["route_binding_sha256"]
        or queue_voice_overrides_sha256(overrides)
        != batch["queue_voice_overrides_sha256"]
    ):
        raise AuthoringWorkbenchError("Known-role fallback route changed")
    items = batch.get("items")
    if not isinstance(items, list) or not items:
        raise AuthoringWorkbenchError("Known-role fallback item ledger is empty")
    observed = []
    for ledger in items:
        fields = {
            "queue_id",
            "evidence_workspace_id",
            "evidence_workspace_sha256",
            "evidence_config_fingerprint",
            "evidence_state_sha256",
            "evidence_item_sha256",
            "evidence_item",
        }
        if not isinstance(ledger, dict) or set(ledger) != fields:
            raise AuthoringWorkbenchError("Known-role fallback item is malformed")
        queue_id = ledger.get("queue_id")
        for field in (
            "evidence_workspace_sha256",
            "evidence_config_fingerprint",
            "evidence_state_sha256",
            "evidence_item_sha256",
        ):
            require_workspace_sha256(ledger.get(field), f"Known-role fallback {field}")
        result = queue["items"].get(queue_id)
        evidence = (
            result.get("live_fallback", {}).get("evidence")
            if isinstance(result, dict)
            else None
        )
        expected_evidence = {
            "schema": KNOWN_ROLE_LIVE_FALLBACK_EVIDENCE_SCHEMA,
            "schema_version": 1,
            "batch_id": batch["batch_id"],
            "queue_id": queue_id,
            "voice_manifest_sha256": batch["voice_manifest_sha256"],
            "route_binding_sha256": batch["route_binding_sha256"],
            "queue_voice_overrides_sha256": batch["queue_voice_overrides_sha256"],
            "source_character": batch["source_character"],
            "synthesis_character": batch["synthesis_character"],
            **{
                key: value
                for key, value in ledger.items()
                if key not in {"queue_id", "evidence_config_fingerprint"}
            },
        }
        if (
            queue_id not in queue_by_id
            or overrides.get(queue_id) != batch["synthesis_character"]
            or evidence != expected_evidence
        ):
            raise AuthoringWorkbenchError(
                f"Known-role fallback result changed for {queue_id!r}"
            )
        observed.append(queue_id)
    if observed != sorted(set(observed)):
        raise AuthoringWorkbenchError("Known-role fallback items are not canonical")


def _synthesis_configuration(run_config, override_sha256):
    policy = MissingVoicePolicy.from_document(run_config.get("missing_voice_policy"))
    repair = FailureRepairPolicy.from_document(run_config.get("failure_repair_policy"))
    overrides = {normalize_character_name(role): "Narrator" for role in policy.roles}
    return {
        "missing_voice_policy": policy.to_document(),
        "synthesis_character_overrides": dict(sorted(overrides.items())),
        "failure_repair_policy": repair.to_document(),
        "queue_voice_overrides_sha256": override_sha256,
    }


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
        target = contained_workspace_path(output, relative, "Known-role base WAV")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        snapshots.append((source, digest))


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AuthoringWorkbenchError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "create_known_role_live_fallback_workspace",
    "validate_known_role_live_fallback_workspace",
]

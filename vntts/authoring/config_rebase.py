"""Carry exact terminal audio decisions across an additive authoring config."""

from __future__ import annotations

import copy
import hashlib
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import VoiceManifestError, load_voice_manifest

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    _write_generated_manifest_from_state,
    load_generation_state,
    process_is_alive,
    synthesis_character_for_line,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
    rename_directory_no_replace,
)
from vntts.authoring.source_reference_bindings import (
    KNOWN_ROLE_REUSE_BINDING_FIELD,
    SourceReferenceBindingError,
    retired_source_reference_variants_from_manifest,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    WorkspaceCreationResult,
    _failure_reference_runtime_binding,
    _load_json,
    _read_file_bytes,
    _require_sha256,
    _safe_relative,
    _selected_voice_manifest,
    _terminal_review_outcome,
    _within,
    _workspace_config_fingerprint,
    _workspace_missing_voice_policy,
    _workspace_queue_voice_overrides,
    _workspace_voice_registry,
    default_workspaces_root,
    load_workspace_authority,
)

CONFIG_REBASE_SCHEMA = "vntts.authoring-workspace-config-rebase"
CONFIG_REBASE_VERSION = 3
SUPPORTED_CONFIG_REBASE_VERSIONS = frozenset({1, 2, CONFIG_REBASE_VERSION})
REBASE_CARRIED_TERMINAL = "carried_terminal"
REBASE_PENDING_KNOWN_ROLE_REUSE = "pending_after_known_role_reuse"
_WORKFLOW_FIELDS = {
    "carry_forward",
    "outcome_merge",
    "terminal_conflict_resolution",
    "config_rebase",
}


def rebase_workspace_config(source_workspace, target_workspace, workspaces_root=None):
    """Publish a successor with source terminal WAVs and target immutable config."""
    source_directory, source_document, source_workspace_sha256 = (
        load_workspace_authority(source_workspace)
    )
    target_directory, target_document, target_workspace_sha256 = (
        load_workspace_authority(target_workspace)
    )
    if source_directory == target_directory:
        raise AuthoringWorkbenchError("Config rebase requires distinct workspaces")
    if source_document["source"]["import_id"] != target_document["source"]["import_id"]:
        raise AuthoringWorkbenchError("Config rebase workspaces use different imports")
    source_queue = source_directory / "queue.jsonl"
    target_queue = target_directory / "queue.jsonl"
    source_queue_payload = _read_file_bytes(source_queue, "config rebase source queue")
    target_queue_payload = _read_file_bytes(target_queue, "config rebase target queue")
    if source_queue_payload != target_queue_payload:
        raise AuthoringWorkbenchError("Config rebase queues are not byte-identical")
    queue_sha256 = hashlib.sha256(source_queue_payload).hexdigest()
    source_output = source_directory / "generated-audio"
    target_output = target_directory / "generated-audio"

    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    staging = None
    try:
        with generation_publication_leases(
            ((source_output, queue_sha256), (target_output, queue_sha256)),
            process_checker=process_is_alive,
        ) as leases:
            source_directory, source_document, source_workspace_sha256 = (
                load_workspace_authority(source_directory)
            )
            target_directory, target_document, target_workspace_sha256 = (
                load_workspace_authority(target_directory)
            )
            audio_event_composition = _rebase_audio_event_composition(
                source_document, target_document
            )
            source_state_path = source_output / "generation-state.json"
            target_state_path = target_output / "generation-state.json"
            source_state_payload = _read_file_bytes(
                source_state_path, "config rebase source state"
            )
            target_state_payload = _read_file_bytes(
                target_state_path, "config rebase target state"
            )
            source_state_sha256 = hashlib.sha256(source_state_payload).hexdigest()
            target_state_sha256 = hashlib.sha256(target_state_payload).hexdigest()
            source_state = load_generation_state(source_state_path, source_queue)
            target_state = load_generation_state(target_state_path, target_queue)
            if (
                source_state.get("active") is not None
                or target_state.get("active") is not None
            ):
                raise AuthoringWorkbenchError(
                    "Config rebase authority has an active attempt"
                )
            if any(source_output.rglob("*.partial.wav")) or any(
                target_output.rglob("*.partial.wav")
            ):
                raise AuthoringWorkbenchError("Config rebase source is incomplete")

            queue = _load_json_queue(source_queue)
            source_registry = _workspace_voice_registry(
                source_directory, source_document
            )
            target_registry = _workspace_voice_registry(
                target_directory, target_document
            )
            source_overrides = _workspace_queue_voice_overrides(
                source_directory, source_document
            )
            source_failure_binding = _failure_reference_runtime_binding(
                source_directory, source_document
            )
            target_overrides = _workspace_queue_voice_overrides(
                target_directory, target_document
            )
            target_reference_sha256s = {
                sha256_file(reference)
                for voice in target_registry.unique_voices()
                for reference in voice.references
            }
            if audio_event_composition is not None:
                target_reference_sha256s.add(
                    _require_sha256(
                        audio_event_composition.get("final_audio_sha256"),
                        "Config rebase audio-event composition WAV SHA-256",
                    )
                )
            target_policy = _workspace_missing_voice_policy(target_document)
            source_policy = _workspace_missing_voice_policy(source_document)
            target_voice = _selected_voice_manifest(target_directory, target_document)
            try:
                target_voice_document, _target_voice_entries = load_voice_manifest(
                    target_voice, allow_legacy=False
                )
                retired_variants = retired_source_reference_variants_from_manifest(
                    target_voice_document
                )
                known_role_reuse = target_voice_document.get(
                    KNOWN_ROLE_REUSE_BINDING_FIELD
                )
            except (VoiceManifestError, SourceReferenceBindingError) as error:
                raise AuthoringWorkbenchError(str(error)) from error

            records = []
            projected_state = copy.deepcopy(target_state)
            for queue_id, result in sorted(source_state["items"].items()):
                if not isinstance(result, dict) or not _terminal_review_outcome(result):
                    continue
                queue_item = queue.get(queue_id)
                if queue_item is None:
                    raise AuthoringWorkbenchError(
                        f"Config rebase source item is absent from queue: {queue_id}"
                    )
                requested = synthesis_character_for_line(
                    queue_item.speaker, queue_item.voice_character
                )
                if target_policy.applies_to(requested) != source_policy.applies_to(
                    requested
                ):
                    source_route = _route_reference_identity(
                        source_registry,
                        source_document,
                        source_overrides,
                        queue_item,
                        result=result,
                        failure_reference_binding=source_failure_binding,
                    )
                    target_route = _route_reference_identity(
                        target_registry,
                        target_document,
                        target_overrides,
                        queue_item,
                        source_result=result,
                        failure_reference_binding=source_failure_binding,
                        allow_missing=True,
                    )
                    if not set(source_route[1]) & set(target_route[1]):
                        raise AuthoringWorkbenchError(
                            "Config rebase changes fallback reference bytes for "
                            f"terminal item {queue_id!r}"
                        )
                source_route = _route_reference_identity(
                    source_registry,
                    source_document,
                    source_overrides,
                    queue_item,
                    result=result,
                    failure_reference_binding=source_failure_binding,
                )
                target_route = _route_reference_identity(
                    target_registry,
                    target_document,
                    target_overrides,
                    queue_item,
                    source_result=result,
                    failure_reference_binding=source_failure_binding,
                    allow_missing=True,
                )
                if not set(source_route[1]).issubset(target_reference_sha256s):
                    raise AuthoringWorkbenchError(
                        "Config rebase target omits source reference bytes for "
                        f"{queue_id!r}"
                    )
                route_status = _target_route_status(
                    queue_id,
                    result.get("status"),
                    result.get("review_status"),
                    source_route,
                    target_route,
                    retired_variants,
                    known_role_reuse,
                    canonical_document_sha256(result),
                )
                relative = _safe_relative(
                    result.get("path"), f"Config rebase item {queue_id!r} WAV"
                )
                audio = _within(source_output, relative, "Config rebase source WAV")
                payload = _read_file_bytes(audio, "config rebase source WAV")
                audio_sha256 = hashlib.sha256(payload).hexdigest()
                if audio_sha256 != _require_sha256(
                    result.get("file_sha256"),
                    f"Config rebase item {queue_id!r} WAV SHA-256",
                ):
                    raise AuthoringWorkbenchError(
                        f"Config rebase source WAV changed for {queue_id!r}"
                    )
                source_live_fallback = result.get("live_fallback")
                projected = _project_source_item(
                    result, exclude_live_fallback=source_live_fallback is not None
                )
                record = {
                    "queue_id": queue_id,
                    "source_item_sha256": canonical_document_sha256(result),
                    "projected_item_sha256": canonical_document_sha256(projected),
                    "audio_sha256": audio_sha256,
                    "status": result["status"],
                    "review_status": result["review_status"],
                    "source_effective_character": source_route[0],
                    "target_effective_character": target_route[0],
                    "source_reference_sha256s": list(source_route[1]),
                    "target_reference_sha256s": list(target_route[1]),
                    "target_route_status": route_status,
                    "successor_state": (
                        REBASE_PENDING_KNOWN_ROLE_REUSE
                        if _known_role_reuse_requeues_rejection(
                            queue_id,
                            result.get("status"),
                            result.get("review_status"),
                            target_route,
                            route_status,
                            known_role_reuse,
                            canonical_document_sha256(result),
                        )
                        else REBASE_CARRIED_TERMINAL
                    ),
                }
                projected["config_rebase"] = {
                    key: value for key, value in record.items() if key != "queue_id"
                }
                if source_live_fallback is not None:
                    projected["live_fallback"] = copy.deepcopy(source_live_fallback)
                if record["successor_state"] == REBASE_PENDING_KNOWN_ROLE_REUSE:
                    projected_state["items"].pop(queue_id, None)
                else:
                    projected_state["items"][queue_id] = projected
                records.append(record)
            if not records:
                raise AuthoringWorkbenchError(
                    "Config rebase source has no terminal review outcomes"
                )

            source_voice = _selected_voice_manifest(source_directory, source_document)
            rebase = {
                "schema": CONFIG_REBASE_SCHEMA,
                "schema_version": CONFIG_REBASE_VERSION,
                "source_workspace_id": source_document["workspace_id"],
                "source_workspace_sha256": source_workspace_sha256,
                "source_state_sha256": source_state_sha256,
                "source_voice_manifest_sha256": sha256_file(source_voice),
                "target_workspace_id": target_document["workspace_id"],
                "target_workspace_sha256": target_workspace_sha256,
                "target_state_sha256": target_state_sha256,
                "target_voice_manifest_sha256": sha256_file(target_voice),
                "queue_sha256": queue_sha256,
                "items": records,
            }
            config_fingerprint = _workspace_config_fingerprint(
                target_document["source"]["import_id"],
                target_document.get("story_index"),
                target_document.get("voice_manifest"),
                target_document["narrator_character"],
                target_document["run_config"],
                target_document.get("carry_forward"),
                target_document.get("outcome_merge"),
                target_document.get("failure_reference_binding"),
                target_document.get("terminal_conflict_merge"),
                config_rebase=rebase,
                audio_event_composition=audio_event_composition,
            )
            workspace_id = (
                "resume-"
                + target_document["source"]["import_id"].removeprefix("legacy-")
                + f"-{config_fingerprint[:16]}"
            )
            destination = _within(root, Path(workspace_id), "Config rebase destination")
            staging = Path(
                tempfile.mkdtemp(prefix=".config-rebase-staging-", dir=root)
            ).resolve()
            snapshots = []
            snapshots.extend(
                (
                    (source_directory / "workspace.json", source_workspace_sha256),
                    (source_state_path, source_state_sha256),
                    (source_queue, queue_sha256),
                    (target_directory / "workspace.json", target_workspace_sha256),
                    (target_state_path, target_state_sha256),
                )
            )
            _copy_tree(target_directory / "inputs", staging / "inputs", snapshots)
            if (
                audio_event_composition is not None
                and target_document.get("audio_event_composition") is None
            ):
                _copy_audio_event_composition_inputs(
                    source_directory,
                    staging,
                    audio_event_composition,
                    snapshots,
                )
            _copy_tree(
                target_directory / "provenance", staging / "provenance", snapshots
            )
            (staging / "queue.jsonl").write_bytes(target_queue_payload)
            snapshots.append((target_queue, queue_sha256))
            source_root = staging / "provenance" / "config-rebase" / "source-root"
            _copy_tree(source_directory / "inputs", source_root / "inputs", snapshots)
            (source_root / "queue.jsonl").parent.mkdir(parents=True, exist_ok=True)
            (source_root / "queue.jsonl").write_bytes(source_queue_payload)
            (source_root / "workspace.json").write_bytes(
                _read_file_bytes(
                    source_directory / "workspace.json", "source workspace"
                )
            )
            (source_root / "generated-audio").mkdir(parents=True)
            (source_root / "generated-audio" / "generation-state.json").write_bytes(
                source_state_payload
            )
            for record in records:
                if record["successor_state"] != REBASE_PENDING_KNOWN_ROLE_REUSE:
                    continue
                source_item = source_state["items"][record["queue_id"]]
                relative = _safe_relative(
                    source_item.get("path"),
                    f"Config rebase pending-history {record['queue_id']!r} WAV",
                )
                source_audio = _within(
                    source_output, relative, "Config rebase pending-history WAV"
                )
                history_audio = _within(
                    source_root / "generated-audio",
                    relative,
                    "Config rebase pending-history WAV",
                )
                history_audio.parent.mkdir(parents=True, exist_ok=True)
                payload = _read_file_bytes(
                    source_audio, "config rebase pending-history WAV"
                )
                history_audio.write_bytes(payload)
                snapshots.append((source_audio, record["audio_sha256"]))
            target_root = staging / "provenance" / "config-rebase" / "target-root"
            target_root.mkdir(parents=True)
            (target_root / "workspace.json").write_bytes(
                _read_file_bytes(
                    target_directory / "workspace.json", "target workspace"
                )
            )
            (target_root / "generation-state.json").write_bytes(target_state_payload)

            output = staging / "generated-audio"
            output.mkdir()
            path_owners = {}
            rebased_queue_ids = {record["queue_id"] for record in records}
            for queue_id, result in projected_state["items"].items():
                if not isinstance(result, dict) or not isinstance(
                    result.get("path"), str
                ):
                    continue
                relative = _safe_relative(
                    result["path"], f"Config rebase state item {queue_id!r} WAV"
                )
                previous = path_owners.setdefault(relative.as_posix(), queue_id)
                if previous != queue_id:
                    raise AuthoringWorkbenchError(
                        f"Config rebase WAV path collides with {previous!r}"
                    )
                authority_output = (
                    source_output if queue_id in rebased_queue_ids else target_output
                )
                source_audio = _within(
                    authority_output, relative, "Config rebase state WAV"
                )
                payload = _read_file_bytes(source_audio, "config rebase state WAV")
                digest = hashlib.sha256(payload).hexdigest()
                if digest != _require_sha256(
                    result.get("file_sha256"),
                    f"Config rebase state item {queue_id!r} WAV SHA-256",
                ):
                    raise AuthoringWorkbenchError(
                        f"Config rebase state WAV changed for {queue_id!r}"
                    )
                target = _within(output, relative, "Config rebase output WAV")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                snapshots.append((source_audio, digest))
            atomic_write_json(
                output / "generation-state.json", projected_state, sort_keys=True
            )
            workspace = copy.deepcopy(target_document)
            for field in (
                "carry_forward",
                "outcome_merge",
                "terminal_conflict_merge",
                "failure_reference_binding",
            ):
                workspace.pop(field, None)
            if audio_event_composition is not None:
                workspace["audio_event_composition"] = copy.deepcopy(
                    audio_event_composition
                )
            workspace.update(
                {
                    "workspace_id": workspace_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "config_rebase": rebase,
                    "config_fingerprint": config_fingerprint,
                }
            )
            atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
            _write_generated_manifest_from_state(
                projected_state, output, output / "manifest.json"
            )
            validate_config_rebase_workspace(staging, workspace, projected_state)
            # The focused projection validator accepts an in-memory state to
            # avoid re-reading a concurrently changing source. Before
            # publication, also load the complete state from disk so
            # workspace-level item authorities (notably audio-event
            # composition) cannot be omitted from an otherwise valid state.
            load_generation_state(
                output / "generation-state.json", staging / "queue.jsonl"
            )
            for path, digest in snapshots:
                if not path.is_file() or sha256_file(path) != digest:
                    raise AuthoringWorkbenchError(
                        f"Config rebase source changed during publication: {path}"
                    )
            for lease in leases:
                lease.assert_owned()
            if destination.exists():
                _directory, existing, _digest = load_workspace_authority(destination)
                if existing.get("config_rebase") != rebase:
                    raise AuthoringWorkbenchError(
                        "Config rebase destination contains different authority"
                    )
                return WorkspaceCreationResult(destination, False)
            try:
                rename_directory_no_replace(staging, destination)
            except (AtomicPublicationError, OSError) as error:
                if destination.exists():
                    _directory, existing, _digest = load_workspace_authority(
                        destination
                    )
                    if existing.get("config_rebase") == rebase:
                        for lease in leases:
                            lease.mark_committed()
                        return WorkspaceCreationResult(destination, False)
                raise AuthoringWorkbenchError(
                    f"Unable to publish config rebase workspace: {error}"
                ) from error
            for lease in leases:
                lease.mark_committed()
            staging = None
            return WorkspaceCreationResult(destination, True)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)


def validate_config_rebase_workspace(directory, workspace, state=None):
    """Validate self-contained config-rebase authority and exact item projection."""
    rebase = workspace.get("config_rebase")
    if rebase is None:
        return
    required = {
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
        "queue_sha256",
        "items",
    }
    if (
        not isinstance(rebase, dict)
        or set(rebase) != required
        or rebase.get("schema") != CONFIG_REBASE_SCHEMA
        or rebase.get("schema_version") not in SUPPORTED_CONFIG_REBASE_VERSIONS
    ):
        raise AuthoringWorkbenchError("Workspace config rebase ledger is malformed")
    for field in (
        "source_workspace_sha256",
        "source_state_sha256",
        "source_voice_manifest_sha256",
        "target_workspace_sha256",
        "target_state_sha256",
        "target_voice_manifest_sha256",
        "queue_sha256",
    ):
        _require_sha256(rebase.get(field), f"Config rebase {field}")
    directory = Path(directory).resolve()
    queue_path = directory / "queue.jsonl"
    if sha256_file(queue_path) != rebase["queue_sha256"]:
        raise AuthoringWorkbenchError("Config rebase queue was modified")
    source_root = directory / "provenance" / "config-rebase" / "source-root"
    source_workspace = source_root / "workspace.json"
    source_state_path = source_root / "generated-audio" / "generation-state.json"
    target_root = directory / "provenance" / "config-rebase" / "target-root"
    target_workspace = target_root / "workspace.json"
    target_state = target_root / "generation-state.json"
    for path, digest, label in (
        (source_workspace, rebase["source_workspace_sha256"], "source workspace"),
        (source_state_path, rebase["source_state_sha256"], "source state"),
        (target_workspace, rebase["target_workspace_sha256"], "target workspace"),
        (target_state, rebase["target_state_sha256"], "target state"),
    ):
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise AuthoringWorkbenchError(f"Config rebase {label} snapshot changed")
    source_document = _load_json(source_workspace, "config rebase source workspace")
    target_document = _load_json(target_workspace, "config rebase target workspace")
    if (
        source_document.get("workspace_id") != rebase["source_workspace_id"]
        or target_document.get("workspace_id") != rebase["target_workspace_id"]
    ):
        raise AuthoringWorkbenchError("Config rebase workspace identity changed")
    source_voice = _selected_voice_manifest(source_root, source_document)
    selected_voice = _selected_voice_manifest(directory, workspace)
    if (
        sha256_file(source_voice) != rebase["source_voice_manifest_sha256"]
        or sha256_file(selected_voice) != rebase["target_voice_manifest_sha256"]
    ):
        raise AuthoringWorkbenchError("Config rebase voice authority changed")
    try:
        target_voice_document, _target_voice_entries = load_voice_manifest(
            selected_voice, allow_legacy=False
        )
        retired_variants = retired_source_reference_variants_from_manifest(
            target_voice_document
        )
        known_role_reuse = target_voice_document.get(KNOWN_ROLE_REUSE_BINDING_FIELD)
    except (VoiceManifestError, SourceReferenceBindingError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    target_registry = _workspace_voice_registry(directory, workspace)
    target_reference_sha256s = {
        sha256_file(reference)
        for voice in target_registry.unique_voices()
        for reference in voice.references
    }
    audio_event_composition = workspace.get("audio_event_composition")
    if isinstance(audio_event_composition, dict):
        target_reference_sha256s.add(
            _require_sha256(
                audio_event_composition.get("final_audio_sha256"),
                "Config rebase audio-event composition WAV SHA-256",
            )
        )
    source_state = _load_json(source_state_path, "config rebase source state")
    if not isinstance(source_state.get("items"), dict):
        raise AuthoringWorkbenchError(
            "Config rebase source state snapshot is malformed"
        )
    if state is None:
        state = load_generation_state(
            directory / "generated-audio" / "generation-state.json", queue_path
        )
    records = rebase.get("items")
    if not isinstance(records, list) or not records:
        raise AuthoringWorkbenchError("Config rebase item ledger is empty")
    later_extensions = {}
    outcome_merge = workspace.get("outcome_merge")
    if isinstance(outcome_merge, dict) and isinstance(outcome_merge.get("items"), list):
        for item in outcome_merge["items"]:
            if isinstance(item, dict) and isinstance(item.get("queue_id"), str):
                later_extensions.setdefault(item["queue_id"], {})["outcome_merge"] = {
                    key: value for key, value in item.items() if key != "queue_id"
                }
    terminal_merge = workspace.get("terminal_conflict_merge")
    if isinstance(terminal_merge, dict) and isinstance(
        terminal_merge.get("items"), list
    ):
        for item in terminal_merge["items"]:
            if isinstance(item, dict) and isinstance(item.get("queue_id"), str):
                later_extensions.setdefault(item["queue_id"], {})[
                    "terminal_conflict_resolution"
                ] = {key: value for key, value in item.items() if key != "queue_id"}
    observed_ids = []
    for record in records:
        record_fields = {
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
        if rebase["schema_version"] >= 2:
            record_fields.add("target_route_status")
        if rebase["schema_version"] >= 3:
            record_fields.add("successor_state")
        if not isinstance(record, dict) or set(record) != record_fields:
            raise AuthoringWorkbenchError("Config rebase item record is malformed")
        queue_id = record["queue_id"]
        if not isinstance(queue_id, str) or not queue_id:
            raise AuthoringWorkbenchError("Config rebase queue identity is invalid")
        observed_ids.append(queue_id)
        for field in ("source_item_sha256", "projected_item_sha256", "audio_sha256"):
            _require_sha256(record.get(field), f"Config rebase item {field}")
        for field in ("source_reference_sha256s", "target_reference_sha256s"):
            values = record.get(field)
            if not isinstance(values, list) or values != sorted(set(values)):
                raise AuthoringWorkbenchError(
                    f"Config rebase item {field} is not canonical"
                )
            if not values and (
                field == "source_reference_sha256s" or rebase["schema_version"] < 2
            ):
                raise AuthoringWorkbenchError(
                    f"Config rebase item {field} is not canonical"
                )
            for value in values:
                _require_sha256(value, f"Config rebase item {field}")
        if not set(record["source_reference_sha256s"]).issubset(
            target_reference_sha256s
        ):
            raise AuthoringWorkbenchError(
                f"Config rebase target omits source reference bytes for {queue_id!r}"
            )
        for field in ("source_effective_character", "target_effective_character"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise AuthoringWorkbenchError(f"Config rebase item {field} is invalid")
        expected_route_status = _target_route_status(
            queue_id,
            record.get("status"),
            record.get("review_status"),
            (
                record["source_effective_character"],
                tuple(record["source_reference_sha256s"]),
            ),
            (
                record["target_effective_character"],
                tuple(record["target_reference_sha256s"]),
            ),
            retired_variants,
            known_role_reuse,
            record["source_item_sha256"],
        )
        if record.get("target_route_status", "active") != expected_route_status:
            raise AuthoringWorkbenchError(
                f"Config rebase target route status is invalid for {queue_id!r}"
            )
        expected_successor_state = (
            REBASE_PENDING_KNOWN_ROLE_REUSE
            if _known_role_reuse_requeues_rejection(
                queue_id,
                record.get("status"),
                record.get("review_status"),
                (
                    record["target_effective_character"],
                    tuple(record["target_reference_sha256s"]),
                ),
                expected_route_status,
                known_role_reuse,
                record["source_item_sha256"],
            )
            else REBASE_CARRIED_TERMINAL
        )
        if record.get("successor_state", REBASE_CARRIED_TERMINAL) != (
            expected_successor_state
        ):
            raise AuthoringWorkbenchError(
                f"Config rebase successor state is invalid for {queue_id!r}"
            )
        source_item = source_state["items"].get(queue_id)
        if (
            not isinstance(source_item, dict)
            or canonical_document_sha256(source_item) != record["source_item_sha256"]
        ):
            raise AuthoringWorkbenchError(
                f"Config rebase source item changed for {queue_id!r}"
            )
        source_live_fallback = source_item.get("live_fallback")
        projected = _project_source_item(
            source_item, exclude_live_fallback=source_live_fallback is not None
        )
        if canonical_document_sha256(projected) != record["projected_item_sha256"]:
            raise AuthoringWorkbenchError(
                f"Config rebase projected source changed for {queue_id!r}"
            )
        current = state["items"].get(queue_id)
        expected_extension = {
            key: value for key, value in record.items() if key != "queue_id"
        }
        if expected_successor_state == REBASE_PENDING_KNOWN_ROLE_REUSE:
            if isinstance(current, dict) and "config_rebase" in current:
                raise AuthoringWorkbenchError(
                    f"Config rebase pending item retained terminal history for {queue_id!r}"
                )
            historical_audio = _within(
                source_root / "generated-audio",
                _safe_relative(
                    source_item.get("path"), "Config rebase pending-history WAV"
                ),
                "Config rebase pending-history WAV",
            )
            if (
                historical_audio.is_symlink()
                or not historical_audio.is_file()
                or sha256_file(historical_audio) != record["audio_sha256"]
            ):
                raise AuthoringWorkbenchError(
                    f"Config rebase pending-history WAV changed for {queue_id!r}"
                )
            continue
        if not isinstance(current, dict):
            raise AuthoringWorkbenchError(
                f"Config rebase state item changed for {queue_id!r}"
            )
        live_fallback = current.get("live_fallback")
        if source_live_fallback is not None:
            expected = copy.deepcopy(projected)
            expected["config_rebase"] = expected_extension
            expected["live_fallback"] = copy.deepcopy(source_live_fallback)
            if current != expected:
                raise AuthoringWorkbenchError(
                    f"Config rebase carried live fallback changed for {queue_id!r}"
                )
            audio = _within(
                directory / "generated-audio",
                _safe_relative(current.get("path"), "Config rebase WAV"),
                "Config rebase WAV",
            )
            if not audio.is_file() or sha256_file(audio) != record["audio_sha256"]:
                raise AuthoringWorkbenchError(
                    f"Config rebase WAV changed for {queue_id!r}"
                )
            continue
        if live_fallback is not None:
            expected_base = copy.deepcopy(projected)
            expected_base["config_rebase"] = expected_extension
            if (
                not isinstance(live_fallback, dict)
                or live_fallback.get("reason") != "generated_audio_rejected"
                or live_fallback.get("previous_result_sha256")
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
            audio = _within(
                directory / "generated-audio",
                _safe_relative(current.get("path"), "Config rebase WAV"),
                "Config rebase WAV",
            )
            if not audio.is_file() or sha256_file(audio) != record["audio_sha256"]:
                raise AuthoringWorkbenchError(
                    f"Config rebase WAV changed for {queue_id!r}"
                )
            continue
        overlays = later_extensions.get(queue_id, {})
        if overlays and all(
            current.get(key) == value for key, value in overlays.items()
        ):
            observed_extension = current.get("config_rebase")
            if (
                observed_extension is not None
                and observed_extension != expected_extension
            ):
                raise AuthoringWorkbenchError(
                    f"Config rebase state item changed for {queue_id!r}"
                )
            continue
        if current.get("config_rebase") != expected_extension:
            raise AuthoringWorkbenchError(
                f"Config rebase state item changed for {queue_id!r}"
            )
        if (
            source_item.get("status") != record["status"]
            or source_item.get("review_status") != record["review_status"]
            or current.get("status") != record["status"]
            or current.get("review_status") != record["review_status"]
        ):
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
        audio = _within(
            directory / "generated-audio",
            _safe_relative(current.get("path"), "Config rebase WAV"),
            "Config rebase WAV",
        )
        if not audio.is_file() or sha256_file(audio) != record["audio_sha256"]:
            raise AuthoringWorkbenchError(f"Config rebase WAV changed for {queue_id!r}")
    if observed_ids != sorted(set(observed_ids)):
        raise AuthoringWorkbenchError("Config rebase item ledger is not canonical")
    marked_ids = sorted(
        queue_id
        for queue_id, item in state["items"].items()
        if isinstance(item, dict) and "config_rebase" in item
    )
    expected_marked_ids = sorted(
        queue_id
        for queue_id in observed_ids
        if isinstance(state["items"].get(queue_id), dict)
        and "config_rebase" in state["items"][queue_id]
    )
    if marked_ids != expected_marked_ids:
        raise AuthoringWorkbenchError("Config rebase state ledger is incomplete")


def validate_config_rebase_publication_authority(state_path, state):
    """Bind marked state to one validated canonical config-rebase workspace."""
    marked = any(
        isinstance(item, dict) and "config_rebase" in item
        for item in state.get("items", {}).values()
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


def _project_source_item(result, *, exclude_live_fallback=False):
    projected = copy.deepcopy(result)
    for field in _WORKFLOW_FIELDS:
        projected.pop(field, None)
    if exclude_live_fallback:
        projected.pop("live_fallback", None)
    return projected


def _retired_route_for_queue(records, queue_id, source_route):
    character, reference_sha256s = source_route
    for record in records:
        if (
            queue_id in record["queue_ids"]
            and record["voice_character"] == character
            and record["reference_sha256"] in reference_sha256s
        ):
            return record
    return None


def _target_route_status(
    queue_id,
    status,
    review_status,
    source_route,
    target_route,
    retired_variants,
    known_role_reuse=None,
    source_item_sha256=None,
):
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
    queue_id,
    status,
    review_status,
    target_route,
    route_status,
    known_role_reuse,
    source_item_sha256,
):
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
    registry,
    workspace,
    overrides,
    queue_item,
    result=None,
    *,
    source_result=None,
    failure_reference_binding=None,
    allow_missing=False,
):
    audio_event_result = result if result is not None else source_result
    if (
        isinstance(audio_event_result, dict)
        and audio_event_result.get("provider") == "original-game-audio-event"
        and isinstance(audio_event_result.get("audio_event_composition"), dict)
    ):
        return (
            "Audio Event",
            (
                _require_sha256(
                    audio_event_result["audio_event_composition"].get(
                        "final_audio_sha256"
                    ),
                    "Config rebase audio-event result WAV SHA-256",
                ),
            ),
        )
    failure_route = _failure_reference_route(
        failure_reference_binding,
        queue_item,
        result if result is not None else source_result,
    )
    if failure_route is not None:
        source_route, requested = failure_route
        if result is not None:
            return source_route
    else:
        requested = synthesis_character_for_line(
            queue_item.speaker, queue_item.voice_character
        )
    prior_route = _prior_config_rebase_target_route(result)
    if prior_route is not None:
        return prior_route
    character = None
    if isinstance(result, dict):
        character = result.get("voice_character")
    character = character or overrides.get(queue_item.queue_id) or requested
    if character == "Narrator":
        character = workspace["narrator_character"]
    voice = registry.resolve(character)
    if voice is None or not voice.references:
        policy = _workspace_missing_voice_policy(workspace)
        if policy.applies_to(requested):
            character = workspace["narrator_character"]
            voice = registry.resolve(character)
    if voice is None or not voice.references:
        if allow_missing:
            return character, ()
        raise AuthoringWorkbenchError(
            f"Config rebase voice references are missing for {character!r}"
        )
    digests = tuple(sorted(sha256_file(reference) for reference in voice.references))
    return character, digests


def _failure_reference_route(binding, queue_item, result):
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
    if runtime_character is not None:
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
        digests = historical_references
    return (synthetic_character, digests), requested.strip()


def _historical_failure_reference_digests(result, synthetic_character):
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
            _require_sha256(
                digest, "Config rebase historical failure reference SHA-256"
            )
            for digest in voice["references"]
        )
    )


def _prior_config_rebase_target_route(result):
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
        _require_sha256(value, "Prior config rebase target reference SHA-256")
        for value in values
    )
    return character, digests


def _rebase_audio_event_composition(source_workspace, target_workspace):
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
    source_directory, staging, composition, snapshots
):
    paths = sorted(
        {
            value
            for key, value in composition.items()
            if (key == "path" or key.endswith("_path"))
            and isinstance(value, str)
            and value
        }
    )
    if not paths:
        raise AuthoringWorkbenchError(
            "Config rebase audio-event composition has no input paths"
        )
    roots = set()
    for value in paths:
        relative = _safe_relative(value, "Config rebase audio-event composition input")
        if len(relative.parts) < 3 or relative.parts[0] != "inputs":
            raise AuthoringWorkbenchError(
                "Config rebase audio-event composition input leaves inputs"
            )
        source_file = _within(
            source_directory,
            relative,
            "Config rebase audio-event composition source",
        )
        if source_file.is_symlink() or not source_file.is_file():
            raise AuthoringWorkbenchError(
                f"Config rebase audio-event composition input is missing: {value}"
            )
        roots.add(Path(*relative.parts[:2]))
    for relative_root in sorted(roots):
        source_root = _within(
            source_directory,
            relative_root,
            "Config rebase audio-event composition source root",
        )
        if source_root.is_symlink() or not source_root.is_dir():
            raise AuthoringWorkbenchError(
                "Config rebase audio-event composition source root is invalid"
            )
        for source in sorted(source_root.rglob("*")):
            if source.is_symlink():
                raise AuthoringWorkbenchError(
                    "Config rebase audio-event composition contains a symlink"
                )
            if source.is_dir():
                continue
            relative = relative_root / source.relative_to(source_root)
            payload = _read_file_bytes(source, "config rebase audio-event composition")
            digest = hashlib.sha256(payload).hexdigest()
            destination = _within(
                staging, relative, "Config rebase audio-event composition target"
            )
            if destination.exists():
                if destination.is_symlink() or not destination.is_file():
                    raise AuthoringWorkbenchError(
                        "Config rebase audio-event composition target is unsafe"
                    )
                if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    raise AuthoringWorkbenchError(
                        "Config rebase audio-event composition input conflicts"
                    )
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
            snapshots.append((source, digest))


def _copy_tree(source, destination, snapshots):
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
        payload = _read_file_bytes(path, "config rebase input")
        digest = hashlib.sha256(payload).hexdigest()
        target = _within(destination, relative, "Config rebase input")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        snapshots.append((path, digest))


def _load_json_queue(path):
    from vntts_artifacts import VoiceGenerationQueue

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

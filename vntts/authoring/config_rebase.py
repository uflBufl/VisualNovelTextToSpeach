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

from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    _canonical_sha256,
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
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    WorkspaceCreationResult,
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
CONFIG_REBASE_VERSION = 1
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
            target_overrides = _workspace_queue_voice_overrides(
                target_directory, target_document
            )
            target_reference_sha256s = {
                sha256_file(reference)
                for voice in target_registry.unique_voices()
                for reference in voice.references
            }
            target_policy = _workspace_missing_voice_policy(target_document)
            source_policy = _workspace_missing_voice_policy(source_document)

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
                    )
                    target_route = _route_reference_identity(
                        target_registry,
                        target_document,
                        target_overrides,
                        queue_item,
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
                )
                target_route = _route_reference_identity(
                    target_registry,
                    target_document,
                    target_overrides,
                    queue_item,
                )
                if source_route[0] != target_route[0]:
                    raise AuthoringWorkbenchError(
                        "Config rebase changes the effective character for "
                        f"{queue_id!r}"
                    )
                if not set(source_route[1]).issubset(target_reference_sha256s):
                    raise AuthoringWorkbenchError(
                        "Config rebase target omits source reference bytes for "
                        f"{queue_id!r}"
                    )
                if not set(source_route[1]).issubset(target_route[1]):
                    raise AuthoringWorkbenchError(
                        "Config rebase changes the effective reference for "
                        f"terminal item {queue_id!r}"
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
                projected = _project_source_item(result)
                record = {
                    "queue_id": queue_id,
                    "source_item_sha256": _canonical_sha256(result),
                    "projected_item_sha256": _canonical_sha256(projected),
                    "audio_sha256": audio_sha256,
                    "status": result["status"],
                    "review_status": result["review_status"],
                    "source_effective_character": source_route[0],
                    "target_effective_character": target_route[0],
                    "source_reference_sha256s": list(source_route[1]),
                    "target_reference_sha256s": list(target_route[1]),
                }
                projected["config_rebase"] = {
                    key: value for key, value in record.items() if key != "queue_id"
                }
                projected_state["items"][queue_id] = projected
                records.append(record)
            if not records:
                raise AuthoringWorkbenchError(
                    "Config rebase source has no terminal review outcomes"
                )

            source_voice = _selected_voice_manifest(source_directory, source_document)
            target_voice = _selected_voice_manifest(target_directory, target_document)
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
                config_rebase=rebase,
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
        or rebase.get("schema_version") != CONFIG_REBASE_VERSION
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
    observed_ids = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
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
        }:
            raise AuthoringWorkbenchError("Config rebase item record is malformed")
        queue_id = record["queue_id"]
        if not isinstance(queue_id, str) or not queue_id:
            raise AuthoringWorkbenchError("Config rebase queue identity is invalid")
        observed_ids.append(queue_id)
        for field in ("source_item_sha256", "projected_item_sha256", "audio_sha256"):
            _require_sha256(record.get(field), f"Config rebase item {field}")
        for field in ("source_reference_sha256s", "target_reference_sha256s"):
            values = record.get(field)
            if (
                not isinstance(values, list)
                or not values
                or values != sorted(set(values))
            ):
                raise AuthoringWorkbenchError(
                    f"Config rebase item {field} is not canonical"
                )
            for value in values:
                _require_sha256(value, f"Config rebase item {field}")
        for field in ("source_effective_character", "target_effective_character"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise AuthoringWorkbenchError(f"Config rebase item {field} is invalid")
        if record["source_effective_character"] != record["target_effective_character"]:
            raise AuthoringWorkbenchError(
                f"Config rebase item changes effective character for {queue_id!r}"
            )
        if not set(record["source_reference_sha256s"]).issubset(
            record["target_reference_sha256s"]
        ):
            raise AuthoringWorkbenchError(
                f"Config rebase item changes reference bytes for {queue_id!r}"
            )
        source_item = source_state["items"].get(queue_id)
        if (
            not isinstance(source_item, dict)
            or _canonical_sha256(source_item) != record["source_item_sha256"]
        ):
            raise AuthoringWorkbenchError(
                f"Config rebase source item changed for {queue_id!r}"
            )
        projected = _project_source_item(source_item)
        if _canonical_sha256(projected) != record["projected_item_sha256"]:
            raise AuthoringWorkbenchError(
                f"Config rebase projected source changed for {queue_id!r}"
            )
        current = state["items"].get(queue_id)
        expected_extension = {
            key: value for key, value in record.items() if key != "queue_id"
        }
        if (
            not isinstance(current, dict)
            or current.get("config_rebase") != expected_extension
        ):
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
            _canonical_sha256(current_without_extension)
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
    if marked_ids != observed_ids:
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


def _project_source_item(result):
    projected = copy.deepcopy(result)
    for field in _WORKFLOW_FIELDS:
        projected.pop(field, None)
    return projected


def _route_reference_identity(registry, workspace, overrides, queue_item, result=None):
    requested = synthesis_character_for_line(
        queue_item.speaker, queue_item.voice_character
    )
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
        raise AuthoringWorkbenchError(
            f"Config rebase voice references are missing for {character!r}"
        )
    digests = tuple(sorted(sha256_file(reference) for reference in voice.references))
    return character, digests


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
    "rebase_workspace_config",
    "validate_config_rebase_publication_authority",
    "validate_config_rebase_workspace",
]

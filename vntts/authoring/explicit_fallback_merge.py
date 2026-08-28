"""Compose exact standalone live-fallback authorities into one workspace."""

from __future__ import annotations

import copy
import hashlib
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    process_is_alive,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
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
from vntts.authoring.workspace_config import workspace_config_fingerprint
from vntts.authoring.workspace_foundation import copy_workspace_tree_snapshot
from vntts.authoring.workspace_state import load_stable_workspace_generation_state

SCHEMA = "vntts.authoring-explicit-fallback-merge"
SCHEMA_VERSION = 1


def merge_explicit_live_fallbacks(
    base_workspace,
    source_workspace,
    queue_ids,
    workspaces_root=None,
):
    """Publish a successor containing only named standalone fallback decisions."""
    base_directory, base_document, base_workspace_sha256 = load_workspace_authority(
        base_workspace
    )
    source_directory, source_document, source_workspace_sha256 = (
        load_workspace_authority(source_workspace)
    )
    if base_directory == source_directory:
        raise AuthoringWorkbenchError(
            "Explicit fallback source must differ from its base"
        )
    selected_ids = tuple(sorted(queue_ids))
    if not selected_ids or len(set(selected_ids)) != len(selected_ids):
        raise AuthoringWorkbenchError(
            "Explicit fallback merge requires unique exact queue IDs"
        )
    if source_document["source"] != base_document["source"]:
        raise AuthoringWorkbenchError(
            "Explicit fallback workspaces must share one immutable import"
        )

    base_queue, base_state, _base_payload, base_state_sha256 = (
        load_stable_workspace_generation_state(
            base_directory,
            base_document,
            "explicit fallback base",
            error_type=AuthoringWorkbenchError,
        )
    )
    source_queue, source_state, _source_payload, source_state_sha256 = (
        load_stable_workspace_generation_state(
            source_directory,
            source_document,
            "explicit fallback source",
            error_type=AuthoringWorkbenchError,
        )
    )
    base_queue_path = base_directory / "queue.jsonl"
    source_queue_path = source_directory / "queue.jsonl"
    base_queue_sha256 = sha256_file(base_queue_path)
    if (
        sha256_file(source_queue_path) != base_queue_sha256
        or source_queue.metadata != base_queue.metadata
        or [item.document for item in source_queue.items]
        != [item.document for item in base_queue.items]
    ):
        raise AuthoringWorkbenchError(
            "Explicit fallback source queue differs from its base"
        )
    queue_by_id = {item.queue_id: item for item in base_queue.items}
    ledgers = []
    selected_items = {}
    for queue_id in selected_ids:
        queue_item = queue_by_id.get(queue_id)
        source_item = source_state["items"].get(queue_id)
        base_item = base_state["items"].get(queue_id)
        if queue_item is None or queue_item.action != "generate":
            raise AuthoringWorkbenchError(
                f"Explicit fallback queue ID is unavailable: {queue_id!r}"
            )
        if (
            not isinstance(source_item, dict)
            or (source_item.get("status"), source_item.get("review_status"))
            != ("live_fallback", "live_fallback")
            or not isinstance(source_item.get("live_fallback"), dict)
        ):
            raise AuthoringWorkbenchError(
                f"Explicit fallback source is not terminal: {queue_id!r}"
            )
        if isinstance(base_item, dict) and (
            base_item.get("status") != "failed"
            or base_item.get("review_status") is not None
            or isinstance(base_item.get("live_fallback"), dict)
        ):
            raise AuthoringWorkbenchError(
                f"Explicit fallback conflicts with base authority: {queue_id!r}"
            )
        ledger = {
            "queue_id": queue_id,
            "base_item_sha256": (
                canonical_document_sha256(base_item)
                if isinstance(base_item, dict)
                else None
            ),
            "source_item_sha256": canonical_document_sha256(source_item),
            "fallback_decision_sha256": canonical_document_sha256(
                source_item["live_fallback"]
            ),
        }
        selected_items[queue_id] = copy.deepcopy(source_item)
        ledgers.append(ledger)

    merge = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "base_workspace_id": base_document["workspace_id"],
        "base_workspace_sha256": base_workspace_sha256,
        "base_state_sha256": base_state_sha256,
        "source_workspace_id": source_document["workspace_id"],
        "source_workspace_sha256": source_workspace_sha256,
        "source_config_fingerprint": source_document["config_fingerprint"],
        "source_state_sha256": source_state_sha256,
        "queue_sha256": base_queue_sha256,
        "items": ledgers,
    }
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
        merge,
    )
    workspace_id = (
        f"resume-{base_document['source']['import_id'].removeprefix('legacy-')}-"
        f"{config_fingerprint[:16]}"
    )
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Explicit fallback destination"
    )
    staging = Path(
        tempfile.mkdtemp(prefix=".fallback-merge-staging-", dir=root)
    ).resolve()
    base_snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (
            base_directory / "generated-audio/generation-state.json",
            base_state_sha256,
        ),
        (base_queue_path, base_queue_sha256),
    ]
    source_snapshots = [
        (source_directory / "workspace.json", source_workspace_sha256),
        (
            source_directory / "generated-audio/generation-state.json",
            source_state_sha256,
        ),
        (source_queue_path, base_queue_sha256),
    ]
    try:
        for tree_name in ("provenance", "inputs"):
            copy_workspace_tree_snapshot(
                base_directory / tree_name,
                staging / tree_name,
                base_snapshots,
                error_type=AuthoringWorkbenchError,
            )
        queue_payload = read_workspace_file_bytes(
            base_queue_path, "explicit fallback base queue"
        )
        (staging / "queue.jsonl").write_bytes(queue_payload)
        output = staging / "generated-audio"
        output.mkdir()
        target_state = copy.deepcopy(base_state)
        _copy_base_wavs(base_directory, output, base_state, base_snapshots)
        for ledger in ledgers:
            queue_id = ledger["queue_id"]
            copied = copy.deepcopy(selected_items[queue_id])
            copied["explicit_fallback_merge"] = {
                key: value for key, value in ledger.items() if key != "queue_id"
            }
            target_state["items"][queue_id] = copied
        target_state["active"] = None
        atomic_write_json(
            output / "generation-state.json", target_state, sort_keys=True
        )
        write_generated_manifest_from_state(
            target_state,
            output,
            output / "manifest.json",
        )
        workspace = copy.deepcopy(base_document)
        workspace.update(
            {
                "workspace_id": workspace_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "explicit_fallback_merge": merge,
                "config_fingerprint": config_fingerprint,
            }
        )
        atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
        import_snapshot = load_workspace_json(
            staging / "provenance/import.json",
            "explicit fallback import snapshot",
        )
        validate_workspace_provenance_extensions(staging, workspace, import_snapshot)
        load_generation_state(output / "generation-state.json", staging / "queue.jsonl")

        try:
            with generation_publication_leases(
                (
                    (base_directory / "generated-audio", base_queue_sha256),
                    (source_directory / "generated-audio", base_queue_sha256),
                ),
                process_checker=process_is_alive,
            ) as held_leases:
                if any(
                    any((directory / "generated-audio").rglob("*.partial.wav"))
                    for directory in (base_directory, source_directory)
                ):
                    raise AuthoringWorkbenchError(
                        "Explicit fallback source became active before publication"
                    )
                for path, digest in (*base_snapshots, *source_snapshots):
                    if not path.is_file() or sha256_file(path) != digest:
                        raise AuthoringWorkbenchError(
                            "Explicit fallback authority changed before publication"
                        )
                for lease in held_leases:
                    lease.assert_owned()
                if destination.exists():
                    _directory, existing, _sha256 = load_workspace_authority(
                        destination
                    )
                    if existing.get("explicit_fallback_merge") != merge:
                        raise AuthoringWorkbenchError(
                            "Explicit fallback destination conflicts with another merge"
                        )
                    return WorkspaceCreationResult(destination, False)
                try:
                    rename_directory_no_replace(staging, destination)
                except (AtomicPublicationError, OSError) as error:
                    if destination.exists():
                        _directory, existing, _sha256 = load_workspace_authority(
                            destination
                        )
                        if existing.get("explicit_fallback_merge") == merge:
                            for lease in held_leases:
                                lease.mark_committed()
                            return WorkspaceCreationResult(destination, False)
                    raise AuthoringWorkbenchError(
                        f"Unable to publish explicit fallback workspace: {error}"
                    ) from error
                for lease in held_leases:
                    lease.mark_committed()
                staging = None
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
    except (BulkGenerationError, OSError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return WorkspaceCreationResult(destination, True)


def validate_explicit_fallback_merge_workspace(directory, workspace):
    """Validate the self-contained fallback overlay in a published workspace."""
    merge = workspace.get("explicit_fallback_merge")
    if merge is None:
        return
    fields = {
        "schema",
        "schema_version",
        "base_workspace_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "source_workspace_id",
        "source_workspace_sha256",
        "source_config_fingerprint",
        "source_state_sha256",
        "queue_sha256",
        "items",
    }
    if (
        not isinstance(merge, dict)
        or set(merge) != fields
        or merge.get("schema") != SCHEMA
        or merge.get("schema_version") != SCHEMA_VERSION
    ):
        raise AuthoringWorkbenchError(
            "Workspace explicit fallback merge provenance is malformed"
        )
    for field in (
        "base_workspace_sha256",
        "base_state_sha256",
        "source_workspace_sha256",
        "source_config_fingerprint",
        "source_state_sha256",
        "queue_sha256",
    ):
        require_workspace_sha256(
            merge.get(field), f"Explicit fallback {field.replace('_', ' ')}"
        )
    items = merge.get("items")
    if not isinstance(items, list) or not items:
        raise AuthoringWorkbenchError("Explicit fallback merge item ledger is empty")
    queue_ids = []
    try:
        state = load_generation_state(
            Path(directory) / "generated-audio/generation-state.json",
            Path(directory) / "queue.jsonl",
        )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    for ledger in items:
        if not isinstance(ledger, dict) or set(ledger) != {
            "queue_id",
            "base_item_sha256",
            "source_item_sha256",
            "fallback_decision_sha256",
        }:
            raise AuthoringWorkbenchError("Explicit fallback merge item is malformed")
        queue_id = ledger.get("queue_id")
        if not isinstance(queue_id, str) or not queue_id:
            raise AuthoringWorkbenchError("Explicit fallback queue ID is invalid")
        if ledger.get("base_item_sha256") is not None:
            require_workspace_sha256(
                ledger["base_item_sha256"], "Explicit fallback base item SHA-256"
            )
        source_item_sha256 = require_workspace_sha256(
            ledger.get("source_item_sha256"),
            "Explicit fallback source item SHA-256",
        )
        decision_sha256 = require_workspace_sha256(
            ledger.get("fallback_decision_sha256"),
            "Explicit fallback decision SHA-256",
        )
        result = state["items"].get(queue_id)
        expected_overlay = {
            key: value for key, value in ledger.items() if key != "queue_id"
        }
        if (
            not isinstance(result, dict)
            or (result.get("status"), result.get("review_status"))
            != ("live_fallback", "live_fallback")
            or result.get("explicit_fallback_merge") != expected_overlay
            or not isinstance(result.get("live_fallback"), dict)
            or canonical_document_sha256(result["live_fallback"]) != decision_sha256
        ):
            raise AuthoringWorkbenchError(
                f"Explicit fallback result changed for {queue_id!r}"
            )
        source_result = copy.deepcopy(result)
        source_result.pop("explicit_fallback_merge", None)
        if canonical_document_sha256(source_result) != source_item_sha256:
            raise AuthoringWorkbenchError(
                f"Explicit fallback source item changed for {queue_id!r}"
            )
        queue_ids.append(queue_id)
    if queue_ids != sorted(set(queue_ids)):
        raise AuthoringWorkbenchError("Explicit fallback merge items are not canonical")


def _copy_base_wavs(base_directory, output, state, snapshots):
    path_owners = {}
    for queue_id, result in state["items"].items():
        if not isinstance(result, dict) or not isinstance(result.get("path"), str):
            continue
        relative = safe_workspace_relative_path(
            result["path"], f"Base generation item {queue_id!r} path"
        )
        owner = path_owners.setdefault(relative.as_posix(), queue_id)
        if owner != queue_id:
            raise AuthoringWorkbenchError(
                f"Base generation WAV path collides with {owner!r}"
            )
        source_path = contained_workspace_path(
            base_directory / "generated-audio", relative, "Base generation WAV"
        )
        payload = read_workspace_file_bytes(source_path, "base generation WAV")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != require_workspace_sha256(
            result.get("file_sha256"), f"Base item {queue_id!r} WAV SHA-256"
        ):
            raise AuthoringWorkbenchError(
                f"Base generation WAV changed for {queue_id!r}"
            )
        target = contained_workspace_path(output, relative, "Merged base WAV")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        snapshots.append((source_path, digest))


__all__ = [
    "merge_explicit_live_fallbacks",
    "validate_explicit_fallback_merge_workspace",
]

"""Compose exact standalone live-fallback authorities into one workspace."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import cast

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueItem,
)

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    JsonDocument,
    load_generation_state,
    process_is_alive,
)
from vntts.authoring.bulk_generation import (
    _state_items as _generation_state_items,
)
from vntts.authoring.generation_lease import GenerationLease
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
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
    target_label="Merged base WAV",
    error_type=AuthoringWorkbenchError,
    source_label="Base generation WAV",
)

SCHEMA = "vntts.authoring-explicit-fallback-merge"
SCHEMA_VERSION = 2
Snapshot = tuple[Path, str]


@dataclass(frozen=True)
class _MergePlan:
    base_directory: Path
    base_document: JsonDocument
    base_workspace_sha256: str
    source_directory: Path
    source_workspace_sha256: str
    base_state: JsonDocument
    base_state_sha256: str
    source_state: JsonDocument
    source_state_sha256: str
    base_queue_path: Path
    base_queue_sha256: str
    source_queue_path: Path
    source_queue_sha256: str
    ledgers: list[JsonDocument]
    selected_items: dict[str, JsonDocument]
    merge: JsonDocument


@dataclass(frozen=True)
class _MergeIdentity:
    root: Path
    destination: Path
    workspace_id: str
    config_fingerprint: str


def merge_explicit_live_fallbacks(
    base_workspace: str | Path,
    source_workspace: str | Path,
    queue_ids: Iterable[str],
    workspaces_root: str | Path | None = None,
) -> WorkspaceCreationResult:
    """Publish a successor containing only named standalone fallback decisions."""
    plan = _select_explicit_fallback_merge(base_workspace, source_workspace, queue_ids)
    identity = _explicit_fallback_identity(plan, workspaces_root)
    try:
        with staged_directory(
            identity.root, prefix=".fallback-merge-staging-"
        ) as staging:
            snapshots, output, target_state = _stage_explicit_fallback_merge(
                plan, staging
            )
            workspace = _mutate_explicit_fallback_merge(
                plan, identity, output, target_state
            )
            _validate_staged_explicit_fallback_merge(staging, output, workspace)
            result = _publish_explicit_fallback_merge(
                plan, identity, staging, snapshots
            )
            if result is not None:
                return result
    except (BulkGenerationError, OSError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    return WorkspaceCreationResult(identity.destination, True)


def _select_explicit_fallback_merge(
    base_workspace: str | Path,
    source_workspace: str | Path,
    queue_ids: Iterable[str],
) -> _MergePlan:
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
    source_queue_sha256 = sha256_file(source_queue_path)
    same_queue = (
        source_queue_sha256 == base_queue_sha256
        and source_queue.metadata == base_queue.metadata
        and [item.document for item in source_queue.items]
        == [item.document for item in base_queue.items]
    )
    additive_source = not same_queue and _is_additive_source_queue(
        base_document,
        base_queue,
        source_queue,
        base_queue_sha256,
        source_queue_sha256,
    )
    if not same_queue and not additive_source:
        raise AuthoringWorkbenchError(
            "Explicit fallback source queue differs from its base"
        )
    queue_by_id = {item.queue_id: item for item in base_queue.items}
    base_items = _generation_state_items(base_state)
    source_items = _generation_state_items(source_state)
    ledgers = []
    selected_items = {}
    for queue_id in selected_ids:
        ledger, source_item = _explicit_fallback_ledger(
            queue_id, queue_by_id, base_items, source_items
        )
        selected_items[queue_id] = copy.deepcopy(source_item)
        ledgers.append(ledger)

    merge = {
        "schema": SCHEMA,
        "schema_version": 1 if same_queue else SCHEMA_VERSION,
        "base_workspace_id": base_document["workspace_id"],
        "base_workspace_sha256": base_workspace_sha256,
        "base_state_sha256": base_state_sha256,
        "source_workspace_id": source_document["workspace_id"],
        "source_workspace_sha256": source_workspace_sha256,
        "source_config_fingerprint": source_document["config_fingerprint"],
        "source_state_sha256": source_state_sha256,
        "items": ledgers,
    }
    if same_queue:
        merge["queue_sha256"] = base_queue_sha256
    else:
        merge.update(
            {
                "base_queue_sha256": base_queue_sha256,
                "source_queue_sha256": source_queue_sha256,
            }
        )
    return _MergePlan(
        base_directory,
        base_document,
        base_workspace_sha256,
        source_directory,
        source_workspace_sha256,
        base_state,
        base_state_sha256,
        source_state,
        source_state_sha256,
        base_queue_path,
        base_queue_sha256,
        source_queue_path,
        source_queue_sha256,
        ledgers,
        selected_items,
        merge,
    )


def _explicit_fallback_ledger(
    queue_id: str,
    queue_by_id: Mapping[str, VoiceGenerationQueueItem],
    base_items: Mapping[str, JsonDocument],
    source_items: Mapping[str, JsonDocument],
) -> tuple[JsonDocument, JsonDocument]:
    queue_item = queue_by_id.get(queue_id)
    source_item = source_items.get(queue_id)
    base_item = base_items.get(queue_id)
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
    return {
        "queue_id": queue_id,
        "base_item_sha256": canonical_document_sha256(base_item)
        if isinstance(base_item, dict)
        else None,
        "source_item_sha256": canonical_document_sha256(source_item),
        "fallback_decision_sha256": canonical_document_sha256(
            source_item["live_fallback"]
        ),
    }, source_item


def _explicit_fallback_identity(
    plan: _MergePlan, workspaces_root: str | Path | None
) -> _MergeIdentity:
    base_document = plan.base_document
    import_id, narrator_character = _workspace_creation_fields(base_document)
    config_fingerprint = workspace_config_fingerprint(
        import_id,
        base_document.get("story_index"),
        base_document.get("voice_manifest"),
        narrator_character,
        base_document["run_config"],
        base_document.get("carry_forward"),
        base_document.get("outcome_merge"),
        base_document.get("failure_reference_binding"),
        base_document.get("terminal_conflict_merge"),
        base_document.get("config_rebase"),
        base_document.get("audio_event_composition"),
        plan.merge,
        base_document.get("known_role_live_fallback"),
        base_document.get("audio_event_omission"),
        base_document.get("audio_event_projection_fallback"),
        base_document.get("reviewed_waveform_publication"),
        base_document.get("reviewed_rejection_live_fallback"),
        queue_extension=base_document.get("queue_extension"),
    )
    workspace_id = (
        f"resume-{import_id.removeprefix('legacy-')}-{config_fingerprint[:16]}"
    )
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Explicit fallback destination"
    )
    return _MergeIdentity(root, destination, workspace_id, config_fingerprint)


def _stage_explicit_fallback_merge(
    plan: _MergePlan, staging: Path
) -> tuple[tuple[Snapshot, ...], Path, JsonDocument]:
    base_snapshots = [
        (plan.base_directory / "workspace.json", plan.base_workspace_sha256),
        (
            plan.base_directory / "generated-audio/generation-state.json",
            plan.base_state_sha256,
        ),
        (plan.base_queue_path, plan.base_queue_sha256),
    ]
    source_snapshots = [
        (plan.source_directory / "workspace.json", plan.source_workspace_sha256),
        (
            plan.source_directory / "generated-audio/generation-state.json",
            plan.source_state_sha256,
        ),
        (plan.source_queue_path, plan.source_queue_sha256),
    ]
    for tree_name in ("provenance", "inputs"):
        copy_workspace_tree_snapshot(
            plan.base_directory / tree_name,
            staging / tree_name,
            base_snapshots,
            error_type=AuthoringWorkbenchError,
        )
    (staging / "queue.jsonl").write_bytes(
        read_workspace_file_bytes(plan.base_queue_path, "explicit fallback base queue")
    )
    output = staging / "generated-audio"
    output.mkdir()
    target_state = copy.deepcopy(plan.base_state)
    _copy_base_wavs(plan.base_directory, output, plan.base_state, base_snapshots)
    return (*base_snapshots, *source_snapshots), output, target_state


def _mutate_explicit_fallback_merge(
    plan: _MergePlan,
    identity: _MergeIdentity,
    output: Path,
    target_state: JsonDocument,
) -> JsonDocument:
    target_items = _generation_state_items(target_state)
    for ledger in plan.ledgers:
        queue_id = _required_text(ledger.get("queue_id"), "Fallback queue ID")
        copied = copy.deepcopy(plan.selected_items[queue_id])
        if plan.merge["schema_version"] == 1:
            copied["explicit_fallback_merge"] = {
                key: value for key, value in ledger.items() if key != "queue_id"
            }
        target_items[queue_id] = copied
    target_state["active"] = None
    atomic_write_json(output / "generation-state.json", target_state, sort_keys=True)
    write_generated_manifest_from_state(target_state, output, output / "manifest.json")
    workspace = copy.deepcopy(plan.base_document)
    workspace.update(
        {
            "workspace_id": identity.workspace_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "explicit_fallback_merge": plan.merge,
            "config_fingerprint": identity.config_fingerprint,
        }
    )
    atomic_write_json(output.parent / "workspace.json", workspace, sort_keys=True)
    return workspace


def _validate_staged_explicit_fallback_merge(
    staging: Path, output: Path, workspace: Mapping[str, object]
) -> None:
    import_snapshot = load_workspace_json(
        staging / "provenance/import.json", "explicit fallback import snapshot"
    )
    validate_workspace_provenance_extensions(staging, workspace, import_snapshot)
    load_generation_state(output / "generation-state.json", staging / "queue.jsonl")


def _publish_explicit_fallback_merge(
    plan: _MergePlan,
    identity: _MergeIdentity,
    staging: Path,
    snapshots: Iterable[Snapshot],
) -> WorkspaceCreationResult | None:
    try:
        with generation_publication_leases(
            (
                (plan.base_directory / "generated-audio", plan.base_queue_sha256),
                (plan.source_directory / "generated-audio", plan.source_queue_sha256),
            ),
            process_checker=process_is_alive,
        ) as leases:
            _validate_explicit_fallback_publication_sources(plan, snapshots)
            for lease in leases:
                lease.assert_owned()
            existing = _existing_explicit_fallback_result(
                identity.destination, plan.merge
            )
            if existing is not None:
                return existing
            raced = _publish_explicit_fallback_directory(
                staging, identity.destination, plan.merge, leases
            )
            if raced is not None:
                return raced
            for lease in leases:
                lease.mark_committed()
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    return None


def _validate_explicit_fallback_publication_sources(
    plan: _MergePlan, snapshots: Iterable[Snapshot]
) -> None:
    if any(
        any((directory / "generated-audio").rglob("*.partial.wav"))
        for directory in (plan.base_directory, plan.source_directory)
    ):
        raise AuthoringWorkbenchError(
            "Explicit fallback source became active before publication"
        )
    for path, digest in snapshots:
        if not path.is_file() or sha256_file(path) != digest:
            raise AuthoringWorkbenchError(
                "Explicit fallback authority changed before publication"
            )


def _existing_explicit_fallback_result(
    destination: Path, merge: Mapping[str, object]
) -> WorkspaceCreationResult | None:
    existing = _matching_explicit_fallback_result(destination, merge)
    if existing is not None:
        return existing
    if destination.exists():
        raise AuthoringWorkbenchError(
            "Explicit fallback destination conflicts with another merge"
        )
    return None


def _matching_explicit_fallback_result(
    destination: Path, merge: Mapping[str, object]
) -> WorkspaceCreationResult | None:
    if not destination.exists():
        return None
    _directory, existing, _sha256 = load_workspace_authority(destination)
    return (
        WorkspaceCreationResult(destination, False)
        if existing.get("explicit_fallback_merge") == merge
        else None
    )


def _publish_explicit_fallback_directory(
    staging: Path,
    destination: Path,
    merge: Mapping[str, object],
    leases: Iterable[GenerationLease],
) -> WorkspaceCreationResult | None:
    try:
        rename_directory_no_replace(staging, destination)
    except (AtomicPublicationError, OSError) as error:
        existing = _matching_explicit_fallback_result(destination, merge)
        if existing is not None:
            for lease in leases:
                lease.mark_committed()
            return existing
        raise AuthoringWorkbenchError(
            f"Unable to publish explicit fallback workspace: {error}"
        ) from error
    return None


def validate_explicit_fallback_merge_workspace(
    directory: str | Path,
    workspace: Mapping[str, object],
    *,
    state: dict[str, object] | None = None,
) -> None:
    """Validate the self-contained fallback overlay in a published workspace."""
    value = workspace.get("explicit_fallback_merge")
    if value is None:
        return
    merge, version = _validated_explicit_fallback_merge(value)
    expected_queue_sha256 = _validate_explicit_fallback_merge_digests(merge, version)
    if sha256_file(Path(directory) / "queue.jsonl") != expected_queue_sha256:
        raise AuthoringWorkbenchError("Explicit fallback base queue changed")
    items = merge.get("items")
    if not isinstance(items, list) or not items:
        raise AuthoringWorkbenchError("Explicit fallback merge item ledger is empty")
    state_items = _explicit_fallback_state_items(directory, state)
    queue_ids = [
        _validate_explicit_fallback_ledger(ledger, version, state_items)
        for ledger in items
    ]
    if queue_ids != sorted(set(queue_ids)):
        raise AuthoringWorkbenchError("Explicit fallback merge items are not canonical")


def _validated_explicit_fallback_merge(value: object) -> tuple[JsonDocument, int]:
    if not isinstance(value, dict):
        raise AuthoringWorkbenchError(
            "Workspace explicit fallback merge provenance is malformed"
        )
    merge = cast(JsonDocument, value)
    version = merge.get("schema_version")
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
        "items",
    }
    fields.add("queue_sha256" if version == 1 else "base_queue_sha256")
    if version == 2:
        fields.add("source_queue_sha256")
    if (
        set(merge) != fields
        or merge.get("schema") != SCHEMA
        or not isinstance(version, int)
        or version not in {1, 2}
    ):
        raise AuthoringWorkbenchError(
            "Workspace explicit fallback merge provenance is malformed"
        )
    return merge, version


def _validate_explicit_fallback_merge_digests(
    merge: Mapping[str, object], version: int
) -> str:
    digest_fields = [
        "base_workspace_sha256",
        "base_state_sha256",
        "source_workspace_sha256",
        "source_config_fingerprint",
        "source_state_sha256",
    ]
    digest_fields.extend(
        ["queue_sha256"]
        if version == 1
        else ["base_queue_sha256", "source_queue_sha256"]
    )
    for field in digest_fields:
        require_workspace_sha256(
            merge.get(field), f"Explicit fallback {field.replace('_', ' ')}"
        )
    return cast(
        str,
        merge["queue_sha256"] if version == 1 else merge["base_queue_sha256"],
    )


def _explicit_fallback_state_items(
    directory: str | Path, state: JsonDocument | None
) -> dict[str, JsonDocument]:
    if state is None:
        try:
            state = load_generation_state(
                Path(directory) / "generated-audio/generation-state.json",
                Path(directory) / "queue.jsonl",
            )
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
    return _state_items(state)


def _validate_explicit_fallback_ledger(
    ledger: object, version: int, state_items: Mapping[str, JsonDocument]
) -> str:
    if not isinstance(ledger, dict) or set(ledger) != {
        "queue_id",
        "base_item_sha256",
        "source_item_sha256",
        "fallback_decision_sha256",
    }:
        raise AuthoringWorkbenchError("Explicit fallback merge item is malformed")
    record = cast(JsonDocument, ledger)
    queue_id = record.get("queue_id")
    if not isinstance(queue_id, str) or not queue_id:
        raise AuthoringWorkbenchError("Explicit fallback queue ID is invalid")
    if record.get("base_item_sha256") is not None:
        require_workspace_sha256(
            record["base_item_sha256"], "Explicit fallback base item SHA-256"
        )
    source_item_sha256 = require_workspace_sha256(
        record.get("source_item_sha256"),
        "Explicit fallback source item SHA-256",
    )
    decision_sha256 = require_workspace_sha256(
        record.get("fallback_decision_sha256"),
        "Explicit fallback decision SHA-256",
    )
    result = state_items.get(queue_id)
    expected_overlay = {
        key: value for key, value in record.items() if key != "queue_id"
    }
    if (
        not isinstance(result, dict)
        or (result.get("status"), result.get("review_status"))
        != ("live_fallback", "live_fallback")
        or not isinstance(result.get("live_fallback"), dict)
        or canonical_document_sha256(result["live_fallback"]) != decision_sha256
    ):
        raise AuthoringWorkbenchError(
            f"Explicit fallback result changed for {queue_id!r}"
        )
    source_result = copy.deepcopy(result)
    if version == 1:
        if result.get("explicit_fallback_merge") != expected_overlay:
            raise AuthoringWorkbenchError(
                f"Explicit fallback result changed for {queue_id!r}"
            )
        source_result.pop("explicit_fallback_merge", None)
    if canonical_document_sha256(source_result) != source_item_sha256:
        raise AuthoringWorkbenchError(
            f"Explicit fallback source item changed for {queue_id!r}"
        )
    return queue_id


def _state_items(state: Mapping[str, object]) -> dict[str, JsonDocument]:
    items = state.get("items")
    if not isinstance(items, dict):
        raise AuthoringWorkbenchError(
            "Explicit fallback generation items are malformed"
        )
    return cast(dict[str, JsonDocument], items)


def _workspace_creation_fields(
    workspace: Mapping[str, object],
) -> tuple[str, str]:
    source = workspace.get("source")
    import_id = source.get("import_id") if isinstance(source, dict) else None
    narrator = workspace.get("narrator_character")
    if not isinstance(import_id, str) or not isinstance(narrator, str):
        raise AuthoringWorkbenchError("Explicit fallback base is malformed")
    return import_id, narrator


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthoringWorkbenchError(f"{label} must be non-empty text")
    return value.strip()


def _is_additive_source_queue(
    base_document: Mapping[str, object],
    base_queue: VoiceGenerationQueue,
    source_queue: VoiceGenerationQueue,
    base_queue_sha256: str,
    source_queue_sha256: str,
) -> bool:
    extension = base_document.get("queue_extension")
    base_items = {item.queue_id: item.document for item in base_queue.items}
    return (
        isinstance(extension, dict)
        and extension.get("base_queue_sha256") == source_queue_sha256
        and extension.get("queue_sha256") == base_queue_sha256
        and len(source_queue.items) < len(base_queue.items)
        and all(
            base_items.get(item.queue_id) == item.document
            for item in source_queue.items
        )
    )


__all__ = [
    "merge_explicit_live_fallbacks",
    "validate_explicit_fallback_merge_workspace",
]

"""Terminal outcome merge workspace lifecycle."""

from __future__ import annotations

import copy
import hashlib
import importlib
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeGuard, TypedDict

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    JsonDocument,
)
from vntts.authoring.failure_repair import (
    BOUNDED_SEED_RETRY,
    INLINE_PAUSE_MARKER,
    OFFLINE_FALLBACK_BACKEND,
    SENTENCE_BOUNDARY_SEGMENTATION,
)
from vntts.authoring.game_pack import FinalGamePackError
from vntts.authoring.generation_lease import (
    GenerationLease,
    process_is_alive,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.publication import generation_publication_leases, staged_directory
from vntts.authoring.publication import (
    rename_directory_no_replace as _rename_directory_no_replace,
)
from vntts.authoring.terminal_conflict_records import is_terminal_review_outcome
from vntts.authoring.workbench_contracts import (
    AuthoringWorkbenchError,
    WorkspaceCreationResult,
    _OutcomeMergeBase,
    _OutcomeMergeSource,
    _OutcomeMergeSources,
)
from vntts.authoring.workspace_authority import (
    _load_json,
    _load_workspace,
    _load_workspace_snapshot,
    _require_sha256,
    _required_text,
    _safe_relative,
    _stable_workspace_state,
    _validate_workspace_carry_forward,
    _validate_workspace_input_config,
    _validate_workspace_offline_fallback_state,
    _validate_workspace_outcome_merge,
    _validate_workspace_terminal_conflict_merge,
    _within,
)
from vntts.authoring.workspace_config import (
    workspace_config_fingerprint,
)
from vntts.authoring.workspace_creation import (
    _copy_workspace_tree_snapshot,
    _read_file_bytes,
    default_workspaces_root,
)

_terminal_review_outcome = is_terminal_review_outcome
_workspace_config_fingerprint = workspace_config_fingerprint


class _ReconciliationSelection(TypedDict):
    report_id: str
    base: JsonDocument
    sources: dict[Path, dict[str, JsonDocument]]


def merge_workspace_outcomes(
    base_workspace: str | Path,
    outcome_workspaces: Iterable[str | Path],
    workspaces_root: str | Path | None = None,
) -> WorkspaceCreationResult:
    """Create a config-addressed successor from exact reviewed repair outcomes."""
    return _merge_workspace_outcomes(
        base_workspace,
        outcome_workspaces,
        workspaces_root,
        reconciliation_selection=None,
    )


def merge_reconciled_workspace_outcomes(
    base_workspace: str | Path,
    outcome_workspaces: Iterable[str | Path],
    reconciliation_selection: _ReconciliationSelection,
    workspaces_root: str | Path | None = None,
) -> WorkspaceCreationResult:
    """Merge only terminal outcomes selected by an immutable reconciliation."""
    return _merge_workspace_outcomes(
        base_workspace,
        outcome_workspaces,
        workspaces_root,
        reconciliation_selection=reconciliation_selection,
    )


def _load_outcome_merge_base(
    base_workspace: str | Path,
    outcome_workspaces: Iterable[str | Path],
    reconciliation_selection: _ReconciliationSelection | None,
) -> tuple[_OutcomeMergeBase, tuple[Path, ...]]:
    base_directory, base_document, base_workspace_sha256 = _load_workspace_snapshot(
        base_workspace, "base"
    )
    source_values = tuple(
        Path(value).expanduser().resolve() for value in outcome_workspaces
    )
    if not source_values:
        raise AuthoringWorkbenchError(
            "Outcome merge requires at least one source workspace"
        )
    if len(set(source_values)) != len(source_values):
        raise AuthoringWorkbenchError("Outcome merge source workspace is duplicated")
    if base_directory in source_values:
        raise AuthoringWorkbenchError("Outcome merge source must differ from its base")

    base_queue, base_state, _base_state_payload, base_state_sha256 = (
        _stable_workspace_state(base_directory, base_document, "base")
    )
    base_queue_sha256 = sha256_file(base_directory / "queue.jsonl")
    if reconciliation_selection is not None:
        base_report = reconciliation_selection["base"]
        if (
            base_report["workspace_id"] != base_document["workspace_id"]
            or base_report["config_fingerprint"] != base_document["config_fingerprint"]
            or base_report["queue_sha256"] != base_queue_sha256
            or base_report["state_sha256"] != base_state_sha256
        ):
            raise AuthoringWorkbenchError(
                "Reconciliation primary workspace authority changed"
            )
    return (
        _OutcomeMergeBase(
            base_directory,
            base_document,
            base_workspace_sha256,
            base_queue,
            base_state,
            base_state_sha256,
            base_queue_sha256,
            {item.queue_id: item for item in base_queue.items},
        ),
        source_values,
    )


def _load_outcome_merge_source(
    source_value: Path,
    base: _OutcomeMergeBase,
    reconciliation_selection: _ReconciliationSelection | None,
) -> _OutcomeMergeSource:
    source_directory, source_document, source_workspace_sha256 = (
        _load_workspace_snapshot(source_value, "source")
    )
    if source_document["source"] != base.document["source"]:
        raise AuthoringWorkbenchError(
            "Outcome merge workspaces must share one immutable import"
        )
    source_queue, source_state, _payload, source_state_sha256 = _stable_workspace_state(
        source_directory, source_document, "source"
    )
    if (
        sha256_file(source_directory / "queue.jsonl") != base.queue_sha256
        or source_queue.metadata != base.queue.metadata
        or [item.document for item in source_queue.items]
        != [item.document for item in base.queue.items]
    ):
        raise AuthoringWorkbenchError(
            "Outcome merge source queue differs from its base"
        )

    selected_records = None
    if reconciliation_selection is None:
        carry = source_document.get("carry_forward")
        if not isinstance(carry, dict) or carry.get("schema_version") not in {3, 4}:
            raise AuthoringWorkbenchError(
                "Outcome merge source must be a current failure-repair workspace"
            )
        selected_ids = carry.get("failed_queue_ids")
        if not isinstance(selected_ids, list) or not selected_ids:
            raise AuthoringWorkbenchError(
                "Outcome merge source has no exact repair selection"
            )
    else:
        selected_records = reconciliation_selection["sources"].get(source_directory)
        if not isinstance(selected_records, dict) or not selected_records:
            raise AuthoringWorkbenchError(
                "Reconciliation source has no exact terminal selection"
            )
        source_report = _outcome_json_object(
            next(iter(selected_records.values())).get("workspace"),
            "reconciliation source workspace",
        )
        if (
            source_report["workspace_id"] != source_document["workspace_id"]
            or Path(
                _required_text(
                    source_report.get("workspace"),
                    "Reconciliation source workspace path",
                )
            ).resolve()
            != source_directory
            or source_report["config_fingerprint"]
            != source_document["config_fingerprint"]
            or source_report["queue_sha256"] != base.queue_sha256
            or source_report["state_sha256"] != source_state_sha256
        ):
            raise AuthoringWorkbenchError(
                "Reconciliation terminal source authority changed"
            )
        selected_ids = sorted(selected_records)
    return _OutcomeMergeSource(
        source_directory,
        source_document,
        source_workspace_sha256,
        source_state,
        source_state_sha256,
        tuple(selected_ids),
        selected_records,
    )


def _collect_outcome_merge_item(
    base: _OutcomeMergeBase,
    source: _OutcomeMergeSource,
    queue_id: str,
    merged_items: dict[str, tuple[JsonDocument, JsonDocument]],
) -> tuple[JsonDocument, JsonDocument, tuple[Path, bytes, Path]] | None:
    result = _outcome_state_items(source.state).get(queue_id)
    if result is None or not _terminal_review_outcome(result):
        return None
    if queue_id in merged_items:
        raise AuthoringWorkbenchError(
            f"Outcome merge has conflicting sources for {queue_id!r}"
        )
    base_result = _outcome_state_items(base.state).get(queue_id)
    if source.selected_records is None:
        repair = result.get("failure_repair")
        if not isinstance(repair, dict) or repair.get("strategy") not in {
            SENTENCE_BOUNDARY_SEGMENTATION,
            BOUNDED_SEED_RETRY,
            INLINE_PAUSE_MARKER,
            OFFLINE_FALLBACK_BACKEND,
        }:
            raise AuthoringWorkbenchError(
                f"Outcome merge item {queue_id!r} lacks a supported repair outcome"
            )
        source_failure = result.get("carry_forward")
        if source_failure is None:
            source_failure = repair.get("source_failure")
        root_source_failure = _root_carry_forward_authority(source_failure)
        if (
            not isinstance(root_source_failure, dict)
            or root_source_failure.get("source_workspace_id")
            != base.document["workspace_id"]
            or not isinstance(base_result, dict)
            or root_source_failure.get("source_item_sha256")
            != canonical_document_sha256(base_result)
        ):
            raise AuthoringWorkbenchError(
                f"Outcome merge source authority is stale for {queue_id!r}"
            )
    else:
        expected = source.selected_records[queue_id]
        action = _outcome_json_object(expected.get("action"), "reconciliation action")
        selected_source = _outcome_json_object(
            expected.get("source"), "reconciliation source"
        )
        queue_item = base.queue_by_id.get(queue_id)
        authority = (
            "approved"
            if (result.get("status"), result.get("review_status"))
            == ("approved", "approved")
            else "rejected"
        )
        if (
            queue_item is None
            or action["line_id"] != queue_item.line_id
            or action["text_sha256"] != queue_item.text_sha256
            or selected_source["workspace_id"] != source.document["workspace_id"]
            or selected_source["authority"] != authority
            or selected_source["state_item_sha256"] != canonical_document_sha256(result)
            or _terminal_review_outcome(base_result)
        ):
            raise AuthoringWorkbenchError(
                f"Reconciliation terminal source is stale for {queue_id!r}"
            )
    if _terminal_review_outcome(base_result):
        raise AuthoringWorkbenchError(
            f"Outcome merge conflicts with existing review authority for {queue_id!r}"
        )

    relative = _safe_relative(
        result.get("path"), f"Outcome merge item {queue_id!r} path"
    )
    audio_path = _within(
        source.directory / "generated-audio",
        relative,
        "Outcome merge source WAV",
    )
    audio_payload = _read_file_bytes(audio_path, "outcome merge source WAV")
    audio_sha256 = hashlib.sha256(audio_payload).hexdigest()
    if audio_sha256 != _require_sha256(
        result.get("file_sha256"),
        f"Outcome merge item {queue_id!r} WAV SHA-256",
    ):
        raise AuthoringWorkbenchError(
            f"Outcome merge source WAV changed for {queue_id!r}"
        )
    ledger = {
        "queue_id": queue_id,
        "source_workspace_id": source.document["workspace_id"],
        "source_state_sha256": source.state_sha256,
        "source_item_sha256": canonical_document_sha256(result),
        "audio_sha256": audio_sha256,
        "status": result["status"],
        "review_status": result["review_status"],
    }
    return copy.deepcopy(result), ledger, (audio_path, audio_payload, relative)


def _collect_outcome_merge_sources(
    base: _OutcomeMergeBase,
    source_values: Sequence[Path],
    reconciliation_selection: _ReconciliationSelection | None,
) -> _OutcomeMergeSources:
    collected = _OutcomeMergeSources({}, [], [], {})
    for source_value in source_values:
        source = _load_outcome_merge_source(
            source_value, base, reconciliation_selection
        )
        source_record = {
            "workspace_id": source.document["workspace_id"],
            "config_fingerprint": _require_sha256(
                source.document.get("config_fingerprint"),
                "Outcome merge source configuration fingerprint",
            ),
            "state_sha256": source.state_sha256,
        }
        terminal_count = 0
        for queue_id in source.selected_ids:
            item = _collect_outcome_merge_item(base, source, queue_id, collected.items)
            if item is None:
                continue
            result, ledger, audio = item
            collected.items[queue_id] = (result, ledger)
            collected.audio[queue_id] = audio
            collected.snapshots.append(
                (
                    audio[0],
                    _required_text(
                        ledger.get("audio_sha256"), "Outcome merge audio SHA-256"
                    ),
                )
            )
            terminal_count += 1
        if terminal_count == 0:
            raise AuthoringWorkbenchError(
                f"Outcome merge source {source.document['workspace_id']!r} has no reviewed repair outcomes"
            )
        source_record["terminal_item_count"] = terminal_count
        collected.records.append(source_record)
        if len({value["workspace_id"] for value in collected.records}) != len(
            collected.records
        ):
            raise AuthoringWorkbenchError(
                "Outcome merge source workspace identity is duplicated"
            )
        collected.snapshots.extend(
            (
                (
                    source.directory / "generated-audio/generation-state.json",
                    source.state_sha256,
                ),
                (source.directory / "workspace.json", source.workspace_sha256),
            )
        )
    collected.records.sort(
        key=lambda value: _required_text(
            value.get("workspace_id"), "Outcome merge source workspace ID"
        )
    )
    return collected


def _outcome_merge_identity(
    base: _OutcomeMergeBase,
    sources: _OutcomeMergeSources,
    reconciliation_selection: _ReconciliationSelection | None,
) -> tuple[JsonDocument, str, str]:
    ledger_items = [sources.items[key][1] for key in sorted(sources.items)]
    outcome_merge = {
        "schema": "vntts.authoring-workspace-outcome-merge",
        "schema_version": 2 if reconciliation_selection is not None else 1,
        "base_workspace_id": base.document["workspace_id"],
        "base_state_sha256": base.state_sha256,
        "sources": sources.records,
        "items": ledger_items,
    }
    if reconciliation_selection is not None:
        outcome_merge["source_reconciliation_id"] = reconciliation_selection[
            "report_id"
        ]
    source = _outcome_json_object(base.document.get("source"), "base source")
    import_id = _required_text(source.get("import_id"), "Base import ID")
    config_fingerprint = _workspace_config_fingerprint(
        import_id,
        base.document.get("story_index"),
        base.document.get("voice_manifest"),
        _required_text(
            base.document.get("narrator_character"), "Base narrator character"
        ),
        base.document["run_config"],
        base.document.get("carry_forward"),
        outcome_merge,
        base.document.get("failure_reference_binding"),
        base.document.get("terminal_conflict_merge"),
        base.document.get("config_rebase"),
        base.document.get("audio_event_composition"),
        base.document.get("explicit_fallback_merge"),
        base.document.get("known_role_live_fallback"),
        base.document.get("audio_event_omission"),
        base.document.get("audio_event_projection_fallback"),
        base.document.get("reviewed_waveform_publication"),
        base.document.get("reviewed_rejection_live_fallback"),
        queue_extension=base.document.get("queue_extension"),
    )
    workspace_id = (
        f"resume-{import_id.removeprefix('legacy-')}-{config_fingerprint[:16]}"
    )
    return outcome_merge, config_fingerprint, workspace_id


def _stage_outcome_merge_base(
    base: _OutcomeMergeBase, staging: Path
) -> tuple[Path, JsonDocument, dict[str, str], list[tuple[Path, str]]]:
    base_snapshots = [
        (base.directory / "workspace.json", base.workspace_sha256),
        (
            base.directory / "generated-audio/generation-state.json",
            base.state_sha256,
        ),
    ]
    for tree_name in ("provenance", "inputs"):
        _copy_workspace_tree_snapshot(
            base.directory / tree_name,
            staging / tree_name,
            base_snapshots,
        )
    queue_payload = _read_file_bytes(
        base.directory / "queue.jsonl", "outcome merge base queue"
    )
    (staging / "queue.jsonl").write_bytes(queue_payload)
    base_snapshots.append((base.directory / "queue.jsonl", base.queue_sha256))
    output = staging / "generated-audio"
    output.mkdir()
    target_state = copy.deepcopy(base.state)
    path_owners: dict[str, str] = {}
    for queue_id, result in _outcome_state_items(base.state).items():
        if not isinstance(result.get("path"), str):
            continue
        relative = _safe_relative(
            result["path"], f"Base generation item {queue_id!r} path"
        )
        owner = path_owners.setdefault(relative.as_posix(), queue_id)
        if owner != queue_id:
            raise AuthoringWorkbenchError(
                f"Base generation WAV path collides with {owner!r}"
            )
        source_path = _within(
            base.directory / "generated-audio", relative, "Base generation WAV"
        )
        payload = _read_file_bytes(source_path, "base generation WAV")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != _require_sha256(
            result.get("file_sha256"),
            f"Base item {queue_id!r} WAV SHA-256",
        ):
            raise AuthoringWorkbenchError(
                f"Base generation WAV changed for {queue_id!r}"
            )
        target_path = _within(output, relative, "Merged base WAV")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(payload)
        base_snapshots.append((source_path, digest))
    return output, target_state, path_owners, base_snapshots


def _outcome_json_object(value: object, label: str) -> JsonDocument:
    if not isinstance(value, dict):
        raise AuthoringWorkbenchError(f"{label.title()} must be an object")
    return value


def _outcome_state_items(state: JsonDocument) -> dict[str, JsonDocument]:
    items = state.get("items")
    if not _is_outcome_state_items(items):
        raise AuthoringWorkbenchError("Generation state items are malformed")
    return items


def _is_outcome_state_items(value: object) -> TypeGuard[dict[str, JsonDocument]]:
    return isinstance(value, dict) and all(
        isinstance(queue_id, str) and isinstance(item, dict)
        for queue_id, item in value.items()
    )


def _overlay_outcome_merge_items(
    output: Path,
    target_state: JsonDocument,
    path_owners: dict[str, str],
    sources: _OutcomeMergeSources,
) -> None:
    target_items = _outcome_state_items(target_state)
    for queue_id, (result, ledger) in sources.items.items():
        previous = target_items.get(queue_id)
        previous_path = previous.get("path") if isinstance(previous, dict) else None
        relative = sources.audio[queue_id][2]
        if previous_path and previous_path != relative.as_posix():
            old_target = _within(
                output,
                _safe_relative(previous_path, "Replaced merge WAV"),
                "Replaced merge WAV",
            )
            if old_target.is_file():
                old_target.unlink()
        owner = path_owners.get(relative.as_posix())
        if owner not in {None, queue_id}:
            raise AuthoringWorkbenchError(
                f"Outcome merge WAV path collides with {owner!r}"
            )
        target_audio = _within(output, relative, "Merged outcome WAV")
        target_audio.parent.mkdir(parents=True, exist_ok=True)
        target_audio.write_bytes(sources.audio[queue_id][1])
        copied = copy.deepcopy(result)
        copied["outcome_merge"] = {
            key: value for key, value in ledger.items() if key != "queue_id"
        }
        target_items[queue_id] = copied


def _write_outcome_merge_workspace(
    base: _OutcomeMergeBase,
    staging: Path,
    output: Path,
    target_state: JsonDocument,
    workspace_id: str,
    outcome_merge: JsonDocument,
    config_fingerprint: str,
) -> None:
    atomic_write_json(output / "generation-state.json", target_state, sort_keys=True)
    workspace = copy.deepcopy(base.document)
    workspace.update(
        {
            "workspace_id": workspace_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "outcome_merge": outcome_merge,
            "config_fingerprint": config_fingerprint,
        }
    )
    atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
    try:
        write_generated_manifest_from_state(
            target_state,
            output,
            output / "manifest.json",
        )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    import_snapshot = _load_json(
        staging / "provenance/import.json", "merged import snapshot"
    )
    _validate_workspace_carry_forward(staging, workspace)
    _validate_workspace_input_config(staging, workspace, import_snapshot)
    _validate_workspace_offline_fallback_state(staging, workspace)
    _validate_workspace_outcome_merge(staging, workspace)
    _validate_workspace_terminal_conflict_merge(staging, workspace)
    if workspace.get("config_rebase") is not None:
        module = importlib.import_module("vntts.authoring.config_rebase")
        module.validate_config_rebase_workspace(staging, workspace, target_state)


def _commit_staged_outcome_merge(
    staging: Path,
    destination: Path,
    outcome_merge: JsonDocument,
    held_leases: Sequence[GenerationLease],
) -> WorkspaceCreationResult:
    if destination.exists():
        _directory, existing = _load_workspace(destination)
        if existing.get("outcome_merge") != outcome_merge:
            raise AuthoringWorkbenchError(
                "Outcome merge destination conflicts with another source set"
            )
        return WorkspaceCreationResult(destination, False)
    try:
        _rename_directory_no_replace(staging, destination)
    except (OSError, FinalGamePackError) as error:
        if destination.exists():
            _directory, existing = _load_workspace(destination)
            if existing.get("outcome_merge") == outcome_merge:
                for lease in held_leases:
                    lease.mark_committed()
                return WorkspaceCreationResult(destination, False)
        raise AuthoringWorkbenchError(
            f"Unable to publish outcome merge workspace: {error}"
        ) from error
    for lease in held_leases:
        lease.mark_committed()
    return WorkspaceCreationResult(destination, True)


def _publish_staged_outcome_merge(
    base: _OutcomeMergeBase,
    source_values: Sequence[Path],
    sources: _OutcomeMergeSources,
    base_snapshots: Sequence[tuple[Path, str]],
    staging: Path,
    destination: Path,
    outcome_merge: JsonDocument,
) -> WorkspaceCreationResult:
    try:
        source_directories = (base.directory, *source_values)
        with generation_publication_leases(
            (
                (directory / "generated-audio", base.queue_sha256)
                for directory in source_directories
            ),
            process_checker=process_is_alive,
        ) as held_leases:
            if any(
                any((directory / "generated-audio").rglob("*.partial.wav"))
                for directory in source_directories
            ):
                raise AuthoringWorkbenchError(
                    "Outcome merge source became active before publication"
                )
            for path, digest in (*base_snapshots, *sources.snapshots):
                if not path.is_file() or sha256_file(path) != digest:
                    raise AuthoringWorkbenchError(
                        "Outcome merge source changed before workspace publication"
                    )
            for lease in held_leases:
                lease.assert_owned()
            return _commit_staged_outcome_merge(
                staging, destination, outcome_merge, held_leases
            )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(
            f"Outcome merge source became active before publication: {error}"
        ) from error


def _merge_workspace_outcomes(
    base_workspace: str | Path,
    outcome_workspaces: Iterable[str | Path],
    workspaces_root: str | Path | None,
    *,
    reconciliation_selection: _ReconciliationSelection | None,
) -> WorkspaceCreationResult:
    """Assemble one exact terminal-outcome successor."""
    base, source_values = _load_outcome_merge_base(
        base_workspace,
        outcome_workspaces,
        reconciliation_selection,
    )
    sources = _collect_outcome_merge_sources(
        base, source_values, reconciliation_selection
    )
    outcome_merge, config_fingerprint, workspace_id = _outcome_merge_identity(
        base, sources, reconciliation_selection
    )
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = _within(root, Path(workspace_id), "Outcome merge destination")
    with staged_directory(root, prefix=".merge-staging-") as staging:
        output, target_state, path_owners, base_snapshots = _stage_outcome_merge_base(
            base, staging
        )
        _overlay_outcome_merge_items(output, target_state, path_owners, sources)
        _write_outcome_merge_workspace(
            base,
            staging,
            output,
            target_state,
            workspace_id,
            outcome_merge,
            config_fingerprint,
        )
        return _publish_staged_outcome_merge(
            base,
            source_values,
            sources,
            base_snapshots,
            staging,
            destination,
            outcome_merge,
        )


def _root_carry_forward_authority(value: object) -> object:
    if not isinstance(value, dict):
        return value
    observed = set()
    current = value
    while isinstance(current.get("source_parent_carry_forward"), dict):
        digest = canonical_document_sha256(current)
        if digest in observed:
            raise AuthoringWorkbenchError("Nested carry-forward provenance is cyclic")
        observed.add(digest)
        current = current["source_parent_carry_forward"]
    return current


__all__ = [
    "_collect_outcome_merge_item",
    "_collect_outcome_merge_sources",
    "_commit_staged_outcome_merge",
    "_load_outcome_merge_base",
    "_load_outcome_merge_source",
    "_merge_workspace_outcomes",
    "_outcome_merge_identity",
    "_overlay_outcome_merge_items",
    "_publish_staged_outcome_merge",
    "_root_carry_forward_authority",
    "_stage_outcome_merge_base",
    "_write_outcome_merge_workspace",
    "merge_reconciled_workspace_outcomes",
    "merge_workspace_outcomes",
]

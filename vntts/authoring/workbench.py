"""Safe mutable workspaces and truthful status for graphical authoring."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import re
import socket
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    validate_voice_manifest,
)

import vntts.authoring.legacy_import as legacy_import
from vntts.authoring.audio_event_workspace import (
    AUDIO_EVENT_PROVIDER,
)
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    ReviewAuthority,
    ReviewCommit,
    is_spoken_queue_item,
    load_generation_state,
    load_review_audio_bytes,
    normalized_failure_record,
    review_generation_item,
    sentence_repair_matches_failure,
)
from vntts.authoring.failure_repair import (
    BOUNDED_SEED_RETRY,
    INLINE_PAUSE_MARKER,
    OFFLINE_FALLBACK_BACKEND,
    SENTENCE_BOUNDARY_SEGMENTATION,
    FailureRepairPolicy,
    FailureRepairPolicyError,
)
from vntts.authoring.game_pack import FinalGamePackError
from vntts.authoring.generation_lease import (
    LEASE_SCHEMA,
    LEASE_VERSION,
    process_is_alive,
    process_started_at,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.missing_voice_policy import (
    NARRATOR_ALL_UNRESOLVED,
    NARRATOR_ROLES,
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.publication import generation_publication_leases, staged_directory
from vntts.authoring.publication import (
    rename_directory_no_replace as _rename_directory_no_replace,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
)
from vntts.authoring.speech_quality import (
    SPEECH_QUALITY_ANALYSIS_VERSION,
    measure_generated_speech_bytes,
)
from vntts.authoring.terminal_conflict_records import is_terminal_review_outcome
from vntts.authoring.workbench_contracts import (
    WORKSPACE_SCHEMA,
    WORKSPACE_VERSION,
    ActiveAttempt,
    AuthoringRuntimeStatus,
    AuthoringWorkbenchError,
    CollectionSelection,
    GenerationReadiness,
    ImmutableHistoryTimestamp,
    ReviewItem,
    WorkbenchProjectionData,
    WorkspaceCollection,
    WorkspaceCreationResult,
    WorkspaceSummary,
    WorkspaceVoice,
    _OutcomeMergeBase,
    _OutcomeMergeSource,
    _OutcomeMergeSources,
    _read_bound_bytes,
    _WorkbenchProjectionRead,
)
from vntts.authoring.workspace_authority import (
    _load_bound_workspace_queue,
    _load_json,
    _load_json_snapshot,
    _load_workspace,
    _load_workspace_snapshot,
    _parse_history_timestamp,
    _require_sha256,
    _required_text,
    _safe_relative,
    _stable_workspace_state,
    _validate_import_history,
    _validate_workspace_carry_forward,
    _validate_workspace_input_config,
    _validate_workspace_offline_fallback_state,
    _validate_workspace_outcome_merge,
    _validate_workspace_terminal_conflict_merge,
    _within,
    _workspace_failure_repair_policy,
    _workspace_run_config_with_policy,
    load_workspace_authority,
    validate_workspace_provenance_extensions,
)
from vntts.authoring.workspace_config import (
    workspace_audio_event_spoken_projection_queue_ids,
    workspace_config_fingerprint,
    workspace_queue_sha256,
)
from vntts.authoring.workspace_creation import (
    _copy_workspace_tree_snapshot,
    _failure_reference_runtime_binding,
    _optional_text,
    _read_file_bytes,
    _selected_voice_manifest,
    _workspace_missing_voice_policy,
    create_audio_event_composition_workspace,
    create_failure_reference_workspace,
    create_resume_workspace,
    default_workspaces_root,
)
from vntts.authoring.workspace_state import (
    cached_workspace_generation_state,
    shared_workspace_state_reads,
)
from vntts.authoring.workspace_voice_runtime import (
    FailureReferenceRuntimeBinding,
)
from vntts.voices import CharacterVoiceRegistry, synthesis_character_for_line

_canonical_sha256 = canonical_document_sha256

REVIEW_ATTENTION_POLICY_VERSION = 3
REVIEW_NOTABLE_SILENCE_RATIO = None
REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS = 1.2
PACE_MINIMUM_WORDS = 5
PACE_MINIMUM_LENGTH_BUCKET_SAMPLES = 3
PACE_MINIMUM_VOICE_SAMPLES = 5
PACE_SLOW_RELATIVE_RATIO = 0.80
PACE_SLOW_MINIMUM_DELTA_WPM = 20.0
_IMPORT_ID_PATTERN = re.compile(r"legacy-[0-9a-f]{24}")


def generation_failure_category(error, *, text=""):
    """Collapse volatile backend diagnostics into actionable failure cohorts."""
    if isinstance(error, dict):
        failure = normalized_failure_record(error, text=text)
        kind = failure.get("kind")
        if (
            kind == "speech_silence"
            and text
            and sentence_repair_matches_failure(failure, text)
        ):
            return "Long sentence-boundary pause"
        return {
            "missed_eos_audio_limit": "audio limit / missed EOS",
            "speech_silence": "speech silence",
            "reference_unavailable": "reference unavailable",
            "cancelled": "cancelled",
            "interrupted": "interrupted",
            "backend_error": "other generation failure",
        }[kind]
    value = str(error or "").casefold()
    if "limited" in value or " limit" in value:
        return "audio limit / missed EOS"
    if "silence" in value:
        return "speech silence"
    return "other generation failure"


def review_technical_summary(item):
    """Describe objective review metrics without making a listening decision."""
    if item.duration_seconds is None:
        if item.failure_category is not None:
            summary = "Failure: " + item.failure_category
            if item.internal_pause_seconds is not None:
                summary += f" | measured raw pause {item.internal_pause_seconds:.2f}s"
            return summary
        return "No generated WAV"
    metrics = [f"{item.duration_seconds:.2f}s"]
    if item.words_per_minute is not None:
        metrics.append(f"{item.words_per_minute:.0f} WPM")
    if item.peak is not None:
        metrics.append(f"peak {item.peak:.3f}")
    if item.technical_flags:
        metrics.append(
            "advisory measurements (listen to decide): "
            + ", ".join(item.technical_flags)
        )
    else:
        metrics.append("technical pass")
    pace_advisories = getattr(item, "pace_advisories", ())
    if pace_advisories:
        metrics.append("pace report only: " + ", ".join(pace_advisories))
    if (
        item.repair_strategy
        in {
            SENTENCE_BOUNDARY_SEGMENTATION,
            INLINE_PAUSE_MARKER,
        }
        and item.internal_pause_seconds is not None
    ):
        metrics.append(f"repaired pause {item.internal_pause_seconds:.2f}s")
    return " | ".join(metrics)


def _review_internal_pause_seconds(result, *, failed):
    source = (
        normalized_failure_record(result).get("speech_quality")
        if failed
        else result.get("speech_quality")
    )
    if not isinstance(source, dict):
        return None
    value = source.get("longest_internal_silence_seconds")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return float(value)


def discover_imports(import_root=None):
    root = (
        Path(import_root or legacy_import.default_import_root()).expanduser().resolve()
    )
    if not root.is_dir():
        return ()
    results = []
    seen = set()
    for manifest_path in sorted(root.glob("legacy-*/import.json")):
        try:
            if manifest_path.is_symlink() or manifest_path.parent.is_symlink():
                continue
            directory = manifest_path.parent.resolve()
            directory.relative_to(root)
            if not _IMPORT_ID_PATTERN.fullmatch(directory.name):
                continue
            key = directory.name.casefold()
            if key in seen:
                continue
            manifest = _load_json(manifest_path, "legacy import")
            if (
                manifest.get("schema") == legacy_import.IMPORT_SCHEMA
                and manifest.get("schema_version")
                in legacy_import.SUPPORTED_IMPORT_SCHEMA_VERSIONS
                and (manifest_path.parent / "queue.jsonl").is_file()
            ):
                _validate_import_history(manifest)
                results.append(directory)
                seen.add(key)
        except AuthoringWorkbenchError, ValueError:
            continue
    return tuple(results)


def discover_workspaces(workspaces_root=None):
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    if not root.is_dir():
        return ()
    results = []
    seen = set()
    for path in sorted(root.glob("*/workspace.json"), reverse=True):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                continue
            directory = path.parent.resolve()
            directory.relative_to(root)
            if not re.fullmatch(r"resume-[0-9a-f]{24}-[0-9a-f]{16}", directory.name):
                continue
            key = directory.name.casefold()
            if key in seen:
                continue
            _load_workspace(directory)
            results.append(directory)
            seen.add(key)
        except AuthoringWorkbenchError, ValueError:
            continue
    return tuple(results)


def merge_workspace_outcomes(
    base_workspace,
    outcome_workspaces,
    workspaces_root=None,
):
    """Create a config-addressed successor from exact reviewed repair outcomes."""
    return _merge_workspace_outcomes(
        base_workspace,
        outcome_workspaces,
        workspaces_root,
        reconciliation_selection=None,
    )


def merge_reconciled_workspace_outcomes(
    base_workspace,
    outcome_workspaces,
    reconciliation_selection,
    workspaces_root=None,
):
    """Merge only terminal outcomes selected by an immutable reconciliation."""
    return _merge_workspace_outcomes(
        base_workspace,
        outcome_workspaces,
        workspaces_root,
        reconciliation_selection=reconciliation_selection,
    )


def _load_outcome_merge_base(
    base_workspace,
    outcome_workspaces,
    reconciliation_selection,
):
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


def _load_outcome_merge_source(source_value, base, reconciliation_selection):
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
        source_report = next(iter(selected_records.values()))["workspace"]
        if (
            source_report["workspace_id"] != source_document["workspace_id"]
            or Path(source_report["workspace"]).resolve() != source_directory
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


def _collect_outcome_merge_item(base, source, queue_id, merged_items):
    result = source.state["items"].get(queue_id)
    if not isinstance(result, dict) or not _terminal_review_outcome(result):
        return None
    if queue_id in merged_items:
        raise AuthoringWorkbenchError(
            f"Outcome merge has conflicting sources for {queue_id!r}"
        )
    base_result = base.state["items"].get(queue_id)
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
        action = expected["action"]
        selected_source = expected["source"]
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


def _collect_outcome_merge_sources(base, source_values, reconciliation_selection):
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
            collected.snapshots.append((audio[0], ledger["audio_sha256"]))
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
    collected.records.sort(key=lambda value: value["workspace_id"])
    return collected


def _outcome_merge_identity(base, sources, reconciliation_selection):
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
    config_fingerprint = _workspace_config_fingerprint(
        base.document["source"]["import_id"],
        base.document.get("story_index"),
        base.document.get("voice_manifest"),
        base.document["narrator_character"],
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
        f"resume-{base.document['source']['import_id'].removeprefix('legacy-')}-"
        f"{config_fingerprint[:16]}"
    )
    return outcome_merge, config_fingerprint, workspace_id


def _stage_outcome_merge_base(base, staging):
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
    path_owners = {}
    for queue_id, result in base.state["items"].items():
        if not isinstance(result, dict) or not isinstance(result.get("path"), str):
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


def _overlay_outcome_merge_items(output, target_state, path_owners, sources):
    for queue_id, (result, ledger) in sources.items.items():
        previous = target_state["items"].get(queue_id)
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
        target_state["items"][queue_id] = copied


def _write_outcome_merge_workspace(
    base,
    staging,
    output,
    target_state,
    workspace_id,
    outcome_merge,
    config_fingerprint,
):
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


def _commit_staged_outcome_merge(staging, destination, outcome_merge, held_leases):
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
    base,
    source_values,
    sources,
    base_snapshots,
    staging,
    destination,
    outcome_merge,
):
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
    base_workspace,
    outcome_workspaces,
    workspaces_root,
    *,
    reconciliation_selection,
):
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


def inspect_workspace(
    workspace_directory,
    *,
    voice_manifest=None,
    local_process_id=None,
    local_process_started_at=None,
    process_checker=process_is_alive,
    process_start_checker=process_started_at,
):
    directory, workspace = _load_workspace(workspace_directory)
    queue_path = _within(
        directory, _safe_relative(workspace["queue"], "Queue"), "Queue"
    )
    output = _within(directory, _safe_relative(workspace["output"], "Output"), "Output")
    try:
        queue = VoiceGenerationQueue.load(queue_path)
    except VoiceGenerationQueueError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    state_path = output / "generation-state.json"
    state = None
    if state_path.is_file():
        try:
            state = load_generation_state(state_path, queue_path)
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error

    return _inspect_workspace_from_read(
        directory,
        workspace,
        queue_path,
        output,
        queue,
        state_path if state_path.is_file() else None,
        state,
        voice_manifest=voice_manifest,
        local_process_id=local_process_id,
        local_process_started_at=local_process_started_at,
        process_checker=process_checker,
        process_start_checker=process_start_checker,
    )


def _inspect_workspace_from_read(
    directory,
    workspace,
    queue_path,
    output,
    queue,
    state_path,
    state,
    *,
    voice_manifest=None,
    local_process_id=None,
    local_process_started_at=None,
    process_checker=process_is_alive,
    process_start_checker=process_started_at,
):
    state_items = {} if state is None else state["items"]

    audio_event_config = workspace.get("audio_event_composition")
    audio_event_ids = (
        {audio_event_config["queue_id"]}
        if isinstance(audio_event_config, dict)
        else set()
    )
    omission_config = workspace.get("audio_event_omission")
    if isinstance(omission_config, dict) and isinstance(
        omission_config.get("items"), list
    ):
        audio_event_ids.update(
            item["queue_id"]
            for item in omission_config["items"]
            if isinstance(item, dict) and isinstance(item.get("queue_id"), str)
        )
    candidates = [item for item in queue.items if item.action == "generate"]
    recoverable_source_audio = sum(
        item.action == "prefer_source_audio" and item.queue_id not in audio_event_ids
        for item in queue.items
    )
    manual_review = sum(
        item.action == "manual_review" and item.queue_id not in audio_event_ids
        for item in queue.items
    )
    resolve_audio = sum(
        item.action == "resolve_audio" and item.queue_id not in audio_event_ids
        for item in queue.items
    )
    spoken = [item for item in candidates if is_spoken_queue_item(item)]
    spoken_ids = {item.queue_id for item in spoken}
    reviewable_ids = spoken_ids | audio_event_ids
    relevant = {
        queue_id: value
        for queue_id, value in state_items.items()
        if queue_id in reviewable_ids
    }
    approved_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if value.get("status") == "approved"
        and value.get("review_status") == "approved"
    }
    rejected_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if value.get("status") == "generated"
        and value.get("review_status") == "rejected"
    }
    generated_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if value.get("status") == "generated"
        and value.get("review_status") == "pending_review"
    }
    failed_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if value.get("status") == "failed"
    }
    live_fallback_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if isinstance(value.get("live_fallback"), dict)
    }
    omitted_ids = {
        queue_id
        for queue_id, value in relevant.items()
        if isinstance(value.get("audio_event_omission"), dict)
    }
    completed_ids = (
        approved_ids
        | rejected_ids
        | generated_ids
        | failed_ids
        | live_fallback_ids
        | omitted_ids
    )
    selected_voice_manifest = _selected_voice_manifest(
        directory, workspace, voice_manifest
    )
    missing_voice_ids, blocked_reasons = _voice_readiness(
        workspace,
        spoken,
        completed_ids,
        selected_voice_manifest,
        directory=directory,
    )
    blocked_reasons = (*_workspace_control_reasons(workspace), *blocked_reasons)
    pending_ids = reviewable_ids - completed_ids - missing_voice_ids
    active = _active_attempt(state.get("active") if state else None, spoken_ids)
    runtime_status = _runtime_status(
        output,
        active,
        len(pending_ids),
        len(generated_ids),
        len(failed_ids),
        len(missing_voice_ids),
        blocked_reasons,
        queue_sha256=sha256_file(queue_path),
        local_process_id=local_process_id,
        local_process_started_at=local_process_started_at,
        process_checker=process_checker,
        process_start_checker=process_start_checker,
    )
    failures = Counter(
        str(value.get("last_error") or "Unknown failure")
        for value in relevant.values()
        if value.get("status") == "failed"
    )
    latest_line, latest_text, latest_status, latest_updated_at = _latest_outcome(
        queue, relevant
    )
    return WorkspaceSummary(
        directory=directory,
        title=_required_text(workspace.get("title"), "Workspace title"),
        runtime_status=runtime_status,
        queue_items=len(queue.items),
        eligible=len(reviewable_ids),
        pending=len(pending_ids),
        generated=len(generated_ids),
        approved=len(approved_ids),
        rejected=len(rejected_ids),
        live_fallback=len(live_fallback_ids),
        omitted=len(omitted_ids),
        failed=len(failed_ids),
        skipped_actions=(
            len(queue.items)
            - len(candidates)
            - recoverable_source_audio
            - manual_review
            - resolve_audio
        ),
        skipped_sound_effects=len(candidates)
        - len(spoken)
        - len(audio_event_ids & {item.queue_id for item in candidates}),
        recoverable_source_audio=recoverable_source_audio,
        manual_review=manual_review,
        resolve_audio=resolve_audio,
        missing_voice=(
            len(missing_voice_ids) if selected_voice_manifest is not None else None
        ),
        blocked_reasons=blocked_reasons,
        active=active,
        failure_reasons=tuple(failures.most_common()),
        queue=queue_path,
        output=output,
        state=state_path,
        voice_manifest=selected_voice_manifest,
        latest_line=latest_line,
        latest_text=latest_text,
        latest_status=latest_status,
        latest_updated_at=latest_updated_at,
    )


def _review_technical_metrics(result, text, *, projected_speech_quality=None):
    quality = result.get("quality")
    if not isinstance(quality, dict):
        return None, None, None, ()
    duration = quality.get("duration_seconds")
    peak = quality.get("peak")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = None
    else:
        duration = float(duration)
    peak = float(peak) if isinstance(peak, (int, float)) else None
    is_audio_event = result.get("provider") == AUDIO_EVENT_PROVIDER
    speech_quality = (
        projected_speech_quality
        if projected_speech_quality is not None
        else result.get("speech_quality")
    )
    speech_quality = (
        {}
        if is_audio_event
        else speech_quality
        if isinstance(speech_quality, dict)
        else {}
    )
    word_count = 0 if is_audio_event else _pace_word_count(text)
    leading_silence = speech_quality.get("leading_silence_seconds")
    trailing_silence = speech_quality.get("trailing_silence_seconds")
    trimmed_seconds = sum(
        float(value)
        for value in (leading_silence, trailing_silence)
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )
    audible_duration = (
        None if duration is None else max(0.0, duration - trimmed_seconds)
    )
    words_per_minute = (
        None
        if is_audio_event or not audible_duration
        else float(word_count * 60 / audible_duration)
    )
    internal_silence = speech_quality.get("longest_internal_silence_seconds")
    flags = []
    if peak is not None and peak >= 0.98:
        flags.append("near clipping")
    if (
        isinstance(internal_silence, (int, float))
        and internal_silence >= REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS
    ):
        flags.append("notable pause")
    return duration, words_per_minute, peak, tuple(flags)


def _pace_word_count(text):
    return len(re.findall(r"[\w’'-]+", str(text or ""), flags=re.UNICODE))


def _pace_length_bucket(word_count):
    if word_count <= 9:
        return "short"
    if word_count <= 20:
        return "medium"
    return "long"


def _pace_voice_key(item):
    return str(item.voice_character or item.speaker or "").strip().casefold()


def _annotate_pace_advisories(records):
    """Project relative slow-pace outliers without changing review authority."""
    eligible = [
        item
        for item in records
        if item.words_per_minute is not None
        and item.words_per_minute > 0
        and _pace_word_count(item.text) >= PACE_MINIMUM_WORDS
    ]
    by_voice = {}
    by_voice_and_length = {}
    for item in eligible:
        voice = _pace_voice_key(item)
        length = _pace_length_bucket(_pace_word_count(item.text))
        by_voice.setdefault(voice, []).append(item.words_per_minute)
        by_voice_and_length.setdefault((voice, length), []).append(
            item.words_per_minute
        )

    annotated = []
    for item in records:
        word_count = _pace_word_count(item.text)
        voice = _pace_voice_key(item)
        length = _pace_length_bucket(word_count)
        same_length = by_voice_and_length.get((voice, length), ())
        same_voice = by_voice.get(voice, ())
        baseline = None
        scope = None
        if word_count >= PACE_MINIMUM_WORDS and item.words_per_minute is not None:
            if len(same_length) >= PACE_MINIMUM_LENGTH_BUCKET_SAMPLES:
                baseline = float(median(same_length))
                scope = f"same voice/{length} lines"
            elif len(same_voice) >= PACE_MINIMUM_VOICE_SAMPLES:
                baseline = float(median(same_voice))
                scope = "same voice/all eligible lengths"
        advisories = ()
        ratio = None
        if baseline is not None and baseline > 0:
            ratio = float(item.words_per_minute / baseline)
            if (
                ratio <= PACE_SLOW_RELATIVE_RATIO
                and baseline - item.words_per_minute >= PACE_SLOW_MINIMUM_DELTA_WPM
            ):
                advisories = (
                    f"slow relative outlier {item.words_per_minute:.0f} WPM "
                    f"vs {baseline:.0f} WPM {scope} median",
                )
        annotated.append(
            replace(
                item,
                pace_baseline_wpm=baseline,
                pace_ratio=ratio,
                pace_baseline_scope=scope,
                pace_advisories=advisories,
            )
        )
    return tuple(annotated)


@lru_cache(maxsize=2048)
def _corrected_legacy_speech_quality(audio_path, expected_sha256):
    """Re-measure one legacy WAV from digest-bound bytes for review attention."""
    path = Path(audio_path)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise AuthoringWorkbenchError(
            f"Unable to read generated WAV for review metrics: {error}"
        ) from error
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise AuthoringWorkbenchError(
            "Generated WAV changed while review metrics were being projected"
        )
    try:
        return asdict(
            measure_generated_speech_bytes(
                content,
                analysis_version=SPEECH_QUALITY_ANALYSIS_VERSION,
            )
        )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _review_voice_character(item, result):
    return str(
        result.get("voice_character")
        or synthesis_character_for_line(item.speaker, item.voice_character)
    )


def _normalize_review_queue_ids(queue_ids):
    if queue_ids is None:
        return None
    if not isinstance(queue_ids, (list, tuple, set, frozenset)):
        raise AuthoringWorkbenchError("Review queue IDs must be a collection")
    selected = set()
    for queue_id in queue_ids:
        if not isinstance(queue_id, str) or not queue_id:
            raise AuthoringWorkbenchError("Review queue ID must be non-empty text")
        if queue_id in selected:
            raise AuthoringWorkbenchError(f"Review queue ID is duplicated: {queue_id}")
        selected.add(queue_id)
    return selected


def list_review_items(workspace_directory, queue_ids=None):
    selected_queue_ids = _normalize_review_queue_ids(queue_ids)
    directory, workspace = _load_workspace(workspace_directory)
    queue_path = _within(
        directory, _safe_relative(workspace["queue"], "Queue"), "Queue"
    )
    output = _within(
        directory,
        _safe_relative(workspace["output"], "Output"),
        "Output",
    )
    state_path = output / "generation-state.json"
    if not state_path.is_file():
        return ()
    queue = _load_bound_workspace_queue(directory, workspace)
    story = _load_bound_story_document(directory, workspace)
    state_sha256 = sha256_file(state_path)
    state = load_generation_state(state_path, queue_path)
    if sha256_file(state_path) != state_sha256:
        raise AuthoringWorkbenchError(
            "Generation state changed while review rows were being projected"
        )
    return _list_review_items_from_read(
        queue,
        story,
        state_path,
        state,
        state_sha256,
        queue_path,
        output,
        selected_queue_ids=selected_queue_ids,
    )


def _list_review_items_from_read(
    queue,
    story,
    state_path,
    state,
    state_sha256,
    queue_path,
    output,
    *,
    selected_queue_ids=None,
):
    collection_by_record = {
        (record.line_id, record.text_sha256): collection.collection_id
        for collection in story.collections
        for record in story.records_for_collection(collection.collection_id)
    }
    records = []
    for item in queue.items:
        if selected_queue_ids is not None and item.queue_id not in selected_queue_ids:
            continue
        result = state["items"].get(item.queue_id)
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "unknown")
        if status not in {"generated", "approved", "failed"}:
            continue
        audio = None
        if result.get("path"):
            audio = _within(
                output,
                _safe_relative(result["path"], "Generated audio"),
                "Generated audio",
            )
        stored_speech_quality = result.get("speech_quality")
        projected_speech_quality = None
        if audio is not None and (
            not isinstance(stored_speech_quality, dict)
            or "analysis_version" not in stored_speech_quality
        ):
            projected_speech_quality = _corrected_legacy_speech_quality(
                str(audio), str(result.get("file_sha256") or "")
            )
        duration, words_per_minute, peak, technical_flags = _review_technical_metrics(
            result,
            item.text,
            projected_speech_quality=projected_speech_quality,
        )
        records.append(
            ReviewItem(
                queue_id=item.queue_id,
                line_id=item.line_id,
                speaker=item.speaker,
                voice_character=_review_voice_character(item, result),
                text=item.text,
                status=status,
                review_status=result.get("review_status"),
                attempts=int(result.get("attempts") or 0),
                seed=result.get("seed"),
                last_error=result.get("last_error"),
                audio=audio,
                collection_id=collection_by_record.get(
                    (item.line_id, item.text_sha256)
                ),
                authority=(
                    ReviewAuthority(
                        queue_sha256=state["queue_sha256"],
                        state_sha256=state_sha256,
                        item_sha256=hashlib.sha256(
                            json.dumps(
                                result,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                        audio_sha256=str(result["file_sha256"]),
                    )
                    if status in {"generated", "approved"}
                    else None
                ),
                state=state_path,
                queue=queue_path,
                duration_seconds=duration,
                words_per_minute=words_per_minute,
                peak=peak,
                technical_flags=technical_flags,
                failure_category=(
                    generation_failure_category(result, text=item.text)
                    if status == "failed"
                    else None
                ),
                internal_pause_seconds=_review_internal_pause_seconds(
                    result, failed=status == "failed"
                ),
                repair_strategy=(
                    result.get("failure_repair", {}).get("strategy")
                    if isinstance(result.get("failure_repair"), dict)
                    else None
                ),
            )
        )
    if selected_queue_ids is not None:
        projected = {record.queue_id for record in records}
        missing = sorted(selected_queue_ids - projected)
        if missing:
            raise AuthoringWorkbenchError(
                f"Requested review outcomes are unavailable: {missing}"
            )
    return _annotate_pace_advisories(records)


def review_workspace_item(
    workspace_directory,
    queue_id,
    decision,
    expected_authority=None,
):
    if expected_authority is None:
        summary = inspect_workspace(workspace_directory)
        if summary.state is None:
            raise AuthoringWorkbenchError("Workspace has no generation state to review")
        state_path = summary.state
        queue_path = summary.queue
    else:
        directory, workspace = _load_workspace(workspace_directory)
        queue_path = _within(
            directory,
            _safe_relative(workspace["queue"], "Queue"),
            "Queue",
        )
        output = _within(
            directory,
            _safe_relative(workspace["output"], "Output"),
            "Output",
        )
        state_path = output / "generation-state.json"
        if not state_path.is_file():
            raise AuthoringWorkbenchError("Workspace has no generation state to review")
    try:
        result = review_generation_item(
            state_path,
            queue_id,
            decision,
            expected_authority=expected_authority,
            queue_path=queue_path,
        )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    if isinstance(result, ReviewCommit):
        return result
    return inspect_workspace(workspace_directory)


def prepare_review_audio(item):
    """Return exact selected WAV bytes without projecting unrelated review rows."""
    if (
        not isinstance(item, ReviewItem)
        or item.authority is None
        or item.state is None
        or item.queue is None
    ):
        raise AuthoringWorkbenchError(
            "Generated review row has no exact state, queue, and WAV authority"
        )
    try:
        return load_review_audio_bytes(
            item.state,
            item.queue,
            item.queue_id,
            item.authority,
        )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def review_selected_item(item, decision):
    """Save one displayed review item without rescanning unrelated outcomes."""
    if (
        not isinstance(item, ReviewItem)
        or item.authority is None
        or item.state is None
        or item.queue is None
    ):
        raise AuthoringWorkbenchError(
            "Generated review row has no exact state, queue, and WAV authority"
        )
    try:
        result = review_generation_item(
            item.state,
            item.queue_id,
            decision,
            expected_authority=item.authority,
            queue_path=item.queue,
        )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    if not isinstance(result, ReviewCommit):
        raise AuthoringWorkbenchError("Review transaction returned no commit identity")
    return result


def inspect_generation_readiness(
    workspace_directory,
    *,
    queue_ids=None,
    regenerate_existing=False,
):
    if regenerate_existing and queue_ids is None:
        raise AuthoringWorkbenchError(
            "Workspace regeneration requires explicit queue IDs"
        )
    summary = inspect_workspace(workspace_directory)
    loaded_directory, loaded_workspace = _load_workspace(workspace_directory)
    projection_ids = set(
        workspace_audio_event_spoken_projection_queue_ids(
            loaded_workspace, error_type=AuthoringWorkbenchError
        )
    )
    queue = VoiceGenerationQueue.load(summary.queue)
    state_items = {}
    state = None
    if summary.state is not None:
        state = load_generation_state(summary.state, summary.queue)
        state_items = state["items"]
    control_workspace = _load_workspace(workspace_directory)[1]
    return _inspect_generation_readiness_from_read(
        loaded_directory,
        loaded_workspace,
        summary,
        queue,
        state,
        queue_ids=queue_ids,
        regenerate_existing=regenerate_existing,
        projection_ids=projection_ids,
        state_items=state_items,
        control_workspace=control_workspace,
    )


def _inspect_generation_readiness_from_read(
    directory,
    workspace,
    summary,
    queue,
    state,
    *,
    queue_ids=None,
    regenerate_existing=False,
    projection_ids=None,
    state_items=None,
    control_workspace=None,
):
    if regenerate_existing and queue_ids is None:
        raise AuthoringWorkbenchError(
            "Workspace regeneration requires explicit queue IDs"
        )
    projection_ids = (
        set(
            workspace_audio_event_spoken_projection_queue_ids(
                workspace, error_type=AuthoringWorkbenchError
            )
        )
        if projection_ids is None
        else set(projection_ids)
    )
    state_items = (
        state["items"]
        if state_items is None and state is not None
        else ({} if state_items is None else state_items)
    )
    control_workspace = workspace if control_workspace is None else control_workspace
    known = {item.queue_id for item in queue.items}
    selected = None
    if queue_ids is not None:
        selected = {_required_text(value, "Queue ID") for value in queue_ids}
        unknown = selected - known
        if unknown:
            raise AuthoringWorkbenchError(
                "Selected queue IDs are absent from the workspace queue: "
                + ", ".join(sorted(unknown))
            )
    candidates = []
    pending = 0
    failed = 0
    for item in queue.items:
        if selected is not None and item.queue_id not in selected:
            continue
        if item.action != "generate" or not (
            is_spoken_queue_item(item) or item.queue_id in projection_ids
        ):
            continue
        result = state_items.get(item.queue_id)
        status = result.get("status") if isinstance(result, dict) else None
        if status == "failed":
            failed += 1
            candidates.append(item)
        elif status is None:
            pending += 1
            candidates.append(item)
        elif (
            regenerate_existing
            and status == "generated"
            and result.get("review_status") == "pending_review"
        ):
            candidates.append(item)
    manifest = summary.voice_manifest
    missing, reasons = _voice_readiness(
        workspace,
        candidates,
        set(),
        manifest,
        directory=directory,
    )
    reasons = (
        *_workspace_control_reasons(control_workspace),
        *reasons,
    )
    if not candidates:
        scope = (
            "pending, failed, or regenerable pending-review"
            if regenerate_existing
            else "pending or failed"
        )
        reasons = (f"No {scope} queue items are selected",)
    return GenerationReadiness(
        selected=len(candidates),
        pending=pending,
        failed=failed,
        ready=len(candidates) - len(missing) if manifest is not None else 0,
        missing_voice=len(missing) if manifest is not None else None,
        blocked_reasons=reasons,
        queue_ids=tuple(item.queue_id for item in candidates),
    )


def inspect_collection_selection(workspace_directory, *, collection_ids=None):
    """Map declared story collections to exact immutable queue identities."""
    directory, workspace = _load_workspace(workspace_directory)
    document = _load_bound_story_document(directory, workspace)
    queue = _load_bound_workspace_queue(directory, workspace)
    selected, record_keys, queue_ids = _collection_selection_scope(
        document, queue, collection_ids
    )
    readiness = inspect_generation_readiness(
        workspace_directory,
        queue_ids=queue_ids,
    )
    return _collection_selection_from_scope(selected, record_keys, queue_ids, readiness)


def _collection_selection_scope(document, queue, collection_ids):
    declared = tuple(collection.collection_id for collection in document.collections)
    if collection_ids is None:
        selected = declared
    else:
        requested = {_required_text(value, "Collection ID") for value in collection_ids}
        unknown = requested - set(declared)
        if unknown:
            raise AuthoringWorkbenchError(
                "Selected collection IDs are absent from the story index: "
                + ", ".join(sorted(unknown))
            )
        selected = tuple(value for value in declared if value in requested)
    record_keys = {
        (record.line_id, record.text_sha256)
        for collection_id in selected
        for record in document.records_for_collection(collection_id)
    }
    queue_ids = tuple(
        item.queue_id
        for item in queue.items
        if (item.line_id, item.text_sha256) in record_keys
    )
    return selected, record_keys, queue_ids


def _collection_selection_from_scope(selected, record_keys, queue_ids, readiness):
    return CollectionSelection(
        collection_ids=selected,
        collection_count=len(selected),
        story_records=len(record_keys),
        queue_items=len(queue_ids),
        queue_ids=queue_ids,
        readiness=readiness,
    )


def list_workspace_collections(workspace_directory):
    directory, workspace = _load_workspace(workspace_directory)
    document = _load_bound_story_document(directory, workspace)
    return _workspace_collections_from_document(document)


def _workspace_collections_from_document(document):
    return tuple(
        WorkspaceCollection(
            collection_id=collection.collection_id,
            title=collection.title,
            kind=collection.kind,
            record_count=len(document.records_for_collection(collection.collection_id)),
        )
        for collection in document.collections
    )


def workspace_voice_snapshot(workspace_directory):
    """Load exact hash-bound voice tokens without trusting cached resolved paths."""
    directory, workspace = _load_workspace(workspace_directory)
    return _workspace_voice_snapshot_from_read(directory, workspace)


def _workspace_voice_snapshot_from_read(directory, workspace):
    voices, _controls = _workspace_voice_projection_from_read(
        directory, workspace, verify_controls=True
    )
    return voices


def _workspace_voice_projection_from_read(directory, workspace, *, verify_controls):
    voice = workspace.get("voice_manifest")
    if not isinstance(voice, dict):
        return (), ()
    manifest_path = _within(
        directory,
        _safe_relative(voice.get("path"), "Voice manifest snapshot"),
        "Voice manifest snapshot",
    )
    payload = _read_bound_bytes(
        manifest_path,
        _require_sha256(voice.get("sha256"), "Voice manifest snapshot SHA-256"),
        "Voice manifest snapshot",
    )
    try:
        document = json.loads(payload.decode("utf-8"))
        entries = validate_voice_manifest(document)
    except (UnicodeDecodeError, json.JSONDecodeError, VoiceManifestError) as error:
        raise AuthoringWorkbenchError(
            f"Workspace voice manifest snapshot is invalid: {error}"
        ) from error
    controls = {}
    for control in voice.get("controls", []):
        if not isinstance(control, dict):
            raise AuthoringWorkbenchError("Workspace voice control is malformed")
        path = _within(
            directory,
            _safe_relative(control.get("path"), "Voice reference snapshot"),
            "Voice reference snapshot",
        )
        controls[path] = _require_sha256(
            control.get("sha256"), "Voice reference snapshot SHA-256"
        )
    values = []
    used = set()
    for entry in entries:
        references = []
        for value in entry.references:
            relative = _safe_relative(value, "Voice reference")
            path = _within(manifest_path.parent, relative, "Voice reference")
            expected = controls.get(path)
            if expected is None:
                raise AuthoringWorkbenchError(
                    f"Voice reference is absent from workspace controls: {value!r}"
                )
            if verify_controls:
                _read_bound_bytes(path, expected, "Voice reference snapshot")
            references.append(path)
            used.add(path)
        values.append(
            WorkspaceVoice(
                character=entry.character,
                speaker=entry.speaker,
                aliases=entry.aliases,
                references=tuple(references),
            )
        )
    if used != set(controls):
        raise AuthoringWorkbenchError(
            "Workspace voice control inventory does not match the manifest snapshot"
        )
    return tuple(values), tuple(controls.items())


def failure_reference_runtime_binding(workspace_directory):
    """Return exact synthetic voices and controls for one bound successor."""
    directory, workspace = _load_workspace(workspace_directory)
    return _failure_reference_runtime_binding(directory, workspace)


def _load_bound_story_document(directory, workspace):
    story = workspace.get("story_index")
    if not isinstance(story, dict):
        raise AuthoringWorkbenchError(
            "Collection selection requires a snapshotted story index"
        )
    path = _within(
        directory,
        _safe_relative(story.get("path"), "Story index snapshot"),
        "Story index snapshot",
    )
    payload = _read_bound_bytes(
        path,
        _require_sha256(story.get("sha256"), "Story index snapshot SHA-256"),
        "Story index snapshot",
    )
    with tempfile.TemporaryDirectory(prefix="vntts-story-snapshot-") as temporary:
        snapshot = Path(temporary) / "story-index.jsonl"
        snapshot.write_bytes(payload)
        try:
            return load_story_index_document(snapshot)
        except StoryIndexError as error:
            raise AuthoringWorkbenchError(str(error)) from error


def immutable_history_timestamps(workspace_directory):
    """Return friendly timestamps from immutable source and workspace records."""
    directory, workspace = _load_workspace(workspace_directory)
    return _immutable_history_timestamps_from_read(directory, workspace)


def _immutable_history_timestamps_from_read(directory, workspace):
    snapshot, snapshot_sha256, _payload = _load_json_snapshot(
        directory / workspace["source"]["snapshot"],
        "workspace import snapshot",
    )
    if snapshot_sha256 != workspace["source"]["import_sha256"]:
        raise AuthoringWorkbenchError("Workspace import snapshot was modified")
    legacy_job = snapshot.get("legacy_job")
    candidates = []
    if isinstance(legacy_job, dict):
        candidates.extend(
            (
                ("Source created", legacy_job.get("created_at")),
                ("Source updated", legacy_job.get("updated_at")),
            )
        )
    candidates.append(("Imported", snapshot.get("imported_at")))
    candidates.append(("Workspace created", workspace.get("created_at")))
    values = []
    for kind, value in candidates:
        parsed = _parse_history_timestamp(value)
        if parsed is None:
            continue
        utc = parsed.astimezone(timezone.utc)
        values.append(
            (
                utc,
                kind,
                ImmutableHistoryTimestamp(
                    kind=kind,
                    instant=utc.isoformat(),
                    display=f"{kind}: {utc:%Y-%m-%d %H:%M:%S} UTC",
                ),
            )
        )
    return tuple(value for _instant, _kind, value in sorted(values))


def _load_workbench_projection_read(workspace_directory):
    with shared_workspace_state_reads():
        return _load_workbench_projection_read_scoped(workspace_directory)


def _load_workbench_projection_read_scoped(workspace_directory):
    directory, workspace, workspace_sha256 = load_workspace_authority(
        workspace_directory
    )
    queue_path = _within(
        directory, _safe_relative(workspace["queue"], "Queue"), "Queue"
    )
    output = _within(directory, _safe_relative(workspace["output"], "Output"), "Output")
    cached_state = cached_workspace_generation_state(directory, workspace)
    queue = (
        cached_state[0]
        if cached_state is not None
        else _load_bound_workspace_queue(directory, workspace)
    )
    state_path = output / "generation-state.json"
    state = None
    state_sha256 = None
    if state_path.is_file():
        if cached_state is not None:
            state, state_sha256 = cached_state[1], cached_state[3]
        else:
            state_sha256 = sha256_file(state_path)
            try:
                state = load_generation_state(state_path, queue_path)
            except BulkGenerationError as error:
                raise AuthoringWorkbenchError(str(error)) from error
        if sha256_file(state_path) != state_sha256:
            raise AuthoringWorkbenchError(
                "Generation state changed while review rows were being projected"
            )
    queue_sha256 = workspace_queue_sha256(workspace, error_type=AuthoringWorkbenchError)
    if sha256_file(queue_path) != queue_sha256:
        raise AuthoringWorkbenchError("Workspace queue was modified")
    story = _load_bound_story_document(directory, workspace)
    voices, voice_controls = _workspace_voice_projection_from_read(
        directory, workspace, verify_controls=False
    )
    if sha256_file(directory / "workspace.json") != workspace_sha256:
        raise AuthoringWorkbenchError("Workspace authority changed while it was loaded")
    return _WorkbenchProjectionRead(
        directory=directory,
        workspace=workspace,
        workspace_sha256=workspace_sha256,
        queue_path=queue_path,
        queue=queue,
        output=output,
        state_path=state_path if state is not None else None,
        state=state,
        state_sha256=state_sha256,
        story=story,
        voices=voices,
        voice_controls=voice_controls,
    )


def load_workbench_projection_data(
    workspace_directory,
    selected_collection_ids=None,
    *,
    local_process_id=None,
    local_process_started_at=None,
):
    """Build one full UI projection from one bounded authority read."""
    read = _load_workbench_projection_read(workspace_directory)
    summary = _inspect_workspace_from_read(
        read.directory,
        read.workspace,
        read.queue_path,
        read.output,
        read.queue,
        read.state_path,
        read.state,
        local_process_id=local_process_id,
        local_process_started_at=local_process_started_at,
    )
    reviews = (
        ()
        if read.state_path is None
        else _list_review_items_from_read(
            read.queue,
            read.story,
            read.state_path,
            read.state,
            read.state_sha256,
            read.queue_path,
            read.output,
        )
    )
    collections = _workspace_collections_from_document(read.story)
    declared = tuple(value.collection_id for value in collections)
    if selected_collection_ids is None:
        selected = declared
    else:
        requested = set(selected_collection_ids)
        selected = tuple(value for value in declared if value in requested)
    selected, record_keys, queue_ids = _collection_selection_scope(
        read.story, read.queue, selected
    )
    readiness = _inspect_generation_readiness_from_read(
        read.directory,
        read.workspace,
        summary,
        read.queue,
        read.state,
        queue_ids=queue_ids,
    )
    collection_selection = _collection_selection_from_scope(
        selected, record_keys, queue_ids, readiness
    )
    history = _immutable_history_timestamps_from_read(read.directory, read.workspace)
    return WorkbenchProjectionData(
        summary=summary,
        reviews=tuple(reviews),
        workspace=read.workspace,
        collections=collections,
        collection_selection=collection_selection,
        history=history,
        voices=read.voices,
        _voice_controls=read.voice_controls,
    )


def generation_command(
    workspace_directory,
    *,
    backend=None,
    voice_manifest=None,
    model=None,
    generation_profile=None,
    narrator_character=None,
    retries=2,
    seed=0,
    include_prefer_source=False,
    queue_ids=None,
    regenerate_existing=False,
):
    if include_prefer_source:
        raise AuthoringWorkbenchError(
            "Recoverable source-audio generation requires an explicit preflight policy"
        )
    if regenerate_existing and queue_ids is None:
        raise AuthoringWorkbenchError(
            "Workspace regeneration requires explicit queue IDs"
        )
    directory, workspace = _load_workspace(workspace_directory)
    run_config = workspace["run_config"]
    configured_backend = run_config.get("backend")
    configured_model = run_config.get("model")
    configured_profile = run_config.get("generation_profile")
    policy = _workspace_missing_voice_policy(workspace)
    repair_policy = _workspace_failure_repair_policy(workspace)
    projection_ids = workspace_audio_event_spoken_projection_queue_ids(
        workspace, error_type=AuthoringWorkbenchError
    )
    if not repair_policy.is_empty:
        if queue_ids is None:
            queue_ids = repair_policy.queue_ids
        elif set(queue_ids) != set(repair_policy.queue_ids):
            raise AuthoringWorkbenchError(
                "Generation queue IDs differ from workspace failure-repair policy"
            )
    if projection_ids:
        if queue_ids is None:
            queue_ids = projection_ids
        elif set(queue_ids) != set(projection_ids):
            raise AuthoringWorkbenchError(
                "Generation queue IDs differ from workspace audio-event projections"
            )
    if repair_policy.offline_fallback_queue_ids and retries != 0:
        raise AuthoringWorkbenchError(
            "Offline fallback is a single backend-owned unseeded attempt; set retries to 0"
        )
    if configured_backend is None:
        raise AuthoringWorkbenchError(
            "Create a config-addressed workspace with a generation backend"
        )
    if backend is not None and backend != configured_backend:
        raise AuthoringWorkbenchError(
            "Generation backend differs from workspace config"
        )
    backend = configured_backend
    if model is not None and model != configured_model:
        raise AuthoringWorkbenchError("Generation model differs from workspace config")
    model = configured_model
    if generation_profile is not None and generation_profile != configured_profile:
        raise AuthoringWorkbenchError(
            "Generation profile differs from workspace config"
        )
    generation_profile = configured_profile
    summary = inspect_workspace(directory, voice_manifest=voice_manifest)
    readiness = inspect_generation_readiness(
        workspace_directory,
        queue_ids=queue_ids,
        regenerate_existing=regenerate_existing,
    )
    if readiness.blocked_reasons:
        raise AuthoringWorkbenchError("; ".join(readiness.blocked_reasons))
    manifest = summary.voice_manifest
    if manifest is None:
        raise AuthoringWorkbenchError("Select an existing voice manifest")
    if backend not in {"pocket-tts", "chatterbox-nano", "moss-tts"}:
        raise AuthoringWorkbenchError(f"Unsupported generation backend: {backend!r}")
    configured_narrator = workspace.get("narrator_character")
    if narrator_character is not None and narrator_character != configured_narrator:
        raise AuthoringWorkbenchError(
            "Persist the narrator selection in workspace configuration before generation"
        )
    command = [
        sys.executable,
        "-m",
        "vntts.authoring.cli",
        "generate",
        "--workspace",
        str(directory),
        "--queue",
        str(summary.queue),
        "--output",
        str(summary.output),
        "--voice-manifest",
        str(manifest.resolve()),
        "--backend",
        backend,
        "--narrator-character",
        _required_text(
            configured_narrator,
            "Narrator character",
        ),
        "--retries",
        str(_nonnegative_integer(retries, "Retries")),
        "--seed",
        str(_integer(seed, "Seed")),
    ]
    if model:
        command.extend(("--model", str(model)))
    if generation_profile:
        command.extend(("--generation-profile", generation_profile))
    if policy.mode == NARRATOR_ROLES:
        for role in policy.roles:
            command.extend(("--narrator-fallback-role", role))
    elif policy.mode == NARRATOR_ALL_UNRESOLVED:
        command.append("--narrator-fallback-all")
    for queue_id in repair_policy.sentence_segment_queue_ids:
        command.extend(("--sentence-segment-failed", queue_id))
    for queue_id in repair_policy.edge_silence_queue_ids:
        command.extend(("--trim-edge-silence-failed", queue_id))
    for queue_id in repair_policy.bounded_seed_retry_queue_ids:
        command.extend(("--bounded-seed-failed", queue_id))
    for queue_id in repair_policy.offline_fallback_queue_ids:
        command.extend(("--offline-fallback-failed", queue_id))
    for queue_id in repair_policy.inline_pause_queue_ids:
        command.extend(("--inline-pause-failed", queue_id))
    for queue_id in projection_ids:
        command.extend(("--audio-event-spoken-projection", queue_id))
    if repair_policy.segment_pause_ms != 180:
        command.extend(("--segment-pause-ms", str(repair_policy.segment_pause_ms)))
    if repair_policy.inline_pause_ms != 180:
        command.extend(("--inline-pause-ms", str(repair_policy.inline_pause_ms)))
    if queue_ids is not None:
        for queue_id in queue_ids:
            command.extend(("--queue-id", _required_text(queue_id, "Queue ID")))
    if regenerate_existing:
        command.append("--regenerate-existing")
    return tuple(command)


def generation_control_bindings(
    workspace_directory,
    *,
    queue,
    output,
    voice_manifest,
    backend,
    model,
    generation_profile,
    narrator_character,
    missing_voice_policy=None,
    failure_repair_policy=None,
    audio_event_spoken_projection_queue_ids=None,
):
    directory, workspace = _load_workspace(workspace_directory)
    expected_queue = (directory / "queue.jsonl").resolve()
    expected_output = (directory / "generated-audio").resolve()
    selected_manifest = _selected_voice_manifest(directory, workspace)
    if Path(queue).expanduser().resolve() != expected_queue:
        raise AuthoringWorkbenchError("Generation queue differs from workspace config")
    if Path(output).expanduser().resolve() != expected_output:
        raise AuthoringWorkbenchError("Generation output differs from workspace config")
    if (
        selected_manifest is None
        or Path(voice_manifest).expanduser().resolve() != selected_manifest
    ):
        raise AuthoringWorkbenchError(
            "Generation voice manifest differs from workspace config"
        )
    run_config = workspace["run_config"]
    try:
        policy = (
            missing_voice_policy
            if isinstance(missing_voice_policy, MissingVoicePolicy)
            else MissingVoicePolicy.from_document(missing_voice_policy)
        )
    except MissingVoicePolicyError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    try:
        repair_policy = (
            failure_repair_policy
            if isinstance(failure_repair_policy, FailureRepairPolicy)
            else FailureRepairPolicy.from_document(failure_repair_policy)
        )
    except FailureRepairPolicyError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    expected = {
        "backend": backend,
        "model": model,
        "generation_profile": generation_profile,
        "missing_voice_policy": policy.to_document(),
        "failure_repair_policy": repair_policy.to_document(),
    }
    projection_ids = tuple(
        sorted(
            _required_text(value, "Audio-event spoken projection queue ID")
            for value in (audio_event_spoken_projection_queue_ids or ())
        )
    )
    if len(projection_ids) != len(set(projection_ids)):
        raise AuthoringWorkbenchError(
            "Audio-event spoken projection queue IDs must be unique"
        )
    if projection_ids:
        expected["audio_event_spoken_projection_queue_ids"] = list(projection_ids)
    if _workspace_run_config_with_policy(run_config) != expected:
        raise AuthoringWorkbenchError("Generation run config differs from workspace")
    if narrator_character != workspace["narrator_character"]:
        raise AuthoringWorkbenchError(
            "Narrator selection differs from workspace config"
        )
    voice = workspace["voice_manifest"]
    bindings = {selected_manifest: voice["sha256"]}
    for control in voice["controls"]:
        path = _within(
            directory,
            _safe_relative(control["path"], "Voice reference snapshot"),
            "Voice reference snapshot",
        )
        bindings[path] = control["sha256"]
    runtime_binding = _failure_reference_runtime_binding(directory, workspace)
    if runtime_binding is not None:
        bindings.update(runtime_binding.controls)
    return bindings


def generation_output_identity(workspace_directory):
    directory, _workspace = _load_workspace(workspace_directory)
    output = directory / "generated-audio"
    metadata = output.stat(follow_symlinks=False)
    return {
        "path": str(output),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


_terminal_review_outcome = is_terminal_review_outcome


def _root_carry_forward_authority(value):
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


def read_workspace_file_bytes(path, label):
    """Read one non-symlink workspace file with workbench error semantics."""
    return _read_file_bytes(path, label)


_workspace_config_fingerprint = workspace_config_fingerprint


def _active_attempt(value, eligible_ids):
    if not isinstance(value, dict) or value.get("queue_id") not in eligible_ids:
        return None
    return ActiveAttempt(
        queue_id=_optional_text(value.get("queue_id")),
        line_id=_optional_text(value.get("line_id")),
        speaker=_optional_text(value.get("speaker") or value.get("voice_character")),
        text=_optional_text(value.get("text")),
        phase=_optional_text(value.get("phase")),
        attempt=_optional_integer(value.get("attempt")),
        attempt_limit=_optional_integer(value.get("attempt_limit")),
        total_attempts=_optional_integer(value.get("total_attempts")),
        seed=_optional_integer(value.get("seed")),
        started_at=_optional_text(value.get("started_at")),
        updated_at=_optional_text(value.get("updated_at")),
        last_error=_optional_text(value.get("last_error")),
    )


def _runtime_status(
    output,
    active,
    pending,
    review_pending,
    failed,
    missing_voice,
    blocked_reasons,
    *,
    queue_sha256,
    local_process_id,
    local_process_started_at,
    process_checker,
    process_start_checker,
):
    lease_path = output / ".generation-lease.json"
    if lease_path.is_file():
        try:
            lease = _load_json(lease_path, "generation lease")
            if (
                lease.get("schema") != LEASE_SCHEMA
                or lease.get("schema_version") != LEASE_VERSION
                or lease.get("queue_sha256") != queue_sha256
            ):
                return AuthoringRuntimeStatus.BLOCKED
            pid = lease.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                return AuthoringRuntimeStatus.BLOCKED
            hostname = lease.get("hostname")
            if hostname not in {None, socket.gethostname()}:
                return AuthoringRuntimeStatus.RUNNING_EXTERNAL
            if process_checker(pid):
                recorded_start = lease.get("process_started_at")
                actual_start = process_start_checker(pid)
                if (
                    recorded_start is not None
                    and actual_start is not None
                    and actual_start != recorded_start
                ):
                    return AuthoringRuntimeStatus.INTERRUPTED
                if (
                    local_process_id is not None
                    and pid == int(local_process_id)
                    and (
                        recorded_start is None
                        or local_process_started_at == recorded_start
                    )
                ):
                    return AuthoringRuntimeStatus.RUNNING_HERE
                return AuthoringRuntimeStatus.RUNNING_EXTERNAL
            return AuthoringRuntimeStatus.INTERRUPTED
        except AuthoringWorkbenchError:
            return AuthoringRuntimeStatus.BLOCKED
    if active is not None:
        return AuthoringRuntimeStatus.INTERRUPTED
    if review_pending:
        return AuthoringRuntimeStatus.NEEDS_REVIEW
    if failed:
        return AuthoringRuntimeStatus.NEEDS_ATTENTION
    if blocked_reasons:
        return AuthoringRuntimeStatus.BLOCKED
    if pending:
        return AuthoringRuntimeStatus.READY
    if missing_voice:
        return AuthoringRuntimeStatus.NEEDS_ATTENTION
    return AuthoringRuntimeStatus.COMPLETE


def _voice_readiness(
    workspace,
    spoken,
    completed_ids,
    manifest_path,
    *,
    directory=None,
):
    if manifest_path is None:
        return set(), ("Select an existing voice manifest",)
    try:
        registry = CharacterVoiceRegistry.from_file(manifest_path)
        document, entries = load_voice_manifest(manifest_path, allow_legacy=False)
        queue_overrides = queue_voice_overrides_from_manifest(
            document,
            voices=entries,
        )
    except (SourceReferenceBindingError, VoiceManifestError, OSError) as error:
        raise AuthoringWorkbenchError(
            f"Unable to load voice manifest: {error}"
        ) from error
    if directory is None:
        directory = Path(manifest_path).expanduser().resolve().parents[2]
    runtime_binding = _failure_reference_runtime_binding(directory, workspace)
    if runtime_binding is not None:
        try:
            registry = CharacterVoiceRegistry(
                (*registry.unique_voices(), *runtime_binding.voices)
            )
        except VoiceManifestError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        queue_overrides = {
            **queue_overrides,
            **runtime_binding.queue_voice_overrides,
        }
    narrator = str(workspace.get("narrator_character") or "Narrator")
    policy = _workspace_missing_voice_policy(workspace)
    narrator_voice = registry.resolve(narrator)
    narrator_ready = (
        narrator_voice is not None
        and bool(narrator_voice.references)
        and all(reference.is_file() for reference in narrator_voice.references)
    )
    missing = set()
    for item in spoken:
        if item.queue_id in completed_ids:
            continue
        requested_character = synthesis_character_for_line(
            item.speaker, item.voice_character
        )
        character = queue_overrides.get(item.queue_id) or (
            narrator if requested_character == "Narrator" else requested_character
        )
        voice = registry.resolve(character or item.speaker or "")
        voice_missing = (
            voice is None
            or not voice.references
            or any(not reference.is_file() for reference in voice.references)
        )
        if voice_missing and policy.applies_to(requested_character) and narrator_ready:
            continue
        if voice_missing:
            missing.add(item.queue_id)
    if missing:
        return missing, (
            f"Voice references are missing or unsafe for {len(missing)} queued line(s)",
        )
    return missing, ()


def inspect_voice_readiness(
    workspace,
    spoken,
    completed_ids,
    manifest_path,
    *,
    directory=None,
):
    """Project exact missing-voice IDs through the workbench policy."""
    return _voice_readiness(
        workspace,
        spoken,
        completed_ids,
        manifest_path,
        directory=directory,
    )


def _workspace_control_reasons(workspace):
    run_config = workspace.get("run_config", {})
    missing = [
        label
        for field, label in (
            ("backend", "generation backend"),
            ("model", "generation model"),
            ("generation_profile", "generation profile"),
        )
        if not _optional_text(run_config.get(field))
    ]
    if not missing:
        return ()
    return ("Workspace requires " + ", ".join(missing),)


def _latest_outcome(queue, relevant):
    if not relevant:
        return None, None, None, None
    queue_by_id = {item.queue_id: item for item in queue.items}
    queue_id, value = max(
        relevant.items(), key=lambda pair: str(pair[1].get("updated_at") or "")
    )
    item = queue_by_id.get(queue_id)
    return (
        None if item is None else item.line_id,
        None if item is None else item.text,
        str(value.get("review_status") or value.get("status") or "unknown"),
        _optional_text(value.get("updated_at")),
    )


def load_workspace_json(path, description):
    """Load one workspace JSON object with workbench error semantics."""
    return _load_json(path, description)


def load_workspace_json_snapshot(path, description):
    """Load one exact workspace JSON object and its payload identity."""
    return _load_json_snapshot(path, description)


def safe_workspace_relative_path(value, label):
    """Validate one canonical POSIX-relative workspace path."""
    return _safe_relative(value, label)


def contained_workspace_path(root, relative, label):
    """Resolve one already validated relative path inside its owning root."""
    return _within(root, relative, label)


def merge_terminal_conflict_resolution(*args, **kwargs):
    """Compatibility facade for the former direct workbench export."""
    module = importlib.import_module("vntts.authoring.terminal_conflict_workspace")
    return module.merge_terminal_conflict_resolution(*args, **kwargs)


def require_workspace_sha256(value, label):
    """Validate one workspace SHA-256 with workbench error semantics."""
    return _require_sha256(value, label)


def _optional_integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _integer(value, label):
    if isinstance(value, bool):
        raise AuthoringWorkbenchError(f"{label} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise AuthoringWorkbenchError(f"{label} must be an integer") from error


def _nonnegative_integer(value, label):
    result = _integer(value, label)
    if result < 0:
        raise AuthoringWorkbenchError(f"{label} must not be negative")
    return result


__all__ = [
    "ActiveAttempt",
    "AuthoringRuntimeStatus",
    "AuthoringWorkbenchError",
    "CollectionSelection",
    "FailureReferenceRuntimeBinding",
    "GenerationReadiness",
    "ImmutableHistoryTimestamp",
    "ReviewItem",
    "WorkbenchProjectionData",
    "WorkspaceCreationResult",
    "WorkspaceCollection",
    "WorkspaceSummary",
    "WorkspaceVoice",
    "WORKSPACE_SCHEMA",
    "WORKSPACE_VERSION",
    "create_audio_event_composition_workspace",
    "create_failure_reference_workspace",
    "create_resume_workspace",
    "default_workspaces_root",
    "discover_imports",
    "discover_workspaces",
    "generation_command",
    "generation_control_bindings",
    "generation_output_identity",
    "inspect_workspace",
    "inspect_collection_selection",
    "inspect_generation_readiness",
    "inspect_voice_readiness",
    "immutable_history_timestamps",
    "list_workspace_collections",
    "list_review_items",
    "load_workbench_projection_data",
    "load_workspace_authority",
    "load_workspace_json",
    "load_workspace_json_snapshot",
    "merge_terminal_conflict_resolution",
    "merge_reconciled_workspace_outcomes",
    "merge_workspace_outcomes",
    "prepare_review_audio",
    "read_workspace_file_bytes",
    "require_workspace_sha256",
    "review_selected_item",
    "review_workspace_item",
    "safe_workspace_relative_path",
    "contained_workspace_path",
    "validate_workspace_provenance_extensions",
    "workspace_voice_snapshot",
]

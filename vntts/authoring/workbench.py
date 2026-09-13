"""Safe mutable workspaces and truthful status for graphical authoring."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file as sha256_file

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    ReviewCommit,
    load_review_audio_bytes,
    review_generation_item,
)
from vntts.authoring.failure_repair import (
    INLINE_PAUSE_MARKER,
    SENTENCE_BOUNDARY_SEGMENTATION,
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
)
from vntts.authoring.workspace_authority import (
    _load_json,
    _load_json_snapshot,
    _load_workspace,
    _require_sha256,
    _safe_relative,
    _within,
    load_workspace_authority,
    validate_workspace_provenance_extensions,
)
from vntts.authoring.workspace_config import (
    workspace_config_fingerprint,
)
from vntts.authoring.workspace_creation import (
    _failure_reference_runtime_binding,
    _read_file_bytes,
    create_audio_event_composition_workspace,
    create_failure_reference_workspace,
    create_resume_workspace,
    default_workspaces_root,
)
from vntts.authoring.workspace_inspection import (
    discover_imports,
    discover_workspaces,
    generation_command,
    generation_control_bindings,
    generation_failure_category,
    generation_output_identity,
    immutable_history_timestamps,
    inspect_collection_selection,
    inspect_generation_readiness,
    inspect_voice_readiness,
    inspect_workspace,
    list_review_items,
    list_workspace_collections,
    load_workbench_projection_data,
    workspace_voice_snapshot,
)
from vntts.authoring.workspace_outcome_merge import (
    merge_reconciled_workspace_outcomes,
    merge_workspace_outcomes,
)
from vntts.authoring.workspace_voice_runtime import (
    FailureReferenceRuntimeBinding,
)

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


def review_technical_summary(item: ReviewItem) -> str:
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


def review_workspace_item(
    workspace_directory: str | Path,
    queue_id: str,
    decision: str,
    expected_authority: object = None,
) -> ReviewCommit | WorkspaceSummary:
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


def prepare_review_audio(item: ReviewItem) -> bytes:
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


def review_selected_item(item: ReviewItem, decision: str) -> ReviewCommit:
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


def failure_reference_runtime_binding(
    workspace_directory: str | Path,
) -> FailureReferenceRuntimeBinding | None:
    """Return exact synthetic voices and controls for one bound successor."""
    directory, workspace = _load_workspace(workspace_directory)
    return _failure_reference_runtime_binding(directory, workspace)


_terminal_review_outcome = is_terminal_review_outcome


def read_workspace_file_bytes(path: str | Path, label: str) -> bytes:
    """Read one non-symlink workspace file with workbench error semantics."""
    return _read_file_bytes(path, label)


_workspace_config_fingerprint = workspace_config_fingerprint


def load_workspace_json(path: str | Path, description: str) -> dict[str, object]:
    """Load one workspace JSON object with workbench error semantics."""
    return _load_json(path, description)


def load_workspace_json_snapshot(
    path: str | Path, description: str
) -> tuple[dict[str, object], str, bytes]:
    """Load one exact workspace JSON object and its payload identity."""
    return _load_json_snapshot(path, description)


def safe_workspace_relative_path(value: object, label: str) -> Path:
    """Validate one canonical POSIX-relative workspace path."""
    return _safe_relative(value, label)


def contained_workspace_path(root: Path, relative: Path, label: str) -> Path:
    """Resolve one already validated relative path inside its owning root."""
    return _within(root, relative, label)


def merge_terminal_conflict_resolution(*args: object, **kwargs: object) -> object:
    """Compatibility facade for the former direct workbench export."""
    module = importlib.import_module("vntts.authoring.terminal_conflict_workspace")
    return module.merge_terminal_conflict_resolution(*args, **kwargs)


def require_workspace_sha256(value: object, label: str) -> str:
    """Validate one workspace SHA-256 with workbench error semantics."""
    return _require_sha256(value, label)


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
    "generation_failure_category",
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

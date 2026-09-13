"""Immutable generation-state snapshots owned by an authoring workspace."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_state import (
    load_stable_generation_queue,
    validate_generation_state_document,
)
from vntts.authoring.workspace_config import workspace_queue_sha256
from vntts.authoring.workspace_foundation import read_regular_file

WorkspaceState = tuple[VoiceGenerationQueue, dict[str, object], bytes, str]
WorkspaceStateCache = dict[tuple[str, object], WorkspaceState]

_SHARED_STATE_READS: ContextVar[WorkspaceStateCache | None] = ContextVar(
    "shared_workspace_state_reads", default=None
)


@contextmanager
def shared_workspace_state_reads() -> Iterator[None]:
    """Reuse one fully validated immutable state during a bounded UI read."""
    if _SHARED_STATE_READS.get() is not None:
        yield
        return
    token = _SHARED_STATE_READS.set({})
    try:
        yield
    finally:
        _SHARED_STATE_READS.reset(token)


def cached_workspace_generation_state(
    directory: str | Path, workspace: Mapping[str, object]
) -> WorkspaceState | None:
    cache = _SHARED_STATE_READS.get()
    if cache is None:
        return None
    return cache.get(_state_cache_key(directory, workspace))


def share_workspace_generation_state(
    directory: str | Path,
    workspace: Mapping[str, object],
    result: WorkspaceState,
) -> None:
    cache = _SHARED_STATE_READS.get()
    if cache is not None:
        cache[_state_cache_key(directory, workspace)] = result


def load_stable_workspace_generation_state(
    directory: str | Path,
    workspace: Mapping[str, object],
    label: str,
    *,
    error_type: type[Exception] = ValueError,
) -> WorkspaceState:
    """Capture one inactive queue-bound state and its exact payload identity."""
    directory = Path(directory).expanduser().resolve()
    cached = cached_workspace_generation_state(directory, workspace)
    if cached is not None:
        return cached
    result = _load_stable_workspace_generation_state(
        directory, workspace, label, error_type
    )
    share_workspace_generation_state(directory, workspace, result)
    return result


def _load_stable_workspace_generation_state(
    directory: Path,
    workspace: Mapping[str, object],
    label: str,
    error_type: type[Exception],
) -> WorkspaceState:
    expected_queue_sha256 = workspace_queue_sha256(workspace, error_type=error_type)
    queue_path = directory / "queue.jsonl"
    if queue_path.is_symlink() or not queue_path.is_file():
        raise error_type("Workspace queue is missing or unsafe")
    try:
        queue: VoiceGenerationQueue
        queue_sha256: str
        queue, queue_sha256 = load_stable_generation_queue(queue_path)
    except BulkGenerationError as error:
        raise error_type(str(error)) from error
    if queue_sha256 != expected_queue_sha256:
        raise error_type("Workspace queue was modified")

    output = directory / "generated-audio"
    state_path = output / "generation-state.json"
    payload = read_regular_file(
        state_path,
        f"{label} generation state",
        error_type=error_type,
    )
    digest = hashlib.sha256(payload).hexdigest()
    try:
        parsed: object = json.loads(payload.decode("utf-8"))
        validated = validate_generation_state_document(
            parsed,
            output,
            queue,
            queue_sha256,
        )
        assert isinstance(parsed, dict)
    except (UnicodeDecodeError, json.JSONDecodeError, BulkGenerationError) as error:
        raise error_type(f"Outcome merge {label} state is invalid: {error}") from error
    if parsed != validated or sha256_file(state_path) != digest:
        raise error_type(f"Outcome merge {label} state changed while it was loaded")
    if parsed.get("active") is not None:
        raise error_type(f"Outcome merge {label} has an active generation attempt")
    if (output / ".generation-lease.json").exists():
        raise error_type(f"Outcome merge {label} has a generation lease")
    if any(output.rglob("*.partial.wav")):
        raise error_type(f"Outcome merge {label} has a partial generation artifact")
    return queue, parsed, payload, digest


def _state_cache_key(
    directory: str | Path, workspace: Mapping[str, object]
) -> tuple[str, object]:
    return (
        str(Path(directory).expanduser().resolve()),
        workspace.get("config_fingerprint"),
    )


__all__ = [
    "cached_workspace_generation_state",
    "load_stable_workspace_generation_state",
    "share_workspace_generation_state",
    "shared_workspace_state_reads",
]

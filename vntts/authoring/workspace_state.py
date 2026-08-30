"""Immutable generation-state snapshots owned by an authoring workspace."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_state import (
    load_stable_generation_queue,
    validate_generation_state_document,
)
from vntts.authoring.workspace_config import workspace_queue_sha256
from vntts.authoring.workspace_foundation import read_regular_file


def load_stable_workspace_generation_state(
    directory,
    workspace,
    label,
    *,
    error_type=ValueError,
):
    """Capture one inactive queue-bound state and its exact payload identity."""
    directory = Path(directory).expanduser().resolve()
    expected_queue_sha256 = workspace_queue_sha256(workspace, error_type=error_type)
    queue_path = directory / "queue.jsonl"
    if queue_path.is_symlink() or not queue_path.is_file():
        raise error_type("Workspace queue is missing or unsafe")
    try:
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
        parsed = json.loads(payload.decode("utf-8"))
        validated = validate_generation_state_document(
            parsed,
            output,
            queue,
            queue_sha256,
        )
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


__all__ = ["load_stable_workspace_generation_state"]

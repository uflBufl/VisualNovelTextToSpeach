"""Leaf identity and filesystem validation for workspace configuration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.workspace_foundation import (
    contained_path,
    require_sha256,
    safe_relative_path,
)


def workspace_config_fingerprint(
    import_id,
    story_config,
    voice_config,
    narrator_character,
    run_config,
    carry_forward=None,
    outcome_merge=None,
    failure_reference_binding=None,
    terminal_conflict_merge=None,
    config_rebase=None,
    audio_event_composition=None,
):
    """Return the canonical SHA-256 identity of one workspace configuration."""
    fingerprint = {
        "import_id": import_id,
        "story_index": story_config,
        "voice_manifest": voice_config,
        "narrator_character": narrator_character,
        "run_config": run_config,
    }
    if carry_forward is not None:
        fingerprint["carry_forward"] = carry_forward
    if outcome_merge is not None:
        fingerprint["outcome_merge"] = outcome_merge
    if terminal_conflict_merge is not None:
        fingerprint["terminal_conflict_merge"] = terminal_conflict_merge
    if failure_reference_binding is not None:
        fingerprint["failure_reference_binding"] = failure_reference_binding
    if config_rebase is not None:
        fingerprint["config_rebase"] = config_rebase
    if audio_event_composition is not None:
        fingerprint["audio_event_composition"] = audio_event_composition
    payload = json.dumps(
        fingerprint,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def selected_voice_manifest_path(
    directory,
    workspace,
    selected=None,
    *,
    error_type=ValueError,
):
    """Resolve and checksum-validate the selected manifest and its references."""
    value = workspace.get("voice_manifest")
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
        return None
    path = contained_path(
        directory,
        safe_relative_path(
            value["path"],
            "Voice manifest snapshot",
            error_type=error_type,
        ),
        "Voice manifest snapshot",
        error_type=error_type,
    )
    if selected is not None and Path(selected).expanduser().resolve() != path:
        raise error_type("Configure the workspace voice snapshot before generation")
    if not path.is_file() or sha256_file(path) != require_sha256(
        value.get("sha256"),
        "Voice manifest snapshot SHA-256",
        error_type=error_type,
    ):
        raise error_type("Workspace voice manifest snapshot was modified")
    for control in value.get("controls", []):
        relative = safe_relative_path(
            control.get("path"),
            "Voice reference snapshot",
            error_type=error_type,
        )
        reference = contained_path(
            directory,
            relative,
            "Voice reference snapshot",
            error_type=error_type,
        )
        if not reference.is_file() or sha256_file(reference) != require_sha256(
            control.get("sha256"),
            "Voice reference snapshot SHA-256",
            error_type=error_type,
        ):
            raise error_type("Workspace voice reference snapshot was modified")
    return path


__all__ = ["selected_voice_manifest_path", "workspace_config_fingerprint"]

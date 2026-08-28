"""Leaf identity and filesystem validation for workspace configuration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.failure_repair import (
    FailureRepairPolicy,
    FailureRepairPolicyError,
)
from vntts.authoring.missing_voice_policy import (
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.workspace_foundation import (
    contained_path,
    require_sha256,
    safe_relative_path,
)


def normalize_workspace_run_config(run_config, *, error_type=ValueError):
    """Normalize every supported workspace run-config generation."""
    if not isinstance(run_config, dict):
        raise error_type("Workspace run configuration is malformed")
    legacy_fields = {"backend", "model", "generation_profile"}
    fallback_fields = legacy_fields | {"missing_voice_policy"}
    current_fields = fallback_fields | {"failure_repair_policy"}
    if frozenset(run_config) not in {
        frozenset(legacy_fields),
        frozenset(fallback_fields),
        frozenset(current_fields),
    }:
        raise error_type("Workspace run configuration is malformed")
    try:
        policy = MissingVoicePolicy.from_document(
            run_config.get("missing_voice_policy")
        )
        repair_policy = FailureRepairPolicy.from_document(
            run_config.get("failure_repair_policy")
        )
    except (MissingVoicePolicyError, FailureRepairPolicyError) as error:
        raise error_type(str(error)) from error
    return {
        "backend": run_config.get("backend"),
        "model": run_config.get("model"),
        "generation_profile": run_config.get("generation_profile"),
        "missing_voice_policy": policy.to_document(),
        "failure_repair_policy": repair_policy.to_document(),
    }


def workspace_missing_voice_policy(workspace, *, error_type=ValueError):
    """Load the normalized missing-voice policy bound to a workspace."""
    config = normalize_workspace_run_config(
        workspace.get("run_config"), error_type=error_type
    )
    return MissingVoicePolicy.from_document(config["missing_voice_policy"])


def workspace_failure_repair_policy(workspace, *, error_type=ValueError):
    """Load the normalized failure-repair policy bound to a workspace."""
    config = normalize_workspace_run_config(
        workspace.get("run_config"), error_type=error_type
    )
    return FailureRepairPolicy.from_document(config["failure_repair_policy"])


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
    explicit_fallback_merge=None,
    known_role_live_fallback=None,
    audio_event_omission=None,
    audio_event_projection_fallback=None,
    reviewed_waveform_publication=None,
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
    if explicit_fallback_merge is not None:
        fingerprint["explicit_fallback_merge"] = explicit_fallback_merge
    if known_role_live_fallback is not None:
        fingerprint["known_role_live_fallback"] = known_role_live_fallback
    if audio_event_omission is not None:
        fingerprint["audio_event_omission"] = audio_event_omission
    if audio_event_projection_fallback is not None:
        fingerprint["audio_event_projection_fallback"] = audio_event_projection_fallback
    if reviewed_waveform_publication is not None:
        fingerprint["reviewed_waveform_publication"] = reviewed_waveform_publication
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


__all__ = [
    "normalize_workspace_run_config",
    "selected_voice_manifest_path",
    "workspace_config_fingerprint",
    "workspace_failure_repair_policy",
    "workspace_missing_voice_policy",
]

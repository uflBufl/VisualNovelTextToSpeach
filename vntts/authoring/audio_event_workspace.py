"""Workspace/state bindings for approved audio-event compositions."""

from __future__ import annotations

import re
from pathlib import Path

from vntts.authoring.audio_event_composition import (
    AudioEventCompositionError,
    load_audio_event_composition,
)
from vntts.authoring.authority import (
    AuthoringAuthorityError,
    canonical_document_sha256,
    capture_authority_file,
)
from vntts.authoring.workspace_foundation import contained_regular_file

AUDIO_EVENT_WORKSPACE_SCHEMA = "vntts.authoring-audio-event-workspace"
AUDIO_EVENT_WORKSPACE_VERSION = 2
AUDIO_EVENT_ITEM_SCHEMA = "vntts.authoring-audio-event-composition-item"
AUDIO_EVENT_ITEM_VERSION = 1
AUDIO_EVENT_PROVIDER = "original-game-audio-event"
AUDIO_EVENT_MODEL = "exact-source-event"
AUDIO_EVENT_PROFILE = "exact-copy-v1"
AUDIO_EVENT_VOICE = "Audio Event"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_WORKSPACE_ID = re.compile(r"resume-[0-9a-f]{24}-[0-9a-f]{16}")


class AudioEventWorkspaceError(RuntimeError):
    """An audio-event workspace or projected state item is invalid."""


def composition_item_ledger(config):
    """Return the exact additive state/manifest provenance for one composition."""
    return {
        "schema": AUDIO_EVENT_ITEM_SCHEMA,
        "schema_version": AUDIO_EVENT_ITEM_VERSION,
        "composition_id": config["composition_id"],
        "composition_sha256": config["composition_sha256"],
        "composition_decision_sha256": config["decision_sha256"],
        "final_audio_sha256": config["final_audio_sha256"],
        "source_workspace_id": config["base_workspace_id"],
        "source_workspace_sha256": config["base_workspace_sha256"],
        "source_state_sha256": config["base_state_sha256"],
        "source_item_sha256": config["base_item_sha256"],
        "source_audio_sha256": config["base_audio_sha256"],
        "speaker_identity_claim": False,
    }


def validate_audio_event_composition_workspace(directory, workspace):
    """Validate the copied composition and its config-addressed workspace ledger."""
    root = Path(directory).expanduser().resolve()
    config = workspace.get("audio_event_composition")
    if config is None:
        return None
    fields = {
        "schema",
        "schema_version",
        "path",
        "decision_path",
        "composition_id",
        "composition_sha256",
        "decision_sha256",
        "final_audio_sha256",
        "queue_id",
        "base_workspace_id",
        "base_workspace_path",
        "base_workspace_sha256",
        "base_state_path",
        "base_state_sha256",
        "base_item_sha256",
        "base_audio_path",
        "base_audio_sha256",
    }
    if (
        not isinstance(config, dict)
        or set(config) != fields
        or config.get("schema") != AUDIO_EVENT_WORKSPACE_SCHEMA
        or config.get("schema_version") != AUDIO_EVENT_WORKSPACE_VERSION
        or config.get("path") != "inputs/audio-event-composition/composition.json"
        or config.get("decision_path")
        != "inputs/audio-event-composition/composition-decision.json"
        or config.get("base_workspace_path") != "inputs/audio-event-base/workspace.json"
        or config.get("base_state_path")
        != "inputs/audio-event-base/generation-state.json"
        or config.get("base_audio_path") != "inputs/audio-event-base/rejected.wav"
        or not isinstance(config.get("queue_id"), str)
        or not config["queue_id"]
        or not _WORKSPACE_ID.fullmatch(str(config.get("base_workspace_id") or ""))
    ):
        raise AudioEventWorkspaceError(
            "Workspace audio-event composition binding is malformed"
        )
    for field in (
        "composition_id",
        "composition_sha256",
        "decision_sha256",
        "final_audio_sha256",
        "base_workspace_sha256",
        "base_state_sha256",
        "base_item_sha256",
        "base_audio_sha256",
    ):
        if not _SHA256.fullmatch(str(config.get(field) or "")):
            raise AudioEventWorkspaceError(f"Workspace audio-event {field} is invalid")
    composition_path = _contained_file(root, config["path"], "composition")
    decision_path = _contained_file(root, config["decision_path"], "decision")
    base_workspace_path = _contained_file(
        root, config["base_workspace_path"], "base workspace"
    )
    base_state_path = _contained_file(root, config["base_state_path"], "base state")
    base_audio_path = _contained_file(root, config["base_audio_path"], "base audio")
    try:
        composition_snapshot = capture_authority_file(
            composition_path, "workspace audio-event composition", root=root
        )
        decision_snapshot = capture_authority_file(
            decision_path, "workspace audio-event decision", root=root
        )
        base_workspace_snapshot = capture_authority_file(
            base_workspace_path, "workspace audio-event base workspace", root=root
        )
        base_state_snapshot = capture_authority_file(
            base_state_path, "workspace audio-event base state", root=root
        )
        base_audio_snapshot = capture_authority_file(
            base_audio_path, "workspace audio-event base audio", root=root
        )
        base_workspace = base_workspace_snapshot.json_document(
            "workspace audio-event base workspace"
        )
        base_state = base_state_snapshot.json_document(
            "workspace audio-event base state"
        )
        composition_document = composition_snapshot.json_document(
            "workspace audio-event composition"
        )
        loaded = load_audio_event_composition(composition_path.parent)
    except (AuthoringAuthorityError, AudioEventCompositionError) as error:
        raise AudioEventWorkspaceError(str(error)) from error
    if (
        loaded.decision != "approved"
        or loaded.composition_id != config["composition_id"]
        or loaded.queue_id != config["queue_id"]
        or loaded.audio_sha256 != config["final_audio_sha256"]
        or composition_snapshot.sha256 != config["composition_sha256"]
        or decision_snapshot.sha256 != config["decision_sha256"]
        or base_workspace_snapshot.sha256 != config["base_workspace_sha256"]
        or base_state_snapshot.sha256 != config["base_state_sha256"]
        or base_audio_snapshot.sha256 != config["base_audio_sha256"]
    ):
        raise AudioEventWorkspaceError(
            "Workspace audio-event composition authority changed"
        )
    base_item = base_state.get("items", {}).get(config["queue_id"])
    if (
        base_workspace.get("schema") != "vntts.authoring-workspace"
        or base_workspace.get("schema_version") != 1
        or base_workspace.get("workspace_id") != config["base_workspace_id"]
        or base_state.get("active") is not None
        or base_state.get("queue_sha256") != composition_document.get("queue_sha256")
        or not isinstance(base_item, dict)
        or canonical_document_sha256(base_item) != config["base_item_sha256"]
        or (base_item.get("status"), base_item.get("review_status"))
        != ("generated", "rejected")
        or base_item.get("file_sha256") != config["base_audio_sha256"]
    ):
        raise AudioEventWorkspaceError("Workspace audio-event base authority changed")
    outcome_merge = workspace.get("outcome_merge")
    if isinstance(outcome_merge, dict):
        source_item = next(
            (
                item
                for item in outcome_merge.get("items", [])
                if isinstance(item, dict) and item.get("queue_id") == config["queue_id"]
            ),
            None,
        )
        if source_item is not None:
            expected = {
                key: value for key, value in source_item.items() if key != "queue_id"
            }
            if base_item.get("outcome_merge") != expected:
                raise AudioEventWorkspaceError(
                    "Workspace audio-event base outcome authority changed"
                )
    return config


def validate_audio_event_composition_state_item(workspace_directory, queue_id, result):
    """Bind one generated state item to its canonical workspace composition."""
    root = Path(workspace_directory).expanduser().resolve()
    try:
        snapshot = capture_authority_file(
            root / "workspace.json", "audio-event workspace", root=root
        )
        workspace = snapshot.json_document("audio-event workspace")
    except AuthoringAuthorityError as error:
        raise AudioEventWorkspaceError(str(error)) from error
    config = validate_audio_event_composition_workspace(root, workspace)
    ledger = result.get("audio_event_composition")
    if config is None or config.get("queue_id") != queue_id:
        raise AudioEventWorkspaceError(
            "Generated audio-event item has no canonical workspace composition"
        )
    if ledger != composition_item_ledger(config):
        raise AudioEventWorkspaceError(
            "Generated audio-event composition ledger changed"
        )
    if (
        result.get("provider") != AUDIO_EVENT_PROVIDER
        or result.get("model") != AUDIO_EVENT_MODEL
        or result.get("generation_profile") != AUDIO_EVENT_PROFILE
        or result.get("voice_character") != AUDIO_EVENT_VOICE
        or result.get("file_sha256") != config["final_audio_sha256"]
        or result.get("prompt_applied") is not False
    ):
        raise AudioEventWorkspaceError(
            "Generated audio-event synthesis identity changed"
        )
    return ledger


def _contained_file(root, value, label):
    return contained_regular_file(
        root,
        value,
        f"workspace audio-event {label}",
        error_type=AudioEventWorkspaceError,
    )


__all__ = [
    "AUDIO_EVENT_ITEM_SCHEMA",
    "AUDIO_EVENT_ITEM_VERSION",
    "AUDIO_EVENT_MODEL",
    "AUDIO_EVENT_PROFILE",
    "AUDIO_EVENT_PROVIDER",
    "AUDIO_EVENT_VOICE",
    "AUDIO_EVENT_WORKSPACE_SCHEMA",
    "AUDIO_EVENT_WORKSPACE_VERSION",
    "AudioEventWorkspaceError",
    "composition_item_ledger",
    "validate_audio_event_composition_state_item",
    "validate_audio_event_composition_workspace",
]

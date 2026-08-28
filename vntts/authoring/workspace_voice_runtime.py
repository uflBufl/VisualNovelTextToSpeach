"""Validated runtime voice projections for immutable authoring workspaces."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
)

from vntts.authoring.failure_reference_binding_records import (
    FailureReferenceBindingError,
    load_failure_reference_binding_document,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
)
from vntts.authoring.workspace_config import selected_voice_manifest_path
from vntts.authoring.workspace_foundation import (
    contained_path,
    read_regular_file,
    safe_relative_path,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


@dataclass(frozen=True)
class FailureReferenceRuntimeBinding:
    directory: Path
    document: dict
    voices: tuple[CharacterVoice, ...]
    queue_voice_overrides: dict[str, str]
    controls: dict[Path, str]


# Preserve the established public workbench pickle and introspection identity.
FailureReferenceRuntimeBinding.__module__ = "vntts.authoring.workbench"


def load_failure_reference_runtime_binding(
    directory,
    workspace,
    *,
    error_type=ValueError,
):
    """Load the exact synthetic voices bound to failed queue items."""
    config = workspace.get("failure_reference_binding")
    if config is None:
        return None
    binding_path = contained_path(
        directory,
        safe_relative_path(
            config["path"], "Failure-reference binding", error_type=error_type
        ),
        "Failure-reference binding",
        error_type=error_type,
    )
    try:
        document = load_failure_reference_binding_document(binding_path.parent)
    except FailureReferenceBindingError as error:
        raise error_type(str(error)) from error
    controls = {binding_path: config["sha256"]}
    voices = []
    for group, control in zip(document["groups"], config["controls"], strict=True):
        reference = contained_path(
            directory,
            safe_relative_path(
                control["path"], "Selected reference", error_type=error_type
            ),
            "Selected reference",
            error_type=error_type,
        )
        _read_bound_bytes(
            reference,
            control["sha256"],
            "Selected reference",
            error_type=error_type,
        )
        controls[reference] = control["sha256"]
        voices.append(
            CharacterVoice(
                character=group["voice_character"],
                speaker=f"failure-reference:{group['group_id']}",
                references=(reference,),
            )
        )
    return FailureReferenceRuntimeBinding(
        binding_path.parent,
        document,
        tuple(voices),
        dict(document["queue_voice_overrides"]),
        controls,
    )


def load_workspace_voice_registry(directory, workspace, *, error_type=ValueError):
    """Build the effective manifest plus failure-reference voice registry."""
    manifest = selected_voice_manifest_path(directory, workspace, error_type=error_type)
    if manifest is None:
        raise error_type("Workspace has no voice manifest snapshot")
    try:
        registry = CharacterVoiceRegistry.from_file(manifest)
    except VoiceManifestError as error:
        raise error_type(str(error)) from error
    runtime_binding = load_failure_reference_runtime_binding(
        directory, workspace, error_type=error_type
    )
    if runtime_binding is None:
        return registry
    try:
        return CharacterVoiceRegistry(
            (*registry.unique_voices(), *runtime_binding.voices)
        )
    except VoiceManifestError as error:
        raise error_type(str(error)) from error


def load_workspace_queue_voice_overrides(
    directory,
    workspace,
    *,
    error_type=ValueError,
):
    """Load exact manifest and failure-reference queue voice overrides."""
    manifest = selected_voice_manifest_path(directory, workspace, error_type=error_type)
    if manifest is None:
        raise error_type("Workspace has no voice manifest snapshot")
    try:
        document, entries = load_voice_manifest(manifest, allow_legacy=False)
        overrides = queue_voice_overrides_from_manifest(document, voices=entries)
    except (SourceReferenceBindingError, VoiceManifestError, OSError) as error:
        raise error_type(
            f"Unable to load carry-forward queue voice bindings: {error}"
        ) from error
    runtime_binding = load_failure_reference_runtime_binding(
        directory, workspace, error_type=error_type
    )
    if runtime_binding is None:
        return overrides
    return {**overrides, **runtime_binding.queue_voice_overrides}


def _read_bound_bytes(path, expected_sha256, label, *, error_type):
    payload = read_regular_file(path, label, error_type=error_type)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise error_type(f"{label} was modified")
    return payload


__all__ = [
    "FailureReferenceRuntimeBinding",
    "load_failure_reference_runtime_binding",
    "load_workspace_queue_voice_overrides",
    "load_workspace_voice_registry",
]

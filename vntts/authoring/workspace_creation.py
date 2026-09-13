"""Workspace creation and carry-forward lifecycle."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_data_path
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
)

import vntts.authoring.legacy_import as legacy_import
from vntts.authoring.audio_event_composition import (
    AudioEventCompositionError,
    load_audio_event_composition,
)
from vntts.authoring.audio_event_workspace import (
    AUDIO_EVENT_MODEL,
    AUDIO_EVENT_PROFILE,
    AUDIO_EVENT_PROVIDER,
    AUDIO_EVENT_VOICE,
    AUDIO_EVENT_WORKSPACE_SCHEMA,
    AUDIO_EVENT_WORKSPACE_VERSION,
    AudioEventWorkspaceError,
    composition_item_ledger,
    validate_audio_event_composition_workspace,
)
from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    NO_PROMPT_SHA256,
    BulkGenerationError,
    inline_pause_matches_failure,
    inspect_generated_wav,
    load_generation_state,
    normalize_short_trailing_ellipsis,
    normalized_failure_record,
    publish_generated_manifest,
    sentence_repair_matches_failure,
    sha256_control_path,
    snapshot_generation_control_files,
)
from vntts.authoring.failure_reference_binding_records import (
    FailureReferenceBindingError,
    load_failure_reference_binding,
    load_failure_reference_binding_document,
)
from vntts.authoring.failure_repair import (
    BOUNDED_SEED_RETRY,
    INLINE_PAUSE_MARKER,
    MAX_BOUNDED_TOTAL_ATTEMPTS,
    OFFLINE_FALLBACK_BACKEND,
    SENTENCE_BOUNDARY_SEGMENTATION,
    FailureRepairPolicy,
    FailureRepairPolicyError,
)
from vntts.authoring.game_pack import FinalGamePackError
from vntts.authoring.generation_lease import (
    process_is_alive,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.missing_voice_policy import (
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.offline_fallback_authority import (
    OfflineFallbackAuthorityError,
    load_offline_fallback_authorities,
)
from vntts.authoring.publication import generation_publication_leases, staged_directory
from vntts.authoring.publication import (
    rename_directory_no_replace as _rename_directory_no_replace,
)
from vntts.authoring.queue_extension import (
    QueueExtensionError,
    workspace_queue_extension,
)
from vntts.authoring.reference_selection import (
    ReferenceSelectionError,
    validate_reference_selection_provenance,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
)
from vntts.authoring.speech_quality import (
    measure_generated_speech_bytes,
)
from vntts.authoring.terminal_conflict_records import is_terminal_review_outcome
from vntts.authoring.workbench_contracts import (
    WORKSPACE_SCHEMA,
    WORKSPACE_VERSION,
    AuthoringWorkbenchError,
    WorkspaceCreationResult,
    _read_bound_bytes,
)
from vntts.authoring.workspace_authority import (
    _legacy_input_digest,
    _load_bound_workspace_queue,
    _load_json,
    _load_json_snapshot,
    _load_workspace,
    _load_workspace_snapshot,
    _require_sha256,
    _required_text,
    _safe_relative,
    _stable_workspace_state,
    _validate_import_history,
    _validate_workspace_carry_forward,
    _validate_workspace_failure_reference_binding,
    _validate_workspace_input_config,
    _validate_workspace_offline_fallback_state,
    _validate_workspace_outcome_merge,
    _within,
    _workspace_failure_repair_policy,
    _workspace_run_config_with_policy,
    _workspace_title,
)
from vntts.authoring.workspace_config import (
    selected_voice_manifest_path,
    workspace_audio_event_spoken_projection_queue_ids,
    workspace_config_fingerprint,
    workspace_missing_voice_policy,
)
from vntts.authoring.workspace_foundation import (
    copy_workspace_tree_snapshot,
    read_regular_file,
)
from vntts.authoring.workspace_voice_runtime import (
    load_failure_reference_runtime_binding,
    load_workspace_queue_voice_overrides,
    load_workspace_voice_registry,
)
from vntts.voices import CharacterVoiceRegistry, synthesis_character_for_line

_terminal_review_outcome = is_terminal_review_outcome
_workspace_config_fingerprint = workspace_config_fingerprint
_IMPORT_ID_PATTERN = re.compile(r"legacy-[0-9a-f]{24}")


def default_workspaces_root():
    return (
        user_data_path("VisualNovelTextToSpeech", appauthor=False)
        / "authoring"
        / "workspaces"
    )


def create_resume_workspace(
    import_directory,
    workspaces_root=None,
    *,
    story_index=None,
    voice_manifest=None,
    narrator_character=None,
    backend=None,
    model=None,
    generation_profile=None,
    missing_voice_policy=None,
    failure_repair_policy=None,
    carry_forward_from=None,
    carry_forward_characters=None,
    offline_fallback_authorities=None,
    generation_queue=None,
    audio_event_spoken_projection_queue_ids=None,
):
    """Copy one immutable import into a separate mutable resume workspace."""
    source = Path(import_directory).expanduser().resolve()
    import_path = source / "import.json"
    manifest, import_sha256, import_payload = _load_json_snapshot(
        import_path, "legacy import"
    )
    if (
        manifest.get("schema") != legacy_import.IMPORT_SCHEMA
        or manifest.get("schema_version")
        not in legacy_import.SUPPORTED_IMPORT_SCHEMA_VERSIONS
    ):
        raise AuthoringWorkbenchError("Only validated VNTTS legacy imports can resume")
    _validate_import_history(manifest)
    if manifest.get("source", {}).get("kind") != (
        "reverse1999-extractor-pregeneration-job"
    ):
        raise AuthoringWorkbenchError(
            "Resume workspaces require a job-backed legacy import"
        )
    import_id = _required_text(manifest.get("import_id"), "Legacy import ID")
    if not _IMPORT_ID_PATTERN.fullmatch(import_id) or source.name != import_id:
        raise AuthoringWorkbenchError(
            "Legacy import ID must be canonical and match its source directory"
        )
    source_fingerprint = _required_text(
        manifest.get("source", {}).get("source_fingerprint"),
        "Legacy source fingerprint",
    )
    artifacts = _validated_import_inventory(source, manifest)
    queue_artifact = next(
        (item for item in artifacts if item["path"] == "queue.jsonl"), None
    )
    state_artifact = next(
        (
            item
            for item in artifacts
            if item["path"] == "generated-audio/generation-state.json"
        ),
        None,
    )
    if queue_artifact is None or state_artifact is None:
        raise AuthoringWorkbenchError(
            "Resume requires an imported queue and authoritative generation state"
        )

    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    copied = [
        item
        for item in artifacts
        if item["path"] == "queue.jsonl" or item["path"].startswith("generated-audio/")
    ]
    with staged_directory(root, prefix=".resume-staging-") as staging:
        import_snapshot = staging / "provenance" / "import.json"
        import_snapshot.parent.mkdir(parents=True)
        import_snapshot.write_bytes(import_payload)
        if sha256_file(import_snapshot) != import_sha256:
            raise AuthoringWorkbenchError("Unable to preserve exact import manifest")
        for item in copied:
            relative = _safe_relative(item["path"], "Imported artifact path")
            source_path = _within(source, relative, "Imported artifact")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
            if sha256_file(target) != item["sha256"]:
                raise AuthoringWorkbenchError(
                    f"Imported artifact changed during workspace copy: {relative}"
                )
        seed_state = _preserve_seed_generation_state(staging, state_artifact)
        queue_extension = None
        selected_queue_source = None
        if generation_queue is not None:
            selected_queue_source = Path(generation_queue).expanduser().resolve()
            queue_extension = _install_extended_generation_queue(
                staging,
                selected_queue_source,
                imported_queue_sha256=queue_artifact["sha256"],
            )
        narrator = _required_text(
            narrator_character or _legacy_narrator(manifest), "Narrator character"
        )
        try:
            queue_snapshot = VoiceGenerationQueue.load(staging / "queue.jsonl")
        except VoiceGenerationQueueError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        story_config, voice_config, selected_sources = _copy_input_snapshots(
            staging,
            story_index=story_index,
            voice_manifest=voice_manifest,
            import_manifest=manifest,
            queue=queue_snapshot,
        )
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
        if projection_ids and not repair_policy.is_empty:
            raise AuthoringWorkbenchError(
                "Audio-event spoken projection cannot mix with failure repair"
            )
        queue_by_id = {item.queue_id: item for item in queue_snapshot.items}
        for queue_id in projection_ids:
            item = queue_by_id.get(queue_id)
            if item is None or item.action != "generate":
                raise AuthoringWorkbenchError(
                    f"Audio-event spoken projection item is unavailable: {queue_id!r}"
                )
            try:
                plan = audio_event_plan_for_record(item)
            except ValueError as error:
                raise AuthoringWorkbenchError(str(error)) from error
            if (
                not isinstance(plan, dict)
                or not plan.get("requires_composition")
                or not plan.get("events")
                or not plan.get("spoken_text")
            ):
                raise AuthoringWorkbenchError(
                    f"Audio-event spoken projection requires mixed speech: {queue_id!r}"
                )
        if projection_ids:
            try:
                seed_projection_state = load_generation_state(
                    staging / "generated-audio/generation-state.json",
                    staging / "queue.jsonl",
                )
            except BulkGenerationError as error:
                raise AuthoringWorkbenchError(str(error)) from error
            already_rendered = sorted(
                set(projection_ids) & set(seed_projection_state["items"])
            )
            if already_rendered:
                raise AuthoringWorkbenchError(
                    "Audio-event spoken projection requires items without seed state: "
                    + ", ".join(already_rendered)
                )
        run_config = {
            "backend": _optional_text(backend),
            "model": _optional_text(model),
            "generation_profile": _optional_text(generation_profile),
            "missing_voice_policy": policy.to_document(),
            "failure_repair_policy": repair_policy.to_document(),
        }
        if projection_ids:
            run_config["audio_event_spoken_projection_queue_ids"] = list(projection_ids)
        failure_reference_binding, binding_sources = (
            _copy_carry_forward_failure_reference_binding(
                staging,
                carry_forward_from,
                repair_policy.queue_ids,
            )
        )
        selected_sources = (*selected_sources, *binding_sources)
        carry_forward, authority_sources = _carry_forward_review_outcomes(
            carry_forward_from,
            staging,
            queue_snapshot,
            import_id=import_id,
            voice_config=voice_config,
            run_config=run_config,
            characters=carry_forward_characters,
            failure_repair_policy=repair_policy,
            failure_reference_binding=failure_reference_binding,
            offline_fallback_authorities=offline_fallback_authorities,
        )
        selected_sources = (*selected_sources, *authority_sources)
        if selected_queue_source is not None:
            selected_sources = (
                *selected_sources,
                (
                    selected_queue_source,
                    queue_extension["queue_sha256"],
                    "generation queue",
                ),
            )
        config_fingerprint = _workspace_config_fingerprint(
            import_id,
            story_config,
            voice_config,
            narrator,
            run_config,
            carry_forward,
            failure_reference_binding=failure_reference_binding,
            queue_extension=queue_extension,
        )
        workspace_id = (
            f"resume-{import_id.removeprefix('legacy-')}-{config_fingerprint[:16]}"
        )
        destination = _within(root, Path(workspace_id), "Workspace destination")
        for existing in root.iterdir():
            if (
                existing.name.casefold() == workspace_id.casefold()
                and existing != destination
            ):
                raise AuthoringWorkbenchError(
                    f"Workspace name collides by case with {existing.name!r}"
                )
        workspace = {
            "schema": WORKSPACE_SCHEMA,
            "schema_version": WORKSPACE_VERSION,
            "workspace_id": workspace_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "title": _workspace_title(manifest, import_id),
            "source": {
                "kind": "legacy-import",
                "import_id": import_id,
                "import_sha256": import_sha256,
                "source_fingerprint": source_fingerprint,
                "snapshot": "provenance/import.json",
            },
            "queue": "queue.jsonl",
            "output": "generated-audio",
            "story_index": story_config,
            "voice_manifest": voice_config,
            "legacy_external_inputs": manifest.get("external_inputs", []),
            "narrator_character": narrator,
            "run_config": run_config,
            "seed_generation_state": seed_state,
            "carry_forward": carry_forward,
            "failure_reference_binding": failure_reference_binding,
            "queue_extension": queue_extension,
            "config_fingerprint": config_fingerprint,
            "seed_inventory": [
                {"path": "provenance/import.json", "sha256": import_sha256},
                *({"path": item["path"], "sha256": item["sha256"]} for item in copied),
            ],
        }
        atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
        _validate_workspace_failure_reference_binding(staging, workspace)
        _verify_import_sources(source, copied, import_path, import_sha256)
        _verify_selected_sources(selected_sources)
        if destination.exists():
            _validate_existing_workspace(
                destination,
                import_id=import_id,
                import_sha256=import_sha256,
                source_fingerprint=source_fingerprint,
            )
            return WorkspaceCreationResult(destination, False)
        try:
            _rename_directory_no_replace(staging, destination)
        except (OSError, FinalGamePackError) as error:
            if destination.exists():
                _validate_existing_workspace(
                    destination,
                    import_id=import_id,
                    import_sha256=import_sha256,
                    source_fingerprint=source_fingerprint,
                )
                return WorkspaceCreationResult(destination, False)
            raise AuthoringWorkbenchError(
                f"Unable to publish authoring workspace: {error}"
            ) from error
    return WorkspaceCreationResult(destination, True)


def create_failure_reference_workspace(
    base_workspace,
    binding_directory,
    workspaces_root=None,
):
    """Create a successor that preserves state and adds one exact-ID overlay."""
    base_directory, base_document, base_workspace_sha256 = _load_workspace_snapshot(
        base_workspace, "failure-reference base"
    )
    if base_document.get("failure_reference_binding") is not None:
        raise AuthoringWorkbenchError(
            "Failure-reference successor already has a selected-reference overlay"
        )
    queue, state, _state_payload, state_sha256 = _stable_workspace_state(
        base_directory, base_document, "failure-reference base"
    )
    if state.get("active") is not None:
        raise AuthoringWorkbenchError(
            "Failure-reference successor cannot copy an active generation attempt"
        )
    output = base_directory / "generated-audio"
    if (output / ".generation-lease.json").exists():
        raise AuthoringWorkbenchError(
            "Failure-reference successor cannot copy a leased workspace"
        )
    try:
        binding = load_failure_reference_binding(binding_directory)
        binding_document = load_failure_reference_binding_document(binding.directory)
    except FailureReferenceBindingError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    authority = binding_document["source_authority"]
    queue_sha256 = sha256_file(base_directory / "queue.jsonl")
    voice = base_document.get("voice_manifest")
    if (
        queue_sha256 != authority["queue_sha256"]
        or not isinstance(voice, dict)
        or voice.get("sha256") != authority["voice_manifest_sha256"]
    ):
        raise AuthoringWorkbenchError(
            "Failure-reference binding belongs to different queue or voice controls"
        )
    source_workspace_id = authority["workspace_id"]
    if (
        source_workspace_id.split("-")[1:2]
        != base_document["workspace_id"].split("-")[1:2]
    ):
        raise AuthoringWorkbenchError(
            "Failure-reference binding belongs to a different immutable import"
        )
    queue_ids = {item.queue_id for item in queue.items}
    selected_ids = set()
    for group in binding_document["groups"]:
        for case in group["cases"]:
            queue_id = case["queue_id"]
            result = state["items"].get(queue_id)
            if queue_id not in queue_ids or not isinstance(result, dict):
                raise AuthoringWorkbenchError(
                    f"Failure-reference base item is missing: {queue_id!r}"
                )
            if canonical_document_sha256(result) != case["failure_sha256"]:
                raise AuthoringWorkbenchError(
                    f"Failure-reference base authority is stale for {queue_id!r}"
                )
            if result.get("status") != "failed":
                raise AuthoringWorkbenchError(
                    f"Failure-reference base item is no longer failed: {queue_id!r}"
                )
            selected_ids.add(queue_id)
    if selected_ids != set(binding_document["queue_voice_overrides"]):
        raise AuthoringWorkbenchError(
            "Failure-reference binding selection inventory is inconsistent"
        )

    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    base_snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (base_directory / "generated-audio/generation-state.json", state_sha256),
        (base_directory / "queue.jsonl", queue_sha256),
    ]
    binding_snapshots = []
    with staged_directory(root, prefix=".reference-binding-staging-") as staging:
        for tree_name in ("provenance", "inputs", "generated-audio"):
            _copy_workspace_tree_snapshot(
                base_directory / tree_name,
                staging / tree_name,
                base_snapshots,
            )
        (staging / "queue.jsonl").write_bytes(
            _read_file_bytes(
                base_directory / "queue.jsonl", "failure-reference base queue"
            )
        )
        target_binding = staging / "inputs" / "failure-reference-binding"
        _copy_workspace_tree_snapshot(
            binding.directory,
            target_binding,
            binding_snapshots,
        )
        binding_path = target_binding / "binding.json"
        controls = []
        for group in binding_document["groups"]:
            relative = _safe_relative(group["reference"], "Selected reference")
            _within(target_binding, relative, "Selected reference")
            controls.append(
                {
                    "path": (
                        Path("inputs") / "failure-reference-binding" / relative
                    ).as_posix(),
                    "sha256": group["reference_sha256"],
                }
            )
        binding_config = {
            "path": "inputs/failure-reference-binding/binding.json",
            "sha256": sha256_file(binding_path),
            "binding_id": binding.binding_id,
            "controls": controls,
            "base_workspace_id": base_document["workspace_id"],
            "base_workspace_sha256": base_workspace_sha256,
            "base_state_sha256": state_sha256,
        }
        config_fingerprint = _workspace_config_fingerprint(
            base_document["source"]["import_id"],
            base_document.get("story_index"),
            base_document.get("voice_manifest"),
            base_document["narrator_character"],
            base_document["run_config"],
            base_document.get("carry_forward"),
            base_document.get("outcome_merge"),
            binding_config,
            base_document.get("terminal_conflict_merge"),
            base_document.get("config_rebase"),
            base_document.get("audio_event_composition"),
            base_document.get("explicit_fallback_merge"),
            base_document.get("known_role_live_fallback"),
            base_document.get("audio_event_omission"),
            base_document.get("audio_event_projection_fallback"),
            base_document.get("reviewed_waveform_publication"),
            base_document.get("reviewed_rejection_live_fallback"),
            queue_extension=base_document.get("queue_extension"),
        )
        workspace_id = (
            f"resume-{base_document['source']['import_id'].removeprefix('legacy-')}-"
            f"{config_fingerprint[:16]}"
        )
        destination = _within(root, Path(workspace_id), "Workspace destination")
        workspace = copy.deepcopy(base_document)
        workspace.update(
            {
                "workspace_id": workspace_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "failure_reference_binding": binding_config,
                "config_fingerprint": config_fingerprint,
            }
        )
        atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
        import_snapshot = _load_json(
            staging / "provenance/import.json", "failure-reference import snapshot"
        )
        _validate_workspace_input_config(staging, workspace, import_snapshot)
        _validate_workspace_failure_reference_binding(staging, workspace)
        _validate_workspace_carry_forward(staging, workspace)
        _validate_workspace_offline_fallback_state(staging, workspace)
        _validate_workspace_outcome_merge(staging, workspace)
        for path, digest in (*base_snapshots, *binding_snapshots):
            if not path.is_file() or sha256_file(path) != digest:
                raise AuthoringWorkbenchError(
                    "Failure-reference source changed before workspace publication"
                )
        if destination.exists():
            _directory, existing = _load_workspace(destination)
            if existing.get("failure_reference_binding") != binding_config:
                raise AuthoringWorkbenchError(
                    "Failure-reference destination conflicts with another binding"
                )
            return WorkspaceCreationResult(destination, False)
        try:
            _rename_directory_no_replace(staging, destination)
        except (OSError, FinalGamePackError) as error:
            if destination.exists():
                _directory, existing = _load_workspace(destination)
                if existing.get("failure_reference_binding") == binding_config:
                    return WorkspaceCreationResult(destination, False)
            raise AuthoringWorkbenchError(
                f"Unable to publish failure-reference workspace: {error}"
            ) from error
    return WorkspaceCreationResult(destination, True)


def create_audio_event_composition_workspace(
    base_workspace,
    composition_directory,
    workspaces_root=None,
):
    """Create a successor with one approved exact event WAV pending review."""
    base_directory, base_document, base_workspace_sha256 = _load_workspace_snapshot(
        base_workspace, "audio-event base"
    )
    if base_document.get("audio_event_composition") is not None:
        raise AuthoringWorkbenchError(
            "Audio-event successor already contains a composition"
        )
    queue, state, state_payload, state_sha256 = _stable_workspace_state(
        base_directory, base_document, "audio-event base"
    )
    try:
        composition = load_audio_event_composition(composition_directory)
    except AudioEventCompositionError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    if composition.decision != "approved":
        raise AuthoringWorkbenchError(
            "Audio-event workspace requires an approved composition"
        )
    composition_root = composition.directory
    composition_document, composition_sha256, _composition_payload = (
        _load_json_snapshot(
            composition_root / "composition.json", "audio-event composition"
        )
    )
    decision_document, decision_sha256, _decision_payload = _load_json_snapshot(
        composition_root / "composition-decision.json",
        "audio-event composition decision",
    )
    queue_sha256 = sha256_file(base_directory / "queue.jsonl")
    queue_by_id = {item.queue_id: item for item in queue.items}
    queue_item = queue_by_id.get(composition.queue_id)
    previous = state["items"].get(composition.queue_id)
    if (
        composition_document.get("queue_sha256") != queue_sha256
        or queue_item is None
        or composition_document.get("line_id") != queue_item.line_id
        or composition_document.get("text_sha256") != queue_item.text_sha256
        or composition_document.get("text") != queue_item.text
    ):
        raise AuthoringWorkbenchError(
            "Audio-event composition belongs to a different queue item"
        )
    if not isinstance(previous, dict) or (
        previous.get("status"),
        previous.get("review_status"),
    ) != ("generated", "rejected"):
        raise AuthoringWorkbenchError(
            "Audio-event successor can replace only an explicitly rejected rendition"
        )
    previous_relative = _safe_relative(
        previous.get("path"), "Rejected audio-event rendition"
    )
    previous_audio = _within(
        base_directory / "generated-audio",
        previous_relative,
        "Rejected audio-event rendition",
    )
    previous_audio_sha256 = _require_sha256(
        previous.get("file_sha256"), "Rejected audio-event rendition SHA-256"
    )
    if (
        not previous_audio.is_file()
        or sha256_file(previous_audio) != previous_audio_sha256
    ):
        raise AuthoringWorkbenchError(
            "Rejected audio-event rendition changed before successor publication"
        )
    base_workspace_payload = _read_file_bytes(
        base_directory / "workspace.json", "audio-event base workspace"
    )
    previous_audio_payload = _read_file_bytes(
        previous_audio, "rejected audio-event rendition"
    )

    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    base_snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (base_directory / "generated-audio/generation-state.json", state_sha256),
        (base_directory / "queue.jsonl", queue_sha256),
        (previous_audio, previous_audio_sha256),
    ]
    composition_snapshots = []
    with staged_directory(root, prefix=".audio-event-staging-") as staging:
        for tree_name in ("provenance", "inputs", "generated-audio"):
            _copy_workspace_tree_snapshot(
                base_directory / tree_name,
                staging / tree_name,
                base_snapshots,
            )
        (staging / "queue.jsonl").write_bytes(
            _read_file_bytes(base_directory / "queue.jsonl", "audio-event base queue")
        )
        copied_composition = staging / "inputs" / "audio-event-composition"
        _copy_workspace_tree_snapshot(
            composition_root,
            copied_composition,
            composition_snapshots,
        )
        copied_base = staging / "inputs" / "audio-event-base"
        copied_base.mkdir(parents=True)
        (copied_base / "workspace.json").write_bytes(base_workspace_payload)
        (copied_base / "generation-state.json").write_bytes(state_payload)
        (copied_base / "rejected.wav").write_bytes(previous_audio_payload)
        composition_config = {
            "schema": AUDIO_EVENT_WORKSPACE_SCHEMA,
            "schema_version": AUDIO_EVENT_WORKSPACE_VERSION,
            "path": "inputs/audio-event-composition/composition.json",
            "decision_path": (
                "inputs/audio-event-composition/composition-decision.json"
            ),
            "composition_id": composition.composition_id,
            "composition_sha256": composition_sha256,
            "decision_sha256": decision_sha256,
            "final_audio_sha256": composition.audio_sha256,
            "queue_id": composition.queue_id,
            "base_workspace_id": base_document["workspace_id"],
            "base_workspace_path": "inputs/audio-event-base/workspace.json",
            "base_workspace_sha256": base_workspace_sha256,
            "base_state_path": "inputs/audio-event-base/generation-state.json",
            "base_state_sha256": state_sha256,
            "base_item_sha256": canonical_document_sha256(previous),
            "base_audio_path": "inputs/audio-event-base/rejected.wav",
            "base_audio_sha256": previous_audio_sha256,
        }
        config_fingerprint = _workspace_config_fingerprint(
            base_document["source"]["import_id"],
            base_document.get("story_index"),
            base_document.get("voice_manifest"),
            base_document["narrator_character"],
            base_document["run_config"],
            base_document.get("carry_forward"),
            base_document.get("outcome_merge"),
            base_document.get("failure_reference_binding"),
            base_document.get("terminal_conflict_merge"),
            base_document.get("config_rebase"),
            composition_config,
            base_document.get("explicit_fallback_merge"),
            base_document.get("known_role_live_fallback"),
            base_document.get("audio_event_omission"),
            base_document.get("audio_event_projection_fallback"),
            base_document.get("reviewed_waveform_publication"),
            base_document.get("reviewed_rejection_live_fallback"),
            queue_extension=base_document.get("queue_extension"),
        )
        workspace_id = (
            f"resume-{base_document['source']['import_id'].removeprefix('legacy-')}-"
            f"{config_fingerprint[:16]}"
        )
        destination = _within(root, Path(workspace_id), "Workspace destination")
        workspace = copy.deepcopy(base_document)
        workspace.update(
            {
                "workspace_id": workspace_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "audio_event_composition": composition_config,
                "config_fingerprint": config_fingerprint,
            }
        )
        atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)

        output = staging / "generated-audio"
        obsolete_audio = _within(
            output, previous_relative, "Replaced audio-event rendition"
        )
        if obsolete_audio.is_file():
            obsolete_audio.unlink()
        target_relative = Path("audio/audio-events") / (
            f"{composition.composition_id[:24]}.wav"
        )
        target_audio = _within(output, target_relative, "Composed audio-event WAV")
        target_audio.parent.mkdir(parents=True, exist_ok=True)
        target_audio.write_bytes(composition.audio.read_bytes())
        if sha256_file(target_audio) != composition.audio_sha256:
            raise AuthoringWorkbenchError(
                "Audio-event composition changed while copied into its successor"
            )
        ledger = composition_item_ledger(composition_config)
        attempts = int(previous.get("attempts", 0))
        attempts_by_provider = copy.deepcopy(previous.get("attempts_by_provider"))
        if attempts_by_provider is None:
            attempts_by_provider = (
                {previous["provider"]: attempts}
                if attempts and isinstance(previous.get("provider"), str)
                else {}
            )
        try:
            quality = asdict(
                inspect_generated_wav(target_audio, allow_short_audio_event=True)
            )
            speech_quality = asdict(
                measure_generated_speech_bytes(target_audio.read_bytes())
            )
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        target_state = copy.deepcopy(state)
        target_item = {
            "status": "generated",
            "review_status": "pending_review",
            "attempts": attempts,
            "attempts_by_provider": attempts_by_provider,
            "path": target_relative.as_posix(),
            "line_id": queue_item.line_id,
            "text_sha256": queue_item.text_sha256,
            "file_sha256": composition.audio_sha256,
            "provider": AUDIO_EVENT_PROVIDER,
            "model": AUDIO_EVENT_MODEL,
            "prompt_sha256": NO_PROMPT_SHA256,
            "prompt_applied": False,
            "queue_annotations_sha256": canonical_document_sha256(
                queue_item.document.get("prompt_adapters") or {}
            ),
            "synthesis_text_sha256": queue_item.text_sha256,
            "text_transform": "audio-event-composition-v1",
            "synthesis_provenance_sha256": canonical_document_sha256(ledger),
            "seed": 0,
            "generation_profile": AUDIO_EVENT_PROFILE,
            "speaker": queue_item.speaker,
            "voice_character": AUDIO_EVENT_VOICE,
            "quality": quality,
            "speech_quality": speech_quality,
            "audio_event_composition": ledger,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        target_state["items"][composition.queue_id] = target_item
        target_state["active"] = None
        target_state_path = output / "generation-state.json"
        atomic_write_json(target_state_path, target_state, sort_keys=True)
        write_generated_manifest_from_state(
            target_state,
            output,
            output / "manifest.json",
        )
        try:
            validate_audio_event_composition_workspace(staging, workspace)
            load_generation_state(target_state_path, staging / "queue.jsonl")
        except (AudioEventWorkspaceError, BulkGenerationError) as error:
            raise AuthoringWorkbenchError(str(error)) from error

        try:
            with generation_publication_leases(
                ((base_directory / "generated-audio", queue_sha256),),
                process_checker=process_is_alive,
            ) as held_leases:
                if any((base_directory / "generated-audio").rglob("*.partial.wav")):
                    raise AuthoringWorkbenchError(
                        "Audio-event base became active before publication"
                    )
                for path, digest in (*base_snapshots, *composition_snapshots):
                    if not path.is_file() or sha256_file(path) != digest:
                        raise AuthoringWorkbenchError(
                            "Audio-event source changed before workspace publication"
                        )
                for lease in held_leases:
                    lease.assert_owned()
                if destination.exists():
                    _directory, existing = _load_workspace(destination)
                    if existing.get("audio_event_composition") != composition_config:
                        raise AuthoringWorkbenchError(
                            "Audio-event destination conflicts with another composition"
                        )
                    return WorkspaceCreationResult(destination, False)
                try:
                    _rename_directory_no_replace(staging, destination)
                except (OSError, FinalGamePackError) as error:
                    if destination.exists():
                        _directory, existing = _load_workspace(destination)
                        if (
                            existing.get("audio_event_composition")
                            == composition_config
                        ):
                            for lease in held_leases:
                                lease.mark_committed()
                            return WorkspaceCreationResult(destination, False)
                    raise AuthoringWorkbenchError(
                        f"Unable to publish audio-event workspace: {error}"
                    ) from error
                for lease in held_leases:
                    lease.mark_committed()
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
    return WorkspaceCreationResult(destination, True)


def _failure_reference_runtime_binding(directory, workspace):
    return load_failure_reference_runtime_binding(
        directory,
        workspace,
        error_type=AuthoringWorkbenchError,
    )


def _preserve_seed_generation_state(staging, state_artifact):
    source = staging / "generated-audio" / "generation-state.json"
    expected = _require_sha256(
        state_artifact.get("sha256"), "Imported generation state SHA-256"
    )
    payload = _read_bound_bytes(source, expected, "Imported generation state")
    relative = Path("provenance/seed-generation-state.json")
    target = staging / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    if sha256_file(target) != expected:
        raise AuthoringWorkbenchError("Unable to preserve seed generation state")
    return {"path": relative.as_posix(), "sha256": expected}


def _install_extended_generation_queue(
    staging, selected_queue, *, imported_queue_sha256
):
    base_queue = staging / "queue.jsonl"
    if sha256_file(base_queue) != _require_sha256(
        imported_queue_sha256, "Imported queue SHA-256"
    ):
        raise AuthoringWorkbenchError("Imported queue changed before extension")
    try:
        config = workspace_queue_extension(selected_queue, base_queue=base_queue)
    except QueueExtensionError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    payload, digest = _read_source_bytes(selected_queue, "generation queue")
    if digest != config["queue_sha256"]:
        raise AuthoringWorkbenchError(
            "Selected generation queue changed while it was validated"
        )
    snapshot = staging / config["queue_path"]
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(payload)
    base_snapshot = staging / config["base_queue_path"]
    base_snapshot.parent.mkdir(parents=True, exist_ok=True)
    base_snapshot.write_bytes(base_queue.read_bytes())
    base_queue.write_bytes(payload)

    state_path = staging / "generated-audio/generation-state.json"
    state = _load_json(state_path, "imported generation state")
    if state.get("active") is not None:
        raise AuthoringWorkbenchError("Queue extension source has an active attempt")
    state["queue_sha256"] = digest
    atomic_write_json(state_path, state, sort_keys=True)
    try:
        load_generation_state(state_path, base_queue)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    return config


@dataclass(frozen=True)
class _CarryForwardSelection:
    repair_policy: FailureRepairPolicy
    failed_queue_ids: tuple[str, ...]
    sentence_queue_ids: frozenset[str]
    bounded_queue_ids: frozenset[str]
    offline_queue_ids: frozenset[str]
    inline_pause_queue_ids: frozenset[str]
    characters: tuple[str, ...]


@dataclass(frozen=True)
class _CarryForwardSource:
    directory: Path
    document: dict
    output: Path
    run_config: dict
    state: dict
    state_path: Path
    state_sha256: str


def _validate_carry_forward_source_config(source_document, run_config, selection):
    source_run_config = source_document.get("run_config")
    source_run_config_normalized = _workspace_run_config_with_policy(source_run_config)
    target_run_config_normalized = _workspace_run_config_with_policy(run_config)
    if (
        source_run_config_normalized != target_run_config_normalized
        and not selection.failed_queue_ids
    ):
        raise AuthoringWorkbenchError(
            "Carry-forward source and target model configuration differs"
        )
    same_backend = (
        selection.sentence_queue_ids
        | selection.bounded_queue_ids
        | selection.inline_pause_queue_ids
    )
    if same_backend and selection.offline_queue_ids:
        raise AuthoringWorkbenchError(
            "One carry-forward workspace cannot mix same-backend failure repair "
            "with cross-backend offline fallback"
        )
    source_base_config = dict(source_run_config_normalized)
    source_base_config["failure_repair_policy"] = FailureRepairPolicy().to_document()
    target_base_config = dict(target_run_config_normalized)
    target_base_config["failure_repair_policy"] = FailureRepairPolicy().to_document()
    if same_backend and source_base_config != target_base_config:
        raise AuthoringWorkbenchError(
            "Same-backend repair requires the exact source backend, model, profile "
            "and missing-voice policy"
        )
    if selection.offline_queue_ids and (
        run_config.get("backend") != "pocket-tts"
        or source_run_config_normalized.get("backend") == run_config.get("backend")
        or run_config.get("model") not in {None, "pocket-tts"}
        or run_config.get("generation_profile") not in {None, "default"}
    ):
        raise AuthoringWorkbenchError(
            "Offline fallback requires a different source backend and the exact "
            "Pocket TTS default model/profile"
        )
    return source_run_config_normalized


def _select_carry_forward_outcomes(
    source_workspace, characters, failure_repair_policy, offline_fallback_authorities
):
    repair_policy = failure_repair_policy
    failed_queue_ids = repair_policy.queue_ids
    sentence_queue_ids = frozenset(repair_policy.sentence_segment_queue_ids)
    bounded_queue_ids = frozenset(repair_policy.bounded_seed_retry_queue_ids)
    offline_queue_ids = frozenset(repair_policy.offline_fallback_queue_ids)
    inline_pause_queue_ids = frozenset(repair_policy.inline_pause_queue_ids)
    unsupported = (
        set(failed_queue_ids)
        - sentence_queue_ids
        - bounded_queue_ids
        - offline_queue_ids
        - inline_pause_queue_ids
    )
    if unsupported:
        raise AuthoringWorkbenchError(
            "Carry-forward currently supports only bounded seed, sentence "
            "segmentation, inline pause and offline fallback failures"
        )
    if source_workspace is None:
        if characters is not None or offline_queue_ids or offline_fallback_authorities:
            raise AuthoringWorkbenchError(
                "Carry-forward outcomes require a source workspace"
            )
        return None
    if characters is None and not failed_queue_ids:
        raise AuthoringWorkbenchError(
            "Carry-forward requires characters or exact repair failures"
        )
    selected = (
        ()
        if characters is None
        else tuple(
            sorted(
                {
                    _required_text(value, "Carry-forward character")
                    for value in characters
                }
            )
        )
    )
    if "Narrator" in selected:
        raise AuthoringWorkbenchError(
            "Carry-forward characters must be explicit and exclude Narrator"
        )
    return _CarryForwardSelection(
        repair_policy,
        failed_queue_ids,
        sentence_queue_ids,
        bounded_queue_ids,
        offline_queue_ids,
        inline_pause_queue_ids,
        selected,
    )


def _load_carry_forward_source(
    source_workspace,
    staging,
    target_queue,
    *,
    import_id,
    run_config,
    failure_reference_binding,
    selection,
):
    source_directory, source_document = _load_workspace(source_workspace)
    if (
        failure_reference_binding is not None
        and source_document.get("failure_reference_binding")
        != failure_reference_binding
    ):
        raise AuthoringWorkbenchError(
            "Carry-forward failure-reference binding differs from its source"
        )
    if source_document["source"].get("import_id") != import_id:
        raise AuthoringWorkbenchError(
            "Carry-forward source and target must share one immutable import"
        )
    source_queue = _load_bound_workspace_queue(source_directory, source_document)
    target_queue_path = staging / "queue.jsonl"
    source_queue_path = source_directory / "queue.jsonl"
    source_queue_sha256 = sha256_file(source_queue_path)
    if (
        source_queue_sha256 != sha256_file(target_queue_path)
        or source_queue.metadata != target_queue.metadata
        or [item.document for item in source_queue.items]
        != [item.document for item in target_queue.items]
    ):
        raise AuthoringWorkbenchError(
            "Carry-forward source and target queues are not byte-identical"
        )
    source_run_config_normalized = _validate_carry_forward_source_config(
        source_document, run_config, selection
    )
    source_output = source_directory / "generated-audio"
    source_state_path = source_output / "generation-state.json"
    source_state_payload = _read_file_bytes(
        source_state_path, "source generation state"
    )
    source_state_sha256 = hashlib.sha256(source_state_payload).hexdigest()
    try:
        state = json.loads(source_state_payload.decode("utf-8"))
        validated_state = load_generation_state(source_state_path, source_queue_path)
    except (UnicodeDecodeError, json.JSONDecodeError, BulkGenerationError) as error:
        raise AuthoringWorkbenchError(
            f"Carry-forward source state is invalid: {error}"
        ) from error
    if (
        state != validated_state
        or sha256_file(source_state_path) != source_state_sha256
    ):
        raise AuthoringWorkbenchError(
            "Carry-forward source state changed while it was loaded"
        )
    if state.get("active") is not None:
        raise AuthoringWorkbenchError(
            "Carry-forward source has an active generation attempt"
        )
    return _CarryForwardSource(
        source_directory,
        source_document,
        source_output,
        source_run_config_normalized,
        state,
        source_state_path,
        source_state_sha256,
    )


def _stage_offline_fallback_authorities(staging, source, selection, authorities):
    try:
        loaded = load_offline_fallback_authorities(
            authorities,
            source.state.get("items", {}),
            selection.offline_queue_ids,
        )
    except OfflineFallbackAuthorityError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    authority_by_queue_id = {
        queue_id: authority for authority in loaded for queue_id in authority.queue_ids
    }
    records = []
    sources = []
    for authority in loaded:
        relative = (
            Path("provenance/offline-fallback-authorities")
            / f"{authority.authority_id}.json"
        )
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(authority.payload)
        if sha256_file(target) != authority.source_sha256:
            raise AuthoringWorkbenchError(
                "Unable to preserve offline fallback authority"
            )
        records.append(authority.snapshot_record(relative.as_posix()))
        sources.append(
            (authority.source, authority.source_sha256, "offline fallback authority")
        )
    return loaded, authority_by_queue_id, records, tuple(sources)


def _load_carry_forward_target(
    staging, target_queue_path, voice_config, failure_reference_binding, source
):
    target_state_path = staging / "generated-audio" / "generation-state.json"
    try:
        target_state = load_generation_state(target_state_path, target_queue_path)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    if target_state.get("active") is not None:
        raise AuthoringWorkbenchError(
            "Carry-forward target seed has an active generation attempt"
        )
    target_registry = _registry_from_staged_voice(
        staging, voice_config, failure_reference_binding
    )
    source_registry = _workspace_voice_registry(source.directory, source.document)
    source_queue_overrides = _workspace_queue_voice_overrides(
        source.directory, source.document
    )
    target_manifest = _within(
        staging,
        _safe_relative(voice_config.get("path"), "Voice manifest snapshot"),
        "Voice manifest snapshot",
    )
    target_queue_overrides = _queue_voice_overrides_for_manifest(target_manifest)
    runtime_binding = _failure_reference_runtime_binding(
        staging, {"failure_reference_binding": failure_reference_binding}
    )
    if runtime_binding is not None:
        target_queue_overrides = {
            **target_queue_overrides,
            **runtime_binding.queue_voice_overrides,
        }
    return (
        target_state_path,
        target_state,
        copy.deepcopy(target_state),
        source_registry,
        target_registry,
        source_queue_overrides,
        target_queue_overrides,
    )


def _carry_forward_reviewed_items(
    staging,
    target_queue,
    source,
    target_state,
    target_seed,
    selection,
    source_registry,
    target_registry,
    source_queue_overrides,
    target_queue_overrides,
):
    source_provenance = None
    snapshots = []
    carried = []
    for queue_item in target_queue.items:
        result = source.state["items"].get(queue_item.queue_id)
        if not isinstance(result, dict) or not _terminal_review_outcome(result):
            continue
        character = synthesis_character_for_line(
            queue_item.speaker, queue_item.voice_character
        )
        if character == "Narrator" or character not in selection.characters:
            continue
        carry_record, snapshot, source_provenance = _carry_forward_reviewed_item(
            staging,
            queue_item,
            result,
            character,
            source,
            target_state,
            target_seed,
            source_provenance,
            source_registry,
            target_registry,
            source_queue_overrides,
            target_queue_overrides,
        )
        carried.append({"queue_id": queue_item.queue_id, **carry_record})
        snapshots.append(snapshot)
    return carried, snapshots


def _carry_forward_reviewed_item(
    staging,
    queue_item,
    result,
    character,
    source,
    target_state,
    target_seed,
    source_provenance,
    source_registry,
    target_registry,
    source_queue_overrides,
    target_queue_overrides,
):
    mode = "review-only"
    if not _same_seed_generation(target_seed["items"].get(queue_item.queue_id), result):
        mode = "full-outcome"
        if source_provenance is None:
            source_provenance = _workspace_generation_provenance(
                source.directory, source.document
            )
        source_character = source_queue_overrides.get(queue_item.queue_id, character)
        target_character = target_queue_overrides.get(queue_item.queue_id, character)
        if source_character != target_character:
            raise AuthoringWorkbenchError(
                f"Carry-forward queue voice differs for {queue_item.queue_id!r}"
            )
        _validate_full_carry_forward_item(
            queue_item,
            result,
            source_character,
            source.document,
            source.run_config,
            source_provenance,
            source_registry,
            target_registry,
        )
    relative = _safe_relative(
        result.get("path"), f"Carry-forward item {queue_item.queue_id!r} path"
    )
    for other_queue_id, other_result in target_seed["items"].items():
        if (
            other_queue_id != queue_item.queue_id
            and isinstance(other_result, dict)
            and other_result.get("path") == relative.as_posix()
        ):
            raise AuthoringWorkbenchError(
                f"Carry-forward WAV path collides with {other_queue_id!r}"
            )
    source_audio = _within(source.output, relative, "Carry-forward source WAV")
    audio_payload = _read_file_bytes(source_audio, "carry-forward source WAV")
    audio_sha256 = hashlib.sha256(audio_payload).hexdigest()
    if audio_sha256 != _require_sha256(
        result.get("file_sha256"),
        f"Carry-forward item {queue_item.queue_id!r} WAV SHA-256",
    ):
        raise AuthoringWorkbenchError(
            f"Carry-forward source WAV changed for {queue_item.queue_id!r}"
        )
    target_audio = _within(
        staging / "generated-audio", relative, "Carry-forward target WAV"
    )
    if mode == "full-outcome":
        target_audio.parent.mkdir(parents=True, exist_ok=True)
        target_audio.write_bytes(audio_payload)
    elif not target_audio.is_file() or sha256_file(target_audio) != audio_sha256:
        raise AuthoringWorkbenchError(
            f"Carry-forward seed WAV differs for {queue_item.queue_id!r}"
        )
    carry_record = {
        "mode": mode,
        "source_workspace_id": source.document["workspace_id"],
        "source_state_sha256": source.state_sha256,
        "source_item_sha256": canonical_document_sha256(result),
        "audio_sha256": audio_sha256,
        "character": character,
    }
    copied_result = copy.deepcopy(result)
    copied_result["carry_forward"] = carry_record
    target_state["items"][queue_item.queue_id] = copied_result
    return carry_record, (source_audio, audio_sha256), source_provenance


def _validate_failed_carry_forward_kind(
    strategy,
    failure,
    text,
    fallback_authority,
    source_provider_attempts,
    source_repair_strategy,
):
    if strategy == SENTENCE_BOUNDARY_SEGMENTATION:
        return not sentence_repair_matches_failure(failure, text)
    if strategy == INLINE_PAUSE_MARKER:
        return not inline_pause_matches_failure(failure, text)
    if strategy != OFFLINE_FALLBACK_BACKEND:
        return failure.get("kind") != "missed_eos_audio_limit"
    attempts_exhausted = (
        isinstance(source_provider_attempts, int)
        and not isinstance(source_provider_attempts, bool)
        and source_provider_attempts >= MAX_BOUNDED_TOTAL_ATTEMPTS
    )
    speech_silence = (
        failure.get("kind") == "speech_silence"
        and source_repair_strategy in {None, BOUNDED_SEED_RETRY, INLINE_PAUSE_MARKER}
        and (
            fallback_authority is not None
            or inline_pause_matches_failure(failure, text)
        )
    )
    return not (
        (fallback_authority is not None or attempts_exhausted)
        and (failure.get("kind") == "missed_eos_audio_limit" or speech_silence)
    )


def _validate_failed_carry_forward_source(
    source, selection, queue_by_id, authority_by_queue_id, queue_id
):
    if queue_id not in queue_by_id:
        raise AuthoringWorkbenchError(
            f"Failure repair references unknown queue item {queue_id!r}"
        )
    result = source.state["items"].get(queue_id)
    if not isinstance(result, dict) or result.get("status") != "failed":
        raise AuthoringWorkbenchError(
            f"Failure repair requires a current failed source outcome for {queue_id!r}"
        )
    queue_item = queue_by_id[queue_id]
    failure = normalized_failure_record(result, text=queue_item.text)
    attempts = result.get("attempts")
    source_model = _required_text(
        result.get("model"), f"Offline fallback source model for {queue_id!r}"
    )
    source_profile = _required_text(
        result.get("generation_profile"),
        f"Offline fallback source profile for {queue_id!r}",
    )
    strategy = selection.repair_policy.strategy_for(queue_id)
    fallback_authority = authority_by_queue_id.get(queue_id)
    attempts_by_provider = result.get("attempts_by_provider")
    source_provider_attempts = (
        attempts_by_provider.get(result.get("provider"), attempts)
        if isinstance(attempts_by_provider, dict)
        else attempts
    )
    source_repair = result.get("failure_repair")
    source_repair_strategy = (
        source_repair.get("strategy") if isinstance(source_repair, dict) else None
    )
    minimum_attempts = (
        MAX_BOUNDED_TOTAL_ATTEMPTS
        if strategy == OFFLINE_FALLBACK_BACKEND and fallback_authority is None
        else 1
    )
    if (
        _validate_failed_carry_forward_kind(
            strategy,
            failure,
            queue_item.text,
            fallback_authority,
            source_provider_attempts,
            source_repair_strategy,
        )
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < minimum_attempts
        or result.get("provider") != source.run_config.get("backend")
        or (
            source.run_config.get("model") is not None
            and source_model != source.run_config.get("model")
        )
        or (
            source.run_config.get("generation_profile") is not None
            and source_profile != source.run_config.get("generation_profile")
        )
    ):
        raise AuthoringWorkbenchError(
            f"Failure-repair source is not a compatible typed backend failure for {queue_id!r}"
        )
    provider_attempts = None
    if strategy in {BOUNDED_SEED_RETRY, INLINE_PAUSE_MARKER}:
        provider_attempts = result.get("attempts_by_provider", {}).get(
            result.get("provider"), attempts
        )
        if (
            not isinstance(provider_attempts, int)
            or isinstance(provider_attempts, bool)
            or not 1 <= provider_attempts < 3
        ):
            raise AuthoringWorkbenchError(
                f"Bounded repair source attempts are exhausted for {queue_id!r}"
            )
    return (
        result,
        failure,
        attempts,
        source_model,
        source_profile,
        strategy,
        fallback_authority,
        source_provider_attempts,
        source_repair_strategy,
        provider_attempts,
    )


def _carry_forward_failed_items(
    source,
    target_state,
    target_queue,
    selection,
    authority_by_queue_id,
    source_registry,
):
    queue_by_id = {item.queue_id: item for item in target_queue.items}
    carried = []
    for queue_id in selection.failed_queue_ids:
        (
            result,
            failure,
            attempts,
            source_model,
            source_profile,
            strategy,
            fallback_authority,
            source_provider_attempts,
            source_repair_strategy,
            provider_attempts,
        ) = _validate_failed_carry_forward_source(
            source, selection, queue_by_id, authority_by_queue_id, queue_id
        )
        queue_item = queue_by_id[queue_id]
        requested_character = synthesis_character_for_line(
            queue_item.speaker, queue_item.voice_character
        )
        effective_character = _required_text(
            result.get("voice_character", requested_character),
            f"Failure-repair source voice character for {queue_id!r}",
        )
        reference_character = (
            _required_text(
                source.document.get("narrator_character"),
                "Carry-forward source narrator character",
            )
            if effective_character == "Narrator"
            else effective_character
        )
        carry_record = {
            "mode": "failed-outcome",
            "source_workspace_id": source.document["workspace_id"],
            "source_state_sha256": source.state_sha256,
            "source_item_sha256": canonical_document_sha256(result),
            "character": effective_character,
            "source_provider": result["provider"],
            "source_model": source_model,
            "source_generation_profile": source_profile,
            "source_attempts": attempts,
            "source_seed": result.get("seed"),
            "source_failure_kind": failure["kind"],
            "source_voice_reference": _voice_reference_identity(
                source_registry, reference_character
            ),
        }
        if source_repair_strategy is not None:
            carry_record["source_repair_strategy"] = source_repair_strategy
        if strategy == OFFLINE_FALLBACK_BACKEND:
            carry_record["source_provider_attempts"] = source_provider_attempts
            if fallback_authority is not None:
                carry_record["source_unresolved_authority"] = (
                    fallback_authority.reference_record(queue_id)
                )
        if strategy == BOUNDED_SEED_RETRY:
            carry_record["source_provider_attempts"] = provider_attempts
        parent_carry = result.get("carry_forward")
        if isinstance(parent_carry, dict):
            carry_record["source_parent_carry_forward"] = copy.deepcopy(parent_carry)
        copied_result = copy.deepcopy(result)
        copied_result["carry_forward"] = carry_record
        target_state["items"][queue_id] = copied_result
        carried.append({"queue_id": queue_id, **carry_record})
    return carried


def _validate_carry_forward_results(selection, carried):
    if not carried:
        raise AuthoringWorkbenchError(
            "Carry-forward source has no terminal review outcomes for the selected characters"
        )
    unknown = set(selection.characters) - {value["character"] for value in carried}
    if unknown:
        raise AuthoringWorkbenchError(
            "Carry-forward has no terminal review outcomes for: "
            + ", ".join(sorted(unknown))
        )


def _publish_carry_forward_staging(target_state_path, target_state, source, snapshots):
    atomic_write_json(target_state_path, target_state, sort_keys=True)
    try:
        publish_generated_manifest(target_state_path)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    if sha256_file(source.state_path) != source.state_sha256:
        raise AuthoringWorkbenchError(
            "Carry-forward source state changed before workspace publication"
        )
    for path, digest in snapshots:
        if not path.is_file() or sha256_file(path) != digest:
            raise AuthoringWorkbenchError(
                "Carry-forward source WAV changed before workspace publication"
            )


def _carry_forward_document(source, selection, carried, authorities, authority_records):
    document = {
        "schema": "vntts.authoring-carry-forward",
        "schema_version": 4
        if authorities
        else (3 if selection.failed_queue_ids else 1),
        "source_workspace_id": source.document["workspace_id"],
        "source_state_sha256": source.state_sha256,
        "characters": list(selection.characters),
        "items": carried,
    }
    if selection.failed_queue_ids:
        document["failed_queue_ids"] = list(selection.failed_queue_ids)
        document["source_run_config"] = source.document.get("run_config")
    if authorities:
        document["offline_fallback_authorities"] = authority_records
    return document


def _carry_forward_review_outcomes(
    source_workspace,
    staging,
    target_queue,
    *,
    import_id,
    voice_config,
    run_config,
    characters,
    failure_repair_policy,
    failure_reference_binding,
    offline_fallback_authorities,
):
    selection = _select_carry_forward_outcomes(
        source_workspace,
        characters,
        failure_repair_policy,
        offline_fallback_authorities,
    )
    if selection is None:
        return None, ()
    source = _load_carry_forward_source(
        source_workspace,
        staging,
        target_queue,
        import_id=import_id,
        run_config=run_config,
        failure_reference_binding=failure_reference_binding,
        selection=selection,
    )
    authorities, authority_by_queue_id, authority_records, authority_sources = (
        _stage_offline_fallback_authorities(
            staging, source, selection, offline_fallback_authorities
        )
    )
    (
        target_state_path,
        target_state,
        target_seed,
        source_registry,
        target_registry,
        source_queue_overrides,
        target_queue_overrides,
    ) = _load_carry_forward_target(
        staging,
        staging / "queue.jsonl",
        voice_config,
        failure_reference_binding,
        source,
    )
    carried, snapshots = _carry_forward_reviewed_items(
        staging,
        target_queue,
        source,
        target_state,
        target_seed,
        selection,
        source_registry,
        target_registry,
        source_queue_overrides,
        target_queue_overrides,
    )
    carried.extend(
        _carry_forward_failed_items(
            source,
            target_state,
            target_queue,
            selection,
            authority_by_queue_id,
            source_registry,
        )
    )
    _validate_carry_forward_results(selection, carried)
    _publish_carry_forward_staging(target_state_path, target_state, source, snapshots)
    return (
        _carry_forward_document(
            source, selection, carried, authorities, authority_records
        ),
        authority_sources,
    )


def _same_seed_generation(seed_result, reviewed_result):
    if not isinstance(seed_result, dict):
        return False
    seed = copy.deepcopy(seed_result)
    reviewed = copy.deepcopy(reviewed_result)
    for value in (seed, reviewed):
        value.pop("carry_forward", None)
        value.pop("updated_at", None)
        value["status"] = "generated"
        value["review_status"] = "pending_review"
    return seed == reviewed


def _validate_full_carry_forward_item(
    queue_item,
    result,
    character,
    source_document,
    run_config,
    source_provenance,
    source_registry,
    target_registry,
):
    expected = {
        "provider": run_config.get("backend"),
        "model": run_config.get("model"),
        "generation_profile": run_config.get("generation_profile"),
        "voice_character": character,
        "prompt_sha256": NO_PROMPT_SHA256,
        "prompt_applied": False,
        "queue_annotations_sha256": canonical_document_sha256(
            queue_item.document.get("prompt_adapters") or {}
        ),
        "synthesis_provenance_sha256": source_provenance,
    }
    projection_ids = set(run_config.get("audio_event_spoken_projection_queue_ids", ()))
    synthesis_text = queue_item.text
    text_transform = None
    if queue_item.queue_id in projection_ids:
        plan = audio_event_plan_for_record(queue_item)
        if not isinstance(plan, dict) or not plan.get("spoken_text"):
            raise AuthoringWorkbenchError(
                f"Carry-forward audio-event projection changed for {queue_item.queue_id!r}"
            )
        synthesis_text = plan["spoken_text"]
        text_transform = "audio-event-spoken-projection-v1"
    elif run_config.get("backend") == "moss-tts":
        synthesis_text = normalize_short_trailing_ellipsis(synthesis_text)
        text_transform = "short-trailing-ellipsis-v1"
    expected["synthesis_text_sha256"] = hashlib.sha256(
        synthesis_text.encode("utf-8")
    ).hexdigest()
    expected["text_transform"] = text_transform
    mismatched = [
        field for field, value in expected.items() if result.get(field) != value
    ]
    if mismatched:
        raise AuthoringWorkbenchError(
            f"Carry-forward controls differ for {queue_item.queue_id!r}: "
            + ", ".join(mismatched)
        )
    source_voice = _voice_reference_identity(source_registry, character)
    target_voice = _voice_reference_identity(target_registry, character)
    if source_voice != target_voice:
        raise AuthoringWorkbenchError(
            f"Carry-forward voice references differ for {character!r}"
        )
    if _workspace_run_config_with_policy(
        source_document.get("run_config")
    ) != _workspace_run_config_with_policy(run_config):
        raise AuthoringWorkbenchError("Carry-forward run configuration changed")


def _workspace_generation_provenance(directory, workspace):
    run_config = workspace["run_config"]
    backend = _required_text(run_config.get("backend"), "Generation backend")
    model = _required_text(run_config.get("model"), "Generation model")
    profile = _required_text(run_config.get("generation_profile"), "Generation profile")
    manifest = _selected_voice_manifest(directory, workspace)
    if manifest is None:
        raise AuthoringWorkbenchError("Carry-forward source has no voice manifest")
    registry = _workspace_voice_registry(directory, workspace)
    queue = _load_bound_workspace_queue(directory, workspace)
    queue_overrides = _workspace_queue_voice_overrides(directory, workspace)
    missing_voice_policy = _workspace_missing_voice_policy(workspace)
    failure_repair_policy = _workspace_failure_repair_policy(workspace)
    projection_ids = workspace_audio_event_spoken_projection_queue_ids(
        workspace, error_type=AuthoringWorkbenchError
    )
    narrator = _required_text(workspace.get("narrator_character"), "Narrator character")
    narrator_voice = registry.resolve(narrator)
    synthesis_character_overrides = _narrator_fallback_overrides(
        queue, registry, narrator_voice, missing_voice_policy
    )
    controls = _workspace_generation_controls(
        directory,
        workspace,
        manifest,
        model,
        narrator,
        narrator_voice,
        registry,
    )
    try:
        snapshots = snapshot_generation_control_files(controls)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    synthesis_configuration = _workspace_synthesis_configuration(
        missing_voice_policy,
        failure_repair_policy,
        synthesis_character_overrides,
        projection_ids,
        queue_overrides,
    )
    return canonical_document_sha256(
        {
            "provider": backend,
            "model": model,
            "generation_profile": profile,
            "text_transform": (
                "audio-event-spoken-projection-v1"
                if projection_ids
                else ("short-trailing-ellipsis-v1" if backend == "moss-tts" else None)
            ),
            **synthesis_configuration,
            "controls": [
                {"role": value["role"], "sha256": value["sha256"]}
                for value in snapshots
            ],
        }
    )


def _narrator_fallback_overrides(queue, registry, narrator_voice, policy):
    narrator_ready = (
        narrator_voice is not None
        and bool(narrator_voice.references)
        and all(reference.is_file() for reference in narrator_voice.references)
    )
    overrides = {}
    for item in queue.items:
        requested = synthesis_character_for_line(item.speaker, item.voice_character)
        voice = registry.resolve(requested)
        missing = (
            voice is None
            or not voice.references
            or any(not reference.is_file() for reference in voice.references)
        )
        if (
            requested != "Narrator"
            and missing
            and policy.applies_to(requested)
            and narrator_ready
        ):
            overrides[requested] = "Narrator"
    return overrides


def _workspace_generation_controls(
    directory, workspace, manifest, model, narrator, narrator_voice, registry
):
    controls = {"voice_manifest": (manifest, sha256_control_path(manifest))}
    references = sorted(
        {
            path.resolve()
            for voice in registry.unique_voices()
            for path in voice.references
        },
        key=str,
    )
    for index, path in enumerate(references, start=1):
        controls[f"voice_reference:{index:04d}"] = (
            path,
            sha256_control_path(path),
        )
    runtime_binding = _failure_reference_runtime_binding(directory, workspace)
    if runtime_binding is not None:
        binding_path = (runtime_binding.directory / "binding.json").resolve()
        controls["failure_reference_binding"] = (
            binding_path,
            runtime_binding.controls[binding_path],
        )
        selected_paths = sorted(
            (path for path in runtime_binding.controls if path != binding_path),
            key=str,
        )
        for index, path in enumerate(selected_paths, start=1):
            controls[f"failure_reference_selected:{index:04d}"] = (
                path,
                runtime_binding.controls[path],
            )
    model_path = Path(model).expanduser()
    if model_path.exists():
        model_path = model_path.resolve()
        controls["model_artifact"] = (
            model_path,
            sha256_control_path(model_path),
        )
    if narrator_voice is not None and narrator_voice.references:
        reference = narrator_voice.references[0]
        controls[f"narrator_selection:{narrator}"] = (
            reference,
            sha256_control_path(reference),
        )
    return controls


def _workspace_synthesis_configuration(
    missing_voice_policy,
    failure_repair_policy,
    synthesis_character_overrides,
    projection_ids,
    queue_overrides,
):
    configuration = {
        "missing_voice_policy": missing_voice_policy.to_document(),
        "synthesis_character_overrides": dict(
            sorted(synthesis_character_overrides.items())
        ),
        "failure_repair_policy": failure_repair_policy.to_document(),
    }
    if projection_ids:
        configuration["audio_event_spoken_projection_queue_ids"] = list(projection_ids)
    if queue_overrides:
        configuration["queue_voice_overrides_sha256"] = queue_voice_overrides_sha256(
            queue_overrides
        )
    return configuration


def _workspace_voice_registry(directory, workspace):
    return load_workspace_voice_registry(
        directory,
        workspace,
        error_type=AuthoringWorkbenchError,
    )


def _registry_from_staged_voice(staging, voice_config, failure_reference_binding=None):
    if not isinstance(voice_config, dict):
        raise AuthoringWorkbenchError(
            "Carry-forward target requires a voice manifest snapshot"
        )
    manifest = _within(
        staging,
        _safe_relative(voice_config.get("path"), "Voice manifest snapshot"),
        "Voice manifest snapshot",
    )
    try:
        registry = CharacterVoiceRegistry.from_file(manifest)
    except VoiceManifestError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    runtime_binding = _failure_reference_runtime_binding(
        staging,
        {"failure_reference_binding": failure_reference_binding},
    )
    if runtime_binding is None:
        return registry
    try:
        return CharacterVoiceRegistry(
            (*registry.unique_voices(), *runtime_binding.voices)
        )
    except VoiceManifestError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _voice_reference_identity(registry, character):
    voice = registry.resolve(character)
    if voice is None or not voice.references:
        raise AuthoringWorkbenchError(
            f"Carry-forward voice references are missing for {character!r}"
        )
    return {
        "character": voice.character,
        "speaker": voice.speaker,
        "aliases": list(voice.aliases),
        "references": [sha256_control_path(path) for path in voice.references],
    }


def _queue_voice_overrides_for_manifest(manifest):
    try:
        document, entries = load_voice_manifest(manifest, allow_legacy=False)
        return queue_voice_overrides_from_manifest(document, voices=entries)
    except (SourceReferenceBindingError, VoiceManifestError, OSError) as error:
        raise AuthoringWorkbenchError(
            f"Unable to load carry-forward queue voice bindings: {error}"
        ) from error


def _workspace_queue_voice_overrides(directory, workspace):
    return load_workspace_queue_voice_overrides(
        directory,
        workspace,
        error_type=AuthoringWorkbenchError,
    )


def _read_file_bytes(path, label):
    return read_regular_file(path, label, error_type=AuthoringWorkbenchError)


def _validated_import_inventory(source, manifest):
    values = manifest.get("artifacts")
    if not isinstance(values, list) or not values:
        raise AuthoringWorkbenchError("Legacy import artifact inventory is missing")
    inventory = []
    seen = set()
    for value in values:
        if not isinstance(value, dict):
            raise AuthoringWorkbenchError(
                "Legacy import artifact inventory is malformed"
            )
        relative = _safe_relative(value.get("path"), "Imported artifact path")
        digest = _require_sha256(value.get("sha256"), "Imported artifact SHA-256")
        path = _within(source, relative, "Imported artifact")
        if relative.as_posix() in seen:
            raise AuthoringWorkbenchError("Legacy import has duplicate artifact paths")
        seen.add(relative.as_posix())
        if not path.is_file() or sha256_file(path) != digest:
            raise AuthoringWorkbenchError(
                f"Imported artifact is missing or changed: {relative}"
            )
        inventory.append({"path": relative.as_posix(), "sha256": digest})
    return tuple(inventory)


def _validate_existing_workspace(
    destination, *, import_id, import_sha256, source_fingerprint
):
    _directory, workspace = _load_workspace(destination)
    source = workspace.get("source")
    expected = {
        "kind": "legacy-import",
        "import_id": import_id,
        "import_sha256": import_sha256,
        "source_fingerprint": source_fingerprint,
        "snapshot": "provenance/import.json",
    }
    if source != expected:
        raise AuthoringWorkbenchError(
            f"Workspace destination conflicts with another source: {destination}"
        )


def _legacy_narrator(manifest):
    legacy = manifest.get("legacy_job")
    if isinstance(legacy, dict):
        value = legacy.get("narrator_character")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Narrator"


def _copy_input_snapshots(
    staging,
    *,
    story_index,
    voice_manifest,
    import_manifest,
    queue,
):
    selected_sources = []
    story_config = None
    if story_index is not None:
        source = Path(story_index).expanduser().resolve()
        payload, digest = _read_source_bytes(source, "story index")
        legacy_digest = _legacy_input_digest(import_manifest, "story_index")
        target = staging / "inputs" / "story-index.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        story_config = {
            "path": "inputs/story-index.jsonl",
            "sha256": digest,
            "legacy_sha256_at_import": legacy_digest,
            "matches_legacy": legacy_digest == digest if legacy_digest else None,
        }
        selected_sources.append((source, digest, "story index"))

    voice_config = None
    if voice_manifest is not None:
        source = Path(voice_manifest).expanduser().resolve()
        payload, digest = _read_source_bytes(source, "voice manifest")
        legacy_digest = _legacy_input_digest(import_manifest, "voice_manifest")
        target = staging / "inputs" / "voice" / "manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        try:
            document, entries = load_voice_manifest(target)
        except VoiceManifestError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        for field in ("game", "language"):
            declared = document.get(field)
            if declared is not None and declared != queue.metadata.get(field):
                raise AuthoringWorkbenchError(
                    f"Selected voice manifest {field} does not match the queue"
                )
        controls = []
        seen = set()
        for entry in entries:
            for value in entry.references:
                relative = _safe_relative(value, "Voice reference")
                key = relative.as_posix()
                if key in seen:
                    continue
                seen.add(key)
                reference_source = _within(
                    source.parent, relative, "Voice reference source"
                )
                reference_payload, reference_digest = _read_source_bytes(
                    reference_source, "voice reference"
                )
                reference_target = staging / "inputs" / "voice" / relative
                reference_target.parent.mkdir(parents=True, exist_ok=True)
                reference_target.write_bytes(reference_payload)
                control_path = (Path("inputs") / "voice" / relative).as_posix()
                controls.append({"path": control_path, "sha256": reference_digest})
                selected_sources.append(
                    (reference_source, reference_digest, "voice reference")
                )
        try:
            validate_reference_selection_provenance(target, document)
        except ReferenceSelectionError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        voice_config = {
            "path": "inputs/voice/manifest.json",
            "sha256": digest,
            "controls": controls,
            "legacy_sha256_at_import": legacy_digest,
            "matches_legacy": legacy_digest == digest if legacy_digest else None,
        }
        selected_sources.append((source, digest, "voice manifest"))
    return story_config, voice_config, tuple(selected_sources)


def _copy_carry_forward_failure_reference_binding(
    staging,
    source_workspace,
    failure_queue_ids,
):
    selected = set(failure_queue_ids)
    if source_workspace is None or not selected:
        return None, ()
    source_directory, source_document = _load_workspace(source_workspace)
    runtime_binding = _failure_reference_runtime_binding(
        source_directory,
        source_document,
    )
    if runtime_binding is None or not (
        selected & set(runtime_binding.queue_voice_overrides)
    ):
        return None, ()
    config = copy.deepcopy(source_document["failure_reference_binding"])
    snapshots = []
    target = staging / "inputs" / "failure-reference-binding"
    _copy_workspace_tree_snapshot(runtime_binding.directory, target, snapshots)
    return config, tuple(
        (path, digest, "failure-reference binding") for path, digest in snapshots
    )


def _read_source_bytes(path, label):
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise AuthoringWorkbenchError(
            f"Unable to read {label} {path}: {error}"
        ) from error
    return payload, hashlib.sha256(payload).hexdigest()


def _verify_selected_sources(selected_sources):
    for path, digest, label in selected_sources:
        if not path.is_file() or sha256_file(path) != digest:
            raise AuthoringWorkbenchError(
                f"Selected {label} changed while workspace was being created"
            )


def _copy_workspace_tree_snapshot(source, target, snapshots):
    return copy_workspace_tree_snapshot(
        source,
        target,
        snapshots,
        error_type=AuthoringWorkbenchError,
    )


def _workspace_missing_voice_policy(workspace):
    return workspace_missing_voice_policy(
        workspace,
        error_type=AuthoringWorkbenchError,
    )


def _selected_voice_manifest(directory, workspace, selected=None):
    return selected_voice_manifest_path(
        directory,
        workspace,
        selected,
        error_type=AuthoringWorkbenchError,
    )


def _verify_import_sources(source, copied, import_path, import_sha256):
    if not import_path.is_file() or sha256_file(import_path) != import_sha256:
        raise AuthoringWorkbenchError(
            "Immutable import manifest changed while workspace was being created"
        )
    for item in copied:
        path = _within(
            source,
            _safe_relative(item["path"], "Imported artifact"),
            "Imported artifact",
        )
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            raise AuthoringWorkbenchError(
                "Immutable import changed while workspace was being created"
            )


def _optional_text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


__all__ = [
    "_CarryForwardSelection",
    "_CarryForwardSource",
    "_carry_forward_document",
    "_carry_forward_failed_items",
    "_carry_forward_review_outcomes",
    "_carry_forward_reviewed_item",
    "_carry_forward_reviewed_items",
    "_copy_carry_forward_failure_reference_binding",
    "_copy_input_snapshots",
    "_copy_workspace_tree_snapshot",
    "_failure_reference_runtime_binding",
    "_install_extended_generation_queue",
    "_legacy_narrator",
    "_load_carry_forward_source",
    "_load_carry_forward_target",
    "_optional_text",
    "_preserve_seed_generation_state",
    "_publish_carry_forward_staging",
    "_queue_voice_overrides_for_manifest",
    "_read_file_bytes",
    "_read_source_bytes",
    "_registry_from_staged_voice",
    "_same_seed_generation",
    "_select_carry_forward_outcomes",
    "_selected_voice_manifest",
    "_stage_offline_fallback_authorities",
    "_validate_carry_forward_results",
    "_validate_carry_forward_source_config",
    "_validate_existing_workspace",
    "_validate_failed_carry_forward_kind",
    "_validate_failed_carry_forward_source",
    "_validate_full_carry_forward_item",
    "_validated_import_inventory",
    "_verify_import_sources",
    "_verify_selected_sources",
    "_voice_reference_identity",
    "_workspace_generation_provenance",
    "_workspace_missing_voice_policy",
    "_workspace_queue_voice_overrides",
    "_workspace_voice_registry",
    "create_audio_event_composition_workspace",
    "create_failure_reference_workspace",
    "create_resume_workspace",
    "default_workspaces_root",
]

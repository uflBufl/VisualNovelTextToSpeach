"""Validated workspace loading and provenance contracts."""

from __future__ import annotations

import copy
import hashlib
import importlib
import re
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)
from vntts_artifacts.voice_manifest import VoiceManifestError, load_voice_manifest

import vntts.authoring.legacy_import as legacy_import
from vntts.authoring.audio_event_workspace import (
    AudioEventWorkspaceError,
    validate_audio_event_composition_workspace,
)
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
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
from vntts.authoring.offline_fallback_authority import (
    OfflineFallbackAuthorityError,
    validate_offline_fallback_authority_records,
)
from vntts.authoring.queue_extension import (
    WORKSPACE_SCHEMA as QUEUE_EXTENSION_WORKSPACE_SCHEMA,
)
from vntts.authoring.queue_extension import (
    WORKSPACE_VERSION as QUEUE_EXTENSION_WORKSPACE_VERSION,
)
from vntts.authoring.queue_extension import (
    QueueExtensionError,
    validate_additive_generation_queue,
)
from vntts.authoring.terminal_conflict_records import (
    is_terminal_review_outcome as _terminal_review_outcome,
)
from vntts.authoring.workbench_contracts import (
    WORKSPACE_SCHEMA,
    WORKSPACE_VERSION,
    AuthoringWorkbenchError,
    _read_bound_bytes,
)
from vntts.authoring.workspace_config import (
    normalize_workspace_run_config,
    workspace_failure_repair_policy,
    workspace_queue_sha256,
)
from vntts.authoring.workspace_config import (
    workspace_config_fingerprint as _workspace_config_fingerprint,
)
from vntts.authoring.workspace_foundation import (
    contained_path,
    load_json_object,
    load_json_object_snapshot,
    read_regular_file,
    require_sha256,
    safe_relative_path,
)
from vntts.authoring.workspace_state import (
    load_stable_workspace_generation_state,
    share_workspace_generation_state,
    shared_workspace_state_reads,
)


def _load_bound_workspace_queue(directory, workspace):
    queue_digest = workspace_queue_sha256(workspace, error_type=AuthoringWorkbenchError)
    payload = _read_bound_bytes(
        directory / "queue.jsonl",
        queue_digest,
        "Workspace queue",
    )
    with tempfile.TemporaryDirectory(prefix="vntts-queue-snapshot-") as temporary:
        snapshot = Path(temporary) / "queue.jsonl"
        snapshot.write_bytes(payload)
        try:
            return VoiceGenerationQueue.load(snapshot)
        except VoiceGenerationQueueError as error:
            raise AuthoringWorkbenchError(str(error)) from error


def _parse_history_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _load_workspace(workspace_directory):
    with shared_workspace_state_reads():
        return _load_workspace_scoped(workspace_directory)


def _load_workspace_scoped(workspace_directory):
    directory = Path(workspace_directory).expanduser().resolve()
    workspace, match, snapshot, expected_import_id, expected_seed = (
        _load_workspace_identity(directory)
    )
    narrator, run_config = _validate_workspace_layout(
        directory, workspace, snapshot, expected_import_id, expected_seed
    )
    state, state_sha256 = _load_workspace_validation_state(directory, workspace)
    _validate_workspace_extensions(directory, workspace, snapshot, state)
    _validate_workspace_config_identity(
        workspace, match, expected_import_id, narrator, run_config
    )
    if (
        state_sha256 is not None
        and sha256_file(directory / "generated-audio/generation-state.json")
        != state_sha256
    ):
        raise AuthoringWorkbenchError("Workspace generation state changed while loaded")
    return directory, workspace


def _load_workspace_identity(directory):
    workspace, match, source, expected_import_id = _load_workspace_document(directory)
    snapshot, snapshot_sha256 = _load_workspace_import_snapshot(
        directory, source, expected_import_id
    )
    expected_seed = [
        {"path": "provenance/import.json", "sha256": snapshot_sha256},
        *(
            {"path": value["path"], "sha256": value["sha256"]}
            for value in snapshot.get("artifacts", [])
            if isinstance(value, dict)
            and (
                value.get("path") == "queue.jsonl"
                or str(value.get("path", "")).startswith("generated-audio/")
            )
        ),
    ]
    if workspace.get("seed_inventory") != expected_seed:
        raise AuthoringWorkbenchError("Workspace seed inventory was modified")
    return workspace, match, snapshot, expected_import_id, expected_seed


def _load_workspace_document(directory):
    workspace_path = directory / "workspace.json"
    if workspace_path.is_symlink():
        raise AuthoringWorkbenchError("Workspace document must not be a symlink")
    workspace = _load_json(workspace_path, "authoring workspace")
    if (
        workspace.get("schema") != WORKSPACE_SCHEMA
        or workspace.get("schema_version") != WORKSPACE_VERSION
    ):
        raise AuthoringWorkbenchError(f"Unsupported authoring workspace: {directory}")
    match = re.fullmatch(r"resume-([0-9a-f]{24})-([0-9a-f]{16})", directory.name)
    if match is None:
        raise AuthoringWorkbenchError("Workspace directory name is not canonical")
    if workspace.get("workspace_id") != directory.name:
        raise AuthoringWorkbenchError("Workspace identity does not match its directory")
    if _parse_history_timestamp(workspace.get("created_at")) is None:
        raise AuthoringWorkbenchError(
            "Workspace creation timestamp is missing or invalid"
        )
    if (
        workspace.get("queue") != "queue.jsonl"
        or workspace.get("output") != "generated-audio"
    ):
        raise AuthoringWorkbenchError("Workspace core paths were modified")
    source = workspace.get("source")
    expected_import_id = f"legacy-{match.group(1)}"
    if not isinstance(source, dict) or source.get("import_id") != expected_import_id:
        raise AuthoringWorkbenchError("Workspace source identity was modified")
    if (
        source.get("kind") != "legacy-import"
        or source.get("snapshot") != "provenance/import.json"
    ):
        raise AuthoringWorkbenchError("Workspace provenance path was modified")
    return workspace, match, source, expected_import_id


def _load_workspace_import_snapshot(directory, source, expected_import_id):
    snapshot_path = _within(
        directory, Path("provenance/import.json"), "Import snapshot"
    )
    if snapshot_path.is_symlink():
        raise AuthoringWorkbenchError("Import snapshot must not be a symlink")
    snapshot, snapshot_sha256, _payload = _load_json_snapshot(
        snapshot_path, "workspace import snapshot"
    )
    if snapshot_sha256 != _require_sha256(
        source.get("import_sha256"), "Workspace import SHA-256"
    ):
        raise AuthoringWorkbenchError("Workspace import snapshot was modified")
    if (
        snapshot.get("schema") != legacy_import.IMPORT_SCHEMA
        or snapshot.get("schema_version")
        not in legacy_import.SUPPORTED_IMPORT_SCHEMA_VERSIONS
        or snapshot.get("import_id") != expected_import_id
        or snapshot.get("source", {}).get("source_fingerprint")
        != source.get("source_fingerprint")
    ):
        raise AuthoringWorkbenchError("Workspace provenance identity is inconsistent")
    _validate_import_history(snapshot)
    return snapshot, snapshot_sha256


def _validate_workspace_layout(
    directory, workspace, snapshot, expected_import_id, expected_seed
):
    _validate_workspace_carry_forward(directory, workspace)
    imported_queue_digest = next(
        (value["sha256"] for value in expected_seed if value["path"] == "queue.jsonl"),
        None,
    )
    _validate_workspace_queue_extension(
        directory, workspace, imported_queue_digest=imported_queue_digest
    )
    queue_digest = workspace_queue_sha256(workspace, error_type=AuthoringWorkbenchError)
    queue_path = directory / "queue.jsonl"
    if (
        queue_path.is_symlink()
        or not queue_path.is_file()
        or queue_path.resolve().parent != directory
        or sha256_file(queue_path) != queue_digest
    ):
        raise AuthoringWorkbenchError("Workspace immutable queue was modified")
    output_path = directory / "generated-audio"
    if (
        output_path.is_symlink()
        or not output_path.is_dir()
        or output_path.resolve().parent != directory
    ):
        raise AuthoringWorkbenchError(
            "Workspace generated-audio directory leaves its canonical root"
        )
    if workspace.get("title") != _workspace_title(snapshot, expected_import_id):
        raise AuthoringWorkbenchError("Workspace title provenance was modified")
    if workspace.get("legacy_external_inputs") != snapshot.get("external_inputs", []):
        raise AuthoringWorkbenchError("Workspace legacy input provenance was modified")
    narrator = _required_text(
        workspace.get("narrator_character"), "Workspace narrator character"
    )
    run_config = workspace.get("run_config")
    _workspace_run_config_with_policy(run_config)
    return narrator, run_config


def _load_workspace_validation_state(directory, workspace):
    stable_extensions = (
        "known_role_live_fallback",
        "audio_event_omission",
        "audio_event_projection_fallback",
        "reviewed_waveform_publication",
        "reviewed_rejection_live_fallback",
    )
    direct_extensions = (
        "outcome_merge",
        "terminal_conflict_merge",
        "config_rebase",
        "explicit_fallback_merge",
    )
    carry = workspace.get("carry_forward")
    if any(workspace.get(field) is not None for field in stable_extensions):
        _queue, state, _payload, state_sha256 = _stable_workspace_state(
            directory, workspace, "workspace validation"
        )
        return state, state_sha256
    if any(workspace.get(field) is not None for field in direct_extensions) or (
        isinstance(carry, dict) and carry.get("schema_version") in {2, 3, 4}
    ):
        state_path = directory / "generated-audio/generation-state.json"
        payload = read_regular_file(
            state_path,
            "workspace generation state",
            error_type=AuthoringWorkbenchError,
        )
        state_sha256 = hashlib.sha256(payload).hexdigest()
        try:
            state = load_generation_state(state_path, directory / "queue.jsonl")
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        queue = _load_bound_workspace_queue(directory, workspace)
        share_workspace_generation_state(
            directory, workspace, (queue, state, payload, state_sha256)
        )
        return state, state_sha256
    return None, None


def _validate_optional_workspace_extension(
    directory,
    workspace,
    state,
    field,
    module_name,
    validator_name,
    *,
    pass_state,
):
    if workspace.get(field) is None:
        return
    validator = getattr(importlib.import_module(module_name), validator_name)
    if pass_state:
        validator(directory, workspace, state=state)
    else:
        validator(directory, workspace)


def _validate_workspace_extensions(directory, workspace, snapshot, state):
    _validate_workspace_input_config(directory, workspace, snapshot)
    _validate_workspace_failure_reference_binding(directory, workspace)
    _validate_workspace_offline_fallback_state(directory, workspace, state=state)
    _validate_workspace_outcome_merge(directory, workspace, state=state)
    _validate_workspace_terminal_conflict_merge(directory, workspace, state=state)
    try:
        validate_audio_event_composition_workspace(directory, workspace)
    except AudioEventWorkspaceError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    extensions = (
        (
            "config_rebase",
            "vntts.authoring.config_rebase",
            "validate_config_rebase_workspace",
            True,
        ),
        (
            "explicit_fallback_merge",
            "vntts.authoring.explicit_fallback_merge",
            "validate_explicit_fallback_merge_workspace",
            True,
        ),
        (
            "known_role_live_fallback",
            "vntts.authoring.known_role_live_fallback",
            "validate_known_role_live_fallback_workspace",
            False,
        ),
        (
            "audio_event_omission",
            "vntts.authoring.audio_event_omission",
            "validate_audio_event_omission_workspace",
            False,
        ),
        (
            "audio_event_projection_fallback",
            "vntts.authoring.audio_event_projection_fallback",
            "validate_audio_event_projection_fallback_workspace",
            False,
        ),
        (
            "reviewed_waveform_publication",
            "vntts.authoring.reviewed_waveform_publication",
            "validate_reviewed_waveform_publication_workspace",
            False,
        ),
        (
            "reviewed_rejection_live_fallback",
            "vntts.authoring.reviewed_rejection_fallback",
            "validate_reviewed_rejection_fallback_workspace",
            False,
        ),
    )
    for field, module_name, validator_name, pass_state in extensions:
        _validate_optional_workspace_extension(
            directory,
            workspace,
            state,
            field,
            module_name,
            validator_name,
            pass_state=pass_state,
        )


def _validate_workspace_config_identity(
    workspace, match, expected_import_id, narrator, run_config
):
    expected_config = _workspace_config_fingerprint(
        expected_import_id,
        workspace.get("story_index"),
        workspace.get("voice_manifest"),
        narrator,
        run_config,
        workspace.get("carry_forward"),
        workspace.get("outcome_merge"),
        workspace.get("failure_reference_binding"),
        workspace.get("terminal_conflict_merge"),
        workspace.get("config_rebase"),
        workspace.get("audio_event_composition"),
        workspace.get("explicit_fallback_merge"),
        workspace.get("known_role_live_fallback"),
        workspace.get("audio_event_omission"),
        workspace.get("audio_event_projection_fallback"),
        workspace.get("reviewed_waveform_publication"),
        workspace.get("reviewed_rejection_live_fallback"),
        workspace.get("queue_extension"),
    )
    if (
        workspace.get("config_fingerprint") != expected_config
        or match.group(2) != expected_config[:16]
    ):
        raise AuthoringWorkbenchError("Workspace configuration identity was modified")


def _workspace_title(manifest, fallback):
    legacy = manifest.get("legacy_job")
    if isinstance(legacy, dict) and isinstance(legacy.get("title"), str):
        if legacy["title"].strip():
            return legacy["title"].strip()
    return fallback


def _validate_import_history(manifest):
    version = manifest.get("schema_version")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise AuthoringWorkbenchError("Import source provenance is malformed")
    kind = source.get("kind")
    job_kind = "reverse1999-extractor-pregeneration-job"
    standalone_kind = "reverse1999-extractor-standalone-generation"
    if kind not in {job_kind, standalone_kind}:
        raise AuthoringWorkbenchError("Import source kind is unsupported")
    legacy_job = manifest.get("legacy_job")
    artifacts = manifest.get("artifacts")
    has_job_artifact = isinstance(artifacts, list) and any(
        isinstance(value, dict) and value.get("role") == "legacy_job"
        for value in artifacts
    )
    has_job_markers = any(
        field in source
        for field in ("job_directory", "job_schema", "job_schema_version")
    )
    if kind == standalone_kind:
        if legacy_job is not None or has_job_artifact or has_job_markers:
            raise AuthoringWorkbenchError(
                "Standalone import contains inconsistent legacy job provenance"
            )
        return
    if (
        not isinstance(legacy_job, dict)
        or not has_job_artifact
        or source.get("job_schema") != legacy_import.LEGACY_JOB_SCHEMA
        or source.get("job_schema_version") != legacy_import.LEGACY_JOB_SCHEMA_VERSION
        or not isinstance(source.get("job_directory"), str)
        or not source["job_directory"].strip()
    ):
        raise AuthoringWorkbenchError("Version 2 import requires legacy job history")
    created_at = _parse_history_timestamp(legacy_job.get("created_at"))
    if version == 2 and created_at is None:
        raise AuthoringWorkbenchError(
            "Version 2 import requires a timezone-aware source created_at"
        )
    updated_value = legacy_job.get("updated_at")
    updated_at = _parse_history_timestamp(updated_value)
    if updated_value is not None and updated_at is None:
        raise AuthoringWorkbenchError(
            "Import source updated_at must be a timezone-aware timestamp"
        )
    if created_at is not None and updated_at is not None and updated_at < created_at:
        raise AuthoringWorkbenchError(
            "Import source updated_at must not precede created_at"
        )


def _external_input(manifest, role):
    for value in manifest.get("external_inputs", []):
        if isinstance(value, dict) and value.get("role") == role:
            path = value.get("source_path")
            digest = value.get("sha256")
            if isinstance(path, str) and path.strip():
                result = {"path": path, "exists_at_import": bool(value.get("exists"))}
                if isinstance(digest, str):
                    result["sha256_at_import"] = digest
                return result
    return None


def _legacy_input_digest(manifest, role):
    declared = _external_input(manifest, role)
    if declared is None:
        return None
    digest = declared.get("sha256_at_import")
    return _require_sha256(digest, f"Legacy {role} SHA-256") if digest else None


def _validate_workspace_queue_extension(directory, workspace, *, imported_queue_digest):
    config = workspace.get("queue_extension")
    if config is None:
        return
    required = {
        "schema",
        "schema_version",
        "base_queue_path",
        "base_queue_sha256",
        "queue_path",
        "queue_sha256",
        "extension_queue_sha256",
        "extension_id",
        "added_item_count",
        "added_queue_ids",
    }
    if (
        not isinstance(config, dict)
        or set(config) != required
        or config.get("schema") != QUEUE_EXTENSION_WORKSPACE_SCHEMA
        or config.get("schema_version") != QUEUE_EXTENSION_WORKSPACE_VERSION
        or config.get("base_queue_sha256") != imported_queue_digest
    ):
        raise AuthoringWorkbenchError("Workspace queue extension is malformed")
    base_path = _within(
        directory,
        _safe_relative(config["base_queue_path"], "Extended base queue snapshot"),
        "Extended base queue snapshot",
    )
    target_path = _within(
        directory,
        _safe_relative(config["queue_path"], "Extended queue snapshot"),
        "Extended queue snapshot",
    )
    if (
        not base_path.is_file()
        or base_path.is_symlink()
        or sha256_file(base_path) != imported_queue_digest
        or not target_path.is_file()
        or target_path.is_symlink()
        or sha256_file(target_path)
        != _require_sha256(config["queue_sha256"], "Extended queue SHA-256")
        or (directory / "queue.jsonl").read_bytes() != target_path.read_bytes()
    ):
        raise AuthoringWorkbenchError("Workspace queue extension snapshot changed")
    try:
        _queue, ledger = validate_additive_generation_queue(
            target_path, base_queue=base_path
        )
    except (OSError, QueueExtensionError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    expected_ids = sorted(record["queue_id"] for record in ledger["added_items"])
    if (
        config.get("extension_queue_sha256") != ledger["extension_queue_sha256"]
        or config.get("extension_id") != ledger["extension_id"]
        or config.get("added_item_count") != len(expected_ids)
        or config.get("added_queue_ids") != expected_ids
    ):
        raise AuthoringWorkbenchError("Workspace queue extension ledger changed")


def validate_workspace_provenance_extensions(directory, workspace, import_snapshot):
    """Validate the optional provenance layers attached to one workspace."""
    _validate_workspace_carry_forward(directory, workspace)
    _validate_workspace_input_config(directory, workspace, import_snapshot)
    _validate_workspace_offline_fallback_state(directory, workspace)
    _validate_workspace_outcome_merge(directory, workspace)
    _validate_workspace_terminal_conflict_merge(directory, workspace)
    if workspace.get("explicit_fallback_merge") is not None:
        module = importlib.import_module("vntts.authoring.explicit_fallback_merge")
        module.validate_explicit_fallback_merge_workspace(directory, workspace)
    if workspace.get("known_role_live_fallback") is not None:
        module = importlib.import_module("vntts.authoring.known_role_live_fallback")
        module.validate_known_role_live_fallback_workspace(directory, workspace)
    if workspace.get("audio_event_omission") is not None:
        module = importlib.import_module("vntts.authoring.audio_event_omission")
        module.validate_audio_event_omission_workspace(directory, workspace)
    if workspace.get("audio_event_projection_fallback") is not None:
        module = importlib.import_module(
            "vntts.authoring.audio_event_projection_fallback"
        )
        module.validate_audio_event_projection_fallback_workspace(directory, workspace)
    if workspace.get("reviewed_waveform_publication") is not None:
        module = importlib.import_module(
            "vntts.authoring.reviewed_waveform_publication"
        )
        module.validate_reviewed_waveform_publication_workspace(directory, workspace)
    if workspace.get("reviewed_rejection_live_fallback") is not None:
        module = importlib.import_module("vntts.authoring.reviewed_rejection_fallback")
        module.validate_reviewed_rejection_fallback_workspace(directory, workspace)


def _validate_workspace_carry_forward(directory, workspace):
    seed = workspace.get("seed_generation_state")
    carry = workspace.get("carry_forward")
    if seed is None:
        if carry is not None:
            raise AuthoringWorkbenchError(
                "Carry-forward workspace has no immutable seed state"
            )
        return
    if not isinstance(seed, dict) or set(seed) != {"path", "sha256"}:
        raise AuthoringWorkbenchError("Workspace seed state binding is malformed")
    if seed.get("path") != "provenance/seed-generation-state.json":
        raise AuthoringWorkbenchError("Workspace seed state path was modified")
    seed_path = _within(
        directory,
        _safe_relative(seed["path"], "Workspace seed state"),
        "Workspace seed state",
    )
    expected_seed = _require_sha256(seed.get("sha256"), "Workspace seed state SHA-256")
    _read_bound_bytes(seed_path, expected_seed, "Workspace seed state")
    if carry is None:
        return
    base_fields = {
        "schema",
        "schema_version",
        "source_workspace_id",
        "source_state_sha256",
        "characters",
        "items",
    }
    version = carry.get("schema_version") if isinstance(carry, dict) else None
    if version == 1:
        expected_fields = base_fields
    else:
        expected_fields = base_fields | {"failed_queue_ids", "source_run_config"}
        if version == 4:
            expected_fields.add("offline_fallback_authorities")
    if not isinstance(carry, dict) or set(carry) != expected_fields:
        raise AuthoringWorkbenchError("Workspace carry-forward provenance is malformed")
    if (
        carry.get("schema") != "vntts.authoring-carry-forward"
        or version not in {1, 2, 3, 4}
        or not isinstance(carry.get("source_workspace_id"), str)
        or not re.fullmatch(
            r"resume-[0-9a-f]{24}-[0-9a-f]{16}", carry["source_workspace_id"]
        )
    ):
        raise AuthoringWorkbenchError("Workspace carry-forward identity is invalid")
    source_state_sha256 = _require_sha256(
        carry.get("source_state_sha256"), "Carry-forward source state SHA-256"
    )
    characters = carry.get("characters")
    if (
        not isinstance(characters, list)
        or (version == 1 and not characters)
        or characters != sorted(set(characters))
        or "Narrator" in characters
        or any(not isinstance(value, str) or not value.strip() for value in characters)
    ):
        raise AuthoringWorkbenchError("Workspace carry-forward characters are invalid")
    failed_queue_ids = []
    if version in {2, 3, 4}:
        failed_queue_ids = carry.get("failed_queue_ids")
        if (
            not isinstance(failed_queue_ids, list)
            or not failed_queue_ids
            or failed_queue_ids != sorted(set(failed_queue_ids))
            or any(
                not isinstance(value, str) or not value.strip()
                for value in failed_queue_ids
            )
        ):
            raise AuthoringWorkbenchError(
                "Workspace carry-forward failure selection is invalid"
            )
        source_run_config = carry.get("source_run_config")
        _workspace_run_config_with_policy(source_run_config)
        target_run_config = _workspace_run_config_with_policy(
            workspace.get("run_config")
        )
        if version == 2:
            if (
                target_run_config["backend"] != "pocket-tts"
                or target_run_config["backend"] == source_run_config["backend"]
                or target_run_config["model"] not in {None, "pocket-tts"}
                or target_run_config["generation_profile"] not in {None, "default"}
            ):
                raise AuthoringWorkbenchError(
                    "Workspace offline fallback backend provenance is inconsistent"
                )
        else:
            try:
                repair_policy = FailureRepairPolicy.from_document(
                    target_run_config["failure_repair_policy"]
                )
            except FailureRepairPolicyError as error:
                raise AuthoringWorkbenchError(str(error)) from error
            if set(failed_queue_ids) != set(repair_policy.queue_ids):
                raise AuthoringWorkbenchError(
                    "Workspace carried failure selection differs from repair policy"
                )
            sentence_selected = set(repair_policy.sentence_segment_queue_ids)
            bounded_selected = set(repair_policy.bounded_seed_retry_queue_ids)
            offline_selected = set(repair_policy.offline_fallback_queue_ids)
            inline_pause_selected = set(repair_policy.inline_pause_queue_ids)
            same_backend_selected = (
                sentence_selected | bounded_selected | inline_pause_selected
            )
            if same_backend_selected and offline_selected:
                raise AuthoringWorkbenchError(
                    "Workspace carry-forward mixes incompatible repair backends"
                )
            source_base = dict(source_run_config)
            source_base["failure_repair_policy"] = FailureRepairPolicy().to_document()
            target_base = dict(target_run_config)
            target_base["failure_repair_policy"] = FailureRepairPolicy().to_document()
            if same_backend_selected and source_base != target_base:
                raise AuthoringWorkbenchError(
                    "Workspace same-backend repair provenance is inconsistent"
                )
            if offline_selected and (
                target_run_config["backend"] != "pocket-tts"
                or target_run_config["backend"] == source_run_config["backend"]
                or target_run_config["model"] not in {None, "pocket-tts"}
                or target_run_config["generation_profile"] not in {None, "default"}
            ):
                raise AuthoringWorkbenchError(
                    "Workspace offline fallback backend provenance is inconsistent"
                )
    items = carry.get("items")
    if not isinstance(items, list) or not items:
        raise AuthoringWorkbenchError("Workspace carry-forward item ledger is missing")
    authorities = ()
    authority_by_queue_id = {}
    if version == 4:
        try:
            authorities = validate_offline_fallback_authority_records(
                carry.get("offline_fallback_authorities"),
                directory,
                {
                    item.get("queue_id"): item.get("source_item_sha256")
                    for item in items
                    if isinstance(item, dict) and item.get("mode") == "failed-outcome"
                },
            )
        except OfflineFallbackAuthorityError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        authority_by_queue_id = {
            queue_id: authority
            for authority in authorities
            for queue_id in authority.queue_ids
        }
        if set(authority_by_queue_id) != set(failed_queue_ids):
            raise AuthoringWorkbenchError(
                "Workspace offline fallback authority selection is incomplete"
            )
    seen = set()
    for item in items:
        terminal_fields = {
            "queue_id",
            "mode",
            "source_workspace_id",
            "source_state_sha256",
            "source_item_sha256",
            "audio_sha256",
            "character",
        }
        failed_fields = terminal_fields - {"audio_sha256"} | {
            "source_provider",
            "source_model",
            "source_generation_profile",
            "source_attempts",
            "source_seed",
            "source_failure_kind",
            "source_voice_reference",
        }
        bounded_failed_fields = failed_fields | {"source_provider_attempts"}
        repaired_failed_fields = bounded_failed_fields | {"source_repair_strategy"}
        nested_failed_fields = failed_fields | {"source_parent_carry_forward"}
        nested_bounded_failed_fields = bounded_failed_fields | {
            "source_parent_carry_forward"
        }
        nested_repaired_failed_fields = repaired_failed_fields | {
            "source_parent_carry_forward"
        }
        authority_variants = {
            frozenset(fields | {"source_unresolved_authority"})
            for fields in (
                bounded_failed_fields,
                repaired_failed_fields,
                nested_bounded_failed_fields,
                nested_repaired_failed_fields,
            )
        }
        if (
            not isinstance(item, dict)
            or frozenset(item)
            not in {
                frozenset(terminal_fields),
                frozenset(failed_fields),
                frozenset(bounded_failed_fields),
                frozenset(repaired_failed_fields),
                frozenset(nested_failed_fields),
                frozenset(nested_bounded_failed_fields),
                frozenset(nested_repaired_failed_fields),
            }
            | authority_variants
        ):
            raise AuthoringWorkbenchError("Workspace carry-forward item is malformed")
        queue_id = _required_text(item.get("queue_id"), "Carry-forward queue ID")
        if queue_id in seen:
            raise AuthoringWorkbenchError(
                "Workspace carry-forward queue ID is duplicated"
            )
        seen.add(queue_id)
        mode = item.get("mode")
        if (
            mode not in {"review-only", "full-outcome", "failed-outcome"}
            or item.get("source_workspace_id") != carry["source_workspace_id"]
            or item.get("source_state_sha256") != source_state_sha256
            or (mode != "failed-outcome" and item.get("character") not in characters)
        ):
            raise AuthoringWorkbenchError(
                "Workspace carry-forward item provenance is inconsistent"
            )
        _require_sha256(
            item.get("source_item_sha256"), "Carry-forward source item SHA-256"
        )
        if mode == "failed-outcome":
            if version not in {2, 3, 4} or queue_id not in failed_queue_ids:
                raise AuthoringWorkbenchError(
                    "Workspace carry-forward failure item is not selected"
                )
            _required_text(item.get("source_provider"), "Carry-forward source provider")
            if item.get("source_provider") != carry["source_run_config"]["backend"]:
                raise AuthoringWorkbenchError(
                    "Workspace carry-forward failure backend is inconsistent"
                )
            strategy = (
                repair_policy.strategy_for(queue_id) if version in {3, 4} else None
            )
            authority = authority_by_queue_id.get(queue_id)
            authority_reference = item.get("source_unresolved_authority")
            if version == 4:
                if (
                    authority is None
                    or authority_reference != authority.reference_record(queue_id)
                    or strategy != OFFLINE_FALLBACK_BACKEND
                ):
                    raise AuthoringWorkbenchError(
                        "Workspace offline fallback authority reference is inconsistent"
                    )
            elif authority_reference is not None:
                raise AuthoringWorkbenchError(
                    "Workspace carries an unexpected offline fallback authority"
                )
            allowed_failure_kinds = {"missed_eos_audio_limit"}
            if strategy in {SENTENCE_BOUNDARY_SEGMENTATION, INLINE_PAUSE_MARKER}:
                allowed_failure_kinds.add("speech_silence")
            source_repair_strategy = item.get("source_repair_strategy")
            source_provider_attempts = item.get("source_provider_attempts")
            if (
                strategy == OFFLINE_FALLBACK_BACKEND
                and source_repair_strategy
                in {None, BOUNDED_SEED_RETRY, INLINE_PAUSE_MARKER}
                and isinstance(source_provider_attempts, int)
                and not isinstance(source_provider_attempts, bool)
                and (
                    authority is not None
                    or source_provider_attempts >= MAX_BOUNDED_TOTAL_ATTEMPTS
                )
            ):
                allowed_failure_kinds.add("speech_silence")
            if item.get("source_failure_kind") not in allowed_failure_kinds:
                raise AuthoringWorkbenchError(
                    "Workspace carry-forward failure kind is unsupported"
                )
            source_voice = item.get("source_voice_reference")
            if (
                not isinstance(source_voice, dict)
                or set(source_voice)
                != {"character", "speaker", "aliases", "references"}
                or not isinstance(source_voice.get("references"), list)
                or not source_voice["references"]
            ):
                raise AuthoringWorkbenchError(
                    "Workspace carry-forward source references are invalid"
                )
            attempts = item.get("source_attempts")
            minimum_attempts = MAX_BOUNDED_TOTAL_ATTEMPTS
            if version in {3, 4}:
                minimum_attempts = (
                    MAX_BOUNDED_TOTAL_ATTEMPTS
                    if strategy == OFFLINE_FALLBACK_BACKEND and authority is None
                    else 1
                )
            if (
                not isinstance(attempts, int)
                or isinstance(attempts, bool)
                or attempts < minimum_attempts
            ):
                raise AuthoringWorkbenchError(
                    "Workspace carry-forward failure attempts are invalid"
                )
            if version in {3, 4} and strategy == BOUNDED_SEED_RETRY:
                provider_attempts = source_provider_attempts
                if (
                    not isinstance(provider_attempts, int)
                    or isinstance(provider_attempts, bool)
                    or not 1 <= provider_attempts < 3
                ):
                    raise AuthoringWorkbenchError(
                        "Workspace bounded-seed source attempts are exhausted"
                    )
            if (
                version in {3, 4}
                and strategy == OFFLINE_FALLBACK_BACKEND
                and authority is None
                and source_provider_attempts is not None
                and (
                    not isinstance(source_provider_attempts, int)
                    or isinstance(source_provider_attempts, bool)
                    or source_provider_attempts < MAX_BOUNDED_TOTAL_ATTEMPTS
                )
            ):
                raise AuthoringWorkbenchError(
                    "Workspace offline-fallback source attempts are not exhausted"
                )
            if source_repair_strategy is not None and source_repair_strategy not in {
                BOUNDED_SEED_RETRY,
                INLINE_PAUSE_MARKER,
                SENTENCE_BOUNDARY_SEGMENTATION,
            }:
                raise AuthoringWorkbenchError(
                    "Workspace carry-forward source repair is invalid"
                )
            parent_carry = item.get("source_parent_carry_forward")
            if parent_carry is not None and not isinstance(parent_carry, dict):
                raise AuthoringWorkbenchError(
                    "Workspace nested carry-forward provenance is malformed"
                )
        else:
            _require_sha256(item.get("audio_sha256"), "Carry-forward WAV SHA-256")
    if version in {2, 3, 4} and set(failed_queue_ids) != {
        item["queue_id"] for item in items if item.get("mode") == "failed-outcome"
    }:
        raise AuthoringWorkbenchError(
            "Workspace carry-forward failure ledger is incomplete"
        )


def _validate_workspace_offline_fallback_state(directory, workspace, *, state=None):
    carry = workspace.get("carry_forward")
    if not isinstance(carry, dict) or carry.get("schema_version") not in {2, 3, 4}:
        return
    queue_path = directory / "queue.jsonl"
    state_path = directory / "generated-audio" / "generation-state.json"
    state = _load_workspace_generation_state(state, state_path, queue_path)
    ledger = {
        item["queue_id"]: {
            key: value for key, value in item.items() if key != "queue_id"
        }
        for item in carry["items"]
        if item.get("mode") == "failed-outcome"
    }
    target_provider = workspace["run_config"]["backend"]
    repair_policy = _workspace_failure_repair_policy(workspace)
    for queue_id, expected in ledger.items():
        result = state["items"].get(queue_id)
        if not isinstance(result, dict):
            raise AuthoringWorkbenchError(
                f"Workspace offline fallback state is missing {queue_id!r}"
            )
        observed = result.get("carry_forward")
        repair = result.get("failure_repair")
        if observed is None and isinstance(repair, dict):
            observed = repair.get("source_failure")
        strategy = repair_policy.strategy_for(queue_id)
        if observed != expected:
            raise AuthoringWorkbenchError(
                f"Workspace carried failure source changed for {queue_id!r}"
            )
        transitioned_same_backend_repair = (
            strategy
            in {
                SENTENCE_BOUNDARY_SEGMENTATION,
                BOUNDED_SEED_RETRY,
                INLINE_PAUSE_MARKER,
            }
            and isinstance(repair, dict)
            and repair.get("strategy") == strategy
        )
        if transitioned_same_backend_repair:
            if result.get("provider") != target_provider:
                raise AuthoringWorkbenchError(
                    f"Workspace same-backend repair provider changed for {queue_id!r}"
                )
        elif result.get("provider") == expected["source_provider"]:
            source_result = copy.deepcopy(result)
            source_result.pop("carry_forward", None)
            parent_carry = expected.get("source_parent_carry_forward")
            if parent_carry is not None:
                source_result["carry_forward"] = copy.deepcopy(parent_carry)
            if (
                canonical_document_sha256(source_result)
                != expected["source_item_sha256"]
            ):
                raise AuthoringWorkbenchError(
                    f"Workspace carried failure changed for {queue_id!r}"
                )
        elif result.get("provider") != target_provider:
            raise AuthoringWorkbenchError(
                f"Workspace offline fallback provider changed for {queue_id!r}"
            )


def _load_workspace_generation_state(state, state_path, queue_path):
    if state is not None:
        return state
    try:
        return load_generation_state(state_path, queue_path)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _validate_workspace_input_config(directory, workspace, import_snapshot):
    story = workspace.get("story_index")
    if story is not None:
        if not isinstance(story, dict) or set(story) != {
            "path",
            "sha256",
            "legacy_sha256_at_import",
            "matches_legacy",
        }:
            raise AuthoringWorkbenchError(
                "Workspace story snapshot binding is malformed"
            )
        path = _within(
            directory,
            _safe_relative(story["path"], "Story index snapshot"),
            "Story index snapshot",
        )
        if story["path"] != "inputs/story-index.jsonl" or not path.is_file():
            raise AuthoringWorkbenchError("Workspace story snapshot path was modified")
        if sha256_file(path) != _require_sha256(
            story["sha256"], "Story index snapshot SHA-256"
        ):
            raise AuthoringWorkbenchError("Workspace story snapshot was modified")
        legacy_digest = _legacy_input_digest(import_snapshot, "story_index")
        if story["legacy_sha256_at_import"] != legacy_digest or story[
            "matches_legacy"
        ] != (legacy_digest == story["sha256"] if legacy_digest else None):
            raise AuthoringWorkbenchError(
                "Workspace story provenance claim was modified"
            )
    voice = workspace.get("voice_manifest")
    if voice is not None:
        if not isinstance(voice, dict) or set(voice) != {
            "path",
            "sha256",
            "controls",
            "legacy_sha256_at_import",
            "matches_legacy",
        }:
            raise AuthoringWorkbenchError(
                "Workspace voice snapshot binding is malformed"
            )
        if voice["path"] != "inputs/voice/manifest.json":
            raise AuthoringWorkbenchError("Workspace voice snapshot path was modified")
        controls = voice.get("controls")
        if not isinstance(controls, list) or any(
            not isinstance(value, dict) or set(value) != {"path", "sha256"}
            for value in controls
        ):
            raise AuthoringWorkbenchError("Workspace voice controls are malformed")
        legacy_digest = _legacy_input_digest(import_snapshot, "voice_manifest")
        if voice["legacy_sha256_at_import"] != legacy_digest or voice[
            "matches_legacy"
        ] != (legacy_digest == voice["sha256"] if legacy_digest else None):
            raise AuthoringWorkbenchError(
                "Workspace voice provenance claim was modified"
            )
        manifest_path = directory / "inputs" / "voice" / "manifest.json"
        if not manifest_path.is_file() or sha256_file(manifest_path) != _require_sha256(
            voice["sha256"], "Voice manifest snapshot SHA-256"
        ):
            raise AuthoringWorkbenchError(
                "Workspace voice manifest snapshot was modified"
            )
        try:
            _document, entries = load_voice_manifest(manifest_path)
        except VoiceManifestError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        expected_controls = []
        seen = set()
        for entry in entries:
            for value in entry.references:
                relative = _safe_relative(value, "Voice reference")
                control_path = (Path("inputs") / "voice" / relative).as_posix()
                if control_path in seen:
                    continue
                seen.add(control_path)
                reference = _within(directory, Path(control_path), "Voice reference")
                if not reference.is_file():
                    raise AuthoringWorkbenchError(
                        "Workspace voice reference snapshot is missing"
                    )
                expected_controls.append(
                    {"path": control_path, "sha256": sha256_file(reference)}
                )
        if controls != expected_controls:
            raise AuthoringWorkbenchError(
                "Workspace voice control inventory was modified"
            )


def _validate_workspace_failure_reference_binding(directory, workspace):
    config = workspace.get("failure_reference_binding")
    if config is None:
        return
    fields = {
        "path",
        "sha256",
        "binding_id",
        "controls",
        "base_workspace_id",
        "base_workspace_sha256",
        "base_state_sha256",
    }
    if not isinstance(config, dict) or set(config) != fields:
        raise AuthoringWorkbenchError(
            "Workspace failure-reference binding is malformed"
        )
    if config["path"] != "inputs/failure-reference-binding/binding.json":
        raise AuthoringWorkbenchError(
            "Workspace failure-reference binding path was modified"
        )
    for field in (
        "sha256",
        "binding_id",
        "base_workspace_sha256",
        "base_state_sha256",
    ):
        _require_sha256(config[field], f"Failure-reference {field}")
    base_workspace_id = config.get("base_workspace_id")
    if not isinstance(base_workspace_id, str) or not re.fullmatch(
        r"resume-[0-9a-f]{24}-[0-9a-f]{16}", base_workspace_id
    ):
        raise AuthoringWorkbenchError(
            "Workspace failure-reference base identity is malformed"
        )
    binding_path = _within(
        directory,
        _safe_relative(config["path"], "Failure-reference binding"),
        "Failure-reference binding",
    )
    if (
        binding_path.is_symlink()
        or not binding_path.is_file()
        or sha256_file(binding_path) != config["sha256"]
    ):
        raise AuthoringWorkbenchError(
            "Workspace failure-reference binding snapshot was modified"
        )
    try:
        binding = load_failure_reference_binding(binding_path.parent)
        document = load_failure_reference_binding_document(binding.directory)
    except FailureReferenceBindingError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    if binding.binding_id != config["binding_id"]:
        raise AuthoringWorkbenchError(
            "Workspace failure-reference binding identity was modified"
        )
    source = document["source_authority"]
    voice = workspace.get("voice_manifest")
    compatible_queue_sha256s = {
        workspace_queue_sha256(workspace, error_type=AuthoringWorkbenchError)
    }
    queue_extension = workspace.get("queue_extension")
    if isinstance(queue_extension, dict):
        compatible_queue_sha256s.add(queue_extension.get("base_queue_sha256"))
    if (
        source["queue_sha256"] not in compatible_queue_sha256s
        or not isinstance(voice, dict)
        or source["voice_manifest_sha256"] != voice.get("sha256")
    ):
        raise AuthoringWorkbenchError(
            "Workspace failure-reference binding controls differ from its workspace"
        )
    expected_controls = []
    for group in document["groups"]:
        relative = (
            Path("inputs")
            / "failure-reference-binding"
            / _safe_relative(group["reference"], "Selected reference")
        )
        control_path = _within(directory, relative, "Selected reference")
        if (
            control_path.is_symlink()
            or not control_path.is_file()
            or sha256_file(control_path) != group["reference_sha256"]
        ):
            raise AuthoringWorkbenchError(
                "Workspace selected-reference snapshot was modified"
            )
        expected_controls.append(
            {
                "path": relative.as_posix(),
                "sha256": group["reference_sha256"],
            }
        )
    if config.get("controls") != expected_controls:
        raise AuthoringWorkbenchError(
            "Workspace failure-reference control inventory was modified"
        )


def _stable_workspace_state(directory, workspace, label):
    return load_stable_workspace_generation_state(
        directory,
        workspace,
        label,
        error_type=AuthoringWorkbenchError,
    )


def _load_workspace_snapshot(workspace_directory, label):
    candidate = Path(workspace_directory).expanduser().resolve()
    document, digest, _payload = _load_json_snapshot(
        candidate / "workspace.json", f"outcome merge {label} workspace"
    )
    directory, validated = _load_workspace(candidate)
    if document != validated or sha256_file(directory / "workspace.json") != digest:
        raise AuthoringWorkbenchError(
            f"Outcome merge {label} workspace changed while it was loaded"
        )
    return directory, document, digest


def load_workspace_authority(workspace_directory):
    """Load one fully validated workspace from an exact document snapshot."""
    return _load_workspace_snapshot(workspace_directory, "authority")


def _validate_workspace_outcome_merge(directory, workspace, *, state=None):
    merge = workspace.get("outcome_merge")
    if merge is None:
        return
    version = merge.get("schema_version") if isinstance(merge, dict) else None
    fields = {
        "schema",
        "schema_version",
        "base_workspace_id",
        "base_state_sha256",
        "sources",
        "items",
    }
    if version == 2:
        fields.add("source_reconciliation_id")
    if (
        not isinstance(merge, dict)
        or set(merge) != fields
        or merge.get("schema") != "vntts.authoring-workspace-outcome-merge"
        or version not in {1, 2}
        or not re.fullmatch(
            r"resume-[0-9a-f]{24}-[0-9a-f]{16}",
            str(merge.get("base_workspace_id", "")),
        )
    ):
        raise AuthoringWorkbenchError("Workspace outcome merge provenance is malformed")
    _require_sha256(merge.get("base_state_sha256"), "Outcome merge base state SHA-256")
    if version == 2:
        _require_sha256(
            merge.get("source_reconciliation_id"),
            "Outcome merge reconciliation ID",
        )
    sources = merge.get("sources")
    if not isinstance(sources, list) or not sources:
        raise AuthoringWorkbenchError("Workspace outcome merge source ledger is empty")
    source_by_id = {}
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "workspace_id",
            "config_fingerprint",
            "state_sha256",
            "terminal_item_count",
        }:
            raise AuthoringWorkbenchError("Workspace outcome merge source is malformed")
        workspace_id = source.get("workspace_id")
        if (
            not isinstance(workspace_id, str)
            or not re.fullmatch(r"resume-[0-9a-f]{24}-[0-9a-f]{16}", workspace_id)
            or workspace_id in source_by_id
            or workspace_id == merge["base_workspace_id"]
        ):
            raise AuthoringWorkbenchError(
                "Workspace outcome merge source identity is invalid"
            )
        _require_sha256(
            source.get("config_fingerprint"),
            "Outcome merge source configuration fingerprint",
        )
        _require_sha256(
            source.get("state_sha256"), "Outcome merge source state SHA-256"
        )
        count = source.get("terminal_item_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise AuthoringWorkbenchError(
                "Workspace outcome merge source count is invalid"
            )
        source_by_id[workspace_id] = source
    if sources != sorted(sources, key=lambda value: value["workspace_id"]):
        raise AuthoringWorkbenchError(
            "Workspace outcome merge sources are not canonical"
        )
    items = merge.get("items")
    if not isinstance(items, list) or not items:
        raise AuthoringWorkbenchError("Workspace outcome merge item ledger is empty")
    terminal_merge = workspace.get("terminal_conflict_merge")
    terminal_items = (
        terminal_merge.get("items") if isinstance(terminal_merge, dict) else None
    )
    terminal_queue_ids = (
        {
            value.get("queue_id")
            for value in terminal_items
            if isinstance(value, dict) and isinstance(value.get("queue_id"), str)
        }
        if isinstance(terminal_items, list)
        else set()
    )
    audio_event_config = workspace.get("audio_event_composition")
    audio_event_queue_id = (
        audio_event_config.get("queue_id")
        if isinstance(audio_event_config, dict)
        else None
    )
    reviewed_rejection = workspace.get("reviewed_rejection_live_fallback")
    reviewed_rejection_items = (
        reviewed_rejection.get("items")
        if isinstance(reviewed_rejection, dict)
        else None
    )
    reviewed_rejection_queue_ids = (
        {
            value.get("queue_id")
            for value in reviewed_rejection_items
            if isinstance(value, dict) and isinstance(value.get("queue_id"), str)
        }
        if isinstance(reviewed_rejection_items, list)
        else set()
    )
    queue_ids = []
    counts = Counter()
    if state is None:
        try:
            state = load_generation_state(
                directory / "generated-audio/generation-state.json",
                directory / "queue.jsonl",
            )
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "queue_id",
            "source_workspace_id",
            "source_state_sha256",
            "source_item_sha256",
            "audio_sha256",
            "status",
            "review_status",
        }:
            raise AuthoringWorkbenchError("Workspace outcome merge item is malformed")
        queue_id = _required_text(item.get("queue_id"), "Outcome merge queue ID")
        source = source_by_id.get(item.get("source_workspace_id"))
        if (
            source is None
            or item.get("source_state_sha256") != source["state_sha256"]
            or (item.get("status"), item.get("review_status"))
            not in {("approved", "approved"), ("generated", "rejected")}
        ):
            raise AuthoringWorkbenchError(
                "Workspace outcome merge item provenance is inconsistent"
            )
        source_item_sha256 = _require_sha256(
            item.get("source_item_sha256"), "Outcome merge source item SHA-256"
        )
        audio_sha256 = _require_sha256(
            item.get("audio_sha256"), "Outcome merge WAV SHA-256"
        )
        if queue_id == audio_event_queue_id:
            queue_ids.append(queue_id)
            counts[item["source_workspace_id"]] += 1
            continue
        result = state["items"].get(queue_id)
        if not isinstance(result, dict) or not _terminal_review_outcome(result):
            raise AuthoringWorkbenchError(
                f"Workspace outcome merge result is not terminal for {queue_id!r}"
            )
        observed = result.get("outcome_merge")
        expected = {key: value for key, value in item.items() if key != "queue_id"}
        if observed != expected:
            raise AuthoringWorkbenchError(
                f"Workspace outcome merge result changed for {queue_id!r}"
            )
        source_result = copy.deepcopy(result)
        source_result.pop("outcome_merge", None)
        if queue_id in terminal_queue_ids:
            source_result.pop("terminal_conflict_resolution", None)
        if queue_id in reviewed_rejection_queue_ids:
            fallback = source_result.pop("live_fallback", None)
            evidence = fallback.get("evidence") if isinstance(fallback, dict) else None
            base_result = (
                copy.deepcopy(evidence.get("base_result"))
                if isinstance(evidence, dict)
                and isinstance(evidence.get("base_result"), dict)
                else None
            )
            if base_result is None:
                raise AuthoringWorkbenchError(
                    f"Workspace merged fallback evidence changed for {queue_id!r}"
                )
            base_result.pop("outcome_merge", None)
            if queue_id in terminal_queue_ids:
                base_result.pop("terminal_conflict_resolution", None)
            if "updated_at" in base_result:
                source_result["updated_at"] = base_result["updated_at"]
            else:
                source_result.pop("updated_at", None)
            if source_result != base_result:
                raise AuthoringWorkbenchError(
                    f"Workspace merged fallback base changed for {queue_id!r}"
                )
        if canonical_document_sha256(source_result) != source_item_sha256:
            raise AuthoringWorkbenchError(
                f"Workspace merged source item changed for {queue_id!r}"
            )
        audio_path = _within(
            directory / "generated-audio",
            _safe_relative(result.get("path"), "Outcome merge WAV path"),
            "Outcome merge WAV",
        )
        if not audio_path.is_file() or sha256_file(audio_path) != audio_sha256:
            raise AuthoringWorkbenchError(
                f"Workspace outcome merge WAV changed for {queue_id!r}"
            )
        queue_ids.append(queue_id)
        counts[item["source_workspace_id"]] += 1
    if queue_ids != sorted(set(queue_ids)):
        raise AuthoringWorkbenchError(
            "Workspace outcome merge item ledger is not canonical"
        )
    if any(
        counts[workspace_id] != source["terminal_item_count"]
        for workspace_id, source in source_by_id.items()
    ):
        raise AuthoringWorkbenchError(
            "Workspace outcome merge source counts are inconsistent"
        )


def _validate_workspace_terminal_conflict_merge(directory, workspace, *, state=None):
    merge = workspace.get("terminal_conflict_merge")
    if merge is None:
        return
    fields = {
        "schema",
        "schema_version",
        "base_workspace_id",
        "base_state_sha256",
        "source_report_id",
        "source_reconciliation_sha256",
        "terminal_resolution_id",
        "terminal_resolution_sha256",
        "terminal_successor_id",
        "terminal_successor_sha256",
        "sources",
        "items",
    }
    if (
        not isinstance(merge, dict)
        or set(merge) != fields
        or merge.get("schema") != "vntts.authoring-terminal-conflict-workspace-merge"
        or merge.get("schema_version") != 1
    ):
        raise AuthoringWorkbenchError(
            "Workspace terminal conflict merge provenance is malformed"
        )
    base_workspace_id = _required_text(
        merge.get("base_workspace_id"), "Terminal conflict base workspace ID"
    )
    if not re.fullmatch(r"resume-[0-9a-f]{24}-[0-9a-f]{16}", base_workspace_id):
        raise AuthoringWorkbenchError(
            "Workspace terminal conflict base identity is invalid"
        )
    for field in (
        "base_state_sha256",
        "source_report_id",
        "source_reconciliation_sha256",
        "terminal_resolution_id",
        "terminal_resolution_sha256",
        "terminal_successor_id",
        "terminal_successor_sha256",
    ):
        _require_sha256(
            merge.get(field),
            f"Terminal conflict {field.replace('_', ' ')}",
        )
    sources = merge.get("sources")
    if not isinstance(sources, list) or not sources:
        raise AuthoringWorkbenchError(
            "Workspace terminal conflict source ledger is empty"
        )
    source_by_id = {}
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "workspace_id",
            "config_fingerprint",
            "state_sha256",
            "terminal_item_count",
        }:
            raise AuthoringWorkbenchError(
                "Workspace terminal conflict source is malformed"
            )
        workspace_id = _required_text(
            source.get("workspace_id"), "Terminal conflict source workspace ID"
        )
        if (
            not re.fullmatch(r"resume-[0-9a-f]{24}-[0-9a-f]{16}", workspace_id)
            or workspace_id in source_by_id
        ):
            raise AuthoringWorkbenchError(
                "Workspace terminal conflict source identity is invalid"
            )
        _require_sha256(
            source.get("config_fingerprint"),
            "Terminal conflict source configuration fingerprint",
        )
        _require_sha256(
            source.get("state_sha256"),
            "Terminal conflict source state SHA-256",
        )
        count = source.get("terminal_item_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise AuthoringWorkbenchError(
                "Workspace terminal conflict source count is invalid"
            )
        source_by_id[workspace_id] = source
    if sources != sorted(sources, key=lambda value: value["workspace_id"]):
        raise AuthoringWorkbenchError(
            "Workspace terminal conflict sources are not canonical"
        )
    items = merge.get("items")
    if not isinstance(items, list) or not items:
        raise AuthoringWorkbenchError(
            "Workspace terminal conflict item ledger is empty"
        )
    if state is None:
        try:
            state = load_generation_state(
                directory / "generated-audio/generation-state.json",
                directory / "queue.jsonl",
            )
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
    queue_ids = []
    counts = Counter()
    for item in items:
        item_fields = {
            "queue_id",
            "source_workspace_id",
            "source_state_sha256",
            "source_item_sha256",
            "audio_sha256",
            "status",
            "review_status",
            "selected_candidate_id",
            "next_action",
        }
        if not isinstance(item, dict) or set(item) != item_fields:
            raise AuthoringWorkbenchError(
                "Workspace terminal conflict item is malformed"
            )
        queue_id = _required_text(item.get("queue_id"), "Terminal conflict queue ID")
        source = source_by_id.get(item.get("source_workspace_id"))
        if (
            source is None
            or item.get("source_state_sha256") != source["state_sha256"]
            or (item.get("status"), item.get("review_status"))
            not in {("approved", "approved"), ("generated", "rejected")}
            or item.get("next_action")
            not in {
                "apply_selected_approved_outcome",
                "retain_explicit_rejection",
            }
        ):
            raise AuthoringWorkbenchError(
                "Workspace terminal conflict item provenance is inconsistent"
            )
        _require_sha256(
            item.get("source_item_sha256"),
            "Terminal conflict source item SHA-256",
        )
        _require_sha256(item.get("audio_sha256"), "Terminal conflict WAV SHA-256")
        _require_sha256(
            item.get("selected_candidate_id"),
            "Terminal conflict selected candidate ID",
        )
        expected_action = (
            "apply_selected_approved_outcome"
            if item["review_status"] == "approved"
            else "retain_explicit_rejection"
        )
        if item["next_action"] != expected_action:
            raise AuthoringWorkbenchError(
                "Workspace terminal conflict action is inconsistent"
            )
        result = state["items"].get(queue_id)
        if not isinstance(result, dict) or not _terminal_review_outcome(result):
            raise AuthoringWorkbenchError(
                f"Workspace terminal conflict result is not terminal for {queue_id!r}"
            )
        observed = result.get("terminal_conflict_resolution")
        expected = {key: value for key, value in item.items() if key != "queue_id"}
        if observed != expected:
            raise AuthoringWorkbenchError(
                f"Workspace terminal conflict result changed for {queue_id!r}"
            )
        source_result = copy.deepcopy(result)
        source_result.pop("terminal_conflict_resolution", None)
        if canonical_document_sha256(source_result) != item["source_item_sha256"]:
            raise AuthoringWorkbenchError(
                f"Workspace terminal conflict source item changed for {queue_id!r}"
            )
        audio_path = _within(
            directory / "generated-audio",
            _safe_relative(result.get("path"), "Terminal conflict WAV path"),
            "Terminal conflict WAV",
        )
        if not audio_path.is_file() or sha256_file(audio_path) != item["audio_sha256"]:
            raise AuthoringWorkbenchError(
                f"Workspace terminal conflict WAV changed for {queue_id!r}"
            )
        queue_ids.append(queue_id)
        counts[item["source_workspace_id"]] += 1
    if queue_ids != sorted(set(queue_ids)):
        raise AuthoringWorkbenchError(
            "Workspace terminal conflict item ledger is not canonical"
        )
    if any(
        counts[workspace_id] != source["terminal_item_count"]
        for workspace_id, source in source_by_id.items()
    ):
        raise AuthoringWorkbenchError(
            "Workspace terminal conflict source counts are inconsistent"
        )


def _workspace_run_config_with_policy(run_config):
    return normalize_workspace_run_config(
        run_config,
        error_type=AuthoringWorkbenchError,
    )


def _workspace_failure_repair_policy(workspace):
    return workspace_failure_repair_policy(
        workspace,
        error_type=AuthoringWorkbenchError,
    )


def _load_json(path, description):
    return load_json_object(path, description, error_type=AuthoringWorkbenchError)


def _load_json_snapshot(path, description):
    return load_json_object_snapshot(
        path, description, error_type=AuthoringWorkbenchError
    )


def _safe_relative(value, label):
    return safe_relative_path(value, label, error_type=AuthoringWorkbenchError)


def _within(root, relative, label):
    return contained_path(root, relative, label, error_type=AuthoringWorkbenchError)


def _require_sha256(value, label):
    return require_sha256(value, label, error_type=AuthoringWorkbenchError)


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AuthoringWorkbenchError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "_external_input",
    "_legacy_input_digest",
    "_load_bound_workspace_queue",
    "_load_json",
    "_load_json_snapshot",
    "_load_workspace",
    "_load_workspace_generation_state",
    "_load_workspace_scoped",
    "_load_workspace_snapshot",
    "_parse_history_timestamp",
    "_require_sha256",
    "_required_text",
    "_safe_relative",
    "_stable_workspace_state",
    "_validate_import_history",
    "_validate_workspace_carry_forward",
    "_validate_workspace_failure_reference_binding",
    "_validate_workspace_input_config",
    "_validate_workspace_offline_fallback_state",
    "_validate_workspace_outcome_merge",
    "_validate_workspace_queue_extension",
    "_validate_workspace_terminal_conflict_merge",
    "_within",
    "_workspace_failure_repair_policy",
    "_workspace_run_config_with_policy",
    "_workspace_title",
    "load_workspace_authority",
    "validate_workspace_provenance_extensions",
]

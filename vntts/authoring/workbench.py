"""Safe mutable workspaces and truthful status for graphical authoring."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import re
import shutil
import socket
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from statistics import median

from platformdirs import user_data_path
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

from vntts.authoring import legacy_import
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
    ReviewAuthority,
    ReviewCommit,
    inline_pause_matches_failure,
    inspect_generated_wav,
    is_spoken_queue_item,
    load_generation_state,
    load_review_audio_bytes,
    normalize_short_trailing_ellipsis,
    normalized_failure_record,
    publish_generated_manifest,
    review_generation_item,
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
from vntts.authoring.game_pack import (
    FinalGamePackError,
    _rename_directory_no_replace,
)
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
from vntts.authoring.offline_fallback_authority import (
    OfflineFallbackAuthorityError,
    load_offline_fallback_authorities,
    validate_offline_fallback_authority_records,
)
from vntts.authoring.publication import generation_publication_leases
from vntts.authoring.queue_extension import (
    WORKSPACE_SCHEMA as QUEUE_EXTENSION_WORKSPACE_SCHEMA,
)
from vntts.authoring.queue_extension import (
    WORKSPACE_VERSION as QUEUE_EXTENSION_WORKSPACE_VERSION,
)
from vntts.authoring.queue_extension import (
    QueueExtensionError,
    validate_additive_generation_queue,
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
    SPEECH_QUALITY_ANALYSIS_VERSION,
    measure_generated_speech_bytes,
)
from vntts.authoring.terminal_conflict_records import is_terminal_review_outcome
from vntts.authoring.workspace_config import (
    normalize_workspace_run_config,
    selected_voice_manifest_path,
    workspace_audio_event_spoken_projection_queue_ids,
    workspace_config_fingerprint,
    workspace_failure_repair_policy,
    workspace_missing_voice_policy,
    workspace_queue_sha256,
)
from vntts.authoring.workspace_foundation import (
    contained_path,
    copy_workspace_tree_snapshot,
    load_json_object,
    load_json_object_snapshot,
    read_regular_file,
    require_sha256,
    safe_relative_path,
)
from vntts.authoring.workspace_state import load_stable_workspace_generation_state
from vntts.authoring.workspace_voice_runtime import (
    FailureReferenceRuntimeBinding,
    load_failure_reference_runtime_binding,
    load_workspace_queue_voice_overrides,
    load_workspace_voice_registry,
)
from vntts.voices import CharacterVoiceRegistry, synthesis_character_for_line

_canonical_sha256 = canonical_document_sha256

WORKSPACE_SCHEMA = "vntts.authoring-workspace"
WORKSPACE_VERSION = 1
REVIEW_ATTENTION_POLICY_VERSION = 2
REVIEW_NOTABLE_SILENCE_RATIO = 0.30
REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS = 1.0
PACE_MINIMUM_WORDS = 5
PACE_MINIMUM_LENGTH_BUCKET_SAMPLES = 3
PACE_MINIMUM_VOICE_SAMPLES = 5
PACE_SLOW_RELATIVE_RATIO = 0.80
PACE_SLOW_MINIMUM_DELTA_WPM = 20.0
_IMPORT_ID_PATTERN = re.compile(r"legacy-[0-9a-f]{24}")


class AuthoringWorkbenchError(RuntimeError):
    """A workspace or authoring action is unsafe or inconsistent."""


class AuthoringRuntimeStatus(str, Enum):
    READY = "ready"
    RUNNING_HERE = "running_here"
    RUNNING_EXTERNAL = "running_external"
    INTERRUPTED = "interrupted"
    NEEDS_REVIEW = "needs_review"
    NEEDS_ATTENTION = "needs_attention"
    COMPLETE = "complete"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WorkspaceCreationResult:
    directory: Path
    created: bool


@dataclass(frozen=True)
class ActiveAttempt:
    queue_id: str | None
    line_id: str | None
    speaker: str | None
    text: str | None
    phase: str | None
    attempt: int | None
    attempt_limit: int | None
    total_attempts: int | None
    seed: int | None
    started_at: str | None
    updated_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class WorkspaceSummary:
    directory: Path
    title: str
    runtime_status: AuthoringRuntimeStatus
    queue_items: int
    eligible: int
    pending: int
    generated: int
    approved: int
    rejected: int
    live_fallback: int
    omitted: int
    failed: int
    skipped_actions: int
    skipped_sound_effects: int
    recoverable_source_audio: int
    manual_review: int
    resolve_audio: int
    missing_voice: int | None
    blocked_reasons: tuple[str, ...]
    active: ActiveAttempt | None
    failure_reasons: tuple[tuple[str, int], ...]
    queue: Path
    output: Path
    state: Path | None
    voice_manifest: Path | None
    latest_line: str | None
    latest_text: str | None
    latest_status: str | None
    latest_updated_at: str | None

    def to_dict(self):
        payload = asdict(self)
        for field in ("directory", "queue", "output", "state", "voice_manifest"):
            value = payload[field]
            payload[field] = None if value is None else str(value)
        return payload


@dataclass(frozen=True)
class ReviewItem:
    queue_id: str
    line_id: str
    speaker: str
    voice_character: str
    text: str
    status: str
    review_status: str | None
    attempts: int
    seed: int | None
    last_error: str | None
    audio: Path | None
    collection_id: str | None = None
    authority: ReviewAuthority | None = None
    state: Path | None = None
    queue: Path | None = None
    duration_seconds: float | None = None
    words_per_minute: float | None = None
    peak: float | None = None
    technical_flags: tuple[str, ...] = ()
    pace_baseline_wpm: float | None = None
    pace_ratio: float | None = None
    pace_baseline_scope: str | None = None
    pace_advisories: tuple[str, ...] = ()
    failure_category: str | None = None
    internal_pause_seconds: float | None = None
    repair_strategy: str | None = None


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


@dataclass(frozen=True)
class GenerationReadiness:
    selected: int
    pending: int
    failed: int
    ready: int
    missing_voice: int | None
    blocked_reasons: tuple[str, ...]
    queue_ids: tuple[str, ...]


@dataclass(frozen=True)
class CollectionSelection:
    collection_ids: tuple[str, ...]
    collection_count: int
    story_records: int
    queue_items: int
    queue_ids: tuple[str, ...]
    readiness: GenerationReadiness


@dataclass(frozen=True)
class ImmutableHistoryTimestamp:
    kind: str
    instant: str
    display: str


@dataclass(frozen=True)
class WorkspaceCollection:
    collection_id: str
    title: str
    kind: str
    record_count: int


@dataclass(frozen=True)
class WorkspaceVoice:
    character: str
    speaker: str
    aliases: tuple[str, ...]
    references: tuple[Path, ...]


def default_workspaces_root():
    return (
        user_data_path("VisualNovelTextToSpeech", appauthor=False)
        / "authoring"
        / "workspaces"
    )


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
    staging = Path(tempfile.mkdtemp(prefix=".resume-staging-", dir=root)).resolve()
    _within(root, Path(staging.name), "Workspace staging directory")
    try:
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
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
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
    staging = Path(
        tempfile.mkdtemp(prefix=".reference-binding-staging-", dir=root)
    ).resolve()
    _within(root, Path(staging.name), "Failure-reference staging directory")
    base_snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (base_directory / "generated-audio/generation-state.json", state_sha256),
        (base_directory / "queue.jsonl", queue_sha256),
    ]
    binding_snapshots = []
    try:
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
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
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
    staging = Path(tempfile.mkdtemp(prefix=".audio-event-staging-", dir=root)).resolve()
    _within(root, Path(staging.name), "Audio-event staging directory")
    base_snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (base_directory / "generated-audio/generation-state.json", state_sha256),
        (base_directory / "queue.jsonl", queue_sha256),
        (previous_audio, previous_audio_sha256),
    ]
    composition_snapshots = []
    try:
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
                staging = None
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return WorkspaceCreationResult(destination, True)


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


def _merge_workspace_outcomes(
    base_workspace,
    outcome_workspaces,
    workspaces_root,
    *,
    reconciliation_selection,
):
    """Assemble one exact terminal-outcome successor."""
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
    base_items = base_state["items"]
    base_queue_by_id = {item.queue_id: item for item in base_queue.items}
    merged_items = {}
    source_records = []
    source_snapshots = []
    source_audio = {}
    for source_value in source_values:
        (
            source_directory,
            source_document,
            source_workspace_sha256,
        ) = _load_workspace_snapshot(source_value, "source")
        if source_document["source"] != base_document["source"]:
            raise AuthoringWorkbenchError(
                "Outcome merge workspaces must share one immutable import"
            )
        source_queue, source_state, _payload, source_state_sha256 = (
            _stable_workspace_state(source_directory, source_document, "source")
        )
        if (
            sha256_file(source_directory / "queue.jsonl") != base_queue_sha256
            or source_queue.metadata != base_queue.metadata
            or [item.document for item in source_queue.items]
            != [item.document for item in base_queue.items]
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
                or source_report["queue_sha256"] != base_queue_sha256
                or source_report["state_sha256"] != source_state_sha256
            ):
                raise AuthoringWorkbenchError(
                    "Reconciliation terminal source authority changed"
                )
            selected_ids = sorted(selected_records)
        source_record = {
            "workspace_id": source_document["workspace_id"],
            "config_fingerprint": _require_sha256(
                source_document.get("config_fingerprint"),
                "Outcome merge source configuration fingerprint",
            ),
            "state_sha256": source_state_sha256,
        }
        terminal_count = 0
        for queue_id in selected_ids:
            result = source_state["items"].get(queue_id)
            if not isinstance(result, dict) or not _terminal_review_outcome(result):
                continue
            if queue_id in merged_items:
                raise AuthoringWorkbenchError(
                    f"Outcome merge has conflicting sources for {queue_id!r}"
                )
            base_result = base_items.get(queue_id)
            if reconciliation_selection is None:
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
                    != base_document["workspace_id"]
                    or not isinstance(base_result, dict)
                    or root_source_failure.get("source_item_sha256")
                    != canonical_document_sha256(base_result)
                ):
                    raise AuthoringWorkbenchError(
                        f"Outcome merge source authority is stale for {queue_id!r}"
                    )
            else:
                expected = selected_records[queue_id]
                action = expected["action"]
                source = expected["source"]
                queue_item = base_queue_by_id.get(queue_id)
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
                    or source["workspace_id"] != source_document["workspace_id"]
                    or source["authority"] != authority
                    or source["state_item_sha256"] != canonical_document_sha256(result)
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
                source_directory / "generated-audio",
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
                "source_workspace_id": source_document["workspace_id"],
                "source_state_sha256": source_state_sha256,
                "source_item_sha256": canonical_document_sha256(result),
                "audio_sha256": audio_sha256,
                "status": result["status"],
                "review_status": result["review_status"],
            }
            merged_items[queue_id] = (copy.deepcopy(result), ledger)
            source_audio[queue_id] = (audio_path, audio_payload, relative)
            source_snapshots.append((audio_path, audio_sha256))
            terminal_count += 1
        if terminal_count == 0:
            raise AuthoringWorkbenchError(
                f"Outcome merge source {source_document['workspace_id']!r} has no reviewed repair outcomes"
            )
        source_record["terminal_item_count"] = terminal_count
        source_records.append(source_record)
        if len({value["workspace_id"] for value in source_records}) != len(
            source_records
        ):
            raise AuthoringWorkbenchError(
                "Outcome merge source workspace identity is duplicated"
            )
        source_snapshots.append(
            (
                source_directory / "generated-audio/generation-state.json",
                source_state_sha256,
            )
        )
        source_snapshots.append(
            (source_directory / "workspace.json", source_workspace_sha256)
        )

    source_records.sort(key=lambda value: value["workspace_id"])
    ledger_items = [merged_items[key][1] for key in sorted(merged_items)]
    outcome_merge = {
        "schema": "vntts.authoring-workspace-outcome-merge",
        "schema_version": 2 if reconciliation_selection is not None else 1,
        "base_workspace_id": base_document["workspace_id"],
        "base_state_sha256": base_state_sha256,
        "sources": source_records,
        "items": ledger_items,
    }
    if reconciliation_selection is not None:
        outcome_merge["source_reconciliation_id"] = reconciliation_selection[
            "report_id"
        ]
    config_fingerprint = _workspace_config_fingerprint(
        base_document["source"]["import_id"],
        base_document.get("story_index"),
        base_document.get("voice_manifest"),
        base_document["narrator_character"],
        base_document["run_config"],
        base_document.get("carry_forward"),
        outcome_merge,
        base_document.get("failure_reference_binding"),
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
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = _within(root, Path(workspace_id), "Outcome merge destination")
    staging = Path(tempfile.mkdtemp(prefix=".merge-staging-", dir=root)).resolve()
    _within(root, Path(staging.name), "Outcome merge staging directory")
    base_snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (
            base_directory / "generated-audio/generation-state.json",
            base_state_sha256,
        ),
    ]
    try:
        for tree_name in ("provenance", "inputs"):
            _copy_workspace_tree_snapshot(
                base_directory / tree_name,
                staging / tree_name,
                base_snapshots,
            )
        queue_payload = _read_file_bytes(
            base_directory / "queue.jsonl", "outcome merge base queue"
        )
        (staging / "queue.jsonl").write_bytes(queue_payload)
        base_snapshots.append((base_directory / "queue.jsonl", base_queue_sha256))
        output = staging / "generated-audio"
        output.mkdir()
        target_state = copy.deepcopy(base_state)
        path_owners = {}
        for queue_id, result in base_items.items():
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
                base_directory / "generated-audio", relative, "Base generation WAV"
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
        for queue_id, (result, ledger) in merged_items.items():
            previous = target_state["items"].get(queue_id)
            previous_path = previous.get("path") if isinstance(previous, dict) else None
            relative = source_audio[queue_id][2]
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
            target_audio.write_bytes(source_audio[queue_id][1])
            copied = copy.deepcopy(result)
            copied["outcome_merge"] = {
                key: value for key, value in ledger.items() if key != "queue_id"
            }
            target_state["items"][queue_id] = copied
        atomic_write_json(
            output / "generation-state.json", target_state, sort_keys=True
        )
        workspace = copy.deepcopy(base_document)
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

        try:
            source_directories = (base_directory, *source_values)
            with generation_publication_leases(
                (
                    (directory / "generated-audio", base_queue_sha256)
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
                for path, digest in (*base_snapshots, *source_snapshots):
                    if not path.is_file() or sha256_file(path) != digest:
                        raise AuthoringWorkbenchError(
                            "Outcome merge source changed before workspace publication"
                        )
                for lease in held_leases:
                    lease.assert_owned()
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
                staging = None
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(
                f"Outcome merge source became active before publication: {error}"
            ) from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return WorkspaceCreationResult(destination, True)


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
    state_items = {}
    if state_path.is_file():
        try:
            state = load_generation_state(state_path, queue_path)
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        state_items = state["items"]

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
        state=state_path if state_path.is_file() else None,
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
    silence_ratio = speech_quality.get("silence_ratio")
    internal_silence = speech_quality.get("longest_internal_silence_seconds")
    flags = []
    if peak is not None and peak >= 0.98:
        flags.append("near clipping")
    if (
        isinstance(silence_ratio, (int, float))
        and silence_ratio >= REVIEW_NOTABLE_SILENCE_RATIO
    ):
        flags.append("notable silence")
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


def list_review_items(workspace_directory, queue_ids=None):
    selected_queue_ids = None
    if queue_ids is not None:
        if not isinstance(queue_ids, (list, tuple, set, frozenset)):
            raise AuthoringWorkbenchError("Review queue IDs must be a collection")
        selected_queue_ids = set()
        for queue_id in queue_ids:
            if not isinstance(queue_id, str) or not queue_id:
                raise AuthoringWorkbenchError("Review queue ID must be non-empty text")
            if queue_id in selected_queue_ids:
                raise AuthoringWorkbenchError(
                    f"Review queue ID is duplicated: {queue_id}"
                )
            selected_queue_ids.add(queue_id)
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
    collection_by_record = {
        (record.line_id, record.text_sha256): collection.collection_id
        for collection in story.collections
        for record in story.records_for_collection(collection.collection_id)
    }
    state_sha256 = sha256_file(state_path)
    state = load_generation_state(state_path, queue_path)
    if sha256_file(state_path) != state_sha256:
        raise AuthoringWorkbenchError(
            "Generation state changed while review rows were being projected"
        )
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
    if summary.state is not None:
        state_items = load_generation_state(summary.state, summary.queue)["items"]
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
        loaded_workspace,
        candidates,
        set(),
        manifest,
        directory=loaded_directory,
    )
    reasons = (
        *_workspace_control_reasons(_load_workspace(workspace_directory)[1]),
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
    readiness = inspect_generation_readiness(
        workspace_directory,
        queue_ids=queue_ids,
    )
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
    voice = workspace.get("voice_manifest")
    if not isinstance(voice, dict):
        return ()
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
    return tuple(values)


def _failure_reference_runtime_binding(directory, workspace):
    return load_failure_reference_runtime_binding(
        directory,
        workspace,
        error_type=AuthoringWorkbenchError,
    )


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


def _read_bound_bytes(path, expected_sha256, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise AuthoringWorkbenchError(f"{label} is missing or unsafe")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise AuthoringWorkbenchError(f"Unable to read {label}: {error}") from error
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise AuthoringWorkbenchError(f"{label} was modified")
    return payload


def immutable_history_timestamps(workspace_directory):
    """Return friendly timestamps from immutable source and workspace records."""
    directory, workspace = _load_workspace(workspace_directory)
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
    repair_policy = failure_repair_policy
    failed_selected = repair_policy.queue_ids
    sentence_selected = set(repair_policy.sentence_segment_queue_ids)
    bounded_selected = set(repair_policy.bounded_seed_retry_queue_ids)
    offline_selected = set(repair_policy.offline_fallback_queue_ids)
    inline_pause_selected = set(repair_policy.inline_pause_queue_ids)
    unsupported_selected = (
        set(failed_selected)
        - sentence_selected
        - bounded_selected
        - offline_selected
        - inline_pause_selected
    )
    if unsupported_selected:
        raise AuthoringWorkbenchError(
            "Carry-forward currently supports only bounded seed, sentence "
            "segmentation, inline pause and offline fallback failures"
        )
    if source_workspace is None:
        if characters is not None or offline_selected or offline_fallback_authorities:
            raise AuthoringWorkbenchError(
                "Carry-forward outcomes require a source workspace"
            )
        return None, ()
    if characters is None and not failed_selected:
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
    source_directory, source_document = _load_workspace(source_workspace)
    if (
        failure_reference_binding is not None
        and source_document.get("failure_reference_binding")
        != failure_reference_binding
    ):
        raise AuthoringWorkbenchError(
            "Carry-forward failure-reference binding differs from its source"
        )
    source_source = source_document["source"]
    if source_source.get("import_id") != import_id:
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
    source_run_config = source_document.get("run_config")
    source_run_config_normalized = _workspace_run_config_with_policy(source_run_config)
    target_run_config_normalized = _workspace_run_config_with_policy(run_config)
    cross_backend = source_run_config_normalized != target_run_config_normalized
    if cross_backend and not failed_selected:
        raise AuthoringWorkbenchError(
            "Carry-forward source and target model configuration differs"
        )
    same_backend_selected = sentence_selected | bounded_selected | inline_pause_selected
    if same_backend_selected and offline_selected:
        raise AuthoringWorkbenchError(
            "One carry-forward workspace cannot mix same-backend failure repair "
            "with cross-backend offline fallback"
        )
    source_base_config = dict(source_run_config_normalized)
    source_base_config["failure_repair_policy"] = FailureRepairPolicy().to_document()
    target_base_config = dict(target_run_config_normalized)
    target_base_config["failure_repair_policy"] = FailureRepairPolicy().to_document()
    if same_backend_selected and source_base_config != target_base_config:
        raise AuthoringWorkbenchError(
            "Same-backend repair requires the exact source backend, model, profile "
            "and missing-voice policy"
        )
    if offline_selected and (
        run_config.get("backend") != "pocket-tts"
        or source_run_config_normalized.get("backend") == run_config.get("backend")
        or run_config.get("model") not in {None, "pocket-tts"}
        or run_config.get("generation_profile") not in {None, "default"}
    ):
        raise AuthoringWorkbenchError(
            "Offline fallback requires a different source backend and the exact "
            "Pocket TTS default model/profile"
        )
    source_output = source_directory / "generated-audio"
    source_state_path = source_output / "generation-state.json"
    source_state_payload = _read_file_bytes(
        source_state_path, "source generation state"
    )
    source_state_sha256 = hashlib.sha256(source_state_payload).hexdigest()
    try:
        parsed_source_state = json.loads(source_state_payload.decode("utf-8"))
        validated_source_state = load_generation_state(
            source_state_path, source_queue_path
        )
    except (UnicodeDecodeError, json.JSONDecodeError, BulkGenerationError) as error:
        raise AuthoringWorkbenchError(
            f"Carry-forward source state is invalid: {error}"
        ) from error
    if (
        parsed_source_state != validated_source_state
        or sha256_file(source_state_path) != source_state_sha256
    ):
        raise AuthoringWorkbenchError(
            "Carry-forward source state changed while it was loaded"
        )
    if parsed_source_state.get("active") is not None:
        raise AuthoringWorkbenchError(
            "Carry-forward source has an active generation attempt"
        )

    try:
        authorities = load_offline_fallback_authorities(
            offline_fallback_authorities,
            parsed_source_state.get("items", {}),
            offline_selected,
        )
    except OfflineFallbackAuthorityError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    authority_by_queue_id = {
        queue_id: authority
        for authority in authorities
        for queue_id in authority.queue_ids
    }
    authority_records = []
    authority_sources = []
    for authority in authorities:
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
        authority_records.append(authority.snapshot_record(relative.as_posix()))
        authority_sources.append(
            (
                authority.source,
                authority.source_sha256,
                "offline fallback authority",
            )
        )

    target_state_path = staging / "generated-audio" / "generation-state.json"
    try:
        target_state = load_generation_state(target_state_path, target_queue_path)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    if target_state.get("active") is not None:
        raise AuthoringWorkbenchError(
            "Carry-forward target seed has an active generation attempt"
        )
    target_seed = copy.deepcopy(target_state)
    target_registry = _registry_from_staged_voice(
        staging,
        voice_config,
        failure_reference_binding,
    )
    source_registry = _workspace_voice_registry(source_directory, source_document)
    source_queue_overrides = _workspace_queue_voice_overrides(
        source_directory,
        source_document,
    )
    target_manifest = _within(
        staging,
        _safe_relative(voice_config.get("path"), "Voice manifest snapshot"),
        "Voice manifest snapshot",
    )
    target_queue_overrides = _queue_voice_overrides_for_manifest(target_manifest)
    target_runtime_binding = _failure_reference_runtime_binding(
        staging,
        {"failure_reference_binding": failure_reference_binding},
    )
    if target_runtime_binding is not None:
        target_queue_overrides = {
            **target_queue_overrides,
            **target_runtime_binding.queue_voice_overrides,
        }
    source_provenance = None
    source_audio_snapshots = []
    carried = []
    for queue_item in target_queue.items:
        result = parsed_source_state["items"].get(queue_item.queue_id)
        if not isinstance(result, dict) or not _terminal_review_outcome(result):
            continue
        character = synthesis_character_for_line(
            queue_item.speaker, queue_item.voice_character
        )
        if character == "Narrator" or character not in selected:
            continue
        seed_result = target_seed["items"].get(queue_item.queue_id)
        mode = "review-only"
        if not _same_seed_generation(seed_result, result):
            mode = "full-outcome"
            if source_provenance is None:
                source_provenance = _workspace_generation_provenance(
                    source_directory, source_document
                )
            source_synthesis_character = source_queue_overrides.get(
                queue_item.queue_id, character
            )
            target_synthesis_character = target_queue_overrides.get(
                queue_item.queue_id, character
            )
            if source_synthesis_character != target_synthesis_character:
                raise AuthoringWorkbenchError(
                    f"Carry-forward queue voice differs for {queue_item.queue_id!r}"
                )
            _validate_full_carry_forward_item(
                queue_item,
                result,
                source_synthesis_character,
                source_document,
                source_run_config_normalized,
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
        source_audio = _within(source_output, relative, "Carry-forward source WAV")
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
        source_item_sha256 = canonical_document_sha256(result)
        carry_record = {
            "mode": mode,
            "source_workspace_id": source_document["workspace_id"],
            "source_state_sha256": source_state_sha256,
            "source_item_sha256": source_item_sha256,
            "audio_sha256": audio_sha256,
            "character": character,
        }
        copied_result = copy.deepcopy(result)
        copied_result["carry_forward"] = carry_record
        target_state["items"][queue_item.queue_id] = copied_result
        carried.append({"queue_id": queue_item.queue_id, **carry_record})
        source_audio_snapshots.append((source_audio, audio_sha256))
    queue_by_id = {item.queue_id: item for item in target_queue.items}
    for queue_id in failed_selected:
        if queue_id not in queue_by_id:
            raise AuthoringWorkbenchError(
                f"Failure repair references unknown queue item {queue_id!r}"
            )
        result = parsed_source_state["items"].get(queue_id)
        if not isinstance(result, dict) or result.get("status") != "failed":
            raise AuthoringWorkbenchError(
                f"Failure repair requires a current failed source outcome for {queue_id!r}"
            )
        failure = normalized_failure_record(result, text=queue_by_id[queue_id].text)
        attempts = result.get("attempts")
        source_model = _required_text(
            result.get("model"), f"Offline fallback source model for {queue_id!r}"
        )
        source_profile = _required_text(
            result.get("generation_profile"),
            f"Offline fallback source profile for {queue_id!r}",
        )
        strategy = repair_policy.strategy_for(queue_id)
        fallback_authority = authority_by_queue_id.get(queue_id)
        minimum_attempts = (
            MAX_BOUNDED_TOTAL_ATTEMPTS
            if strategy == OFFLINE_FALLBACK_BACKEND and fallback_authority is None
            else 1
        )
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
        sentence_mismatch = strategy == SENTENCE_BOUNDARY_SEGMENTATION and not (
            sentence_repair_matches_failure(failure, queue_by_id[queue_id].text)
        )
        inline_pause_mismatch = strategy == INLINE_PAUSE_MARKER and not (
            inline_pause_matches_failure(failure, queue_by_id[queue_id].text)
        )
        if strategy == SENTENCE_BOUNDARY_SEGMENTATION:
            failure_kind_mismatch = sentence_mismatch
        elif strategy == INLINE_PAUSE_MARKER:
            failure_kind_mismatch = inline_pause_mismatch
        elif strategy == OFFLINE_FALLBACK_BACKEND:
            failure_kind_mismatch = not (
                (
                    fallback_authority is not None
                    or (
                        isinstance(source_provider_attempts, int)
                        and not isinstance(source_provider_attempts, bool)
                        and source_provider_attempts >= MAX_BOUNDED_TOTAL_ATTEMPTS
                    )
                )
                and (
                    failure.get("kind") == "missed_eos_audio_limit"
                    or (
                        failure.get("kind") == "speech_silence"
                        and source_repair_strategy
                        in {None, BOUNDED_SEED_RETRY, INLINE_PAUSE_MARKER}
                        and (
                            fallback_authority is not None
                            or inline_pause_matches_failure(
                                failure, queue_by_id[queue_id].text
                            )
                        )
                    )
                )
            )
        else:
            failure_kind_mismatch = failure.get("kind") != "missed_eos_audio_limit"
        if (
            failure_kind_mismatch
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts < minimum_attempts
            or result.get("provider") != source_run_config_normalized.get("backend")
            or (
                source_run_config_normalized.get("model") is not None
                and source_model != source_run_config_normalized.get("model")
            )
            or (
                source_run_config_normalized.get("generation_profile") is not None
                and source_profile
                != source_run_config_normalized.get("generation_profile")
            )
        ):
            raise AuthoringWorkbenchError(
                f"Failure-repair source is not a compatible typed backend failure for {queue_id!r}"
            )
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
        source_item_sha256 = canonical_document_sha256(result)
        requested_character = synthesis_character_for_line(
            queue_by_id[queue_id].speaker,
            queue_by_id[queue_id].voice_character,
        )
        effective_character = _required_text(
            result.get("voice_character", requested_character),
            f"Failure-repair source voice character for {queue_id!r}",
        )
        reference_character = (
            _required_text(
                source_document.get("narrator_character"),
                "Carry-forward source narrator character",
            )
            if effective_character == "Narrator"
            else effective_character
        )
        carry_record = {
            "mode": "failed-outcome",
            "source_workspace_id": source_document["workspace_id"],
            "source_state_sha256": source_state_sha256,
            "source_item_sha256": source_item_sha256,
            "character": effective_character,
            "source_provider": result["provider"],
            "source_model": source_model,
            "source_generation_profile": source_profile,
            "source_attempts": attempts,
            "source_seed": result.get("seed"),
            "source_failure_kind": failure["kind"],
            "source_voice_reference": _voice_reference_identity(
                source_registry,
                reference_character,
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
    if not carried:
        raise AuthoringWorkbenchError(
            "Carry-forward source has no terminal review outcomes for the selected characters"
        )
    unknown = set(selected) - {value["character"] for value in carried}
    if unknown:
        raise AuthoringWorkbenchError(
            "Carry-forward has no terminal review outcomes for: "
            + ", ".join(sorted(unknown))
        )
    atomic_write_json(target_state_path, target_state, sort_keys=True)
    try:
        publish_generated_manifest(target_state_path)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    if sha256_file(source_state_path) != source_state_sha256:
        raise AuthoringWorkbenchError(
            "Carry-forward source state changed before workspace publication"
        )
    for path, digest in source_audio_snapshots:
        if not path.is_file() or sha256_file(path) != digest:
            raise AuthoringWorkbenchError(
                "Carry-forward source WAV changed before workspace publication"
            )
    document = {
        "schema": "vntts.authoring-carry-forward",
        "schema_version": 4 if authorities else (3 if failed_selected else 1),
        "source_workspace_id": source_document["workspace_id"],
        "source_state_sha256": source_state_sha256,
        "characters": list(selected),
        "items": carried,
    }
    if failed_selected:
        document["failed_queue_ids"] = list(failed_selected)
        document["source_run_config"] = source_run_config
    if authorities:
        document["offline_fallback_authorities"] = authority_records
    return document, tuple(authority_sources)


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
    narrator_ready = (
        narrator_voice is not None
        and bool(narrator_voice.references)
        and all(reference.is_file() for reference in narrator_voice.references)
    )
    synthesis_character_overrides = {}
    for item in queue.items:
        requested = synthesis_character_for_line(item.speaker, item.voice_character)
        voice = registry.resolve(requested)
        if (
            requested != "Narrator"
            and (
                voice is None
                or not voice.references
                or any(not reference.is_file() for reference in voice.references)
            )
            and missing_voice_policy.applies_to(requested)
            and narrator_ready
        ):
            synthesis_character_overrides[requested] = "Narrator"
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
    try:
        snapshots = snapshot_generation_control_files(controls)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    synthesis_configuration = {
        "missing_voice_policy": missing_voice_policy.to_document(),
        "synthesis_character_overrides": dict(
            sorted(synthesis_character_overrides.items())
        ),
        "failure_repair_policy": failure_repair_policy.to_document(),
    }
    if projection_ids:
        synthesis_configuration["audio_event_spoken_projection_queue_ids"] = list(
            projection_ids
        )
    if queue_overrides:
        synthesis_configuration["queue_voice_overrides_sha256"] = (
            queue_voice_overrides_sha256(queue_overrides)
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


def read_workspace_file_bytes(path, label):
    """Read one non-symlink workspace file with workbench error semantics."""
    return _read_file_bytes(path, label)


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


def _load_workspace(workspace_directory):
    directory = Path(workspace_directory).expanduser().resolve()
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
    _validate_workspace_input_config(directory, workspace, snapshot)
    _validate_workspace_failure_reference_binding(directory, workspace)
    _validate_workspace_offline_fallback_state(directory, workspace)
    _validate_workspace_outcome_merge(directory, workspace)
    _validate_workspace_terminal_conflict_merge(directory, workspace)
    try:
        validate_audio_event_composition_workspace(directory, workspace)
    except AudioEventWorkspaceError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    config_rebase = workspace.get("config_rebase")
    if config_rebase is not None:
        module = importlib.import_module("vntts.authoring.config_rebase")
        module.validate_config_rebase_workspace(directory, workspace)
    explicit_fallback_merge = workspace.get("explicit_fallback_merge")
    if explicit_fallback_merge is not None:
        module = importlib.import_module("vntts.authoring.explicit_fallback_merge")
        module.validate_explicit_fallback_merge_workspace(directory, workspace)
    known_role_live_fallback = workspace.get("known_role_live_fallback")
    if known_role_live_fallback is not None:
        module = importlib.import_module("vntts.authoring.known_role_live_fallback")
        module.validate_known_role_live_fallback_workspace(directory, workspace)
    audio_event_omission = workspace.get("audio_event_omission")
    if audio_event_omission is not None:
        module = importlib.import_module("vntts.authoring.audio_event_omission")
        module.validate_audio_event_omission_workspace(directory, workspace)
    audio_event_projection_fallback = workspace.get("audio_event_projection_fallback")
    if audio_event_projection_fallback is not None:
        module = importlib.import_module(
            "vntts.authoring.audio_event_projection_fallback"
        )
        module.validate_audio_event_projection_fallback_workspace(directory, workspace)
    reviewed_waveform_publication = workspace.get("reviewed_waveform_publication")
    if reviewed_waveform_publication is not None:
        module = importlib.import_module(
            "vntts.authoring.reviewed_waveform_publication"
        )
        module.validate_reviewed_waveform_publication_workspace(directory, workspace)
    reviewed_rejection_live_fallback = workspace.get("reviewed_rejection_live_fallback")
    if reviewed_rejection_live_fallback is not None:
        module = importlib.import_module("vntts.authoring.reviewed_rejection_fallback")
        module.validate_reviewed_rejection_fallback_workspace(directory, workspace)
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
        config_rebase,
        workspace.get("audio_event_composition"),
        explicit_fallback_merge,
        known_role_live_fallback,
        audio_event_omission,
        audio_event_projection_fallback,
        reviewed_waveform_publication,
        reviewed_rejection_live_fallback,
        workspace.get("queue_extension"),
    )
    if (
        workspace.get("config_fingerprint") != expected_config
        or match.group(2) != expected_config[:16]
    ):
        raise AuthoringWorkbenchError("Workspace configuration identity was modified")
    return directory, workspace


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


def _legacy_narrator(manifest):
    legacy = manifest.get("legacy_job")
    if isinstance(legacy, dict):
        value = legacy.get("narrator_character")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Narrator"


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


def _legacy_input_digest(manifest, role):
    declared = _external_input(manifest, role)
    if declared is None:
        return None
    digest = declared.get("sha256_at_import")
    return _require_sha256(digest, f"Legacy {role} SHA-256") if digest else None


def _verify_selected_sources(selected_sources):
    for path, digest, label in selected_sources:
        if not path.is_file() or sha256_file(path) != digest:
            raise AuthoringWorkbenchError(
                f"Selected {label} changed while workspace was being created"
            )


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


def _validate_workspace_offline_fallback_state(directory, workspace):
    carry = workspace.get("carry_forward")
    if not isinstance(carry, dict) or carry.get("schema_version") not in {2, 3, 4}:
        return
    queue_path = directory / "queue.jsonl"
    state_path = directory / "generated-audio" / "generation-state.json"
    try:
        state = load_generation_state(state_path, queue_path)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
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


def _copy_workspace_tree_snapshot(source, target, snapshots):
    return copy_workspace_tree_snapshot(
        source,
        target,
        snapshots,
        error_type=AuthoringWorkbenchError,
    )


def _validate_workspace_outcome_merge(directory, workspace):
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


def _validate_workspace_terminal_conflict_merge(directory, workspace):
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


_workspace_config_fingerprint = workspace_config_fingerprint


def _workspace_run_config_with_policy(run_config):
    return normalize_workspace_run_config(
        run_config,
        error_type=AuthoringWorkbenchError,
    )


def _workspace_missing_voice_policy(workspace):
    return workspace_missing_voice_policy(
        workspace,
        error_type=AuthoringWorkbenchError,
    )


def _workspace_failure_repair_policy(workspace):
    return workspace_failure_repair_policy(
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


def _load_json(path, description):
    return load_json_object(path, description, error_type=AuthoringWorkbenchError)


def load_workspace_json(path, description):
    """Load one workspace JSON object with workbench error semantics."""
    return _load_json(path, description)


def _load_json_snapshot(path, description):
    return load_json_object_snapshot(
        path, description, error_type=AuthoringWorkbenchError
    )


def load_workspace_json_snapshot(path, description):
    """Load one exact workspace JSON object and its payload identity."""
    return _load_json_snapshot(path, description)


def _safe_relative(value, label):
    return safe_relative_path(value, label, error_type=AuthoringWorkbenchError)


def safe_workspace_relative_path(value, label):
    """Validate one canonical POSIX-relative workspace path."""
    return _safe_relative(value, label)


def _within(root, relative, label):
    return contained_path(root, relative, label, error_type=AuthoringWorkbenchError)


def contained_workspace_path(root, relative, label):
    """Resolve one already validated relative path inside its owning root."""
    return _within(root, relative, label)


def merge_terminal_conflict_resolution(*args, **kwargs):
    """Compatibility facade for the former direct workbench export."""
    module = importlib.import_module("vntts.authoring.terminal_conflict_workspace")
    return module.merge_terminal_conflict_resolution(*args, **kwargs)


def _require_sha256(value, label):
    return require_sha256(value, label, error_type=AuthoringWorkbenchError)


def require_workspace_sha256(value, label):
    """Validate one workspace SHA-256 with workbench error semantics."""
    return _require_sha256(value, label)


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AuthoringWorkbenchError(f"{label} must be non-empty text")
    return value.strip()


def _optional_text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


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
    "WorkspaceCreationResult",
    "WorkspaceCollection",
    "WorkspaceSummary",
    "WorkspaceVoice",
    "create_audio_event_composition_workspace",
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

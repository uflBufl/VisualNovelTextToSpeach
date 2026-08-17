"""Safe mutable workspaces and truthful status for graphical authoring."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import socket
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath

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
from vntts.authoring.bulk_generation import (
    LEASE_SCHEMA,
    LEASE_VERSION,
    BulkGenerationError,
    is_spoken_queue_item,
    load_generation_state,
    process_is_alive,
    process_started_at,
    review_generation_item,
)
from vntts.authoring.game_pack import (
    FinalGamePackError,
    _rename_directory_no_replace,
)
from vntts.voices import CharacterVoiceRegistry, synthesis_character_for_line

WORKSPACE_SCHEMA = "vntts.authoring-workspace"
WORKSPACE_VERSION = 1
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
        except (AuthoringWorkbenchError, ValueError):
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
        except (AuthoringWorkbenchError, ValueError):
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
        run_config = {
            "backend": _optional_text(backend),
            "model": _optional_text(model),
            "generation_profile": _optional_text(generation_profile),
        }
        config_fingerprint = _workspace_config_fingerprint(
            import_id, story_config, voice_config, narrator, run_config
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
            "config_fingerprint": config_fingerprint,
            "seed_inventory": [
                {"path": "provenance/import.json", "sha256": import_sha256},
                *({"path": item["path"], "sha256": item["sha256"]} for item in copied),
            ],
        }
        atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
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

    candidates = [item for item in queue.items if item.action == "generate"]
    recoverable_source_audio = sum(
        item.action == "prefer_source_audio" for item in queue.items
    )
    manual_review = sum(item.action == "manual_review" for item in queue.items)
    resolve_audio = sum(item.action == "resolve_audio" for item in queue.items)
    spoken = [item for item in candidates if is_spoken_queue_item(item)]
    spoken_ids = {item.queue_id for item in spoken}
    relevant = {
        queue_id: value
        for queue_id, value in state_items.items()
        if queue_id in spoken_ids
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
    completed_ids = approved_ids | rejected_ids | generated_ids | failed_ids
    selected_voice_manifest = _selected_voice_manifest(
        directory, workspace, voice_manifest
    )
    missing_voice_ids, blocked_reasons = _voice_readiness(
        workspace, spoken, completed_ids, selected_voice_manifest
    )
    blocked_reasons = (*_workspace_control_reasons(workspace), *blocked_reasons)
    pending_ids = spoken_ids - completed_ids - missing_voice_ids
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
        eligible=len(spoken),
        pending=len(pending_ids),
        generated=len(generated_ids),
        approved=len(approved_ids),
        rejected=len(rejected_ids),
        failed=len(failed_ids),
        skipped_actions=(
            len(queue.items)
            - len(candidates)
            - recoverable_source_audio
            - manual_review
            - resolve_audio
        ),
        skipped_sound_effects=len(candidates) - len(spoken),
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


def list_review_items(workspace_directory):
    summary = inspect_workspace(workspace_directory)
    if summary.state is None:
        return ()
    directory, workspace = _load_workspace(workspace_directory)
    queue_path = _within(
        directory, _safe_relative(workspace["queue"], "Queue"), "Queue"
    )
    queue = VoiceGenerationQueue.load(queue_path)
    state = load_generation_state(summary.state, queue_path)
    records = []
    for item in queue.items:
        result = state["items"].get(item.queue_id)
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "unknown")
        if status not in {"generated", "approved", "failed"}:
            continue
        audio = None
        if result.get("path"):
            audio = _within(
                summary.output,
                _safe_relative(result["path"], "Generated audio"),
                "Generated audio",
            )
        records.append(
            ReviewItem(
                queue_id=item.queue_id,
                line_id=item.line_id,
                speaker=item.speaker,
                voice_character=item.voice_character,
                text=item.text,
                status=status,
                review_status=result.get("review_status"),
                attempts=int(result.get("attempts") or 0),
                seed=result.get("seed"),
                last_error=result.get("last_error"),
                audio=audio,
            )
        )
    return tuple(records)


def review_workspace_item(workspace_directory, queue_id, decision):
    summary = inspect_workspace(workspace_directory)
    if summary.state is None:
        raise AuthoringWorkbenchError("Workspace has no generation state to review")
    try:
        review_generation_item(summary.state, queue_id, decision)
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    return inspect_workspace(workspace_directory)


def inspect_generation_readiness(workspace_directory, *, queue_ids=None):
    summary = inspect_workspace(workspace_directory)
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
        if item.action != "generate" or not is_spoken_queue_item(item):
            continue
        result = state_items.get(item.queue_id)
        status = result.get("status") if isinstance(result, dict) else None
        if status == "failed":
            failed += 1
            candidates.append(item)
        elif status is None:
            pending += 1
            candidates.append(item)
    manifest = summary.voice_manifest
    missing, reasons = _voice_readiness(
        _load_workspace(workspace_directory)[1], candidates, set(), manifest
    )
    reasons = (
        *_workspace_control_reasons(_load_workspace(workspace_directory)[1]),
        *reasons,
    )
    if not candidates:
        reasons = ("No pending or failed queue items are selected",)
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
    queue_digest = next(
        (
            value.get("sha256")
            for value in workspace.get("seed_inventory", [])
            if isinstance(value, dict) and value.get("path") == "queue.jsonl"
        ),
        None,
    )
    payload = _read_bound_bytes(
        directory / "queue.jsonl",
        _require_sha256(queue_digest, "Workspace queue SHA-256"),
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
):
    if include_prefer_source:
        raise AuthoringWorkbenchError(
            "Recoverable source-audio generation requires an explicit preflight policy"
        )
    directory, workspace = _load_workspace(workspace_directory)
    run_config = workspace["run_config"]
    configured_backend = run_config.get("backend")
    configured_model = run_config.get("model")
    configured_profile = run_config.get("generation_profile")
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
    readiness = inspect_generation_readiness(workspace_directory, queue_ids=queue_ids)
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
    if queue_ids is not None:
        for queue_id in queue_ids:
            command.extend(("--queue-id", _required_text(queue_id, "Queue ID")))
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
    expected = {
        "backend": backend,
        "model": model,
        "generation_profile": generation_profile,
    }
    if run_config != expected:
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
    queue_digest = next(
        (value["sha256"] for value in expected_seed if value["path"] == "queue.jsonl"),
        None,
    )
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
    if not isinstance(run_config, dict) or set(run_config) != {
        "backend",
        "model",
        "generation_profile",
    }:
        raise AuthoringWorkbenchError("Workspace run configuration is malformed")
    _validate_workspace_input_config(directory, workspace, snapshot)
    expected_config = _workspace_config_fingerprint(
        expected_import_id,
        workspace.get("story_index"),
        workspace.get("voice_manifest"),
        narrator,
        run_config,
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
        voice_config = {
            "path": "inputs/voice/manifest.json",
            "sha256": digest,
            "controls": controls,
            "legacy_sha256_at_import": legacy_digest,
            "matches_legacy": legacy_digest == digest if legacy_digest else None,
        }
        selected_sources.append((source, digest, "voice manifest"))
    return story_config, voice_config, tuple(selected_sources)


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


def _workspace_config_fingerprint(
    import_id, story_config, voice_config, narrator_character, run_config
):
    payload = json.dumps(
        {
            "import_id": import_id,
            "story_index": story_config,
            "voice_manifest": voice_config,
            "narrator_character": narrator_character,
            "run_config": run_config,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _selected_voice_manifest(directory, workspace, selected=None):
    value = workspace.get("voice_manifest")
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
        return None
    path = _within(
        directory,
        _safe_relative(value["path"], "Voice manifest snapshot"),
        "Voice manifest snapshot",
    )
    if selected is not None and Path(selected).expanduser().resolve() != path:
        raise AuthoringWorkbenchError(
            "Configure the workspace voice snapshot before generation"
        )
    if not path.is_file() or sha256_file(path) != _require_sha256(
        value.get("sha256"), "Voice manifest snapshot SHA-256"
    ):
        raise AuthoringWorkbenchError("Workspace voice manifest snapshot was modified")
    for control in value.get("controls", []):
        relative = _safe_relative(control.get("path"), "Voice reference snapshot")
        reference = _within(directory, relative, "Voice reference snapshot")
        if not reference.is_file() or sha256_file(reference) != _require_sha256(
            control.get("sha256"), "Voice reference snapshot SHA-256"
        ):
            raise AuthoringWorkbenchError(
                "Workspace voice reference snapshot was modified"
            )
    return path


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


def _voice_readiness(workspace, spoken, completed_ids, manifest_path):
    if manifest_path is None:
        return set(), ("Select an existing voice manifest",)
    try:
        registry = CharacterVoiceRegistry.from_file(manifest_path)
    except Exception as error:
        raise AuthoringWorkbenchError(
            f"Unable to load voice manifest: {error}"
        ) from error
    narrator = str(workspace.get("narrator_character") or "Narrator")
    missing = set()
    for item in spoken:
        if item.queue_id in completed_ids:
            continue
        requested_character = synthesis_character_for_line(
            item.speaker, item.voice_character
        )
        character = (
            narrator if requested_character == "Narrator" else requested_character
        )
        voice = registry.resolve(character or item.speaker or "")
        if (
            voice is None
            or not voice.references
            or any(not reference.is_file() for reference in voice.references)
        ):
            missing.add(item.queue_id)
    if missing:
        return missing, (
            f"Voice references are missing or unsafe for {len(missing)} queued line(s)",
        )
    return missing, ()


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
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuthoringWorkbenchError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise AuthoringWorkbenchError(f"{description.title()} must be a JSON object")
    return value


def _load_json_snapshot(path, description):
    try:
        payload = Path(path).read_bytes()
    except OSError as error:
        raise AuthoringWorkbenchError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    digest = hashlib.sha256(payload).hexdigest()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthoringWorkbenchError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise AuthoringWorkbenchError(f"{description.title()} must be a JSON object")
    return value, digest, payload


def _safe_relative(value, label):
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise AuthoringWorkbenchError(f"{label} must be a POSIX-relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise AuthoringWorkbenchError(f"{label} must stay inside its workspace")
    return Path(*pure.parts)


def _within(root, relative, label):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise AuthoringWorkbenchError(f"{label} leaves its owning directory") from error
    return path


def _require_sha256(value, label):
    if not isinstance(value, str) or len(value) != 64:
        raise AuthoringWorkbenchError(f"{label} must be a full SHA-256")
    try:
        int(value, 16)
    except ValueError as error:
        raise AuthoringWorkbenchError(f"{label} must be hexadecimal") from error
    return value


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
    "GenerationReadiness",
    "ImmutableHistoryTimestamp",
    "ReviewItem",
    "WorkspaceCreationResult",
    "WorkspaceCollection",
    "WorkspaceSummary",
    "WorkspaceVoice",
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
    "immutable_history_timestamps",
    "list_workspace_collections",
    "list_review_items",
    "review_workspace_item",
    "workspace_voice_snapshot",
]

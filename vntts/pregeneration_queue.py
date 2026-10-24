"""Materialize private generation inputs from a player-facing voice plan."""

from __future__ import annotations

import copy
import hashlib
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter, process_time
from typing import Protocol, TypeAlias, TypedDict

import numpy as np
import soundfile as sf
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    StoryIndexRecord,
    write_story_index_document,
)
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueItem,
)
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    normalize_character_name,
    write_voice_manifest,
)

from vntts.audio_store import (
    SharedAudioStore,
    _sync_directory,
    _sync_regular_file,
    link_shared_audio,
)
from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.generation_lease import GenerationLease, process_is_alive
from vntts.authoring.generation_state import (
    STATE_SCHEMA,
    STATE_VERSION,
    BulkGenerationError,
    generation_state_file,
    generation_state_items,
    load_generation_state_from_snapshot,
    load_stable_generation_queue,
    write_generation_state,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.queue_builder import (
    GenerationQueueBuildError,
    inspect_generation_queue,
    publish_generation_queue,
)
from vntts.document_identity import canonical_document_sha256, sha256_file
from vntts.generation_database import (
    read_state,
    validate_database_path,
)
from vntts.generation_database import (
    state_path as generation_state_path,
)
from vntts.pregeneration_setup import (
    PregenerationJob,
    PregenerationJobStore,
    PregenerationSetupError,
    load_verified_story_index_document,
)
from vntts.pregeneration_voices import VoiceGroup, VoicePlan
from vntts.preparation_catalog import PreparationCatalog
from vntts.recording_index import RecordingIndex
from vntts.source_audio_semantics import (
    SourceAudioSemanticEvidenceError,
    load_source_audio_semantic_evidence,
    project_source_audio_semantics,
)
from vntts.support import record_background_operation
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    pocket_tts_preset_voices,
    read_voice_reference_bytes,
    synthesis_character_for_line,
)

generation_input_schema_version = 4
VoiceChoice: TypeAlias = tuple[CharacterVoice | None, tuple[str, ...], str]
StoryRecordDocument: TypeAlias = dict[str, object]


class EffectiveVoiceRoutes(TypedDict):
    routes: dict[str, VoiceChoice]
    narrator_roles: tuple[str, ...]
    line_voice_characters: dict[str, str]


class _Cancellation(Protocol):
    def is_set(self) -> bool: ...


class PregenerationQueueError(RuntimeError):
    """Resolved voices cannot yet produce safe private generation inputs."""


class PregenerationQueueCancelled(PregenerationQueueError):
    """The player cancelled private input materialization."""


@dataclass(frozen=True)
class PregenerationInput:
    identity: str
    directory: Path
    story_index: Path
    voice_manifest: Path
    queue: Path
    queue_sha256: str
    queue_items: int
    ready_items: int
    narrator_fallback_roles: tuple[str, ...]
    story_index_sha256: str
    voice_manifest_sha256: str
    source_audio_semantic_evidence_sha256: str | None
    audio_event_projection_queue_ids: tuple[str, ...] = ()
    audio_event_omission_queue_ids: tuple[str, ...] = ()
    source_audio_semantic_evidence: Path | None = None

    @property
    def output_directory(self) -> Path:
        return Path(self.directory).parent / f"generation-output-{self.identity[:16]}"


class PregenerationInputStore:
    def __init__(self, job_store: PregenerationJobStore) -> None:
        self.job_store = job_store

    def materialize(
        self,
        job: PregenerationJob,
        plan: VoicePlan,
        *,
        cancellation: _Cancellation | None = None,
    ) -> PregenerationInput:
        _validate_plan_for_job(job, plan)
        story, registry = self._load_materialize_sources(job, plan, cancellation)
        phase_started, cpu_started = perf_counter(), process_time()
        selected = _selected_records(story, job.selected_line_ids)
        effective = _effective_voice_routes(plan, registry)
        identity = _generation_input_identity(job, plan)
        _record_input_phase("identity", phase_started, cpu_started)
        destination = self.job_store.directory_for(job.job_id) / (
            f"generation-input-{identity[:16]}"
        )
        try:
            if self.job_store.catalog.input_staging(destination) is not None:
                load_generation_input_metadata(destination)
        except (AtomicPublicationError, OSError, ValueError) as error:
            raise PregenerationQueueError(
                f"Saved offline generation inputs are invalid: {error}"
            ) from error
        if destination.is_dir():
            result = self._reused_input(destination, identity, effective)
            _reuse_completed_audio(result, plan, self.job_store)
            return result
        root = destination.parent
        root.mkdir(parents=True, exist_ok=True)
        try:
            with staged_directory(root, prefix=".generation-input-") as staging:
                story_path = staging / "story-index.jsonl"
                voice_path = staging / "voice-manifest.json"
                queue_path = staging / "queue.jsonl"
                phase_started, cpu_started = perf_counter(), process_time()
                routed_records = _routed_story_records(
                    selected,
                    effective["line_voice_characters"],
                )
                story_metadata, selected_records, semantic_evidence = (
                    project_source_audio_semantics(
                        story,
                        routed_records,
                        staging,
                    )
                )
                story_metadata = dict(story_metadata)
                story_metadata.pop("selection_origin", None)
                write_story_index_document(story_path, story_metadata, selected_records)
                if semantic_evidence is not None:
                    load_source_audio_semantic_evidence(semantic_evidence, story_path)
                _record_input_phase(
                    "story-projection",
                    phase_started,
                    cpu_started,
                    files_examined=1 + (1 if semantic_evidence is not None else 0),
                    bytes_examined=_file_size(story_path)
                    + _file_size(semantic_evidence),
                )
                _raise_if_cancelled(cancellation)
                phase_started, cpu_started = perf_counter(), process_time()
                voices = _write_effective_voices(staging, effective)
                write_voice_manifest(
                    voice_path,
                    {"version": 2, "voices": voices},
                )
                _record_input_phase(
                    "voice-copy",
                    phase_started,
                    cpu_started,
                    files_examined=sum(
                        len(value[1]) for value in effective["routes"].values()
                    ),
                    bytes_examined=sum(
                        _file_size(path)
                        for voice, _hashes, _speaker in effective["routes"].values()
                        if voice is not None
                        for path in voice.references
                    ),
                )
                phase_started, cpu_started = perf_counter(), process_time()
                queue_plan = inspect_generation_queue(
                    story_path,
                    voice_path,
                    unknown_action="resolve_audio",
                    generated_at=job.created_at,
                )
                publish_generation_queue(queue_plan, queue_path)
                queue = VoiceGenerationQueue.load(queue_path)
                projection_ids, omission_ids, ready_items = _generation_queue_routes(
                    queue, effective
                )
                _record_input_phase(
                    "queue-build",
                    phase_started,
                    cpu_started,
                    files_examined=3,
                    bytes_examined=_file_size(story_path)
                    + _file_size(voice_path)
                    + _file_size(queue_path),
                )
                _raise_if_cancelled(cancellation)
                phase_started, cpu_started = perf_counter(), process_time()
                document = {
                    "schema_version": generation_input_schema_version,
                    "identity": identity,
                    "job_id": job.job_id,
                    "source_story_index_sha256": job.story_index_sha256,
                    "voice_plan_controls_sha256": plan.synthesis_controls_sha256,
                    "story_index_sha256": sha256_file(story_path),
                    "voice_manifest_sha256": sha256_file(voice_path),
                    "queue_sha256": sha256_file(queue_path),
                    "queue_items": queue_plan.summary.queue_items,
                    "ready_items": ready_items,
                    "narrator_fallback_roles": list(effective["narrator_roles"]),
                    "audio_event_projection_queue_ids": list(projection_ids),
                    "audio_event_omission_queue_ids": list(omission_ids),
                    "source_audio_semantic_evidence_sha256": (
                        None
                        if semantic_evidence is None
                        else sha256_file(semantic_evidence)
                    ),
                }
                _publish_input(
                    self.job_store.catalog, job.job_id, document, staging, destination
                )
                result = _load_existing(destination, identity, effective)
                _reuse_completed_audio(result, plan, self.job_store)
                _record_input_phase(
                    "publish",
                    phase_started,
                    cpu_started,
                    files_examined=3,
                    bytes_examined=sum(
                        _file_size(destination / name)
                        for name in (
                            "story-index.jsonl",
                            "voice-manifest.json",
                            "queue.jsonl",
                        )
                    ),
                )
                return result
        except (
            AtomicPublicationError,
            GenerationQueueBuildError,
            OSError,
            SourceAudioSemanticEvidenceError,
            StoryIndexError,
            VoiceManifestError,
            ValueError,
        ) as error:
            raise PregenerationQueueError(
                f"Unable to prepare offline generation inputs: {error}"
            ) from error

    def _load_materialize_sources(
        self,
        job: PregenerationJob,
        plan: VoicePlan,
        cancellation: _Cancellation | None,
    ) -> tuple[StoryIndexDocument, CharacterVoiceRegistry]:
        _raise_if_cancelled(cancellation)
        phase_started, cpu_started = perf_counter(), process_time()
        story = _load_story(job)
        registry = _load_source_registry(plan)
        _record_input_phase(
            "load",
            phase_started,
            cpu_started,
            files_examined=1 + (1 if plan.voice_manifest else 0),
            bytes_examined=_file_size(job.story_index)
            + _file_size(plan.voice_manifest),
        )
        return story, registry

    @staticmethod
    def _reused_input(
        destination: Path, identity: str, effective: EffectiveVoiceRoutes
    ) -> PregenerationInput:
        phase_started, cpu_started = perf_counter(), process_time()
        result = _load_existing(destination, identity, effective)
        _record_input_phase("reuse", phase_started, cpu_started, cache_state="disk")
        return result


def _validate_plan_for_job(job: PregenerationJob, plan: VoicePlan) -> None:
    if not isinstance(plan, VoicePlan) or plan.job_id != job.job_id:
        raise PregenerationQueueError("Voice plan does not belong to this preparation")
    if plan.story_index_sha256 != job.story_index_sha256:
        raise PregenerationQueueError("Voice plan dialogue identity changed")


def _may_have_saved_recordings(
    job_store: PregenerationJobStore,
    directory: Path,
    state: Path,
    document: dict[str, object],
    current: dict[str, VoiceGenerationQueueItem],
    controls_sha256: str,
) -> bool:
    if document.get("voice_plan_controls_sha256") != controls_sha256:
        return False
    index = RecordingIndex(job_store.catalog.path.parent)
    if not index.path.exists() and not index.path.is_symlink():
        return True
    generation_state_file(state)
    validate_database_path(state)
    paths = (directory / "queue.jsonl", state)
    before = tuple(_recording_fingerprint(path) for path in paths)
    source = (
        sha256_file(paths[0]),
        sha256_file(state),
        canonical_document_sha256(document),
    )
    indexed = index.load(
        directory, source, {item.line_id: item.text_sha256 for item in current.values()}
    )
    validate_database_path(state)
    if tuple(_recording_fingerprint(path) for path in paths) != before:
        raise PregenerationQueueError("Saved generation state changed while inspected")
    return indexed is None or bool(indexed[0] or indexed[1])


def _recording_fingerprint(path: Path) -> tuple[int, ...]:
    info = path.stat(follow_symlinks=False)
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _reuse_completed_audio(
    current: PregenerationInput, plan: VoicePlan, job_store: PregenerationJobStore
) -> None:
    """Carry verified completed lines across voice-plan revisions without rendering."""
    state_path = generation_state_path(current.output_directory)
    queue, queue_sha256 = load_stable_generation_queue(current.queue)
    existing = (
        load_generation_state_from_snapshot(state_path, queue, queue_sha256)
        if state_path.exists()
        else None
    )
    protected = {} if existing is None else generation_state_items(existing)
    if existing is not None and (
        existing.get("active") is not None or len(protected) == len(queue.items)
    ):
        return
    voices = CharacterVoiceRegistry.from_file(current.voice_manifest)
    current_items = {item.queue_id: item for item in queue.items}
    voice_identities: dict[str, object] = {}
    saved: dict[str, dict[str, object]] = {}
    controls: dict[str, object] = {}
    sources: dict[str, Path] = {}
    try:
        previous_inputs = job_store.generation_input_directories()
    except PregenerationSetupError as error:
        raise PregenerationQueueError(
            f"Unable to find saved offline audio: {error}"
        ) from error
    current_directory = current.directory.resolve()
    for previous in previous_inputs:
        if len(protected) + len(saved) == len(current_items):
            break
        if previous == current_directory or not previous.is_dir():
            continue
        previous_state = generation_state_path(
            previous.with_name(
                previous.name.replace("generation-input-", "generation-output-", 1)
            )
        )
        if not previous_state.is_file():
            continue
        try:
            document = load_generation_input_metadata(previous)
            if not _may_have_saved_recordings(
                job_store,
                previous,
                previous_state,
                document,
                current_items,
                plan.synthesis_controls_sha256,
            ):
                continue
            old_queue, old_sha256 = load_stable_generation_queue(
                previous / "queue.jsonl"
            )
            if any(
                old_queue.metadata.get(field) != queue.metadata.get(field)
                for field in ("game", "game_id", "language")
            ):
                continue
            if not any(
                item.queue_id not in protected
                and item.queue_id not in saved
                and item.queue_id in current_items
                and item.document == current_items[item.queue_id].document
                for item in old_queue.items
            ):
                continue
            for name, suffix in (
                ("story_index", "story-index.jsonl"),
                ("voice_manifest", "voice-manifest.json"),
                ("queue", "queue.jsonl"),
            ):
                if sha256_file(previous / suffix) != document.get(f"{name}_sha256"):
                    raise ValueError(f"Previous {name} changed")
            state = read_state(
                previous_state,
                selected_line_ids=frozenset(
                    item.line_id for item in current_items.values()
                ),
                allowed_queue_ids=frozenset(item.queue_id for item in old_queue.items),
            )
            old_items = generation_state_items(state)
            old_voices = CharacterVoiceRegistry.from_file(
                previous / "voice-manifest.json"
            )
            old_roles = document.get("narrator_fallback_roles")
            if not isinstance(old_roles, list):
                raise ValueError("Previous narrator roles are invalid")
            eligible: list[str] = []
            old_identities: dict[str, object] = {}
            for item in old_queue.items:
                target = current_items.get(item.queue_id)
                result = old_items.get(item.queue_id)
                if (
                    target is None
                    or item.queue_id in protected
                    or item.queue_id in saved
                    or target.document != item.document
                    or not isinstance(result, dict)
                    or result.get("status") not in {"generated", "approved"}
                    or "audio_event_composition" in result
                    or result.get("provider") != plan.synthesis_backend
                ):
                    continue
                requested = synthesis_character_for_line(
                    item.speaker, item.voice_character
                )
                old_character = "Narrator" if requested in old_roles else requested
                current_character = (
                    "Narrator"
                    if requested in current.narrator_fallback_roles
                    else requested
                )
                if (
                    old_character != current_character
                    or result.get("voice_character") != old_character
                ):
                    continue
                if current_character not in voice_identities:
                    voice_identities[current_character] = _voice_content_identity(
                        voices, current_character
                    )
                if old_character not in old_identities:
                    old_identities[old_character] = _voice_content_identity(
                        old_voices, old_character
                    )
                identity = voice_identities[current_character]
                if identity is not None and old_identities[old_character] == identity:
                    eligible.append(item.queue_id)
            if not eligible:
                continue
            state = load_generation_state_from_snapshot(
                previous_state,
                old_queue,
                old_sha256,
                state_document=state,
                selected_line_ids=frozenset(
                    current_items[queue_id].line_id for queue_id in eligible
                ),
            )
            for queue_id in eligible:
                result = old_items[queue_id]
                saved[queue_id] = copy.deepcopy(result)
                sources[queue_id] = previous_state.parent / str(result["path"])
            registry = state.get("synthesis_controls")
            if isinstance(registry, dict):
                controls.update(registry)
        except (BulkGenerationError, OSError, ValueError, VoiceManifestError) as error:
            raise PregenerationQueueError(
                f"Unable to reuse saved offline audio: {error}"
            ) from error
    if not saved:
        return
    try:
        store = SharedAudioStore(current.directory.parent.parent / "audio-store")
        with staged_directory(
            current.directory.parent, prefix=".generation-reuse-"
        ) as staging:
            for queue_id, result in saved.items():
                digest = str(result["file_sha256"])
                source = sources[queue_id]
                if sha256_file(source) != digest:
                    raise ValueError("Saved recording changed before reuse")
                canonical = store.put(
                    canonical_document_sha256({"legacy_completed_audio": result}),
                    source,
                )
                link_shared_audio(canonical, source, expected_sha256=digest)
                relative = f"audio/{digest}.wav"
                link_shared_audio(canonical, staging / relative, expected_sha256=digest)
                result["path"] = relative
            state = {
                "schema": STATE_SCHEMA,
                "schema_version": STATE_VERSION,
                "queue_sha256": queue_sha256,
                "game": queue.metadata.get("game"),
                "language": queue.metadata.get("language"),
                "items": saved,
                "active": None,
                "synthesis_controls": controls,
            }
            write_generation_state(staging / "generation-state.sqlite3", state)
            load_generation_state_from_snapshot(
                staging / "generation-state.sqlite3", queue, queue_sha256
            )
            if existing is None:
                rename_directory_no_replace(staging, current.output_directory)
            else:
                with GenerationLease(
                    current.output_directory,
                    queue_sha256,
                    process_checker=process_is_alive,
                ):
                    latest = load_generation_state_from_snapshot(
                        state_path, queue, queue_sha256
                    )
                    if latest.get("active") is not None:
                        return
                    latest_items = generation_state_items(latest)
                    for queue_id, result in saved.items():
                        if queue_id not in latest_items:
                            relative = str(result["path"])
                            link_shared_audio(
                                staging / relative,
                                current.output_directory / relative,
                                expected_sha256=str(result["file_sha256"]),
                            )
                            latest_items[queue_id] = result
                    latest_controls = latest.setdefault("synthesis_controls", {})
                    if isinstance(latest_controls, dict):
                        latest_controls.update(controls)
                    write_generation_state(state_path, latest)
    except (AtomicPublicationError, BulkGenerationError, OSError, ValueError) as error:
        raise PregenerationQueueError(
            f"Unable to reuse saved offline audio: {error}"
        ) from error


def _voice_content_identity(registry: CharacterVoiceRegistry, character: str) -> object:
    voice = registry.resolve(character)
    if voice is None:
        return None
    return (
        voice.speaker,
        voice.reference_transcript,
        tuple(sha256_file(path) for path in voice.references),
    )


def _load_story(job: PregenerationJob) -> StoryIndexDocument:
    path = Path(job.story_index).expanduser().resolve()
    try:
        return load_verified_story_index_document(path, job.story_index_sha256)
    except PregenerationQueueError:
        raise
    except (OSError, StoryIndexError, ValueError) as error:
        raise PregenerationQueueError(
            f"Unable to read selected dialogue: {error}"
        ) from error


def _load_source_registry(plan: VoicePlan) -> CharacterVoiceRegistry:
    if not plan.voice_manifest:
        if plan.synthesis_backend == "pocket-tts":
            return CharacterVoiceRegistry()
        raise PregenerationQueueError(
            "Choose a narrator voice before generating offline audio"
        )
    path = Path(plan.voice_manifest).expanduser().resolve()
    try:
        before = sha256_file(path)
        if before != plan.voice_manifest_sha256:
            raise PregenerationQueueError("Character voice inventory changed")
        registry = CharacterVoiceRegistry.from_file(path)
        if sha256_file(path) != before:
            raise PregenerationQueueError(
                "Character voice inventory changed while read"
            )
        return registry
    except PregenerationQueueError:
        raise
    except (OSError, VoiceManifestError, ValueError) as error:
        raise PregenerationQueueError(
            f"Unable to read character voices: {error}"
        ) from error


def _selected_records(
    story: StoryIndexDocument, selected_line_ids: Sequence[str]
) -> tuple[StoryIndexRecord, ...]:
    by_id = {record.line_id: record for record in story.records}
    try:
        records = tuple(by_id[line_id] for line_id in selected_line_ids)
    except KeyError as error:
        raise PregenerationQueueError(
            "Selected dialogue changed after preparation was planned"
        ) from error
    if len(set(selected_line_ids)) != len(selected_line_ids):
        raise PregenerationQueueError("Selected dialogue contains duplicate identities")
    return records


def _generation_input_identity(job: PregenerationJob, plan: VoicePlan) -> str:
    return canonical_document_sha256(
        {
            "generation_input_schema_version": generation_input_schema_version,
            "job_id": job.job_id,
            "story_index_sha256": job.story_index_sha256,
            "selected_line_ids": list(job.selected_line_ids),
            "groups": [
                {
                    "group_id": group.group_id,
                    "control_sha256": group.control_sha256,
                    "route": group.route,
                    "source_id": group.source_id,
                }
                for group in plan.groups
            ],
        }
    )


def _effective_voice_routes(
    plan: VoicePlan, registry: CharacterVoiceRegistry
) -> EffectiveVoiceRoutes:
    selections: list[tuple[VoiceGroup, str, VoiceChoice]] = []
    narrator_roles: dict[str, str] = {}
    for group in plan.groups:
        target = "Narrator" if group.route == "narrator" else group.character
        if group.route == "narrator" and group.character != "Narrator":
            key = normalize_character_name(group.character)
            previous = narrator_roles.get(key)
            if previous is None or len(group.character) < len(previous):
                narrator_roles[key] = group.character
        selections.append((group, target, _voice_choice(group, plan, registry)))

    routes, line_voice_characters = _collect_effective_routes(selections)
    return {
        "routes": routes,
        "narrator_roles": tuple(sorted(narrator_roles.values(), key=str.casefold)),
        "line_voice_characters": line_voice_characters,
    }


def _voice_choice(
    group: VoiceGroup, plan: VoicePlan, registry: CharacterVoiceRegistry
) -> VoiceChoice:
    if not group.source_character:
        if group.route == "narrator" and plan.synthesis_backend == "pocket-tts":
            return (None, (), "alba")
        if group.route == "narrator":
            raise PregenerationQueueError(
                "Choose a narrator voice before generating offline audio"
            )
        raise PregenerationQueueError(
            f"Choose a voice for {group.character} before generating offline audio"
        )
    source_id = (
        group.narrator_candidate.source_id
        if group.route == "narrator" and group.narrator_candidate is not None
        else group.source_id
    )
    try:
        voice = registry.resolve_source(source_id)
    except VoiceManifestError as error:
        raise PregenerationQueueError(
            f"The selected voice for {group.character} is no longer available"
        ) from error
    if voice is None or not voice.references:
        embedded = _pocket_embedded_voice(group, plan)
        if embedded is None:
            raise PregenerationQueueError(
                f"The selected voice for {group.character} has no usable reference"
            )
        return (voice, (), embedded)
    observed = tuple(
        hashlib.sha256(read_voice_reference_bytes(voice, path)).hexdigest()
        for path in voice.references
    )
    if observed != group.reference_sha256s:
        raise PregenerationQueueError(
            f"The selected voice reference changed for {group.character}"
        )
    return (voice, observed, voice.speaker)


def _collect_effective_routes(
    selections: Sequence[tuple[VoiceGroup, str, VoiceChoice]],
) -> tuple[dict[str, VoiceChoice], dict[str, str]]:
    choices_by_target: dict[str, list[tuple[VoiceGroup, VoiceChoice]]] = {}
    for group, target, choice in selections:
        choices_by_target.setdefault(target, []).append((group, choice))

    routes: dict[str, VoiceChoice] = {}
    line_voice_characters: dict[str, str] = {}
    for target, values in choices_by_target.items():
        distinct = {(choice[2], choice[1]) for _group, choice in values}
        if target == "Narrator" and len(distinct) > 1:
            raise PregenerationQueueError("Choose one narrator voice before generation")
        canonical = (values[0][1][2], values[0][1][1])
        for group, choice in values:
            effective_target = (
                target
                if len(distinct) == 1 or (choice[2], choice[1]) == canonical
                else f"{target}__vntts_{group.group_id[:12]}"
            )
            routes[effective_target] = choice
            if effective_target != group.character and group.route != "narrator":
                line_voice_characters.update(
                    {line_id: effective_target for line_id in group.line_ids}
                )
    return routes, line_voice_characters


def _routed_story_records(
    records: Sequence[StoryIndexRecord], line_voice_characters: Mapping[str, str]
) -> list[StoryRecordDocument]:
    result: list[StoryRecordDocument] = []
    for record in records:
        value = record.to_record()
        if target := line_voice_characters.get(record.line_id):
            value["voice_character"] = target
        result.append(value)
    return result


def _write_effective_voices(
    staging: Path, effective: EffectiveVoiceRoutes
) -> list[dict[str, object]]:
    references = staging / "references"
    references.mkdir()
    copied: dict[str, str] = {}
    store = SharedAudioStore(staging.parent.parent / "audio-store")
    entries: list[dict[str, object]] = []
    for character, (voice, expected_hashes, speaker) in sorted(
        effective["routes"].items(), key=lambda item: item[0].casefold()
    ):
        relative_references = []
        for source, expected in zip(
            voice.references if voice else (), expected_hashes, strict=True
        ):
            if voice is None:
                raise PregenerationQueueError(
                    f"The selected voice for {character} has no reference source"
                )
            payload = read_voice_reference_bytes(voice, source)
            if hashlib.sha256(payload).hexdigest() != expected:
                raise PregenerationQueueError(
                    f"The selected voice reference changed for {character}"
                )
            relative = copied.get(expected)
            if relative is None:
                relative = f"references/{expected}.wav"
                _write_reference_wav(
                    staging / relative,
                    payload,
                    character,
                    store=store,
                    raw_sha256=expected,
                )
                copied[expected] = relative
            relative_references.append(relative)
        entry: dict[str, object] = {
            "character": character,
            "speaker": speaker,
            "vntts.source_character": (
                voice.source_character or voice.character if voice else speaker
            ),
            "aliases": [],
            "references": relative_references,
        }
        if voice is not None and voice.reference_transcript:
            entry["vntts.reference_transcript"] = voice.reference_transcript
        entries.append(entry)
    return entries


def _pocket_embedded_voice(group: VoiceGroup, plan: VoicePlan) -> str | None:
    if plan.synthesis_backend != "pocket-tts":
        return None
    candidates = [
        group.source_speaker,
        group.source_id.removeprefix("preset:")
        if group.source_id.startswith("preset:")
        else None,
    ]
    return next(
        (value for value in candidates if value in pocket_tts_preset_voices), None
    )


def _generation_queue_routes(
    queue: VoiceGenerationQueue, effective: EffectiveVoiceRoutes
) -> tuple[tuple[str, ...], tuple[str, ...], int]:
    roles = set(effective["routes"]) | set(effective["narrator_roles"])
    projections: list[str] = []
    omissions: list[str] = []
    ready_items = 0
    for item in queue.items:
        plan = audio_event_plan_for_record(item)
        spoken = False
        if isinstance(plan, dict) and plan.get("requires_composition"):
            spoken = bool(plan.get("spoken_text"))
            (projections if spoken else omissions).append(item.queue_id)
        if (
            item.action == "generate"
            and (spoken or not isinstance(plan, dict))
            and synthesis_character_for_line(item.speaker, item.voice_character)
            in roles
        ):
            ready_items += 1
    return tuple(sorted(projections)), tuple(sorted(omissions)), ready_items


def _write_reference_wav(
    path: Path,
    payload: bytes,
    character: str,
    *,
    store: SharedAudioStore,
    raw_sha256: str,
) -> None:
    key = f"reference-pcm16-v1-{raw_sha256}"
    store.directory.mkdir(parents=True, exist_ok=True)
    with exclusive_advisory_lock(
        store.directory / ".reference-normalization.guard", blocking=True
    ):
        store.initialize()
        saved = store.get(key)
        if saved is None:
            with NamedTemporaryFile(
                suffix=".wav", dir=store.directory, delete=False
            ) as output:
                temporary = Path(output.name)
            try:
                _normalize_reference_wav(temporary, payload, character)
                saved = store.put(key, temporary)
            finally:
                temporary.unlink(missing_ok=True)
    link_shared_audio(saved, path)


def _normalize_reference_wav(path: Path, payload: bytes, character: str) -> None:
    try:
        samples, sample_rate = sf.read(
            io.BytesIO(payload), dtype="float32", always_2d=True
        )
    except (RuntimeError, TypeError, ValueError) as error:
        raise PregenerationQueueError(
            f"Unable to decode the selected voice for {character}: {error}"
        ) from error
    if samples.size == 0 or sample_rate <= 0 or not np.isfinite(samples).all():
        raise PregenerationQueueError(
            f"The selected voice for {character} contains invalid audio"
        )
    mono = samples.mean(axis=1, dtype=np.float64)
    write_pcm16_wav(path, mono, int(sample_rate))


def _publish_input(
    catalog: PreparationCatalog,
    job_id: str,
    document: dict[str, object],
    staging: Path,
    destination: Path,
) -> None:
    _sync_input_directory(staging)
    with exclusive_advisory_lock(_input_publication_lock(destination), blocking=True):
        _recover_input_publication(catalog, destination)
        if not destination.exists():
            try:
                catalog.save_input(job_id, document, destination, staging=staging)
                rename_directory_no_replace(staging, destination)
                _sync_directory(destination.parent)
            except BaseException:
                if not destination.exists():
                    catalog.discard_unpublished_input(destination)
                raise
            catalog.save_input(job_id, document, destination)


def _input_publication_lock(directory: Path) -> Path:
    return directory.parent / f".{directory.name}.guard"


def _sync_input_directory(directory: Path) -> None:
    paths = tuple(directory.rglob("*"))
    for path in paths:
        if path.is_symlink() or path.is_junction():
            raise ValueError("Generation input assets must not be links")
        if path.is_file():
            _sync_regular_file(path)
    for path in reversed(paths):
        if path.is_dir():
            _sync_directory(path)
    _sync_directory(directory)


def _validate_input_publication(
    directory: Path, document: dict[str, object], *, assets: Path | None = None
) -> str:
    assets = assets or directory
    if document.get("schema_version") != generation_input_schema_version:
        raise ValueError("Unsupported offline generation input schema")
    identity = _required_text(document.get("identity"), "Generation input identity")
    if directory.name != f"generation-input-{identity[:16]}":
        raise ValueError("Generation input directory identity changed")
    job_id = _required_text(document.get("job_id"), "Generation input job")
    if directory.parent.name != job_id:
        raise ValueError("Generation input job directory changed")
    for name, suffix in (
        ("story_index", "story-index.jsonl"),
        ("voice_manifest", "voice-manifest.json"),
        ("queue", "queue.jsonl"),
        ("source_audio_semantic_evidence", "source-audio-semantic-evidence.json"),
    ):
        expected = document.get(f"{name}_sha256")
        if name == "source_audio_semantic_evidence" and expected is None:
            continue
        if sha256_file(assets / suffix) != expected:
            raise ValueError(f"Generation input {name} changed")
    for name in ("queue_items", "ready_items"):
        _nonnegative_int(document.get(name), name)
    roles = document.get("narrator_fallback_roles")
    if not isinstance(roles, list) or any(
        not isinstance(role, str) or not role.strip() for role in roles
    ):
        raise ValueError("Generation input narrator roles are malformed")
    return job_id


def _recover_input_publication(catalog: PreparationCatalog, directory: Path) -> None:
    staging = catalog.input_staging(directory)
    if staging is None:
        return
    if (
        directory.parent.parent.resolve() != catalog.path.parent.resolve()
        or staging.parent != directory.parent.resolve()
        or not staging.name.startswith(".generation-input-")
        or any(
            path.is_symlink() or path.is_junction()
            for path in (directory, directory.parent, staging)
        )
    ):
        raise ValueError(
            "Generation input publication leaves its preparation directory"
        )
    document = catalog.input_document(directory)
    if document is None:
        raise ValueError("Generation input publication lost its catalog metadata")
    if not directory.exists():
        if not staging.is_dir():
            raise ValueError("Generation input publication lost its staged assets")
        _validate_input_publication(directory, document, assets=staging)
        rename_directory_no_replace(staging, directory)
        _sync_directory(directory.parent)
    job_id = _validate_input_publication(directory, document)
    catalog.save_input(job_id, document, directory)


def load_generation_input_metadata(directory: Path) -> dict[str, object]:
    """Read SQL metadata, completing a durable pending publication if necessary."""
    catalog = PreparationCatalog(directory.parent.parent, owned_root=True)
    if catalog.input_staging(directory) is not None:
        with exclusive_advisory_lock(_input_publication_lock(directory), blocking=True):
            _recover_input_publication(catalog, directory)
    document = catalog.input_document(directory)
    if document is None:
        raise ValueError(
            "Generation input metadata is missing from preparation.sqlite3; "
            "restore the catalog or run the explicit legacy migration"
        )
    if document.get("schema_version") != generation_input_schema_version:
        raise ValueError("Unsupported offline generation input schema")
    return document


def _load_existing(
    directory: Path, identity: str, effective: EffectiveVoiceRoutes
) -> PregenerationInput:
    try:
        document = load_generation_input_metadata(directory)
        if document.get("identity") != identity:
            raise ValueError("generation input identity changed")
        paths = {
            "story_index": directory / "story-index.jsonl",
            "voice_manifest": directory / "voice-manifest.json",
            "queue": directory / "queue.jsonl",
        }
        queue, queue_sha256 = load_stable_generation_queue(paths["queue"])
        for name, path in paths.items():
            digest = queue_sha256 if name == "queue" else sha256_file(path)
            if digest != document.get(f"{name}_sha256"):
                raise ValueError(f"{name.replace('_', ' ')} changed")
        roles = document.get("narrator_fallback_roles")
        if not isinstance(roles, list) or not all(
            isinstance(value, str) and value.strip() for value in roles
        ):
            raise ValueError("narrator fallback roles are invalid")
        if set(roles) != set(effective["narrator_roles"]):
            raise ValueError("narrator fallback roles changed")
        projection_ids, omission_ids, expected_ready = _generation_queue_routes(
            queue, effective
        )
        event_routes = {}
        for name, expected in zip(
            ("audio_event_projection_queue_ids", "audio_event_omission_queue_ids"),
            (projection_ids, omission_ids),
            strict=True,
        ):
            values = document.get(name)
            if (
                not isinstance(values, list)
                or not all(isinstance(value, str) and value.strip() for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"{name.replace('_', ' ')} are invalid")
            if set(values) != set(expected):
                raise ValueError(f"{name.replace('_', ' ')} changed")
            event_routes[name] = tuple(values)
        queue_items = _nonnegative_int(document.get("queue_items"), "queue items")
        ready_items = _nonnegative_int(document.get("ready_items"), "ready items")
        if (queue_items, ready_items) != (len(queue.items), expected_ready):
            raise ValueError("queue item counts changed")
        semantic_sha256 = document.get("source_audio_semantic_evidence_sha256")
        semantic_path = None
        if semantic_sha256 is not None:
            semantic_path = directory / "source-audio-semantic-evidence.json"
            if sha256_file(semantic_path) != semantic_sha256:
                raise ValueError("source audio semantic evidence changed")
        return PregenerationInput(
            identity=identity,
            directory=directory,
            story_index=paths["story_index"],
            voice_manifest=paths["voice_manifest"],
            queue=paths["queue"],
            queue_sha256=_required_text(document.get("queue_sha256"), "queue SHA-256"),
            queue_items=queue_items,
            ready_items=ready_items,
            narrator_fallback_roles=tuple(roles),
            story_index_sha256=_required_text(
                document.get("story_index_sha256"), "story index SHA-256"
            ),
            voice_manifest_sha256=_required_text(
                document.get("voice_manifest_sha256"), "voice manifest SHA-256"
            ),
            source_audio_semantic_evidence_sha256=(
                None
                if semantic_sha256 is None
                else _required_text(
                    semantic_sha256, "source audio semantic evidence SHA-256"
                )
            ),
            audio_event_projection_queue_ids=event_routes[
                "audio_event_projection_queue_ids"
            ],
            audio_event_omission_queue_ids=event_routes[
                "audio_event_omission_queue_ids"
            ],
            source_audio_semantic_evidence=semantic_path,
        )
    except (BulkGenerationError, OSError, TypeError, ValueError) as error:
        raise PregenerationQueueError(
            f"Saved offline generation inputs are invalid: {error}"
        ) from error


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _raise_if_cancelled(cancellation: _Cancellation | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise PregenerationQueueCancelled("Offline preparation was cancelled")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _file_size(path: str | Path | None) -> int:
    if path is None:
        return 0
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _record_input_phase(
    name: str, started: float, cpu_started: float, **details: object
) -> None:
    record_background_operation(
        f"pregeneration-input-{name}",
        (perf_counter() - started) * 1000,
        "complete",
        cpu_ms=(process_time() - cpu_started) * 1000,
        **details,
    )


__all__ = [
    "PregenerationInput",
    "PregenerationInputStore",
    "PregenerationQueueCancelled",
    "PregenerationQueueError",
]

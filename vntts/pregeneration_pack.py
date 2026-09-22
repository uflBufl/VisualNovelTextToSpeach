"""Atomic portable game-pack publication for self-service pregeneration."""

from __future__ import annotations

import copy
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import perf_counter, process_time
from typing import Protocol

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import GamePackError, write_game_pack
from vntts_artifacts.generated_audio import (
    GeneratedAudioDocument,
    GeneratedAudioManifestError,
    load_generated_audio_document,
    write_generated_audio_manifest,
)
from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    StoryIndexRecord,
    load_story_index_document,
    write_story_index_document,
)
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)
from vntts_artifacts.voice_manifest import (
    VoiceManifestEntry,
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
    write_voice_manifest,
)

from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.generation_manifest import approved_manifest_entries
from vntts.authoring.generation_state import (
    AUDIO_EVENT_OMISSION_REASON,
    AUDIO_EVENT_OMISSION_SCHEMA,
    AUDIO_EVENT_OMISSION_VERSION,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.chapter_voice_preload import (
    _source_audio_covers_full_line,
    _validated_source_audio_line_ids,
)
from vntts.document_identity import is_lowercase_sha256
from vntts.game_pack import GamePackImport, import_game_pack
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.pregeneration_contract import (
    OfflineGenerationCancelled,
    OfflineGenerationError,
    OfflineGenerationResult,
)
from vntts.pregeneration_generation import (
    _generation_output,
)
from vntts.pregeneration_queue import (
    PregenerationInput,
    project_source_audio_semantics,
)
from vntts.pregeneration_setup import (
    GameContent,
    PregenerationJob,
    PregenerationJobStore,
    load_verified_story_index_document,
)
from vntts.source_audio_semantics import (
    SourceAudioSemanticEvidence,
    canonical_document_sha256,
    load_source_audio_semantic_evidence,
)
from vntts.support import record_background_operation

JsonObject = dict[str, object]
JsonRecords = list[JsonObject]
GenerationState = JsonObject


class Cancellation(Protocol):
    def is_set(self) -> bool: ...


class OfflinePackError(OfflineGenerationError):
    """A validated self-service result could not be published portably."""


@dataclass(frozen=True)
class OfflinePackResult:
    identity: str
    directory: Path
    manifest: Path
    imported: GamePackImport
    approved: int
    live_fallbacks: int
    story_lines: int = 0
    omissions: int = 0


@dataclass(frozen=True)
class OfflinePreparationChanges:
    reused: int
    new: int
    failed: int
    live_fallbacks: int
    omissions: int
    original: int
    replacement_candidates: int
    preserved: int
    switches_pack: bool = False


@dataclass(frozen=True)
class StoryAudioCoverage:
    title: str
    manifest: Path | None
    original: int = 0
    generated: int = 0
    live: int = 0
    omitted: int = 0
    non_spoken: int = 0
    missing: int = 0


def inspect_story_audio(
    content: GameContent,
    selection_id: str,
    job_store: PregenerationJobStore,
    *,
    manifest: str | Path | None = None,
    imported_pack: GamePackImport | None = None,
) -> StoryAudioCoverage:
    """Verify one story against one saved pack; never combine incompatible packs."""
    selection = next(
        value for value in content.selections if value.selection_id == selection_id
    )
    try:
        source = load_verified_story_index_document(
            content.story_index, content.story_index_sha256
        )
    except (OSError, StoryIndexError, ValueError) as error:
        raise OfflinePackError(
            "Story content changed. Refresh the story list and retry."
        ) from error
    explicit_pack = manifest is not None or imported_pack is not None
    if imported_pack is not None:
        manifest = imported_pack.pack.manifest_path
    elif manifest is not None:
        manifest = Path(manifest).expanduser().resolve()
    else:
        manifests = [
            path
            for job in job_store.jobs_for_content(content)
            if selection_id in job.selected_story_ids
            for path in job_store.published_packs(job)
        ]
        manifest = max(
            manifests,
            key=lambda path: (path.stat().st_mtime_ns, str(path)),
            default=None,
        )
    library = None
    pack_records: dict[str, StoryIndexRecord] = {}
    pack_story: StoryIndexDocument | None = None
    source_audio_line_ids = _validated_source_audio_line_ids(
        content.story_index,
        source,
    )
    pack_source_audio_line_ids: frozenset[str] = frozenset()
    if manifest is not None:
        imported_pack = imported_pack or import_game_pack(manifest)
        pack_story = load_story_index_document(imported_pack.story_index)
        pack_source_audio_line_ids = _validated_source_audio_line_ids(
            imported_pack.story_index,
            pack_story,
        )
        pack_records = {record.line_id: record for record in pack_story.records}
        if imported_pack.generated_audio_manifest is not None:
            library = GeneratedAudioLibrary(
                load_generated_audio_document(imported_pack.generated_audio_manifest),
                cache_size=1,
            )
    counts = dict(
        original=0,
        generated=0,
        live=0,
        omitted=0,
        non_spoken=0,
        missing=0,
    )
    line_ids = set(selection.line_ids)
    for record in source.records:
        if record.line_id not in line_ids:
            continue
        saved = pack_records.get(record.line_id)
        if saved is not None and saved.text_sha256 != record.text_sha256:
            raise OfflinePackError(
                "Saved story text differs from the selected content. Prepare this story again."
            )
        # Published source semantics can distinguish speech from a game sound cue.
        effective = saved or record
        completion_document = (
            pack_story if saved is not None and pack_story is not None else source
        )
        completion_contract = completion_document.metadata.get(
            "source_audio_completion"
        )
        semantic_authorized = record.line_id in (
            pack_source_audio_line_ids if saved is not None else source_audio_line_ids
        )
        if explicit_pack and saved is None:
            route = "missing"
        elif library and library.find_audio_event_omission(
            record.line_id, record.text_sha256
        ):
            route = "omitted"
        elif not effective.speakable:
            route = "non_spoken"
        elif (
            library
            and library.index.find(
                record.line_id, record.text_sha256, verify_file=False
            )
            is not None
        ):
            if library.find(record.line_id, record.text_sha256) is None:
                raise OfflinePackError(
                    f"Saved audio is missing or damaged for {record.line_id}. Prepare this story again."
                )
            route = "generated"
        elif library and library.find_live_fallback(record.line_id, record.text_sha256):
            route = "live"
        elif _source_audio_covers_full_line(
            effective.document,
            completion_contract=completion_contract,
            semantic_authorized=semantic_authorized,
        ):
            route = "original"
        else:
            route = "missing"
        counts[route] += 1
    manifest_path = Path(manifest) if manifest is not None else None
    return StoryAudioCoverage(selection.title, manifest_path, **counts)


class OfflinePackPublisher:
    def __init__(self, *, base_pack: str | Path | None = None) -> None:
        self.base_pack = Path(base_pack).expanduser().resolve() if base_pack else None

    def inspect_changes(
        self,
        job: PregenerationJob,
        generation_input: PregenerationInput,
        cancel_event: Cancellation | None = None,
    ) -> OfflinePreparationChanges:
        """Read-only forecast using the same validated base and resume state as publication."""
        _raise_if_cancelled(cancel_event)
        story = load_story_index_document(generation_input.story_index)
        base, _source = _load_incremental_base(self.base_pack, job, story)
        queue = VoiceGenerationQueue.load(generation_input.queue)
        if sha256_file(generation_input.queue) != generation_input.queue_sha256:
            raise OfflinePackError("Generation queue changed before confirmation")
        state_path = _generation_output(generation_input) / "generation-state.json"
        state = (
            load_generation_state(state_path, generation_input.queue)
            if state_path.exists()
            else {"items": {}}
        )
        state_items = _state_items(state)
        results = tuple(state_items.values())
        saved = {
            result["line_id"]: result["file_sha256"]
            for result in results
            if result.get("status") in {"generated", "approved"}
        }
        failed = sum(result.get("status") == "failed" for result in results)
        live = sum(result.get("status") == "live_fallback" for result in results)
        omitted = set(generation_input.audio_event_omission_queue_ids) | {
            queue_id
            for queue_id, result in state_items.items()
            if result.get("status") in {"omitted", "not_reproducible"}
        }
        selected = {record.line_id for record in story.records}
        replacements = preserved = 0
        if base is not None and base.generated_audio_manifest is not None:
            document = load_generated_audio_document(base.generated_audio_manifest)
            library = GeneratedAudioLibrary(document, cache_size=1)
            for record in document.records:
                _raise_if_cancelled(cancel_event)
                if library.find(record.line_id, record.text_sha256) is None:
                    raise OfflinePackError(
                        "Existing recording failed verification before confirmation"
                    )
                if record.line_id not in selected:
                    preserved += 1
                elif saved.get(record.line_id) != record.audio_sha256:
                    replacements += 1
        queued = {item.line_id for item in queue.items}
        authoritative_source_lines = _validated_source_audio_line_ids(
            generation_input.story_index, story
        )
        source_completion = story.metadata.get("source_audio_completion")
        original = sum(
            record.speakable
            and record.line_id in authoritative_source_lines
            and _source_audio_covers_full_line(
                record.document,
                completion_contract=source_completion,
                semantic_authorized=True,
            )
            and record.line_id not in queued
            for record in story.records
        )
        _raise_if_cancelled(cancel_event)
        return OfflinePreparationChanges(
            reused=len(saved),
            new=max(0, generation_input.ready_items - len(saved) - failed - live),
            failed=failed,
            live_fallbacks=live,
            omissions=len(omitted),
            original=original,
            replacement_candidates=replacements,
            preserved=preserved,
            switches_pack=self.base_pack is not None and base is None,
        )

    def publish(
        self,
        job: PregenerationJob,
        generation_input: PregenerationInput,
        generation_result: OfflineGenerationResult,
        cancel_event: Cancellation | None = None,
    ) -> OfflinePackResult:
        _validate_inputs(job, generation_input, generation_result)
        _raise_if_cancelled(cancel_event)
        phase_started, cpu_started = perf_counter(), process_time()
        try:
            story = load_story_index_document(generation_input.story_index)
            state_sha256 = sha256_file(generation_result.state)
        except (
            OSError,
            StoryIndexError,
            ValueError,
        ) as error:
            raise OfflinePackError(
                f"Unable to inspect prepared audio: {error}"
            ) from error
        base, source_story = _load_incremental_base(self.base_pack, job, story)
        base_identity = (
            None
            if base is None
            else base.pack.extensions["vntts.self-service"]["identity"]
        )
        identity = _identity(generation_input, state_sha256, base_identity)
        destination = (
            generation_input.directory.parent / "game-packs" / (f"pack-{identity[:24]}")
        )
        _record_publication_phase("identity", phase_started, cpu_started)
        if destination.is_dir():
            phase_started, cpu_started = perf_counter(), process_time()
            result = _load_existing(destination, identity)
            _record_publication_phase(
                "reuse", phase_started, cpu_started, cache_state="disk"
            )
            return result
        phase_started, cpu_started = perf_counter(), process_time()
        state, _queue, voice_document, voices, current_omissions = (
            _load_terminal_generation(
                job, generation_input, generation_result, state_sha256
            )
        )
        _record_publication_phase("terminal-load", phase_started, cpu_started)
        phase_started, cpu_started = perf_counter(), process_time()
        _ensure_pack_disk_space(
            destination.parent,
            base,
            story,
            state,
            generation_result,
            generation_input,
            voice_document,
            voices,
        )
        _record_publication_phase("disk-preflight", phase_started, cpu_started)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with staged_directory(
                destination.parent, prefix=f".{destination.name}."
            ) as staging:
                story_copy = staging / "story" / "story-index.jsonl"
                voice_copy = staging / "voices" / "voice-manifest.json"
                generated_copy = staging / "generated" / "manifest.json"
                phase_started, cpu_started = perf_counter(), process_time()
                if base is None:
                    _copy_file(generation_input.story_index, story_copy)
                    _copy_file(generation_input.voice_manifest, voice_copy)
                    _copy_voice_references(
                        generation_input.voice_manifest,
                        voice_copy,
                        voice_document,
                        voices,
                    )
                    published_story = story
                else:
                    published_story, semantic_copy, semantic_document = (
                        _write_cumulative_story(
                            base,
                            source_story,
                            generation_input,
                            story_copy,
                        )
                    )
                    _write_cumulative_voices(
                        base,
                        generation_input.voice_manifest,
                        voice_document,
                        voices,
                        voice_copy,
                    )
                _record_publication_phase(
                    "story-and-voices", phase_started, cpu_started
                )
                _raise_if_cancelled(cancel_event)
                phase_started, cpu_started = perf_counter(), process_time()
                generated_records, live_fallbacks, omissions = _write_cumulative_routes(
                    base,
                    story,
                    state,
                    generation_result,
                    generated_copy,
                    current_omissions,
                )
                write_generated_audio_manifest(
                    generated_copy,
                    {
                        "game": published_story.game,
                        "language": published_story.language,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "vntts.self-service.incremental": {
                            "schema_version": 1,
                            "base_pack_identity": base_identity,
                            "current_queue_sha256": generation_input.queue_sha256,
                        },
                        "vntts.authoring.live_fallback": {
                            "schema_version": 1,
                            "mode": "explicit",
                            "entries": live_fallbacks,
                        },
                        "vntts.authoring.audio_event_omission": {
                            "schema_version": 1,
                            "mode": "explicit",
                            "entries": omissions,
                        },
                    },
                    generated_records,
                )
                _record_publication_phase(
                    "audio-routes",
                    phase_started,
                    cpu_started,
                    files_examined=len(generated_records),
                    bytes_examined=sum(
                        _file_size(
                            generated_copy.parent
                            / Path(
                                *_safe_relative(
                                    record.get("audio"), "Generated WAV"
                                ).parts
                            )
                        )
                        for record in generated_records
                    ),
                )
                phase_started, cpu_started = perf_counter(), process_time()
                if base is None:
                    semantic_copy, semantic_document = _copy_semantic_evidence(
                        generation_input,
                        staging,
                        story_copy,
                    )
                components = {
                    "story_index": story_copy,
                    "voice_manifest": voice_copy,
                    "generated_audio": generated_copy,
                }
                pack_metadata = {
                    "game": {
                        "id": published_story.game or job.game,
                        "version": job.game_version or "local",
                    },
                    "producers": [{"name": "vntts-self-service", "version": "1"}],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "vntts.self-service": {
                        "schema_version": 1,
                        "identity": identity,
                        "job_id": job.job_id,
                        "generation_input_identity": generation_input.identity,
                        "source_queue_sha256": generation_input.queue_sha256,
                        "source_state_sha256": state_sha256,
                        "approved_count": len(generated_records),
                        "live_fallback_count": len(live_fallbacks),
                        "omission_count": len(omissions),
                        "story_line_count": len(published_story.records),
                        "base_pack_identity": base_identity,
                    },
                }
                if semantic_copy is not None:
                    if semantic_document is None:
                        raise OfflinePackError(
                            "Source-audio semantic evidence is missing"
                        )
                    semantic_entries = semantic_document.get("entries")
                    if not isinstance(semantic_entries, list):
                        raise OfflinePackError(
                            "Source-audio semantic evidence entries are invalid"
                        )
                    pack_metadata["vntts.authoring"] = {
                        "source_audio_semantic_evidence": {
                            "path": "story/source-audio-semantic-evidence.json",
                            "sha256": sha256_file(semantic_copy),
                            "evidence_id": semantic_document["evidence_id"],
                            "entry_count": len(semantic_entries),
                        }
                    }
                pack_manifest = staging / "game-pack.json"
                write_game_pack(pack_manifest, pack_metadata, components)
                import_game_pack(pack_manifest)
                GeneratedAudioLibrary(load_generated_audio_document(generated_copy))
                _record_publication_phase(
                    "staged-validation", phase_started, cpu_started
                )
                _raise_if_cancelled(cancel_event)
                phase_started, cpu_started = perf_counter(), process_time()
                try:
                    rename_directory_no_replace(staging, destination)
                except AtomicPublicationError:
                    if destination.is_dir():
                        return _load_existing(destination, identity)
                    raise
                _record_publication_phase("atomic-publish", phase_started, cpu_started)
                phase_started, cpu_started = perf_counter(), process_time()
                result = _load_existing(destination, identity)
                if result.approved != len(
                    generated_records
                ) or result.live_fallbacks != len(live_fallbacks):
                    raise OfflinePackError("Published offline pack counts changed")
                _record_publication_phase(
                    "published-validation", phase_started, cpu_started
                )
                return result
        except OfflineGenerationCancelled:
            raise
        except (
            AtomicPublicationError,
            BulkGenerationError,
            GamePackError,
            GeneratedAudioManifestError,
            OSError,
            StoryIndexError,
            ValueError,
            VoiceManifestError,
        ) as error:
            raise OfflinePackError(
                f"Unable to publish offline pack: {error}"
            ) from error


def load_saved_pack(manifest: str | Path) -> OfflinePackResult:
    """Load one self-service pack only after its published identity validates."""
    try:
        imported = import_game_pack(manifest)
        extension = imported.pack.extensions.get("vntts.self-service")
        identity = extension.get("identity") if isinstance(extension, dict) else None
        if not isinstance(identity, str) or not is_lowercase_sha256(identity):
            raise OfflinePackError("Saved offline pack identity is invalid")
        return _load_existing(
            imported.pack.manifest_path.parent, identity, imported=imported
        )
    except OfflinePackError:
        raise
    except (GamePackError, OSError, ValueError) as error:
        raise OfflinePackError(f"Unable to load saved offline pack: {error}") from error


def _ensure_pack_disk_space(
    destination_parent: Path,
    base: GamePackImport | None,
    story: StoryIndexDocument,
    state: GenerationState,
    generation_result: OfflineGenerationResult,
    generation_input: PregenerationInput,
    voice_document: JsonObject,
    voices: tuple[VoiceManifestEntry, ...],
) -> None:
    try:
        required = _pack_staging_bytes(
            base,
            story,
            state,
            generation_result,
            generation_input,
            voice_document,
            voices,
        )
        free = shutil.disk_usage(_existing_parent(destination_parent)).free
    except OSError as error:
        raise OfflinePackError(
            f"Unable to inspect files needed for offline pack publication: {error}"
        ) from error
    if free < required:
        raise OfflinePackError(
            "Not enough free disk space to publish the offline pack: "
            f"need about {_megabytes(required)} MB for staging, have "
            f"{_megabytes(free)} MB. Free space or choose fewer stories, then "
            "retry; saved work stays."
        )


def _pack_staging_bytes(
    base: GamePackImport | None,
    story: StoryIndexDocument,
    state: GenerationState,
    generation_result: OfflineGenerationResult,
    generation_input: PregenerationInput,
    voice_document: JsonObject,
    voices: tuple[VoiceManifestEntry, ...],
) -> int:
    copies: dict[str, Path] = {}
    for record in approved_manifest_entries(state, generation_result.output):
        relative = _safe_relative(record["audio"], "Generated WAV")
        copies[f"audio/{record['audio_sha256']}.wav"] = generation_result.output / Path(
            *relative.parts
        )
    metadata = [generation_input.story_index, generation_input.voice_manifest]
    if generation_input.source_audio_semantic_evidence is not None:
        metadata.append(generation_input.source_audio_semantic_evidence)
    _add_voice_reference_copies(
        copies,
        generation_input.voice_manifest,
        voice_document,
        voices,
        portable=base is not None,
    )
    if base is not None:
        metadata.extend((base.story_index, base.voice_manifest))
        base_evidence = base.story_index.parent / "source-audio-semantic-evidence.json"
        if base_evidence.is_file():
            metadata.append(base_evidence)
        base_document, base_voices = load_voice_manifest(
            base.voice_manifest,
            allow_legacy=False,
        )
        _add_voice_reference_copies(
            copies,
            base.voice_manifest,
            base_document,
            base_voices,
            portable=True,
        )
        if base.generated_audio_manifest is not None:
            current_line_ids = {record.line_id for record in story.records}
            for record in load_generated_audio_document(
                base.generated_audio_manifest
            ).records:
                if record.line_id not in current_line_ids:
                    copies[f"audio/{record.audio_sha256}.wav"] = record.audio
    return int(
        1_048_576
        + sum(path.stat().st_size for path in copies.values())
        + sum(path.stat().st_size for path in dict.fromkeys(metadata))
    )


def _add_voice_reference_copies(
    copies: dict[str, Path],
    source_manifest: Path,
    document: JsonObject,
    voices: tuple[VoiceManifestEntry, ...],
    *,
    portable: bool,
) -> None:
    raw_voices = document.get("voices")
    if not isinstance(raw_voices, list) or len(raw_voices) != len(voices):
        raise OfflinePackError("Offline voice manifest changed")
    for raw, voice in zip(raw_voices, voices, strict=True):
        if tuple(raw.get("references") or ()) != voice.references:
            raise OfflinePackError("Offline voice manifest changed")
        for configured in voice.references:
            relative = _safe_relative(configured, "Voice reference")
            source = source_manifest.parent / Path(*relative.parts)
            target = (
                f"references/{sha256_file(source)}.wav" if portable else str(relative)
            )
            copies[target] = source


def _megabytes(value: int) -> int:
    return max(1, (value + 999_999) // 1_000_000)


def _existing_parent(path: str | Path) -> Path:
    path = Path(path)
    while not path.exists():
        path = path.parent
    return path


def _load_terminal_generation(
    job: PregenerationJob,
    generation_input: PregenerationInput,
    generation_result: OfflineGenerationResult,
    state_sha256: str,
) -> tuple[
    GenerationState,
    VoiceGenerationQueue,
    JsonObject,
    tuple[VoiceManifestEntry, ...],
    JsonRecords,
]:
    try:
        state = load_generation_state(
            generation_result.state,
            generation_input.queue,
        )
        queue = VoiceGenerationQueue.load(generation_input.queue)
        voice_document, voices = load_voice_manifest(
            generation_input.voice_manifest,
            allow_legacy=False,
        )
        omissions = _self_service_omission_records(
            job,
            generation_input,
            state_sha256,
            queue,
        )
        omission_queue_ids: set[str] = set()
        for value in omissions:
            queue_id = value.get("queue_id")
            if not isinstance(queue_id, str):
                raise OfflinePackError("Offline omission queue identity is invalid")
            omission_queue_ids.add(queue_id)
        _require_terminal_generation(
            state,
            queue,
            omission_queue_ids=omission_queue_ids,
        )
        return state, queue, voice_document, voices, omissions
    except (
        BulkGenerationError,
        GamePackError,
        GeneratedAudioManifestError,
        OSError,
        StoryIndexError,
        ValueError,
        VoiceGenerationQueueError,
        VoiceManifestError,
    ) as error:
        raise OfflinePackError(f"Unable to inspect prepared audio: {error}") from error


def _validate_inputs(
    job: object, generation_input: object, generation_result: object
) -> None:
    if not isinstance(job, PregenerationJob):
        raise OfflinePackError("Offline preparation job is invalid")
    if not isinstance(generation_input, PregenerationInput):
        raise OfflinePackError("Offline generation input is invalid")
    if not isinstance(generation_result, OfflineGenerationResult):
        raise OfflinePackError("Offline generation result is invalid")
    expected_output = generation_input.directory.parent / (
        f"generation-output-{generation_input.identity[:16]}"
    )
    if generation_result.output.resolve() != expected_output.resolve():
        raise OfflinePackError("Offline pack output identity changed")


def _require_terminal_generation(
    state: GenerationState,
    queue: VoiceGenerationQueue,
    *,
    omission_queue_ids: Iterable[str] = (),
) -> None:
    if state.get("active") is not None:
        raise OfflinePackError("Offline generation is still active")
    expected = {item.queue_id for item in queue.items if item.action == "generate"}
    items = _state_items(state)
    actual = set(items)
    omission_queue_ids = set(omission_queue_ids)
    if actual != expected - omission_queue_ids:
        raise OfflinePackError("Offline generation does not cover the selected queue")
    for queue_id in sorted(expected - omission_queue_ids):
        item = items[queue_id]
        status = item.get("status")
        review = item.get("review_status")
        if (status, review) not in {
            ("approved", "approved"),
            ("live_fallback", "live_fallback"),
        }:
            raise OfflinePackError(
                f"Offline generation item is not terminal: {queue_id!r}"
            )


def _state_items(state: JsonObject) -> dict[str, JsonObject]:
    raw_items = state.get("items")
    if not isinstance(raw_items, dict) or any(
        not isinstance(queue_id, str) or not isinstance(item, dict)
        for queue_id, item in raw_items.items()
    ):
        raise OfflinePackError("Offline generation state items are malformed")
    return {
        queue_id: item
        for queue_id, item in raw_items.items()
        if isinstance(queue_id, str) and isinstance(item, dict)
    }


def _identity(
    generation_input: PregenerationInput,
    state_sha256: str,
    base_pack_identity: str | None = None,
) -> str:
    payload = {
        "schema_version": 1,
        "generation_input_identity": generation_input.identity,
        "queue_sha256": generation_input.queue_sha256,
        "state_sha256": state_sha256,
        "story_index_sha256": sha256_file(generation_input.story_index),
        "voice_manifest_sha256": sha256_file(generation_input.voice_manifest),
        "semantic_evidence_sha256": (
            None
            if generation_input.source_audio_semantic_evidence is None
            else sha256_file(generation_input.source_audio_semantic_evidence)
        ),
    }
    if base_pack_identity is not None:
        payload["base_pack_identity"] = base_pack_identity
    return str(canonical_document_sha256(payload))


def _load_incremental_base(
    path: Path | None,
    job: PregenerationJob,
    selected_story: StoryIndexDocument,
) -> tuple[GamePackImport | None, StoryIndexDocument | None]:
    if path is None or not path.is_file():
        return None, None
    try:
        imported = import_game_pack(path)
        extension = imported.pack.extensions.get("vntts.self-service")
        if not isinstance(extension, dict) or not is_lowercase_sha256(
            extension.get("identity")
        ):
            return None, None
        if imported.pack.game_id != (
            selected_story.game or job.game
        ) or imported.pack.game_version != (job.game_version or "local"):
            return None, None
        source_path = Path(job.story_index).expanduser().resolve()
        if sha256_file(source_path) != job.story_index_sha256:
            raise OfflinePackError("Selected source story changed")
        source = load_story_index_document(source_path)
        base_story = load_story_index_document(imported.story_index)
    except (GamePackError, OSError, StoryIndexError, ValueError) as error:
        raise OfflinePackError(
            f"Unable to inspect the active offline pack: {error}"
        ) from error
    if (
        source.game != selected_story.game
        or source.language != selected_story.language
        or base_story.game != source.game
        or base_story.language != source.language
    ):
        return None, None
    source_by_id = {record.line_id: record for record in source.records}
    if any(
        record.line_id not in source_by_id
        or source_by_id[record.line_id].text_sha256 != record.text_sha256
        for record in base_story.records
    ):
        return None, None
    return imported, source


def _write_cumulative_story(
    base: GamePackImport,
    source_story: StoryIndexDocument,
    generation_input: PregenerationInput,
    story_copy: Path,
) -> tuple[
    StoryIndexDocument, Path | None, SourceAudioSemanticEvidence | None
]:
    base_story = load_story_index_document(base.story_index)
    current_story = load_story_index_document(generation_input.story_index)
    selected_ids = {
        *(record.line_id for record in base_story.records),
        *(record.line_id for record in current_story.records),
    }
    records = [
        record.to_record()
        for record in source_story.records
        if record.line_id in selected_ids
    ]
    if {record["line_id"] for record in records} != selected_ids:
        raise OfflinePackError("Cumulative story selection changed")
    story_copy.parent.mkdir(parents=True, exist_ok=True)
    metadata, records, semantic_copy = project_source_audio_semantics(
        source_story.path,
        source_story.metadata,
        records,
        story_copy.parent,
    )
    published_story = write_story_index_document(story_copy, metadata, records)
    semantic_document = (
        None
        if semantic_copy is None
        else load_source_audio_semantic_evidence(semantic_copy, story_copy)
    )
    return published_story, semantic_copy, semantic_document


def _write_cumulative_voices(
    base: GamePackImport,
    current_manifest: Path,
    current_document: JsonObject,
    current_voices: tuple[VoiceManifestEntry, ...],
    target_manifest: Path,
) -> None:
    base_document, base_voices = load_voice_manifest(
        base.voice_manifest,
        allow_legacy=False,
    )
    merged = _portable_voice_entries(
        base.voice_manifest,
        target_manifest,
        base_document,
        base_voices,
    )
    for candidate in _portable_voice_entries(
        current_manifest,
        target_manifest,
        current_document,
        current_voices,
    ):
        names = {normalize_character_name(value) for value in _voice_names(candidate)}
        merged = [
            existing
            for existing in merged
            if names.isdisjoint(
                normalize_character_name(value)
                for value in _voice_names(existing)
            )
        ]
        merged.append(candidate)
    merged.sort(key=lambda value: _voice_names(value)[0].casefold())
    write_voice_manifest(target_manifest, {"version": 2, "voices": merged})


def _voice_names(entry: JsonObject) -> tuple[str, ...]:
    character = entry.get("character")
    aliases = entry.get("aliases", [])
    if not isinstance(character, str) or not isinstance(aliases, list) or any(
        not isinstance(value, str) for value in aliases
    ):
        raise OfflinePackError("Offline voice identity is malformed")
    return character, *aliases


def _portable_voice_entries(
    source_manifest: Path,
    target_manifest: Path,
    document: JsonObject,
    voices: tuple[VoiceManifestEntry, ...],
) -> JsonRecords:
    raw_voices = document.get("voices")
    if not isinstance(raw_voices, list) or len(raw_voices) != len(voices):
        raise OfflinePackError("Offline voice manifest changed")
    result: JsonRecords = []
    for raw, voice in zip(raw_voices, voices, strict=True):
        if tuple(raw.get("references") or ()) != voice.references:
            raise OfflinePackError("Offline voice references changed")
        candidate = copy.deepcopy(raw)
        candidate.pop("reference", None)
        candidate["references"] = []
        for configured in voice.references:
            relative = _safe_relative(configured, "Voice reference")
            source = source_manifest.parent / Path(*relative.parts)
            digest = sha256_file(source)
            portable = f"references/{digest}.wav"
            _copy_file(source, target_manifest.parent / portable)
            candidate["references"].append(portable)
        result.append(candidate)
    return result


def _write_cumulative_routes(
    base: GamePackImport | None,
    current_story: StoryIndexDocument,
    state: GenerationState,
    generation_result: OfflineGenerationResult,
    generated_copy: Path,
    current_omissions: JsonRecords,
) -> tuple[JsonRecords, JsonRecords, JsonRecords]:
    current_line_ids = {record.line_id for record in current_story.records}
    records: JsonRecords = []
    live_fallbacks: JsonRecords = []
    omissions: JsonRecords = []
    if base is not None:
        if base.generated_audio_manifest is None:
            raise OfflinePackError("Active self-service pack has no audio routes")
        base_generated = load_generated_audio_document(base.generated_audio_manifest)
        for record in base_generated.records:
            if record.line_id not in current_line_ids:
                records.append(
                    _portable_generated_record(
                        record.to_record(),
                        record.audio,
                        generated_copy,
                        reuse=True,
                    )
                )
        live_fallbacks.extend(
            value
            for value in _document_records(
                base_generated,
                "vntts.authoring.live_fallback",
                "live fallback",
            )
            if value.get("line_id") not in current_line_ids
        )
        omissions.extend(
            value
            for value in _document_records(
                base_generated,
                "vntts.authoring.audio_event_omission",
                "audio-event omission",
            )
            if value.get("line_id") not in current_line_ids
        )
    for record in approved_manifest_entries(state, generation_result.output):
        relative = _safe_relative(record["audio"], "Generated WAV")
        records.append(
            _portable_generated_record(
                record,
                generation_result.output / Path(*relative.parts),
                generated_copy,
            )
        )
    live_fallbacks.extend(_live_fallback_records(state))
    omissions.extend(copy.deepcopy(current_omissions))
    records.sort(key=lambda value: (value["line_id"], value["text_sha256"]))
    live_fallbacks.sort(key=lambda value: (value["line_id"], value["text_sha256"]))
    omissions.sort(key=lambda value: (value["line_id"], value["text_sha256"]))
    return records, live_fallbacks, omissions


def _portable_generated_record(
    record: JsonObject,
    source: Path,
    generated_copy: Path,
    *,
    reuse: bool = False,
) -> JsonObject:
    candidate = copy.deepcopy(record)
    portable = f"audio/{candidate['audio_sha256']}.wav"
    destination = generated_copy.parent / portable
    if reuse:
        digest = candidate.get("audio_sha256")
        if not isinstance(digest, str):
            raise OfflinePackError("Generated audio digest is invalid")
        _link_verified_file(source, destination, digest)
    else:
        _copy_file(source, destination)
    candidate["audio"] = portable
    return candidate


def _document_records(
    document: GeneratedAudioDocument, field: str, label: str
) -> JsonRecords:
    extension = document.producer_metadata.get(field)
    if extension is None:
        return []
    entries = extension.get("entries") if isinstance(extension, dict) else None
    if not isinstance(entries, list):
        raise OfflinePackError(f"Active pack {label} ledger is malformed")
    return copy.deepcopy(entries)


def _self_service_omission_records(
    job: PregenerationJob,
    generation_input: PregenerationInput,
    state_sha256: str,
    queue: VoiceGenerationQueue,
) -> JsonRecords:
    queue_by_id = {item.queue_id: item for item in queue.items}
    queue_ids = tuple(sorted(generation_input.audio_event_omission_queue_ids))
    batch_id = canonical_document_sha256(
        {
            "schema_version": 1,
            "generation_input_identity": generation_input.identity,
            "state_sha256": state_sha256,
            "queue_sha256": generation_input.queue_sha256,
            "queue_ids": list(queue_ids),
        }
    )
    authority = {
        "batch_id": batch_id,
        "base_workspace_id": f"self-service:{generation_input.identity}",
        "base_workspace_sha256": generation_input.identity,
        "base_state_sha256": state_sha256,
        "queue_sha256": generation_input.queue_sha256,
    }
    records: JsonRecords = []
    for queue_id in queue_ids:
        item = queue_by_id.get(queue_id)
        plan = None if item is None else audio_event_plan_for_record(item)
        if (
            item is None
            or item.action not in {"generate", "prefer_source_audio"}
            or (
                item.action == "prefer_source_audio"
                and item.source_audio_status
                not in {"configured_unavailable", "unavailable"}
            )
            or not isinstance(plan, dict)
            or not plan.get("requires_composition")
            or plan.get("spoken_text") != ""
        ):
            raise OfflinePackError(
                f"Offline audio-event omission is invalid: {queue_id!r}"
            )
        decision = {
            "schema": AUDIO_EVENT_OMISSION_SCHEMA,
            "schema_version": AUDIO_EVENT_OMISSION_VERSION,
            "reason": AUDIO_EVENT_OMISSION_REASON,
            "queue_id": queue_id,
            "line_id": item.line_id,
            "text_sha256": item.text_sha256,
            "speaker": item.speaker,
            "plan_sha256": plan["plan_sha256"],
            "spoken_text_sha256": plan["spoken_text_sha256"],
            "decided_at": job.created_at,
            "authority": copy.deepcopy(authority),
        }
        records.append(
            {
                **decision,
                "decision_sha256": canonical_document_sha256(decision),
            }
        )
    return records


def _live_fallback_records(state: GenerationState) -> JsonRecords:
    records: JsonRecords = []
    for item in _state_items(state).values():
        decision = item.get("live_fallback")
        if not isinstance(decision, dict):
            continue
        records.append(
            {
                **copy.deepcopy(decision),
                "decision_sha256": canonical_document_sha256(decision),
            }
        )
    return sorted(records, key=lambda value: (value["line_id"], value["text_sha256"]))


def _copy_voice_references(
    source_manifest: Path,
    target_manifest: Path,
    document: JsonObject,
    voices: tuple[VoiceManifestEntry, ...],
) -> None:
    raw_voices = document.get("voices")
    if not isinstance(raw_voices, list) or len(raw_voices) != len(voices):
        raise OfflinePackError("Offline voice manifest changed")
    for raw, voice in zip(raw_voices, voices, strict=True):
        if tuple(raw.get("references") or ()) != voice.references:
            raise OfflinePackError("Offline voice references changed")
        for configured in voice.references:
            relative = _safe_relative(configured, "Voice reference")
            _copy_file(
                source_manifest.parent / Path(*relative.parts),
                target_manifest.parent / Path(*relative.parts),
            )


def _copy_semantic_evidence(
    generation_input: PregenerationInput, staging: Path, story_copy: Path
) -> tuple[Path | None, SourceAudioSemanticEvidence | None]:
    source = generation_input.source_audio_semantic_evidence
    if source is None:
        return None, None
    destination = staging / "story" / "source-audio-semantic-evidence.json"
    _copy_file(source, destination)
    return destination, load_source_audio_semantic_evidence(destination, story_copy)


def _copy_file(source: str | Path, destination: Path) -> None:
    source = Path(source).resolve()
    if not source.is_file() or source.is_symlink():
        raise OfflinePackError(f"Offline pack source is unsafe: {source}")
    before = sha256_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if sha256_file(source) != before or sha256_file(destination) != before:
        raise OfflinePackError(f"Offline pack source changed: {source}")


def _link_verified_file(
    source: str | Path, destination: Path, expected_sha256: str
) -> None:
    source = Path(source)
    if not source.is_file() or source.is_symlink():
        raise OfflinePackError(f"Offline pack source is unsafe: {source}")
    source = source.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if sha256_file(destination) == expected_sha256:
            return
        raise OfflinePackError(f"Offline pack destination conflicts: {destination}")
    try:
        os.link(source, destination)
    except OSError:
        _copy_file(source, destination)
        return
    if sha256_file(destination) != expected_sha256:
        raise OfflinePackError(f"Offline pack source changed: {source}")


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise OfflinePackError(f"{label} path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise OfflinePackError(f"{label} leaves its component directory")
    return relative


def _load_existing(
    destination: Path,
    identity: str,
    *,
    imported: GamePackImport | None = None,
) -> OfflinePackResult:
    imported = imported or import_game_pack(destination / "game-pack.json")
    extension = imported.pack.extensions.get("vntts.self-service")
    if not isinstance(extension, dict) or extension.get("identity") != identity:
        raise OfflinePackError("Existing offline pack identity changed")
    story_lines = len(load_story_index_document(imported.story_index).records)
    if extension.get("story_line_count", story_lines) != story_lines:
        raise OfflinePackError("Existing offline pack coverage changed")
    generated = load_generated_audio_document(imported.generated_audio_manifest)
    library = GeneratedAudioLibrary(generated)
    approved = extension.get("approved_count")
    live_fallbacks = extension.get("live_fallback_count")
    omissions = extension.get("omission_count", 0)
    if (
        approved != len(generated.records)
        or live_fallbacks != len(library.live_fallbacks)
        or omissions != len(library.audio_event_omissions)
    ):
        raise OfflinePackError("Existing offline pack route counts changed")
    return OfflinePackResult(
        identity=identity,
        directory=destination,
        manifest=imported.pack.manifest_path,
        imported=imported,
        approved=approved,
        live_fallbacks=live_fallbacks,
        story_lines=story_lines,
        omissions=omissions,
    )


def _raise_if_cancelled(cancel_event: Cancellation | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OfflineGenerationCancelled("Offline pack publication was cancelled")


def _file_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _record_publication_phase(
    name: str, started: float, cpu_started: float, **details: object
) -> None:
    record_background_operation(
        f"pregeneration-publication-{name}",
        (perf_counter() - started) * 1000,
        "complete",
        cpu_ms=(process_time() - cpu_started) * 1000,
        **details,
    )


__all__ = [
    "OfflinePackError",
    "OfflinePackPublisher",
    "OfflinePackResult",
    "load_saved_pack",
]

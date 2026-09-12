"""Player-facing content selection and durable self-service preparation state."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from time import perf_counter, process_time

from platformdirs import user_data_path
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)

from vntts.application_directories import get_local_data_directory
from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.settings import AppSettings
from vntts.versioned_json import read_versioned_json, write_versioned_json

job_schema_version = 1
# ponytail: assumes 12 text chars/sec and PCM16 mono 24 kHz; upgrade with measured
# durations and the selected backend's output format if storage estimates matter.
ROUGH_SPEECH_CHARACTERS_PER_SECOND = 12
PCM16_MONO_24KHZ_BYTES_PER_SECOND = 48_000


class PregenerationSetupError(RuntimeError):
    """Player-selected story content cannot be prepared safely."""


@dataclass(frozen=True)
class StorySelection:
    selection_id: str
    title: str
    kind: str
    order: int
    line_ids: tuple[str, ...]
    line_count: int
    speakable_lines: int
    original_audio_lines: int
    generation_lines: int
    speaker_count: int
    speakers: tuple[str, ...]
    generation_text_characters: int


@dataclass(frozen=True)
class GameContent:
    provider_id: str
    game: str
    game_version: str | None
    story_index: Path
    story_index_sha256: str
    selections: tuple[StorySelection, ...]

    @property
    def display_name(self):
        version = f" {self.game_version}" if self.game_version else ""
        return f"{self.game}{version}"


@dataclass(frozen=True)
class ContentDiscovery:
    content: tuple[GameContent, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparationEstimate:
    selected_lines: int
    original_audio_lines: int
    generation_lines: int
    speaker_count: int
    estimated_generation_minutes: int
    estimated_disk_bytes: int
    generation_text_characters: int = 0
    rough_audio_seconds: int = 0


@dataclass(frozen=True)
class GenerationResourceEstimate:
    total_items: int
    remaining_items: int
    total_text_characters: int
    remaining_text_characters: int
    rough_audio_seconds: int
    remaining_rough_audio_seconds: int
    estimated_disk_bytes: int
    remaining_disk_bytes: int


@dataclass(frozen=True)
class PregenerationJob:
    job_id: str
    created_at: str
    updated_at: str
    status: str
    provider_id: str
    game: str
    game_version: str | None
    story_index: str
    story_index_sha256: str
    selected_story_ids: tuple[str, ...]
    selected_line_ids: tuple[str, ...]
    estimate: PreparationEstimate

    @classmethod
    def from_document(cls, document):
        estimate = document.get("estimate")
        if not isinstance(estimate, dict):
            raise PregenerationSetupError("Saved preparation estimate is invalid")
        try:
            return cls(
                job_id=_required_text(document, "job_id"),
                created_at=_required_text(document, "created_at"),
                updated_at=_required_text(document, "updated_at"),
                status=_required_text(document, "status"),
                provider_id=_required_text(document, "provider_id"),
                game=_required_text(document, "game"),
                game_version=_optional_text(document.get("game_version")),
                story_index=_required_text(document, "story_index"),
                story_index_sha256=_sha256_text(document, "story_index_sha256"),
                selected_story_ids=_text_tuple(document, "selected_story_ids"),
                selected_line_ids=_text_tuple(document, "selected_line_ids"),
                estimate=PreparationEstimate(
                    selected_lines=_nonnegative_int(estimate, "selected_lines"),
                    original_audio_lines=_nonnegative_int(
                        estimate, "original_audio_lines"
                    ),
                    generation_lines=_nonnegative_int(estimate, "generation_lines"),
                    speaker_count=_nonnegative_int(estimate, "speaker_count"),
                    estimated_generation_minutes=_nonnegative_int(
                        estimate, "estimated_generation_minutes"
                    ),
                    estimated_disk_bytes=_nonnegative_int(
                        estimate, "estimated_disk_bytes"
                    ),
                    generation_text_characters=_optional_nonnegative_int(
                        estimate, "generation_text_characters"
                    ),
                    rough_audio_seconds=_optional_nonnegative_int(
                        estimate, "rough_audio_seconds"
                    ),
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PregenerationSetupError(
                f"Saved preparation state is invalid: {error}"
            ) from error

    def to_document(self):
        value = asdict(self)
        value["selected_story_ids"] = list(self.selected_story_ids)
        value["selected_line_ids"] = list(self.selected_line_ids)
        return value


def inspect_story_index(path, *, provider_id="local-story-index"):
    started = perf_counter()
    cpu_started = process_time()
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise PregenerationSetupError(f"Story content was not found: {path}")
    try:
        checksum = sha256_file(path)
        cache_hits = _cached_story_index_document.cache_info().hits
        document = _cached_story_index_document(str(path), checksum)
    except (OSError, StoryIndexError, ValueError) as error:
        raise PregenerationSetupError(f"Story content is invalid: {error}") from error
    selections = _story_selections(document)
    if not selections:
        raise PregenerationSetupError("Story content has no selectable dialogue")
    metadata = document.metadata
    version = metadata.get("game_version")
    content = GameContent(
        provider_id=provider_id,
        game=document.game or "Visual novel",
        game_version=version.strip()
        if isinstance(version, str) and version.strip()
        else None,
        story_index=path,
        story_index_sha256=checksum,
        selections=selections,
    )
    from vntts.support import record_background_operation

    try:
        size = path.stat().st_size
    except OSError:
        size = None
    details = {
        "cpu_ms": round((process_time() - cpu_started) * 1000, 3),
        "files_examined": 1,
        "cache_state": (
            "hit"
            if _cached_story_index_document.cache_info().hits > cache_hits
            else "miss"
        ),
    }
    if size is not None:
        details["bytes_examined"] = size
    record_background_operation(
        "story-index-inspection",
        (perf_counter() - started) * 1000,
        "complete",
        **details,
    )
    return content


@lru_cache(maxsize=8)
def _cached_story_index_document(path, expected_sha256):
    document = load_story_index_document(path)
    if sha256_file(path) != expected_sha256:
        raise ValueError("Story content changed while it was being read")
    return document


def load_verified_story_index_document(path, expected_sha256):
    """Reuse an immutable parse while still checking the current file bytes."""
    path = Path(path).expanduser().resolve()
    if sha256_file(path) != expected_sha256:
        raise ValueError("Story content checksum changed")
    return _cached_story_index_document(str(path), expected_sha256)


@lru_cache(maxsize=8)
def _cached_story_index(path, provider_id, expected_sha256):
    content = inspect_story_index(path, provider_id=provider_id)
    if content.story_index_sha256 != expected_sha256:
        raise PregenerationSetupError(
            "Story content changed while it was being inspected. Refresh to retry."
        )
    return content


def discover_game_content(settings, *, environment=None, extra_paths=()):
    """Discover bounded, known story-index locations without scanning user files."""
    environment = os.environ if environment is None else environment
    candidates = []
    if isinstance(settings, AppSettings) and settings.story_index:
        candidates.append((settings.story_index, "configured-story-index"))
    candidates.extend((path, "selected-story-index") for path in extra_paths)
    extractor_root = environment.get("R1999_EXTRACTOR_DATA")
    extractor_root = (
        Path(extractor_root).expanduser()
        if extractor_root
        else user_data_path("Reverse1999Extractor", appauthor=False)
    )
    candidates.append(
        (
            get_local_data_directory()
            / "game-content"
            / "reverse1999"
            / "reverse1999"
            / "story-index.jsonl",
            "reverse1999",
        )
    )
    candidates.append(
        (extractor_root / "reverse1999" / "story-index.jsonl", "reverse1999")
    )

    discovered = []
    errors = []
    seen = set()
    for raw_path, provider_id in candidates:
        path = Path(raw_path).expanduser().resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        if _is_outdated_reverse1999_index(path):
            errors.append(
                "Outdated Reverse: 1999 story content was ignored. Import the "
                "installed game again to rebuild its story catalog."
            )
            continue
        try:
            discovered.append(
                _cached_story_index(str(path), provider_id, sha256_file(path))
            )
        except PregenerationSetupError as error:
            errors.append(str(error))
    discovered.sort(key=lambda content: len(content.selections), reverse=True)
    return ContentDiscovery(tuple(discovered), tuple(errors))


def estimate_preparation(content, selected_story_ids):
    selected_ids = _normalized_selection_ids(content, selected_story_ids)
    selected = tuple(
        value for value in content.selections if value.selection_id in selected_ids
    )
    generation_lines = sum(value.generation_lines for value in selected)
    speakers = set()
    for value in selected:
        if value.generation_lines:
            speakers.update(value.speakers)
    text_characters = _generation_text_characters(content, selected_ids)
    rough_audio_seconds = _rough_audio_seconds(text_characters)
    return PreparationEstimate(
        selected_lines=sum(value.line_count for value in selected),
        original_audio_lines=sum(value.original_audio_lines for value in selected),
        generation_lines=generation_lines,
        speaker_count=max(
            len(speakers), max((value.speaker_count for value in selected), default=0)
        ),
        # Retained for saved-job compatibility. Runtime duration depends on the
        # selected model and hardware, so callers should present text and storage only.
        estimated_generation_minutes=0,
        estimated_disk_bytes=rough_audio_seconds * PCM16_MONO_24KHZ_BYTES_PER_SECOND,
        generation_text_characters=text_characters,
        rough_audio_seconds=rough_audio_seconds,
    )


def estimate_generation_resources(generation_input):
    """Estimate remaining PCM WAV storage from the exact private queue."""
    try:
        if sha256_file(generation_input.queue) != generation_input.queue_sha256:
            raise PregenerationSetupError(
                "Generation queue changed before estimating storage"
            )
        queue = VoiceGenerationQueue.load(generation_input.queue)
        state_path = (
            generation_input.directory.parent
            / f"generation-output-{generation_input.identity[:16]}"
            / "generation-state.json"
        )
        state = (
            load_generation_state(state_path, generation_input.queue)
            if state_path.is_file()
            else {"items": {}}
        )
    except PregenerationSetupError:
        raise
    except (
        BulkGenerationError,
        OSError,
        ValueError,
        VoiceGenerationQueueError,
    ) as error:
        raise PregenerationSetupError(
            f"Unable to estimate remaining offline audio storage: {error}"
        ) from error
    terminal = {"generated", "approved", "live_fallback", "omitted", "not_reproducible"}
    omissions = set(generation_input.audio_event_omission_queue_ids)
    items = tuple(
        item
        for item in queue.items
        if item.action == "generate" and item.queue_id not in omissions
    )
    remaining = tuple(
        item
        for item in items
        if state["items"].get(item.queue_id, {}).get("status") not in terminal
    )
    total_characters = sum(len(item.text) for item in items)
    remaining_characters = sum(len(item.text) for item in remaining)
    total_seconds = _rough_audio_seconds(total_characters)
    remaining_seconds = _rough_audio_seconds(remaining_characters)
    return GenerationResourceEstimate(
        total_items=len(items),
        remaining_items=len(remaining),
        total_text_characters=total_characters,
        remaining_text_characters=remaining_characters,
        rough_audio_seconds=total_seconds,
        remaining_rough_audio_seconds=remaining_seconds,
        estimated_disk_bytes=total_seconds * PCM16_MONO_24KHZ_BYTES_PER_SECOND,
        remaining_disk_bytes=remaining_seconds * PCM16_MONO_24KHZ_BYTES_PER_SECOND,
    )


class PregenerationJobStore:
    def __init__(self, root=None, *, clock=None):
        self.root = Path(
            root or get_local_data_directory() / "pregeneration" / "jobs"
        ).expanduser()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _selection_path(self, content):
        checksum = _sha256_text({"checksum": content.story_index_sha256}, "checksum")
        return self.root / "selections" / f"{checksum}.json"

    def selection_for_content(self, content):
        path = self._selection_path(content)
        try:
            document = read_versioned_json(
                path, schema_version=1, document_name="story selection"
            )
        except FileNotFoundError:
            return None
        try:
            if document["story_index_sha256"] != content.story_index_sha256:
                raise ValueError("Story selection belongs to different content")
            values = document["selected_story_ids"]
            return (
                ()
                if values == []
                else _normalized_selection_ids(
                    content, _text_tuple(document, "selected_story_ids")
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PregenerationSetupError(
                f"Saved story selection is invalid: {error}"
            ) from error

    def save_selection(self, content, selected_story_ids):
        selected = tuple(selected_story_ids)
        if selected:
            selected = _normalized_selection_ids(content, selected)
        path = self._selection_path(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_versioned_json(
            path,
            1,
            {
                "story_index_sha256": content.story_index_sha256,
                "selected_story_ids": list(selected),
            },
        )

    def create_or_resume(self, content, selected_story_ids):
        selected_ids = _normalized_selection_ids(content, selected_story_ids)
        selected = tuple(
            value for value in content.selections if value.selection_id in selected_ids
        )
        line_ids = tuple(line_id for value in selected for line_id in value.line_ids)
        identity = hashlib.sha256()
        identity.update(content.story_index_sha256.encode("ascii"))
        for selection_id in selected_ids:
            identity.update(b"\0")
            identity.update(selection_id.encode("utf-8"))
        job_id = identity.hexdigest()[:24]
        path = self.path_for(job_id)
        if path.is_file():
            job = self.load(job_id)
            if (
                job.story_index_sha256 != content.story_index_sha256
                or job.selected_story_ids != selected_ids
                or job.selected_line_ids != line_ids
            ):
                raise PregenerationSetupError(
                    "Saved preparation identity conflicts with the selected story"
                )
            return job
        timestamp = self.clock().astimezone(timezone.utc).isoformat()
        job = PregenerationJob(
            job_id=job_id,
            created_at=timestamp,
            updated_at=timestamp,
            status="planned",
            provider_id=content.provider_id,
            game=content.game,
            game_version=content.game_version,
            story_index=str(content.story_index),
            story_index_sha256=content.story_index_sha256,
            selected_story_ids=selected_ids,
            selected_line_ids=line_ids,
            estimate=estimate_preparation(content, selected_ids),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        write_versioned_json(path, job_schema_version, job.to_document())
        return job

    def load(self, job_id):
        path = self.path_for(job_id)
        try:
            document = read_versioned_json(
                path,
                schema_version=job_schema_version,
                document_name="offline audio preparation",
            )
        except (OSError, TypeError, ValueError) as error:
            raise PregenerationSetupError(
                f"Unable to resume offline audio preparation: {error}"
            ) from error
        job = PregenerationJob.from_document(document)
        if job.job_id != job_id:
            raise PregenerationSetupError("Saved preparation identity changed")
        return job

    def latest_for_content(self, content):
        return max(
            self.jobs_for_content(content),
            key=lambda value: value.updated_at,
            default=None,
        )

    def jobs_for_content(self, content):
        if not self.root.is_dir():
            return ()
        matches = []
        for path in self.root.glob("*/job.json"):
            try:
                job = self.load(path.parent.name)
            except PregenerationSetupError:
                continue
            if job.story_index_sha256 == content.story_index_sha256:
                matches.append(job)
        return tuple(matches)

    def source_story_indexes(self):
        if not self.root.is_dir():
            return ()
        sources = []
        for path in self.root.glob("*/job.json"):
            try:
                job = self.load(path.parent.name)
            except PregenerationSetupError:
                continue
            source = Path(job.story_index).expanduser()
            if source.is_file():
                sources.append(source)
        return tuple(dict.fromkeys(sources))

    def prepared_story_ids(self, content):
        return frozenset(
            selection_id
            for selection_id, status in self.story_statuses(content).items()
            if status == "ready"
        )

    def story_statuses(self, content):
        statuses = {}
        for job in self.jobs_for_content(content):
            prepared = job.status == "prepared" or self._has_published_pack(job)
            for selection_id in job.selected_story_ids:
                if prepared:
                    statuses[selection_id] = "ready"
                elif statuses.get(selection_id) != "ready":
                    statuses[selection_id] = "in_progress"
        return statuses

    def _has_published_pack(self, job):
        return bool(self.published_packs(job))

    def published_packs(self, job):
        root = self.path_for(job.job_id).parent / "game-packs"
        if not root.is_dir():
            return ()
        manifests = []
        for manifest in root.glob("pack-*/game-pack.json"):
            identity = manifest.parent.name.removeprefix("pack-")
            if len(identity) == 24 and manifest.is_file():
                try:
                    int(identity, 16)
                except ValueError:
                    continue
                manifests.append(manifest)
        return tuple(manifests)

    def mark_prepared(self, job):
        if not isinstance(job, PregenerationJob):
            raise PregenerationSetupError("Preparation job is invalid")
        current = self.load(job.job_id)
        if (
            current.story_index_sha256 != job.story_index_sha256
            or current.selected_story_ids != job.selected_story_ids
            or current.selected_line_ids != job.selected_line_ids
        ):
            raise PregenerationSetupError("Saved preparation identity changed")
        prepared = replace(
            current,
            status="prepared",
            updated_at=self.clock().astimezone(timezone.utc).isoformat(),
        )
        write_versioned_json(
            self.path_for(job.job_id), job_schema_version, prepared.to_document()
        )
        return prepared

    def path_for(self, job_id):
        if not isinstance(job_id, str) or len(job_id) != 24:
            raise PregenerationSetupError("Preparation identity is invalid")
        try:
            int(job_id, 16)
        except ValueError as error:
            raise PregenerationSetupError("Preparation identity is invalid") from error
        return self.root / job_id / "job.json"


def _story_selections(document):
    if document.collections:
        records_by_collection = {}
        for record in document.records:
            records_by_collection.setdefault(record.collection_id, []).append(record)
        groups = [
            (
                collection.collection_id,
                collection.title,
                collection.kind,
                collection.order,
                tuple(records_by_collection.get(collection.collection_id, ())),
            )
            for collection in document.collections
        ]
    else:
        records_by_chapter = {}
        for record in document.records:
            records_by_chapter.setdefault(record.chapter, []).append(record)
        groups = [
            (
                f"chapter:{chapter}",
                f"Chapter {chapter}",
                "chapter",
                order,
                tuple(records),
            )
            for order, (chapter, records) in enumerate(
                sorted(records_by_chapter.items())
            )
        ]
    return tuple(
        _selection_from_records(selection_id, title, kind, order, records)
        for selection_id, title, kind, order, records in groups
        if records
    )


def _is_outdated_reverse1999_index(path):
    try:
        with path.open(encoding="utf-8") as stream:
            metadata = json.loads(next(stream))
    except OSError, StopIteration, json.JSONDecodeError:
        return False
    return (
        isinstance(metadata, dict)
        and metadata.get("game") == "Reverse: 1999"
        and not metadata.get("collections")
    )


def _selection_from_records(selection_id, title, kind, order, records):
    speakable = tuple(record for record in records if record.speakable)
    original = tuple(
        record for record in speakable if record.source_audio_status == "available"
    )
    generation = tuple(
        record for record in speakable if record.source_audio_status != "available"
    )
    speakers = {
        (record.voice_character or record.speaker).strip()
        for record in generation
        if (record.voice_character or record.speaker).strip()
    }
    selection = StorySelection(
        selection_id=selection_id,
        title=title,
        kind=kind,
        order=order,
        line_ids=tuple(record.line_id for record in records),
        line_count=len(records),
        speakable_lines=len(speakable),
        original_audio_lines=len(original),
        generation_lines=len(generation),
        speaker_count=len(speakers),
        speakers=tuple(sorted(speakers)),
        generation_text_characters=sum(
            len(getattr(record, "text", "")) for record in generation
        ),
    )
    return selection


def _generation_text_characters(content, selected_ids):
    return sum(
        selection.generation_text_characters
        for selection in content.selections
        if selection.selection_id in selected_ids
    )


def _rough_audio_seconds(text_characters):
    return (
        text_characters + ROUGH_SPEECH_CHARACTERS_PER_SECOND - 1
    ) // ROUGH_SPEECH_CHARACTERS_PER_SECOND


def _normalized_selection_ids(content, selected_story_ids):
    requested = tuple(dict.fromkeys(str(value).strip() for value in selected_story_ids))
    if not requested or any(not value for value in requested):
        raise PregenerationSetupError("Select at least one story or chapter")
    declared = {value.selection_id for value in content.selections}
    unknown = tuple(value for value in requested if value not in declared)
    if unknown:
        raise PregenerationSetupError(f"Unknown story selection: {', '.join(unknown)}")
    return tuple(
        value.selection_id
        for value in content.selections
        if value.selection_id in requested
    )


def _required_text(document, name):
    value = document[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("optional text must be null or non-empty")
    return value.strip()


def _sha256_text(document, name):
    value = _required_text(document, name)
    if len(value) != 64:
        raise ValueError(f"{name} must be SHA-256 text")
    int(value, 16)
    return value


def _text_tuple(document, name):
    values = document[name]
    if not isinstance(values, list) or not values:
        raise ValueError(f"{name} must be a non-empty list")
    result = tuple(values)
    if not all(isinstance(value, str) and value.strip() for value in result):
        raise ValueError(f"{name} must contain non-empty text")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _nonnegative_int(document, name):
    value = document[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_nonnegative_int(document, name):
    return 0 if name not in document else _nonnegative_int(document, name)


__all__ = [
    "ContentDiscovery",
    "GameContent",
    "GenerationResourceEstimate",
    "PreparationEstimate",
    "PregenerationJob",
    "PregenerationJobStore",
    "PregenerationSetupError",
    "StorySelection",
    "discover_game_content",
    "estimate_generation_resources",
    "estimate_preparation",
    "inspect_story_index",
]

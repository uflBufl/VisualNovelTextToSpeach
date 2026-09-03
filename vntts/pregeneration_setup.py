"""Player-facing content selection and durable self-service preparation state."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_data_path
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document

from vntts.application_directories import get_local_data_directory
from vntts.settings import AppSettings
from vntts.versioned_json import read_versioned_json, write_versioned_json

job_schema_version = 1


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
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise PregenerationSetupError(f"Story content was not found: {path}")
    try:
        document = load_story_index_document(path)
    except (OSError, StoryIndexError, ValueError) as error:
        raise PregenerationSetupError(f"Story content is invalid: {error}") from error
    selections = _story_selections(document)
    if not selections:
        raise PregenerationSetupError("Story content has no selectable dialogue")
    metadata = document.metadata
    version = metadata.get("game_version")
    return GameContent(
        provider_id=provider_id,
        game=document.game or "Visual novel",
        game_version=version.strip()
        if isinstance(version, str) and version.strip()
        else None,
        story_index=path,
        story_index_sha256=sha256_file(path),
        selections=selections,
    )


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
        try:
            discovered.append(inspect_story_index(path, provider_id=provider_id))
        except PregenerationSetupError as error:
            errors.append(str(error))
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
    estimated_audio_seconds = generation_lines * 6
    return PreparationEstimate(
        selected_lines=sum(value.line_count for value in selected),
        original_audio_lines=sum(value.original_audio_lines for value in selected),
        generation_lines=generation_lines,
        speaker_count=max(
            len(speakers), max((value.speaker_count for value in selected), default=0)
        ),
        estimated_generation_minutes=(generation_lines * 15 + 59) // 60,
        estimated_disk_bytes=estimated_audio_seconds * 24_000 * 2,
    )


class PregenerationJobStore:
    def __init__(self, root=None, *, clock=None):
        self.root = Path(
            root or get_local_data_directory() / "pregeneration" / "jobs"
        ).expanduser()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

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
        if not self.root.is_dir():
            return None
        matches = []
        for path in self.root.glob("*/job.json"):
            try:
                job = self.load(path.parent.name)
            except PregenerationSetupError:
                continue
            if job.story_index_sha256 == content.story_index_sha256:
                matches.append(job)
        return max(matches, key=lambda value: value.updated_at, default=None)

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
        groups = [
            (
                collection.collection_id,
                collection.title,
                collection.kind,
                collection.order,
                tuple(
                    record
                    for record in document.records
                    if record.collection_id == collection.collection_id
                ),
            )
            for collection in document.collections
        ]
    else:
        chapters = sorted({record.chapter for record in document.records})
        groups = [
            (
                f"chapter:{chapter}",
                f"Chapter {chapter}",
                "chapter",
                order,
                tuple(
                    record for record in document.records if record.chapter == chapter
                ),
            )
            for order, chapter in enumerate(chapters)
        ]
    return tuple(
        _selection_from_records(selection_id, title, kind, order, records)
        for selection_id, title, kind, order, records in groups
        if records
    )


def _selection_from_records(selection_id, title, kind, order, records):
    speakable = tuple(record for record in records if record.speakable)
    original = tuple(
        record for record in speakable if record.source_audio_status == "available"
    )
    generation = tuple(record for record in speakable if record not in original)
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
    )
    return selection


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


__all__ = [
    "ContentDiscovery",
    "GameContent",
    "PreparationEstimate",
    "PregenerationJob",
    "PregenerationJobStore",
    "PregenerationSetupError",
    "StorySelection",
    "discover_game_content",
    "estimate_preparation",
    "inspect_story_index",
]

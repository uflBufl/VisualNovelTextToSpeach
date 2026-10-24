"""Player-facing content selection and durable self-service preparation state."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from string import hexdigits
from time import perf_counter, process_time
from typing import TYPE_CHECKING

from platformdirs import user_data_path
from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    StoryIndexRecord,
)
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueueError,
)

from vntts.application_directories import get_local_data_directory
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_state import (
    load_generation_state_from_snapshot,
    load_stable_generation_queue,
)
from vntts.chapter_voice_preload import (
    _has_authoritative_source_audio,
    _source_audio_evidence_payload,
    _validated_source_audio_line_ids,
)
from vntts.document_identity import is_sha256, sha256_file
from vntts.generation_database import state_path as generation_state_path
from vntts.json_types import decode_json
from vntts.path_safety import open_regular_text, read_regular_bytes
from vntts.preparation_catalog import PreparationCatalog, PreparationCatalogError
from vntts.settings import AppSettings
from vntts.story_index_snapshot import load_story_index_snapshot
from vntts.versioned_json import read_versioned_json, write_versioned_json

if TYPE_CHECKING:
    from vntts.pregeneration_queue import PregenerationInput

job_schema_version = 1
story_catalog_schema_version = 8
story_catalog_minimum_bytes = 8 * 1024 * 1024
# ponytail: assumes 12 text chars/sec and PCM16 mono 24 kHz; upgrade with measured
# durations and the selected backend's output format if storage estimates matter.
ROUGH_SPEECH_CHARACTERS_PER_SECOND = 12
PCM16_MONO_24KHZ_BYTES_PER_SECOND = 48_000
PathInput = str | os.PathLike[str]


class PregenerationSetupError(RuntimeError):
    """Player-selected story content cannot be prepared safely."""


class StoryContentChanged(ValueError):
    """The selected story index was replaced after its job was created."""


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
    playback_speakers: tuple[str, ...]
    generation_text_characters: int
    episode_titles: tuple[str, ...] = ()
    character_names: tuple[str, ...] = ()
    parent_story_id: str | None = None
    parent_story_title: str | None = None


@dataclass(frozen=True)
class GameContent:
    provider_id: str
    game: str
    game_version: str | None
    story_index: Path
    story_index_sha256: str
    selections: tuple[StorySelection, ...]
    source_story_index: Path | None = None
    source_story_index_sha256: str | None = None
    game_id: str | None = None

    @property
    def catalog_story_index(self) -> Path:
        return self.source_story_index or self.story_index

    @property
    def catalog_story_index_sha256(self) -> str:
        return self.source_story_index_sha256 or self.story_index_sha256

    @property
    def display_name(self) -> str:
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
    source_story_index: str | None = None
    source_story_index_sha256: str | None = None
    game_id: str | None = None

    @property
    def catalog_story_index(self) -> str:
        return self.source_story_index or self.story_index

    @property
    def catalog_story_index_sha256(self) -> str:
        return self.source_story_index_sha256 or self.story_index_sha256

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> PregenerationJob:
        estimate = document.get("estimate")
        if not isinstance(estimate, Mapping):
            raise PregenerationSetupError("Saved preparation estimate is invalid")
        estimate = _string_mapping(estimate)
        try:
            source_story_index = (
                _required_text(document, "source_story_index")
                if "source_story_index" in document
                else None
            )
            source_story_index_sha256 = (
                _sha256_text(document, "source_story_index_sha256")
                if "source_story_index_sha256" in document
                else None
            )
            if (source_story_index is None) != (source_story_index_sha256 is None):
                raise ValueError("Saved source story identity is incomplete")
            return cls(
                job_id=_required_text(document, "job_id"),
                created_at=_required_text(document, "created_at"),
                updated_at=_required_text(document, "updated_at"),
                status=_required_text(document, "status"),
                provider_id=_required_text(document, "provider_id"),
                game=_required_text(document, "game"),
                game_id=_required_text(document, "game_id")
                if "game_id" in document
                else None,
                game_version=_optional_text(document.get("game_version")),
                story_index=_required_text(document, "story_index"),
                story_index_sha256=_sha256_text(document, "story_index_sha256"),
                selected_story_ids=_text_tuple(document, "selected_story_ids"),
                selected_line_ids=_text_tuple(document, "selected_line_ids"),
                source_story_index=source_story_index,
                source_story_index_sha256=source_story_index_sha256,
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

    def to_document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "job_id": self.job_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "provider_id": self.provider_id,
            "game": self.game,
            "game_version": self.game_version,
            "story_index": self.story_index,
            "story_index_sha256": self.story_index_sha256,
            "selected_story_ids": list(self.selected_story_ids),
            "selected_line_ids": list(self.selected_line_ids),
            "estimate": asdict(self.estimate),
        }
        if self.source_story_index is not None:
            document["source_story_index"] = self.source_story_index
            document["source_story_index_sha256"] = self.source_story_index_sha256
        if self.game_id is not None:
            document["game_id"] = self.game_id
        return document


def _adapter_game_id(game: str | None, provider_id: str | None) -> str | None:
    # Only the installed-game adapter owns this mapping; generic providers do not.
    if provider_id == "reverse1999" and game == "Reverse: 1999":
        return "reverse1999"
    return None


def story_game_id(
    document: StoryIndexDocument, *, provider_id: str | None = None
) -> str | None:
    """Resolve explicit producer metadata or a supported installed-game adapter."""
    value = document.metadata.get("game_id")
    if "game_id" in document.metadata:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise PregenerationSetupError("Story canonical game identity is invalid")
    adapter_id = _adapter_game_id(document.game, provider_id)
    if value is not None and adapter_id is not None and value != adapter_id:
        raise PregenerationSetupError(
            "Story canonical game identity conflicts with its adapter"
        )
    return value if isinstance(value, str) else adapter_id


def _with_game_id(content: GameContent, game_id: str | None) -> GameContent:
    if game_id is None:
        return content
    if (
        not isinstance(game_id, str)
        or not game_id.strip()
        or game_id != game_id.strip()
    ):
        raise PregenerationSetupError("Canonical game identity is invalid")
    if content.game_id is not None and content.game_id != game_id:
        raise PregenerationSetupError(
            "Canonical game identity conflicts with story content"
        )
    return replace(content, game_id=game_id)


def inspect_story_index(
    path: PathInput,
    *,
    provider_id: str = "local-story-index",
    source_audio_evidence_payload: bytes | None = None,
    game_id: str | None = None,
) -> GameContent:
    started = perf_counter()
    cpu_started = process_time()
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise PregenerationSetupError(f"Story content was not found: {path}")
    try:
        size = path.stat().st_size
        checksum = sha256_file(path)
        if source_audio_evidence_payload is None:
            source_audio_evidence_payload = _source_audio_evidence_payload(path)
        evidence_sha256 = hashlib.sha256(source_audio_evidence_payload).hexdigest()
        selected_snapshot = _has_selection_origin(path)
        persistent_cache = size >= story_catalog_minimum_bytes and not selected_snapshot
        content = (
            _load_story_catalog(path, checksum, provider_id, evidence_sha256)
            if persistent_cache
            else None
        )
        cache_state = "disk" if content is not None else "miss"
        if content is None:
            cache_hits = _cached_story_index_document.cache_info().hits
            document = (
                load_verified_story_index_document(path, checksum)
                if selected_snapshot
                else _cached_story_index_document(str(path), checksum)
            )
            selections = _story_selections(
                document,
                _validated_source_audio_line_ids(
                    path, document, evidence_payload=source_audio_evidence_payload
                ),
            )
            if not selections:
                raise PregenerationSetupError(
                    "Story content has no selectable dialogue"
                )
            metadata = document.metadata
            from vntts.selected_story_snapshot import selected_story_origin

            origin = selected_story_origin(document)
            version = metadata.get("game_version")
            content = GameContent(
                provider_id=provider_id,
                game=document.game or "Visual novel",
                game_id=story_game_id(document),
                game_version=version.strip()
                if isinstance(version, str) and version.strip()
                else None,
                story_index=path,
                story_index_sha256=checksum,
                selections=selections,
                source_story_index=origin[0] if origin else None,
                source_story_index_sha256=origin[1] if origin else None,
            )
            cache_state = (
                "memory"
                if _cached_story_index_document.cache_info().hits > cache_hits
                else "miss"
            )
            if persistent_cache:
                _save_story_catalog(content, evidence_sha256)
    except (OSError, StoryIndexError, ValueError) as error:
        raise PregenerationSetupError(f"Story content is invalid: {error}") from error
    from vntts.support import record_background_operation

    details: dict[str, object] = {
        "cpu_ms": round((process_time() - cpu_started) * 1000, 3),
        "files_examined": 1,
        "cache_state": cache_state,
    }
    details["bytes_examined"] = size
    record_background_operation(
        "story-index-inspection",
        (perf_counter() - started) * 1000,
        "complete",
        **details,
    )
    content = _with_game_id(content, _adapter_game_id(content.game, provider_id))
    return _with_game_id(content, game_id)


def _story_catalog_path(checksum: str) -> Path:
    return (
        Path(get_local_data_directory())
        / "pregeneration"
        / "story-catalogs"
        / f"{checksum}.json"
    )


def _load_story_catalog(
    path: Path, checksum: str, provider_id: str, evidence_sha256: str
) -> GameContent | None:
    catalog = _story_catalog_path(checksum)
    if not catalog.is_file():
        return None
    try:
        document = read_versioned_json(
            catalog,
            schema_version=story_catalog_schema_version,
            document_name="story catalog cache",
        )
        if (
            set(document)
            != {
                "schema_version",
                "source_story_index_sha256",
                "source_audio_semantic_evidence_sha256",
                "game",
                "game_version",
                "game_id",
                "selections",
            }
            or document["source_story_index_sha256"] != checksum
            or document["source_audio_semantic_evidence_sha256"] != evidence_sha256
        ):
            raise ValueError("story catalog cache identity changed")
        raw_selections = document["selections"]
        if not isinstance(raw_selections, list) or not raw_selections:
            raise ValueError("story catalog cache selections are invalid")
        selections = tuple(_cached_story_selection(value) for value in raw_selections)
        if len({value.selection_id for value in selections}) != len(selections):
            raise ValueError("story catalog cache selection identities overlap")
        return GameContent(
            provider_id=provider_id,
            game=_required_text(document, "game"),
            game_id=_required_text(document, "game_id")
            if document["game_id"] is not None
            else None,
            game_version=_optional_text(document.get("game_version")),
            story_index=path,
            story_index_sha256=checksum,
            selections=selections,
        )
    except OSError, KeyError, TypeError, ValueError:
        return None


def _save_story_catalog(content: GameContent, evidence_sha256: str) -> None:
    try:
        catalog = _story_catalog_path(content.story_index_sha256)
        catalog.parent.mkdir(parents=True, exist_ok=True)
        write_versioned_json(
            catalog,
            story_catalog_schema_version,
            {
                "source_story_index_sha256": content.story_index_sha256,
                "source_audio_semantic_evidence_sha256": evidence_sha256,
                "game": content.game,
                "game_id": content.game_id,
                "game_version": content.game_version,
                "selections": [asdict(value) for value in content.selections],
            },
        )
    except OSError:
        pass


def _cached_story_selection(document: object) -> StorySelection:
    fields = {
        "selection_id",
        "title",
        "kind",
        "order",
        "line_ids",
        "line_count",
        "speakable_lines",
        "original_audio_lines",
        "generation_lines",
        "speaker_count",
        "speakers",
        "playback_speakers",
        "generation_text_characters",
        "episode_titles",
        "character_names",
        "parent_story_id",
        "parent_story_title",
    }
    if not isinstance(document, Mapping):
        raise ValueError("story catalog cache selection is malformed")
    document = _string_mapping(document)
    if set(document) != fields:
        raise ValueError("story catalog cache selection is malformed")
    line_ids = _text_tuple(document, "line_ids")
    speakers = _text_tuple(document, "speakers", allow_empty=True)
    selection = StorySelection(
        selection_id=_required_text(document, "selection_id"),
        title=_required_text(document, "title"),
        kind=_required_text(document, "kind"),
        order=_nonnegative_int(document, "order"),
        line_ids=line_ids,
        line_count=_nonnegative_int(document, "line_count"),
        speakable_lines=_nonnegative_int(document, "speakable_lines"),
        original_audio_lines=_nonnegative_int(document, "original_audio_lines"),
        generation_lines=_nonnegative_int(document, "generation_lines"),
        speaker_count=_nonnegative_int(document, "speaker_count"),
        speakers=speakers,
        playback_speakers=_text_tuple(document, "playback_speakers", allow_empty=True),
        generation_text_characters=_nonnegative_int(
            document, "generation_text_characters"
        ),
        episode_titles=_text_tuple(document, "episode_titles", allow_empty=True),
        character_names=_text_tuple(document, "character_names", allow_empty=True),
        parent_story_id=_optional_text(document.get("parent_story_id")),
        parent_story_title=_optional_text(document.get("parent_story_title")),
    )
    if (
        selection.line_count != len(line_ids)
        or selection.speakable_lines
        != selection.original_audio_lines + selection.generation_lines
        or selection.speakable_lines > selection.line_count
        or selection.speaker_count != len(speakers)
        or (selection.parent_story_id is None) != (selection.parent_story_title is None)
    ):
        raise ValueError("story catalog cache selection counts changed")
    return selection


@lru_cache(maxsize=8)
def _cached_story_index_document(path: str, expected_sha256: str) -> StoryIndexDocument:
    payload = read_regular_bytes(Path(path))
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise StoryContentChanged("Story content changed while it was being read")
    document = load_story_index_snapshot(path, payload)
    if sha256_file(path) != expected_sha256:
        raise StoryContentChanged("Story content changed while it was being read")
    return document


def load_verified_story_index_document(
    path: PathInput, expected_sha256: str
) -> StoryIndexDocument:
    """Reuse an immutable parse while still checking the current file bytes."""
    path = Path(path).expanduser().resolve()
    if sha256_file(path) != expected_sha256:
        raise StoryContentChanged("Story content checksum changed")
    if _has_selection_origin(path):
        payload = read_regular_bytes(path)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise StoryContentChanged("Story content changed while it was being read")
        document = load_story_index_snapshot(path, payload)
        if sha256_file(path) != expected_sha256:
            raise StoryContentChanged("Story content changed while it was being read")
        return document
    return _cached_story_index_document(str(path), expected_sha256)


def _has_selection_origin(path: Path) -> bool:
    with open_regular_text(path, encoding="utf-8") as stream:
        metadata = decode_json(stream.readline())
    return isinstance(metadata, dict) and "selection_origin" in metadata


@lru_cache(maxsize=8)
def _cached_story_index(
    path: str, provider_id: str, expected_sha256: str, evidence_sha256: str
) -> GameContent:
    evidence_payload = _source_audio_evidence_payload(path)
    if hashlib.sha256(evidence_payload).hexdigest() != evidence_sha256:
        raise PregenerationSetupError(
            "Story audio evidence changed while it was being inspected. Refresh to retry."
        )
    content = inspect_story_index(
        path,
        provider_id=provider_id,
        source_audio_evidence_payload=evidence_payload,
    )
    if content.story_index_sha256 != expected_sha256:
        raise PregenerationSetupError(
            "Story content changed while it was being inspected. Refresh to retry."
        )
    return content


def discover_game_content(
    settings: AppSettings | object,
    *,
    environment: Mapping[str, str] | None = None,
    extra_paths: Iterable[PathInput] = (),
) -> ContentDiscovery:
    """Discover bounded, known story-index locations without scanning user files."""
    environment = os.environ if environment is None else environment
    candidates: list[tuple[PathInput, str]] = []
    if isinstance(settings, AppSettings) and settings.story_index:
        candidates.append((settings.story_index, "configured-story-index"))
    candidates.extend((path, "selected-story-index") for path in extra_paths)
    configured_extractor_root = environment.get("R1999_EXTRACTOR_DATA")
    extractor_root: Path = (
        Path(configured_extractor_root).expanduser()
        if configured_extractor_root
        else Path(user_data_path("Reverse1999Extractor", appauthor=False))
    )
    installed_story = (
        get_local_data_directory()
        / "game-content"
        / "reverse1999"
        / "reverse1999"
        / "story-index.jsonl"
    )
    extractor_story = extractor_root / "reverse1999" / "story-index.jsonl"
    trusted_game_paths = {installed_story.resolve(), extractor_story.resolve()}
    candidates.append(
        (
            installed_story,
            "reverse1999",
        )
    )
    candidates.append((extractor_story, "reverse1999"))

    discovered: list[GameContent] = []
    errors: list[str] = []
    seen: set[Path] = set()
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
            provider_id = "reverse1999" if path in trusted_game_paths else provider_id
            if _has_selection_origin(path):
                content = inspect_story_index(path, provider_id=provider_id)
            else:
                content = _cached_story_index(
                    str(path),
                    provider_id,
                    sha256_file(path),
                    hashlib.sha256(_source_audio_evidence_payload(path)).hexdigest(),
                )
            if (
                isinstance(settings, AppSettings)
                and settings.game_pack
                and settings.story_index
                and path == Path(settings.story_index).expanduser().resolve()
            ):
                from vntts.game_pack import GamePackError, import_game_pack

                try:
                    imported = import_game_pack(settings.game_pack)
                except (GamePackError, OSError, ValueError) as error:
                    raise PregenerationSetupError(
                        f"Configured game pack is invalid: {error}"
                    ) from error
                if (
                    imported.story_index == path
                    and imported.pack.story_index.sha256 == content.story_index_sha256
                ):
                    content = _with_game_id(content, imported.pack.game_id)
            discovered.append(content)
        except (OSError, PregenerationSetupError) as error:
            errors.append(str(error))
    discovered.sort(key=lambda content: len(content.selections), reverse=True)
    return ContentDiscovery(tuple(discovered), tuple(errors))


def estimate_preparation(
    content: GameContent, selected_story_ids: Iterable[object]
) -> PreparationEstimate:
    selected_ids = _normalized_selection_ids(content, selected_story_ids)
    selected_id_set = set(selected_ids)
    selected = tuple(
        value for value in content.selections if value.selection_id in selected_id_set
    )
    generation_lines = sum(value.generation_lines for value in selected)
    speakers: set[str] = set()
    for value in selected:
        if value.generation_lines:
            speakers.update(value.speakers)
    text_characters = sum(value.generation_text_characters for value in selected)
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


def estimate_generation_resources(
    generation_input: PregenerationInput,
) -> GenerationResourceEstimate:
    """Estimate remaining PCM WAV storage from the exact private queue."""
    try:
        queue, queue_sha256 = load_stable_generation_queue(generation_input.queue)
        if queue_sha256 != generation_input.queue_sha256:
            raise PregenerationSetupError(
                "Generation queue changed before estimating storage"
            )
        state_path = generation_state_path(generation_input.output_directory)
        state = (
            load_generation_state_from_snapshot(state_path, queue, queue_sha256)
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
    state_items = _generation_state_items(state)
    items = tuple(
        item
        for item in queue.items
        if item.action == "generate" and item.queue_id not in omissions
    )
    remaining = tuple(
        item
        for item in items
        if state_items.get(item.queue_id, {}).get("status") not in terminal
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
    def __init__(
        self,
        root: PathInput | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(
            root or get_local_data_directory() / "pregeneration" / "jobs"
        ).expanduser()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.catalog = PreparationCatalog(self.root)
        self._legacy_jobs_imported = False
        self._legacy_generation_inputs: tuple[Path, ...] | None = None

    def _catalog_call[**P, T](
        self, operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T:
        try:
            return operation(*args, **kwargs)
        except PreparationCatalogError as error:
            raise PregenerationSetupError(str(error)) from error

    def _selection_path(self, content: GameContent) -> Path:
        checksum = _sha256_text(
            {"checksum": content.catalog_story_index_sha256}, "checksum"
        )
        return self.root / "selections" / f"{checksum}.json"

    def selection_for_content(self, content: GameContent) -> tuple[str, ...] | None:
        saved = self._catalog_call(
            self.catalog.selection, content.catalog_story_index_sha256
        )
        if saved is not None:
            return _normalized_selection_ids(content, saved) if saved else ()
        path = self._selection_path(content)
        try:
            document = read_versioned_json(
                path, schema_version=1, document_name="story selection"
            )
        except FileNotFoundError:
            return None
        try:
            if document["story_index_sha256"] != content.catalog_story_index_sha256:
                raise ValueError("Story selection belongs to different content")
            values = document["selected_story_ids"]
            selected = (
                ()
                if values == []
                else _normalized_selection_ids(
                    content, _text_tuple(document, "selected_story_ids")
                )
            )
            self._catalog_call(
                self.catalog.save_selection,
                content.catalog_story_index_sha256,
                selected,
                replace=False,
            )
            return self._catalog_call(
                self.catalog.selection, content.catalog_story_index_sha256
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PregenerationSetupError(
                f"Saved story selection is invalid: {error}"
            ) from error

    def save_selection(
        self, content: GameContent, selected_story_ids: Iterable[object]
    ) -> None:
        requested = tuple(selected_story_ids)
        selected = _normalized_selection_ids(content, requested) if requested else ()
        self._catalog_call(
            self.catalog.save_selection, content.catalog_story_index_sha256, selected
        )

    def create_or_resume(
        self, content: GameContent, selected_story_ids: Iterable[object]
    ) -> PregenerationJob:
        selected_ids = _normalized_selection_ids(content, selected_story_ids)
        selected_id_set = set(selected_ids)
        selected = tuple(
            value
            for value in content.selections
            if value.selection_id in selected_id_set
        )
        line_ids = tuple(line_id for value in selected for line_id in value.line_ids)
        identity = hashlib.sha256()
        identity.update(content.story_index_sha256.encode("ascii"))
        for selection_id in selected_ids:
            identity.update(b"\0")
            identity.update(selection_id.encode("utf-8"))
        job_id = identity.hexdigest()[:24]
        document = self._load_job_document(job_id)
        if document is not None:
            job = PregenerationJob.from_document(document)
            if (
                job.story_index_sha256 != content.story_index_sha256
                or job.selected_story_ids != selected_ids
                or job.selected_line_ids != line_ids
                or job.catalog_story_index_sha256 != content.catalog_story_index_sha256
                or Path(job.catalog_story_index).expanduser().resolve()
                != content.catalog_story_index.expanduser().resolve()
            ):
                raise PregenerationSetupError(
                    "Saved preparation identity conflicts with the selected story"
                )
            if (
                job.game_id is not None
                and content.game_id is not None
                and job.game_id != content.game_id
            ):
                raise PregenerationSetupError(
                    "Saved canonical game identity conflicts with the selected story"
                )
            if (
                job.provider_id != content.provider_id
                and content.provider_id == "reverse1999"
            ) or (job.game_id is None and content.game_id is not None):
                job = replace(
                    job,
                    provider_id=content.provider_id
                    if content.provider_id == "reverse1999"
                    else job.provider_id,
                    game_id=content.game_id or job.game_id,
                )
                self._catalog_call(self.catalog.save_job, job.to_document())
            return job
        timestamp = self.clock().astimezone(timezone.utc).isoformat()
        job = PregenerationJob(
            job_id=job_id,
            created_at=timestamp,
            updated_at=timestamp,
            status="planned",
            provider_id=content.provider_id,
            game=content.game,
            game_id=content.game_id,
            game_version=content.game_version,
            story_index=str(content.story_index),
            story_index_sha256=content.story_index_sha256,
            selected_story_ids=selected_ids,
            selected_line_ids=line_ids,
            estimate=estimate_preparation(content, selected_ids),
            source_story_index=str(content.source_story_index)
            if content.source_story_index is not None
            else None,
            source_story_index_sha256=content.source_story_index_sha256,
        )
        self._catalog_call(self.catalog.save_job, job.to_document(), replace=False)
        return self.load(job_id)

    def selected_content(self, job: PregenerationJob) -> GameContent:
        """Prepare strict, source-bound dialogue inside a background worker."""
        from vntts.selected_story_snapshot import create_selected_story_snapshot

        snapshot_identity = hashlib.sha256(
            json.dumps(
                [job.story_index_sha256, job.selected_story_ids, job.selected_line_ids],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        try:
            snapshot = create_selected_story_snapshot(
                Path(job.story_index),
                job.story_index_sha256,
                job.selected_line_ids,
                job.selected_story_ids,
                self.root / "story-snapshots" / snapshot_identity,
            )
            content = inspect_story_index(
                snapshot, provider_id=job.provider_id, game_id=job.game_id
            )
        except (OSError, StoryIndexError, ValueError) as error:
            raise PregenerationSetupError(
                f"Unable to prepare selected story snapshot: {error}"
            ) from error
        if set(job.selected_line_ids) != {
            line_id
            for selection in content.selections
            for line_id in selection.line_ids
        }:
            raise PregenerationSetupError("Selected story snapshot changed its lines")
        return content

    def _load_job_document(self, job_id: str) -> dict[str, object] | None:
        path = self.path_for(job_id)
        document = self._catalog_call(self.catalog.load_job, job_id)
        if document is not None:
            return document
        if not path.exists():
            return None
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
        self._catalog_call(self.catalog.save_job, job.to_document(), replace=False)
        return self._catalog_call(self.catalog.load_job, job_id)

    def load(self, job_id: str) -> PregenerationJob:
        document = self._load_job_document(job_id)
        if document is None:
            raise PregenerationSetupError("Offline audio preparation was not found")
        job = PregenerationJob.from_document(document)
        if job.job_id != job_id:
            raise PregenerationSetupError("Saved preparation identity changed")
        return job

    def latest_for_content(self, content: GameContent) -> PregenerationJob | None:
        return max(
            self.jobs_for_content(content),
            key=lambda value: value.updated_at,
            default=None,
        )

    def _import_legacy_jobs(self) -> None:
        if self._legacy_jobs_imported:
            return
        if self.root.is_dir():
            for path in self.root.glob("*/job.json"):
                try:
                    self.load(path.parent.name)
                except PregenerationSetupError:
                    continue
        self._legacy_jobs_imported = True

    def _saved_jobs(self) -> Iterator[PregenerationJob]:
        self._import_legacy_jobs()
        for document in self._catalog_call(self.catalog.jobs):
            yield PregenerationJob.from_document(document)

    def jobs_for_content(self, content: GameContent) -> tuple[PregenerationJob, ...]:
        self._import_legacy_jobs()
        return tuple(
            PregenerationJob.from_document(document)
            for document in self._catalog_call(
                self.catalog.jobs, content.catalog_story_index_sha256
            )
        )

    def generation_input_directories(
        self, content_hash: str | None = None
    ) -> tuple[Path, ...]:
        """Query current inputs while discovering unindexed legacy assets once."""
        self._import_legacy_jobs()
        indexed = self._catalog_call(self.catalog.input_directories, content_hash)
        root = self.root.resolve()
        if self._legacy_generation_inputs is None:
            self._legacy_generation_inputs = tuple(
                sorted(root.glob("*/generation-input-*"))
            )
        legacy = self._legacy_generation_inputs
        if content_hash is not None:
            jobs = {
                document["job_id"]
                for document in self._catalog_call(self.catalog.jobs, content_hash)
            }
            legacy = tuple(path for path in legacy if path.parent.name in jobs)
        directories = tuple(sorted(set(indexed) | set(legacy)))
        if any(
            path.parent.parent != root
            or path.is_symlink()
            or path.is_junction()
            or path.parent.is_symlink()
            or path.parent.is_junction()
            for path in directories
        ):
            raise PregenerationSetupError(
                "Saved generation input leaves its preparation directory"
            )
        return directories

    def source_story_indexes(self) -> tuple[Path, ...]:
        sources = (
            Path(job.catalog_story_index).expanduser() for job in self._saved_jobs()
        )
        return tuple(dict.fromkeys(source for source in sources if source.is_file()))

    def prepared_story_ids(self, content: GameContent) -> frozenset[str]:
        return frozenset(
            selection_id
            for selection_id, status in self.story_statuses(content).items()
            if status == "ready"
        )

    def story_statuses(self, content: GameContent) -> dict[str, str]:
        statuses: dict[str, str] = {}
        for job in self.jobs_for_content(content):
            prepared = job.status == "prepared" or self._has_published_pack(job)
            for selection_id in job.selected_story_ids:
                if prepared:
                    statuses[selection_id] = "ready"
                elif statuses.get(selection_id) != "ready":
                    statuses[selection_id] = "in_progress"
        return statuses

    def _has_published_pack(self, job: PregenerationJob) -> bool:
        return bool(self.published_packs(job))

    def published_packs(self, job: PregenerationJob) -> tuple[Path, ...]:
        job_directory = self.directory_for(job.job_id)
        root = job_directory / "game-packs"
        if not root.is_dir() or any(
            path.is_symlink() or path.is_junction() for path in (job_directory, root)
        ):
            return ()
        manifests: list[Path] = []
        for manifest in root.glob("pack-*/game-pack.json"):
            identity = manifest.parent.name.removeprefix("pack-")
            if (
                len(identity) == 24
                and manifest.is_file()
                and not any(
                    path.is_symlink() or path.is_junction()
                    for path in (manifest.parent, manifest)
                )
            ):
                if not all(character in hexdigits for character in identity):
                    continue
                manifests.append(manifest)
        return tuple(manifests)

    def mark_prepared(self, job: PregenerationJob) -> PregenerationJob:
        if not isinstance(job, PregenerationJob):
            raise PregenerationSetupError("Preparation job is invalid")
        current = self.load(job.job_id)
        if (
            current.story_index_sha256 != job.story_index_sha256
            or current.selected_story_ids != job.selected_story_ids
            or current.selected_line_ids != job.selected_line_ids
            or current.catalog_story_index_sha256 != job.catalog_story_index_sha256
            or current.catalog_story_index != job.catalog_story_index
        ):
            raise PregenerationSetupError("Saved preparation identity changed")
        prepared = replace(
            current,
            status="prepared",
            updated_at=self.clock().astimezone(timezone.utc).isoformat(),
        )
        self._catalog_call(self.catalog.save_job, prepared.to_document())
        return prepared

    def path_for(self, job_id: str) -> Path:
        """Legacy import location; use directory_for for owned job assets."""
        return self.directory_for(job_id) / "job.json"

    def directory_for(self, job_id: str) -> Path:
        if (
            not isinstance(job_id, str)
            or len(job_id) != 24
            or not all(character in hexdigits for character in job_id)
        ):
            raise PregenerationSetupError("Preparation identity is invalid")
        return self.root / job_id


def _story_selections(
    document: StoryIndexDocument,
    authoritative_source_lines: Iterable[str] = frozenset(),
) -> tuple[StorySelection, ...]:
    metadata = getattr(document, "metadata", {})
    raw_completion_contract = metadata.get("source_audio_completion")
    completion_contract = (
        raw_completion_contract if isinstance(raw_completion_contract, str) else None
    )
    if document.collections:
        origin = metadata.get("selection_origin")
        origin_selection_ids = (
            frozenset(origin["selected_story_ids"])
            if isinstance(origin, dict)
            else frozenset()
        )
        records_by_collection: dict[str | None, list[StoryIndexRecord]] = {}
        for record in document.records:
            records_by_collection.setdefault(record.collection_id, []).append(record)
        groups = [
            group
            for collection in document.collections
            for group in _collection_stage_groups(
                collection.collection_id,
                collection.title,
                collection.kind,
                collection.order,
                tuple(records_by_collection.get(collection.collection_id, ())),
                force_stages=any(
                    f"{collection.collection_id}:stage:{record.chapter}"
                    in origin_selection_ids
                    for record in records_by_collection.get(
                        collection.collection_id, ()
                    )
                )
                if origin_selection_ids
                else False,
            )
        ]
    else:
        records_by_chapter: dict[str, list[StoryIndexRecord]] = {}
        for record in document.records:
            records_by_chapter.setdefault(record.chapter, []).append(record)
        groups = [
            (
                f"chapter:{chapter}",
                f"Chapter {chapter}",
                "chapter",
                order,
                tuple(records),
                None,
                None,
            )
            for order, (chapter, records) in enumerate(
                sorted(records_by_chapter.items())
            )
        ]
    return tuple(
        _selection_from_records(
            selection_id,
            title,
            kind,
            order,
            records,
            parent_story_id=parent_story_id,
            parent_story_title=parent_story_title,
            completion_contract=completion_contract,
            authoritative_source_lines=authoritative_source_lines,
        )
        for selection_id, title, kind, order, records, parent_story_id, parent_story_title in groups
        if records
    )


def _collection_stage_groups(
    collection_id: str,
    title: str,
    kind: str,
    order: int,
    records: tuple[StoryIndexRecord, ...],
    *,
    force_stages: bool = False,
) -> tuple[
    tuple[str, str, str, int, tuple[StoryIndexRecord, ...], str | None, str | None],
    ...,
]:
    """Split a collection only when its source story-step IDs prove stages."""
    by_chapter: dict[str, list[StoryIndexRecord]] = {}
    stage_order: list[str] = []
    for record in records:
        chapter = getattr(record, "chapter", None)
        if not isinstance(chapter, str) or not chapter.strip():
            return ((collection_id, title, kind, order, records, None, None),)
        by_chapter.setdefault(chapter, []).append(record)
        if not stage_order or stage_order[-1] != chapter:
            stage_order.append(chapter)
    if len(by_chapter) < 2 and not force_stages:
        return ((collection_id, title, kind, order, records, None, None),)
    if len(stage_order) != len(by_chapter):
        return ((collection_id, title, kind, order, records, None, None),)
    stage_label = "Episode" if kind in {"main_story", "main-story"} else "Stage"
    return tuple(
        (
            f"{collection_id}:stage:{chapter}",
            f"{title} - {stage_label} {position}",
            kind,
            order,
            tuple(stage_records),
            collection_id,
            title,
        )
        for position, chapter in enumerate(stage_order, start=1)
        for stage_records in (by_chapter[chapter],)
    )


def _is_outdated_reverse1999_index(path: Path) -> bool:
    try:
        with open_regular_text(path, encoding="utf-8") as stream:
            metadata = decode_json(next(stream))
    except OSError, UnicodeError, StopIteration, json.JSONDecodeError:
        return False
    return (
        isinstance(metadata, dict)
        and metadata.get("game") == "Reverse: 1999"
        and not metadata.get("collections")
    )


def _selection_from_records(
    selection_id: str,
    title: str,
    kind: str,
    order: int,
    records: Sequence[StoryIndexRecord],
    *,
    parent_story_id: str | None = None,
    parent_story_title: str | None = None,
    completion_contract: str | None = None,
    authoritative_source_lines: Iterable[str] = frozenset(),
) -> StorySelection:
    speakable = tuple(record for record in records if record.speakable)
    original = tuple(
        record
        for record in speakable
        if _has_authoritative_source_audio(
            record, completion_contract, authoritative_source_lines
        )
    )
    original_ids = {record.line_id for record in original}
    generation = tuple(
        record for record in speakable if record.line_id not in original_ids
    )
    speakers = {
        (record.voice_character or record.speaker).strip()
        for record in generation
        if (record.voice_character or record.speaker).strip()
    }
    playback_speakers = {
        (record.voice_character or record.speaker).strip()
        for record in speakable
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
        playback_speakers=tuple(sorted(playback_speakers)),
        generation_text_characters=sum(
            len(getattr(record, "text", "")) for record in generation
        ),
        episode_titles=tuple(
            dict.fromkeys(
                episode_title.strip()
                for record in records
                if isinstance(
                    episode_title := getattr(record, "document", {}).get(
                        "episode_title"
                    ),
                    str,
                )
                and episode_title.strip()
            )
        ),
        character_names=tuple(
            dict.fromkeys(
                record.speaker.strip() for record in speakable if record.speaker.strip()
            )
        ),
        parent_story_id=parent_story_id,
        parent_story_title=parent_story_title,
    )
    return selection


def _rough_audio_seconds(text_characters: int) -> int:
    return (
        text_characters + ROUGH_SPEECH_CHARACTERS_PER_SECOND - 1
    ) // ROUGH_SPEECH_CHARACTERS_PER_SECOND


def _normalized_selection_ids(
    content: GameContent, selected_story_ids: Iterable[object]
) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(str(value).strip() for value in selected_story_ids))
    if not requested or any(not value for value in requested):
        raise PregenerationSetupError("Select at least one story, stage, or chapter")
    declared = {value.selection_id for value in content.selections}
    unknown = tuple(value for value in requested if value not in declared)
    if unknown:
        raise PregenerationSetupError(f"Unknown story selection: {', '.join(unknown)}")
    requested_ids = set(requested)
    return tuple(
        value.selection_id
        for value in content.selections
        if value.selection_id in requested_ids
    )


def _required_text(document: Mapping[str, object], name: str) -> str:
    value = document[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("optional text must be null or non-empty")
    return value.strip()


def _sha256_text(document: Mapping[str, object], name: str) -> str:
    value = _required_text(document, name)
    if not is_sha256(value):
        raise ValueError(f"{name} must be SHA-256 text")
    return value


def _text_tuple(
    document: Mapping[str, object], name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    values = document[name]
    if not isinstance(values, list) or not values and not allow_empty:
        raise ValueError(
            f"{name} must be a{' non-empty' if not allow_empty else ''} list"
        )
    result = tuple(value for value in values if isinstance(value, str))
    if len(result) != len(values) or not all(value.strip() for value in result):
        raise ValueError(f"{name} must contain non-empty text")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _nonnegative_int(document: Mapping[str, object], name: str) -> int:
    value = document[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_nonnegative_int(document: Mapping[str, object], name: str) -> int:
    return 0 if name not in document else _nonnegative_int(document, name)


def _string_mapping(value: Mapping[object, object]) -> Mapping[str, object]:
    if not all(isinstance(key, str) for key in value):
        raise ValueError("document keys must be text")
    return {str(key): item for key, item in value.items()}


def _generation_state_items(
    state: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    raw_items = state.get("items")
    if not isinstance(raw_items, Mapping):
        raise PregenerationSetupError("Generation state items are invalid")
    result: dict[str, Mapping[str, object]] = {}
    for queue_id, item in raw_items.items():
        if not isinstance(queue_id, str) or not isinstance(item, Mapping):
            raise PregenerationSetupError("Generation state items are invalid")
        result[queue_id] = _string_mapping(item)
    return result


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

"""Imported dialogue and prepared routes without auditing archived audio files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cached_property, partial
from pathlib import Path

from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    GeneratedAudioManifestError,
)
from vntts_artifacts.story_index import StoryIndexError

from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.current_voice import CurrentVoiceSnapshot, RecordedReferenceSources
from vntts.document_identity import is_lowercase_sha256
from vntts.generated_audio import (
    GeneratedAudioEntryLike,
    GeneratedAudioLibrary,
    GeneratedAudioReference,
    PreparedGeneratedAudio,
)
from vntts.generation_database import require_registered_output
from vntts.generation_database import state_path as generation_state_path
from vntts.path_safety import (
    contained_path,
    open_regular_text,
    read_regular_bytes,
    safe_relative_path,
)
from vntts.pregeneration_setup import (
    PregenerationJobStore,
    PregenerationSetupError,
    open_reading_catalog,
)
from vntts.preparation_catalog import PreparationCatalogError
from vntts.prepared_dialogue_store import PreparedDialogueStore, canonical_identity
from vntts.reading_catalog import ReadingCatalog
from vntts.settings import AppSettings
from vntts.voices import synthesis_character_for_line


@dataclass(frozen=True)
class PreparedAudioSource:
    game_id: str
    content_sha256: str
    story_index: Path
    preloader: ChapterVoicePreloader
    library: GeneratedAudioLibrary
    game_name: str = ""
    pack_manifest: Path | None = None
    catalog_story_index: Path | None = None
    voice_manifest: Path | None = None
    voice_manifest_sha256: str | None = None
    voice_roles: Mapping[str, str] = field(default_factory=dict)
    story_sha256: str | None = None
    reference_sources: RecordedReferenceSources | None = None

    @property
    def scope(self) -> tuple[str, str]:
        return self.game_id, self.content_sha256


type _PathSignature = tuple[int, int, int, frozenset[str] | None] | None


@dataclass(frozen=True)
class PreparedAudioSnapshot:
    sources: tuple[PreparedAudioSource, ...] = ()
    watched_paths: tuple[tuple[Path, _PathSignature], ...] = ()
    catalogs: tuple[ReadingCatalog, ...] = ()

    def changed(self) -> bool:
        return any(
            _signature(path) != signature for path, signature in self.watched_paths
        )

    def scoped(
        self, *, game_id: str | None = None, content_sha256: str | None = None
    ) -> PreparedAudioSnapshot:
        return PreparedAudioSnapshot(
            tuple(
                source
                for source in self.sources
                if (game_id is None or source.game_id == game_id)
                and (content_sha256 is None or source.content_sha256 == content_sha256)
            ),
            self.watched_paths,
            tuple(
                catalog
                for catalog in self.catalogs
                if game_id is None or catalog.game_id == game_id
            ),
        )

    def for_settings(self, settings: AppSettings) -> PreparedAudioSnapshot:
        """Bind the current capture to one known game without guessing from OCR."""
        for value, attribute in (
            (settings.game_pack, "pack_manifest"),
            (settings.story_index, "story_index"),
        ):
            if not value:
                continue
            path = Path(value).expanduser().resolve()
            matching = tuple(
                source
                for source in self.sources
                if (
                    (
                        getattr(source, attribute) is not None
                        and getattr(source, attribute).resolve() == path
                    )
                    or (
                        attribute == "story_index"
                        and source.catalog_story_index is not None
                        and source.catalog_story_index.resolve() == path
                    )
                )
            )
            games = {source.game_id for source in matching}
            if attribute == "story_index":
                games.update(
                    catalog.game_id
                    for catalog in self.catalogs
                    if catalog.story_index.resolve() == path
                )
            if len(games) == 1:
                return self.scoped(game_id=next(iter(games)))
            return PreparedAudioSnapshot((), self.watched_paths)
        title = _name_key(settings.game_window_title or "")
        games = {
            source.game_id
            for source in self.sources
            if source.game_name
            and _name_key(source.game_name)
            and f" {_name_key(source.game_name)} " in f" {title} "
        }
        games.update(
            catalog.game_id
            for catalog in self.catalogs
            if _name_key(catalog.game_name)
            and f" {_name_key(catalog.game_name)} " in f" {title} "
        )
        if len(games) == 1:
            return self.scoped(game_id=next(iter(games)))
        return PreparedAudioSnapshot((), self.watched_paths)

    @cached_property
    def preloader(self) -> ChapterVoicePreloader:
        return ChapterVoicePreloader.from_store(
            PreparedDialogueStore(
                tuple(
                    (source.game_id, source.preloader.store) for source in self.sources
                ),
                tuple(catalog.store for catalog in self.catalogs),
            )
        )

    def library_for(
        self,
        entry_filter: CurrentVoiceSnapshot | Callable[[GeneratedAudioEntryLike], bool],
        *,
        game_id: str | None = None,
        content_sha256: str | None = None,
    ) -> DiscoveredAudioLibrary:
        scoped = self.scoped(game_id=game_id, content_sha256=content_sha256)
        return DiscoveredAudioLibrary(
            scoped.sources, entry_filter, catalogs=scoped.catalogs
        )


class DiscoveredAudioLibrary(GeneratedAudioLibrary):
    """Select compatible archive entries, retaining their own lazy file readers."""

    # A missing archive entry must fall through to live speech, never wait for an
    # unrelated historical generation job.
    runtime_progress = False

    def __init__(
        self,
        sources: tuple[PreparedAudioSource, ...],
        entry_filter: CurrentVoiceSnapshot | Callable[[GeneratedAudioEntryLike], bool],
        *,
        catalogs: tuple[ReadingCatalog, ...] = (),
    ) -> None:
        self.sources = sources
        self.entry_filter = entry_filter
        self.catalogs = catalogs
        self.reference_sources: dict[Path, RecordedReferenceSources] = {}
        self.invalid_reference_manifests: set[Path] = set()
        if isinstance(entry_filter, CurrentVoiceSnapshot):
            for source in sources:
                if (
                    source.voice_manifest is None
                    or source.voice_manifest_sha256 is None
                ):
                    continue
                try:
                    self.reference_sources[source.voice_manifest] = (
                        source.reference_sources
                        or (
                            RecordedReferenceSources.capture(
                                source.voice_manifest, source.voice_manifest_sha256
                            )
                        )
                    )
                except OSError, UnicodeError, ValueError:
                    self.invalid_reference_manifests.add(source.voice_manifest)
        self.routes: defaultdict[
            tuple[str, str], list[tuple[PreparedAudioSource, GeneratedAudioEntryLike]]
        ] = defaultdict(list)
        for source in sources:
            for entry in source.library.index.entries:
                self.routes[entry.line_id, entry.text_sha256].append((source, entry))
        self.index = GeneratedAudioIndex(
            sources[0].library.manifest_path if sources else Path.cwd(),
            {},
            [
                candidates[0][0]
                for identity in sorted(self.routes)
                if (candidates := self._candidates(*identity))
            ],
        )

    def _candidates(
        self, line_id: str, text_sha256: str
    ) -> list[tuple[GeneratedAudioEntryLike, GeneratedAudioLibrary]]:
        candidates = []
        games = set()
        canonical_lines = set()
        for source, entry in self.routes.get((line_id, text_sha256), ()):
            games.add(source.game_id)
            line = source.preloader.line_for_id(line_id)
            if line is not None:
                canonical_lines.add(canonical_identity(line))
            if self._matches(entry, source):
                candidates.append((entry, source.library))
        if len(games) > 1 or len(canonical_lines) != 1:
            return []
        return sorted(
            candidates,
            key=lambda pair: (pair[0].audio_sha256, str(pair[1].manifest_path)),
        )

    def _has_original_speech(self, game_id: str, line: ChapterDialogue) -> bool:
        current = next(
            (
                row
                for catalog in reversed(self.catalogs)
                if catalog.game_id == game_id
                and (row := catalog.store.line_for_id(line.line_id or "")) is not None
            ),
            None,
        )
        identity = canonical_identity(line)
        if (
            current is not None
            and canonical_identity(current) == identity
            and current.has_original_speech
        ):
            return True
        return any(
            source.game_id == game_id
            and (row := source.preloader.line_for_id(line.line_id)) is not None
            and canonical_identity(row) == identity
            and row.has_original_speech
            and (
                current is None
                or (current.source_audio_id, current.source_audio_status)
                == (row.source_audio_id, row.source_audio_status)
            )
            for source in self.sources
        )

    def _matches(
        self, entry: GeneratedAudioEntryLike, source: PreparedAudioSource
    ) -> bool:
        line = source.preloader.line_for_id(entry.line_id)
        if line is not None and self._has_original_speech(source.game_id, line):
            return False
        if not isinstance(self.entry_filter, CurrentVoiceSnapshot):
            return self.entry_filter(entry)
        if source.voice_manifest in self.invalid_reference_manifests:
            return False
        references = (
            self.reference_sources.get(source.voice_manifest)
            if source.voice_manifest is not None
            else None
        )
        voice_role = source.voice_roles.get(entry.line_id)
        if voice_role is None:
            voice_role = source.preloader.store.voice_role_for_id(entry.line_id)
        if voice_role and re.fullmatch(r".+__vntts_[0-9a-f]{12}", voice_role):
            voice_role = None
        role = (
            synthesis_character_for_line(line.speaker, voice_role or line.speaker)
            if line
            else None
        )
        return self.entry_filter.matches(
            entry,
            role=role,
            reference_source_sha256s=references.for_entry(entry)
            if references
            else None,
        )

    def has_entry(self, line_id: str, text_sha256: str) -> bool:
        return bool(self._candidates(line_id, text_sha256))

    def reserve(self, line_id: str, text_sha256: str) -> GeneratedAudioReference | None:
        candidates = self._candidates(line_id, text_sha256)
        if not candidates:
            return None
        entry, _library = candidates[0]
        return GeneratedAudioReference(entry)

    def find(self, line_id: str, text_sha256: str) -> PreparedGeneratedAudio | None:
        return self.find_with_preflight(line_id, text_sha256)[0]

    def find_with_preflight(
        self, line_id: str, text_sha256: str
    ) -> tuple[PreparedGeneratedAudio | None, str]:
        state = "generated-audio-entry-not-found"
        for _entry, library in self._candidates(line_id, text_sha256):
            prepared, state = library.find_with_preflight(line_id, text_sha256)
            if prepared is not None:
                current = library.index.find(line_id, text_sha256, verify_file=False)
                if (
                    current is not None
                    and current.audio_sha256 == prepared.audio_sha256
                    and any(
                        source.library is library and self._matches(current, source)
                        for source in self.sources
                    )
                ):
                    return prepared, state
                state = "generated-audio-voice-changed"
        return None, state

    def find_live_fallback(self, line_id: str, text_sha256: str) -> None:
        return None

    def find_audio_event_omission(self, line_id: str, text_sha256: str) -> None:
        return None

    def progress_description(self, line_id: str, text_sha256: str) -> None:
        return None


def _document(path: Path) -> dict[str, object]:
    if path.is_symlink() or path.is_junction():
        raise ValueError("Prepared manifest must not be a link")
    value = json.loads(read_regular_bytes(path))
    if not isinstance(value, dict):
        raise ValueError("Prepared manifest must be an object")
    return value


def _signature(path: Path) -> _PathSignature:
    try:
        info = path.stat()
        names = frozenset(os.listdir(path)) if stat.S_ISDIR(info.st_mode) else None
        return info.st_mtime_ns, info.st_ctime_ns, info.st_size, names
    except OSError:
        return None


def _name_key(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _bound_component(root: Path, binding: object) -> tuple[Path, str]:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise ValueError("Prepared pack component binding is invalid")
    checksum = binding["sha256"]
    if not is_lowercase_sha256(checksum):
        raise ValueError("Prepared pack component checksum is invalid")
    relative = safe_relative_path(binding["path"], "Prepared pack component")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            raise ValueError("Prepared pack component must not be a link")
    path = contained_path(root, relative, "Prepared pack component")
    if hashlib.sha256(read_regular_bytes(path)).hexdigest() != checksum:
        raise ValueError("Prepared pack component checksum changed")
    return path, checksum


def discover_prepared_audio(
    store: PregenerationJobStore | None = None,
    *,
    game_pack: str | Path | None = None,
    story_index: str | Path | None = None,
    generated_audio_manifest: str | Path | None = None,
    warn: Callable[[str], object] | None = None,
    previous: PreparedAudioSnapshot | None = None,
    catalog_root: Path | None = None,
    game_window_title: str | None = None,
) -> PreparedAudioSnapshot:
    """Capture known pack/progress metadata once; never recurse or read WAVs."""
    warn = warn or (lambda _message: None)
    sources: dict[Path, PreparedAudioSource] = {}
    watched: set[Path] = set()
    stamps: dict[Path, _PathSignature] = {}
    prior_stamps = dict(previous.watched_paths) if previous else {}
    prior_sources = (
        {source.library.manifest_path: source for source in previous.sources}
        if previous
        else {}
    )
    prior_packs = (
        {
            source.pack_manifest: source
            for source in previous.sources
            if source.pack_manifest is not None
        }
        if previous
        else {}
    )

    def manifest_unchanged(path: Path) -> bool:
        return path in prior_stamps and _signature(path) == prior_stamps[path]

    def add_source(
        story: Path,
        manifest: Path,
        game: str,
        content_hash: str,
        *,
        story_hash: str | None = None,
        queue_hash: str | None = None,
        pack_manifest: Path | None = None,
        catalog_story_index: Path | None = None,
        voice_manifest: Path | None = None,
        voice_manifest_sha256: str | None = None,
    ) -> None:
        manifest = manifest.absolute()
        if manifest.suffix == ".sqlite3":
            require_registered_output(manifest.parent)
        if pack_manifest is None:
            watched.add(manifest)
        if manifest in sources:
            return
        old = prior_sources.get(manifest.resolve())
        if (
            old is not None
            and old.story_index == story
            and (not game or old.game_id == game)
            and (not content_hash or old.content_sha256 == content_hash)
            and (story_hash is None or old.story_sha256 == story_hash)
            and old.voice_manifest == voice_manifest
            and old.voice_manifest_sha256 == voice_manifest_sha256
            and (pack_manifest is not None or manifest_unchanged(manifest))
            and (
                queue_hash is None
                or old.library.index.metadata.get("source_queue_sha256") == queue_hash
            )
        ):
            sources[manifest] = old
            return
        canonical_game: str | None
        if (
            old is not None
            and old.story_index == story
            and (story_hash is None or old.story_sha256 == story_hash)
            and (not content_hash or old.content_sha256 == content_hash)
        ):
            actual_hash = old.story_sha256
            preloader = old.preloader
            canonical_game = old.game_id
            game_name = old.game_name
            voice_roles = old.voice_roles
        else:
            catalog = open_reading_catalog(story, game_id=game or None)
            actual_hash = catalog.revision.source_sha256
            canonical_game = catalog.game_id
            game_name = catalog.game_name
            preloader = ChapterVoicePreloader.from_store(catalog.store)
            voice_roles = {}
        if story_hash is not None and actual_hash != story_hash:
            raise ValueError("Prepared story checksum changed")
        if canonical_game and game and canonical_game != game:
            raise ValueError("Prepared story game identity changed")
        library = (
            old.library
            if old is not None and manifest_unchanged(manifest)
            else GeneratedAudioLibrary.load_optional(
                manifest, warn=warn, follow_updates=pack_manifest is None
            )
        )
        if library is None:
            return
        reference_sources = None
        if voice_manifest is not None:
            if (
                old is not None
                and old.voice_manifest == voice_manifest
                and old.voice_manifest_sha256 == voice_manifest_sha256
            ):
                reference_sources = old.reference_sources
            elif voice_manifest_sha256 is not None:
                reference_sources = RecordedReferenceSources.capture(
                    voice_manifest, voice_manifest_sha256
                )
        if (
            queue_hash is not None
            and library.index.metadata.get("source_queue_sha256") != queue_hash
        ):
            raise ValueError("Prepared progress queue identity changed")
        if any(
            (line := preloader.line_for_id(entry.line_id)) is None
            or line.text_sha256 != entry.text_sha256
            for entry in library.index.entries
        ):
            raise ValueError("Prepared audio does not belong to its story")
        sources[manifest] = PreparedAudioSource(
            canonical_game or game or game_name,
            content_hash or str(actual_hash),
            story,
            preloader,
            library,
            game_name,
            pack_manifest,
            catalog_story_index,
            voice_manifest,
            voice_manifest_sha256,
            voice_roles,
            actual_hash,
            reference_sources,
        )

    def add_pack(
        path: Path,
        game: str = "",
        content_hash: str = "",
        catalog_story_index: Path | None = None,
    ) -> None:
        old = prior_packs.get(path)
        if (
            old is not None
            and (not game or game == old.game_id)
            and (not content_hash or content_hash == old.content_sha256)
        ):
            sources[old.library.manifest_path] = old
            return
        document = _document(path)
        if document.get("schema") != "vntts.game-pack" or document.get(
            "schema_version"
        ) not in {1, 2}:
            raise ValueError("Unsupported prepared game pack")
        game_metadata = document.get("game")
        if not isinstance(game_metadata, dict) or not isinstance(
            game_metadata.get("id"), str
        ):
            raise ValueError("Prepared pack has no game identity")
        pack_game = game_metadata["id"]
        if game and game != pack_game:
            raise ValueError("Prepared pack game identity changed")
        components = document.get("components")
        if not isinstance(components, dict) or not components.get("generated_audio"):
            return
        story, story_hash = _bound_component(path.parent, components.get("story_index"))
        generated = components["generated_audio"]
        if not isinstance(generated, dict) or set(generated) != {"manifest", "wavs"}:
            raise ValueError("Prepared generated-audio component is invalid")
        manifest, _checksum = _bound_component(path.parent, generated["manifest"])
        voices, voice_hash = _bound_component(
            path.parent, components.get("voice_manifest")
        )
        add_source(
            story,
            manifest,
            pack_game,
            content_hash,
            story_hash=story_hash,
            pack_manifest=path,
            catalog_story_index=catalog_story_index,
            voice_manifest=voices,
            voice_manifest_sha256=voice_hash,
        )

    def attempt(operation: Callable[[], object]) -> None:
        try:
            operation()
        except (
            OSError,
            UnicodeError,
            ValueError,
            GeneratedAudioManifestError,
            StoryIndexError,
            PreparationCatalogError,
            PregenerationSetupError,
        ) as error:
            warn(f"Prepared audio discovery ignored: {error}")

    if store is not None:
        watched.update(
            (
                store.catalog.path,
                store.catalog.path.with_name(store.catalog.path.name + "-wal"),
            )
        )

        def from_store() -> None:
            jobs = {job["job_id"]: job for job in store.catalog.playback_jobs()}
            for job in jobs.values():
                directory = store.directory_for(str(job["job_id"]))
                root = directory / "game-packs"
                if any(
                    path.is_symlink() or path.is_junction()
                    for path in (directory, root)
                ):
                    continue
                stamps[root] = _signature(root)
                for path in sorted(root.glob("pack-*/game-pack.json")):
                    if path.parent.is_symlink() or path.parent.is_junction():
                        continue
                    attempt(
                        partial(
                            add_pack,
                            path,
                            str(job["game_id"] or ""),
                            str(job["catalog_story_index_sha256"]),
                            Path(str(job["source_story_index"] or job["story_index"]))
                            .expanduser()
                            .absolute(),
                        )
                    )
            for directory in store.generation_input_directories():
                metadata = store.catalog.input_document(directory)
                if metadata is None:
                    continue
                job_id = metadata.get("job_id")
                input_job = jobs.get(job_id) if isinstance(job_id, str) else None
                identity = metadata.get("identity")
                if input_job is None or not is_lowercase_sha256(identity):
                    continue
                if directory.name != f"generation-input-{identity[:16]}":
                    continue
                manifest = generation_state_path(
                    directory.parent / f"generation-output-{identity[:16]}"
                )
                watched.add(manifest)
                if (
                    not manifest.is_file()
                    or manifest.parent.is_symlink()
                    or manifest.parent.is_junction()
                ):
                    continue
                known_games = {
                    source.game_id
                    for source in sources.values()
                    if source.content_sha256 == input_job["catalog_story_index_sha256"]
                }
                expected_game = str(input_job["game_id"] or "")
                if not expected_game and len(known_games) == 1:
                    expected_game = next(iter(known_games))
                attempt(
                    partial(
                        add_source,
                        directory / "story-index.jsonl",
                        manifest,
                        expected_game,
                        str(input_job["catalog_story_index_sha256"]),
                        story_hash=str(metadata.get("story_index_sha256")),
                        queue_hash=str(metadata.get("queue_sha256")),
                        catalog_story_index=Path(
                            str(
                                input_job["source_story_index"]
                                or input_job["story_index"]
                            )
                        )
                        .expanduser()
                        .absolute(),
                        voice_manifest=directory / "voice-manifest.json",
                        voice_manifest_sha256=str(
                            metadata.get("voice_manifest_sha256")
                        ),
                    )
                )

        attempt(from_store)
    if game_pack:
        attempt(lambda: add_pack(Path(game_pack).expanduser().absolute()))
    if story_index and generated_audio_manifest:
        story = Path(story_index).expanduser().absolute()
        manifest = Path(generated_audio_manifest).expanduser().absolute()
        if manifest not in sources:
            attempt(lambda: add_source(story, manifest, "", ""))
    root = catalog_root or (
        store.root.parent.parent / "game-content" if store else None
    )
    paths: set[Path] = set()
    if root is not None:
        stamps[root] = _signature(root)
        for provider in root.glob("*"):
            if provider.is_dir():
                directory = provider / provider.name
                stamps[directory] = _signature(directory)
                index = directory / "story-index.jsonl"
                if index.is_file():
                    paths.add(index)
    paths.update(
        source.catalog_story_index
        for source in sources.values()
        if source.catalog_story_index
    )
    if story_index:
        paths.add(Path(story_index).expanduser().absolute())
    prior_catalogs = (
        {catalog.story_index: catalog for catalog in previous.catalogs}
        if previous
        else {}
    )
    catalogs = []
    for path in sorted(paths):
        path = path.absolute()

        def add_catalog() -> None:
            old = prior_catalogs.get(path)
            if old is not None:
                catalogs.append(old)
                return
            known = next(
                (source for source in sources.values() if source.story_index == path),
                None,
            )
            if known is None and game_window_title and not (game_pack or story_index):
                with open_regular_text(path, encoding="utf-8") as stream:
                    header = json.loads(stream.readline())
                if not isinstance(header, dict):
                    raise ValueError("Imported story metadata must be an object")
                name = _name_key(header.get("game", ""))
                if not name or f" {name} " not in f" {_name_key(game_window_title)} ":
                    return
            game = (
                known.game_id
                if known is not None
                else next(
                    (
                        source.game_id
                        for source in sources.values()
                        if source.catalog_story_index == path
                    ),
                    None,
                )
            )
            catalog = open_reading_catalog(
                path,
                provider_id=path.parent.parent.name,
                game_id=game,
            )
            if catalog.game_id:
                catalogs.append(
                    ReadingCatalog(
                        catalog.game_id,
                        catalog.game_name,
                        catalog.revision,
                        catalog.store,
                        root / catalog.game_id if root is not None else None,
                    )
                )

        attempt(add_catalog)
    stamps.update((path, _signature(path)) for path in watched)
    return PreparedAudioSnapshot(
        tuple(sources.values()),
        tuple(sorted(stamps.items())),
        tuple(catalogs),
    )

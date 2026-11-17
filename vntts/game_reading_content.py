"""Owned game-content preparation ahead of live speech routing."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import Protocol

from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.content_revision import ContentRevision
from vntts.game_audio_decoder import Cancellation, ProgressCallback
from vntts.game_content_importer import (
    GameContentImportCancelled,
    GameContentImportError,
    Reverse1999GameImporter,
)
from vntts.reading_catalog import ReadingCatalog


class ReadingAudioProvider(Protocol):
    def prepare_reading_source_audio(
        self,
        revision: ContentRevision,
        chapter: str,
        cancel_event: Cancellation | None,
        *,
        progress: ProgressCallback | None = None,
    ) -> Path: ...


def _provider(catalog: ReadingCatalog) -> ReadingAudioProvider:
    if catalog.game_id == Reverse1999GameImporter.provider_id:
        return Reverse1999GameImporter(output_root=catalog.audio_root)
    raise GameContentImportError("Original speech timing is unavailable for this game")


@dataclass
class _PreparationCancellation:
    parent: Cancellation
    requested: Event = field(default_factory=Event)

    def is_set(self) -> bool:
        return self.requested.is_set() or self.parent.is_set()


@dataclass
class _ChapterPreparation:
    catalog: ReadingCatalog
    cancellation: _PreparationCancellation
    future: Future[ChapterVoicePreloader]


class GameReadingContent:
    def __init__(
        self,
        provider_factory: Callable[[ReadingCatalog], ReadingAudioProvider] = _provider,
    ) -> None:
        self._provider_factory = provider_factory
        self._providers: dict[tuple[str, Path | None], ReadingAudioProvider] = {}
        self._chapters: dict[tuple[int, str], _ChapterPreparation] = {}
        self._work: set[Future[ChapterVoicePreloader]] = set()
        self._executor: ThreadPoolExecutor | None = None
        self._lock = Lock()
        self._closed = False

    def request_originals(
        self,
        catalog: ReadingCatalog,
        chapter: str,
        cancellation: Cancellation,
        progress: ProgressCallback,
    ) -> Future[ChapterVoicePreloader]:
        key = (id(catalog), chapter)
        with self._lock:
            if self._closed or cancellation.is_set():
                raise GameContentImportCancelled(
                    "Reading source-audio preparation cancelled"
                )
            existing = self._chapters.get(key)
            if existing is not None:
                future = existing.future
                completed = (
                    future.done()
                    and not future.cancelled()
                    and future.exception() is None
                )
                if completed or not existing.cancellation.is_set():
                    return future
            provider_key = (catalog.game_id, catalog.audio_root)
            provider = self._providers.get(provider_key)
            if provider is None:
                provider = self._provider_factory(catalog)
                self._providers[provider_key] = provider
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="game-source-audio"
                )
            token = _PreparationCancellation(cancellation)
            future = self._executor.submit(
                self._prepare, provider, catalog, chapter, token, progress
            )
            self._work = {task for task in self._work if not task.done()}
            self._work.add(future)
            self._chapters[key] = _ChapterPreparation(catalog, token, future)
            return future

    @staticmethod
    def _prepare(
        provider: ReadingAudioProvider,
        catalog: ReadingCatalog,
        chapter: str,
        cancellation: _PreparationCancellation,
        progress: ProgressCallback,
    ) -> ChapterVoicePreloader:
        def report(message: str) -> None:
            if not cancellation.is_set():
                progress(message)

        try:
            path = provider.prepare_reading_source_audio(
                catalog.revision, chapter, cancellation, progress=report
            )
            if cancellation.is_set():
                raise GameContentImportCancelled(
                    "Reading source-audio preparation cancelled"
                )
            prepared = ChapterVoicePreloader.load_optional(path)
            if cancellation.is_set():
                raise GameContentImportCancelled(
                    "Reading source-audio preparation cancelled"
                )
        except GameContentImportCancelled:
            report("Original game speech timing cancelled")
            raise
        except Exception as error:
            report(f"Original game speech timing failed: {error}")
            raise
        report("Original game speech timing ready")
        return prepared

    def prepare_original(
        self,
        catalog: ReadingCatalog,
        line: ChapterDialogue,
        cancellation: Cancellation,
        progress: ProgressCallback,
    ) -> ChapterDialogue:
        # OCR prefetch retains failures; only an actual route request retries them.
        with self._lock:
            key = (id(catalog), line.chapter)
            previous = self._chapters.get(key)
            if (
                previous is not None
                and previous.future.done()
                and not previous.future.cancelled()
                and previous.future.exception() is not None
            ):
                del self._chapters[key]
        future = self.request_originals(catalog, line.chapter, cancellation, progress)
        while not wait((future,), timeout=0.05).done:
            if cancellation.is_set():
                raise GameContentImportCancelled(
                    "Reading source-audio preparation cancelled"
                )
        if cancellation.is_set():
            raise GameContentImportCancelled(
                "Reading source-audio preparation cancelled"
            )
        try:
            timed = future.result()
        except CancelledError as error:
            raise GameContentImportCancelled(
                "Reading source-audio preparation cancelled"
            ) from error
        measured = timed.line_for_id(line.line_id)
        if (
            measured is None
            or (measured.speaker, measured.text, measured.source_audio_id)
            != (line.speaker, line.text, line.source_audio_id)
            or not measured.has_full_source_audio
        ):
            raise GameContentImportError(
                "Original speech duration could not be verified"
            )
        return measured

    def retain_catalogs(self, catalogs: Sequence[ReadingCatalog]) -> None:
        current = {id(catalog) for catalog in catalogs}
        with self._lock:
            for key, preparation in tuple(self._chapters.items()):
                if id(preparation.catalog) not in current:
                    preparation.cancellation.requested.set()
                    preparation.future.cancel()
                    del self._chapters[key]

    def cancel_pending(self) -> None:
        with self._lock:
            self._cancel_pending()

    def _cancel_pending(self) -> None:
        for key, preparation in tuple(self._chapters.items()):
            future = preparation.future
            if (
                not future.done()
                or future.cancelled()
                or future.exception() is not None
            ):
                preparation.cancellation.requested.set()
                future.cancel()
                del self._chapters[key]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel_pending()
            self._chapters.clear()
            self._providers.clear()
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def reopen(self) -> None:
        with self._lock:
            if not self._closed:
                return
            if any(not task.done() for task in self._work):
                raise RuntimeError("Game-content preparation is still stopping")
            self._executor = None
            self._work.clear()
            self._closed = False

    @property
    def is_stopped(self) -> bool:
        with self._lock:
            return all(task.done() for task in self._work)

    def wait(self) -> None:
        with self._lock:
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._work.clear()

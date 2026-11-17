"""Indexed candidate retrieval; semantic acceptance belongs to the story matcher."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, overload

from rapidfuzz.distance import Levenshtein

from vntts.audio_cache import BoundedCache
from vntts.dialog import is_symbol_only_text
from vntts.story_text_matching import prepare_text, retrieval_distance

if TYPE_CHECKING:
    from vntts.chapter_voice_preload import ChapterDialogue
    from vntts.story_catalog import DialogueCatalog


def text_projection(text: object) -> str:
    return prepare_text(text).key


def projection_may_match(
    observed: str, source: str, *, distance: int = 4, prefix: bool = True
) -> bool:
    if Levenshtein.distance(observed, source, score_cutoff=distance) <= distance:
        return True
    if not prefix:
        return False
    return any(
        Levenshtein.distance(observed, source[:end], score_cutoff=distance) <= distance
        for end in range(
            max(0, len(observed) - distance),
            min(len(source), len(observed) + distance) + 1,
        )
    )


class DialogueStore:
    """One immutable dialogue source, with bounded hydration and candidate queries."""

    def __len__(self) -> int:
        raise NotImplementedError

    @contextmanager
    def query(self, cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        if cancelled is not None and cancelled():
            from vntts.chapter_voice_preload import StoryMatchingCancelled

            raise StoryMatchingCancelled
        yield

    @property
    def has_dialogue(self) -> bool:
        return bool(len(self))

    def iter_rows(self) -> Iterator[ChapterDialogue]:
        raise NotImplementedError

    def iter_original_rows(self) -> Iterator[ChapterDialogue]:
        return (row for row in self.iter_rows() if row.has_original_speech)

    def line_for_id(self, line_id: str) -> ChapterDialogue | None:
        raise NotImplementedError

    def chapter_ids(self) -> tuple[str, ...]:
        raise NotImplementedError

    def chapter_rows(self, chapter: str) -> tuple[ChapterDialogue, ...]:
        raise NotImplementedError

    def speakers(self) -> Mapping[str, str]:
        raise NotImplementedError

    def exact_rows(
        self, text: object, *, speaker: str | None = None, normalized: bool = False
    ) -> tuple[ChapterDialogue, ...]:
        raise NotImplementedError

    def candidates(
        self,
        text: object,
        *,
        chapter: str | None = None,
        distance: int | None = None,
        prefix: bool = True,
        original: bool | None = None,
    ) -> tuple[ChapterDialogue, ...]:
        raise NotImplementedError

    def iter_candidates(
        self,
        text: object,
        *,
        chapter: str | None = None,
        distance: int | None = None,
        prefix: bool = True,
        original: bool | None = None,
    ) -> Iterator[ChapterDialogue]:
        return iter(
            self.candidates(
                text,
                chapter=chapter,
                distance=distance,
                prefix=prefix,
                original=original,
            )
        )

    @property
    def has_original_speech(self) -> bool:
        raise NotImplementedError

    @property
    def has_full_source_audio(self) -> bool:
        raise NotImplementedError

    def voice_role_for_id(self, line_id: str) -> str | None:
        return None


class MemoryDialogueStore(DialogueStore):
    """Small explicit selections and fixtures use the same candidate contract."""

    def __init__(
        self,
        rows: Iterable[ChapterDialogue] = (),
        *,
        voice_roles: Mapping[str, str] | None = None,
    ) -> None:
        from vntts.chapter_voice_preload import (
            _normalize,
            _normalize_exact_text,
            _story_position,
        )

        self.rows = tuple(rows)
        self._voice_roles = voice_roles or {}
        self.by_id = {row.line_id: row for row in self.rows if row.line_id}
        self._chapters: dict[str, tuple[ChapterDialogue, ...]] = {}
        self._speakers: dict[str, str] = {}
        self._exact: dict[tuple[bool, str], list[ChapterDialogue]] = {}
        self._normalized: dict[str, list[ChapterDialogue]] = {}
        self.normalized_values: dict[ChapterDialogue, str] = {}
        chapters: dict[str, list[ChapterDialogue]] = {}
        for row in self.rows:
            chapters.setdefault(row.chapter, []).append(row)
            self._speakers.setdefault(_normalize(row.speaker), row.speaker)
            self._exact.setdefault(
                (is_symbol_only_text(row.text), _normalize_exact_text(row.text)), []
            ).append(row)
            normalized = _normalize(row.text)
            self.normalized_values[row] = normalized
            self._normalized.setdefault(normalized, []).append(row)
        self._chapters = {
            key: tuple(sorted(value, key=_story_position))
            for key, value in chapters.items()
        }
        self._has_original = any(row.has_original_speech for row in self.rows)
        self._has_full_source = any(row.has_full_source_audio for row in self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def iter_rows(self) -> Iterator[ChapterDialogue]:
        return iter(self.rows)

    def line_for_id(self, line_id: str) -> ChapterDialogue | None:
        return self.by_id.get(line_id)

    def chapter_ids(self) -> tuple[str, ...]:
        return tuple(self._chapters)

    def chapter_rows(self, chapter: str) -> tuple[ChapterDialogue, ...]:
        return self._chapters.get(chapter, ())

    def speakers(self) -> Mapping[str, str]:
        return self._speakers

    def exact_rows(
        self, text: object, *, speaker: str | None = None, normalized: bool = False
    ) -> tuple[ChapterDialogue, ...]:
        from vntts.chapter_voice_preload import _normalize, _normalize_exact_text

        rows = (
            self._normalized.get(_normalize(text), ())
            if normalized and _normalize(text)
            else ()
            if normalized
            else self._exact.get(
                (is_symbol_only_text(text), _normalize_exact_text(text)), ()
            )
        )
        return tuple(
            row for row in rows if speaker is None or _normalize(row.speaker) == speaker
        )

    def candidates(
        self,
        text: object,
        *,
        chapter: str | None = None,
        distance: int | None = None,
        prefix: bool = True,
        original: bool | None = None,
    ) -> tuple[ChapterDialogue, ...]:
        if is_symbol_only_text(text):
            return tuple(
                row
                for row in self.exact_rows(text)
                if (chapter is None or row.chapter == chapter)
                and (original is None or row.has_original_speech == original)
            )
        shape = prepare_text(text)
        observed = shape.key
        if distance is None:
            distance = retrieval_distance(shape)
        if not observed:
            return ()
        return tuple(
            row
            for row in (self.rows if chapter is None else self.chapter_rows(chapter))
            if (original is None or row.has_original_speech == original)
            and not is_symbol_only_text(row.text)
            and projection_may_match(
                observed, text_projection(row.text), distance=distance, prefix=prefix
            )
        )

    @property
    def has_original_speech(self) -> bool:
        return self._has_original

    @property
    def has_full_source_audio(self) -> bool:
        return self._has_full_source

    def voice_role_for_id(self, line_id: str) -> str | None:
        return self._voice_roles.get(line_id)


class IndexedDialogueStore(DialogueStore):
    def __init__(self, catalog: DialogueCatalog) -> None:
        self.catalog = catalog
        self._rows: BoundedCache[str, ChapterDialogue] = BoundedCache(512)
        self._chapters: BoundedCache[str, tuple[ChapterDialogue, ...]] = BoundedCache(2)
        self._chapter_ids = catalog.chapter_ids()
        self._speakers = catalog.speakers()

    def __len__(self) -> int:
        return self.catalog.count

    @contextmanager
    def query(self, cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        with self.catalog.query(cancelled):
            yield

    def _hydrate(self, line_ids: Sequence[str]) -> tuple[ChapterDialogue, ...]:
        rows = {
            line_id: row
            for line_id in line_ids
            if (row := self._rows.get(line_id)) is not None
        }
        missing = tuple(line_id for line_id in line_ids if line_id not in rows)
        for row in self.catalog.dialogues(missing):
            assert row.line_id is not None
            rows[row.line_id] = row
            self._rows.put(row.line_id, row)
        return tuple(rows[line_id] for line_id in line_ids if line_id in rows)

    def iter_rows(self) -> Iterator[ChapterDialogue]:
        from itertools import batched

        for ids in batched(self.catalog.iter_line_ids(), 256):
            yield from self._hydrate(ids)

    def iter_original_rows(self) -> Iterator[ChapterDialogue]:
        from itertools import batched

        for ids in batched(self.catalog.iter_line_ids(original_only=True), 256):
            yield from self._hydrate(ids)

    def line_for_id(self, line_id: str) -> ChapterDialogue | None:
        self.catalog.validate_current()
        rows = self._hydrate((line_id,))
        return rows[0] if rows else None

    def chapter_ids(self) -> tuple[str, ...]:
        self.catalog.validate_current()
        return self._chapter_ids

    def chapter_rows(self, chapter: str) -> tuple[ChapterDialogue, ...]:
        self.catalog.validate_current()
        rows = self._chapters.get(chapter)
        if rows is None:
            from vntts.chapter_voice_preload import _story_position

            rows = tuple(
                sorted(
                    self._hydrate(self.catalog.line_ids(chapter)), key=_story_position
                )
            )
            self._chapters.put(chapter, rows)
        return rows

    def speakers(self) -> Mapping[str, str]:
        self.catalog.validate_current()
        return self._speakers

    def exact_rows(
        self, text: object, *, speaker: str | None = None, normalized: bool = False
    ) -> tuple[ChapterDialogue, ...]:
        from vntts.chapter_voice_preload import _normalize, _normalize_exact_text

        if normalized and not _normalize(text):
            return ()
        ids = (
            self.catalog.normalized_ids(speaker, _normalize(text))
            if normalized
            else self.catalog.exact_ids(
                speaker, is_symbol_only_text(text), _normalize_exact_text(text)
            )
        )
        return self._hydrate(ids)

    def candidates(
        self,
        text: object,
        *,
        chapter: str | None = None,
        distance: int | None = None,
        prefix: bool = True,
        original: bool | None = None,
    ) -> tuple[ChapterDialogue, ...]:
        return tuple(
            self.iter_candidates(
                text,
                chapter=chapter,
                distance=distance,
                prefix=prefix,
                original=original,
            )
        )

    def iter_candidates(
        self,
        text: object,
        *,
        chapter: str | None = None,
        distance: int | None = None,
        prefix: bool = True,
        original: bool | None = None,
    ) -> Iterator[ChapterDialogue]:
        if is_symbol_only_text(text):
            yield from (
                row
                for row in self.exact_rows(text)
                if (chapter is None or row.chapter == chapter)
                and (original is None or row.has_original_speech == original)
            )
            return
        shape = prepare_text(text)
        observed = shape.key
        if distance is None:
            distance = retrieval_distance(shape)
        if not observed:
            return
        from itertools import batched

        ids = (
            line_id
            for line_id, projection in self.catalog.iter_seed_rows(
                observed,
                max_distance=distance,
                prefix=prefix,
                chapter=chapter,
                original=original,
            )
            if projection_may_match(
                observed, projection, distance=distance, prefix=prefix
            )
        )
        first = next(ids, None)
        if first is not None:
            yield from self._hydrate((first,))
        for batch in batched(ids, 32):
            yield from self._hydrate(batch)

    @property
    def has_original_speech(self) -> bool:
        return self.catalog.has_original_speech()

    @property
    def has_full_source_audio(self) -> bool:
        return self.catalog.has_full_source_audio()

    def voice_role_for_id(self, line_id: str) -> str | None:
        return self.catalog.voice_role_for_id(line_id)


class DialogueLineMapping(Mapping[str, "ChapterDialogue"]):
    def __init__(self, store: DialogueStore) -> None:
        self.store = store

    def __getitem__(self, key: str) -> ChapterDialogue:
        row = self.store.line_for_id(key)
        if row is None:
            raise KeyError(key)
        return row

    def __iter__(self) -> Iterator[str]:
        return (row.line_id for row in self.store.iter_rows() if row.line_id)

    def __len__(self) -> int:
        return len(self.store)


class DialogueRows(Sequence["ChapterDialogue"]):
    """Explicit iteration for exports; normal reading uses indexed operations."""

    def __init__(self, store: DialogueStore) -> None:
        self.store = store

    def __len__(self) -> int:
        return len(self.store)

    @overload
    def __getitem__(self, index: int) -> ChapterDialogue: ...
    @overload
    def __getitem__(self, index: slice) -> tuple[ChapterDialogue, ...]: ...
    def __getitem__(
        self, index: int | slice
    ) -> ChapterDialogue | tuple[ChapterDialogue, ...]:
        from itertools import islice

        if isinstance(index, slice):
            if (
                index.stop is None
                or (index.start or 0) < 0
                or index.stop < 0
                or (index.step or 1) < 0
            ):
                start, stop, step = index.indices(len(self))
                if step < 0:
                    return tuple(self.store.iter_rows())[index]
            else:
                start, stop, step = index.start or 0, index.stop, index.step or 1
            return tuple(islice(self.store.iter_rows(), start, stop, step))
        if index < 0:
            index += len(self)
        if index >= 0:
            for row in islice(self.store.iter_rows(), index, index + 1):
                return row
        raise IndexError(index)

    def __iter__(self) -> Iterator[ChapterDialogue]:
        return self.store.iter_rows()

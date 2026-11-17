"""Resolve archived and current dialogue only for requested candidates."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from functools import lru_cache

from vntts.chapter_voice_preload import ChapterDialogue
from vntts.dialogue_search import DialogueStore


def canonical_identity(line: ChapterDialogue) -> tuple[object, ...]:
    return line.line_id, line.text_sha256, line.speaker, line.text, line.chapter


class PreparedDialogueStore(DialogueStore):
    def __init__(
        self,
        sources: tuple[tuple[str, DialogueStore], ...],
        catalogs: tuple[DialogueStore, ...],
    ) -> None:
        self.sources = sources
        self.catalogs = catalogs
        self.stores = (*[store for _game, store in sources], *catalogs)
        self._cached_line = lru_cache(maxsize=512)(self._resolve_line)

    @contextmanager
    def query(self, cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        with ExitStack() as stack:
            for store in dict.fromkeys(self.stores):
                stack.enter_context(store.query(cancelled))
            yield

    def line_for_id(self, line_id: str) -> ChapterDialogue | None:
        with self.query():
            return self._cached_line(line_id)

    def _resolve_line(self, line_id: str) -> ChapterDialogue | None:
        candidates = [
            (game, line)
            for game, store in self.sources
            if (line := store.line_for_id(line_id)) is not None
        ]
        current = next(
            (
                line
                for store in reversed(self.catalogs)
                if (line := store.line_for_id(line_id)) is not None
            ),
            None,
        )
        if current is not None:
            return next(
                (
                    line
                    for _game, line in sorted(
                        candidates,
                        key=lambda pair: pair[1].has_full_source_audio,
                        reverse=True,
                    )
                    if canonical_identity(line) == canonical_identity(current)
                    and line.source_audio_id == current.source_audio_id
                    and line.source_audio_status == current.source_audio_status
                    and line.source_audio_authoritative
                    and line.source_audio_duration_seconds is not None
                ),
                current,
            )
        if not candidates or len({game for game, _line in candidates}) != 1:
            return None
        if len({canonical_identity(line) for _game, line in candidates}) != 1:
            return None
        return next(
            (line for _game, line in candidates if line.has_full_source_audio),
            candidates[0][1],
        )

    def _resolved(
        self, query: Callable[[DialogueStore], tuple[ChapterDialogue, ...]]
    ) -> tuple[ChapterDialogue, ...]:
        candidates = tuple(line for store in self.stores for line in query(store))
        identities = {canonical_identity(line) for line in candidates}
        rows: dict[str, ChapterDialogue] = {}
        for candidate in candidates:
            if (
                candidate.line_id
                and (line := self.line_for_id(candidate.line_id)) is not None
                and canonical_identity(line) in identities
            ):
                rows.setdefault(candidate.line_id, line)
        return tuple(rows.values())

    def __len__(self) -> int:
        return sum(1 for _line in self.iter_rows())

    @property
    def has_dialogue(self) -> bool:
        return any(self.chapter_rows(chapter) for chapter in self.chapter_ids())

    def iter_rows(self) -> Iterator[ChapterDialogue]:
        seen: set[str] = set()
        for store in self.stores:
            for candidate in store.iter_rows():
                if not candidate.line_id or candidate.line_id in seen:
                    continue
                seen.add(candidate.line_id)
                line = self.line_for_id(candidate.line_id)
                if line is not None:
                    yield line

    def iter_original_rows(self) -> Iterator[ChapterDialogue]:
        seen: set[str] = set()
        with self.query():
            for store in self.stores:
                for candidate in store.iter_original_rows():
                    if not candidate.line_id or candidate.line_id in seen:
                        continue
                    seen.add(candidate.line_id)
                    line = self.line_for_id(candidate.line_id)
                    if line is not None and line.has_original_speech:
                        yield line

    def chapter_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                chapter for store in self.stores for chapter in store.chapter_ids()
            )
        )

    def chapter_rows(self, chapter: str) -> tuple[ChapterDialogue, ...]:
        return tuple(
            sorted(
                self._resolved(lambda store: store.chapter_rows(chapter)),
                key=lambda line: (
                    line.story_order if line.story_order is not None else line.sequence
                ),
            )
        )

    def speakers(self) -> Mapping[str, str]:
        return {
            key: name
            for store in reversed(self.stores)
            for key, name in store.speakers().items()
        }

    def exact_rows(
        self, text: object, *, speaker: str | None = None, normalized: bool = False
    ) -> tuple[ChapterDialogue, ...]:
        return self._resolved(
            lambda store: store.exact_rows(text, speaker=speaker, normalized=normalized)
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
        seen: set[str] = set()
        for store in self.stores:
            for candidate in store.iter_candidates(
                text,
                chapter=chapter,
                distance=distance,
                prefix=prefix,
                original=original,
            ):
                if not candidate.line_id or candidate.line_id in seen:
                    continue
                line = self.line_for_id(candidate.line_id)
                if (
                    line is not None
                    and canonical_identity(line) == canonical_identity(candidate)
                    and (original is None or line.has_original_speech == original)
                ):
                    seen.add(candidate.line_id)
                    yield line

    @property
    def has_original_speech(self) -> bool:
        return any(store.has_original_speech for store in self.stores)

    @property
    def has_full_source_audio(self) -> bool:
        return any(store.has_full_source_audio for store in self.stores)

    def voice_role_for_id(self, line_id: str) -> str | None:
        return next(
            (
                role
                for store in reversed(self.stores)
                if (role := store.voice_role_for_id(line_id))
            ),
            None,
        )

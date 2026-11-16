"""Imported dialogue belonging to one admitted game-content revision."""

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from vntts.chapter_voice_preload import ChapterDialogue
from vntts.content_revision import ContentRevision


@dataclass(frozen=True)
class ReadingCatalog:
    game_id: str
    game_name: str
    revision: ContentRevision
    dialogue: tuple[ChapterDialogue, ...]
    audio_root: Path | None = None

    @property
    def story_index(self) -> Path:
        return self.revision.source_path

    @cached_property
    def by_line_id(self) -> dict[str, ChapterDialogue]:
        return {line.line_id: line for line in self.dialogue if line.line_id}

"""Imported dialogue belonging to one admitted game-content revision."""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from vntts.chapter_voice_preload import ChapterDialogue
from vntts.content_revision import ContentRevision
from vntts.dialogue_search import (
    DialogueLineMapping,
    DialogueStore,
    MemoryDialogueStore,
)


@dataclass(frozen=True)
class ReadingCatalog:
    game_id: str
    game_name: str
    revision: ContentRevision
    dialogue: tuple[ChapterDialogue, ...] | DialogueStore
    audio_root: Path | None = None

    @property
    def story_index(self) -> Path:
        return self.revision.source_path

    @cached_property
    def store(self) -> DialogueStore:
        return (
            self.dialogue
            if isinstance(self.dialogue, DialogueStore)
            else MemoryDialogueStore(self.dialogue)
        )

    @cached_property
    def by_line_id(self) -> Mapping[str, ChapterDialogue]:
        return DialogueLineMapping(self.store)

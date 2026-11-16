"""Imported dialogue and lazy original-speech timing, independent of generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from threading import Lock

from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.game_audio_decoder import Cancellation, ProgressCallback


@dataclass
class ReadingCatalog:
    game_id: str
    game_name: str
    story_index: Path
    dialogue: tuple[ChapterDialogue, ...]
    audio_root: Path | None = None
    _timed: dict[str, ChapterVoicePreloader] = field(default_factory=dict, repr=False)
    _timing_lock: Lock = field(default_factory=Lock, repr=False)

    @cached_property
    def by_line_id(self) -> dict[str, ChapterDialogue]:
        return {line.line_id: line for line in self.dialogue if line.line_id}

    def prepare_original(
        self,
        line: ChapterDialogue,
        cancellation: Cancellation,
        progress: ProgressCallback,
    ) -> ChapterDialogue:
        from vntts.game_content_importer import (
            GameContentImportError,
            Reverse1999GameImporter,
        )

        if self.game_id != Reverse1999GameImporter.provider_id:
            raise GameContentImportError(
                "Original speech timing is unavailable for this game"
            )
        with self._timing_lock:
            timed = self._timed.get(line.chapter)
            if timed is None:
                importer = Reverse1999GameImporter(output_root=self.audio_root)
                path = importer.measure_reading_source_audio(
                    self.story_index, line.chapter, cancellation, progress=progress
                )
                timed = ChapterVoicePreloader.load_optional(path)
                self._timed[line.chapter] = timed
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

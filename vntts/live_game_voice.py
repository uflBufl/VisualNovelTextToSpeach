"""Resolve installed-game character references without preparing a story."""

from __future__ import annotations

import logging
from dataclasses import replace
from threading import Lock
from typing import TYPE_CHECKING

from vntts.game_audio_decoder import (
    Cancellation,
    DecoderSetupCancelled,
    DecoderSetupError,
)
from vntts.reference_quality import analyze_reference_bytes
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    VoiceManifestError,
    normalize_character_name,
    read_voice_reference_bytes,
)

if TYPE_CHECKING:
    from vntts.game_content_importer import Reverse1999GameImporter

logger = logging.getLogger(__name__)


class LiveGameVoiceReferences:
    """Reuse extractor references and missing-role results during live reading."""

    def __init__(self, importer: Reverse1999GameImporter) -> None:
        self.importer = importer
        self._lock = Lock()
        self._resolved: dict[str, CharacterVoice | None] = {}

    def resolve(
        self,
        character: str,
        *,
        cancellation: Cancellation | None = None,
    ) -> CharacterVoice | None:
        """Reuse exact same-speaker speech selected by the extractor's narrator API."""
        role = normalize_character_name(character)
        if not role:
            return None
        with self._lock:
            self._check_cancelled(cancellation)
            if role in self._resolved:
                return self._resolved[role]
            try:
                from r1999extractor.story_voice_candidates import (
                    StoryVoiceCandidateError,
                )

                from vntts.game_content_importer import (
                    GameContentImportCancelled,
                    GameContentImportError,
                )
            except ImportError as error:
                logger.warning("Installed-game voices unavailable: %s", error)
                self._resolved[role] = None
                return None
            try:
                voice = self._prepare(character, cancellation)
                self._check_cancelled(cancellation)
            except GameContentImportCancelled, DecoderSetupCancelled:
                raise
            except (
                GameContentImportError,
                StoryVoiceCandidateError,
                DecoderSetupError,
                VoiceManifestError,
                OSError,
                ValueError,
                ImportError,
            ) as error:
                logger.warning(
                    "Installed-game voice unavailable for %s: %s", character, error
                )
                voice = None
            self._resolved[role] = voice
            return voice

    def _prepare(
        self, character: str, cancellation: Cancellation | None
    ) -> CharacterVoice | None:
        from vntts.pregeneration_voices import (
            STORY_LINE_ROUTE,
            automatic_voice_reference_is_eligible,
        )

        for reference in self.importer.narrator_references(character):
            self._check_cancelled(cancellation)
            manifest = self.importer.prepare_voice_roles(
                (character,),
                cancellation,
                narrator=True,
                narrator_line_id=reference.line_id,
                speech_backend="pocket-tts",
            )
            voices = CharacterVoiceRegistry.from_file(manifest).unique_voices()
            if len(voices) != 1 or len(voices[0].references) != 1:
                return None
            voice = voices[0]
            report = analyze_reference_bytes(
                read_voice_reference_bytes(voice, voice.references[0]),
                path=voice.references[0],
            )
            if report["objective_preflight"] == "pass" and (
                automatic_voice_reference_is_eligible(
                    candidate_origin=STORY_LINE_ROUTE,
                    source_bank=reference.source_bank,
                    source_line_ids=(reference.line_id,),
                    duration_seconds=report["duration_seconds"],
                )
            ):
                return replace(
                    voice,
                    character=character,
                    speaker=normalize_character_name(character),
                    source_character=character,
                    reference_transcript=reference.text,
                )
        return None

    @staticmethod
    def _check_cancelled(cancellation: Cancellation | None) -> None:
        if cancellation is not None and cancellation.is_set():
            raise DecoderSetupCancelled("Live voice preparation cancelled")

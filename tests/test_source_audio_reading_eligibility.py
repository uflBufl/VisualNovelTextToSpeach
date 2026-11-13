import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import numpy as np
from vntts_artifacts.generated_audio import text_sha256

from tests.audio_output_fixtures import FakeAudioOutput
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioRoute,
    LiveTTSRoute,
    PreparedGeneratedAudio,
    SourceAudioRoute,
)
from vntts.live import SpeechChunk
from vntts.ocr import OCRResult
from vntts.playback import PreparedPlayback
from vntts.settings import AppSettings
from vntts.speech_backend_contract import SpeechBackendCapabilities


def story_line(line_id, text="Huh?", *, chapter=None, speaker="Ada", full=False):
    return ChapterDialogue(
        line_id,
        chapter or line_id,
        1,
        speaker,
        text,
        text_sha256(text),
        source_audio_status="available" if full else "absent",
        source_audio_id=f"voice:{line_id}" if full else None,
        source_audio_duration_seconds=1.0 if full else None,
        source_audio_completeness="full" if full else "unknown",
        source_audio_authoritative=full,
    )


class SourceAudioReadingEligibilityTests(unittest.TestCase):
    def controller(self, rows, *, generated=True):
        controller = AppController(
            AppSettings(
                audio_source_policy="prefer-generated",
                speaker_announcement_mode="off",
            ),
            chapter_voice_preloader=ChapterVoicePreloader(rows),
        )
        controller.automatic_prepared_reading = True
        live = Mock()
        live.name = "live-test"
        live.capabilities = SpeechBackendCapabilities(False, False, True)
        live.prepare_playback.return_value = PreparedPlayback(
            object(), 0.0, 0.0, None, "live"
        )
        library = None
        if generated:
            library = Mock(runtime_progress=False)
            library.find_audio_event_omission.return_value = None
            library.find_with_preflight.side_effect = lambda line_id, text_hash: (
                PreparedGeneratedAudio(
                    line_id, text_hash, np.zeros(1, dtype=np.float32), 24_000
                ),
                "generated-audio-entry-ready",
            )
        backend = GeneratedAudioFallbackBackend(
            live,
            library,
            controller.chapter_voice_preloader,
            audio_source_policy="prefer-generated",
            audio_output=FakeAudioOutput(),
        )
        backend.set_live_mode_active(True)
        controller.speech_backend = backend
        return controller, live

    def resolve(self, controller, text="Huh?", speaker="Ada"):
        resolution = {}
        admitted = controller._resolve_story_ocr_result(
            OCRResult(speaker, text, 95, "fixture", 1), resolution=resolution
        )
        return admitted, resolution.get("line")

    def route(self, controller, admitted, line):
        return controller._prepare_live_chunk(
            SpeechChunk(
                1,
                admitted.character,
                admitted.text,
                line_id=None if line is None else line.line_id,
            )
        )

    def test_full_originals_in_other_chapters_do_not_compete_with_unvoiced_line(self):
        unvoiced = story_line("unvoiced")
        controller, live = self.controller(
            (
                story_line("original-a", full=True),
                unvoiced,
                story_line("original-b", full=True),
            )
        )

        admitted, line = self.resolve(controller)

        self.assertIs(line, unvoiced)
        route = self.route(controller, admitted, line)
        self.assertIsInstance(route, GeneratedAudioRoute)
        self.assertEqual(route.trace.line_id, unvoiced.line_id)
        live.prepare_playback.assert_not_called()

    def test_unvoiced_duplicates_remain_ambiguous(self):
        controller, live = self.controller(
            (
                story_line("unvoiced-a"),
                story_line("unvoiced-b"),
                story_line("original", full=True),
            )
        )

        admitted, line = self.resolve(controller)

        self.assertIsNone(line)
        self.assertIsInstance(self.route(controller, admitted, line), LiveTTSRoute)
        live.prepare_playback.assert_called_once_with("Ada", "Huh?")

    def test_known_chapter_original_keeps_its_identity(self):
        original = story_line("original", full=True)
        controller, live = self.controller((original, story_line("unvoiced")))
        controller.chapter_voice_preloader.select_line_id(original.line_id)

        admitted, line = self.resolve(controller)

        self.assertIs(line, original)
        self.assertIsInstance(self.route(controller, admitted, line), SourceAudioRoute)
        self.assertEqual(
            controller.chapter_voice_preloader.current_match.chapter, original.chapter
        )
        live.prepare_playback.assert_not_called()

    def test_global_recovery_ignores_other_chapter_original_duplicates(self):
        anchor = story_line("anchor", "The previously identified chapter.")
        unvoiced = story_line("unvoiced")
        controller, _live = self.controller(
            (anchor, story_line("original", full=True), unvoiced)
        )
        controller.chapter_voice_preloader.select_line_id(anchor.line_id)

        _admitted, line = self.resolve(controller)

        self.assertIs(line, unvoiced)
        self.assertEqual(
            controller.chapter_voice_preloader.current_match.chapter, unvoiced.chapter
        )

    def test_generated_prefix_never_expands_to_full_original(self):
        text = "We should leave this place before the sun rises."
        original = story_line("original", text, full=True)
        unvoiced = story_line("unvoiced", text)
        for rows, anchor, expected in (
            ((original,), None, None),
            ((original, unvoiced), None, unvoiced),
            ((original, unvoiced), original, None),
        ):
            with self.subTest(rows=rows, anchor=anchor):
                controller, live = self.controller(rows)
                if anchor is not None:
                    controller.chapter_voice_preloader.select_line_id(anchor.line_id)
                line = controller._resolve_early_indexed_dialogue(
                    "Ada", "We should leave this place"
                )
                self.assertIs(line, expected)
                self.assertFalse(
                    controller.speech_backend.reserve_generated_line_for_early_playback(
                        original
                    )
                )
                live.prepare_playback.assert_not_called()

    def test_unique_full_original_is_recognized_despite_old_generated_audio(self):
        original = story_line(
            "original", "The complete original spoken line.", full=True
        )
        controller, live = self.controller((original,))

        admitted, line = self.resolve(controller, original.text)

        self.assertIs(line, original)
        route = self.route(controller, admitted, line)
        self.assertIsInstance(route, SourceAudioRoute)
        self.assertEqual(route.prepared.source_audio_id, original.source_audio_id)
        self.assertEqual(route.prepared.completion_seconds, 1.35)
        controller.speech_backend.library.find_with_preflight.assert_not_called()
        live.prepare_playback.assert_not_called()

    def test_controller_builds_source_route_without_a_generated_library(self):
        original = story_line("original", "The original spoken line.", full=True)
        for policy in ("prefer-generated", "live-tts-only"):
            with self.subTest(policy=policy):
                controller, live = self.controller((original,), generated=False)
                controller.settings = controller.settings.updated(
                    audio_source_policy=policy
                )
                with patch.object(controller, "_refresh_prepared_audio"):
                    backend = controller._create_generated_audio_backend(live)
                self.assertIsInstance(backend, GeneratedAudioFallbackBackend)
                controller.speech_backend = backend
                backend.set_live_mode_active(True)
                admitted, line = self.resolve(controller, original.text)
                self.assertIs(line, original)
                self.assertIsInstance(
                    self.route(controller, admitted, line), SourceAudioRoute
                )
                live.prepare_playback.assert_not_called()

    def test_live_tts_only_factory_keeps_generated_library_out_of_live_routes(self):
        original = story_line("original", "The original spoken line.", full=True)
        unvoiced = story_line("unvoiced", "The unvoiced spoken line.")
        controller, live = self.controller((original, unvoiced))
        controller.prepared_audio_library = controller.speech_backend.library
        controller.settings = controller.settings.updated(
            audio_source_policy="live-tts-only"
        )
        with patch.object(controller, "_refresh_prepared_audio"):
            backend = controller._create_generated_audio_backend(live)
        self.assertIsInstance(backend, GeneratedAudioFallbackBackend)
        self.assertIsNone(backend.library)
        backend.set_live_mode_active(True)

        self.assertIsInstance(
            backend.prepare_route(
                original.speaker, original.text, line_id=original.line_id
            ),
            SourceAudioRoute,
        )
        self.assertIsInstance(
            backend.prepare_route(
                unvoiced.speaker, unvoiced.text, line_id=unvoiced.line_id
            ),
            LiveTTSRoute,
        )
        live.prepare_playback.assert_called_once_with(unvoiced.speaker, unvoiced.text)

    def test_narrator_without_verified_full_original_remains_readable(self):
        original = story_line(
            "narration", "The door opens.", speaker="Narrator", full=True
        )
        for line in (
            replace(original, source_audio_completeness="partial"),
            replace(original, source_audio_completeness="unknown"),
            replace(original, source_audio_authoritative=False),
            replace(original, source_audio_duration_seconds=None),
            replace(original, source_audio_status="absent"),
        ):
            for generated in (False, True):
                with self.subTest(line=line, generated=generated):
                    controller, live = self.controller((line,), generated=generated)
                    admitted, resolved = self.resolve(controller, line.text, "Narrator")
                    self.assertIs(resolved, line)
                    route = self.route(controller, admitted, resolved)
                    self.assertIsInstance(
                        route, GeneratedAudioRoute if generated else LiveTTSRoute
                    )
                    if generated:
                        live.prepare_playback.assert_not_called()
                    else:
                        live.prepare_playback.assert_called_once_with(
                            "Narrator", line.text
                        )


if __name__ == "__main__":
    unittest.main()

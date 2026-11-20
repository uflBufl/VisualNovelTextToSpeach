import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np
from vntts_artifacts.generated_audio import text_sha256

from tests.audio_output_fixtures import FakeAudioOutput
from tests.test_plotvoc_source_audio import QUOTE, plotvoc_story
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioReference,
    GeneratedAudioRoute,
    LiveTTSRoute,
    PreparedGeneratedAudio,
    SourceAudioRoute,
)
from vntts.live import SpeechChunk
from vntts.live_tracking import IncrementalDialogTracker
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
    def test_hansel_typewriter_waits_for_upcoming_original_without_visible_name(self):
        prefixes = (
            '"Whe',
            '"What really s:',
            '"What really scared n',
            '"What really scared me was nc',
            '"What really scared me was not the thr:',
            '"What really scared me was not the threat to my life, bu',
            '"What really scared me was not the threat to my life, but the possil',
            '"What really scared me was not the threat to my life, '
            "but the possibility of d\\",
            '"What really scared me was not the threat to my life, '
            "but the possibility of dying ignor",
        )
        for timed in (False, True):
            with self.subTest(timed=timed), TemporaryDirectory() as directory:
                story = plotvoc_story(Path(directory), timed=timed)
                preloader = ChapterVoicePreloader.load_optional(story.path)
                controller, live = self.controller(preloader.dialogue)
                preloader = controller.chapter_voice_preloader
                previous = preloader.select_line_id("reverse1999:100631:58")
                owner = preloader.current_match
                tracker = IncrementalDialogTracker(
                    complete_dialogue_only=True,
                    early_dialogue_resolver=controller._resolve_early_indexed_dialogue,
                    incomplete_dialogue_probe=preloader.is_unique_incomplete_prefix,
                    clock=lambda: 0.0,
                )
                tracker.observe_identified(
                    previous.speaker, previous.text, previous.line_id, exact=True
                )
                tracker.expect_new_dialog()

                for prefix in prefixes:
                    admitted, line = self.resolve(controller, prefix, "Narrator")
                    self.assertIsNone(line)
                    self.assertEqual(
                        tracker.observe(admitted.character, admitted.text), []
                    )
                    self.assertEqual(preloader.current_match, owner)

                admitted, line = self.resolve(controller, QUOTE, "Narrator")
                self.assertEqual(line.line_id, "reverse1999:100631:59")
                chunks = tracker.observe_identified(
                    admitted.character, admitted.text, line.line_id, exact=True
                )
                self.assertEqual(
                    [(chunk.line_id, chunk.character, chunk.text) for chunk in chunks],
                    [(line.line_id, "Vertin", QUOTE)],
                )
                library = controller.speech_backend.library
                library.find_with_preflight.assert_not_called()
                library.reserve.assert_not_called()
                live.prepare_playback.assert_not_called()

    def test_rejected_early_generated_candidate_preserves_current_owner(self):
        previous = story_line("previous", "The scene begins.", chapter="known")
        candidate = replace(
            story_line(
                "candidate",
                "We should leave this place before the sun rises.",
                chapter="known",
            ),
            sequence=2,
        )
        for anchored in (False, True):
            with self.subTest(anchored=anchored):
                controller, live = self.controller((previous, candidate))
                preloader = controller.chapter_voice_preloader
                if anchored:
                    preloader.select_line_id(previous.line_id)
                owner = preloader.current_match
                library = controller.speech_backend.library
                library.find_with_preflight.side_effect = None
                library.find_with_preflight.return_value = (
                    None,
                    "generated-audio-entry-missing",
                )

                self.assertIsNone(
                    controller._resolve_early_indexed_dialogue(
                        "Ada", "We should leave this place"
                    )
                )

                self.assertEqual(preloader.current_match, owner)
                library.find_with_preflight.assert_called_once_with(
                    candidate.line_id, candidate.text_sha256
                )
                live.prepare_playback.assert_not_called()

    def test_hansel_repeated_quote_uses_upcoming_plotvoc_not_old_narrator_recording(
        self,
    ):
        with TemporaryDirectory() as directory:
            story = plotvoc_story(Path(directory), timed=True)
            preloader = ChapterVoicePreloader.load_optional(story.path)
            controller, live = self.controller(preloader.dialogue)
            controller.chapter_voice_preloader.select_line_id("reverse1999:100631:58")

            admitted, line = self.resolve(controller, QUOTE, "Narrator")

            self.assertEqual(line.line_id, "reverse1999:100631:59")
            route = self.route(controller, admitted, line)
            self.assertIsInstance(route, SourceAudioRoute)
            self.assertEqual(route.prepared.source_audio_id, "610018168")
            controller.speech_backend.library.find_with_preflight.assert_not_called()
            controller.speech_backend.library.reserve.assert_not_called()
            live.prepare_playback.assert_not_called()

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

            def ready_audio(line_id, text_hash):
                return (
                    PreparedGeneratedAudio(
                        line_id, text_hash, np.zeros(1, dtype=np.float32), 24_000
                    ),
                    "generated-audio-entry-ready",
                )

            def reserve_audio(line_id, text_hash):
                reference = Mock(spec=GeneratedAudioReference)
                reference.line_id = line_id
                reference.load.return_value = ready_audio(line_id, text_hash)
                return reference

            library.find_with_preflight.side_effect = ready_audio
            library.reserve.side_effect = reserve_audio
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
        resolution = controller._resolve_story_ocr_result(
            OCRResult(speaker, text, 95, "fixture", 1)
        )
        return resolution.ocr_result, resolution.line

    def route(self, controller, admitted, line):
        accepted = controller._accept_live_chunk(
            SpeechChunk(
                1,
                admitted.character,
                admitted.text,
                line_id=None if line is None else line.line_id,
            )
        )
        self.assertIsNotNone(accepted)
        return controller._prepare_live_chunk(accepted)

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

    def test_unvoiced_duplicates_stay_canonically_ambiguous_with_equivalent_audio(self):
        controller, live = self.controller(
            (
                story_line("unvoiced-a"),
                story_line("unvoiced-b"),
                story_line("original", full=True),
            )
        )

        admitted, line = self.resolve(controller)

        self.assertIsNone(line)
        route = self.route(controller, admitted, line)
        self.assertIsInstance(route, GeneratedAudioRoute)
        self.assertIsNone(route.trace.line_id)
        self.assertIn(route.trace.audio_line_id, ("unvoiced-a", "unvoiced-b"))
        self.assertIsNone(controller.chapter_voice_preloader.current_match)
        live.prepare_playback.assert_not_called()

    def test_unvoiced_duplicates_without_ready_audio_still_use_live_tts(self):
        controller, live = self.controller(
            (story_line("unvoiced-a"), story_line("unvoiced-b")), generated=False
        )

        admitted, line = self.resolve(controller)

        self.assertIsNone(line)
        self.assertIsInstance(self.route(controller, admitted, line), LiveTTSRoute)
        self.assertIsNone(controller.chapter_voice_preloader.current_match)
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

    def test_known_chapter_resolves_original_before_synthesis_candidates(self):
        for text, other_text in (
            ("I ...", "Isolde stands alone on the street."),
            ("Huh?", "Huh!"),
        ):
            for speaker in ("Ada", "Narrator"):
                with self.subTest(text=text, speaker=speaker):
                    anchor = story_line("anchor", "The scene begins.", chapter="known")
                    original = replace(
                        story_line("original", text, chapter="known", full=True),
                        sequence=2,
                    )
                    other = replace(
                        story_line("unvoiced", other_text, chapter="known"), sequence=3
                    )
                    controller, live = self.controller((anchor, original, other))
                    controller.chapter_voice_preloader.select_line_id(anchor.line_id)

                    admitted, line = self.resolve(controller, text, speaker)

                    self.assertIs(line, original)
                    route = self.route(controller, admitted, line)
                    self.assertIsInstance(route, SourceAudioRoute)
                    self.assertEqual(route.trace.line_id, original.line_id)
                    live.prepare_playback.assert_not_called()
                    library = controller.speech_backend.library
                    library.find_with_preflight.assert_not_called()

    def test_known_chapter_duplicate_identity_is_not_resolved_by_audio_availability(
        self,
    ):
        original = story_line("original", chapter="known", full=True)
        unvoiced = story_line("unvoiced", chapter="known")
        controller, _live = self.controller((original, unvoiced))
        controller.chapter_voice_preloader.select_line_id(original.line_id)

        _admitted, line = self.resolve(controller)

        self.assertIsNone(line)
        self.assertEqual(
            controller.chapter_voice_preloader.current_match.chapter, "known"
        )

    def test_known_chapter_enqueue_preserves_original_identity(self):
        original = story_line("original", chapter="known", full=True)
        unvoiced = story_line("unvoiced", "Huh!", chapter="known")
        controller, _live = self.controller((original, unvoiced))
        controller.chapter_voice_preloader.select_line_id(original.line_id)
        controller.live_reader = Mock(is_running=True)
        with (
            patch.object(
                controller, "_canonical_observed_character", return_value="Ada"
            ),
            patch.object(controller, "_dialog_observed", return_value=None),
        ):
            self.assertTrue(controller._enqueue_dialog("Ada", original.text))

        character, text = controller.live_reader.enqueue.call_args.args
        line_id = controller.live_reader.enqueue.call_args.kwargs["line_id"]
        self.assertEqual(line_id, original.line_id)
        route = controller.speech_backend.prepare_route(
            character, text, line_id=line_id, resolve_unidentified=False
        )
        self.assertIsInstance(route, SourceAudioRoute)
        self.assertEqual(route.trace.line_id, original.line_id)

    def test_known_chapter_prefix_keeps_originals_in_uniqueness_check(self):
        text = "We should leave this place before the sun rises."
        original = story_line("original", text, chapter="known", full=True)
        unvoiced = story_line("unvoiced", text + " Follow me.", chapter="known")
        controller, live = self.controller((original, unvoiced))
        controller.chapter_voice_preloader.select_line_id(original.line_id)

        self.assertIsNone(
            controller._resolve_early_indexed_dialogue(
                "Ada", "We should leave this place"
            )
        )
        controller.speech_backend.library.find_with_preflight.assert_not_called()
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
                if expected is None:
                    controller.speech_backend.library.find_with_preflight.assert_not_called()
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

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image
from vntts_artifacts.generated_audio import text_sha256

from vntts.chapter_voice_preload import (
    ChapterDialogue,
    ChapterMatch,
    ChapterVoicePreloader,
)
from vntts.controller import AppController
from vntts.dialog_capture import (
    CapturedDialogFrame,
    OCRUncertainError,
    analyze_dialog_snapshot,
)
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioRoute,
    PreparedGeneratedAudio,
)
from vntts.live_sequence import StoryCursorState
from vntts.ocr import OCRResult
from vntts.settings import AppSettings
from vntts.speech_backend import SpeechBackendCapabilities
from vntts.voices import CharacterVoiceRegistry


def story_line(text, speaker="Narrator", line_id="line-1", chapter="chapter-1"):
    return ChapterDialogue(line_id, chapter, 1, speaker, text, text_sha256(text))


def observed(text, speaker="a ae Ce", dialogue_confidence=94.9):
    return OCRResult(
        speaker,
        text,
        26,
        "balanced",
        3,
        processed_image=Image.new("L", (100, 40)),
        speaker_image=Image.new("L", (80, 20)),
        speaker_confidence=26,
        dialogue_confidence=dialogue_confidence,
    )


class StoryOCRAuthorityTest(unittest.TestCase):
    def controller(self, rows):
        controller = AppController(
            AppSettings(audio_source_policy="prefer-generated"),
            chapter_voice_preloader=ChapterVoicePreloader(rows),
        )
        controller.voice_router = SimpleNamespace(registry=CharacterVoiceRegistry())
        controller.uncertain_frame_recorder = None
        controller._ocr_uncertain = Mock()
        controller._publish_diagnostic = Mock()
        controller._resolve_voice_label = Mock(return_value="Prepared story voice")
        return controller

    def test_downloaded_failures_reach_the_canonical_generated_recording(self):
        for text, noise, speaker in (
            (
                "She scans the instructions quickly, then signs her name in the bottom right corner.",
                "To a",
                "Narrator",
            ),
            (
                "The clerk's suspicious gaze slides past Cornerstone.",
                "a ae Ce",
                "Narrator",
            ),
            (
                "Known words from the prepared character recording.",
                "Narrator",
                "Cornerstone",
            ),
        ):
            with self.subTest(text=text):
                line = story_line(text, speaker)
                controller = self.controller((line,))
                raw = observed(text.upper(), noise)
                resolved = controller._resolve_story_ocr_result(raw)
                self.assertEqual((resolved.character, resolved.text), (speaker, text))
                self.assertEqual(resolved.confidence, 94.9)
                self.assertEqual(resolved.speaker_confidence, 26)
                self.assertIs(resolved.speaker_image, raw.speaker_image)
                self.assertIn(noise, resolved.speaker_note)
                self.assertIsNone(controller.chapter_voice_preloader.current_match)
                with patch(
                    "vntts.dialog_capture.recognize_dialog_image_result",
                    return_value=raw,
                ):
                    actual = controller._recognize_live_frame(
                        CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
                    )
                self.assertEqual(actual, (speaker, text))
                controller._ocr_uncertain.assert_not_called()
                snapshot = controller._publish_diagnostic.call_args.args[0]
                self.assertEqual(snapshot.character, speaker)
                self.assertEqual(snapshot.speaker_confidence, 26)
                self.assertIn(noise, snapshot.speaker_note)

                live = Mock(name="live-test")
                live.name = "live-test"
                live.capabilities = SpeechBackendCapabilities(True, False, True)
                prepared = PreparedGeneratedAudio(
                    line.line_id,
                    line.text_sha256,
                    np.zeros(1, dtype=np.float32),
                    24_000,
                )
                library = Mock(runtime_progress=False)
                library.find_audio_event_omission.return_value = None
                library.find_with_preflight.return_value = (
                    prepared,
                    "generated-audio-entry-verified",
                )
                backend = GeneratedAudioFallbackBackend(
                    live,
                    library,
                    controller.chapter_voice_preloader,
                    audio_output=Mock(),
                )
                route = backend.prepare_route(*actual)
                self.assertIsInstance(route, GeneratedAudioRoute)
                self.assertIs(route.prepared, prepared)
                self.assertEqual(route.trace.line_id, line.line_id)
                live.prepare_playback.assert_not_called()

    def test_ambiguous_unmatched_partial_or_uncertain_text_keeps_original_gate(self):
        text = "The clerk's suspicious gaze slides past Cornerstone."
        line = story_line(text, "Cornerstone")
        for rows, raw in (
            (
                (line, story_line(text, "Someone else", "line-2")),
                observed(text, "Cornerstone"),
            ),
            ((line,), observed("The clerk's suspicious gaze")),
            (
                (line, story_line(text + " More words.", "Other", "line-2")),
                observed(text),
            ),
            ((line,), observed("Unrelated complete text.")),
            ((line,), observed(text, dialogue_confidence=35)),
            ((replace(line, text_sha256=None),), observed(text)),
            ((replace(line, line_id=None),), observed(text)),
        ):
            with self.subTest(rows=rows, text=raw.text):
                controller = self.controller(rows)
                self.assertIs(controller._resolve_story_ocr_result(raw), raw)
                with patch(
                    "vntts.dialog_capture.recognize_dialog_image_result",
                    return_value=raw,
                ):
                    with self.assertRaises(OCRUncertainError):
                        controller._recognize_live_frame(
                            CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
                        )
                controller._ocr_uncertain.assert_not_called()

    def test_cursor_candidates_keep_an_exact_match_inside_the_selected_path(self):
        text = "A repeated complete phrase."
        first = story_line(text, "Correct speaker")
        other = story_line(text, "Wrong branch", "line-2")
        controller = self.controller((first, other))
        controller.settings = controller.settings.updated(
            live_sequence_mode="audio-manual"
        )
        event = SimpleNamespace(line_id=first.line_id, is_speech=True)
        forbidden = SimpleNamespace(line_id=other.line_id, is_speech=True)
        controller.live_sequence_plan = SimpleNamespace(
            events={"first": event, "forbidden": forbidden}
        )
        controller.story_cursor = SimpleNamespace(
            state=StoryCursorState.LOCKED,
            current_event=event,
            bounded_visible_successors=Mock(return_value=()),
        )
        raw = observed(text, "Wrong branch")
        resolved = controller._resolve_story_ocr_result(raw)
        self.assertEqual(resolved.character, first.speaker)
        self.assertIsNone(controller.chapter_voice_preloader.current_match)
        self.assertEqual(controller.story_cursor.state, StoryCursorState.LOCKED)

    def test_current_chapter_is_preserved_and_manual_diagnostics_get_the_same_resolution(
        self,
    ):
        text = "A complete prepared line."
        line = story_line(text, "Actual speaker")
        other = story_line(text, "Other chapter speaker", "line-2", "chapter-2")
        controller = self.controller((line, other))
        previous = ChapterMatch(line.chapter, 0, 1.0)
        controller.chapter_voice_preloader.current_match = previous
        snapshots = []
        raw = observed(text)
        with (
            patch(
                "vntts.dialog_capture.capture_dialog",
                return_value=(Image.new("RGB", (300, 100)), None),
            ),
            patch(
                "vntts.dialog_capture.recognize_dialog_image_result", return_value=raw
            ),
        ):
            _, _, result = analyze_dialog_snapshot(
                "captures",
                result_resolver=controller._resolve_story_ocr_result,
                diagnostic_handler=snapshots.append,
            )
        self.assertEqual(result.character, line.speaker)
        self.assertEqual(snapshots[0].speaker_confidence, 26)
        self.assertTrue(snapshots[0].speaker_note)
        self.assertIs(controller.chapter_voice_preloader.current_match, previous)


if __name__ == "__main__":
    unittest.main()

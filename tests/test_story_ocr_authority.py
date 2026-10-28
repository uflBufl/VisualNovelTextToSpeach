import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image
from vntts_artifacts.generated_audio import text_sha256

from tests.test_live import AutoAdvanceFakeFrameHarness
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
from vntts.live import RecognizedDialog
from vntts.live_sequence import StoryCursorState
from vntts.ocr import OCRResult, recognize_dialog_image_result
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

    def assert_generated_route(self, controller, line, actual):
        if isinstance(actual, RecognizedDialog):
            self.assertEqual(actual.line_id, line.line_id)
            actual = actual.character, actual.text
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
        library.find_with_preflight.assert_called_once_with(
            line.line_id, line.text_sha256
        )
        live.prepare_playback.assert_not_called()

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
                self.assertEqual(
                    actual,
                    RecognizedDialog(
                        speaker, text, line.line_id, allow_unheard_replacement=False
                    ),
                )
                controller._ocr_uncertain.assert_not_called()
                snapshot = controller._publish_diagnostic.call_args.args[0]
                self.assertEqual(snapshot.character, speaker)
                self.assertEqual(snapshot.speaker_confidence, 26)
                self.assertIn(noise, snapshot.speaker_note)

                self.assert_generated_route(controller, line, actual)

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_retained_frames_reach_the_canonical_generated_recording(self):
        fixtures = Path(__file__).resolve().parent / "fixtures" / "ocr-prepared-lines"
        cases = (
            (66, "So, where is it?", "Grundriss"),
            (74, "So what's the hold-up?", "Grundriss"),
            (76, "Remember Nowa Miedź?", "Cornerstone"),
            (92, "I'll handle it.", "Cornerstone"),
        )
        rows = tuple(
            story_line(text, speaker, f"line-{number}")
            for number, text, speaker in cases
        )
        for line, (number, text, speaker) in zip(rows, cases, strict=True):
            with (
                self.subTest(line=number),
                Image.open(fixtures / f"line{number:03d}.png") as image,
            ):
                raw = recognize_dialog_image_result(image)
                controller = self.controller(rows)
                resolved = controller._resolve_story_ocr_result(raw)
                self.assertEqual((resolved.character, resolved.text), (speaker, text))
                self.assertEqual(resolved.confidence, raw.dialogue_confidence)
                self.assertIsNone(controller.chapter_voice_preloader.current_match)
                self.assert_generated_route(
                    controller, line, (resolved.character, resolved.text)
                )

    def test_diacritics_and_short_ocr_typos_reach_the_canonical_recording(self):
        for text, ocr_text in (
            ("Remember Nowa Miedź?", "Remember Nowa Miedz?"),
            ("Remember Nowa Miedź?", "Rememher Nowa Miedz?"),
            ("Ouch!", "Oucb!"),
        ):
            with self.subTest(text=text, ocr_text=ocr_text):
                line = story_line(text, "Cornerstone")
                controller = self.controller((line,))
                raw = observed(ocr_text)
                resolved = controller._resolve_story_ocr_result(raw)
                self.assertEqual(
                    (resolved.character, resolved.text), (line.speaker, line.text)
                )
                self.assertEqual(resolved.confidence, raw.dialogue_confidence)
                self.assertIsNone(controller.chapter_voice_preloader.current_match)
                self.assert_generated_route(
                    controller, line, (resolved.character, resolved.text)
                )

    def test_ambiguous_unmatched_partial_or_uncertain_text_keeps_original_gate(self):
        text = "The clerk's suspicious gaze slides past Cornerstone."
        line = story_line(text, "Cornerstone")
        accented = story_line("Remember Nowa Miedź?", "Cornerstone")
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
            (
                (
                    accented,
                    story_line("Remember Nowa Miedż?", "Someone else", "line-2"),
                ),
                observed("Remember Nowa Miedz?"),
            ),
            (
                (accented,),
                observed("Remember Nowa Miedz?", dialogue_confidence=35),
            ),
            (
                (story_line("Ouch!"),),
                observed("Oucb!", dialogue_confidence=35),
            ),
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

    def test_saved_narrator_frames_resolve_exact_and_normalized_text_only(self):
        for text, raw_text, noise in (
            (
                "She picks up a discarded draft and places it atop the mountain "
                "of paper beside her.",
                "She picks up a discarded draft and places it atop the mountain "
                "of paper beside her.",
                "les ad 5 rr ww",
            ),
            (
                "The sheer height of the stack finally catches up to her.",
                "The sheer height of the stack-finally catches up to her.",
                "— ~- es . | .",
            ),
        ):
            with self.subTest(text=text):
                controller = self.controller((story_line(text),))
                raw = observed(raw_text, noise)
                resolved = controller._resolve_story_ocr_result(raw)
                self.assertEqual(
                    (resolved.character, resolved.text), ("Narrator", text)
                )
                self.assertEqual(resolved.speaker_confidence, raw.speaker_confidence)
                self.assertEqual(resolved.confidence, raw.dialogue_confidence)
                self.assertIsNone(controller.chapter_voice_preloader.current_match)

    def test_complete_ocr_typo_after_early_audio_continues_once(self):
        # 34's substitution was documented live; 97's was reproduced from a
        # saved screenshot derivative. Neither is a saved full live OCR trace.
        for canonical, old, new in (
            (
                "I'm afraid I won't be handling your make-up assignment. You'll "
                "have to ask those old gentlemen in the office when applications open.",
                "afraid I",
                "afraid J]",
            ),
            (
                "A plane? I-I'm sure I could catch a train there if they don't "
                "mind me arriving a little—Never mind! I owe you!",
                "sure I",
                "sure J",
            ),
        ):
            with self.subTest(canonical=canonical):
                line = story_line(canonical, "Cornerstone")
                controller = self.controller((line,))
                preloader = controller.chapter_voice_preloader
                harness = AutoAdvanceFakeFrameHarness(
                    tracker_options={
                        "complete_dialogue_only": True,
                        "early_dialogue_resolver": lambda speaker, text: (
                            resolved.text
                            if (
                                resolved := preloader.resolve_unique_prefix(
                                    speaker, text
                                )
                            )
                            else None
                        ),
                    },
                )

                def recognize(frame):
                    result = controller._resolve_story_ocr_result(
                        observed(frame["text"], frame["character"])
                    )
                    return result.character, result.text

                harness.reader.recognize_frame = recognize
                with patch(
                    "vntts.live.Timer", side_effect=harness.timer_scheduler.create
                ):
                    harness.start()
                    try:
                        prefix = canonical[:30]
                        for _ in range(2):
                            harness.push(
                                line.speaker,
                                prefix,
                                background="prefix",
                                fingerprint="prefix",
                            )
                        self.assertEqual(len(harness.speech_executor.jobs), 1)
                        self.assertEqual(
                            harness.speech_executor.jobs[0][2][0].text, canonical
                        )
                        for _ in range(3):
                            harness.clock.advance(0.2)
                            harness.push(
                                line.speaker,
                                canonical.replace(old, new),
                                background="full",
                                fingerprint="full",
                                complete=True,
                                expected=(line.speaker, canonical),
                            )
                        self.assertEqual(harness.reader.dialog_ready_generation, 1)
                        self.assertEqual(harness.advance_calls, [])
                        harness.finish_playback()
                        harness.timer_scheduler.fire_next("_run_auto_advance")
                        self.assertEqual(harness.advance_calls, [1])
                        self.assertEqual(len(harness.played), 1)
                        self.assertEqual(harness.speech_executor.jobs, [])
                        self.assertEqual(harness.reader.active_generation, 1)
                        for _ in range(2):
                            harness.push(
                                "Duvalle",
                                "The next dialogue is now visible.",
                                background="next",
                                fingerprint="next",
                            )
                        self.assertEqual(
                            harness.states, [("dispatched", 1, 1), ("confirmed", 1, 1)]
                        )
                        self.assertEqual(harness.advance_calls, [1])
                        self.assertEqual(harness.errors, [])
                    finally:
                        harness.stop()

    def test_cursor_candidates_keep_matches_inside_the_selected_path(self):
        for text, ocr_text in (
            ("A repeated complete phrase.", "A repeated complete phrase."),
            ("Remember Nowa Miedź?", "Remember Nowa Miedz?"),
            ("Ouch!", "Oucb!"),
        ):
            with self.subTest(text=text):
                first = story_line(text, "Correct speaker")
                other = story_line(ocr_text, "Wrong branch", "line-2")
                controller = self.controller((first, other))
                controller.settings = controller.settings.updated(
                    live_sequence_mode="audio-manual"
                )
                event = SimpleNamespace(
                    line_id=first.line_id, is_speech=True, chapter=first.chapter
                )
                forbidden = SimpleNamespace(
                    line_id=other.line_id, is_speech=True, chapter=other.chapter
                )
                controller.live_sequence_plan = SimpleNamespace(
                    events={"first": event, "forbidden": forbidden}
                )
                controller.story_cursor = SimpleNamespace(
                    state=StoryCursorState.LOCKED,
                    current_event=event,
                    bounded_visible_successors=Mock(return_value=()),
                )
                raw = observed(ocr_text, "Wrong branch")
                resolved = controller._resolve_story_ocr_result(raw)
                self.assertEqual(
                    (resolved.character, resolved.text), (first.speaker, text)
                )
                self.assertIsNone(controller.chapter_voice_preloader.current_match)
                self.assertEqual(controller.story_cursor.state, StoryCursorState.LOCKED)

    def test_current_chapter_is_preserved_and_manual_diagnostics_get_the_same_resolution(
        self,
    ):
        for text, ocr_text in (
            ("A complete prepared line.", "A complete prepared line."),
            ("Remember Nowa Miedź?", "Remember Nowa Miedz?"),
            ("Ouch!", "Oucb!"),
        ):
            with self.subTest(text=text):
                line = story_line(text, "Actual speaker")
                other = story_line(
                    ocr_text, "Other chapter speaker", "line-2", "chapter-2"
                )
                controller = self.controller((line, other))
                previous = ChapterMatch(line.chapter, 0, 1.0)
                controller.chapter_voice_preloader.current_match = previous
                snapshots = []
                raw = observed(ocr_text)
                with (
                    patch(
                        "vntts.dialog_capture.capture_dialog",
                        return_value=(Image.new("RGB", (300, 100)), None),
                    ),
                    patch(
                        "vntts.dialog_capture.recognize_dialog_image_result",
                        return_value=raw,
                    ),
                ):
                    _, _, result = analyze_dialog_snapshot(
                        "captures",
                        result_resolver=controller._resolve_story_ocr_result,
                        diagnostic_handler=snapshots.append,
                    )
                self.assertEqual((result.character, result.text), (line.speaker, text))
                self.assertEqual(snapshots[0].speaker_confidence, 26)
                self.assertTrue(snapshots[0].speaker_note)
                self.assertIs(
                    controller.chapter_voice_preloader.current_match, previous
                )


if __name__ == "__main__":
    unittest.main()

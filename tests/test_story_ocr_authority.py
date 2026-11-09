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
    read_dialog,
)
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioRoute,
    PreparedGeneratedAudio,
)
from vntts.live import RecognizedDialog
from vntts.live_sequence import StoryCursorState
from vntts.live_snapshot import read_live_snapshot
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

    def anchor(self, controller, current, successors=(), state=StoryCursorState.LOCKED):
        controller.settings = controller.settings.updated(
            live_sequence_mode="audio-manual"
        )
        events = tuple(
            SimpleNamespace(line_id=line.line_id, is_speech=True, chapter=line.chapter)
            for line in (current, *successors)
        )
        controller.live_sequence_plan = SimpleNamespace(events=dict(enumerate(events)))
        controller.story_cursor = SimpleNamespace(
            state=state,
            current_event=events[0],
            bounded_visible_successors=Mock(return_value=events[1:]),
        )

    def test_bounded_exact_source_admits_low_confidence_without_inflating_it(self):
        previous = story_line("The previous complete line.", line_id="previous")
        line = story_line("Eurus ... Eurus ...", "Elma")
        for state in (
            StoryCursorState.LOCKED,
            StoryCursorState.PLAYING,
            StoryCursorState.WAITING_TRANSITION,
        ):
            for successor in (False, True):
                with self.subTest(state=state, successor=successor):
                    controller = self.controller((previous, line))
                    self.anchor(
                        controller,
                        previous if successor else line,
                        (line,) if successor else (),
                        state,
                    )
                    raw = observed(
                        "Ｅｕｒｕｓ  ...\nEurus ...", dialogue_confidence=52.5
                    )
                    resolution = {}
                    resolved = controller._resolve_story_ocr_result(
                        raw, resolution=resolution
                    )
                    self.assertEqual(
                        (resolved.character, resolved.text), (line.speaker, line.text)
                    )
                    self.assertEqual(resolved.confidence, 52.5)
                    self.assertEqual(resolved.dialogue_confidence, 52.5)
                    self.assertEqual(
                        resolved.speaker_confidence, raw.speaker_confidence
                    )
                    self.assertFalse(resolved.is_confident())
                    self.assertTrue(resolved.is_readable())
                    self.assertTrue(resolved.source_text_confirmed)
                    self.assertEqual(resolution["match_result"], "expected-text-only")
                    self.assertIsNone(controller.chapter_voice_preloader.current_match)
                    with patch(
                        "vntts.dialog_capture.recognize_dialog_image_result",
                        return_value=raw,
                    ):
                        actual = controller._recognize_live_frame(
                            CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
                        )
                    self.assertIsInstance(actual, RecognizedDialog)
                    self.assertTrue(actual.exact_match)
                    self.assert_generated_route(controller, line, actual)
                    snapshot = controller._publish_diagnostic.call_args.args[0]
                    self.assertEqual(snapshot.confidence, 52.5)
                    self.assertEqual(snapshot.dialogue_confidence, 52.5)
                    controller._ocr_uncertain.assert_not_called()

    def test_low_confidence_source_authority_does_not_admit_other_matches(self):
        line = story_line("Remember Nowa Miedź?", "Elma")
        previous = story_line("An unrelated previous line.", line_id="previous")
        cases = (
            ((line,), line, (), "Remember Nowa Miedź", StoryCursorState.LOCKED),
            ((line,), line, (), "Remember Nowa Miedz?", StoryCursorState.LOCKED),
            ((line,), line, (), "Rememher Nowa Miedź?", StoryCursorState.LOCKED),
            ((line,), line, (), "Remember Nowa Miedź? dust", StoryCursorState.LOCKED),
            ((line,), line, (), "Remember Nowa", StoryCursorState.LOCKED),
            (
                (line, story_line(line.text, "Other", "duplicate")),
                line,
                (story_line(line.text, "Other", "duplicate"),),
                line.text,
                StoryCursorState.LOCKED,
            ),
            (
                (line, story_line(line.text + " More words.", line_id="longer")),
                line,
                (story_line(line.text + " More words.", line_id="longer"),),
                line.text,
                StoryCursorState.LOCKED,
            ),
            ((previous, line), previous, (), line.text, StoryCursorState.LOCKED),
            (
                (replace(line, line_id=None),),
                line,
                (),
                line.text,
                StoryCursorState.LOCKED,
            ),
            (
                (replace(line, text_sha256=None),),
                line,
                (),
                line.text,
                StoryCursorState.LOCKED,
            ),
            *(
                ((line,), line, (), line.text, state)
                for state in (
                    StoryCursorState.UNSYNCHRONIZED,
                    StoryCursorState.ANCHORING,
                    StoryCursorState.DESYNCHRONIZED,
                )
            ),
        )
        for rows, current, successors, text, state in cases:
            with self.subTest(rows=rows, text=text, state=state):
                controller = self.controller(rows)
                self.anchor(controller, current, successors, state)
                raw = observed(text, dialogue_confidence=52.5)
                self.assertIs(controller._resolve_story_ocr_result(raw), raw)
                self.assertFalse(raw.source_text_confirmed)
                with patch(
                    "vntts.dialog_capture.recognize_dialog_image_result",
                    return_value=raw,
                ):
                    with self.assertRaises(OCRUncertainError):
                        controller._recognize_live_frame(
                            CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
                        )

    def test_low_confidence_complete_source_advances_once_after_playback(self):
        line = story_line("Eurus ... Eurus ...", "Elma")
        following = story_line("The next complete line.", "Elma", "next")
        controller = self.controller((line, following))
        self.anchor(controller, line, (following,))
        harness = AutoAdvanceFakeFrameHarness(
            tracker_options={"complete_dialogue_only": True}
        )

        def recognize(frame):
            with patch(
                "vntts.dialog_capture.recognize_dialog_image_result",
                return_value=observed(frame["text"], frame["character"], 52.5),
            ):
                return controller._recognize_live_frame(
                    CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
                )

        harness.reader.recognize_frame = recognize
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            self.addCleanup(harness.stop)
            for _ in range(3):
                harness.clock.advance(0.2)
                harness.push(
                    line.speaker, line.text, background="current", complete=True
                )
            harness.finish_playback()
            harness.timer_scheduler.fire_next("_run_auto_advance")
            harness.push(line.speaker, line.text, background="recheck", complete=True)
            self.assertEqual(harness.advance_calls, [1])
            self.assertEqual(len(harness.played), 1)
            self.assertEqual(harness.speech_executor.jobs, [])
            for _ in range(2):
                harness.push(following.speaker, following.text, background="next")
            self.assertEqual(
                harness.states, [("dispatched", 1, 1), ("confirmed", 1, 1)]
            )
            self.assertEqual(harness.advance_calls, [1])
            self.assertEqual(harness.errors, [])

    def test_all_reading_consumers_use_source_authority_and_keep_diagnostics(self):
        line = story_line("Eurus ... Eurus ...", "Elma")
        controller = self.controller((line,))
        self.anchor(controller, line)
        raw = observed(line.text, dialogue_confidence=52.5)
        image = Image.new("RGB", (300, 100))
        speech = Mock()
        snapshots = []
        with (
            patch("vntts.dialog_capture.capture_dialog", return_value=(image, None)),
            patch(
                "vntts.dialog_capture.recognize_dialog_image_result", return_value=raw
            ),
        ):
            kwargs = dict(
                result_resolver=controller._resolve_story_ocr_result,
                diagnostic_handler=snapshots.append,
            )
            read_dialog(controller.voice_router, ".", speech_handler=speech, **kwargs)
            speech.assert_called_once_with(line.speaker, line.text)
            self.assertEqual(
                read_live_snapshot(".", **kwargs), (line.speaker, line.text)
            )
            with patch.object(
                type(controller.diagnostics), "_test_audio_route"
            ) as route:
                self.assertEqual(
                    controller.diagnostics.test_current_dialog(prepare_live=Mock()),
                    (line.speaker, line.text),
                )
                self.assertEqual(route.call_args.args[:2], (line.speaker, line.text))
        self.assertEqual([item.confidence for item in snapshots], [52.5, 52.5])
        self.assertEqual(
            controller._publish_diagnostic.call_args.args[0].confidence, 52.5
        )

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
            "generated-audio-entry-ready",
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

    def test_partial_name_loss_cannot_move_scope_before_complete_generated_line(self):
        previous = replace(
            story_line(
                "The previous complete dialogue.",
                "Cornerstone",
                "reverse1999:314001:42",
                "314001",
            ),
            sequence=42,
        )
        line = replace(
            story_line(
                "Thank you ever so much, madam.",
                "Duvalle",
                "reverse1999:314001:43",
                "314001",
            ),
            sequence=43,
        )
        foreign = story_line(
            "Lore picks up her uniform and signs a thank-you.",
            "Narrator",
            "reverse1999:302002:15",
            "302002",
        )
        controller = self.controller((foreign, previous, line))
        preloader = controller.chapter_voice_preloader
        self.assertIs(preloader.select_line_id(previous.line_id), previous)
        anchor = preloader.current_match

        for prefix in ("Thank yo", "Thank you eve", "Thank you ever so much, m"):
            with self.subTest(prefix=prefix):
                raw = replace(
                    observed(prefix, "Narrator", dialogue_confidence=96), confidence=96
                )
                with patch(
                    "vntts.dialog_capture.recognize_dialog_image_result",
                    return_value=raw,
                ):
                    actual = controller._recognize_live_frame(
                        CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
                    )
                self.assertIsInstance(actual, tuple)
                self.assertTrue(controller._dialog_observed(*actual))
                if prefix in {"Thank yo", "Thank you eve"}:
                    self.assertIs(preloader.current_match, anchor)
                self.assertEqual(preloader.current_match.chapter, line.chapter)

        raw = replace(
            observed(line.text, "Narrator", dialogue_confidence=96), confidence=96
        )
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=raw
        ):
            actual = controller._recognize_live_frame(
                CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
            )

        self.assertEqual(
            actual,
            RecognizedDialog(
                line.speaker,
                line.text,
                line.line_id,
                exact_match=True,
                allow_unheard_replacement=False,
            ),
        )
        self.assert_generated_route(controller, line, actual)

    def test_full_owned_successor_recovers_unreadable_speaker(self):
        line = story_line("Thank you ever so much, madam.", "Duvalle")
        for text in (line.text, line.text.upper()):
            with self.subTest(text=text):
                controller = self.controller((line,))
                controller._expected_sequence_prefix_line_locked = Mock(
                    return_value=line
                )
                raw = replace(
                    observed(text, "Narrator", dialogue_confidence=96),
                    confidence=0,
                    speaker_confidence=0,
                    speaker_note="Visible name could not be read",
                )
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
                        line.speaker,
                        line.text,
                        line.line_id,
                        exact_match=True,
                        allow_unheard_replacement=False,
                    ),
                )
                self.assert_generated_route(controller, line, actual)

    def test_full_owned_successor_preserves_complete_match_guards(self):
        line = story_line("Thank you ever so much, madam.", "Duvalle")
        raw = observed(line.text, "Narrator")
        for rows, result in (
            ((line, story_line(line.text, "Other", "line-2")), raw),
            ((line, story_line(line.text + " More words.", "Other", "line-2")), raw),
            ((line,), replace(raw, dialogue_confidence=35)),
        ):
            with self.subTest(rows=rows, confidence=result.dialogue_confidence):
                controller = self.controller(rows)
                controller._expected_sequence_prefix_line_locked = Mock(
                    return_value=line
                )
                self.assertIs(controller._resolve_story_ocr_result(result), result)

    def test_owned_symbol_successor_uses_complete_source_match(self):
        line = story_line("...", "Duvalle")
        controller = self.controller((line,))
        controller._expected_sequence_prefix_line_locked = Mock(return_value=line)
        raw = replace(
            observed(line.text, "Narrator", dialogue_confidence=96),
            confidence=0,
            speaker_confidence=0,
        )
        resolution = {}
        resolved = controller._resolve_story_ocr_result(raw, resolution=resolution)
        self.assertEqual((resolved.character, resolved.text), (line.speaker, line.text))
        self.assertEqual(resolved.confidence, 96)
        self.assertEqual(resolution["match_result"], "expected-text-only")

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
            ("Remember Nowa Miedź?", "Remember Nowa Mied:z"),
            ("Remember Nowa Miedź?", "Remember Nowa Mied:z?"),
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

    def test_recorded_split_diacritic_reaches_ready_audio_without_live_synthesis(self):
        line = story_line("Remember Nowa Miedź?", "Cornerstone")
        controller = self.controller((line,))
        raw = observed("Remember Nowa Mied:z")
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=raw
        ):
            actual = controller._recognize_live_frame(
                CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
            )
        self.assertIsInstance(actual, RecognizedDialog)
        self.assertEqual(
            (actual.character, actual.text, actual.line_id),
            (line.speaker, line.text, line.line_id),
        )
        self.assert_generated_route(controller, line, actual)

    def test_full_substitutions_at_either_end_reach_the_same_generated_recording(self):
        line = story_line("The lantern glows beside the old bridge.", "Cornerstone")
        for text in (
            "Tbe lantern glows beside the old bridge.",
            "The lantern glows beside the old bridgc.",
        ):
            with self.subTest(text=text):
                controller = self.controller((line,))
                raw = observed(text)
                resolved = controller._resolve_story_ocr_result(raw)
                self.assertEqual(
                    (resolved.character, resolved.text), (line.speaker, line.text)
                )
                self.assert_generated_route(
                    controller, line, (resolved.character, resolved.text)
                )
                initial, _ = controller._resolve_initial_live_sequence_line(
                    raw.character, raw.text
                )
                self.assertIs(initial, line)
                bounded, _ = controller.chapter_voice_preloader.resolve_bounded_among(
                    raw.character, raw.text, (line.line_id,)
                )
                self.assertIs(bounded, line)

    def test_terminal_truncation_cannot_canonicalize_or_lock_startup_scope(self):
        line = story_line("The lantern glows beside the old bridge.", "Cornerstone")
        for text in (
            "The lantern glows beside the old bridg",
            "The lantern glows beside the old",
        ):
            with self.subTest(text=text):
                controller = self.controller((line,))
                raw = observed(text, line.speaker)
                self.assertIs(controller._resolve_story_ocr_result(raw), raw)
                initial, _ = controller._resolve_initial_live_sequence_line(
                    raw.character, raw.text
                )
                self.assertIsNone(initial)
                self.assertIsNone(controller.chapter_voice_preloader.current_match)

    def test_ocr_token_split_before_unchanged_number_keeps_generated_identity(self):
        for text, ocr_text in (
            ("The lantern needs 12 coins.", "The lan:tern needs 12 coins."),
            ("The lantern needs +12.5% fuel.", "The lan:tern needs +12.5% fuel."),
        ):
            with self.subTest(text=text):
                line = story_line(text, "Cornerstone")
                controller = self.controller((line,))
                raw = observed(ocr_text)
                resolved = controller._resolve_story_ocr_result(raw)
                self.assertEqual(
                    (resolved.character, resolved.text), (line.speaker, line.text)
                )
                self.assert_generated_route(
                    controller, line, (resolved.character, resolved.text)
                )
                initial, _ = controller._resolve_initial_live_sequence_line(
                    raw.character, raw.text
                )
                self.assertIs(initial, line)

    def test_changed_numbers_signs_or_negation_never_acquire_story_authority(self):
        for canonical, text in (
            ("The lantern needs 12 coins.", "The lantern needs 13 coins."),
            ("The lantern needs -12 coins.", "The lantern needs 12 coins."),
            ("The lantern needs -12 coins.", "The lantern needs +12 coins."),
            ("The lantern needs 1.2 coins.", "The lantern needs 12 coins."),
            ("The lantern needs 12% fuel.", "The lantern needs 12 fuel."),
            ("The lantern needs one coin.", "The lantern needs two coin."),
            ("The lantern is not ready.", "The lantern is now ready."),
            ("The lantern cannot glow.", "The lantern can glow."),
            ("The lantern isn't ready.", "The lantern is ready."),
        ):
            with self.subTest(canonical=canonical, text=text):
                line = story_line(canonical, "Cornerstone")
                controller = self.controller((line,))
                raw = observed(text, line.speaker)
                self.assertIs(controller._resolve_story_ocr_result(raw), raw)
                initial, _ = controller._resolve_initial_live_sequence_line(
                    raw.character, raw.text
                )
                self.assertIsNone(initial)
                bounded, _ = controller.chapter_voice_preloader.resolve_bounded_among(
                    raw.character, raw.text, (line.line_id,)
                )
                self.assertIsNone(bounded)
                self.assertIsNone(controller.chapter_voice_preloader.current_match)

    def test_startup_cannot_select_an_exact_line_outside_the_sequence_plan(self):
        allowed = story_line("The lantern needs 12 coins.", "Cornerstone")
        forbidden = story_line(
            "The lantern needs 13 coins.", "Cornerstone", "forbidden", "chapter-2"
        )
        controller = self.controller((allowed, forbidden))
        controller.settings = controller.settings.updated(
            live_sequence_mode="audio-manual"
        )
        controller.live_sequence_plan = SimpleNamespace(
            events={"allowed": SimpleNamespace(line_id=allowed.line_id, is_speech=True)}
        )
        line, _ = controller._resolve_initial_live_sequence_line(
            forbidden.speaker, forbidden.text
        )
        self.assertIsNone(line)
        self.assertIsNone(controller.chapter_voice_preloader.current_match)

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
                        "early_dialogue_resolver": preloader.resolve_unique_prefix,
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
                        self.assertEqual(harness.advance_calls, [])
                        harness.push(
                            line.speaker,
                            canonical.replace(old, new),
                            background="advance-recheck",
                            fingerprint="full",
                            complete=True,
                            expected=(line.speaker, canonical),
                        )
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
            ("Remember Nowa Miedź?", "Remember Nowa Mied:z"),
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

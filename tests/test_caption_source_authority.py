import unittest
from hashlib import sha256
from threading import Event, Lock
from unittest.mock import Mock, patch

from PIL import Image

from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController
from vntts.dialog_capture import CapturedDialogFrame, recognize_live_frame
from vntts.live import LiveDialogReader
from vntts.ocr import DialogRegion, OCRPreviewCancelled, OCRResult
from vntts.ocr_corrections import OCRCorrectionDictionary
from vntts.settings import AppSettings
from vntts.text_detection import ScreenTextDetection, TextLine


def row(line_id, text, *, speaker="Hedone", sequence=0, chapter="chapter"):
    return ChapterDialogue(
        line_id, chapter, sequence, speaker, text, sha256(text.encode()).hexdigest()
    )


class CaptionSourceAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.dialogue = row("lower", "The ordinary dialogue is still visible.")
        self.caption = row(
            "caption", "A prepared story caption appears here.", sequence=1
        )
        self.preloader = ChapterVoicePreloader((self.dialogue, self.caption))
        self.preloader.select_line_id(self.dialogue.line_id)
        self.controller = AppController(
            AppSettings(discover_text_layouts=True),
            chapter_voice_preloader=self.preloader,
        )
        self.image = Image.new("RGB", (400, 300), "navy")
        self.region = DialogRegion(0, 0.7, 1, 0.3)
        self.story = DialogRegion(0, 0, 1, 1)
        self.frame = CapturedDialogFrame(
            self.region.crop(self.image),
            0,
            canonical_image=self.image,
            capture_region=self.region,
        )
        self.worker = Mock()
        self.controller.caption_discovery = self.worker
        self.cancel = Event()

    def line(self, text, *, top=0.3, confidence=95):
        return TextLine(DialogRegion(0.1, top, 0.8, 0.04), text, confidence)

    def select(self, *lines):
        self.worker.detect.return_value = ScreenTextDetection(lines)
        with patch.object(self.controller, "_story_region", return_value=self.story):
            return self.controller._select_text_layout(
                self.frame, cancel_event=self.cancel
            )

    def test_unrelated_artwork_keeps_dialogue_even_when_noisy_or_ambiguous(self):
        for lines in (
            (self.line("Ears"),),
            (self.line("Ears", confidence=20),),
            (self.line("Ears"), self.line("5 YEARS ESS", top=0.5)),
        ):
            with self.subTest(lines=lines):
                selected = self.select(*lines)
                self.assertEqual(selected.layout_kind, "dialogue")
                self.assertIs(selected.image, self.frame.image)
                self.assertTrue(selected.navigation_allowed)
                result = OCRResult(
                    self.dialogue.speaker, self.dialogue.text, 95, "dialogue", 1
                )
                with patch(
                    "vntts.dialog_capture.recognize_screenshot_result",
                    return_value=result,
                ):
                    self.assertEqual(
                        recognize_live_frame(selected),
                        (self.dialogue.speaker, self.dialogue.text),
                    )

    def test_prepared_caption_is_eligible_when_the_dialogue_body_is_empty(self):
        for top in (0.05, 0.5):
            with self.subTest(top=top):
                selected = self.select(
                    self.line(self.caption.text, top=top),
                    self.line("Ears", top=0.2, confidence=20),
                )
                self.assertEqual(selected.layout_kind, "caption")
                self.assertEqual(selected.recognized_result.text, self.caption.text)
                self.assertFalse(selected.navigation_allowed)

    def test_detected_dialogue_body_wins_with_or_without_a_prepared_source(self):
        for preloader in (self.preloader, ChapterVoicePreloader()):
            with self.subTest(has_source=preloader.has_dialogue):
                self.controller.chapter_voice_preloader = preloader
                self.worker.detect.return_value = ScreenTextDetection((), True)
                with (
                    patch.object(
                        self.controller, "_story_region", return_value=self.story
                    ),
                    patch.object(
                        self.controller, "_story_matching_snapshot_locked"
                    ) as matching,
                ):
                    selected = self.controller._select_text_layout(
                        self.frame, cancel_event=self.cancel
                    )
                self.assertEqual(selected.layout_kind, "dialogue")
                self.assertIs(selected.image, self.frame.image)
                self.assertEqual(selected.ocr_regions, self.frame.ocr_regions)
                self.assertEqual(selected.provenance, self.frame.provenance)
                self.assertEqual(
                    selected.navigation_allowed, self.frame.navigation_allowed
                )
                matching.assert_not_called()
                result = OCRResult(
                    self.dialogue.speaker, "Raw lower dialogue.", 95, "dialogue", 1
                )
                with patch(
                    "vntts.dialog_capture.recognize_screenshot_result",
                    return_value=result,
                ):
                    self.assertEqual(
                        recognize_live_frame(selected), (result.character, result.text)
                    )

    def test_raw_lower_text_beats_even_a_known_complete_caption(self):
        selected = self.select(
            self.line("Raw lower dialogue", top=0.8), self.line(self.caption.text)
        )
        self.assertEqual(selected.layout_kind, "dialogue")
        self.assertIs(selected.image, self.frame.image)

    def test_source_prefix_and_ambiguous_source_text_hold_dialogue(self):
        duplicate = row(
            "duplicate", self.caption.text, speaker="Another", chapter="elsewhere"
        )
        for rows, observed in (
            ((self.caption,), "A prepared story caption"),
            ((self.caption, duplicate), self.caption.text),
        ):
            with self.subTest(observed=observed):
                self.controller.chapter_voice_preloader = ChapterVoicePreloader(rows)
                selected = self.select(self.line(observed), self.line("Ears", top=0.5))
                self.assertEqual(selected.layout_kind, "pending")
                self.assertIsNone(selected.recognized_result)
                self.assertFalse(selected.navigation_allowed)

    def test_known_current_occurrence_disambiguates_duplicate_without_advancing(self):
        duplicate = row(
            "duplicate", self.caption.text, speaker="Another", chapter="elsewhere"
        )
        self.controller.chapter_voice_preloader = ChapterVoicePreloader(
            (self.dialogue, self.caption, duplicate)
        )
        self.controller.chapter_voice_preloader.select_line_id(self.dialogue.line_id)
        current = self.controller.chapter_voice_preloader.current_match
        selected = self.select(self.line(self.caption.text))
        self.assertEqual(selected.layout_kind, "caption")
        self.assertEqual(self.controller.chapter_voice_preloader.current_match, current)

    def test_source_match_does_not_bypass_confidence_or_layout_ambiguity(self):
        for lines in (
            (self.line(self.caption.text, confidence=20),),
            (self.line(self.caption.text), self.line(self.dialogue.text, top=0.5)),
        ):
            with self.subTest(lines=lines):
                self.assertEqual(self.select(*lines).layout_kind, "pending")

    def test_short_and_symbol_story_text_do_not_depend_on_a_length_filter(self):
        for text in ("No!", "?", "...", "Elma"):
            with self.subTest(text=text):
                self.controller.chapter_voice_preloader = ChapterVoicePreloader(
                    (row("short", text),)
                )
                selected = self.select(self.line(text))
                self.assertEqual(selected.layout_kind, "caption")
                self.assertEqual(selected.recognized_result.text, text)

    def test_saved_correction_participates_without_rewriting_raw_caption_evidence(self):
        self.controller.correction_dictionary = OCRCorrectionDictionary(
            {"unrecognized artwork": self.caption.text}
        )
        selected = self.select(self.line("unrecognized artwork"))
        self.assertEqual(selected.layout_kind, "caption")
        self.assertEqual(selected.recognized_result.text, "unrecognized artwork")

    def test_preview_and_live_use_source_authority_without_mutating_matching_state(
        self,
    ):
        self.preloader.last_resolution_diagnostics = {"match_result": "unchanged"}
        current = self.preloader.current_match
        for text, profile in (
            ("Ears", "story-area-empty"),
            (self.caption.text, "caption-ppocr"),
            ("A prepared story caption", "layout-pending"),
        ):
            with self.subTest(text=text):
                self.worker.detect.return_value = ScreenTextDetection(
                    (self.line(text),)
                )
                result = self.controller.preview_story_area(
                    self.image, self.story, self.region, self.cancel
                )
                self.assertEqual(result.profile, profile)
                self.assertEqual(self.preloader.current_match, current)
                self.assertEqual(
                    self.preloader.last_resolution_diagnostics,
                    {"match_result": "unchanged"},
                )

    def test_source_free_story_caption_still_uses_confirmed_scope(self):
        self.controller.chapter_voice_preloader = ChapterVoicePreloader()
        self.assertEqual(self.select(self.line("Ears")).layout_kind, "caption")
        with patch.object(self.controller, "_story_region", return_value=None):
            self.assertIs(self.controller._select_text_layout(self.frame), self.frame)

    def test_switching_to_source_free_game_does_not_reuse_discovered_source_authority(
        self,
    ):
        self.controller.automatic_prepared_reading = True
        self.controller.discovered_story_preloader = self.preloader
        with patch.object(self.controller, "_create_capture_target", return_value=None):
            self.controller.runtime_lifecycle._refresh_runtime_settings(
                AppSettings(
                    discover_text_layouts=True, game_window_title="Another game"
                )
            )
        self.assertFalse(self.controller.chapter_voice_preloader.has_dialogue)
        self.assertEqual(
            self.select(self.line("New game's caption.")).layout_kind, "caption"
        )

    def test_reader_evidence_is_read_without_holding_controller_matching_lock(self):
        lock = Lock()
        self.controller.story_cursor_lock = lock
        reader = Mock(
            spec=LiveDialogReader,
            stop_event=Event(),
            run_id="reader",
            frame_route_epoch=0,
        )
        self.controller.live_reader = reader

        def observed():
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()
            return None

        reader.fully_observed_dialog.side_effect = observed
        self.assertEqual(
            self.select(self.line(self.caption.text)).layout_kind, "caption"
        )
        reader.fully_observed_dialog.assert_called_once()

    def test_cancelled_or_replaced_source_cannot_publish_selected_caption(self):
        original = ChapterVoicePreloader.resolve_observation_among

        for invalidate in (
            self.cancel.set,
            lambda: setattr(
                self.controller, "chapter_voice_preloader", ChapterVoicePreloader()
            ),
        ):
            with self.subTest(invalidate=invalidate):
                self.cancel.clear()
                self.controller.chapter_voice_preloader = self.preloader

                def resolve(preloader, *args, **kwargs):
                    result = original(preloader, *args, **kwargs)
                    invalidate()
                    return result

                with (
                    patch.object(
                        ChapterVoicePreloader,
                        "resolve_observation_among",
                        new=resolve,
                    ),
                    self.assertRaises(OCRPreviewCancelled),
                ):
                    self.select(self.line(self.caption.text))


if __name__ == "__main__":
    unittest.main()

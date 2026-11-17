import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import Mock, patch

from PIL import Image

from tests.native_capture_fixtures import native_frame
from vntts.caption_layout import select_caption
from vntts.controller import AppController
from vntts.dialog_capture import (
    CapturedDialogFrame,
    analyze_dialog_snapshot,
    capture_live_frame,
    dialog_completion_cue_visible,
    dialog_glyphs_visible,
    fingerprint_dialog_frame,
    recognize_live_frame,
)
from vntts.live_replay import _recognize_replay_frame, load_live_replay_corpus
from vntts.live_replay_capture import LiveReplayCaptureSession
from vntts.ocr import DialogRegion, OCRRegions
from vntts.profiles import GameProfile
from vntts.settings import AppSettings
from vntts.text_detection import ScreenTextDetection, TextBlock, TextLine
from vntts.window_capture import DisplayCaptureTarget


def detection(*blocks):
    return ScreenTextDetection(tuple(line for block in blocks for line in block.lines))


class CaptionLayoutTest(unittest.TestCase):
    def frame(self):
        image = Image.new("RGB", (400, 300), "white")
        region = DialogRegion(0, 0.7, 1, 0.3)
        return CapturedDialogFrame(
            region.crop(image), 10, canonical_image=image, capture_region=region
        )

    def block(self, text="A temporary story caption.", top=0.3, confidence=95):
        return TextBlock(
            (TextLine(DialogRegion(0.1, top, 0.8, 0.06), text, confidence),)
        )

    def test_caption_is_text_only_and_has_no_advance_permission(self):
        original = self.frame()
        caption = select_caption(original, detection(self.block()), 60)
        self.assertEqual(caption.layout_kind, "caption")
        self.assertIs(caption.canonical_image, original.canonical_image)
        self.assertIsNone(caption.ocr_regions)
        self.assertEqual(caption.recognized_result.text, "A temporary story caption.")
        self.assertTrue(dialog_glyphs_visible(caption))
        self.assertFalse(dialog_completion_cue_visible(caption))
        self.assertFalse(caption.navigation_allowed)
        self.assertNotEqual(
            fingerprint_dialog_frame(caption), fingerprint_dialog_frame(original)
        )

    def test_discovery_does_not_depend_on_screen_center_or_text_color(self):
        original = self.frame()
        for top in (0.05, 0.5):
            caption = select_caption(original, detection(self.block(top=top)), 60)
            self.assertEqual(caption.layout_kind, "caption")

    def test_visible_bottom_dialogue_takes_priority_over_a_caption(self):
        original = self.frame()
        bottom = self.block("The ordinary dialogue remains here.", top=0.8)
        caption = select_caption(original, detection(bottom, self.block()), 60)
        self.assertIs(caption, original)

    def test_lower_body_wins_before_outside_confidence_ambiguity_or_source_matching(
        self,
    ):
        original = self.frame()
        for text in ("A partial", "...", "?"):
            for confidence in (20, 95):
                for outside in (
                    (self.block("Ears", confidence=10),),
                    (self.block(), self.block("Other artwork", top=0.5)),
                ):
                    with self.subTest(
                        text=text, confidence=confidence, outside=outside
                    ):
                        matcher = Mock(
                            side_effect=AssertionError(
                                "Outside source matching must not run"
                            )
                        )
                        selected = select_caption(
                            original,
                            detection(
                                self.block(text, top=0.8, confidence=confidence),
                                *outside,
                            ),
                            60,
                            story_matcher=matcher,
                        )
                        self.assertIs(selected, original)
                        matcher.assert_not_called()

    def test_body_line_in_a_crossing_group_keeps_the_ordinary_crop(self):
        original = self.frame()
        block = TextBlock(
            (self.block().lines[0], self.block("Dialogue", top=0.8).lines[0])
        )
        self.assertIs(select_caption(original, detection(block), 60), original)

    def test_nameplate_is_not_dialogue_body_and_is_not_added_to_caption(self):
        original = replace(
            self.frame(),
            ocr_regions=OCRRegions(
                DialogRegion(0, 0, 1, 0.25), DialogRegion(0, 0.4, 1, 0.6)
            ),
        )
        name = self.block("Ada", top=0.73, confidence=20)
        outside = self.block(top=0.66)
        caption = select_caption(original, detection(name, outside), 60)
        self.assertEqual(caption.layout_kind, "caption")
        self.assertEqual(caption.recognized_result.text, outside.text)

    def test_body_mapping_uses_canonical_crop_pixels(self):
        image = Image.new("RGB", (1003, 777))
        crop = DialogRegion(0.17, 0.41, 0.67, 0.53)
        regions = OCRRegions(
            DialogRegion(0, 0, 1, 0.2), DialogRegion(0.12, 0.3, 0.73, 0.6)
        )
        frame = CapturedDialogFrame(
            crop.crop(image), 0, regions, canonical_image=image, capture_region=crop
        )
        left, top, right, bottom = crop._pixel_box(*image.size)
        x, y, end_x, end_y = regions.dialogue._pixel_box(right - left, bottom - top)
        self.assertEqual(
            frame.dialogue_text_region._pixel_box(*image.size),
            (left + x, top + y, left + end_x, top + end_y),
        )

    def test_blank_and_lower_dialogue_keep_the_calibrated_reader(self):
        original = self.frame()
        for blocks in ((), (self.block(top=0.8),)):
            self.assertIs(select_caption(original, detection(*blocks), 60), original)

    def test_confirmed_story_text_can_be_a_single_word_or_symbol(self):
        for text in ("No!", "?", "...", "Elma"):
            with self.subTest(text=text):
                selected = select_caption(self.frame(), detection(self.block(text)), 60)
                self.assertEqual(selected.layout_kind, "caption")
                self.assertEqual(selected.recognized_result.text, text)

    def test_unconfirmed_area_never_starts_discovery_or_changes_dialogue(self):
        controller = AppController(AppSettings(discover_text_layouts=True))
        with (
            patch.object(controller, "_story_region", return_value=None),
            patch("vntts.controller.prepare_text_detection_models") as prepare,
        ):
            original = self.frame()
            self.assertIs(controller._select_text_layout(original), original)
            prepare.assert_not_called()

    def test_multiple_blocks_and_uncertain_text_hold_speech_and_keys(self):
        original = self.frame()
        for blocks in (
            (self.block(), self.block("Another independent block.", top=0.5)),
            (self.block(confidence=20),),
        ):
            selected = select_caption(original, detection(*blocks), 60)
            self.assertEqual(selected.layout_kind, "pending")
            self.assertIsNone(selected.recognized_result)
            self.assertFalse(selected.navigation_allowed)
            self.assertFalse(dialog_glyphs_visible(selected))

    def test_caption_reuses_corrections_source_resolution_and_diagnostics(self):
        caption = select_caption(self.frame(), detection(self.block()), 60)
        canonical = replace(
            caption.recognized_result,
            character="Canonical",
            text="Canonical story text.",
        )
        resolver = Mock(return_value=canonical)
        raw, diagnostics = [], []
        with patch("vntts.dialog_capture.recognize_screenshot_result") as ordinary_ocr:
            actual = recognize_live_frame(
                caption,
                result_resolver=resolver,
                raw_result_observer=raw.append,
                diagnostic_handler=diagnostics.append,
            )
        ordinary_ocr.assert_not_called()
        self.assertEqual(actual, ("Canonical", "Canonical story text."))
        self.assertEqual(raw, [caption.recognized_result])
        self.assertEqual(diagnostics[0].preprocessing_profile, "caption-ppocr")

    def test_native_capture_retains_full_content_once_for_both_views(self):
        image = Image.new("RGB", (240, 140), "red")
        image.paste("blue", (20, 20, 220, 120))
        backend = Mock(clock_offset=0.0, clock_uncertainty=0.001)
        backend.capture.return_value = native_frame(
            image, content_rect=(20, 20, 200, 100)
        )
        target = DisplayCaptureTarget()
        with patch("vntts.window_capture.NativeCaptureSession", return_value=backend):
            try:
                frame = capture_live_frame(
                    "unused", target, region=DialogRegion(0, 0.5, 1, 0.5)
                )
            finally:
                target.close()
        self.assertEqual(frame.canonical_image.size, (200, 100))
        self.assertEqual(frame.image.size, (200, 50))
        self.assertEqual(frame.canonical_image.getpixel((0, 0)), (0, 0, 255))
        backend.capture.assert_called_once()

    def test_profile_migration_preserves_fast_path_but_new_profiles_discover(self):
        fresh = GameProfile.from_settings("Fresh game", AppSettings())
        self.assertTrue(fresh.discover_text_layouts)
        self.assertTrue(fresh.apply(AppSettings()).discover_text_layouts)
        legacy = fresh.to_mapping()
        legacy.pop("discover_text_layouts")
        migrated = GameProfile.from_mapping(legacy, source_schema=10)
        self.assertFalse(migrated.discover_text_layouts)
        self.assertFalse(
            AppSettings.from_mapping({"schema_version": 31}).discover_text_layouts
        )
        self.assertTrue(
            AppSettings.from_mapping({"schema_version": 32}).discover_text_layouts
        )

    def test_snapshot_saves_the_selected_image_with_its_actual_crop_provenance(self):
        backend = Mock(clock_offset=0.0, clock_uncertainty=0.001)
        backend.capture.return_value = native_frame(
            Image.new("RGB", (400, 300), "white")
        )
        target = DisplayCaptureTarget()
        block = self.block()
        with (
            TemporaryDirectory() as directory,
            patch("vntts.window_capture.NativeCaptureSession", return_value=backend),
        ):
            try:
                image, output, result = analyze_dialog_snapshot(
                    directory,
                    capture_target=target,
                    save_screenshot=True,
                    region=DialogRegion(0, 0.7, 1, 0.3),
                    frame_selector=lambda frame: select_caption(
                        frame, detection(block), 60
                    ),
                )
                metadata = json.loads(
                    Path(output).with_suffix(".capture.json").read_text()
                )
                self.assertEqual(tuple(metadata["output_dimensions"]), image.size)
                self.assertEqual(
                    metadata["capture_bounds"],
                    block.box.capture_box({"width": 400, "height": 300}),
                )
                with Image.open(output) as saved:
                    self.assertEqual(saved.size, image.size)
                self.assertEqual(result.text, block.text)
            finally:
                target.close()
        backend.capture.assert_called_once()

    def test_live_and_diagnostics_share_one_owned_model_worker(self):
        controller = AppController(AppSettings(discover_text_layouts=True))
        entered, release = Event(), Event()

        def prepare(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return "detector", "recognizer"

        worker = Mock()
        worker.detect.return_value = ScreenTextDetection(self.block().lines)
        errors, results = [], []

        def select():
            try:
                results.append(controller._select_text_layout(self.frame()))
            except Exception as error:
                errors.append(error)

        with (
            patch.object(
                controller, "_story_region", return_value=DialogRegion(0, 0.1, 1, 0.9)
            ),
            patch(
                "vntts.controller.prepare_text_detection_models", side_effect=prepare
            ) as preparation,
            patch(
                "vntts.controller.TextDetectionWorker", return_value=worker
            ) as factory,
        ):
            first, second = Thread(target=select), Thread(target=select)
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            release.set()
            first.join(2)
            second.join(2)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(
                [item.layout_kind for item in results], ["caption", "caption"]
            )
            preparation.assert_called_once()
            factory.assert_called_once()
            controller._close_text_layouts()
            worker.close.assert_called_once()
            self.assertTrue(
                all(
                    call.kwargs["allowed_region"] == DialogRegion(0, 0.1, 1, 0.9)
                    and call.kwargs["covered_region"] == self.frame().capture_region
                    and call.kwargs["dialogue_region"]
                    == self.frame().dialogue_text_region
                    for call in worker.detect.call_args_list
                )
            )

    def test_captured_caption_replay_preserves_layout_and_declares_recorded_ocr(self):
        caption = select_caption(self.frame(), detection(self.block()), 60)
        with TemporaryDirectory() as directory:
            session = LiveReplayCaptureSession(Path(directory) / "capture")
            session.observe(caption, "Narrator", caption.recognized_result.text)
            session.observe(caption, "Narrator", caption.recognized_result.text)
            result = session.finish()
            corpus = load_live_replay_corpus(result.corpus)
            frame = corpus.dialogue[0].frames[0]
            self.assertEqual(frame.layout_kind, "caption")
            self.assertFalse(frame.navigation_allowed)
            self.assertTrue(dialog_glyphs_visible(frame))
            self.assertEqual(
                _recognize_replay_frame(frame),
                ("Narrator", caption.recognized_result.text),
            )
            self.assertEqual(
                corpus.dialogue[0].frame_recognition_sources[0], "declared-observation"
            )


if __name__ == "__main__":
    unittest.main()

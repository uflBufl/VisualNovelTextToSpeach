import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw, ImageOps

from vntts.dialog_capture import (
    CapturedDialogFrame,
    fingerprint_dialog_frame,
    recognize_live_frame,
)
from vntts.ocr import (
    DialogRegion,
    OCRRegions,
    OCRResult,
    default_dialog_region,
    get_ocr_regions,
    recognize_dialog_image_result,
    resolve_ocr_regions,
    speaker_glyphs_visible,
)


class AdaptiveOCRTest(unittest.TestCase):
    @staticmethod
    def frame(lines=1, name="X", text="?", inverted=False):
        background, foreground = ("white", "black") if inverted else ("black", "white")
        image = Image.new("RGB", (420, 180), background)
        draw = ImageDraw.Draw(image)
        divider = 120 - (lines - 1) * 25
        draw.text((18, divider - 25), name, fill=foreground, font_size=18)
        draw.line((10, divider, 405, divider), fill=foreground)
        for row in range(lines):
            draw.text((12, divider + 5 + row * 25), text, fill=foreground, font_size=18)
        return image, divider

    @classmethod
    def wide_scenery_frame(
        cls, name="", fill="white", font_size=18, inverted=False, boundary=False
    ):
        image, divider = cls.frame(name="", text="I")
        draw = ImageDraw.Draw(image)
        draw.rectangle((390, 0, 410, image.height), fill="#555555")
        draw.rectangle((180, divider - 40, 219, divider - 35), fill="#555555")
        # Clip one ink row at the name band's upper boundary, leaving real strokes.
        y = (
            55 - draw.textbbox((0, 0), name, font_size=font_size)[1]
            if boundary
            else divider - 25
        )
        draw.text((180, y), name, fill=fill, font_size=font_size)
        return ImageOps.invert(image) if inverted else image

    def test_follows_one_to_four_lines_and_both_polarities_without_clipping(self):
        for lines in range(1, 5):
            for inverted in (False, True):
                with self.subTest(lines=lines, inverted=inverted):
                    image, divider = self.frame(lines, inverted=inverted)
                    regions = resolve_ocr_regions(image)
                    self.assertIsNotNone(regions)
                    _, name_top, _, name_bottom = regions.speaker._pixel_box(
                        *image.size
                    )
                    _, text_top, _, text_bottom = regions.dialogue._pixel_box(
                        *image.size
                    )
                    self.assertLessEqual(name_top, divider - 18)
                    self.assertLess(name_bottom, divider)
                    self.assertGreater(text_top, divider)
                    self.assertLessEqual(text_top, divider + 5)
                    self.assertEqual(text_bottom, image.height)
                    frame = CapturedDialogFrame(image, 0)
                    self.assertEqual(frame.ocr_regions, regions)
                    self.assertEqual(resolve_ocr_regions(image, regions), regions)
                    self.assertIs(frame.image, image)

    def test_preserves_configured_horizontal_authority_when_divider_moves(self):
        image, divider = self.frame(lines=4, name="???")
        configured = OCRRegions(
            DialogRegion(0.02, 0.5, 0.3, 0.14), DialogRegion(0.02, 0.72, 0.95, 0.28)
        )
        regions = resolve_ocr_regions(image, configured)
        self.assertLess(regions.speaker.top, configured.speaker.top)
        self.assertGreater(regions.dialogue.top * image.height, divider)
        old_box = configured.dialogue._pixel_box(*image.size)
        new_box = regions.dialogue._pixel_box(*image.size)
        self.assertEqual((new_box[0], new_box[2]), (old_box[0], old_box[2]))
        self.assertEqual(new_box[3], old_box[3])

    def test_resolved_regions_are_reused_without_detecting_the_frame_again(self):
        image, _ = self.frame()
        regions = resolve_ocr_regions(image)
        self.assertTrue(regions._resolved)

        with patch("vntts.ocr._dialogue_divider", side_effect=AssertionError):
            self.assertIs(resolve_ocr_regions(image, regions), regions)
            self.assertIs(CapturedDialogFrame(image, 0, regions).ocr_regions, regions)

    def test_fresh_detection_uses_original_configuration_for_longer_names(self):
        configured = OCRRegions(
            DialogRegion(0.02, 0.1, 0.94, 0.2),
            DialogRegion(0.02, 0.72, 0.95, 0.28),
        )
        short_image, _ = self.frame(name="X")
        long_image, _ = self.frame(name="Adar Llwch Gwin")

        short_regions = resolve_ocr_regions(short_image, configured)
        long_regions = resolve_ocr_regions(long_image, configured)

        self.assertFalse(configured._resolved)
        self.assertGreater(
            long_regions.speaker._pixel_box(*long_image.size)[2],
            short_regions.speaker._pixel_box(*short_image.size)[2],
        )
        self.assertEqual(long_regions.speaker, resolve_ocr_regions(long_image).speaker)

    def test_in_memory_preview_becomes_fresh_profile_calibration(self):
        preview_image, _ = self.frame(name="X")
        preview_regions = resolve_ocr_regions(preview_image)
        configured = get_ocr_regions(
            default_dialog_region, default_dialog_region, preview_regions
        )
        long_image, divider = self.frame(lines=3, name="Adar Llwch Gwin")

        regions = resolve_ocr_regions(long_image, configured)

        self.assertTrue(preview_regions._resolved)
        self.assertFalse(configured._resolved)
        self.assertTrue(regions._resolved)
        self.assertNotEqual(regions.dialogue.top, preview_regions.dialogue.top)
        self.assertGreater(regions.dialogue._pixel_box(*long_image.size)[1], divider)

    def test_contiguous_five_row_divider_is_one_rule_in_both_polarities(self):
        for inverted in (False, True):
            with self.subTest(inverted=inverted):
                image, divider = self.frame(name="Cornerstone", text="A complete line.")
                draw = ImageDraw.Draw(image)
                # Three lit rows and their two edges form one five-row ridge.
                for offset, intensity in enumerate((80, 110, 80)):
                    draw.line(
                        (10, divider + offset, 405, divider + offset),
                        fill=(intensity,) * 3,
                    )
                if inverted:
                    image = ImageOps.invert(image)

                regions = resolve_ocr_regions(image)

                self.assertIsNotNone(regions)
                self.assertEqual(
                    regions.dialogue._pixel_box(*image.size)[1], divider + 4
                )
                self.assertLess(regions.speaker._pixel_box(*image.size)[3], divider)
                self.assertEqual(resolve_ocr_regions(image, regions), regions)

    def test_ambiguous_missing_thick_or_outside_divider_retains_calibration(self):
        configured = OCRRegions(
            DialogRegion(0.1, 0.1, 0.3, 0.2), DialogRegion(0.1, 0.4, 0.7, 0.5)
        )
        for variant in ("missing", "two", "nearby", "thick", "wide-ridge", "outside"):
            image = Image.new("RGB", (420, 180), "black")
            draw = ImageDraw.Draw(image)
            if variant == "two":
                for y in (50, 90):
                    draw.line((45, y, 330, y), fill="white")
            elif variant in {"nearby", "wide-ridge"}:
                profile = (
                    (80, 110, 80) if variant == "nearby" else (80, 110, 80, 110, 80)
                )
                for offset, intensity in enumerate(profile):
                    draw.line(
                        (45, 50 + offset, 330, 50 + offset), fill=(intensity,) * 3
                    )
                if variant == "nearby":
                    draw.line((45, 56, 330, 56), fill="white")
            elif variant == "thick":
                draw.rectangle((45, 40, 330, 70), fill="white")
            elif variant == "outside":
                draw.line((0, 60, 35, 60), fill="white")
            with self.subTest(variant=variant):
                regions = resolve_ocr_regions(image, configured)
                self.assertEqual(regions, configured)
                self.assertFalse(regions._resolved)
                self.assertIsNone(resolve_ocr_regions(image))

    def test_name_crop_stops_at_distant_art_but_retains_spaces_within_name(self):
        image, divider = self.frame(name="Adar Llwch Gwin")
        ImageDraw.Draw(image).text(
            (330, divider - 25), "JUNK", fill="white", font_size=18
        )
        regions = resolve_ocr_regions(image)
        box = regions.speaker._pixel_box(*image.size)
        self.assertGreater(box[2], 150)
        self.assertLess(box[2], 300)
        self.assertEqual(resolve_ocr_regions(image, regions), regions)

    def test_empty_anchor_excludes_distant_scenery_without_stealing_dialogue(self):
        image, _ = self.frame(name="", text="I")
        ImageDraw.Draw(image).rectangle((390, 0, 410, image.height), fill="#555555")
        regions = resolve_ocr_regions(image)
        self.assertFalse(speaker_glyphs_visible(regions.speaker.crop(image)))
        self.assertEqual(resolve_ocr_regions(image, regions), regions)
        result = recognize_dialog_image_result(
            image,
            recognize_text=Mock(return_value="I"),
            recognize_data=Mock(
                side_effect=[
                    {"text": ["noise"], "conf": [27]},
                    {"text": ["I"], "conf": [96]},
                ]
            ),
        )
        self.assertEqual((result.character, result.text), ("Narrator", "I"))
        self.assertIsNone(result.speaker_confidence)
        self.assertTrue(result.is_confident())

    def test_wide_unanchored_scenery_does_not_supply_a_narrator_name(self):
        for inverted in (False, True):
            with self.subTest(inverted=inverted):
                image = self.wide_scenery_frame(inverted=inverted)
                frame = CapturedDialogFrame(image, 0)
                regions = frame.ocr_regions
                self.assertFalse(speaker_glyphs_visible(regions.speaker.crop(image)))
                self.assertEqual(resolve_ocr_regions(image, regions), regions)
                self.assertLessEqual(regions.dialogue.top * image.height, 125)
                self.assertEqual(
                    regions.dialogue._pixel_box(*image.size)[3], image.height
                )
                result = recognize_dialog_image_result(
                    image,
                    recognize_text=Mock(return_value="I"),
                    recognize_data=Mock(
                        side_effect=[
                            {"text": ["scenery"], "conf": [27]},
                            {"text": ["I"], "conf": [96]},
                        ]
                    ),
                    ocr_regions=regions,
                )
                self.assertEqual((result.character, result.text), ("Narrator", "I"))
                self.assertIsNone(result.speaker_confidence)
                self.assertEqual(result.confidence, 96)
                self.assertTrue(result.is_confident())

    def test_empty_ocr_of_visible_unanchored_names_uses_narrator(self):
        for name in ("X", "I", "J", "L", "T", "???", "6", "37"):
            for font_size in (10, 18):
                for fill in ("white", "#181818"):
                    for inverted in (False, True):
                        for boundary in (False, True):
                            with self.subTest(
                                name=name,
                                font_size=font_size,
                                fill=fill,
                                inverted=inverted,
                                boundary=boundary,
                            ):
                                image = self.wide_scenery_frame(
                                    name, fill, font_size, inverted, boundary
                                )
                                regions = resolve_ocr_regions(image)
                                self.assertTrue(
                                    speaker_glyphs_visible(regions.speaker.crop(image))
                                )
                                self.assertEqual(
                                    resolve_ocr_regions(image, regions), regions
                                )
                                result = recognize_dialog_image_result(
                                    image,
                                    recognize_text=Mock(return_value="I"),
                                    recognize_data=Mock(
                                        side_effect=[
                                            {"text": [], "conf": []},
                                            {"text": ["I"], "conf": [96]},
                                        ]
                                    ),
                                    ocr_regions=regions,
                                )
                                self.assertEqual(
                                    (result.character, result.text), ("Narrator", "I")
                                )
                                self.assertTrue(result.is_confident())
                                self.assertEqual(result.confidence, 96)
                                self.assertIsNone(result.speaker_confidence)
                                self.assertEqual(result.speaker_note, "")

    def test_wide_unanchored_scenery_does_not_change_an_anchored_name_crop(self):
        image = self.wide_scenery_frame()
        ImageDraw.Draw(image).text((18, 95), "Cornerstone", fill="white", font_size=18)
        clean, _ = self.frame(name="Cornerstone", text="I")
        for inverted in (False, True):
            with self.subTest(inverted=inverted):
                source = ImageOps.invert(image) if inverted else image
                control = ImageOps.invert(clean) if inverted else clean
                regions = resolve_ocr_regions(source)
                self.assertEqual(regions.speaker, resolve_ocr_regions(control).speaker)
                self.assertEqual(resolve_ocr_regions(source, regions), regions)

    def test_unanchored_short_or_uniformly_faint_names_remain_visible(self):
        for name in ("X", "I", "???", "6", "37"):
            for fill in ("white", "#181818"):
                for inverted in (False, True):
                    with self.subTest(name=name, fill=fill, inverted=inverted):
                        image, divider = self.frame(name="", text="I")
                        ImageDraw.Draw(image).text(
                            (180, divider - 25), name, fill=fill, font_size=18
                        )
                        if inverted:
                            image = ImageOps.invert(image)
                        regions = resolve_ocr_regions(image)
                        self.assertTrue(
                            speaker_glyphs_visible(regions.speaker.crop(image))
                        )
                        self.assertEqual(resolve_ocr_regions(image, regions), regions)
                        result = recognize_dialog_image_result(
                            image,
                            recognize_text=Mock(return_value="I"),
                            recognize_data=Mock(
                                side_effect=[
                                    {"text": [], "conf": []},
                                    {"text": ["I"], "conf": [96]},
                                ]
                            ),
                        )
                        self.assertEqual(
                            (result.character, result.text), ("Narrator", "I")
                        )
                        self.assertTrue(result.is_confident())
                        self.assertEqual(result.confidence, 96)
                        self.assertIsNone(result.speaker_confidence)
                        self.assertEqual(result.speaker_note, "")

    def test_faint_centered_short_names_survive_distant_bright_scenery(self):
        for name in ("X", "I", "J", "L", "T", "???", "6", "37"):
            for font_size in (10, 18):
                for inverted in (False, True):
                    with self.subTest(
                        name=name, font_size=font_size, inverted=inverted
                    ):
                        image, divider = self.frame(name="", text="I")
                        draw = ImageDraw.Draw(image)
                        draw.rectangle((390, 0, 399, divider - 1), fill="#505050")
                        draw.text(
                            (180, divider - 25),
                            name,
                            fill="#181818",
                            font_size=font_size,
                        )
                        if inverted:
                            image = ImageOps.invert(image)
                        regions = resolve_ocr_regions(image)
                        self.assertTrue(
                            speaker_glyphs_visible(regions.speaker.crop(image))
                        )
                        self.assertEqual(resolve_ocr_regions(image, regions), regions)
                        result = recognize_dialog_image_result(
                            image,
                            recognize_text=Mock(return_value="I"),
                            recognize_data=Mock(
                                side_effect=[
                                    {"text": [], "conf": []},
                                    {"text": ["I"], "conf": [96]},
                                ]
                            ),
                        )
                        self.assertEqual(
                            (result.character, result.text), ("Narrator", "I")
                        )
                        self.assertTrue(result.is_confident())
                        self.assertEqual(result.confidence, 96)
                        self.assertIsNone(result.speaker_confidence)
                        self.assertEqual(result.speaker_note, "")

    def test_faint_name_at_upper_crop_boundary_keeps_glyphs_and_allows_narrator(self):
        for name in ("I", "J", "L", "T", "???", "6", "37"):
            with self.subTest(name=name):
                image, divider = self.frame(name="", text="I")
                draw = ImageDraw.Draw(image)
                draw.rectangle((390, 0, 399, divider - 1), fill="#505050")
                draw.text((180, 50), name, fill="#181818", font_size=18)
                regions = resolve_ocr_regions(image)
                self.assertTrue(speaker_glyphs_visible(regions.speaker.crop(image)))
                result = recognize_dialog_image_result(
                    image,
                    recognize_text=Mock(return_value="I"),
                    recognize_data=Mock(
                        side_effect=[
                            {"text": [], "conf": []},
                            {"text": ["I"], "conf": [96]},
                        ]
                    ),
                )
                self.assertEqual((result.character, result.text), ("Narrator", "I"))
                self.assertTrue(result.is_confident())
                self.assertEqual(result.confidence, 96)
                self.assertIsNone(result.speaker_confidence)
                self.assertEqual(result.speaker_note, "")

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_original_six_narrator_frames_keep_dialogue_and_live_acceptance(self):
        directory = Path(__file__).parent / "fixtures" / "ocr-narrator-scenery"
        for sequence in (55, 58, 60, 94, 95, 99):
            with (
                self.subTest(sequence=sequence),
                Image.open(directory / f"event-{sequence}.png") as source,
            ):
                image = source.convert("RGB")
                frame = CapturedDialogFrame(image, 0)
                self.assertFalse(
                    speaker_glyphs_visible(frame.ocr_regions.speaker.crop(image))
                )
                result = recognize_dialog_image_result(
                    image, ocr_regions=frame.ocr_regions
                )
                self.assertEqual(result.character, "Narrator")
                self.assertTrue(result.text.strip())
                self.assertIsNone(result.speaker_confidence)
                self.assertTrue(result.is_confident())
                with patch(
                    "vntts.dialog_capture.recognize_screenshot_result",
                    return_value=result,
                ):
                    self.assertEqual(
                        recognize_live_frame(frame), ("Narrator", result.text)
                    )

    def test_empty_speaker_ocr_uses_narrator_with_scenery_or_added_name(self):
        directory = Path(__file__).parent / "fixtures" / "ocr-narrator-scenery"
        for filename in ("lecture-books.png", "gallery.png", "room.png"):
            with Image.open(directory / filename) as source:
                original = source.convert("RGB")
            for name in ("", "I", "X", "???"):
                with self.subTest(scene=filename, name=name):
                    image = original.copy()
                    regions = resolve_ocr_regions(image)
                    _, _, _, bottom = regions.speaker._pixel_box(*image.size)
                    ImageDraw.Draw(image).text(
                        (30, max(0, bottom - 25)), name, fill="white", font_size=18
                    )
                    regions = resolve_ocr_regions(image)
                    self.assertEqual(
                        speaker_glyphs_visible(regions.speaker.crop(image)), bool(name)
                    )
                    result = recognize_dialog_image_result(
                        image,
                        recognize_text=Mock(return_value="A complete dialogue."),
                        recognize_data=Mock(
                            side_effect=[
                                {"text": [], "conf": []},
                                {"text": ["A complete dialogue."], "conf": [96]},
                            ]
                        ),
                    )
                    self.assertEqual(result.text, "A complete dialogue.")
                    self.assertTrue(result.is_confident())
                    self.assertEqual(result.character, "Narrator")
                    self.assertEqual(result.confidence, 96)
                    self.assertIsNone(result.speaker_confidence)
                    self.assertEqual(result.speaker_note, "")

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_recorded_scenery_frames_are_accepted_without_a_speaker(self):
        directory = Path(__file__).parent / "fixtures" / "ocr-narrator-scenery"
        for filename in ("lecture-books.png", "gallery.png", "room.png"):
            with (
                self.subTest(scene=filename),
                Image.open(directory / filename) as image,
            ):
                result = recognize_dialog_image_result(image)
                self.assertEqual(result.character, "Narrator")
                self.assertIsNone(result.speaker_confidence)
                self.assertTrue(result.is_confident())

    def test_spatial_roles_preserve_short_names_and_dialogue(self):
        for name in ("X", "I", "???", "6", "37"):
            for text in ("I", "?", "...", "No.", ":", ";", "—"):
                with self.subTest(name=name, text=text):
                    image, _ = self.frame(lines=4, name=name, text=text)
                    result = recognize_dialog_image_result(
                        image,
                        recognize_text=Mock(return_value=text),
                        recognize_data=Mock(
                            side_effect=[
                                {"text": [name], "conf": [95]},
                                {"text": [text], "conf": [96]},
                            ]
                        ),
                    )
                    self.assertEqual((result.character, result.text), (name, text))
                    self.assertTrue(result.is_confident())

    def test_empty_speaker_ocr_with_visible_name_is_accepted_as_narrator(self):
        image, _ = self.frame(name="Unknown")
        result = recognize_dialog_image_result(
            image,
            recognize_text=Mock(return_value="?"),
            recognize_data=Mock(
                side_effect=[{"text": [], "conf": []}, {"text": ["?"], "conf": [96]}]
            ),
        )
        self.assertEqual((result.character, result.text), ("Narrator", "?"))
        self.assertTrue(result.is_confident())
        self.assertEqual(result.confidence, 96)
        self.assertIsNone(result.speaker_confidence)
        self.assertEqual(result.speaker_note, "")
        uncertain = Mock()
        with (
            patch(
                "vntts.dialog_capture.recognize_screenshot_result", return_value=result
            ),
            patch(
                "vntts.dialog_capture.detect_standalone_ellipsis_frame",
                return_value=True,
            ),
        ):
            self.assertEqual(
                recognize_live_frame(
                    CapturedDialogFrame(image, 0), uncertain_handler=uncertain
                ),
                ("Narrator", "?"),
            )
        uncertain.assert_not_called()
        with (
            patch(
                "vntts.dialog_capture.recognize_screenshot_result",
                return_value=replace(result, text=""),
            ),
            patch(
                "vntts.dialog_capture.detect_standalone_ellipsis_frame",
                return_value=True,
            ),
        ):
            self.assertEqual(
                recognize_live_frame(CapturedDialogFrame(image, 0)), ("Narrator", "...")
            )

    def test_absent_name_keeps_first_short_dialogue_as_dialogue(self):
        image, _ = self.frame(name="", text="I")
        result = recognize_dialog_image_result(
            image,
            recognize_text=Mock(return_value="I"),
            recognize_data=Mock(
                side_effect=[{"text": [], "conf": []}, {"text": ["I"], "conf": [96]}]
            ),
        )
        self.assertEqual((result.character, result.text), ("Narrator", "I"))
        self.assertTrue(result.is_confident())
        self.assertIsNone(result.speaker_confidence)

    def test_visual_ellipsis_waives_body_confidence_without_waiving_name_confidence(
        self,
    ):
        image, divider = self.frame(text="")
        for x in (12, 24, 36):
            ImageDraw.Draw(image).rectangle(
                (x, divider + 12, x + 4, divider + 15), fill="white"
            )
        for speaker_confidence in (20, 95):
            result = OCRResult(
                "X",
                "...",
                0,
                "sauvola",
                1,
                speaker_confidence=speaker_confidence,
                dialogue_confidence=0,
            )
            with (
                self.subTest(speaker_confidence=speaker_confidence),
                patch(
                    "vntts.dialog_capture.recognize_screenshot_result",
                    return_value=result,
                ),
            ):
                expected = (None, "") if speaker_confidence == 20 else ("X", "...")
                self.assertEqual(
                    recognize_live_frame(CapturedDialogFrame(image, 0)), expected
                )

    def test_recognized_symbols_keep_text_and_require_ocr_confidence(self):
        image, _ = self.frame(text="?")
        for symbols in ("...", "?", "?!", "—", "_", "「」", "♪"):
            for confidence in (20, 95):
                with self.subTest(symbols=symbols, confidence=confidence):
                    result = OCRResult(
                        "X",
                        symbols,
                        confidence,
                        "sauvola",
                        1,
                        speaker_confidence=95,
                        dialogue_confidence=confidence,
                    )
                    with patch(
                        "vntts.dialog_capture.recognize_screenshot_result",
                        return_value=result,
                    ):
                        self.assertEqual(
                            recognize_live_frame(CapturedDialogFrame(image, 0)),
                            ("X", symbols) if confidence == 95 else (None, ""),
                        )

    def test_dot_fallback_does_not_replace_recognized_symbols_or_waive_their_confidence(
        self,
    ):
        image, _ = self.frame(text="?")
        for symbols in ("?", "?!", "…"):
            for confidence in (20, 95):
                with self.subTest(symbols=symbols, confidence=confidence):
                    result = OCRResult(
                        "X",
                        symbols,
                        confidence,
                        "sauvola",
                        1,
                        speaker_confidence=95,
                        dialogue_confidence=confidence,
                    )
                    with (
                        patch(
                            "vntts.dialog_capture.recognize_screenshot_result",
                            return_value=result,
                        ),
                        patch(
                            "vntts.dialog_capture.detect_standalone_ellipsis_frame",
                            return_value=True,
                        ),
                    ):
                        self.assertEqual(
                            recognize_live_frame(CapturedDialogFrame(image, 0)),
                            ("X", symbols)
                            if confidence == 95 or symbols == "…"
                            else (None, ""),
                        )

    def test_resolved_fingerprints_ignore_continue_indicator_blink(self):
        image, _ = self.frame(text="Hi.")
        first = CapturedDialogFrame(image, 0)
        changed = image.copy()
        ImageDraw.Draw(changed).polygon(
            ((401, 153), (407, 153), (404, 159)), fill="white"
        )
        second = CapturedDialogFrame(changed, 0)
        self.assertEqual(
            fingerprint_dialog_frame(first), fingerprint_dialog_frame(second)
        )

    def test_cropped_ellipsis_is_detected_near_dialogue_top(self):
        image, divider = self.frame(lines=4, text="")
        draw = ImageDraw.Draw(image)
        for x in (12, 18, 24):
            draw.rectangle((x, divider + 12, x + 1, divider + 13), fill="white")
        result = OCRResult("X", "", 95, "sauvola", 1)
        with patch(
            "vntts.dialog_capture.recognize_screenshot_result", return_value=result
        ):
            self.assertEqual(
                recognize_live_frame(CapturedDialogFrame(image, 0)), ("X", "...")
            )

    def test_visual_ellipsis_from_empty_ocr_keeps_speaker_confidence_guard(self):
        image, divider = self.frame(lines=4, text="")
        draw = ImageDraw.Draw(image)
        for x in (12, 24, 36):
            draw.rectangle((x, divider + 12, x + 4, divider + 15), fill="white")
        for confidence in (20, 95):
            with self.subTest(speaker_confidence=confidence):
                result = OCRResult(
                    "X",
                    "",
                    0,
                    "sauvola",
                    1,
                    speaker_confidence=confidence,
                    dialogue_confidence=0,
                )
                with patch(
                    "vntts.dialog_capture.recognize_screenshot_result",
                    return_value=result,
                ):
                    self.assertEqual(
                        recognize_live_frame(CapturedDialogFrame(image, 0)),
                        (None, "") if confidence == 20 else ("X", "..."),
                    )

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_real_wide_sample_keeps_single_letter_name(self):
        with Image.open(
            Path(__file__).resolve().parents[1] / "samples" / "02.png"
        ) as source:
            image = DialogRegion(0.0, 0.68, 1.0, 0.24).crop(source)
        result = recognize_dialog_image_result(image)
        self.assertEqual(result.character, "X")
        self.assertEqual(
            result.text,
            "You were the perfect fulcrum! I don't know what I would have done without you.",
        )
        self.assertTrue(result.is_confident())


if __name__ == "__main__":
    unittest.main()

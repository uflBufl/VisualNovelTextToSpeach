import unittest

from PIL import Image, ImageDraw

from vntts.dialog_capture import (
    CapturedDialogFrame,
    dialog_completion_cue_visible,
    dialog_glyphs_visible,
)
from vntts.ocr import DialogRegion, OCRRegions


class SplitCompletionCueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.regions = OCRRegions(
            DialogRegion(0.05, 0.10, 0.25, 0.12),
            DialogRegion(0.05, 0.30, 0.75, 0.55),
        )

    @staticmethod
    def _frame_with_dialogue_and_continue_cue() -> Image.Image:
        image = Image.new("RGB", (1200, 240), "#202020")
        draw = ImageDraw.Draw(image)
        draw.text((100, 100), "Sharpodonties, sir.", fill="white")
        draw.polygon(
            ((1150, 204), (1160, 204), (1155, 213)),
            fill="#d0d0d0",
        )
        return image

    def test_full_capture_detects_cue_when_dialogue_crop_excludes_it(self):
        image = self._frame_with_dialogue_and_continue_cue()
        frame = CapturedDialogFrame(image, 0, self.regions)

        self.assertTrue(dialog_glyphs_visible(frame))
        self.assertTrue(dialog_completion_cue_visible(frame))

        dialogue_crop = self.regions.dialogue.crop(image)
        self.assertFalse(
            dialog_completion_cue_visible(CapturedDialogFrame(dialogue_crop, 0))
        )

    def test_ui_only_pixels_outside_dialogue_region_do_not_pass_presence_gate(self):
        image = Image.new("RGB", (1200, 240), "#202020")
        ImageDraw.Draw(image).rectangle((980, 40, 1150, 210), fill="white")
        frame = CapturedDialogFrame(image, 0, self.regions)

        self.assertFalse(dialog_glyphs_visible(frame))
        self.assertFalse(dialog_completion_cue_visible(frame))


if __name__ == "__main__":
    unittest.main()

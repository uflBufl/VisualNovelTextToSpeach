import unittest

from PIL import Image, ImageDraw

from vntts.ocr import detect_standalone_ellipsis_frame, resolve_ocr_regions


def dots_image(boxes=((10, 12, 14, 15), (22, 12, 26, 15), (33, 12, 37, 15))):
    image = Image.new("RGB", (200, 80), "#202020")
    draw = ImageDraw.Draw(image)
    for box in boxes:
        draw.rectangle(box, fill="white")
    return image


class EllipsisDetectionTest(unittest.TestCase):
    def test_dot_geometry_scales_with_image(self):
        original = dots_image()
        for resampling in (Image.Resampling.NEAREST, Image.Resampling.LANCZOS):
            for scale in (0.5, 0.75, 1, 1.5, 2, 3):
                with self.subTest(scale=scale, resampling=resampling):
                    image = original.resize(
                        (round(original.width * scale), round(original.height * scale)),
                        resampling,
                    )
                    self.assertTrue(
                        detect_standalone_ellipsis_frame(image, dialogue_only=True)
                    )

    def test_requires_exactly_three_similar_compact_aligned_dots(self):
        for name, boxes in (
            ("empty", ()),
            ("two", ((10, 12, 14, 15), (22, 12, 26, 15))),
            ("four", tuple((x, 12, x + 4, 15) for x in (10, 22, 33, 45))),
            ("noise", tuple((x, 12, x, 12) for x in (10, 22, 33))),
            ("tall", tuple((x, 12, x + 1, 21) for x in (10, 22, 33))),
            ("oversized", tuple((x, 12, x + 14, 26) for x in (10, 50, 90))),
            ("different-sizes", ((10, 12, 14, 15), (22, 12, 26, 15), (33, 10, 41, 18))),
            (
                "different-baselines",
                ((10, 12, 14, 15), (22, 12, 26, 15), (33, 25, 37, 28)),
            ),
            ("different-gaps", ((10, 12, 14, 15), (20, 12, 24, 15), (40, 12, 44, 15))),
        ):
            with self.subTest(name=name):
                self.assertFalse(
                    detect_standalone_ellipsis_frame(
                        dots_image(boxes), dialogue_only=True
                    )
                )

    def test_text_anywhere_in_dialogue_prevents_silent_classification(self):
        for text, position, with_dots in (
            ("Wait...", (4, 8), False),
            ("... Wait", (4, 8), False),
            ("Wait", (120, 8), True),
        ):
            with self.subTest(text=text, position=position):
                image = dots_image() if with_dots else dots_image(())
                ImageDraw.Draw(image).text(position, text, fill="white", font_size=18)
                self.assertFalse(
                    detect_standalone_ellipsis_frame(image, dialogue_only=True)
                )

    def test_full_frame_uses_divider_to_exclude_nameplate(self):
        for dots_in_nameplate in (False, True):
            with self.subTest(dots_in_nameplate=dots_in_nameplate):
                image = Image.new("RGB", (320, 120), "#202020")
                draw = ImageDraw.Draw(image)
                draw.line((8, 64, 310, 64), fill="white")
                if not dots_in_nameplate:
                    draw.text((16, 36), "Cornerstone", fill="white", font_size=18)
                top = 44 if dots_in_nameplate else 76
                for left in (16, 28, 40):
                    draw.rectangle((left, top, left + 4, top + 3), fill="white")
                self.assertIsNotNone(resolve_ocr_regions(image))
                self.assertEqual(
                    detect_standalone_ellipsis_frame(image), not dots_in_nameplate
                )

    def test_three_compact_glyphs_are_not_dots(self):
        for text, font_size in (("e e e", 17), ("# # #", 13), ("o o o", 17)):
            with self.subTest(text=text):
                image = dots_image(())
                ImageDraw.Draw(image).text(
                    (12, 12), text, fill="white", font_size=font_size
                )
                self.assertFalse(
                    detect_standalone_ellipsis_frame(image, dialogue_only=True)
                )

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.qt_images import pixmap_from_pil  # noqa: E402


class QtImagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_pixmap_owns_rgb_pixels_after_source_image_changes(self):
        for mode, color in (("RGB", (12, 34, 56)), ("RGBA", (12, 34, 56, 0))):
            with self.subTest(mode=mode):
                image = Image.new(mode, (3, 2), color)
                pixmap = pixmap_from_pil(image)
                image.paste(0, (0, 0, 3, 2))
                image.close()

                self.assertEqual((pixmap.width(), pixmap.height()), (3, 2))
                pixels = pixmap.toImage()
                for x in range(3):
                    for y in range(2):
                        self.assertEqual(
                            pixels.pixelColor(x, y).getRgb(), (12, 34, 56, 255)
                        )


if __name__ == "__main__":
    unittest.main()

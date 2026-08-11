import unittest
from types import SimpleNamespace

import numpy as np
from PIL import Image

from vntts.ocr_backend import RapidOCRBackend
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class RapidOCRBackendTest(unittest.TestCase):
    def test_orders_lines_and_returns_shared_ocr_result(self):
        output = SimpleNamespace(
            boxes=np.array(
                [
                    [[20, 80], [300, 80], [300, 110], [20, 110]],
                    [[20, 20], [180, 20], [180, 50], [20, 50]],
                ],
                dtype=np.float32,
            ),
            txts=("These old ones can carry everyone.", "Kamuta"),
            scores=(0.92, 0.98),
        )
        def engine(image, **options):
            del image, options
            return output
        registry = CharacterVoiceRegistry([CharacterVoice("Kamuta", "kamuta")])

        result = RapidOCRBackend(engine).recognize(
            Image.new("RGB", (640, 160)),
            registry,
        )

        self.assertEqual(result.character, "Kamuta")
        self.assertEqual(result.text, "These old ones can carry everyone.")
        self.assertGreater(result.confidence, 92.0)
        self.assertEqual(result.profile, "rapidocr-onnx")

    def test_empty_output_returns_empty_narrator_result(self):
        output = SimpleNamespace(boxes=None, txts=None, scores=None)

        result = RapidOCRBackend(lambda image, **options: output).recognize(
            Image.new("RGB", (10, 10))
        )

        self.assertEqual(result.character, "Narrator")
        self.assertEqual(result.text, "")
        self.assertEqual(result.confidence, 0.0)

    def test_rejects_language_without_a_configured_model(self):
        backend = RapidOCRBackend(lambda image, **options: None)

        with self.assertRaisesRegex(ValueError, "English only"):
            backend.recognize(Image.new("RGB", (10, 10)), language="jpn")


if __name__ == "__main__":
    unittest.main()

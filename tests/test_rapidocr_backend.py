import unittest
from types import SimpleNamespace

import numpy as np
from PIL import Image

from vntts.ocr import DialogRegion, OCRRegions
from vntts.ocr_backend import RapidOCRBackend
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class RapidOCRBackendTest(unittest.TestCase):
    def test_configured_regions_read_name_and_short_dialogue_independently(self):
        inputs = []
        outputs = iter(
            (
                SimpleNamespace(
                    boxes=[[[0, 0], [20, 0], [20, 20], [0, 20]]],
                    txts=["X"],
                    scores=[0.96],
                ),
                SimpleNamespace(
                    boxes=[[[0, 0], [20, 0], [20, 20], [0, 20]]],
                    txts=["Hey!"],
                    scores=[0.95],
                ),
            )
        )

        def engine(image, **options):
            self.assertEqual(options, {"use_cls": False})
            inputs.append(image)
            return next(outputs)

        result = RapidOCRBackend(engine).recognize(
            Image.new("RGB", (400, 200), (10, 20, 30)),
            ocr_regions=OCRRegions(
                DialogRegion(0.1, 0.1, 0.3, 0.2), DialogRegion(0.1, 0.4, 0.7, 0.4)
            ),
        )
        self.assertEqual((result.character, result.text), ("X", "Hey!"))
        self.assertEqual(inputs[0].shape, (40, 120, 3))
        self.assertEqual(inputs[1].shape, (80, 280, 3))
        self.assertTrue(np.array_equal(np.asarray(result.speaker_image), inputs[0]))
        self.assertTrue(np.array_equal(np.asarray(result.processed_image), inputs[1]))
        self.assertEqual(result.speaker_confidence, 96)
        self.assertEqual(result.dialogue_confidence, 95)

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
            self.assertEqual(options, {"use_cls": False})
            self.assertEqual(image.shape, (160, 640, 3))
            self.assertTrue(np.all(image == [10, 20, 30]))
            return output

        registry = CharacterVoiceRegistry([CharacterVoice("Kamuta", "kamuta")])

        result = RapidOCRBackend(engine).recognize(
            Image.new("RGB", (640, 160), (10, 20, 30)),
            registry,
        )

        self.assertEqual(result.character, "Kamuta")
        self.assertEqual(result.text, "These old ones can carry everyone.")
        self.assertGreater(result.confidence, 92.0)
        self.assertEqual(result.profile, "rapidocr-onnx")
        self.assertEqual(result.processed_image.mode, "RGB")
        self.assertEqual(result.processed_image.getpixel((0, 0)), (10, 20, 30))

    def test_empty_output_returns_empty_narrator_result(self):
        output = SimpleNamespace(boxes=None, txts=None, scores=None)

        result = RapidOCRBackend(lambda image, **options: output).recognize(
            Image.new("RGB", (10, 10))
        )

        self.assertEqual(result.character, "Narrator")
        self.assertEqual(result.text, "")
        self.assertEqual(result.confidence, 0.0)

    def test_rejects_inconsistent_output_vectors(self):
        output = SimpleNamespace(boxes=([0],), txts=(), scores=(0.98,))

        with self.assertRaises(ValueError):
            RapidOCRBackend(lambda image, **options: output).recognize(
                Image.new("RGB", (10, 10))
            )

    def test_rejects_language_without_a_configured_model(self):
        backend = RapidOCRBackend(lambda image, **options: None)

        with self.assertRaisesRegex(ValueError, "English only"):
            backend.recognize(Image.new("RGB", (10, 10)), language="jpn")


if __name__ == "__main__":
    unittest.main()

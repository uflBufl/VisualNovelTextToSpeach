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

    def test_rejects_inconsistent_output_vectors(self):
        output = SimpleNamespace(boxes=([0],), txts=(), scores=(0.98,))

        with self.assertRaises(ValueError):
            RapidOCRBackend(lambda image, **options: output).recognize(
                Image.new("RGB", (10, 10))
            )

    def test_rejects_malformed_scores_at_rapidocr_boundary(self):
        for score in (
            True,
            np.bool_(True),
            np.nan,
            np.inf,
            -np.inf,
            10**400,
            "0.9",
        ):
            with self.subTest(score=repr(score)):
                output = SimpleNamespace(
                    boxes=([[0, 0], [10, 0], [10, 10], [0, 10]],),
                    txts=("Hello",),
                    scores=(score,),
                )

                with self.assertRaisesRegex(ValueError, "score"):
                    RapidOCRBackend(lambda image, **options: output).recognize(
                        Image.new("RGB", (10, 10))
                    )

    def test_rejects_nonfinite_rapidocr_confidence_aggregate(self):
        output = SimpleNamespace(
            boxes=([[0, 0], [10, 0], [10, 10], [0, 10]],),
            txts=("Hello",),
            scores=(1e308,),
        )

        with self.assertRaisesRegex(ValueError, "confidence aggregate"):
            RapidOCRBackend(lambda image, **options: output).recognize(
                Image.new("RGB", (10, 10))
            )

    def test_normalizes_valid_numpy_scores_to_float(self):
        output = SimpleNamespace(
            boxes=([[0, 0], [10, 0], [10, 10], [0, 10]],),
            txts=("Hello",),
            scores=(np.float32(0.92),),
        )

        result = RapidOCRBackend(lambda image, **options: output).recognize(
            Image.new("RGB", (10, 10))
        )

        self.assertIs(type(result.confidence), float)
        self.assertAlmostEqual(result.confidence, 92.0, places=5)

    def test_rejects_language_without_a_configured_model(self):
        backend = RapidOCRBackend(lambda image, **options: None)

        with self.assertRaisesRegex(ValueError, "English only"):
            backend.recognize(Image.new("RGB", (10, 10)), language="jpn")


if __name__ == "__main__":
    unittest.main()

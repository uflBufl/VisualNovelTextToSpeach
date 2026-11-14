import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from vntts.asset_integrity import ModelIntegrityError
from vntts.model_assets import ModelDownloadCancelled
from vntts.sparse_ocr import (
    SparseLineRecognizer,
    isolate_sparse_line,
    prepare_sparse_ocr_model,
)


class SparseLineIsolationTest(unittest.TestCase):
    def line_image(self):
        image = Image.new("RGB", (400, 100), (12, 20, 30))
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 32, 74, 52), fill=(225, 230, 235))
        draw.rectangle((85, 41, 109, 45), fill=(225, 230, 235))
        return image

    def test_preserves_rgb_pixels_with_relative_border_across_scale_and_polarity(self):
        image = self.line_image()
        for scale in (0.5, 1, 1.5):
            scaled = image.resize(
                (round(image.width * scale), round(image.height * scale))
            )
            for source in (scaled, ImageOps.invert(scaled)):
                with self.subTest(scale=scale, background=source.getpixel((0, 0))):
                    crop = isolate_sparse_line(source)
                    self.assertIsNotNone(crop)
                    self.assertEqual(crop.mode, "RGB")
                    self.assertLess(crop.width, source.width * 0.35)
                    self.assertLess(crop.height, source.height * 0.65)
                    self.assertEqual(crop.getpixel((0, 0)), source.getpixel((0, 0)))
        crop = isolate_sparse_line(image)
        self.assertEqual(crop.size, (44, 25))
        np.testing.assert_array_equal(
            np.asarray(crop)[2:-2, 2:-2], np.asarray(image)[32:53, 70:110]
        )

    def test_blank_and_low_contrast_frames_have_no_line(self):
        for color in ("black", "white"):
            with self.subTest(color=color):
                self.assertIsNone(
                    isolate_sparse_line(Image.new("RGB", (400, 100), color))
                )
        image = Image.new("L", (400, 100), 100)
        ImageDraw.Draw(image).rectangle((70, 32, 109, 52), fill=120)
        self.assertIsNone(isolate_sparse_line(image))

    def test_competing_groups_and_ordinary_wide_line_are_not_chosen(self):
        image = self.line_image()
        ImageDraw.Draw(image).rectangle((260, 32, 264, 52), fill=(225, 230, 235))
        self.assertIsNone(isolate_sparse_line(image))
        image = Image.new("L", (400, 100))
        draw = ImageDraw.Draw(image)
        for left in range(40, 300, 15):
            draw.rectangle((left, 32, left + 4, 52), fill="white")
        self.assertIsNone(isolate_sparse_line(image))

    def test_clipped_fragments_are_excluded_without_discarding_separate_line(self):
        image = self.line_image()
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 10, 4, 15), fill=(225, 230, 235))
        crop = isolate_sparse_line(image)
        self.assertEqual(crop.size, (44, 25))
        clipped = Image.new("L", image.size)
        ImageDraw.Draw(clipped).rectangle((0, 10, 4, 15), fill="white")
        self.assertIsNone(isolate_sparse_line(clipped))


class SparseModelPreparationTest(unittest.TestCase):
    def test_prepares_pinned_model_through_manager_and_verifies_cached_bytes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = Mock()
            manager.download.return_value = root
            progress = Mock()
            cancelled = Event()
            with patch(
                "vntts.sparse_ocr.file_sha256",
                return_value="c3461add59bb4323ecba96a492ab75e06dda42467c9e3d0c18db5d1d21924be8",
            ) as checksum:
                path = prepare_sparse_ocr_model(manager, progress, cancelled)
                self.assertEqual(path, root / "en_PP-OCRv5_rec_mobile.onnx")
                options = manager.download.call_args.kwargs
                self.assertIs(options["progress"], progress)
                self.assertIs(options["cancel_event"], cancelled)
                self.assertIn("/v3.9.2/onnx/PP-OCRv5/rec/en_", options["asset"].urls[0])
                checksum.assert_called_once_with(path, error_type=ModelIntegrityError)
                checksum.return_value = "corrupt"
                with self.assertRaisesRegex(ModelIntegrityError, "publisher catalog"):
                    prepare_sparse_ocr_model(manager)

    def test_download_failure_and_cancellation_propagate(self):
        manager = Mock()
        manager.download.side_effect = OSError("offline")
        with self.assertRaisesRegex(OSError, "offline"):
            prepare_sparse_ocr_model(manager)
        manager.reset_mock()
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(ModelDownloadCancelled):
            prepare_sparse_ocr_model(manager, cancel_event=cancelled)
        manager.download.assert_not_called()
        cancelled.clear()
        manager.download.side_effect = lambda *args, **kwargs: (
            cancelled.set() or Path("cache")
        )
        with patch(
            "vntts.sparse_ocr.file_sha256",
            return_value="c3461add59bb4323ecba96a492ab75e06dda42467c9e3d0c18db5d1d21924be8",
        ):
            with self.assertRaises(ModelDownloadCancelled):
                prepare_sparse_ocr_model(manager, cancel_event=cancelled)


class SparseLineRecognizerTest(unittest.TestCase):
    def fake_runtime(self, *, embedded_dictionary=True):
        calls = []
        cfg = SimpleNamespace(
            Rec=SimpleNamespace(),
            EngineConfig=SimpleNamespace(onnxruntime=SimpleNamespace()),
        )

        class FakeRecognizer:
            def __init__(self, options):
                calls.append(options)
                self.session = SimpleNamespace(
                    have_key=lambda: embedded_dictionary,
                    get_character_list=lambda: ["a"],
                )
                self.get_character_dict(options)

            def __call__(self, request):
                calls.append(request.img[0])
                return SimpleNamespace(txts=("  I ...  ",), scores=(0.437,))

        modules = {
            "rapidocr": SimpleNamespace(__file__="package/__init__.py"),
            "rapidocr.ch_ppocr_rec": SimpleNamespace(
                TextRecognizer=FakeRecognizer,
                TextRecInput=lambda img: SimpleNamespace(img=img),
            ),
            "rapidocr.utils.parse_parameters": SimpleNamespace(
                ParseParams=SimpleNamespace(load=Mock(return_value=cfg))
            ),
            "rapidocr.utils.typings": SimpleNamespace(
                EngineType=SimpleNamespace(ONNXRUNTIME="onnxruntime"),
                LangRec=SimpleNamespace(EN="en"),
                ModelType=SimpleNamespace(MOBILE="mobile"),
                OCRVersion=SimpleNamespace(PPOCRV5="v5"),
            ),
        }
        return modules, calls

    def test_lazily_loads_once_and_returns_measured_confidence_with_bgr_pixels(self):
        modules, calls = self.fake_runtime()
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.onnx"
            path.write_bytes(b"model")
            recognizer = SparseLineRecognizer(path)
            self.assertEqual(calls, [])
            with patch.dict(sys.modules, modules):
                for _ in range(2):
                    self.assertEqual(
                        recognizer.recognize(Image.new("RGB", (40, 20), (10, 20, 30))),
                        ("I ...", 43.7),
                    )
            options = calls[0]
            self.assertEqual(options.model_path, str(path))
            self.assertEqual(options.lang_type, "en")
            self.assertEqual(options.engine_type, "onnxruntime")
            self.assertEqual(options.engine_cfg.intra_op_num_threads, 1)
            self.assertEqual(options.engine_cfg.inter_op_num_threads, 1)
            self.assertIsNone(options.font_path)
            self.assertEqual(len(calls), 3)
            np.testing.assert_array_equal(calls[1][0, 0], (30, 20, 10))
            self.assertTrue(calls[1].flags.c_contiguous)

    def test_missing_model_or_embedded_dictionary_fails_without_download(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.onnx"
            with self.assertRaisesRegex(RuntimeError, "model is missing"):
                SparseLineRecognizer(path).recognize(Image.new("L", (20, 10)))
            path.write_bytes(b"model")
            modules, _ = self.fake_runtime(embedded_dictionary=False)
            with patch.dict(sys.modules, modules):
                with self.assertRaisesRegex(
                    RuntimeError, "no embedded character dictionary"
                ):
                    SparseLineRecognizer(path).recognize(Image.new("L", (20, 10)))

    def test_empty_result_keeps_empty_text_and_zero_confidence(self):
        recognizer = SparseLineRecognizer("unused")
        recognizer._recognizer = Mock(
            return_value=SimpleNamespace(txts=("",), scores=(0.0,))
        )
        self.assertEqual(recognizer.recognize(Image.new("L", (20, 10))), ("", 0.0))

    def test_malformed_results_are_rejected(self):
        for texts, scores in (
            (None, [0.5]),
            ([], []),
            (["x", "y"], [0.8, 0.8]),
            ([5], [0.5]),
            (["x"], [float("nan")]),
            (["x"], [-1]),
            (["x"], [1.1]),
            (["x"], [True]),
        ):
            with self.subTest(texts=texts, scores=scores):
                recognizer = SparseLineRecognizer("unused")
                recognizer._recognizer = Mock(
                    return_value=SimpleNamespace(txts=texts, scores=scores)
                )
                with self.assertRaises(ValueError):
                    recognizer.recognize(Image.new("L", (20, 10)))


if __name__ == "__main__":
    unittest.main()

import json
import os
import shutil
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from tests.symlink_support import symlink_or_skip
from vntts import release_smoke_test
from vntts.ocr import OCRResult
from vntts.release_smoke_test import run_release_smoke_test
from vntts.window_capture import WindowCaptureTarget


class ReleaseSmokeTest(unittest.TestCase):
    def test_production_advance_preserves_dispatch_error_during_shutdown(self):
        primary = RuntimeError("key dispatch failed")
        controller = Mock()
        controller._auto_advance_dialog.side_effect = primary
        controller.shutdown.side_effect = OSError("shutdown failed")
        with (
            patch("vntts.controller.AppController", return_value=controller),
            self.assertRaises(RuntimeError) as raised,
        ):
            release_smoke_test._production_auto_advance("game")
        self.assertIs(raised.exception, primary)
        self.assertEqual(
            primary.__notes__, ["Smoke controller shutdown failed: shutdown failed"]
        )
        controller._auto_advance_dialog.assert_called_once_with()
        controller.shutdown.assert_called_once_with()

    def test_production_advance_propagates_standalone_shutdown_failure(self):
        controller = Mock()
        controller._auto_advance_dialog.return_value = True
        controller.shutdown.side_effect = OSError("shutdown failed")
        with (
            patch("vntts.controller.AppController", return_value=controller),
            self.assertRaisesRegex(OSError, "shutdown failed"),
        ):
            release_smoke_test._production_auto_advance("game")
        controller.shutdown.assert_called_once_with()

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_real_sample_reaches_speech_stage(self):
        from vntts.ocr_worker import close_ocr_worker, open_ocr_worker

        open_ocr_worker()
        self.addCleanup(open_ocr_worker)
        self.addCleanup(close_ocr_worker)
        sample = Path(__file__).resolve().parents[1] / "samples" / "01.jpeg"
        engine = Mock(speak=Mock(return_value=True))
        with TemporaryDirectory() as temporary_directory:
            successful, report_path = run_release_smoke_test(
                image_path=sample,
                report_path=Path(temporary_directory) / "report.json",
                expected_speaker="Marcus",
                engine_factory=Mock(return_value=engine),
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertTrue(successful)
        self.assertEqual(report["speaker"], "Marcus")
        self.assertIn("wanted to go home", report["text"])
        engine.speak.assert_called_once_with(report["text"])

    def test_report_cannot_replace_image_before_ocr_or_speech(self):
        for alias in ("direct", "hardlink", "symlink", "default"):
            with self.subTest(alias=alias), TemporaryDirectory() as directory:
                root = Path(directory)
                image = root / (
                    "release-smoke-test.json" if alias == "default" else "dialog.png"
                )
                Image.new("RGB", (20, 10), "white").save(image, format="PNG")
                original = image.read_bytes()
                output = (
                    image if alias in {"direct", "default"} else root / "report.json"
                )
                if alias == "hardlink":
                    output.hardlink_to(image)
                elif alias == "symlink":
                    symlink_or_skip(output, image)
                recognize, engine_factory = Mock(), Mock()
                with (
                    patch.object(
                        release_smoke_test,
                        "get_local_data_directory",
                        return_value=root,
                    ),
                    self.assertRaisesRegex(ValueError, "must differ from input files"),
                ):
                    run_release_smoke_test(
                        image_path=image,
                        report_path=None if alias == "default" else output,
                        recognize=recognize,
                        engine_factory=engine_factory,
                    )
                recognize.assert_not_called()
                engine_factory.assert_not_called()
                self.assertEqual(image.read_bytes(), original)
                self.assertTrue(output.samefile(image))
                if alias == "symlink":
                    self.assertTrue(output.is_symlink())

    def test_static_image_runs_ocr_synthesis_and_audio_playback(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            image_path = root / "dialog.png"
            report_path = root / "report.json"
            Image.new("RGB", (1000, 800), "white").save(image_path)
            original = image_path.read_bytes()
            report_path.write_text("old report")
            recognize = Mock(
                return_value=OCRResult(
                    "Marcus",
                    "This is a release smoke test.",
                    94.0,
                    "balanced",
                    1,
                )
            )
            engine = Mock(speak=Mock(return_value=True))
            engine_factory = Mock(return_value=engine)

            result = run_release_smoke_test(
                image_path=image_path,
                report_path=report_path,
                expected_speaker="Marcus",
                recognize=recognize,
                engine_factory=engine_factory,
            )
            successful, written_path = result

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(successful)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(written_path, report_path)
            self.assertTrue(report["success"])
            self.assertEqual(image_path.read_bytes(), original)
            self.assertEqual(report["speaker"], "Marcus")
            self.assertEqual(report["confidence"], 94.0)
            engine_factory.assert_called_once_with(model_name="tts_models/en/vctk/vits")
            engine.speak.assert_called_once_with("This is a release smoke test.")

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_image_acquisition_refuses_fifo_before_ocr_and_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "dialog.png"
            Image.new("RGB", (20, 10), "white").save(path)
            recognize = Mock(
                return_value=OCRResult("X", "Dialog.", 95.0, "balanced", 1)
            )
            engine_factory = Mock(return_value=Mock(speak=Mock(return_value=True)))
            report = root / "report.json"
            gate: RegularFileGate = {"path": path, "armed": True}
            with swap_at_acquisition(self, gate):
                refused = run_release_smoke_test(
                    image_path=path,
                    report_path=report,
                    recognize=recognize,
                    engine_factory=engine_factory,
                )
                self.assertFalse(refused.successful)
                document = json.loads(report.read_text(encoding="utf-8"))
                self.assertIn("not a regular file", document["checks"][-1]["message"])
                recognize.assert_not_called()
                engine_factory.assert_not_called()
            accepted = run_release_smoke_test(
                image_path=path,
                report_path=report,
                recognize=recognize,
                engine_factory=engine_factory,
            )
            self.assertTrue(accepted.successful)
            recognize.assert_called_once()
            engine_factory.assert_called_once()

    def test_image_source_ownership_retains_crop_and_failure_reports(self):
        for role in ("PNG", "JPEG", "TIFF", "unidentified", "conversion", "missing"):
            with self.subTest(role=role), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                path = root / "dialog.image"
                if role == "unidentified":
                    path.write_bytes(b"not an image")
                elif role != "missing":
                    Image.new("L", (1000, 800), 123).save(
                        path, format=role if role in {"PNG", "JPEG", "TIFF"} else "PNG"
                    )
                expected = release_smoke_test.default_dialog_region.crop(
                    Image.new("RGB", (1000, 800), (123, 123, 123))
                )
                sources = []
                acquire = release_smoke_test.open_regular_binary
                decode = Image.open

                @contextmanager
                def opened(candidate):
                    with acquire(candidate) as source:
                        sources.append(source)
                        yield source

                def decoded(source, *args, **kwargs):
                    self.assertIs(source, sources[-1])
                    self.assertFalse(source.closed)
                    return decode(source, *args, **kwargs)

                def recognized(image, *_args, **_kwargs):
                    self.assertTrue(sources[-1].closed)
                    self.assertEqual(image.mode, "RGB")
                    self.assertEqual(image.size, expected.size)
                    self.assertEqual(image.tobytes(), expected.tobytes())
                    return OCRResult("X", "Dialog.", 95.0, "balanced", 1)

                recognize = Mock(side_effect=recognized)
                engine_factory = Mock(return_value=Mock(speak=Mock(return_value=True)))
                with (
                    patch.object(release_smoke_test, "open_regular_binary", opened),
                    patch.object(Image, "open", decoded),
                    patch.object(
                        Image.Image,
                        "convert",
                        side_effect=ValueError("forced RGB failure"),
                    )
                    if role == "conversion"
                    else nullcontext(),
                ):
                    result = run_release_smoke_test(
                        image_path=path,
                        report_path=root / "report.json",
                        recognize=recognize,
                        engine_factory=engine_factory,
                    )
                document = json.loads(
                    (root / "report.json").read_text(encoding="utf-8")
                )
                if role in {"unidentified", "conversion", "missing"}:
                    self.assertFalse(result.successful)
                    message = {
                        "unidentified": "cannot identify image",
                        "conversion": "forced RGB failure",
                        "missing": "dialog.image",
                    }[role]
                    self.assertIn(message, document["checks"][-1]["message"])
                    recognize.assert_not_called()
                    engine_factory.assert_not_called()
                else:
                    self.assertTrue(result.successful)
                    recognize.assert_called_once()
                    engine_factory.assert_called_once()
                self.assertEqual(len(sources), 0 if role == "missing" else 1)
                self.assertTrue(all(source.closed for source in sources))

    def test_selected_window_uses_production_capture_target(self):
        image = Image.new("RGB", (400, 120), "white")
        capture = Mock(return_value=(image, None))
        recognize = Mock(
            return_value=OCRResult(
                "X",
                "Window capture works.",
                90.0,
                "balanced",
                1,
            )
        )
        engine = Mock(speak=Mock(return_value=True))
        with TemporaryDirectory() as temporary_directory:
            successful, _ = run_release_smoke_test(
                window_title="Reverse: 1999",
                report_path=Path(temporary_directory) / "report.json",
                capture=capture,
                recognize=recognize,
                engine_factory=Mock(return_value=engine),
            )

        self.assertTrue(successful)
        capture_target = capture.call_args.kwargs["capture_target"]
        self.assertIsInstance(capture_target, WindowCaptureTarget)
        self.assertEqual(capture_target.window_title, "Reverse: 1999")
        self.assertFalse(capture.call_args.kwargs["save_screenshot"])
        engine.speak.assert_called_once_with("Window capture works.")

    def test_window_smoke_dispatches_and_visually_acknowledges_auto_advance(self):
        images = [Image.new("RGB", (400, 120), color) for color in ("white", "black")]
        capture = Mock(side_effect=((images[0], None), (images[1], None)))
        recognize = Mock(
            side_effect=(
                OCRResult("Marcus", "First dialog.", 95.0, "balanced", 1),
                OCRResult(
                    "Marcus",
                    "Auto advance acknowledged.",
                    96.0,
                    "balanced",
                    1,
                ),
            )
        )
        auto_advance = Mock(return_value=True)
        with TemporaryDirectory() as temporary_directory:
            successful, report_path = run_release_smoke_test(
                window_title="VNTTS fixture",
                report_path=Path(temporary_directory) / "report.json",
                expected_speaker="Marcus",
                capture=capture,
                recognize=recognize,
                engine_factory=Mock(return_value=Mock(speak=Mock(return_value=True))),
                auto_advance_expected_text="Auto advance acknowledged.",
                auto_advance=auto_advance,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertTrue(successful)
        self.assertTrue(report["auto_advance_dispatched"])
        self.assertTrue(report["auto_advance_acknowledged"])
        self.assertEqual(
            report["auto_advance_controller"],
            "AppController._auto_advance_dialog",
        )
        auto_advance.assert_called_once_with()
        self.assertEqual(capture.call_count, 2)

    def test_auto_advance_dispatch_without_visual_change_fails_closed(self):
        image = Image.new("RGB", (400, 120), "white")
        capture = Mock(return_value=(image, None))
        recognize = Mock(
            return_value=OCRResult("Marcus", "Unchanged dialog.", 95.0, "balanced", 1)
        )
        with TemporaryDirectory() as temporary_directory:
            successful, report_path = run_release_smoke_test(
                window_title="VNTTS fixture",
                report_path=Path(temporary_directory) / "report.json",
                capture=capture,
                recognize=recognize,
                engine_factory=Mock(return_value=Mock(speak=Mock(return_value=True))),
                auto_advance_expected_text="Next dialog.",
                auto_advance=Mock(return_value=True),
                auto_advance_timeout_seconds=0,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertFalse(successful)
        self.assertTrue(report["auto_advance_dispatched"])
        self.assertFalse(report["auto_advance_acknowledged"])
        self.assertIn("did not show", report["checks"][-1]["message"])

    def test_uncertain_ocr_fails_without_loading_speech_model(self):
        recognize = Mock(
            return_value=OCRResult(
                "Marcus",
                "Uncertain text",
                25.0,
                "balanced",
                3,
            )
        )
        engine_factory = Mock()
        with TemporaryDirectory() as temporary_directory:
            image_path = Path(temporary_directory) / "dialog.png"
            report_path = Path(temporary_directory) / "report.json"
            Image.new("RGB", (1000, 800), "white").save(image_path)

            successful, _ = run_release_smoke_test(
                image_path=image_path,
                report_path=report_path,
                recognize=recognize,
                engine_factory=engine_factory,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertFalse(successful)
        self.assertFalse(report["success"])
        self.assertIn("below 60%", report["checks"][-1]["message"])
        engine_factory.assert_not_called()

    def test_requires_exactly_one_capture_source(self):
        with TemporaryDirectory() as temporary_directory:
            successful, report_path = run_release_smoke_test(
                report_path=Path(temporary_directory) / "report.json",
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertFalse(successful)
        self.assertIn("exactly one", report["checks"][0]["message"])

    def test_invalid_auto_advance_timeout_fails_before_side_effects(self):
        for timeout in (-1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout), TemporaryDirectory() as directory:
                capture = Mock()
                engine_factory = Mock()
                auto_advance = Mock()
                successful, report_path = run_release_smoke_test(
                    window_title="VNTTS fixture",
                    report_path=Path(directory) / "report.json",
                    capture=capture,
                    engine_factory=engine_factory,
                    auto_advance=auto_advance,
                    auto_advance_expected_text="Next dialog.",
                    auto_advance_timeout_seconds=timeout,
                )
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertFalse(successful)
                self.assertIn("finite and nonnegative", report["checks"][0]["message"])
                capture.assert_not_called()
                engine_factory.assert_not_called()
                auto_advance.assert_not_called()

    def test_incomplete_playback_fails_before_auto_advance(self):
        capture = Mock(return_value=(Image.new("RGB", (400, 120), "white"), None))
        recognize = Mock(
            return_value=OCRResult("Marcus", "Initial dialog.", 95.0, "balanced", 1)
        )
        engine = Mock(speak=Mock(return_value=False))
        auto_advance = Mock(return_value=True)
        with TemporaryDirectory() as directory:
            successful, report_path = run_release_smoke_test(
                window_title="VNTTS fixture",
                report_path=Path(directory) / "report.json",
                capture=capture,
                recognize=recognize,
                engine_factory=Mock(return_value=engine),
                auto_advance=auto_advance,
                auto_advance_expected_text="Next dialog.",
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertFalse(successful)
        self.assertIn("playback did not complete", report["checks"][-1]["message"])
        self.assertFalse(report["auto_advance_dispatched"])
        self.assertFalse(report["auto_advance_acknowledged"])
        self.assertEqual(capture.call_count, 1)
        auto_advance.assert_not_called()

    def test_acknowledgement_must_be_new_nonblank_text(self):
        for text in ("", "  ", "Already visible."):
            with self.subTest(text=text), TemporaryDirectory() as directory:
                capture = Mock(
                    return_value=(Image.new("RGB", (400, 120), "white"), None)
                )
                recognize = Mock(
                    return_value=OCRResult(
                        "Marcus", "Already visible.", 95.0, "balanced", 1
                    )
                )
                engine_factory = Mock()
                auto_advance = Mock(return_value=True)
                successful, report_path = run_release_smoke_test(
                    window_title="VNTTS fixture",
                    report_path=Path(directory) / "report.json",
                    capture=capture,
                    recognize=recognize,
                    engine_factory=engine_factory,
                    auto_advance=auto_advance,
                    auto_advance_expected_text=text,
                )
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertFalse(successful)
                expected = "already visible" if text.strip() else "must not be blank"
                self.assertIn(expected, report["checks"][-1]["message"])
                self.assertFalse(report["auto_advance_acknowledged"])
                self.assertEqual(capture.call_count, 1 if text.strip() else 0)
                engine_factory.assert_not_called()
                auto_advance.assert_not_called()


if __name__ == "__main__":
    unittest.main()

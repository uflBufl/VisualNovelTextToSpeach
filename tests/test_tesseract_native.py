import ctypes as ct
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

from PIL import Image

from vntts.ocr import (
    DialogRegion,
    OCRRegions,
    recognize_dialog_image_result,
)
from vntts.tesseract_config import (
    SAUVOLA_PARAMETERS,
    require_sauvola_cli,
    tesseract_config,
)
from vntts.tesseract_native import (
    NativeTesseract,
    _library_candidates,
    _load_library,
    _tessdata_directory,
)


class NativeTesseractTest(unittest.TestCase):
    def setUp(self):
        self.library = Mock()
        self.library.TessBaseAPICreate.return_value = 123
        self.library.TessBaseAPIInit3.return_value = 0
        self.library.TessBaseAPISetVariable.return_value = 1
        self.library.TessBaseAPIRecognize.return_value = 0
        self.library.TessBaseAPIGetLoadedLanguagesAsVector.return_value = [
            b"eng",
            b"rus",
            None,
        ]
        self.buffers = [
            ct.create_string_buffer("Hello Привет\n".encode()),
            ct.create_string_buffer(
                b"1\t1\t0\t0\t0\t0\t0\t0\t100\t30\t-1\t\n"
                b"5\t1\t1\t1\t1\t1\t5\t6\t40\t15\t96.750000\tHello\n"
            ),
        ]
        self.library.TessBaseAPIGetUTF8Text.return_value = ct.addressof(self.buffers[0])
        self.library.TessBaseAPIGetTsvText.return_value = ct.addressof(self.buffers[1])
        self.dll_directory = Mock()
        loader = patch(
            "vntts.tesseract_native._load_library",
            return_value=(self.library, self.dll_directory),
        )
        loader.start()
        self.addCleanup(loader.stop)
        self.engine = NativeTesseract("eng+rus", tessdata_dir="/models")
        self.addCleanup(self.engine.close)
        self.image = Image.new("L", (100, 30), 255)

    def test_reuses_models_and_recognizes_both_outputs_once(self):
        for psm in (7, 6, 11):
            text, data = self.engine.recognize(self.image, psm, text=True, data=True)
            self.assertEqual(text, "Hello Привет\n")
            self.assertEqual(data["text"], ["", "Hello"])
            self.assertEqual(data["conf"], [-1, 96])
            self.assertEqual(data["left"], [0, 5])
        self.library.TessBaseAPIInit3.assert_called_once_with(
            123, b"/models", b"eng+rus"
        )
        self.assertEqual(
            self.library.TessBaseAPISetVariable.call_args_list,
            [
                call(123, name.encode("ascii"), value.encode("ascii"))
                for name, value in SAUVOLA_PARAMETERS
            ],
        )
        self.assertEqual(self.library.TessBaseAPIRecognize.call_count, 3)
        self.assertEqual(self.library.TessDeleteText.call_count, 6)
        self.assertEqual(self.library.TessBaseAPIClear.call_count, 3)
        self.assertEqual(self.library.TessBaseAPIClearAdaptiveClassifier.call_count, 6)
        self.assertIs(self.library.TessBaseAPIGetUTF8Text.restype, ct.c_void_p)
        self.library.TessBaseAPISetImage.assert_called_with(
            123, self.image.tobytes(), 100, 30, 1, 100
        )

    def test_data_only_does_not_request_text(self):
        text, data = self.engine.recognize(self.image, 7, text=False, data=True)
        self.assertEqual(text, "")
        self.assertEqual(data["text"], ["", "Hello"])
        self.library.TessBaseAPIGetUTF8Text.assert_not_called()
        self.library.TessDeleteText.assert_called_once_with(
            ct.addressof(self.buffers[1])
        )

    def test_failed_recognition_clears_image_and_adaptation(self):
        self.library.TessBaseAPIRecognize.return_value = -1
        with self.assertRaisesRegex(RuntimeError, "recognition failed"):
            self.engine.recognize(self.image, 6, text=True, data=True)
        self.library.TessBaseAPIClear.assert_called_once_with(123)
        self.assertEqual(self.library.TessBaseAPIClearAdaptiveClassifier.call_count, 2)
        self.library.TessBaseAPIGetUTF8Text.assert_not_called()

    def test_invalid_output_frees_pointer_before_clearing_image(self):
        invalid = ct.create_string_buffer(b"\xff")
        self.library.TessBaseAPIGetUTF8Text.return_value = ct.addressof(invalid)
        with self.assertRaises(UnicodeDecodeError):
            self.engine.recognize(self.image, 6, text=True, data=False)
        self.library.TessDeleteText.assert_called_once_with(ct.addressof(invalid))
        self.library.TessBaseAPIClear.assert_called_once_with(123)

    def test_missing_output_is_an_error_and_clears_image(self):
        self.library.TessBaseAPIGetUTF8Text.return_value = None
        with self.assertRaisesRegex(RuntimeError, "did not return"):
            self.engine.recognize(self.image, 6, text=True, data=False)
        self.library.TessDeleteText.assert_not_called()
        self.library.TessBaseAPIClear.assert_called_once_with(123)

    def test_close_is_idempotent_and_releases_dll_search_directory_last(self):
        self.engine.close()
        self.engine.close()
        self.library.TessBaseAPIDelete.assert_called_once_with(123)
        self.dll_directory.close.assert_called_once_with()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.engine.recognize(self.image, 6, text=True, data=True)

    def test_failed_model_initialization_releases_native_handle(self):
        self.engine.close()
        self.library.reset_mock()
        self.dll_directory.reset_mock()
        self.library.TessBaseAPIInit3.return_value = -1
        with self.assertRaisesRegex(RuntimeError, "TESSDATA_PREFIX"):
            NativeTesseract("missing", tessdata_dir="/models")
        self.library.TessBaseAPIDelete.assert_called_once_with(123)
        self.dll_directory.close.assert_called_once_with()
        self.library.TessBaseAPISetVariable.assert_not_called()

    def test_partial_language_load_fails_and_releases_language_array_and_engine(self):
        self.engine.close()
        self.library.reset_mock()
        self.library.TessBaseAPIGetLoadedLanguagesAsVector.return_value = [b"eng", None]
        with self.assertRaisesRegex(RuntimeError, "requested languages: rus"):
            NativeTesseract("eng+rus", tessdata_dir="/models")
        self.library.TessDeleteTextArray.assert_called_once_with([b"eng", None])
        self.library.TessBaseAPIDelete.assert_called_once_with(123)
        self.library.TessBaseAPISetVariable.assert_not_called()

    def test_unsupported_sauvola_setting_releases_engine_and_dll_directory(self):
        self.engine.close()
        for index, (name, value) in enumerate(SAUVOLA_PARAMETERS):
            with self.subTest(setting=name):
                self.library.reset_mock()
                self.dll_directory.reset_mock()
                self.library.TessBaseAPISetVariable.side_effect = [1] * index + [0]
                with self.assertRaisesRegex(
                    RuntimeError, f"Sauvola setting {name}={value}.*Tesseract 5"
                ):
                    NativeTesseract("eng+rus", tessdata_dir="/models")
                self.assertEqual(
                    self.library.TessBaseAPISetVariable.call_count, index + 1
                )
                self.library.TessBaseAPIDelete.assert_called_once_with(123)
                self.dll_directory.close.assert_called_once_with()
                self.library.TessBaseAPIRecognize.assert_not_called()

    def test_rejects_invalid_images_modes_and_empty_output_requests(self):
        for image, psm, text, data in (
            (self.image.convert("RGB"), 6, True, True),
            (Image.new("L", (0, 10)), 6, True, True),
            (self.image, 14, True, True),
            (self.image, True, True, True),
            (self.image, 6, False, False),
        ):
            with self.subTest(psm=psm, image=image.size, mode=image.mode):
                with self.assertRaises(ValueError):
                    self.engine.recognize(image, psm, text=text, data=data)
        self.library.TessBaseAPIRecognize.assert_not_called()


class TesseractConfigTest(unittest.TestCase):
    def setUp(self):
        cache = patch("vntts.tesseract_config._SAUVOLA_CLI_EXECUTABLES", set())
        cache.start()
        self.addCleanup(cache.stop)

    def test_subprocess_config_uses_fixed_sauvola_settings(self):
        self.assertEqual(
            tesseract_config(7),
            "--psm 7 -c thresholding_method=2 -c thresholding_window_size=0.33 "
            "-c thresholding_kfactor=0.5",
        )

    def test_supported_cli_is_checked_once_per_executable_with_bounded_timeout(self):
        parameters = "\n".join(f"{name}\t{value}" for name, value in SAUVOLA_PARAMETERS)
        with patch(
            "vntts.tesseract_config.subprocess.run",
            return_value=Mock(stdout=parameters),
        ) as run:
            require_sauvola_cli("configured", None)
            require_sauvola_cli("configured", 2.5)
            run.assert_called_once_with(
                ["configured", "--print-parameters"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10.0,
            )
            require_sauvola_cli("another", 2.5)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.kwargs["timeout"], 2.5)

    def test_cli_rejects_each_missing_parameter_without_caching_failure(self):
        for missing, _value in SAUVOLA_PARAMETERS:
            with self.subTest(parameter=missing):
                parameters = "\n".join(
                    f"{name}\t{value}"
                    for name, value in SAUVOLA_PARAMETERS
                    if name != missing
                )
                with patch(
                    "vntts.tesseract_config.subprocess.run",
                    return_value=Mock(stdout=parameters),
                ) as run:
                    for _attempt in range(2):
                        with self.assertRaisesRegex(
                            RuntimeError, f"Sauvola settings: {missing}.*Tesseract 5"
                        ):
                            require_sauvola_cli("configured", 2.5)
                    self.assertEqual(run.call_count, 2)

    def test_cli_probe_errors_are_actionable(self):
        for error in (
            FileNotFoundError("missing executable"),
            subprocess.CalledProcessError(1, "configured"),
            subprocess.TimeoutExpired("configured", 2.5),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch("vntts.tesseract_config.subprocess.run", side_effect=error),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Sauvola support.*Tesseract 5"
                ):
                    require_sauvola_cli("configured", 2.5)


class TesseractDiscoveryTest(unittest.TestCase):
    def test_explicit_and_environment_model_directories_skip_executable(self):
        with (
            patch.dict(os.environ, {"TESSDATA_PREFIX": "/environment-models"}),
            patch("vntts.tesseract_native.shutil.which") as which,
        ):
            self.assertEqual(
                _tessdata_directory("configured", "/explicit"), "/explicit"
            )
            self.assertEqual(
                _tessdata_directory("configured", None), "/environment-models"
            )
        which.assert_not_called()

    def test_discovers_models_beside_configured_executable_or_install_prefix(self):
        for relative in (
            "bin/tessdata",
            "share/tessdata",
            "share/tesseract-ocr/5/tessdata",
        ):
            with self.subTest(relative=relative), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                executable = root / "bin/tesseract"
                executable.parent.mkdir()
                executable.touch()
                models = root / relative
                models.mkdir(parents=True)
                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch(
                        "vntts.tesseract_native.shutil.which",
                        return_value=str(executable),
                    ),
                ):
                    self.assertEqual(
                        _tessdata_directory("configured", None), str(models)
                    )

    def test_missing_executable_uses_library_default_models(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.tesseract_native.shutil.which", return_value=None),
        ):
            self.assertIsNone(_tessdata_directory("missing", None))

    def test_discovery_finds_macos_library_in_pyinstaller_frameworks(self):
        with TemporaryDirectory() as temporary:
            frameworks = Path(temporary) / "Contents/Frameworks"
            binary = frameworks / "tesseract/tesseract"
            binary.parent.mkdir(parents=True)
            binary.touch()
            library = frameworks / "libtesseract.5.dylib"
            library.touch()
            with (
                patch(
                    "vntts.tesseract_native.sys._MEIPASS", str(frameworks), create=True
                ),
                patch("vntts.tesseract_native.shutil.which", return_value=str(binary)),
                patch("vntts.tesseract_native.find_library", return_value=None),
            ):
                self.assertIn(str(library), _library_candidates("configured"))

    def test_discovery_finds_windows_dll_names_beside_configured_executable(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            executable = directory / "tesseract.exe"
            executable.touch()
            names = ("libtesseract-5.dll", "tesseract55.dll")
            for name in names:
                (directory / name).touch()
            with (
                patch(
                    "vntts.tesseract_native.shutil.which", return_value=str(executable)
                ),
                patch(
                    "vntts.tesseract_native.find_library", return_value="system-library"
                ),
            ):
                candidates = _library_candidates("configured")
            self.assertEqual(candidates[:2], [str(directory / name) for name in names])
            self.assertIn("system-library", candidates)

    def test_explicit_library_failure_is_actionable_and_does_not_fallback(self):
        with (
            patch(
                "vntts.tesseract_native.ct.CDLL",
                side_effect=OSError("missing dependency"),
            ) as load,
            patch("vntts.tesseract_native._library_candidates") as candidates,
            self.assertRaisesRegex(
                RuntimeError, "VNTTS_TESSERACT_LIBRARY.*missing dependency"
            ),
        ):
            _load_library("configured", "/missing/library")
        load.assert_called_once_with("/missing/library")
        candidates.assert_not_called()

    def test_windows_dll_directory_remains_open_until_engine_close(self):
        dll_directory = Mock()
        library = Mock()
        with (
            patch("vntts.tesseract_native.sys.platform", "win32"),
            patch(
                "vntts.tesseract_native.os.add_dll_directory",
                create=True,
                return_value=dll_directory,
            ),
            patch("vntts.tesseract_native.ct.CDLL", return_value=library),
        ):
            loaded, directory = _load_library(
                "configured", "/installed/libtesseract-5.dll"
            )
        self.assertIs(loaded, library)
        self.assertIs(directory, dll_directory)
        dll_directory.close.assert_not_called()


class CombinedOcrInjectionTest(unittest.TestCase):
    def test_combined_callback_supplies_both_initial_and_cropped_dialogue(self):
        initial = {
            "text": ["Alice", "Hello world."],
            "conf": [95, 95],
            "block_num": [1, 1],
            "par_num": [1, 1],
            "line_num": [1, 2],
            "left": [5, 5],
            "top": [5, 40],
            "width": [30, 100],
            "height": [10, 10],
        }
        combined = Mock(
            side_effect=[
                ("Alice\nHello world.\n", initial),
                ("Hello world.\n", {"text": ["Hello world."], "conf": [95]}),
            ]
        )
        text, data = Mock(), Mock()
        result = recognize_dialog_image_result(
            Image.new("L", (200, 100)),
            recognize_text=text,
            recognize_data=data,
            recognize_combined=combined,
            language="eng+rus",
        )
        self.assertEqual(
            (result.character, result.text, result.confidence),
            ("Alice", "Hello world.", 95),
        )
        self.assertEqual(combined.call_count, 2)
        self.assertEqual(combined.call_args.args[1], "eng+rus")
        self.assertIsNone(combined.call_args.args[2]())
        text.assert_not_called()
        data.assert_not_called()

    def test_isolated_dialogue_uses_combined_callback_and_speaker_keeps_psm7(self):
        combined = Mock(
            return_value=("Hello world.\n", {"text": ["Hello world."], "conf": [95]})
        )
        text = Mock()
        data = Mock(return_value={"text": ["Alice"], "conf": [95]})
        regions = OCRRegions(DialogRegion(0, 0, 1, 0.4), DialogRegion(0, 0.5, 1, 0.5))
        with (
            patch("vntts.ocr.resolve_ocr_regions", return_value=regions),
            patch("vntts.ocr.speaker_glyphs_visible", return_value=True),
        ):
            result = recognize_dialog_image_result(
                Image.new("L", (200, 100)),
                recognize_text=text,
                recognize_data=data,
                recognize_combined=combined,
                ocr_regions=regions,
            )
        self.assertEqual((result.character, result.text), ("Alice", "Hello world."))
        combined.assert_called_once()
        data.assert_called_once()
        self.assertEqual(data.call_args.kwargs["config"], tesseract_config(7))
        text.assert_not_called()


if __name__ == "__main__":
    unittest.main()

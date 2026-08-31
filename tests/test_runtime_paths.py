import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytesseract

from vntts.package_self_test import (
    probe_bundled_pocket_runtime,
    run_package_self_test,
)
from vntts.runtime_paths import (
    configure_bundled_dependencies,
    find_bundled_espeak,
    find_bundled_speech_runtime,
    get_bundle_root,
)


class RuntimePathsTest(unittest.TestCase):
    @staticmethod
    def _speech_runtime_report():
        return {"executable": "python", "modules": {}}

    def test_finds_allowlisted_speech_runtime_in_bundle(self):
        with TemporaryDirectory() as temporary_directory:
            bundle_root = Path(temporary_directory)
            runtime = bundle_root / "speech-runtimes" / "pocket-tts"
            runtime.mkdir(parents=True)

            self.assertEqual(
                find_bundled_speech_runtime("pocket-tts", bundle_root),
                runtime.resolve(),
            )
            self.assertIsNone(
                find_bundled_speech_runtime("unknown-backend", bundle_root)
            )

    def test_configures_tesseract_from_frozen_bundle(self):
        with TemporaryDirectory() as temporary_directory:
            bundle_root = Path(temporary_directory)
            tesseract = bundle_root / "tesseract" / "tesseract.exe"
            language_data = bundle_root / "tesseract" / "tessdata" / "eng.traineddata"
            language_data.parent.mkdir(parents=True)
            tesseract.write_bytes(b"exe")
            language_data.write_bytes(b"language")

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", temporary_directory, create=True),
                patch.object(pytesseract.pytesseract, "tesseract_cmd", "tesseract"),
                patch.dict(os.environ, {}, clear=True),
            ):
                configured = configure_bundled_dependencies()

                self.assertEqual(get_bundle_root(), bundle_root.resolve())
                self.assertEqual(configured, tesseract.resolve())
                self.assertEqual(
                    pytesseract.pytesseract.tesseract_cmd,
                    str(tesseract.resolve()),
                )
                self.assertEqual(
                    os.environ["TESSDATA_PREFIX"],
                    str(language_data.parent.resolve()),
                )

    def test_incomplete_bundle_does_not_override_system_tesseract(self):
        with TemporaryDirectory() as temporary_directory:
            with patch.object(
                pytesseract.pytesseract,
                "tesseract_cmd",
                "system-tesseract",
            ):
                configured = configure_bundled_dependencies(temporary_directory)

                self.assertIsNone(configured)
                self.assertEqual(
                    pytesseract.pytesseract.tesseract_cmd,
                    "system-tesseract",
                )

    def test_configures_macos_tesseract_from_frozen_bundle(self):
        with TemporaryDirectory() as temporary_directory:
            bundle_root = Path(temporary_directory)
            tesseract = bundle_root / "tesseract" / "tesseract"
            language_data = bundle_root / "tesseract" / "tessdata" / "eng.traineddata"
            language_data.parent.mkdir(parents=True)
            tesseract.write_bytes(b"executable")
            language_data.write_bytes(b"language")

            with patch.object(pytesseract.pytesseract, "tesseract_cmd", "tesseract"):
                configured = configure_bundled_dependencies(bundle_root)

                self.assertEqual(configured, tesseract)
                self.assertEqual(pytesseract.pytesseract.tesseract_cmd, str(tesseract))

    def test_configures_espeak_path_and_voice_data_from_frozen_bundle(self):
        with TemporaryDirectory() as temporary_directory:
            bundle_root = Path(temporary_directory)
            espeak = bundle_root / "espeak-ng" / "bin" / "espeak-ng.exe"
            espeak_data = bundle_root / "espeak-ng" / "share" / "espeak-ng-data"
            espeak.parent.mkdir(parents=True)
            espeak_data.mkdir(parents=True)
            espeak.write_bytes(b"exe")

            with patch.dict(os.environ, {"PATH": "system-path"}, clear=True):
                configure_bundled_dependencies(bundle_root)

                self.assertEqual(
                    find_bundled_espeak(bundle_root),
                    (espeak, espeak_data),
                )
                self.assertEqual(
                    os.environ["PATH"].split(os.pathsep),
                    [str(espeak.parent), "system-path"],
                )
                self.assertEqual(os.environ["ESPEAK_DATA_PATH"], str(espeak_data))

    def test_frozen_package_self_test_requires_bundled_espeak(self):
        with TemporaryDirectory() as temporary_directory:
            bundle_root = Path(temporary_directory)
            tesseract = bundle_root / "tesseract" / "tesseract.exe"
            language_data = bundle_root / "tesseract" / "tessdata" / "eng.traineddata"
            language_data.parent.mkdir(parents=True)
            tesseract.write_bytes(b"exe")
            language_data.write_bytes(b"language")
            report_path = bundle_root / "report.json"

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", temporary_directory, create=True),
                patch.object(
                    pytesseract.pytesseract,
                    "tesseract_cmd",
                    "tesseract",
                ),
                patch.dict(os.environ, {}, clear=True),
            ):
                successful, _ = run_package_self_test(
                    report_path,
                    import_module=Mock(),
                    tesseract_probe=Mock(return_value="5.5.0"),
                    speech_runtime_probe=Mock(
                        return_value=self._speech_runtime_report()
                    ),
                )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(successful)
            self.assertIn(
                "Bundled eSpeak-NG",
                [
                    check["name"]
                    for check in report["checks"]
                    if check["status"] == "error"
                ],
            )

    def test_finds_macos_espeak_and_voice_data(self):
        with TemporaryDirectory() as temporary_directory:
            bundle_root = Path(temporary_directory)
            espeak = bundle_root / "espeak-ng" / "espeak-ng"
            espeak_data = bundle_root / "espeak-ng" / "espeak-ng-data"
            espeak.parent.mkdir(parents=True)
            espeak_data.mkdir(parents=True)
            espeak.write_bytes(b"executable")

            self.assertEqual(
                find_bundled_espeak(bundle_root),
                (espeak, espeak_data),
            )

    def test_frozen_package_self_test_executes_bundled_espeak_probe(self):
        with TemporaryDirectory() as temporary_directory:
            bundle_root = Path(temporary_directory)
            tesseract = bundle_root / "tesseract" / "tesseract.exe"
            language_data = bundle_root / "tesseract" / "tessdata" / "eng.traineddata"
            espeak = bundle_root / "espeak-ng" / "espeak-ng.exe"
            espeak_data = bundle_root / "espeak-ng" / "espeak-ng-data"
            language_data.parent.mkdir(parents=True)
            espeak_data.mkdir(parents=True)
            tesseract.write_bytes(b"exe")
            language_data.write_bytes(b"language")
            espeak.write_bytes(b"exe")
            report_path = bundle_root / "report.json"
            espeak_probe = Mock(return_value="eSpeak NG 1.52.0")

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", temporary_directory, create=True),
                patch.object(
                    pytesseract.pytesseract,
                    "tesseract_cmd",
                    "tesseract",
                ),
                patch.dict(os.environ, {}, clear=True),
            ):
                successful, _ = run_package_self_test(
                    report_path,
                    import_module=Mock(),
                    tesseract_probe=Mock(return_value="5.5.0"),
                    espeak_probe=espeak_probe,
                    speech_runtime_probe=Mock(
                        return_value=self._speech_runtime_report()
                    ),
                )

            self.assertTrue(successful)
            espeak_probe.assert_called_once_with(espeak.resolve())

    def test_pocket_runtime_probe_rejects_module_outside_bundle(self):
        with TemporaryDirectory() as temporary_directory:
            bundle_root = Path(temporary_directory).resolve()
            runtime = bundle_root / "speech-runtimes/pocket-tts"
            interpreter = runtime / "bin/python"
            site = runtime / "lib/python3.11/site-packages"
            site.mkdir(parents=True)
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            report = {
                "executable": str(interpreter),
                "prefix": str(runtime),
                "base_prefix": str(bundle_root / "speech-runtimes/_python"),
                "modules": {
                    name: str(site / name / "__init__.py")
                    for name in (
                        "durable_file",
                        "numpy",
                        "platformdirs",
                        "pocket_tts",
                        "safetensors",
                        "scipy",
                        "torch",
                        "vntts",
                        "vntts_artifacts",
                    )
                },
            }
            report["modules"]["torch"] = "/developer/.venv/torch/__init__.py"
            runner = Mock(return_value=Mock(stdout=json.dumps(report)))

            with (
                patch(
                    "vntts.package_self_test._runtime_paths",
                    return_value=(runtime, interpreter, site),
                ),
                self.assertRaisesRegex(RuntimeError, "module:torch"),
            ):
                probe_bundled_pocket_runtime(bundle_root, runner)

    def test_package_self_test_writes_machine_readable_report(self):
        with TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "report.json"
            importer = Mock()

            result = run_package_self_test(
                report_path,
                import_module=importer,
                tesseract_probe=Mock(return_value="5.5.0"),
            )
            successful, written_path = result

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(successful)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(written_path, report_path)
            self.assertTrue(report["success"])
            self.assertEqual(
                report["python_executable"],
                str(Path(sys.executable).resolve()),
            )
            self.assertTrue(
                any(check["name"] == "Tesseract OCR" for check in report["checks"])
            )


if __name__ == "__main__":
    unittest.main()

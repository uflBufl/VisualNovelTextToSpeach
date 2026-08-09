import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytesseract

from vntts.package_self_test import run_package_self_test
from vntts.runtime_paths import (
    configure_bundled_dependencies,
    get_bundle_root,
)


class RuntimePathsTest(unittest.TestCase):
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

    def test_package_self_test_writes_machine_readable_report(self):
        with TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "report.json"
            importer = Mock()

            successful, written_path = run_package_self_test(
                report_path,
                import_module=importer,
                tesseract_probe=Mock(return_value="5.5.0"),
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(successful)
            self.assertEqual(written_path, report_path)
            self.assertTrue(report["success"])
            self.assertTrue(
                any(check["name"] == "Tesseract OCR" for check in report["checks"])
            )


if __name__ == "__main__":
    unittest.main()

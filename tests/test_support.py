import json
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from vntts.diagnostics import DiagnosticSnapshot
from vntts.settings import AppSettings
from vntts.support import (
    RuntimeSupportLog,
    SupportBundleBuilder,
    collect_ocr_metrics,
    redact_text,
)


class RuntimeSupportLogTest(unittest.TestCase):
    def test_log_is_bounded_and_returns_a_copy(self):
        log = RuntimeSupportLog(
            maximum_entries=2,
            clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
        log.add("status", "one")
        log.add("status", "two")
        log.add("error", "three")

        entries = log.snapshot()
        entries.clear()

        self.assertEqual(
            [entry["message"] for entry in log.snapshot()], ["two", "three"]
        )

    def test_user_home_is_redacted_from_unix_and_windows_paths(self):
        self.assertNotIn(str(Path.home()), redact_text(Path.home() / "secret"))
        self.assertEqual(
            redact_text(r"C:\Users\Ada\private\settings.json"),
            r"<home>\private\settings.json",
        )

    def test_log_can_persist_redacted_json_lines(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "runtime.log"
            log = RuntimeSupportLog(
                clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
                path=path,
            )

            log.add("error", f"Failed under {Path.home() / 'private'}")

            entry = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(entry["level"], "error")
        self.assertIn("<home>", entry["message"])
        self.assertNotIn(str(Path.home()), entry["message"])


class SupportBundleBuilderTest(unittest.TestCase):
    def test_bundle_excludes_dialog_images_text_and_environment_values(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            diagnostics_directory = directory / "ocr"
            diagnostics_directory.mkdir()
            (diagnostics_directory / "uncertain-one.json").write_text(
                json.dumps(
                    {
                        "character": "PRIVATE CHARACTER",
                        "text": "PRIVATE DIALOGUE",
                        "confidence": 42,
                        "attempts": 3,
                        "preprocessing_profile": "balanced",
                    }
                ),
                encoding="utf-8",
            )
            log = RuntimeSupportLog()
            log.add("status", f"Settings at {Path.home() / 'private'}")
            settings = AppSettings(
                ocr_diagnostics_directory=str(diagnostics_directory),
                screenshot_directory=str(Path.home() / "screenshots"),
            )
            diagnostic = DiagnosticSnapshot(
                Image.new("RGB", (10, 10), "red"),
                character="PRIVATE CHARACTER",
                text="PRIVATE DIALOGUE",
                confidence=42,
                preprocessing_profile="balanced",
                corrections=("PRIVATE -> SECRET",),
            )

            output = SupportBundleBuilder(
                settings,
                log,
                diagnostic=diagnostic,
                dependency_probe=lambda: {"test": "ok"},
            ).build(directory / "support.zip")
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                combined = b"\n".join(archive.read(name) for name in names).decode()
                metrics = json.loads(archive.read("ocr-metrics.json"))

        self.assertEqual(
            names,
            {
                "manifest.json",
                "sanitized-settings.json",
                "runtime-events.json",
                "ocr-metrics.json",
                "diagnostics.json",
                "dependencies.json",
            },
        )
        self.assertNotIn("PRIVATE CHARACTER", combined)
        self.assertNotIn("PRIVATE DIALOGUE", combined)
        self.assertNotIn("PRIVATE -> SECRET", combined)
        self.assertNotIn(str(Path.home()), combined)
        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["average_confidence"], 42)

    def test_ocr_metrics_report_resolved_pending_and_invalid_counts(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "uncertain-pending.json").write_text(
                json.dumps(
                    {
                        "confidence": 40,
                        "attempts": 2,
                        "preprocessing_profile": "balanced",
                    }
                ),
                encoding="utf-8",
            )
            (directory / "uncertain-resolved.json").write_text(
                json.dumps(
                    {
                        "confidence": 60,
                        "attempts": 4,
                        "preprocessing_profile": "balanced",
                        "resolved": True,
                    }
                ),
                encoding="utf-8",
            )
            (directory / "uncertain-invalid.json").write_text(
                "bad json",
                encoding="utf-8",
            )

            metrics = collect_ocr_metrics(directory)

        self.assertEqual(metrics["sample_count"], 2)
        self.assertEqual(metrics["resolved_count"], 1)
        self.assertEqual(metrics["pending_count"], 1)
        self.assertEqual(metrics["invalid_metadata_count"], 1)
        self.assertEqual(metrics["average_confidence"], 50)
        self.assertEqual(metrics["average_attempts"], 3)


if __name__ == "__main__":
    unittest.main()

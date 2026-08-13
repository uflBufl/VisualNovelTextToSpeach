import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.ocr import OCRResult, UncertainFrameRecorder  # noqa: E402
from vntts.ocr_corrections import OCRCorrectionStore  # noqa: E402
from vntts.ocr_review import (  # noqa: E402
    OCR_REVIEW_SCHEMA_VERSION,
    OCRReviewStore,
)
from vntts.ocr_review_ui import OCRReviewDialog  # noqa: E402


def record_uncertain_sample(directory):
    recorder = UncertainFrameRecorder(directory)
    return recorder.record(
        Image.new("RGB", (320, 100), "black"),
        OCRResult("Mareus", "Hello tiniekeeper.", 42.5, "balanced", 3),
        60,
    )


class OCRReviewStoreTest(unittest.TestCase):
    def test_loads_pending_sample_and_preserves_resolution_metadata(self):
        with TemporaryDirectory() as temporary_directory:
            image_path = record_uncertain_sample(temporary_directory)
            store = OCRReviewStore(temporary_directory)

            sample = store.pending_samples()[0]
            store.mark_resolved(
                sample,
                scope="game",
                corrections={"Mareus": "Marcus"},
            )

            metadata = json.loads(
                image_path.with_suffix(".json").read_text(encoding="utf-8")
            )
            pending = store.pending_samples()

        self.assertEqual(sample.character, "Mareus")
        self.assertEqual(sample.text, "Hello tiniekeeper.")
        self.assertEqual(sample.confidence, 42.5)
        self.assertEqual(pending, [])
        self.assertTrue(metadata["resolved"])
        self.assertEqual(metadata["correction_scope"], "game")
        self.assertEqual(metadata["corrections"], {"Mareus": "Marcus"})
        self.assertEqual(metadata["schema_version"], OCR_REVIEW_SCHEMA_VERSION)
        self.assertIn("resolved_at", metadata)

    def test_skips_invalid_metadata_and_missing_images(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "uncertain-invalid.json").write_text(
                "not json",
                encoding="utf-8",
            )
            (directory / "uncertain-missing.json").write_text(
                json.dumps({"image": "missing.png"}),
                encoding="utf-8",
            )

            self.assertEqual(OCRReviewStore(directory).pending_samples(), [])

    def test_future_metadata_schema_is_not_offered_for_review(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            image_path = directory / "future.png"
            image_path.write_bytes(b"image")
            (directory / "uncertain-future.json").write_text(
                json.dumps(
                    {
                        "schema_version": OCR_REVIEW_SCHEMA_VERSION + 1,
                        "image": image_path.name,
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(OCRReviewStore(directory).pending_samples(), [])

    def test_legacy_unversioned_metadata_is_upgraded_when_resolved(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            image_path = directory / "legacy.png"
            image_path.write_bytes(b"image")
            metadata_path = directory / "uncertain-legacy.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "image": image_path.name,
                        "character": "Narrator",
                        "text": "Legacy sample",
                        "confidence": 40,
                        "minimum_confidence": 60,
                        "preprocessing_profile": "balanced",
                        "attempts": 1,
                    }
                ),
                encoding="utf-8",
            )
            store = OCRReviewStore(directory)

            sample = store.pending_samples()[0]
            store.mark_resolved(sample)
            upgraded = json.loads(metadata_path.read_text(encoding="utf-8"))

        self.assertEqual(upgraded["schema_version"], OCR_REVIEW_SCHEMA_VERSION)
        self.assertTrue(upgraded["resolved"])


class OCRReviewDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_saves_profile_corrections_reloads_runtime_and_resolves_sample(self):
        with TemporaryDirectory() as temporary_directory:
            review_directory = Path(temporary_directory) / "review"
            record_uncertain_sample(review_directory)
            correction_store = OCRCorrectionStore(
                Path(temporary_directory) / "corrections.json"
            )
            corrections_changed = Mock()
            dialog = OCRReviewDialog(
                review_directory,
                correction_store,
                "game",
                "Reverse: 1999",
                corrections_changed,
            )
            dialog.corrected_character.setText("Marcus")
            dialog.corrected_text.setPlainText("Hello timekeeper.")

            dialog.save_correction()

            loaded = OCRCorrectionStore.load(correction_store.path)
            pending = OCRReviewStore(review_directory).pending_samples()

        self.assertEqual(
            loaded.profile_entries["game"],
            {
                "Mareus": "Marcus",
                "Hello tiniekeeper.": "Hello timekeeper.",
            },
        )
        self.assertEqual(pending, [])
        corrections_changed.assert_called_once_with()
        self.assertEqual(dialog.sample_list.count(), 0)
        dialog.deleteLater()

    def test_can_resolve_sample_without_creating_a_rule(self):
        with TemporaryDirectory() as temporary_directory:
            review_directory = Path(temporary_directory) / "review"
            record_uncertain_sample(review_directory)
            correction_store = OCRCorrectionStore(
                Path(temporary_directory) / "corrections.json"
            )
            dialog = OCRReviewDialog(review_directory, correction_store)

            dialog.resolve_without_correction()

        self.assertEqual(correction_store.global_entries, {})
        self.assertEqual(dialog.sample_list.count(), 0)
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import Qt, QThreadPool, QTimer  # noqa: E402
from PySide6.QtGui import QCloseEvent, QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialogButtonBox,
    QFormLayout,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
)

from tests.qt_task_fixtures import (  # noqa: E402
    ManualThreadPool,
    release_and_wait_for_pool,
)
from tests.symlink_support import symlink_or_skip  # noqa: E402
from vntts.ocr import OCRResult, UncertainFrameRecorder  # noqa: E402
from vntts.ocr_corrections import OCRCorrectionStore  # noqa: E402
from vntts.ocr_review import (  # noqa: E402
    OCR_REVIEW_SCHEMA_VERSION,
    OCRReviewStore,
)
from vntts.ocr_review_ui import OCRReviewDialog  # noqa: E402
from vntts.versioned_json import (  # noqa: E402
    StaleDocumentError,
    read_versioned_json_snapshot,
)


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

    def test_skips_images_outside_review_directory(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "review"
            directory.mkdir()
            outside = root / "outside.png"
            outside.write_bytes(b"image")
            for index, image in enumerate(("../outside.png", str(outside))):
                (directory / f"uncertain-outside-{index}.json").write_text(
                    json.dumps({"image": image}),
                    encoding="utf-8",
                )

            self.assertEqual(OCRReviewStore(directory).pending_samples(), [])

    def test_skips_symlinked_metadata_and_images(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "review"
            directory.mkdir()
            outside_image = root / "outside.png"
            outside_image.write_bytes(b"image")
            symlink_or_skip(directory / "linked.png", outside_image)
            (directory / "uncertain-linked-image.json").write_text(
                json.dumps({"image": "linked.png"}), encoding="utf-8"
            )

            inside_image = directory / "inside.png"
            inside_image.write_bytes(b"image")
            outside_metadata = root / "outside.json"
            outside_metadata.write_text(
                json.dumps({"image": inside_image.name}), encoding="utf-8"
            )
            symlink_or_skip(
                directory / "uncertain-linked-metadata.json", outside_metadata
            )

            self.assertEqual(OCRReviewStore(directory).pending_samples(), [])

    def test_skips_invalid_numeric_metadata(self):
        cases = (
            ("confidence", True),
            ("confidence", "NaN"),
            ("confidence", 10**400),
            ("minimum_confidence", "Infinity"),
            ("minimum_confidence", 10**400),
            ("attempts", True),
            ("attempts", 1.5),
            ("attempts", -1),
        )
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            image = directory / "sample.png"
            image.write_bytes(b"image")
            for index, (field, value) in enumerate(cases):
                payload = {
                    "image": image.name,
                    "confidence": 40,
                    "minimum_confidence": 60,
                    "attempts": 1,
                    field: value,
                }
                (directory / f"uncertain-invalid-number-{index}.json").write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )

            valid = directory / "uncertain-000-valid.json"
            valid.write_text(
                json.dumps(
                    {"image": image.name, "confidence": "42.5", "attempts": "1"}
                ),
                encoding="utf-8",
            )
            samples = OCRReviewStore(directory).pending_samples()

            self.assertEqual([sample.metadata_path for sample in samples], [valid])
            self.assertEqual(samples[0].confidence, 42.5)
            self.assertEqual(
                len(list(directory.glob("uncertain-invalid-number-*.json"))), len(cases)
            )

    def test_skips_nonstring_text_metadata_without_changing_files(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "sample.png").write_bytes(b"image")
            originals = {}
            for field in ("character", "text", "preprocessing_profile"):
                for index, value in enumerate((123, 0, 1.5, True, False, [], {})):
                    path = directory / f"uncertain-{field}-{index}.json"
                    raw = json.dumps({"image": "sample.png", field: value})
                    path.write_text(raw, encoding="utf-8")
                    originals[path] = raw

            self.assertEqual(OCRReviewStore(directory).pending_samples(), [])
            for path, raw in originals.items():
                self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_preserves_optional_text_defaults_and_literal_strings(self):
        cases = (
            ({}, ("Narrator", "", "unknown")),
            (
                {
                    field: None
                    for field in ("character", "text", "preprocessing_profile")
                },
                ("Narrator", "", "unknown"),
            ),
            (
                {field: "" for field in ("character", "text", "preprocessing_profile")},
                ("Narrator", "", "unknown"),
            ),
            (
                {
                    "character": "123",
                    "text": "  Hello.  ",
                    "preprocessing_profile": "fast",
                },
                ("123", "  Hello.  ", "fast"),
            ),
        )
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "sample.png").write_bytes(b"image")
            for index, (fields, expected) in enumerate(cases):
                with self.subTest(fields=fields):
                    path = directory / f"uncertain-{index}.json"
                    path.write_text(
                        json.dumps({"image": "sample.png", **fields}), encoding="utf-8"
                    )
                    samples = OCRReviewStore(directory).pending_samples()
                    sample = next(
                        item for item in samples if item.metadata_path == path
                    )
                    self.assertEqual(
                        (sample.character, sample.text, sample.preprocessing_profile),
                        expected,
                    )

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

    def test_stale_sample_cannot_resolve_replaced_metadata(self):
        with TemporaryDirectory() as temporary_directory:
            store = OCRReviewStore(temporary_directory)
            record_uncertain_sample(temporary_directory)
            sample = store.pending_samples()[0]
            replacement = json.loads(sample.metadata_path.read_text(encoding="utf-8"))
            replacement["text"] = "A newer observation."
            sample.metadata_path.write_text(json.dumps(replacement), encoding="utf-8")

            with self.assertRaisesRegex(StaleDocumentError, "changed"):
                store.mark_resolved(sample)

            preserved = json.loads(sample.metadata_path.read_text(encoding="utf-8"))

        self.assertEqual(preserved["text"], "A newer observation.")
        self.assertNotIn("resolved", preserved)

    def test_concurrent_metadata_edit_is_not_overwritten(self):
        with TemporaryDirectory() as temporary_directory:
            store = OCRReviewStore(temporary_directory)
            record_uncertain_sample(temporary_directory)
            sample = store.pending_samples()[0]

            def read_then_edit(path, **_kwargs):
                payload, revision = read_versioned_json_snapshot(
                    path,
                    schema_version=OCR_REVIEW_SCHEMA_VERSION,
                    document_name="OCR review metadata",
                    allow_unversioned=True,
                )
                path.write_text(
                    json.dumps({**payload, "note": "concurrent edit"}),
                    encoding="utf-8",
                )
                return payload, revision

            with (
                patch(
                    "vntts.ocr_review.read_versioned_json_snapshot",
                    side_effect=read_then_edit,
                ),
                self.assertRaisesRegex(OSError, "changed on disk"),
            ):
                store.mark_resolved(sample)

            preserved = json.loads(sample.metadata_path.read_text(encoding="utf-8"))

        self.assertEqual(preserved["note"], "concurrent edit")
        self.assertNotIn("resolved", preserved)

    def test_resolution_uses_the_revision_of_the_decoded_snapshot(self):
        with TemporaryDirectory() as directory:
            store = OCRReviewStore(directory)
            record_uncertain_sample(directory)
            sample_a = store.pending_samples()[0]
            snapshot_a = sample_a.metadata_path.read_bytes()
            payload_b = json.loads(snapshot_a)
            payload_b["text"] = "A concurrent observation."
            sample_a.metadata_path.write_text(json.dumps(payload_b), encoding="utf-8")
            sample_b = store.pending_samples()[0]
            snapshot_b = sample_b.metadata_path.read_bytes()
            sample_b.metadata_path.write_bytes(snapshot_a)

            def read_b_then_restore_a(path, **_kwargs):
                path.write_bytes(snapshot_b)
                payload, revision = read_versioned_json_snapshot(
                    path,
                    schema_version=OCR_REVIEW_SCHEMA_VERSION,
                    document_name="OCR review metadata",
                    allow_unversioned=True,
                )
                path.write_bytes(snapshot_a)
                return payload, revision

            with (
                patch(
                    "vntts.ocr_review.read_versioned_json_snapshot",
                    side_effect=read_b_then_restore_a,
                ),
                self.assertRaisesRegex(StaleDocumentError, "changed on disk"),
            ):
                store.mark_resolved(sample_b)

            restored = json.loads(sample_b.metadata_path.read_text(encoding="utf-8"))

        self.assertEqual(restored["text"], sample_a.text)
        self.assertNotIn("resolved", restored)


class OCRReviewDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("Timed out waiting for OCR review write")

    def test_blank_source_is_not_savable_but_other_field_can_be_corrected(self):
        with TemporaryDirectory() as directory:
            UncertainFrameRecorder(directory).record(
                Image.new("RGB", (320, 100), "black"),
                OCRResult("Mareus", "", 42, "balanced", 1),
                60,
            )
            store = OCRCorrectionStore(Path(directory) / "rules.json")
            dialog = OCRReviewDialog(directory, store)
            dialog.corrected_text.setPlainText("New dialogue")
            self.assertFalse(dialog.save_button.isEnabled())
            self.assertIn("detected text to match", dialog.impact.text())
            dialog.save_correction()
            self.assertFalse(dialog._write_active)
            self.assertFalse(store.path.exists())
            dialog.corrected_text.clear()
            dialog.corrected_character.setText("Marcus")
            dialog.save_correction()
            self.wait_for(lambda: not dialog._write_active)
            self.assertEqual(store.global_entries, {"Mareus": "Marcus"})
            dialog.deleteLater()

    def test_stale_rules_disable_writes_even_after_editing_and_switching(self):
        with TemporaryDirectory() as directory:
            record_uncertain_sample(directory)
            store = OCRCorrectionStore(Path(directory) / "rules.json")
            dialog = OCRReviewDialog(directory, store)
            OCRCorrectionStore.load(store.path).upsert_entries({"Other": "Current"})
            speaker_draft = "<b>Marcus of the Northern Library</b> & " * 20
            text_draft = "<b>The complete drafted dialogue</b> & " * 60
            dialog.corrected_character.setText(speaker_draft)
            dialog.corrected_text.setPlainText(text_draft)
            dialog.save_correction()
            self.wait_for(lambda: not dialog._write_active)
            self.assertIn("reopen OCR review", dialog.status.toPlainText())
            self.assertFalse(dialog.save_button.isEnabled())
            self.assertFalse(dialog.resolve_button.isEnabled())
            speaker_draft += "Another draft"
            dialog.corrected_character.setText(speaker_draft)
            dialog.show_sample(0)
            self.assertFalse(dialog.save_button.isEnabled())
            self.assertFalse(dialog.resolve_button.isEnabled())
            dialog.save_correction()
            dialog.resolve_without_correction()
            self.assertFalse(dialog._write_active)
            for editor, expected in (
                (dialog.corrected_character, speaker_draft),
                (dialog.corrected_text, text_draft),
            ):
                editor.setFocus()
                editor.selectAll()
                QTest.keySequence(editor, QKeySequence.StandardKey.Copy)
                self.assertEqual(self.application.clipboard().text(), expected)
            self.assertEqual(
                OCRCorrectionStore.load(store.path).global_entries, {"Other": "Current"}
            )
            dialog.deleteLater()

    def test_long_literal_values_and_error_remain_copyable_at_large_font(self):
        with TemporaryDirectory() as directory:
            source = "<b>literal dialogue</b> & " * 60
            speaker = "<b>Speaker from the Northern Library</b> & " * 20
            UncertainFrameRecorder(directory).record(
                Image.new("RGB", (320, 100), "black"),
                OCRResult(speaker, source, 42, "balanced", 1),
                60,
            )
            dialog = OCRReviewDialog(directory)
            font = dialog.font()
            font.setPointSize(22)
            dialog.setFont(font)
            dialog.resize(730, 560)
            dialog.show()
            self.application.processEvents()
            self.assertEqual(
                dialog.source_character.textFormat(), Qt.TextFormat.PlainText
            )
            dialog.source_character.setFocus()
            dialog.source_character.setSelection(0, len(speaker))
            QTest.keySequence(dialog.source_character, QKeySequence.StandardKey.Copy)
            self.assertEqual(self.application.clipboard().text(), speaker)
            self.assertEqual(dialog.source_text.toPlainText(), source)
            dialog.source_text.setFocus()
            dialog.source_text.selectAll()
            QTest.keySequence(dialog.source_text, QKeySequence.StandardKey.Copy)
            self.assertEqual(self.application.clipboard().text(), source)
            error = "<b>Disk error</b> " * 100
            dialog._write_finished(None, OSError(error))
            self.application.processEvents()
            self.assertEqual(dialog.size().width(), 730)
            self.assertEqual(dialog.size().height(), 560)
            self.assertEqual(
                dialog.form.rowWrapPolicy(), QFormLayout.RowWrapPolicy.WrapAllRows
            )
            self.assertTrue(dialog.save_button.isDefault())
            self.assertGreater(dialog.details_scroll.verticalScrollBar().maximum(), 0)
            self.assertGreater(dialog.status.verticalScrollBar().maximum(), 0)
            self.assertIn(error, dialog.status.toPlainText())
            dialog.status.setFocus()
            dialog.status.selectAll()
            QTest.keySequence(dialog.status, QKeySequence.StandardKey.Copy)
            self.assertEqual(
                self.application.clipboard().text(), dialog.status.toPlainText()
            )
            self.assertTrue(
                dialog.rect().contains(dialog.save_button.geometry().bottomRight())
            )
            dialog.close()
            dialog.deleteLater()

    def test_resolving_does_not_resize_window_and_bad_image_has_explanation(self):
        with TemporaryDirectory() as directory:
            record_uncertain_sample(directory)
            dialog = OCRReviewDialog(directory)
            dialog.resize(730, 560)
            dialog.show()
            self.application.processEvents()
            sample = dialog.current_sample()
            sample.image_path.write_bytes(b"invalid png")
            dialog.show_sample(0)
            self.assertFalse(dialog.zoom_button.isEnabled())
            self.assertIn("unavailable", dialog.preview.text())
            dialog.reload_samples()
            self.application.processEvents()
            self.assertEqual(dialog.width(), 730)
            dialog._start_write(
                dialog.review_store.mark_resolved, sample, applies_corrections=False
            )
            self.wait_for(lambda: not dialog._write_active)
            self.assertEqual(dialog.size().width(), 730)
            self.assertEqual(dialog.size().height(), 560)
            self.assertTrue(dialog.empty_message.isVisible())
            self.assertFalse(dialog.save_button.isVisible())
            dialog.close()
            dialog.deleteLater()

    def test_long_dialogue_editors_grow_with_window_and_shrink_for_short_text(self):
        with TemporaryDirectory() as directory:
            UncertainFrameRecorder(directory).record(
                Image.new("RGB", (320, 100), "black"),
                OCRResult(
                    "Marcus", "The long literal dialogue. " * 80, 42, "balanced", 1
                ),
                60,
            )
            dialog = OCRReviewDialog(directory)
            dialog.show()
            self.application.processEvents()
            editors = (dialog.source_text, dialog.corrected_text)
            self.wait_for(
                lambda: all(
                    editor.height() > editor.minimumHeight() for editor in editors
                )
            )
            heights = tuple(editor.height() for editor in editors)
            for editor in editors:
                self.assertGreater(editor.height(), editor.minimumHeight())
            self.assertEqual(
                dialog.source_character.sizePolicy().verticalPolicy(),
                QSizePolicy.Policy.Maximum,
            )
            dialog.resize(960, 900)
            self.wait_for(
                lambda: all(
                    editor.height() > previous_height
                    for editor, previous_height in zip(editors, heights, strict=True)
                )
            )
            for editor, previous_height in zip(editors, heights, strict=True):
                self.assertGreater(editor.height(), previous_height)
            for editor in editors:
                old_maximum = editor.maximumHeight()
                editor.setPlainText("Short dialogue.")
                self.wait_for(lambda: editor.maximumHeight() == editor.minimumHeight())
                self.assertLess(editor.maximumHeight(), old_maximum)
                self.assertEqual(editor.maximumHeight(), editor.minimumHeight())
            dialog.close()
            dialog.deleteLater()

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
            self.assertFalse(dialog.save_button.isEnabled())
            dialog.corrected_character.setText("Marcus")
            self.assertTrue(dialog.save_button.isEnabled())
            dialog.corrected_text.setPlainText("Hello timekeeper.")

            dialog.save_correction()
            self.wait_for(lambda: not dialog._write_active)

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

            with patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.Cancel,
            ):
                dialog.resolve_without_correction()
            self.assertFalse(dialog._write_active)
            self.assertEqual(dialog.progress.text(), "Line 1 of 1")
            self.assertEqual(len(OCRReviewStore(review_directory).pending_samples()), 1)
            confirmation = dialog._dismissal_dialog()
            self.assertEqual(
                confirmation.button(QMessageBox.StandardButton.Cancel).text(),
                "Keep reviewing",
            )
            confirmation.deleteLater()
            with patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog.resolve_without_correction()
            self.wait_for(lambda: not dialog._write_active)

        self.assertEqual(correction_store.global_entries, {})
        self.assertEqual(dialog.sample_list.count(), 0)
        self.assertEqual(dialog.progress.text(), "0 lines to review")
        dialog.deleteLater()

    def test_stale_sample_requests_reopen_instead_of_retry(self):
        with TemporaryDirectory() as temporary_directory:
            review_directory = Path(temporary_directory) / "review"
            record_uncertain_sample(review_directory)
            dialog = OCRReviewDialog(review_directory)
            sample = dialog.current_sample()
            self.assertIsNotNone(sample)
            replacement = json.loads(sample.metadata_path.read_text(encoding="utf-8"))
            replacement["text"] = "A newer observation."
            sample.metadata_path.write_text(json.dumps(replacement), encoding="utf-8")

            with patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog.resolve_without_correction()
            self.wait_for(lambda: not dialog._write_active)

            self.assertIn("reopen OCR review", dialog.status.toPlainText())
            self.assertNotIn("button to retry", dialog.status.toPlainText())
            self.assertEqual(
                OCRReviewStore(review_directory).pending_samples()[0].text,
                "A newer observation.",
            )
            dialog.deleteLater()

    def test_failed_save_keeps_draft_and_partial_rule_refreshes_runtime(self):
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
            dialog.review_store.mark_resolved = Mock(
                side_effect=OSError("Review directory is read-only")
            )

            dialog.save_correction()
            self.wait_for(lambda: not dialog._write_active)

            self.assertEqual(dialog.corrected_character.text(), "Marcus")
            self.assertEqual(dialog.corrected_text.toPlainText(), "Hello timekeeper.")
            self.assertTrue(dialog.save_button.isEnabled())
            self.assertIn(
                "Rule saved, but this line is still in review",
                dialog.status.toPlainText(),
            )
            self.assertEqual(len(OCRReviewStore(review_directory).pending_samples()), 1)
            self.assertEqual(
                OCRCorrectionStore.load(correction_store.path).profile_entries["game"],
                {
                    "Mareus": "Marcus",
                    "Hello tiniekeeper.": "Hello timekeeper.",
                },
            )
            corrections_changed.assert_called_once_with()
            dialog.review_store.mark_resolved = OCRReviewStore(
                review_directory
            ).mark_resolved
            dialog.save_correction()
            self.wait_for(lambda: not dialog._write_active)
            self.assertEqual(OCRReviewStore(review_directory).pending_samples(), [])
            self.assertEqual(len(correction_store.profile_entries["game"]), 2)
            dialog.deleteLater()

    def test_empty_state_and_scope_explain_impact(self):
        with TemporaryDirectory() as temporary_directory:
            dialog = OCRReviewDialog(temporary_directory)
            self.assertTrue(dialog.empty_message.isVisible() or not dialog.isVisible())
            self.assertFalse(dialog.details_panel.isVisible())
            self.assertFalse(dialog.save_button.isEnabled())
            record_uncertain_sample(temporary_directory)
            dialog.reload_samples()
            dialog.corrected_character.setText("Marcus")
            self.assertIn("Future OCR for all games", dialog.impact.text())
            self.assertNotIn("Mareus", dialog.impact.text())
            self.assertIn("removes this line", dialog.impact.text())
            zoom = dialog._screenshot_dialog()
            self.assertIsNotNone(zoom)
            self.assertGreater(
                zoom.findChild(QScrollArea).widget().pixmap().width(),
                320,
            )
            zoom.deleteLater()
            dialog.deleteLater()

    def test_partial_failure_after_close_can_dismiss_without_removing_saved_rules(self):
        with TemporaryDirectory() as directory:
            record_uncertain_sample(directory)
            store = OCRCorrectionStore(Path(directory) / "rules.json")
            pool = ManualThreadPool()
            refreshed = Mock()
            dialog = OCRReviewDialog(
                directory, store, corrections_changed=refreshed, thread_pool=pool
            )
            dialog.show()
            dialog.corrected_character.setText("Marcus")
            dialog.corrected_text.setPlainText("Hello timekeeper.")
            original_resolve = dialog.review_store.mark_resolved
            dialog.review_store.mark_resolved = Mock(side_effect=OSError("read-only"))
            dialog.save_correction()
            dialog.close()
            self.assertTrue(dialog._close_pending)
            pool.run_next()
            self.application.processEvents()

            self.assertTrue(dialog.isVisible())
            self.assertFalse(dialog._close_pending)
            self.assertFalse(dialog._write_active)
            self.assertEqual(dialog.corrected_character.text(), "Marcus")
            self.assertEqual(dialog.corrected_text.toPlainText(), "Hello timekeeper.")
            self.assertTrue(dialog.save_button.isEnabled())
            self.assertTrue(dialog.resolve_button.isEnabled())
            self.assertIn("Rule saved", dialog.status.toPlainText())
            saved_rules = OCRCorrectionStore.load(store.path).global_entries
            self.assertEqual(len(saved_rules), 2)
            refreshed.assert_called_once_with()
            confirmation = dialog._dismissal_dialog()
            self.assertIn("keeps any rules already saved", confirmation.text())
            confirmation.deleteLater()

            dialog.review_store.mark_resolved = original_resolve
            with patch.object(
                QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes
            ):
                dialog.resolve_without_correction()
            pool.run_next()
            self.application.processEvents()
            self.assertEqual(OCRReviewStore(directory).pending_samples(), [])
            self.assertEqual(
                OCRCorrectionStore.load(store.path).global_entries, saved_rules
            )
            refreshed.assert_called_once_with()
            dialog.close()
            dialog.deleteLater()

    def assert_review_tab_reaches_fields_and_footer(self, dialog, *, locked):
        close_button = dialog.findChild(QDialogButtonBox).button(
            QDialogButtonBox.StandardButton.Close
        )
        required = {
            dialog.source_text,
            dialog.corrected_character,
            dialog.corrected_text,
            dialog.scope,
            dialog.status,
            close_button,
        }
        if not locked:
            required.update((dialog.save_button, dialog.resolve_button))
        dialog.source_text.setFocus()
        visited = set()
        for _ in range(40):
            current = self.application.focusWidget()
            visited.add(current)
            QTest.keyClick(current, Qt.Key.Key_Tab)
            self.application.processEvents()
        if not locked:
            for _ in range(20):
                current = self.application.focusWidget()
                if current is dialog.save_button:
                    break
                QTest.keyClick(current, Qt.Key.Key_Tab)
            self.assertIs(self.application.focusWidget(), dialog.save_button)
            QTest.keyClick(dialog.save_button, Qt.Key.Key_Left)
            visited.add(self.application.focusWidget())
            self.assertIs(self.application.focusWidget(), close_button)
        self.assertFalse(
            required - visited,
            [widget.text() for widget in required - visited],
        )
        self.assertTrue(
            dialog.rect().contains(
                close_button.mapTo(dialog, close_button.rect().bottomRight())
            )
        )

    def test_long_errors_preserve_full_copy_and_keyboard_access_at_22pt(self):
        for failure in ("ordinary", "partial", "stale"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                record_uncertain_sample(directory)
                pool = ManualThreadPool()
                store = OCRCorrectionStore(Path(directory) / "rules.json")
                dialog = OCRReviewDialog(directory, store, thread_pool=pool)
                font = dialog.font()
                font.setPointSize(22)
                dialog.setFont(font)
                dialog.resize(730, 560)
                dialog.show()
                speaker = "<b>Correct speaker</b> & " * 20
                text = "<b>Correct full dialogue</b> & " * 60
                dialog.corrected_character.setText(speaker)
                dialog.corrected_text.setPlainText(text)
                detail = (
                    "<b>Access denied</b>: /Volumes/Long recordings/" * 30
                ) + " END OF ERROR"
                error = (
                    StaleDocumentError(detail)
                    if failure == "stale"
                    else OSError(detail)
                )
                owner = dialog.review_store if failure == "partial" else store
                method = "mark_resolved" if failure == "partial" else "upsert_entries"
                with patch.object(owner, method, side_effect=error):
                    dialog.save_correction()
                    pool.run_next()
                    self.application.processEvents()
                self.assertIn(detail, dialog.status.toPlainText())
                self.assertGreater(dialog.status.verticalScrollBar().maximum(), 0)
                for editor, expected in (
                    (dialog.corrected_character, speaker),
                    (dialog.corrected_text, text),
                    (dialog.status, dialog.status.toPlainText()),
                ):
                    editor.setFocus()
                    editor.selectAll()
                    QTest.keySequence(editor, QKeySequence.StandardKey.Copy)
                    self.assertEqual(self.application.clipboard().text(), expected)
                status_bar = dialog.status.verticalScrollBar()
                status_bar.setValue(status_bar.maximum())
                self.assertEqual(status_bar.value(), status_bar.maximum())
                dialog.details_scroll.ensureWidgetVisible(dialog.impact)
                self.assert_review_tab_reaches_fields_and_footer(
                    dialog, locked=failure == "stale"
                )
                self.assertEqual(dialog.corrected_text.toPlainText(), text)
                dialog.close()
                dialog.deleteLater()

    def test_single_field_correction_and_scope(self):
        with TemporaryDirectory() as temporary_directory:
            record_uncertain_sample(temporary_directory)
            store = OCRCorrectionStore(Path(temporary_directory) / "rules.json")
            dialog = OCRReviewDialog(
                temporary_directory, store, "game", "Reverse: 1999"
            )
            dialog.corrected_character.setText("Marcus")
            self.assertTrue(dialog.save_button.isEnabled())
            self.assertIn("the Reverse: 1999 profile", dialog.impact.text())
            dialog.save_correction()
            self.wait_for(lambda: not dialog._write_active)
            self.assertEqual(store.profile_entries["game"], {"Mareus": "Marcus"})
            dialog.deleteLater()

    def test_large_screenshot_can_scroll_both_axes(self):
        with TemporaryDirectory() as temporary_directory:
            UncertainFrameRecorder(temporary_directory).record(
                Image.new("RGB", (1200, 1000), "black"),
                OCRResult("Mareus", "Hello tiniekeeper.", 42.5, "balanced", 3),
                60,
            )
            dialog = OCRReviewDialog(temporary_directory)
            zoom = dialog._screenshot_dialog()
            self.assertIsNotNone(zoom)
            zoom.show()
            self.application.processEvents()
            scroll = zoom.findChild(QScrollArea)
            self.assertGreater(scroll.horizontalScrollBar().maximum(), 0)
            self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
            for at_end in (True, False):
                for bar in (scroll.horizontalScrollBar(), scroll.verticalScrollBar()):
                    bar.setValue(bar.maximum() if at_end else 0)
                    self.assertEqual(bar.value(), bar.maximum() if at_end else 0)
                self.application.processEvents()
                image_corner = (
                    scroll.widget().rect().bottomRight()
                    if at_end
                    else scroll.widget().rect().topLeft()
                )
                self.assertTrue(
                    scroll.viewport()
                    .rect()
                    .contains(scroll.widget().mapTo(scroll.viewport(), image_corner))
                )
            zoom.close()
            dialog.deleteLater()

    def test_text_only_rule_applies_to_other_game_when_all_games_selected(self):
        with TemporaryDirectory() as temporary_directory:
            record_uncertain_sample(temporary_directory)
            store = OCRCorrectionStore(Path(temporary_directory) / "rules.json")
            dialog = OCRReviewDialog(
                temporary_directory, store, "game", "Reverse: 1999"
            )
            dialog.corrected_text.setPlainText("Hello timekeeper.")
            dialog.scope.setCurrentIndex(0)
            self.assertTrue(dialog.save_button.isEnabled())
            self.assertIn("Future OCR for all games", dialog.impact.text())
            dialog.save_correction()
            self.wait_for(lambda: not dialog._write_active)
            self.assertEqual(
                store.global_entries,
                {"Hello tiniekeeper.": "Hello timekeeper."},
            )
            result = store.dictionary_for("another-game").correct_result(
                OCRResult("Mareus", "Hello tiniekeeper.", 95, "balanced", 1)
            )
            self.assertEqual(result.text, "Hello timekeeper.")
            dialog.deleteLater()

    def test_same_source_cannot_save_conflicting_replacements(self):
        with TemporaryDirectory() as temporary_directory:
            UncertainFrameRecorder(temporary_directory).record(
                Image.new("RGB", (320, 100), "black"),
                OCRResult("No", "No", 42.5, "balanced", 3),
                60,
            )
            store = OCRCorrectionStore(Path(temporary_directory) / "rules.json")
            dialog = OCRReviewDialog(temporary_directory, store)
            dialog.corrected_character.setText("Narrator")
            dialog.corrected_text.setPlainText("Yes")
            self.assertFalse(dialog.save_button.isEnabled())
            self.assertIn("same replacement", dialog.impact.text())
            dialog.save_correction()
            self.assertFalse(store.path.exists())

            dialog.corrected_text.setPlainText("Narrator")
            self.assertTrue(dialog.save_button.isEnabled())
            dialog.save_correction()
            self.wait_for(lambda: not dialog._write_active)
            self.assertEqual(store.global_entries, {"No": "Narrator"})
            dialog.deleteLater()

    def test_save_disables_competing_edits_and_failed_deferred_close_keeps_draft(self):
        release = Event()
        pool = QThreadPool()
        dialog = None
        with (
            TemporaryDirectory() as temporary_directory,
            release_and_wait_for_pool(
                pool,
                release,
                finish=lambda: self.wait_for(
                    lambda: dialog is None or (not dialog._write_active)
                ),
            ),
        ):
            record_uncertain_sample(temporary_directory)
            dialog = OCRReviewDialog(temporary_directory, thread_pool=pool)
            dialog.corrected_character.setText("Marcus")
            started = Event()

            def fail_late(*_args):
                started.set()
                release.wait(3)
                raise OSError("read-only")

            dialog.correction_store.upsert_entries = fail_late
            dialog.save_correction()
            self.wait_for(started.is_set)
            self.assertFalse(dialog.sample_list.isEnabled())
            self.assertFalse(dialog.scope.isEnabled())
            self.assertFalse(dialog.corrected_character.isEnabled())
            self.assertFalse(dialog.corrected_text.isEnabled())
            close_event = QCloseEvent()
            dialog.closeEvent(close_event)
            self.assertFalse(close_event.isAccepted())
            release.set()
            self.wait_for(lambda: not dialog._write_active)
            self.assertEqual(dialog.corrected_character.text(), "Marcus")
            self.assertTrue(dialog.save_button.isEnabled())
            self.assertIn("Could not finish review", dialog.status.toPlainText())
            dialog.deleteLater()

    def test_switching_samples_preserves_unsaved_draft(self):
        with TemporaryDirectory() as temporary_directory:
            record_uncertain_sample(temporary_directory)
            for speaker in ("Other", "Third"):
                UncertainFrameRecorder(temporary_directory).record(
                    Image.new("RGB", (320, 100), "black"),
                    OCRResult(speaker, f"{speaker} phrase.", 41, "balanced", 2),
                    60,
                )
            dialog = OCRReviewDialog(
                temporary_directory, profile_id="game", profile_name="Current game"
            )
            dialog.scope.setCurrentIndex(0)
            samples = tuple(dialog.samples)
            for index in range(3):
                dialog.sample_list.setCurrentRow(index)
                dialog.corrected_character.setText(f"Edited speaker {index}")
                dialog.corrected_text.setPlainText(f"Edited phrase {index}")
            for index in (0, 2, 1, 0):
                dialog.sample_list.setCurrentRow(index)
                self.assertEqual(dialog.current_sample(), samples[index])
                self.assertEqual(
                    dialog.corrected_character.text(), f"Edited speaker {index}"
                )
                self.assertEqual(
                    dialog.corrected_text.toPlainText(), f"Edited phrase {index}"
                )
                self.assertIsNone(dialog.scope.currentData())
            dialog.deleteLater()

    def test_slow_resolution_keeps_qt_responsive_and_defers_close(self):
        release = Event()
        pool = QThreadPool()
        dialog = None
        with (
            TemporaryDirectory() as temporary_directory,
            release_and_wait_for_pool(
                pool,
                release,
                finish=lambda: self.wait_for(
                    lambda: dialog is None or (not dialog._write_active)
                ),
            ),
        ):
            review_directory = Path(temporary_directory) / "review"
            record_uncertain_sample(review_directory)
            dialog = OCRReviewDialog(review_directory, thread_pool=pool)
            original = dialog.review_store.mark_resolved
            started = Event()

            def slow_resolve(*args, **kwargs):
                started.set()
                release.wait(3)
                return original(*args, **kwargs)

            dialog.review_store.mark_resolved = slow_resolve
            heartbeat = []
            QTimer.singleShot(0, lambda: heartbeat.append("painted"))
            before = time.monotonic()
            with patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog.resolve_without_correction()
            elapsed = time.monotonic() - before
            self.wait_for(lambda: started.is_set() and bool(heartbeat))

            self.assertLess(elapsed, 0.1)
            self.assertTrue(dialog._write_active)
            self.assertFalse(dialog.resolve_button.isEnabled())
            self.assertIn("background", dialog.status.toPlainText())
            dialog.reject()
            self.assertTrue(dialog._close_pending)
            self.assertIn(
                "will close when saving finishes", dialog.status.toPlainText()
            )
            close_event = QCloseEvent()
            dialog.closeEvent(close_event)
            self.assertFalse(close_event.isAccepted())
            rejected = []
            finished = []
            dialog.rejected.connect(lambda: rejected.append(True))
            dialog.finished.connect(lambda _result: finished.append(True))
            dialog.show()
            self.application.processEvents()
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
            self.application.processEvents()
            self.assertTrue(dialog._close_pending)
            self.assertEqual(rejected, [])
            self.assertEqual(finished, [])

            release.set()
            self.wait_for(lambda: not dialog._write_active)
            self.assertEqual(OCRReviewStore(review_directory).pending_samples(), [])


if __name__ == "__main__":
    unittest.main()

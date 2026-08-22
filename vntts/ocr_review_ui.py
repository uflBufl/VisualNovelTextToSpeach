from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from vntts.async_ui import LatestTaskRunner
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.ocr_review import OCRReviewStore


class OCRReviewDialog(QDialog):
    def __init__(
        self,
        directory,
        correction_store=None,
        profile_id=None,
        profile_name=None,
        corrections_changed=None,
        thread_pool=None,
        parent=None,
    ):
        super().__init__(parent)
        self.review_store = OCRReviewStore(directory)
        self.correction_store = correction_store or OCRCorrectionStore.load()
        self.profile_id = profile_id
        self.corrections_changed = corrections_changed or (lambda: None)
        self.write_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.write_runner.finished.connect(self._write_finished)
        self._write_active = False
        self._close_pending = False
        self._write_applies_corrections = False
        self._resolve_confirmation_sample = None
        self.samples = []
        self.setWindowTitle("Review uncertain OCR")
        self.resize(960, 620)

        self.sample_list = QListWidget()
        self.sample_list.setMinimumWidth(260)
        self.sample_list.currentRowChanged.connect(self.show_sample)
        self.progress = QLabel("Pending OCR samples: 0")
        self.progress.setAccessibleName("OCR review progress")

        self.preview = QLabel("No uncertain screenshots to review")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(220)
        self.preview.setStyleSheet(
            "QLabel { background: #202124; color: #d0d0d0; border: 1px solid #555; }"
        )
        self.source_character = QLabel("-")
        self.source_text = QTextEdit()
        self.source_text.setReadOnly(True)
        self.source_text.setMinimumHeight(80)
        self.confidence = QLabel("-")
        self.corrected_character = QLineEdit()
        self.corrected_text = QTextEdit()
        self.corrected_text.setMinimumHeight(80)
        self.scope = QComboBox()
        self.scope.addItem("All games", None)
        if profile_id:
            self.scope.addItem(profile_name or "Current game profile", profile_id)
            self.scope.setCurrentIndex(1)

        form = QFormLayout()
        form.addRow("Detected speaker", self.source_character)
        form.addRow("Detected text", self.source_text)
        form.addRow("OCR result", self.confidence)
        form.addRow("Correct speaker", self.corrected_character)
        form.addRow("Correct text", self.corrected_text)
        form.addRow("Save correction for", self.scope)

        self.save_button = QPushButton("Save correction and resolve")
        self.resolve_button = QPushButton("Resolve without correction")
        self.save_button.clicked.connect(self.save_correction)
        self.resolve_button.clicked.connect(self.resolve_without_correction)
        actions = QHBoxLayout()
        actions.addWidget(self.save_button)
        actions.addWidget(self.resolve_button)
        actions.addStretch()
        self.status = QLabel()
        self.status.setAccessibleName("OCR review save status")
        self.status.setWordWrap(True)

        details = QVBoxLayout()
        details.addWidget(self.preview)
        details.addLayout(form)
        details.addLayout(actions)
        details.addWidget(self.status)

        content = QHBoxLayout()
        sample_navigation = QVBoxLayout()
        sample_navigation.addWidget(self.progress)
        sample_navigation.addWidget(self.sample_list, 1)
        content.addLayout(sample_navigation)
        content.addLayout(details, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addLayout(content)
        layout.addWidget(buttons)
        self.reload_samples()

    def reload_samples(self):
        selected_metadata = None
        sample = self.current_sample()
        if sample is not None:
            selected_metadata = sample.metadata_path
        self.samples = self.review_store.pending_samples()
        self.sample_list.clear()
        for sample in self.samples:
            preview = " ".join(sample.text.split()) or "No text"
            if len(preview) > 52:
                preview = f"{preview[:49]}..."
            self.sample_list.addItem(
                f"{sample.character} - {sample.confidence:.0f}%\n{preview}"
            )
        if not self.samples:
            self.progress.setText("Pending OCR samples: 0")
            self.show_sample(-1)
            return
        selected_index = next(
            (
                index
                for index, item in enumerate(self.samples)
                if item.metadata_path == selected_metadata
            ),
            0,
        )
        self.sample_list.setCurrentRow(selected_index)

    def current_sample(self):
        row = self.sample_list.currentRow()
        return self.samples[row] if 0 <= row < len(self.samples) else None

    def show_sample(self, row):
        sample = self.samples[row] if 0 <= row < len(self.samples) else None
        self._reset_resolve_confirmation()
        enabled = sample is not None
        self.save_button.setEnabled(enabled and not self._write_active)
        self.resolve_button.setEnabled(enabled and not self._write_active)
        if sample is None:
            self.progress.setText(f"Pending OCR samples: {len(self.samples)}")
            self.preview.setText("No uncertain screenshots to review")
            self.preview.setPixmap(QPixmap())
            self.source_character.setText("-")
            self.source_text.clear()
            self.confidence.setText("-")
            self.corrected_character.clear()
            self.corrected_text.clear()
            return
        self.progress.setText(
            f"Pending OCR samples: {len(self.samples)} | Current {row + 1} of "
            f"{len(self.samples)}"
        )
        pixmap = QPixmap(str(sample.image_path))
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.source_character.setText(sample.character)
        self.source_text.setPlainText(sample.text)
        self.confidence.setText(
            f"{sample.confidence:.1f}% (required {sample.minimum_confidence:.1f}%), "
            f"{sample.preprocessing_profile}, {sample.attempts} attempts"
        )
        self.corrected_character.setText(sample.character)
        self.corrected_text.setPlainText(sample.text)

    def save_correction(self):
        sample = self.current_sample()
        if sample is None or self._write_active:
            return
        self._reset_resolve_confirmation()
        corrected_character = self.corrected_character.text().strip()
        corrected_text = self.corrected_text.toPlainText().strip()
        entries = {}
        if corrected_character and corrected_character != sample.character.strip():
            entries[sample.character] = corrected_character
        if corrected_text and corrected_text != sample.text.strip():
            entries[sample.text] = corrected_text
        if not entries:
            QMessageBox.information(
                self,
                "No correction entered",
                "Change the detected speaker or text, or mark this sample resolved.",
            )
            return
        profile_id = self.scope.currentData()
        self._start_write(
            self._save_and_resolve,
            self.correction_store,
            self.review_store,
            sample,
            dict(entries),
            profile_id,
            applies_corrections=True,
        )

    def resolve_without_correction(self):
        sample = self.current_sample()
        if sample is None or self._write_active:
            return
        if self._resolve_confirmation_sample != sample.metadata_path:
            self._resolve_confirmation_sample = sample.metadata_path
            self.resolve_button.setText("Confirm resolve without correction")
            self.status.setText(
                "Confirm to mark this sample resolved without saving any reusable "
                "speaker or text correction. Change the text and use Save correction "
                "instead if this OCR result should be fixed next time."
            )
            return
        self._reset_resolve_confirmation()
        self._start_write(
            self.review_store.mark_resolved,
            sample,
            applies_corrections=False,
        )

    def _reset_resolve_confirmation(self):
        had_confirmation = self._resolve_confirmation_sample is not None
        self._resolve_confirmation_sample = None
        self.resolve_button.setText("Resolve without correction")
        if had_confirmation and not self._write_active:
            self.status.clear()

    @staticmethod
    def _save_and_resolve(correction_store, review_store, sample, entries, profile_id):
        correction_store.upsert_entries(entries, profile_id)
        scope = str(profile_id) if profile_id else "global"
        return review_store.mark_resolved(
            sample,
            scope=scope,
            corrections=entries,
        )

    def _start_write(self, operation, *arguments, applies_corrections):
        self._write_active = True
        self._write_applies_corrections = applies_corrections
        self.save_button.setEnabled(False)
        self.resolve_button.setEnabled(False)
        self.sample_list.setEnabled(False)
        self.status.setText("Saving OCR review authority in the background...")
        self.write_runner.start(operation, *arguments)

    def _write_finished(self, _result, error):
        self._write_active = False
        self.sample_list.setEnabled(True)
        if error is not None:
            self.show_sample(self.sample_list.currentRow())
            self.status.setText(
                f"OCR review was not saved: {error}. Choose the action again to retry."
            )
        else:
            if self._write_applies_corrections:
                self.corrections_changed()
            self.reload_samples()
            self.status.setText("OCR review saved.")
        if self._close_pending:
            self._close_pending = False
            self.close()

    def closeEvent(self, event):
        if self._write_active:
            self._close_pending = True
            self.status.setText(
                "Saving OCR review authority. Close is deferred until it finishes."
            )
            event.ignore()
            return
        super().closeEvent(event)

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
        parent=None,
    ):
        super().__init__(parent)
        self.review_store = OCRReviewStore(directory)
        self.correction_store = correction_store or OCRCorrectionStore.load()
        self.profile_id = profile_id
        self.corrections_changed = corrections_changed or (lambda: None)
        self.samples = []
        self.setWindowTitle("Review uncertain OCR")
        self.resize(960, 620)

        self.sample_list = QListWidget()
        self.sample_list.setMinimumWidth(260)
        self.sample_list.currentRowChanged.connect(self.show_sample)

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
        self.resolve_button = QPushButton("Mark resolved")
        self.save_button.clicked.connect(self.save_correction)
        self.resolve_button.clicked.connect(self.resolve_without_correction)
        actions = QHBoxLayout()
        actions.addWidget(self.save_button)
        actions.addWidget(self.resolve_button)
        actions.addStretch()

        details = QVBoxLayout()
        details.addWidget(self.preview)
        details.addLayout(form)
        details.addLayout(actions)

        content = QHBoxLayout()
        content.addWidget(self.sample_list)
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
        enabled = sample is not None
        self.save_button.setEnabled(enabled)
        self.resolve_button.setEnabled(enabled)
        if sample is None:
            self.preview.setText("No uncertain screenshots to review")
            self.preview.setPixmap(QPixmap())
            self.source_character.setText("-")
            self.source_text.clear()
            self.confidence.setText("-")
            self.corrected_character.clear()
            self.corrected_text.clear()
            return
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
        if sample is None:
            return
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
        try:
            self.correction_store.upsert_entries(entries, profile_id)
            scope = str(profile_id) if profile_id else "global"
            self.review_store.mark_resolved(
                sample,
                scope=scope,
                corrections=entries,
            )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Unable to save correction", str(error))
            return
        self.corrections_changed()
        self.reload_samples()

    def resolve_without_correction(self):
        sample = self.current_sample()
        if sample is None:
            return
        try:
            self.review_store.mark_resolved(sample)
        except (OSError, ValueError, KeyError) as error:
            QMessageBox.warning(self, "Unable to resolve sample", str(error))
            return
        self.reload_samples()

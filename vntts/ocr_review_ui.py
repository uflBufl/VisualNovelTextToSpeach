from collections.abc import Callable, Mapping
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QCloseEvent, QPixmap
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
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.ocr_review import OCRReviewSample, OCRReviewStore


class OCRReviewDialog(QDialog):
    def __init__(
        self,
        directory: str | Path,
        correction_store: OCRCorrectionStore | None = None,
        profile_id: str | None = None,
        profile_name: str | None = None,
        corrections_changed: Callable[[], object] | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
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
        self.samples: list[OCRReviewSample] = []
        self._drafts: dict[Path, tuple[str, str]] = {}
        self.setWindowTitle("Review uncertain OCR")
        self.resize(960, 620)

        self.sample_list = QListWidget()
        self.sample_list.setMinimumWidth(180)
        self.sample_list.setMaximumWidth(240)
        self.sample_list.currentRowChanged.connect(self.show_sample)
        self.progress = QLabel("0 lines to review")
        self.progress.setAccessibleName("OCR review progress")

        self.preview = QLabel("No uncertain screenshots to review")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(150)
        self.preview.setMaximumHeight(190)
        self.preview.setStyleSheet(
            "QLabel { background: #202124; color: #d0d0d0; border: 1px solid #555; }"
        )
        self.zoom_button = QPushButton("Enlarge screenshot")
        self.zoom_button.setAutoDefault(False)
        self.zoom_button.clicked.connect(self.enlarge_screenshot)
        self.source_character = QLabel("-")
        self.source_text = QLabel("-")
        self.source_text.setWordWrap(True)
        self.source_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
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
        form.addRow("Recognition confidence", self.confidence)
        form.addRow("Correct speaker", self.corrected_character)
        form.addRow("Correct text", self.corrected_text)
        form.addRow("Apply future corrections in", self.scope)

        self.impact = QLabel()
        self.impact.setWordWrap(True)
        self.save_button = QPushButton("Save rule and resolve")
        self.save_button.setDefault(True)
        self.save_button.setAccessibleDescription(
            "Enabled after the detected speaker or dialogue text is corrected"
        )
        self.save_button.setToolTip(
            "Change the detected speaker or text before saving a correction."
        )
        self.resolve_button = QPushButton("Dismiss without rule")
        self.resolve_button.setAutoDefault(False)
        self.save_button.clicked.connect(self.save_correction)
        self.resolve_button.clicked.connect(self.resolve_without_correction)
        self.corrected_character.textChanged.connect(self._update_save_enabled)
        self.corrected_text.textChanged.connect(self._update_save_enabled)
        self.corrected_character.textChanged.connect(self._remember_draft)
        self.corrected_text.textChanged.connect(self._remember_draft)
        self.scope.currentIndexChanged.connect(self._update_save_enabled)
        actions = QHBoxLayout()
        actions.addWidget(self.save_button)
        actions.addWidget(self.resolve_button)
        actions.addStretch()
        self.status = QLabel()
        self.status.setAccessibleName("OCR review save status")
        self.status.setWordWrap(True)

        details = QVBoxLayout()
        screenshot_header = QHBoxLayout()
        screenshot_header.addWidget(QLabel("Captured dialogue"))
        screenshot_header.addStretch()
        screenshot_header.addWidget(self.zoom_button)
        details.addLayout(screenshot_header)
        details.addWidget(self.preview)
        details.addSpacing(10)
        details.addLayout(form)
        details.addWidget(self.impact)
        details.addLayout(actions)
        details.addWidget(self.status)
        self.details_panel = QWidget()
        self.details_panel.setLayout(details)
        self.empty_message = QLabel("No uncertain lines need review.")
        self.empty_message.setAlignment(Qt.AlignmentFlag.AlignCenter)

        content = QHBoxLayout()
        sample_navigation = QVBoxLayout()
        sample_navigation.addWidget(self.progress)
        sample_navigation.addWidget(self.sample_list, 1)
        self.navigation_panel = QWidget()
        self.navigation_panel.setLayout(sample_navigation)
        content.addWidget(self.navigation_panel)
        content.addWidget(self.details_panel, 1)
        content.addWidget(self.empty_message, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addLayout(content)
        layout.addWidget(buttons)
        self.reload_samples()
        if self.samples:
            self.corrected_character.setFocus()

    def reload_samples(self) -> None:
        selected_metadata: Path | None = None
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
            self.progress.setText("0 lines to review")
            self.show_sample(-1)
            self.resize(520, 220)
            return
        if self.width() < 900:
            self.resize(960, 620)
        selected_index = next(
            (
                index
                for index, item in enumerate(self.samples)
                if item.metadata_path == selected_metadata
            ),
            0,
        )
        self.sample_list.setCurrentRow(selected_index)

    def current_sample(self) -> OCRReviewSample | None:
        row = self.sample_list.currentRow()
        return self.samples[row] if 0 <= row < len(self.samples) else None

    def show_sample(self, row: int) -> None:
        sample = self.samples[row] if 0 <= row < len(self.samples) else None
        enabled = sample is not None
        self.navigation_panel.setVisible(enabled)
        self.details_panel.setVisible(enabled)
        self.empty_message.setVisible(not enabled)
        self.resolve_button.setEnabled(enabled and not self._write_active)
        self.zoom_button.setEnabled(enabled)
        if sample is None:
            self.save_button.setEnabled(False)
            self.progress.setText(f"{len(self.samples)} lines to review")
            self.preview.setText("No uncertain screenshots to review")
            self.preview.setPixmap(QPixmap())
            self.source_character.setText("-")
            self.source_text.setText("-")
            self.confidence.setText("-")
            self.corrected_character.clear()
            self.corrected_text.clear()
            return
        self.progress.setText(
            f"{len(self.samples)} to review | {row + 1} of {len(self.samples)}"
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
        self.source_text.setText(sample.text or "No text detected")
        self.confidence.setText(
            f"{sample.confidence:.0f}% (review below {sample.minimum_confidence:.0f}%)"
        )
        self.confidence.setToolTip(
            f"Recognition method: {sample.preprocessing_profile}; "
            f"attempts: {sample.attempts}"
        )
        draft = self._drafts.get(sample.metadata_path, (sample.character, sample.text))
        self.corrected_character.setText(draft[0])
        self.corrected_text.setPlainText(draft[1])
        self._update_save_enabled()

    def _remember_draft(self, *_args: object) -> None:
        sample = self.current_sample()
        if sample is not None:
            self._drafts[sample.metadata_path] = (
                self.corrected_character.text(),
                self.corrected_text.toPlainText(),
            )

    def _update_save_enabled(self, *_args: object) -> None:
        sample = self.current_sample()
        speaker = self.corrected_character.text().strip()
        dialogue = self.corrected_text.toPlainText().strip()
        changed = bool(
            sample is not None
            and (
                (speaker and speaker != sample.character.strip())
                or (dialogue and dialogue != sample.text.strip())
            )
        )
        valid = bool(
            sample is not None
            and (not sample.character.strip() or speaker)
            and (not sample.text.strip() or dialogue)
        )
        shared_source = bool(
            sample is not None
            and sample.character.strip().casefold() == sample.text.strip().casefold()
        )
        conflict = shared_source and speaker != dialogue
        self.save_button.setEnabled(
            changed and valid and not conflict and not self._write_active
        )
        if sample is None:
            self.impact.clear()
        elif not valid:
            self.impact.setText(
                "Corrections cannot be blank. Restore the detected value or enter a replacement."
            )
        elif conflict:
            self.impact.setText(
                "Cannot save different replacements for the same detected text: "
                "one rule changes both speaker and dialogue. Enter the same "
                "replacement in both fields, or dismiss this line without a rule."
            )
        elif not changed:
            self.impact.setText(
                "Change the speaker or text to create a rule for future OCR. Existing audio will not change."
            )
        else:
            scope = (
                f"the {self.scope.currentText()} profile"
                if self._selected_profile_id()
                else "all games"
            )
            changes = []
            if speaker != sample.character.strip():
                changes.append(f"“{sample.character}” to “{speaker}”")
            if dialogue != sample.text.strip():
                changes.append(f"“{sample.text}” to “{dialogue}”")
            self.impact.setText(
                f"Future OCR for {scope} will replace matching words or phrases "
                f"in speaker names and dialogue: {'; '.join(changes)}. "
                "Existing audio will not change."
            )

    def enlarge_screenshot(self) -> None:
        dialog = self._screenshot_dialog()
        if dialog is not None:
            dialog.exec()

    def _screenshot_dialog(self) -> QDialog | None:
        sample = self.current_sample()
        if sample is None:
            return None
        pixmap = QPixmap(str(sample.image_path))
        if pixmap.isNull():
            self.status.setText("Captured screenshot is unavailable.")
            return None
        dialog = QDialog(self)
        dialog.setWindowTitle("Captured dialogue - enlarged")
        dialog.resize(900, 540)
        image = QLabel()
        image.setPixmap(
            pixmap.scaled(
                pixmap.size() * 2,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        scroll = QScrollArea()
        scroll.setWidget(image)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close)
        layout = QVBoxLayout(dialog)
        layout.addWidget(scroll)
        layout.addWidget(buttons)
        return dialog

    def save_correction(self) -> None:
        sample = self.current_sample()
        if sample is None or self._write_active:
            return
        corrected_character = self.corrected_character.text().strip()
        corrected_text = self.corrected_text.toPlainText().strip()
        if (
            sample.character.strip().casefold() == sample.text.strip().casefold()
            and corrected_character != corrected_text
        ):
            self._update_save_enabled()
            return
        entries: dict[str, str] = {}
        if corrected_character and corrected_character != sample.character.strip():
            entries[sample.character] = corrected_character
        if corrected_text and corrected_text != sample.text.strip():
            if sample.text.strip().casefold() != sample.character.strip().casefold():
                entries[sample.text] = corrected_text
            elif not entries:
                entries[sample.character] = corrected_text
        if not entries:
            QMessageBox.information(
                self,
                "No correction entered",
                "Change the detected speaker or text, or mark this sample resolved.",
            )
            return
        profile_id = self._selected_profile_id()
        self._start_write(
            self._save_and_resolve,
            self.correction_store,
            self.review_store,
            sample,
            dict(entries),
            profile_id,
            applies_corrections=True,
        )

    def resolve_without_correction(self) -> None:
        sample = self.current_sample()
        if sample is None or self._write_active:
            return
        if self._dismissal_dialog().exec() != QMessageBox.StandardButton.Yes:
            return
        self._start_write(
            self.review_store.mark_resolved,
            sample,
            applies_corrections=False,
        )

    def _dismissal_dialog(self) -> QMessageBox:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Dismiss this line?")
        dialog.setText(
            "Remove this line from review without saving a rule? "
            "This action creates no new correction for future OCR."
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dialog.button(QMessageBox.StandardButton.Yes).setText("Dismiss line")
        dialog.button(QMessageBox.StandardButton.Cancel).setText("Keep reviewing")
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return dialog

    @staticmethod
    def _save_and_resolve(
        correction_store: OCRCorrectionStore,
        review_store: OCRReviewStore,
        sample: OCRReviewSample,
        entries: Mapping[str, str],
        profile_id: str | None,
    ) -> None:
        correction_store.upsert_entries(entries, profile_id)
        scope = str(profile_id) if profile_id else "global"
        try:
            review_store.mark_resolved(
                sample,
                scope=scope,
                corrections=entries,
            )
        except Exception as error:
            raise RuntimeError(
                f"Rule saved, but this line is still in review: {error}"
            ) from error

    def _selected_profile_id(self) -> str | None:
        value = self.scope.currentData()
        return value if isinstance(value, str) else None

    def _start_write(
        self,
        operation: Callable[..., object],
        *arguments: object,
        applies_corrections: bool,
    ) -> None:
        self._write_active = True
        self._write_applies_corrections = applies_corrections
        self.save_button.setEnabled(False)
        self.resolve_button.setEnabled(False)
        self.sample_list.setEnabled(False)
        self.corrected_character.setEnabled(False)
        self.corrected_text.setEnabled(False)
        self.scope.setEnabled(False)
        self.status.setText("Saving review in the background...")
        self.write_runner.start(operation, *arguments)

    def _write_finished(self, _result: object, error: Exception | None) -> None:
        self._write_active = False
        self.sample_list.setEnabled(True)
        self.corrected_character.setEnabled(True)
        self.corrected_text.setEnabled(True)
        self.scope.setEnabled(True)
        if error is not None:
            self.resolve_button.setEnabled(self.current_sample() is not None)
            self._update_save_enabled()
            if self._write_applies_corrections:
                self.corrections_changed()
            self.status.setText(
                f"Could not finish review: {error}. Check access to application "
                "data, then use the same button to retry. Your edits are still here."
            )
        else:
            if self._write_applies_corrections:
                self.corrections_changed()
            self.reload_samples()
            self.status.setText("OCR review saved.")
        if self._close_pending:
            self._close_pending = False
            if error is None:
                self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._write_active:
            self._close_pending = True
            self.status.setText(
                "Saving OCR review authority. Close is deferred until it finishes."
            )
            event.ignore()
            return
        super().closeEvent(event)

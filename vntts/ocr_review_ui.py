from collections.abc import Callable, Mapping
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool
from PySide6.QtGui import QCloseEvent, QColor, QPalette, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vntts.action_icons import set_action_icon
from vntts.async_ui import LatestTaskRunner
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.ocr_review import OCRReviewSample, OCRReviewStore
from vntts.qt_dialogs import CloseGuardedDialog
from vntts.ui_text import make_text_copyable
from vntts.versioned_json import StaleDocumentError


class OCRReviewDialog(CloseGuardedDialog):
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
        self._reload_required = False
        self._close_pending = False
        self._write_applies_corrections = False
        self.samples: list[OCRReviewSample] = []
        self._drafts: dict[Path, tuple[str, str]] = {}
        self._preview_pixmap = QPixmap()
        self.setWindowTitle("Review uncertain OCR")
        self.resize(960, 620)

        self.sample_list = QListWidget()
        self.sample_list.setMinimumWidth(180)
        self.sample_list.setMaximumWidth(240)
        self.sample_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.sample_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        selection_palette = self.sample_list.palette()
        selection_palette.setColor(QPalette.ColorRole.Highlight, QColor("#596575"))
        selection_palette.setColor(
            QPalette.ColorRole.HighlightedText, QColor("#ffffff")
        )
        self.sample_list.setPalette(selection_palette)
        self.sample_list.currentRowChanged.connect(self.show_sample)
        self.progress = QLabel("0 lines to review")
        self.progress.setAccessibleName("OCR review progress")

        self.preview = self._create_preview()
        self.zoom_button = QPushButton("Enlarge")
        set_action_icon(self.zoom_button, "expand")
        self.zoom_button.setAccessibleName("Enlarge captured screenshot")
        self.zoom_button.setToolTip("Enlarge captured screenshot")
        self.zoom_button.setAutoDefault(False)
        self.zoom_button.clicked.connect(self.enlarge_screenshot)
        form = self.form = self._create_correction_form(profile_name)

        self.impact = QLabel()
        self.impact.setWordWrap(True)
        self.impact.setTextFormat(Qt.TextFormat.PlainText)
        self.save_button = QPushButton("Save rule")
        set_action_icon(self.save_button, "save")
        self.save_button.setDefault(True)
        self.save_button.setAccessibleDescription(
            "Enabled after the detected speaker or dialogue text is corrected"
        )
        self.save_button.setToolTip(
            "Change the detected speaker or text before saving a correction."
        )
        self.resolve_button = QPushButton("Dismiss without rule")
        set_action_icon(self.resolve_button, "skip-forward")
        self.resolve_button.setToolTip(
            "Dismiss the selected line without saving a new OCR rule"
        )
        self.resolve_button.setAutoDefault(False)
        self.save_button.clicked.connect(self.save_correction)
        self.resolve_button.clicked.connect(self.resolve_without_correction)
        self.corrected_character.textChanged.connect(self._update_save_enabled)
        self.corrected_text.textChanged.connect(self._update_save_enabled)
        self.corrected_character.textChanged.connect(self._remember_draft)
        self.corrected_text.textChanged.connect(self._remember_draft)
        self.scope.currentIndexChanged.connect(self._update_save_enabled)
        self.status = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.status.setAccessibleName("OCR review save status")
        self.status.setFrameShape(QFrame.Shape.NoFrame)
        self.status.viewport().setAutoFillBackground(False)
        self.status.textChanged.connect(self._size_status)
        self._size_status()

        details = QVBoxLayout()
        screenshot_header = QHBoxLayout()
        screenshot_header.addStretch()
        screenshot_header.addWidget(self.zoom_button)
        details.addLayout(screenshot_header)
        details.addWidget(self.preview)
        details.addSpacing(10)
        details.addLayout(form)
        details.addWidget(self.impact)
        self.details_panel = QWidget()
        self.details_panel.setLayout(details)
        self.details_scroll = QScrollArea()
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.details_scroll.setWidget(self.details_panel)
        self.empty_message = QLabel("No uncertain lines need review.")
        self.empty_message.setAlignment(Qt.AlignmentFlag.AlignCenter)

        content = QHBoxLayout()
        sample_navigation = QVBoxLayout()
        sample_navigation.addWidget(self.progress)
        sample_navigation.addWidget(self.sample_list, 1)
        self.navigation_panel = QWidget()
        self.navigation_panel.setLayout(sample_navigation)
        content.addWidget(self.navigation_panel)
        content.addWidget(self.details_scroll, 1)
        content.addWidget(self.empty_message, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        set_action_icon(buttons.button(QDialogButtonBox.StandardButton.Close), "x")
        buttons.addButton(self.resolve_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.save_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.button(QDialogButtonBox.StandardButton.Close).setAutoDefault(False)
        self.save_button.setDefault(True)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addLayout(content, 1)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        for watched in (
            self.preview,
            self.source_text,
            self.corrected_text,
            self.status,
            self.details_scroll.viewport(),
        ):
            watched.installEventFilter(self)
        make_text_copyable(self)
        self.reload_samples()
        if self.samples:
            self.corrected_character.setFocus()
        else:
            self.resize(520, 220)

    @staticmethod
    def _create_preview() -> QLabel:
        preview = QLabel("No uncertain screenshots to review")
        preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setMinimumHeight(150)
        preview.setMaximumHeight(190)
        preview.setStyleSheet(
            "QLabel { background: #202124; color: #d0d0d0; border: 1px solid #555; }"
        )
        preview.setAccessibleName("Captured dialogue")
        return preview

    def _create_correction_form(self, profile_name: str | None) -> QFormLayout:
        self.source_character = QLabel("-")
        self.source_character.setTextFormat(Qt.TextFormat.PlainText)
        self.source_character.setWordWrap(True)
        source_policy = self.source_character.sizePolicy()
        source_policy.setVerticalPolicy(QSizePolicy.Policy.Maximum)
        self.source_character.setSizePolicy(source_policy)
        self.source_text = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.corrected_character = QLineEdit()
        self.corrected_text = QPlainTextEdit(tabChangesFocus=True)
        self.scope = QComboBox()
        self.scope.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.scope.setMinimumContentsLength(12)
        self.scope.addItem("All games", None)
        if self.profile_id:
            self.scope.addItem(profile_name or "Current game profile", self.profile_id)
            self.scope.setCurrentIndex(1)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Detected speaker", self.source_character)
        form.addRow("Detected text", self.source_text)
        form.addRow("Correct speaker", self.corrected_character)
        form.addRow("Correct text", self.corrected_text)
        form.addRow("Apply rule in", self.scope)

        for editor in (self.source_text, self.corrected_text):
            self._size_editor(editor)
            editor.document().documentLayout().documentSizeChanged.connect(
                self._resize_editors, Qt.ConnectionType.QueuedConnection
            )
            editor.updateRequest.connect(
                self._resize_editors, Qt.ConnectionType.QueuedConnection
            )
        self.source_text.setAccessibleName("Detected text")
        self.corrected_text.setAccessibleName("Correct text")
        return form

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(watched, QPlainTextEdit)
            and event.type() == QEvent.Type.FontChange
        ):
            if watched is self.status:
                self._size_status()
            else:
                self._size_editor(watched)
        if watched is self.preview and event.type() == QEvent.Type.Resize:
            self._show_preview()
        if watched is self.details_scroll.viewport() and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.FontChange,
        ):
            minimum_columns = self.fontMetrics().horizontalAdvance(
                "Detected speaker  A dialogue correction"
            )
            self.form.setRowWrapPolicy(
                QFormLayout.RowWrapPolicy.WrapAllRows
                if self.details_scroll.viewport().width() < minimum_columns
                else QFormLayout.RowWrapPolicy.WrapLongRows
            )
        return super().eventFilter(watched, event)

    def _resize_editors(self, *_args: object) -> None:
        for editor in (self.source_text, self.corrected_text):
            self._size_editor(editor)

    @staticmethod
    def _size_editor(editor: QPlainTextEdit) -> None:
        lines = editor.document().documentLayout().documentSize().height()
        line_height = editor.fontMetrics().lineSpacing()
        minimum = line_height * 3 + 16
        maximum = int(line_height * max(3, lines)) + 16
        if editor.minimumHeight() != minimum:
            editor.setMinimumHeight(minimum)
        if editor.maximumHeight() != maximum:
            editor.setMaximumHeight(maximum)

    def _size_status(self) -> None:
        lines = 3 if self.status.toPlainText() else 1
        self.status.setFixedHeight(self.status.fontMetrics().lineSpacing() * lines + 12)

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
                f"{sample.confidence:.0f}% - {sample.character or 'No speaker'}\n{preview}"
            )
            self.sample_list.item(self.sample_list.count() - 1).setToolTip(
                f"{sample.character or 'No speaker detected'}\n{sample.text or 'No text detected'}\n"
                f"Recognition confidence: {sample.confidence:.0f}% "
                f"(review below {sample.minimum_confidence:.0f}%)"
            )
        if not self.samples:
            self.progress.setText("0 lines to review")
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

    def current_sample(self) -> OCRReviewSample | None:
        row = self.sample_list.currentRow()
        return self.samples[row] if 0 <= row < len(self.samples) else None

    def show_sample(self, row: int) -> None:
        sample = self.samples[row] if 0 <= row < len(self.samples) else None
        if not self._write_active and not self._reload_required:
            self.status.clear()
        enabled = sample is not None
        self.navigation_panel.setVisible(enabled)
        self.details_scroll.setVisible(enabled)
        self.save_button.setVisible(enabled)
        self.resolve_button.setVisible(enabled)
        self.empty_message.setVisible(not enabled)
        self.resolve_button.setEnabled(
            enabled and not self._write_active and not self._reload_required
        )
        self.zoom_button.setEnabled(enabled)
        if sample is None:
            self.save_button.setEnabled(False)
            self.progress.setText(f"{len(self.samples)} lines to review")
            self._preview_pixmap = QPixmap()
            self.preview.setText("No uncertain screenshots to review")
            self.source_character.setText("-")
            self.source_text.clear()
            self.corrected_character.clear()
            self.corrected_text.clear()
            return
        self.progress.setText(f"Line {row + 1} of {len(self.samples)}")
        self._preview_pixmap = QPixmap(str(sample.image_path))
        self.zoom_button.setEnabled(not self._preview_pixmap.isNull())
        self._show_preview()
        self.source_character.setText(sample.character or "No speaker detected")
        self.source_text.setPlainText(sample.text)
        self.source_text.setPlaceholderText("No text detected")
        draft = self._drafts.get(sample.metadata_path, (sample.character, sample.text))
        self.corrected_character.setText(draft[0])
        self.corrected_text.setPlainText(draft[1])
        self._update_save_enabled()

    def _show_preview(self) -> None:
        if self._preview_pixmap.isNull():
            self.preview.setText("Captured screenshot is unavailable.")
        else:
            self.preview.setPixmap(
                self._preview_pixmap.scaled(
                    self.preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

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
        missing_source = bool(
            sample is not None
            and (
                (speaker and not sample.character.strip())
                or (dialogue and not sample.text.strip())
            )
        )
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
            changed
            and valid
            and not missing_source
            and not conflict
            and not self._write_active
            and not self._reload_required
        )
        if sample is None:
            self.impact.clear()
        elif missing_source:
            self.impact.setText(
                "A rule needs detected text to match. Leave an undetected field blank; "
                "you can correct the other field, or dismiss this line without a rule."
            )
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
            self.impact.setText(
                f"Future OCR for {scope} will replace matching words or phrases "
                "in speaker names and dialogue. Saving removes this line from review; "
                "existing audio will not change."
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
            self.status.setPlainText("Captured screenshot is unavailable.")
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
        set_action_icon(buttons.button(QDialogButtonBox.StandardButton.Close), "x")
        buttons.rejected.connect(dialog.close)
        layout = QVBoxLayout(dialog)
        layout.addWidget(scroll)
        layout.addWidget(buttons)
        return dialog

    def save_correction(self) -> None:
        sample = self.current_sample()
        self._update_save_enabled()
        if sample is None or not self.save_button.isEnabled():
            return
        corrected_character = self.corrected_character.text().strip()
        corrected_text = self.corrected_text.toPlainText().strip()
        entries: dict[str, str] = {}
        if corrected_character and corrected_character != sample.character.strip():
            entries[sample.character] = corrected_character
        if corrected_text and corrected_text != sample.text.strip():
            if sample.text.strip().casefold() != sample.character.strip().casefold():
                entries[sample.text] = corrected_text
            elif not entries:
                entries[sample.character] = corrected_text
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
        if sample is None or self._write_active or self._reload_required:
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
            "Remove this line from review? "
            "This creates no new rule and keeps any rules already saved."
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dialog.button(QMessageBox.StandardButton.Yes).setText("Dismiss line")
        dialog.button(QMessageBox.StandardButton.Cancel).setText("Keep reviewing")
        set_action_icon(dialog.button(QMessageBox.StandardButton.Yes), "x")
        set_action_icon(
            dialog.button(QMessageBox.StandardButton.Cancel), "chevron-left"
        )
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
        self.status.setPlainText("Saving review in the background...")
        self.write_runner.start(operation, *arguments)

    def _write_finished(self, _result: object, error: Exception | None) -> None:
        self._write_active = False
        self.sample_list.setEnabled(True)
        self.corrected_character.setEnabled(True)
        self.corrected_text.setEnabled(True)
        self.scope.setEnabled(True)
        if self._write_applies_corrections:
            self.corrections_changed()
        if error is not None:
            if isinstance(error, StaleDocumentError) or isinstance(
                error.__cause__, StaleDocumentError
            ):
                self._reload_required = True
                self.status.setPlainText(
                    "Review data changed elsewhere. Copy your edits, then close and reopen "
                    f"OCR review before continuing. Details: {error}"
                )
            else:
                self.status.setPlainText(
                    "Could not finish review. Your edits are still here. Check access to "
                    f"application data, then retry. Details: {error}"
                )
            self.resolve_button.setEnabled(
                self.current_sample() is not None and not self._reload_required
            )
            self._update_save_enabled()
        else:
            self.reload_samples()
            self.status.setPlainText("OCR review saved.")
        if self._close_pending:
            self._close_pending = False
            if error is None:
                self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._write_active:
            self._close_pending = True
            self.status.setPlainText(
                "Saving review... This window will close when saving finishes."
            )
            event.ignore()
            return
        super().closeEvent(event)

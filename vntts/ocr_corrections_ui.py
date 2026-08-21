from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.ocr_corrections import OCRCorrectionStore


class OCRCorrectionsDialog(QDialog):
    def __init__(
        self,
        profile_id=None,
        profile_name=None,
        store=None,
        thread_pool=None,
        parent=None,
    ):
        super().__init__(parent)
        self.profile_id = profile_id
        self.store = store or OCRCorrectionStore.load()
        self.save_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.save_runner.finished.connect(self._save_finished)
        self._save_active = False
        self._close_pending = False
        self.setWindowTitle("OCR corrections")
        self.resize(680, 460)

        self.tabs = QTabWidget()
        self.global_table = self._create_table(self.store.global_entries)
        self.tabs.addTab(
            self._create_table_page(
                self.global_table,
                "Corrections applied to every game profile.",
            ),
            "Global",
        )

        self.profile_table = self._create_table(
            self.store.profile_entries.get(str(profile_id), {})
        )
        profile_label = profile_name or "Current profile"
        profile_page = self._create_table_page(
            self.profile_table,
            "These entries override global corrections for this game profile.",
        )
        profile_index = self.tabs.addTab(profile_page, profile_label)
        self.tabs.setTabEnabled(profile_index, profile_id is not None)

        note = QLabel(
            "Corrections match complete words or phrases without changing case "
            "inside unrelated words. They are applied to both speaker names and "
            "dialog text."
        )
        note.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        self.status = QLabel()
        self.status.setAccessibleName("OCR correction save status")
        self.status.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.tabs)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

    def save(self):
        try:
            global_entries = self._entries_from_table(self.global_table)
            profile_entries = self._entries_from_table(self.profile_table)
        except ValueError as error:
            QMessageBox.warning(self, "Unable to save OCR corrections", str(error))
            return
        self._save_active = True
        self.tabs.setEnabled(False)
        self.buttons.setEnabled(False)
        self.status.setText("Saving OCR corrections in the background...")
        self.save_runner.start(
            self.store.replace_entries,
            global_entries,
            self.profile_id,
            profile_entries,
        )

    def _save_finished(self, _result, error):
        self._save_active = False
        self.tabs.setEnabled(True)
        self.buttons.setEnabled(True)
        if error is not None:
            self.status.setText(
                f"OCR corrections were not saved: {error}. Select Save to retry."
            )
        else:
            self.accept()
        if self._close_pending and error is not None:
            self._close_pending = False
            self.close()

    def reject(self):
        if self._save_active:
            self._close_pending = True
            self.status.setText(
                "Saving OCR corrections. Close is deferred until the write finishes."
            )
            return
        super().reject()

    def closeEvent(self, event):
        if self._save_active:
            self._close_pending = True
            self.status.setText(
                "Saving OCR corrections. Close is deferred until the write finishes."
            )
            event.ignore()
            return
        super().closeEvent(event)

    @staticmethod
    def _create_table(entries):
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["OCR text", "Replace with"])
        table.horizontalHeader().setStretchLastSection(True)
        for source, replacement in entries.items():
            OCRCorrectionsDialog._append_row(table, source, replacement)
        return table

    @staticmethod
    def _create_table_page(table, description):
        page = QWidget()
        add_button = QPushButton("Add")
        remove_button = QPushButton("Remove selected")
        add_button.clicked.connect(lambda: OCRCorrectionsDialog._append_row(table))
        remove_button.clicked.connect(
            lambda: OCRCorrectionsDialog._remove_selected_rows(table)
        )
        actions = QHBoxLayout()
        actions.addWidget(add_button)
        actions.addWidget(remove_button)
        actions.addStretch()
        label = QLabel(description)
        label.setWordWrap(True)
        layout = QVBoxLayout(page)
        layout.addWidget(label)
        layout.addWidget(table)
        layout.addLayout(actions)
        return page

    @staticmethod
    def _append_row(table, source="", replacement=""):
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(source))
        table.setItem(row, 1, QTableWidgetItem(replacement))
        if not source:
            table.setCurrentCell(row, 0)
            table.editItem(table.item(row, 0))

    @staticmethod
    def _remove_selected_rows(table):
        rows = sorted({item.row() for item in table.selectedItems()}, reverse=True)
        for row in rows:
            table.removeRow(row)

    @staticmethod
    def _entries_from_table(table):
        entries = {}
        for row in range(table.rowCount()):
            source_item = table.item(row, 0)
            replacement_item = table.item(row, 1)
            source = source_item.text().strip() if source_item is not None else ""
            replacement = (
                replacement_item.text().strip() if replacement_item is not None else ""
            )
            if not source and not replacement:
                continue
            if not source or not replacement:
                raise ValueError(f"Complete both fields in row {row + 1}")
            if source.casefold() in {key.casefold() for key in entries}:
                raise ValueError(f"Duplicate OCR correction source: {source}")
            entries[source] = replacement
        return entries

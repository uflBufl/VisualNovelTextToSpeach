from PySide6.QtCore import QEvent, QSignalBlocker, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
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
        self._discard_confirmed = False
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
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.status = QLabel()
        self.status.setAccessibleName("OCR correction save status")
        self.status.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.tabs)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)
        self._initial_rows = self._all_table_rows()

    def save(self):
        if self.validate_rows():
            return
        global_entries = self._entries_from_table(self.global_table)
        profile_entries = self._entries_from_table(self.profile_table)
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
        if self._guard_unsaved_close():
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
        if self._guard_unsaved_close():
            event.ignore()
            return
        super().closeEvent(event)

    def _create_table(self, entries):
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["OCR text", "Replace with"])
        table.horizontalHeader().setStretchLastSection(True)
        for source, replacement in entries.items():
            OCRCorrectionsDialog._append_row(table, source, replacement)
        table.itemChanged.connect(self._mark_changed)
        table.installEventFilter(self)
        return table

    def _create_table_page(self, table, description):
        page = QWidget()
        add_button = QPushButton("Add")
        remove_button = QPushButton("Remove selected")
        add_button.setAccessibleDescription("Add a new OCR correction row (Insert)")
        remove_button.setAccessibleDescription(
            "Remove the selected OCR correction rows (Control+Delete)"
        )
        add_button.clicked.connect(lambda: self._append_row(table))
        remove_button.clicked.connect(lambda: self._remove_selected_rows(table))
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

    def eventFilter(self, watched, event):
        if isinstance(watched, QTableWidget) and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Insert:
                self._append_row(watched)
                return True
            if (
                event.key() == Qt.Key.Key_Delete
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                self._remove_selected_rows(watched)
                return True
        return super().eventFilter(watched, event)

    @staticmethod
    def _append_row(table, source="", replacement=""):
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(source))
        table.setItem(row, 1, QTableWidgetItem(replacement))
        if not source:
            table.setCurrentCell(row, 0)
            table.editItem(table.item(row, 0))

    def _remove_selected_rows(self, table):
        rows = sorted({item.row() for item in table.selectedItems()}, reverse=True)
        for row in rows:
            table.removeRow(row)
        if rows:
            self._mark_changed()

    @staticmethod
    def _table_rows(table):
        return tuple(
            tuple(
                table.item(row, column).text()
                if table.item(row, column) is not None
                else ""
                for column in range(2)
            )
            for row in range(table.rowCount())
        )

    def _all_table_rows(self):
        return (
            self._table_rows(self.global_table),
            self._table_rows(self.profile_table),
        )

    def _mark_changed(self, *_args):
        self._discard_confirmed = False
        self.cancel_button.setText("Cancel")
        if not self._save_active:
            self.validate_rows(show_valid=False)

    def _guard_unsaved_close(self):
        if self._all_table_rows() == self._initial_rows:
            return False
        if self._discard_confirmed:
            return False
        self._discard_confirmed = True
        self.cancel_button.setText("Discard changes")
        self.status.setText(
            "Unsaved OCR corrections will be lost. Select Discard changes or "
            "close again to confirm, or choose Save."
        )
        return True

    @staticmethod
    def _clear_validation(table):
        for row in range(table.rowCount()):
            for column in range(2):
                item = table.item(row, column)
                if item is not None:
                    item.setBackground(QColor())
                    item.setToolTip("")

    def validate_rows(self, *, show_valid=True):
        signal_blockers = (
            QSignalBlocker(self.global_table),
            QSignalBlocker(self.profile_table),
        )
        errors = []
        first_item = None
        for scope, table in (
            ("Global", self.global_table),
            ("Profile", self.profile_table),
        ):
            self._clear_validation(table)
            seen = {}
            for row in range(table.rowCount()):
                source_item = table.item(row, 0)
                replacement_item = table.item(row, 1)
                source = source_item.text().strip() if source_item else ""
                replacement = (
                    replacement_item.text().strip() if replacement_item else ""
                )
                if not source and not replacement:
                    continue
                invalid_items = []
                message = None
                if not source or not replacement:
                    message = f"{scope} row {row + 1}: complete both fields."
                    invalid_items = [
                        item
                        for item, value in (
                            (source_item, source),
                            (replacement_item, replacement),
                        )
                        if not value and item is not None
                    ]
                elif source.casefold() in seen:
                    message = f"{scope} row {row + 1}: duplicate source '{source}'."
                    invalid_items = [source_item]
                    original = seen[source.casefold()]
                    original.setBackground(QColor("#ffd9d5"))
                    original.setToolTip("Duplicate OCR correction source")
                else:
                    seen[source.casefold()] = source_item
                if message is None:
                    continue
                errors.append(message)
                for item in invalid_items:
                    item.setBackground(QColor("#ffd9d5"))
                    item.setToolTip(message)
                    first_item = first_item or item
        if errors:
            self.status.setText(
                f"Fix {len(errors)} correction row error(s) before saving:\n"
                + "\n".join(f"- {message}" for message in errors)
            )
            if first_item is not None:
                first_item.tableWidget().setCurrentItem(first_item)
        elif show_valid and not self._save_active:
            self.status.setText("All correction rows are valid.")
        del signal_blockers
        return tuple(errors)

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

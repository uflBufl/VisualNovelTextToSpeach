import csv
from io import StringIO

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QObject,
    QSignalBlocker,
    Qt,
    QThreadPool,
)
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QKeyEvent,
    QResizeEvent,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from vntts.action_icons import set_action_icon
from vntts.async_ui import LatestTaskRunner
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.versioned_json import StaleDocumentError

TableRow = tuple[str, str]
TableRows = tuple[TableRow, ...]
AllTableRows = tuple[TableRows, TableRows]


class OCRCorrectionsDialog(QDialog):
    def __init__(
        self,
        profile_id: str | None = None,
        profile_name: str | None = None,
        store: OCRCorrectionStore | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile_id = profile_id
        self.store = store or OCRCorrectionStore.load()
        self.save_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.save_runner.finished.connect(self._save_finished)
        self._save_active = False
        self._reload_message = ""
        self.setWindowTitle("OCR corrections")
        self.resize(680, 460)

        self.tabs = QTabWidget()
        self.global_table = self._create_table(self.store.global_entries)
        self.tabs.addTab(
            self._create_table_page(
                self.global_table,
                "Rules here apply to every game.",
            ),
            "All games",
        )

        self.profile_table = self._create_table(
            self.store.profile_entries.get(str(profile_id), {})
        )
        profile_label = profile_name or "Current profile"
        profile_page = self._create_table_page(
            self.profile_table,
            "For matching OCR text, these rules override All games.",
        )
        profile_index = self.tabs.addTab(profile_page, profile_label)
        self.tabs.setTabEnabled(profile_index, profile_id is not None)
        if profile_id is not None:
            self.tabs.setCurrentIndex(profile_index)

        note = QLabel(
            "Replace whole words or phrases in speaker names and dialogue. "
            "Other words are unchanged."
        )
        note.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        set_action_icon(self.save_button, "save")
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        set_action_icon(self.cancel_button, "x")
        self.status = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.status.setAccessibleName("OCR correction save status")
        self.status.setFrameShape(QFrame.Shape.NoFrame)
        self.status.viewport().setAutoFillBackground(False)
        self.status.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.status.setFixedHeight(self.status.fontMetrics().lineSpacing() * 3 + 12)
        self.status.installEventFilter(self)
        self.copy_button = QPushButton("Copy rules")
        set_action_icon(self.copy_button, "copy")
        self.copy_button.setToolTip(
            "Copy all edited rules from both scopes, including unsaved changes"
        )
        self.copy_button.clicked.connect(self.copy_rules)
        footer = QHBoxLayout()
        footer.addWidget(self.copy_button)
        footer.addStretch()
        footer.addWidget(self.buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.status)
        layout.addLayout(footer)
        self._initial_rows = self._all_table_rows()
        self.save_button.setEnabled(False)
        self.setTabOrder(self.status, self.copy_button)
        self.setTabOrder(self.copy_button, self.save_button)
        self.setTabOrder(self.save_button, self.cancel_button)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        layout = self.layout()
        if layout is not None:
            self.setMinimumHeight(
                layout.totalMinimumHeightForWidth(event.size().width())
            )

    def save(self) -> None:
        if self.validate_rows(focus_first=True):
            return
        global_entries = self._entries_from_table(self.global_table)
        profile_entries = self._entries_from_table(self.profile_table)
        self._save_active = True
        self.tabs.setEnabled(False)
        self.buttons.setEnabled(False)
        self.status.setPlainText("Saving OCR corrections in the background...")
        self.save_runner.start(
            self.store.replace_entries,
            global_entries,
            self.profile_id,
            profile_entries,
        )

    def _save_finished(self, _result: object, error: Exception | None) -> None:
        self._save_active = False
        self.tabs.setEnabled(True)
        self.buttons.setEnabled(True)
        if error is not None:
            if isinstance(error, StaleDocumentError):
                self._reload_message = (
                    "Rules changed elsewhere. Select Copy rules to keep your edits, then close "
                    "and reopen OCR corrections to load the latest rules."
                )
                self.save_button.setEnabled(False)
                self.status.setPlainText(self._reload_message)
            else:
                self.status.setPlainText(
                    "Rules were not saved. Your edits are still here.\n"
                    "Check access to application data, then select Save again.\n"
                    f"{error}"
                )
        else:
            self.accept()

    def reject(self) -> None:
        if self._save_active:
            self.status.setPlainText(
                "Saving OCR corrections. Close is deferred until the write finishes."
            )
            return
        if self._has_unsaved_changes() and not self._confirm_discard():
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._save_active:
            self.status.setPlainText(
                "Saving OCR corrections. Close is deferred until the write finishes."
            )
            event.ignore()
            return
        if self._has_unsaved_changes() and not self._confirm_discard():
            event.ignore()
            return
        super().reject()
        event.accept()

    def copy_rules(self) -> None:
        output = StringIO()
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(("Scope", "OCR text", "Replace with"))
        for index, rows in enumerate(self._all_table_rows()):
            writer.writerows((self.tabs.tabText(index), *row) for row in rows)
        QApplication.clipboard().setText(output.getvalue())

    def _create_table(self, entries: dict[str, str]) -> QTableWidget:
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["OCR text", "Replace with"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for source, replacement in entries.items():
            OCRCorrectionsDialog._append_row(table, source, replacement)
        table.itemChanged.connect(self._mark_changed)
        self._size_table(table)
        table.installEventFilter(self)
        return table

    def _create_table_page(self, table: QTableWidget, description: str) -> QWidget:
        page = QWidget()
        add_button = QPushButton("Add")
        set_action_icon(add_button, "plus")
        remove_button = QPushButton("Remove selected")
        set_action_icon(remove_button, "trash-2")
        remove_button.setEnabled(False)
        add_button.setAccessibleDescription("Add a new OCR correction row (Insert)")
        remove_button.setAccessibleDescription(
            "Remove the selected OCR correction rows (Control+Delete)"
        )
        add_button.clicked.connect(lambda: self._append_row(table))
        remove_button.clicked.connect(lambda: self._remove_selected_rows(table))
        table.itemSelectionChanged.connect(
            lambda: remove_button.setEnabled(bool(table.selectedItems()))
        )
        actions = QHBoxLayout()
        actions.addWidget(add_button)
        actions.addWidget(remove_button)
        actions.addStretch()
        label = QLabel(description)
        label.setWordWrap(True)
        empty_hint = QLabel("No rules in this scope. Select Add to create one.")

        def refresh_empty(*_args: object) -> None:
            empty = table.rowCount() == 0
            empty_hint.setVisible(empty)
            add_button.setStyleSheet("font-weight: 700;" if empty else "")

        inspection_hint = QLabel("Full value on hover; F2 or double-click to edit.")
        inspection_hint.setWordWrap(True)
        table.model().rowsInserted.connect(refresh_empty)
        table.model().rowsRemoved.connect(refresh_empty)
        refresh_empty()
        layout = QVBoxLayout(page)
        layout.addWidget(label)
        layout.addWidget(empty_hint)
        layout.addWidget(table)
        layout.addLayout(actions)
        layout.addWidget(inspection_hint)
        return page

    @staticmethod
    def _size_table(table: QTableWidget) -> None:
        table.setMinimumHeight(
            table.horizontalHeader().sizeHint().height()
            + max(
                table.verticalHeader().defaultSectionSize(),
                table.fontMetrics().lineSpacing() + 8,
            )
            * 3
            + table.frameWidth() * 2
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QTableWidget) and event.type() == QEvent.Type.FontChange:
            self._size_table(watched)
        if (
            isinstance(watched, QPlainTextEdit)
            and event.type() == QEvent.Type.FontChange
        ):
            watched.setFixedHeight(watched.fontMetrics().lineSpacing() * 3 + 12)
        if (
            isinstance(watched, QTableWidget)
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
        ):
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
    def _append_row(
        table: QTableWidget, source: str = "", replacement: str = ""
    ) -> None:
        row = table.rowCount()
        table.insertRow(row)
        source_item = QTableWidgetItem(source)
        replacement_item = QTableWidgetItem(replacement)
        table.setItem(row, 0, source_item)
        table.setItem(row, 1, replacement_item)
        source_item.setToolTip(source)
        replacement_item.setToolTip(replacement)
        if not source:
            table.setCurrentCell(row, 0)
            table.editItem(source_item)

    def _remove_selected_rows(self, table: QTableWidget) -> None:
        rows = sorted({item.row() for item in table.selectedItems()}, reverse=True)
        for row in rows:
            table.removeRow(row)
        if rows:
            self._mark_changed()

    @staticmethod
    def _table_rows(table: QTableWidget) -> TableRows:
        return tuple(
            values
            for values in (
                (
                    OCRCorrectionsDialog._cell_text(table.item(row, 0)),
                    OCRCorrectionsDialog._cell_text(table.item(row, 1)),
                )
                for row in range(table.rowCount())
            )
            if any(value.strip() for value in values)
        )

    @staticmethod
    def _cell_text(item: QTableWidgetItem | None) -> str:
        return item.text() if item is not None else ""

    def _all_table_rows(self) -> AllTableRows:
        return (
            self._table_rows(self.global_table),
            self._table_rows(self.profile_table),
        )

    def _mark_changed(self, *_args: object) -> None:
        self.save_button.setEnabled(
            self._all_table_rows() != self._initial_rows
            and not self._save_active
            and not self._reload_message
        )
        if not self._save_active:
            self.validate_rows(show_valid=False)

    def _has_unsaved_changes(self) -> bool:
        for table in (self.global_table, self.profile_table):
            item = table.currentItem()
            for editor in table.findChildren(QLineEdit):
                if editor.isVisible() and editor.text() != (
                    item.text() if item is not None else ""
                ):
                    return True
        return self._all_table_rows() != self._initial_rows

    def _confirm_discard(self) -> bool:
        return self._discard_dialog().exec() == QMessageBox.StandardButton.Discard

    def _discard_dialog(self) -> QMessageBox:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Discard OCR corrections?")
        dialog.setText("Discard unsaved OCR correction changes?")
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
        )
        dialog.button(QMessageBox.StandardButton.Discard).setText("Discard changes")
        dialog.button(QMessageBox.StandardButton.Cancel).setText("Keep editing")
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return dialog

    @staticmethod
    def _clear_validation(table: QTableWidget) -> None:
        for row in range(table.rowCount()):
            for column in range(2):
                item = table.item(row, column)
                if item is not None:
                    item.setBackground(QColor())
                    item.setForeground(QBrush())
                    item.setToolTip(item.text())

    def _validate_table(
        self, scope: str, table: QTableWidget
    ) -> tuple[list[str], QTableWidgetItem | None]:
        self._clear_validation(table)
        errors: list[str] = []
        first_item: QTableWidgetItem | None = None
        seen: dict[str, QTableWidgetItem] = {}
        for row in range(table.rowCount()):
            source_item = table.item(row, 0)
            replacement_item = table.item(row, 1)
            source = self._cell_text(source_item).strip()
            replacement = self._cell_text(replacement_item).strip()
            if not source and not replacement:
                continue
            invalid_items: list[QTableWidgetItem] = []
            message: str | None = None
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
                invalid_items = [source_item] if source_item is not None else []
                original = seen[source.casefold()]
                original.setBackground(QColor("#ffd9d5"))
                original.setForeground(QColor("#4a1010"))
                original.setToolTip("Duplicate OCR correction source")
            elif source == replacement:
                message = f"{scope} row {row + 1}: replacement must differ."
                invalid_items = [
                    item for item in (source_item, replacement_item) if item is not None
                ]
            elif source_item is not None:
                seen[source.casefold()] = source_item
            if message is None:
                continue
            errors.append(message)
            for item in invalid_items:
                item.setBackground(QColor("#ffd9d5"))
                item.setForeground(QColor("#4a1010"))
                item.setToolTip(message)
                first_item = first_item or item
        return errors, first_item

    def validate_rows(
        self, *, show_valid: bool = True, focus_first: bool = False
    ) -> tuple[str, ...]:
        with QSignalBlocker(self.global_table), QSignalBlocker(self.profile_table):
            errors: list[str] = []
            first_item: QTableWidgetItem | None = None
            for scope, table in (
                ("All games", self.global_table),
                ("Profile", self.profile_table),
            ):
                table_errors, table_first_item = self._validate_table(scope, table)
                errors.extend(table_errors)
                first_item = first_item or table_first_item
            if errors:
                self.status.setPlainText(
                    f"Fix {len(errors)} correction row error(s) before saving:\n"
                    + "\n".join(f"- {message}" for message in errors)
                )
                if focus_first and first_item is not None:
                    table = first_item.tableWidget()
                    if table is not None:
                        page = table.parentWidget()
                        if page is not None:
                            self.tabs.setCurrentWidget(page)
                        table.setCurrentItem(
                            first_item, QItemSelectionModel.SelectionFlag.NoUpdate
                        )
                        table.setFocus()
            elif self._reload_message:
                self.status.setPlainText(self._reload_message)
            elif show_valid and not self._save_active:
                self.status.setPlainText("All correction rows are valid.")
            elif not errors and not self._save_active:
                self.status.clear()
        return tuple(errors)

    def _entries_from_table(self, table: QTableWidget) -> dict[str, str]:
        return {
            source.strip(): replacement.strip()
            for source, replacement in self._table_rows(table)
            if source.strip()
        }

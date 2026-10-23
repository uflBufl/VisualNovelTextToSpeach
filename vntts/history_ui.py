from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QThreadPool
from PySide6.QtGui import QCloseEvent, QTextOption
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.history import DialogueHistory, DialogueHistoryEntry
from vntts.qt_dialogs import CloseGuardedDialog


class DialogueHistoryDialog(CloseGuardedDialog):
    def __init__(
        self,
        history: DialogueHistory,
        replay_handler: Callable[[str, str], object],
        parent: QWidget | None = None,
        *,
        stop_handler: Callable[[], object] | None = None,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self.history = history
        self.replay_handler = replay_handler
        self.stop_handler = stop_handler
        self.replay_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.replay_runner.finished.connect(self._replay_finished)
        self.stop_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.stop_runner.finished.connect(self._stop_finished)
        self._close_pending = False
        self._stop_unsupported = False
        self._spoken_character = ""
        self._replay_error: Exception | None = None
        self.visible_entries: list[DialogueHistoryEntry] = []
        self.setWindowTitle("Dialogue history")
        self.resize(820, 480)

        self._create_entry_widgets()
        self._create_speech_controls()
        self._create_layout()
        self.refresh()

    def _create_entry_widgets(self) -> None:
        self.search = QLineEdit()
        self.search.setPlaceholderText("Speaker or dialogue text")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search dialogue history")
        self.search.textChanged.connect(self.refresh)
        self.search_label = QLabel("&Find a line")
        self.search_label.setBuddy(self.search)
        self.entries = QListWidget()
        self.entries.setAccessibleName("Dialogue history entries")
        self.entries.setWordWrap(True)
        self.entries.setViewportMargins(6, 6, 6, 6)
        self.entries.setSpacing(6)
        self.entries.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.entries.currentRowChanged.connect(self.show_entry)
        self.empty_message = QLabel()
        self.empty_message.setWordWrap(True)
        self.empty_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_message.setMargin(12)
        self.entry_stack = QStackedWidget()
        self.entry_stack.addWidget(self.entries)
        self.entry_stack.addWidget(self.empty_message)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.document().setDocumentMargin(10)
        self.details.setAccessibleName("Selected dialogue details")
        self.details_label = QLabel("Selected &line")
        self.details_label.setBuddy(self.details)

    def _create_speech_controls(self) -> None:
        self.replay_button = QPushButton("Speak with current voice")
        self.replay_button.setEnabled(False)
        self.replay_button.setToolTip(
            "Speak the selected text using its current voice assignment. "
            "This does not replay the original audio."
        )
        self.stop_button = QPushButton("Stop speaking")
        self.stop_button.setAccessibleName("Stop current speech")
        self.stop_button.setEnabled(False)
        self.export_button = QPushButton("Export history...")
        self.export_button.setToolTip(
            "Export all retained lines, including those hidden by search."
        )
        self.status = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.status.setAccessibleName("Dialogue speech status")
        self.status.setFrameShape(QFrame.Shape.NoFrame)
        self.status.viewport().setAutoFillBackground(False)
        self.status.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.status.setFixedHeight(self.status.fontMetrics().lineSpacing() * 2 + 12)
        self.status.installEventFilter(self)
        self.replay_button.clicked.connect(self.replay_selected)
        self.stop_button.clicked.connect(self.stop_replay)
        self.export_button.clicked.connect(self.export_history)

    def _create_layout(self) -> None:
        actions = QHBoxLayout()
        actions.addWidget(self.replay_button)
        actions.addWidget(self.stop_button)
        actions.addStretch()

        entries_layout = QVBoxLayout()
        entries_layout.addWidget(QLabel("Captured this session"))
        entries_layout.addWidget(self.entry_stack)
        details_layout = QVBoxLayout()
        details_layout.addWidget(self.details_label)
        details_layout.addWidget(self.details)
        details_layout.addLayout(actions)
        details_layout.addWidget(self.status)
        content = QHBoxLayout()
        content.addLayout(entries_layout, 1)
        content.addLayout(details_layout, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        footer = QHBoxLayout()
        footer.addWidget(self.export_button)
        footer.addStretch()
        footer.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(self.search_label)
        layout.addWidget(self.search)
        layout.addLayout(content)
        layout.addLayout(footer)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.status and event.type() == QEvent.Type.FontChange:
            self.status.setFixedHeight(self.status.fontMetrics().lineSpacing() * 2 + 12)
        return super().eventFilter(watched, event)

    def refresh(self) -> None:
        selected_id: str | None = None
        entry = self.current_entry()
        if entry is not None:
            selected_id = entry.id
        scroll_bar = self.entries.verticalScrollBar()
        previous_scroll = scroll_bar.value()
        latest = self.history.search(self.search.text())
        self.export_button.setEnabled(bool(self.history.snapshot()))
        self.entry_stack.setCurrentWidget(
            self.entries if latest else self.empty_message
        )
        if not latest:
            self.empty_message.setText(
                "No matching lines. Try another search."
                if self.search.text().strip()
                else "No dialogue has been captured in this session yet."
            )
        if latest == self.visible_entries:
            return
        self.visible_entries = latest
        selected_index = next(
            (
                index
                for index, item in enumerate(self.visible_entries)
                if item.id == selected_id
            ),
            len(self.visible_entries) - 1,
        )
        with QSignalBlocker(self.entries):
            self.entries.clear()
            for entry in self.visible_entries:
                recorded_at = datetime.fromisoformat(entry.recorded_at).astimezone()
                preview = (
                    entry.text if len(entry.text) <= 100 else f"{entry.text[:97]}..."
                )
                self.entries.addItem(
                    f"{recorded_at:%H:%M:%S}  {entry.character}\n{preview}"
                )
            self.entries.setCurrentRow(selected_index)
        self.show_entry(selected_index)
        if not self.visible_entries:
            return
        if selected_id is not None:
            scroll_bar.setValue(min(previous_scroll, scroll_bar.maximum()))
        else:
            scroll_bar.setValue(scroll_bar.maximum())

    def current_entry(self) -> DialogueHistoryEntry | None:
        row = self.entries.currentRow()
        return (
            self.visible_entries[row] if 0 <= row < len(self.visible_entries) else None
        )

    def show_entry(self, row: int) -> None:
        entry = (
            self.visible_entries[row] if 0 <= row < len(self.visible_entries) else None
        )
        self._update_replay_button(entry)
        if entry is None:
            self.details.clear()
            return
        recorded_at = datetime.fromisoformat(entry.recorded_at).astimezone()
        self.details.setPlainText(
            f"{entry.character} · {recorded_at:%b %d, %Y %H:%M}\n\n{entry.text}"
        )

    def _update_replay_button(self, entry: DialogueHistoryEntry | None) -> None:
        self.replay_button.setEnabled(
            entry is not None
            and not self.replay_runner.active
            and not self.stop_runner.active
        )

    def replay_selected(self) -> None:
        entry = self.current_entry()
        if entry is None or self.replay_runner.active or self.stop_runner.active:
            return
        self._stop_unsupported = False
        self._spoken_character = entry.character
        self._replay_error = None
        self.status.setToolTip(entry.text)
        self.replay_button.setEnabled(False)
        self.stop_button.setEnabled(self.stop_handler is not None)
        self.status.setPlainText(
            f"Speaking as {entry.character} with the current voice..."
        )
        self.replay_runner.start(
            self._run_replay,
            self.replay_handler,
            entry.character,
            entry.text,
        )

    @staticmethod
    def _run_replay(
        handler: Callable[[str, str], object], character: str, text: str
    ) -> object:
        result = handler(character, text)
        if isinstance(result, Future):
            return result.result()
        return result

    def _replay_finished(self, _result: object, error: Exception | None) -> None:
        self._replay_error = error
        self.stop_button.setEnabled(False)
        self._update_replay_button(self.current_entry())
        if self.stop_runner.active:
            return
        if error is not None:
            self.status.setPlainText(
                f"Could not speak as {self._spoken_character}: {error}. "
                "Check voice settings, then try again."
            )
        else:
            self.status.setPlainText(f"Speech finished for {self._spoken_character}.")
        if self._close_pending and not self.stop_runner.active:
            self._close_pending = False
            self.close()

    def stop_replay(self, *, close_after: bool = False) -> None:
        if close_after:
            self._close_pending = True
        if self.stop_runner.active:
            return
        if not self.replay_runner.active:
            if self._close_pending:
                self._close_pending = False
                self.close()
            return
        if self.stop_handler is None:
            self.status.setPlainText("This speech engine cannot be stopped here.")
            return
        if self._stop_unsupported:
            self.status.setPlainText(
                "This speech engine cannot stop playback. Wait for it to finish."
            )
            return
        self.replay_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.status.setPlainText(f"Stopping speech for {self._spoken_character}...")
        self.stop_runner.start(self.stop_handler)

    def _stop_finished(self, _result: object, error: Exception | None) -> None:
        if error is not None:
            self._close_pending = False
            self.stop_button.setEnabled(self.replay_runner.active)
            self._update_replay_button(self.current_entry())
            self.status.setPlainText(
                f"Unable to stop speech for {self._spoken_character}: {error}"
            )
            return
        if _result is False and self.replay_runner.active:
            self._stop_unsupported = True
            self.stop_button.setEnabled(False)
            self.status.setPlainText(
                "This speech engine cannot stop playback. Wait for it to finish."
            )
            return
        if _result is False:
            self._replay_finished(None, self._replay_error)
            return
        self.replay_runner.cancel()
        self.stop_button.setEnabled(False)
        self._update_replay_button(self.current_entry())
        self.status.setPlainText(f"Speech stopped for {self._spoken_character}.")
        if self._close_pending:
            self._close_pending = False
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.replay_runner.active or self.stop_runner.active:
            self.stop_replay(close_after=True)
            event.ignore()
            return
        super().closeEvent(event)

    def export_history(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export dialogue history",
            "dialogue-history.txt",
            "Text files (*.txt);;JSON files (*.json)",
        )
        if not path:
            return
        if not path.lower().endswith((".txt", ".json")):
            path += ".json" if "JSON" in selected_filter else ".txt"
        try:
            self.history.export(path)
        except OSError as error:
            QMessageBox.warning(self, "Unable to export history", str(error))

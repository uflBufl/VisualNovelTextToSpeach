from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime

from PySide6.QtCore import QSignalBlocker, Qt, QThreadPool
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
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
        self.visible_entries: list[DialogueHistoryEntry] = []
        self.setWindowTitle("Dialogue history")
        self.resize(820, 560)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Speaker or dialogue text")
        self.search.setAccessibleName("Search dialogue history")
        self.search.textChanged.connect(self.refresh)
        self.search_label = QLabel("&Find a line")
        self.search_label.setBuddy(self.search)
        self.entries = QListWidget()
        self.entries.setAccessibleName("Dialogue history entries")
        self.entries.setWordWrap(True)
        self.entries.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.entries.currentRowChanged.connect(self.show_entry)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Selected dialogue details")
        self.details_label = QLabel("Selected &line")
        self.details_label.setBuddy(self.details)
        self.replay_button = QPushButton("Speak with current voice")
        self.replay_button.setEnabled(False)
        self.stop_button = QPushButton("Stop speaking")
        self.stop_button.setAccessibleName("Stop speaking selected dialogue")
        self.stop_button.setEnabled(False)
        self.export_button = QPushButton("Export session...")
        self.status = QLabel()
        self.status.setAccessibleName("Dialogue speech status")
        self.status.setWordWrap(True)
        self.replay_button.clicked.connect(self.replay_selected)
        self.stop_button.clicked.connect(self.stop_replay)
        self.export_button.clicked.connect(self.export_history)
        actions = QHBoxLayout()
        actions.addWidget(self.replay_button)
        actions.addWidget(self.stop_button)
        actions.addStretch()

        entries_layout = QVBoxLayout()
        entries_layout.addWidget(QLabel("Captured lines"))
        entries_layout.addWidget(self.entries)
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
        layout.addWidget(
            QLabel(
                "Captured this session. Speak again uses the current voice, not the original audio."
            )
        )
        layout.addWidget(self.search_label)
        layout.addWidget(self.search)
        layout.addLayout(content)
        layout.addLayout(footer)

        self.refresh()

    def refresh(self) -> None:
        selected_id: str | None = None
        entry = self.current_entry()
        if entry is not None:
            selected_id = entry.id
        scroll_bar = self.entries.verticalScrollBar()
        previous_scroll = scroll_bar.value()
        latest = self.history.search(self.search.text())
        self.export_button.setEnabled(bool(self.history.snapshot()))
        if not latest:
            self.status.setText(
                "No matching lines. Try another search."
                if self.search.text()
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
        if self.status.text().startswith(("No matching lines", "No dialogue has")):
            self.status.clear()
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
        self.replay_button.setEnabled(False)
        self.stop_button.setEnabled(self.stop_handler is not None)
        self.status.setText(f"Speaking as {entry.character} with the current voice...")
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
        self.stop_button.setEnabled(False)
        self._update_replay_button(self.current_entry())
        if error is not None:
            self.status.setText(
                f"Could not speak this line: {error}. Check voice settings, then try again."
            )
        else:
            self.status.setText("Speech finished.")
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
            self.status.setText("This speech engine cannot be stopped here.")
            return
        if self._stop_unsupported:
            self.status.setText(
                "This speech engine cannot stop playback. Wait for it to finish."
            )
            return
        self.replay_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.status.setText("Stopping speech...")
        self.stop_runner.start(self.stop_handler)

    def _stop_finished(self, _result: object, error: Exception | None) -> None:
        if error is not None:
            self._close_pending = False
            self.stop_button.setEnabled(self.replay_runner.active)
            self._update_replay_button(self.current_entry())
            self.status.setText(f"Unable to stop speech: {error}")
            return
        if _result is False and self.replay_runner.active:
            self._stop_unsupported = True
            self.stop_button.setEnabled(False)
            self.status.setText(
                "This speech engine cannot stop playback. Wait for it to finish."
            )
            return
        self.replay_runner.cancel()
        self.stop_button.setEnabled(False)
        self._update_replay_button(self.current_entry())
        self.status.setText("Speech stopped.")
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

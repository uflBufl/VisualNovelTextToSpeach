from datetime import datetime

from PySide6.QtCore import QSignalBlocker, QTimer
from PySide6.QtWidgets import (
    QDialog,
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
)

from vntts.async_ui import LatestTaskRunner


class DialogueHistoryDialog(QDialog):
    def __init__(
        self,
        history,
        replay_handler,
        parent=None,
        *,
        stop_handler=None,
        thread_pool=None,
    ):
        super().__init__(parent)
        self.history = history
        self.replay_handler = replay_handler
        self.stop_handler = stop_handler
        self.replay_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.replay_runner.finished.connect(self._replay_finished)
        self.stop_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.stop_runner.finished.connect(self._stop_finished)
        self._close_pending = False
        self.visible_entries = []
        self.setWindowTitle("Dialogue history")
        self.resize(820, 560)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search speakers or dialogue...")
        self.search.setAccessibleName("Search dialogue history")
        self.search.textChanged.connect(self.refresh)
        self.search_label = QLabel("&Search dialogue history")
        self.search_label.setBuddy(self.search)
        self.entries = QListWidget()
        self.entries.setAccessibleName("Dialogue history entries")
        self.entries.currentRowChanged.connect(self.show_entry)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Selected dialogue details")
        self.details_label = QLabel("Selected dialogue &details")
        self.details_label.setBuddy(self.details)
        self.replay_button = QPushButton("Replay selected")
        self.stop_button = QPushButton("Stop / skip replay")
        self.stop_button.setAccessibleName("Stop or skip dialogue replay")
        self.stop_button.setEnabled(False)
        self.export_button = QPushButton("Export...")
        self.status = QLabel("Select a dialogue to replay or export this session.")
        self.status.setAccessibleName("Dialogue replay status")
        self.status.setWordWrap(True)
        self.replay_button.clicked.connect(self.replay_selected)
        self.stop_button.clicked.connect(self.stop_replay)
        self.export_button.clicked.connect(self.export_history)
        actions = QHBoxLayout()
        actions.addWidget(self.replay_button)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.export_button)
        actions.addStretch()

        entries_layout = QVBoxLayout()
        entries_layout.addWidget(self.entries)
        details_layout = QVBoxLayout()
        details_layout.addWidget(self.details_label)
        details_layout.addWidget(self.details)
        content = QHBoxLayout()
        content.addLayout(entries_layout, 1)
        content.addLayout(details_layout, 2)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Current application session"))
        layout.addWidget(self.search_label)
        layout.addWidget(self.search)
        layout.addLayout(content)
        layout.addLayout(actions)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        self.timer = QTimer(self)
        self.timer.setInterval(750)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        selected_id = None
        entry = self.current_entry()
        if entry is not None:
            selected_id = entry.id
        scroll_bar = self.entries.verticalScrollBar()
        previous_scroll = scroll_bar.value()
        latest = self.history.search(self.search.text())
        if latest == self.visible_entries:
            return
        self.visible_entries = latest
        signal_blocker = QSignalBlocker(self.entries)
        self.entries.clear()
        for entry in self.visible_entries:
            recorded_at = datetime.fromisoformat(entry.recorded_at)
            preview = entry.text if len(entry.text) <= 58 else f"{entry.text[:55]}..."
            self.entries.addItem(
                f"{recorded_at:%H:%M:%S}  {entry.character}\n{preview}"
            )
        if not self.visible_entries:
            del signal_blocker
            self.show_entry(-1)
            return
        selected_index = next(
            (
                index
                for index, item in enumerate(self.visible_entries)
                if item.id == selected_id
            ),
            len(self.visible_entries) - 1,
        )
        self.entries.setCurrentRow(selected_index)
        del signal_blocker
        self.show_entry(selected_index)
        if selected_id is not None:
            scroll_bar.setValue(min(previous_scroll, scroll_bar.maximum()))
        else:
            scroll_bar.setValue(scroll_bar.maximum())

    def current_entry(self):
        row = self.entries.currentRow()
        return (
            self.visible_entries[row] if 0 <= row < len(self.visible_entries) else None
        )

    def show_entry(self, row):
        entry = (
            self.visible_entries[row] if 0 <= row < len(self.visible_entries) else None
        )
        self.replay_button.setEnabled(
            entry is not None
            and not self.replay_runner.active
            and not self.stop_runner.active
        )
        if entry is None:
            self.details.clear()
            return
        self.details.setPlainText(
            f"{entry.character}\n{entry.recorded_at}\n\n{entry.text}"
        )

    def replay_selected(self):
        entry = self.current_entry()
        if entry is None or self.replay_runner.active:
            return
        self.replay_button.setEnabled(False)
        self.stop_button.setEnabled(self.stop_handler is not None)
        self.status.setText(f"Preparing replay for {entry.character}...")
        self.replay_runner.start(
            self._run_replay,
            self.replay_handler,
            entry.character,
            entry.text,
        )

    @staticmethod
    def _run_replay(handler, character, text):
        result = handler(character, text)
        if hasattr(result, "result") and callable(result.result):
            return result.result()
        return result

    def _replay_finished(self, _result, error):
        self.stop_button.setEnabled(False)
        self.replay_button.setEnabled(
            self.current_entry() is not None and not self.stop_runner.active
        )
        if error is not None:
            self.status.setText(f"Replay failed: {error}. Select Replay to retry.")
        else:
            self.status.setText("Replay finished.")
        if self._close_pending:
            self._close_pending = False
            self.close()

    def stop_replay(self, *, close_after=False):
        if close_after:
            self._close_pending = True
        if not self.replay_runner.active:
            if self._close_pending:
                self._close_pending = False
                self.close()
            return
        if self.stop_handler is None:
            self.status.setText("This replay backend does not expose cancellation.")
            return
        if self.stop_runner.active:
            return
        self.replay_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.status.setText("Stopping the current replay...")
        self.stop_runner.start(self.stop_handler)

    def _stop_finished(self, _result, error):
        if error is not None:
            self._close_pending = False
            self.stop_button.setEnabled(self.replay_runner.active)
            self.status.setText(f"Unable to stop replay: {error}")
            return
        self.replay_runner.cancel()
        self.stop_button.setEnabled(False)
        self.replay_button.setEnabled(self.current_entry() is not None)
        self.status.setText("Replay stopped.")
        if self._close_pending:
            self._close_pending = False
            self.close()

    def closeEvent(self, event):
        if self.replay_runner.active:
            self.stop_replay(close_after=True)
            event.ignore()
            return
        super().closeEvent(event)

    def export_history(self):
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

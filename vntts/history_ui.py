from datetime import datetime

from PySide6.QtCore import QTimer
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
        thread_pool=None,
    ):
        super().__init__(parent)
        self.history = history
        self.replay_handler = replay_handler
        self.replay_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.replay_runner.finished.connect(self._replay_finished)
        self._close_pending = False
        self.visible_entries = []
        self.setWindowTitle("Dialogue history")
        self.resize(820, 560)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search speakers or dialogue...")
        self.search.textChanged.connect(self.refresh)
        self.entries = QListWidget()
        self.entries.currentRowChanged.connect(self.show_entry)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.replay_button = QPushButton("Replay selected")
        self.export_button = QPushButton("Export...")
        self.status = QLabel("Select a dialogue to replay or export this session.")
        self.status.setAccessibleName("Dialogue replay status")
        self.status.setWordWrap(True)
        self.replay_button.clicked.connect(self.replay_selected)
        self.export_button.clicked.connect(self.export_history)
        actions = QHBoxLayout()
        actions.addWidget(self.replay_button)
        actions.addWidget(self.export_button)
        actions.addStretch()

        content = QHBoxLayout()
        content.addWidget(self.entries, 1)
        content.addWidget(self.details, 2)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Current application session"))
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
        latest = self.history.search(self.search.text())
        if latest == self.visible_entries:
            return
        self.visible_entries = latest
        self.entries.clear()
        for entry in self.visible_entries:
            recorded_at = datetime.fromisoformat(entry.recorded_at)
            preview = entry.text if len(entry.text) <= 58 else f"{entry.text[:55]}..."
            self.entries.addItem(
                f"{recorded_at:%H:%M:%S}  {entry.character}\n{preview}"
            )
        if not self.visible_entries:
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
            entry is not None and not self.replay_runner.active
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
        self.replay_button.setEnabled(self.current_entry() is not None)
        if error is not None:
            self.status.setText(f"Replay failed: {error}. Select Replay to retry.")
        else:
            self.status.setText("Replay finished.")
        if self._close_pending:
            self._close_pending = False
            self.close()

    def closeEvent(self, event):
        if self.replay_runner.active:
            self._close_pending = True
            self.status.setText(
                "Replay is still running. Close is deferred until speech finishes."
            )
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

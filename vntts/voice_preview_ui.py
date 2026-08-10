from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)


class VoicePreviewSignals(QObject):
    finished = Signal(bool, str)


class VoicePreviewDialog(QDialog):
    def __init__(self, characters, preview_handler, parent=None):
        super().__init__(parent)
        self.preview_handler = preview_handler
        self.signals = VoicePreviewSignals()
        self.setWindowTitle("Voice previews")
        self.setMinimumWidth(520)

        self.character = QComboBox()
        self.character.addItems(characters)
        self.text = QTextEdit()
        self.text.setPlainText("The storm has passed. We can continue our journey.")
        self.text.setMinimumHeight(100)
        self.preview_button = QPushButton("Play preview")
        self.preview_button.clicked.connect(self.preview)
        self.status = QLabel("Choose the narrator or a character voice.")
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Voice", self.character)
        form.addRow("Preview text", self.text)
        form.addRow("", self.preview_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        self.signals.finished.connect(self.preview_finished)

    def preview(self):
        try:
            future = self.preview_handler(
                self.character.currentText(),
                self.text.toPlainText(),
            )
        except Exception as error:
            self.preview_finished(False, str(error))
            return
        self.preview_button.setEnabled(False)
        self.status.setText("Synthesizing preview...")
        future.add_done_callback(self._future_finished)

    def _future_finished(self, future):
        try:
            character, _text = future.result()
        except Exception as error:
            self.signals.finished.emit(False, str(error))
        else:
            self.signals.finished.emit(True, f"Played {character} preview")

    def preview_finished(self, successful, message):
        self.preview_button.setEnabled(True)
        self.status.setText(message if successful else f"Preview failed: {message}")

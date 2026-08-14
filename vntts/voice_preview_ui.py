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
    def __init__(
        self,
        characters,
        choices,
        preview_handler,
        assignment_handler,
        current_assignment_handler,
        *,
        initial_character=None,
        parent=None,
    ):
        super().__init__(parent)
        self.preview_handler = preview_handler
        self.assignment_handler = assignment_handler
        self.current_assignment_handler = current_assignment_handler
        self.signals = VoicePreviewSignals()
        self.setWindowTitle("Choose character voices")
        self.setMinimumWidth(560)

        self.character = QComboBox()
        self.character.setEditable(True)
        self.character.addItems(characters)
        if initial_character:
            self.character.setCurrentText(initial_character)
        self.voice = QComboBox()
        for choice in choices:
            self.voice.addItem(choice.label, choice.id)
            self.voice.setItemData(self.voice.count() - 1, choice.description, 3)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.text = QTextEdit()
        self.text.setPlainText("The storm has passed. We can continue our journey.")
        self.text.setMinimumHeight(100)
        self.preview_button = QPushButton("Play selected voice")
        self.assign_button = QPushButton("Use for this character")
        self.preview_button.clicked.connect(self.preview)
        self.assign_button.clicked.connect(self.assign)
        self.status = QLabel(
            "Choose a target, listen to candidates, then save the one you prefer."
        )
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Narrator or character", self.character)
        form.addRow("Candidate voice", self.voice)
        form.addRow("", self.description)
        form.addRow("Preview text", self.text)
        form.addRow("", self.preview_button)
        form.addRow("", self.assign_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        self.signals.finished.connect(self.preview_finished)
        self.voice.currentIndexChanged.connect(self.update_description)
        self.character.currentTextChanged.connect(self.select_current_assignment)
        self.update_description()
        self.select_current_assignment()

    def update_description(self):
        self.description.setText(self.voice.currentData(3) or "")

    def select_current_assignment(self):
        source_id = self.current_assignment_handler(self.character.currentText())
        index = self.voice.findData(source_id)
        if index >= 0:
            self.voice.setCurrentIndex(index)

    def preview(self):
        try:
            future = self.preview_handler(
                self.voice.currentData(),
                self.text.toPlainText(),
            )
        except Exception as error:
            self.preview_finished(False, str(error))
            return
        self.preview_button.setEnabled(False)
        self.assign_button.setEnabled(False)
        self.status.setText("Synthesizing preview...")
        future.add_done_callback(self._future_finished)

    def _future_finished(self, future):
        try:
            voice, _text = future.result()
        except Exception as error:
            self.signals.finished.emit(False, str(error))
        else:
            self.signals.finished.emit(True, f"Played {voice} preview")

    def preview_finished(self, successful, message):
        self.preview_button.setEnabled(True)
        self.assign_button.setEnabled(True)
        self.status.setText(message if successful else f"Preview failed: {message}")

    def assign(self):
        try:
            character = self.character.currentText().strip()
            self.assignment_handler(character, self.voice.currentData())
        except Exception as error:
            self.status.setText(f"Voice assignment failed: {error}")
            return
        self.status.setText(
            f"Saved {self.voice.currentText()} for {character or 'the selected target'}"
        )

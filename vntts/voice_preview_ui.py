from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
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
        clear_assignment_handler=None,
        *,
        force_live_handler=None,
        current_force_live_handler=None,
        initial_character=None,
        parent=None,
    ):
        super().__init__(parent)
        self.preview_handler = preview_handler
        self.assignment_handler = assignment_handler
        self.current_assignment_handler = current_assignment_handler
        self.clear_assignment_handler = clear_assignment_handler
        self.force_live_handler = force_live_handler
        self.current_force_live_handler = current_force_live_handler
        self.signals = VoicePreviewSignals()
        self.setWindowTitle("Choose narrator or character voice")
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
        self.automatic_button = QPushButton("Use automatic voice routing")
        self.preview_button.clicked.connect(self.preview)
        self.assign_button.clicked.connect(self.assign)
        self.automatic_button.clicked.connect(self.clear_assignment)
        self.automatic_button.setVisible(clear_assignment_handler is not None)
        self.force_live = QCheckBox(
            "Always use live TTS for Narrator (bypass pregenerated tracks)"
        )
        self.routing_note = QLabel()
        self.routing_note.setWordWrap(True)
        self.status = QLabel(
            "Choose a target, listen to candidates, then save the one you prefer."
        )
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Narrator or character", self.character)
        form.addRow("Routing", self.routing_note)
        form.addRow("Candidate voice", self.voice)
        form.addRow("", self.description)
        form.addRow("Preview text", self.text)
        form.addRow("", self.preview_button)
        form.addRow("", self.assign_button)
        form.addRow("", self.force_live)
        form.addRow("", self.automatic_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        self.signals.finished.connect(self.preview_finished)
        self.voice.currentIndexChanged.connect(self.update_description)
        self.character.currentTextChanged.connect(self.target_changed)
        self.update_description()
        self.target_changed()

    def update_description(self):
        self.description.setText(self.voice.currentData(3) or "")

    def select_current_assignment(self):
        source_id = self.current_assignment_handler(self.character.currentText())
        index = self.voice.findData(source_id)
        if index >= 0:
            self.voice.setCurrentIndex(index)

    def target_changed(self):
        narrator = self.character.currentText().strip().casefold() == "narrator"
        if narrator:
            self.routing_note.setText(
                "The selected voice is used by live fallback. Pregenerated "
                "Narrator tracks keep priority unless force-live is checked."
            )
            self.assign_button.setText("Use selected Narrator fallback voice")
            self.automatic_button.setText("Use default Narrator voice")
            self.force_live.setVisible(self.force_live_handler is not None)
            self.force_live.setChecked(
                bool(
                    self.current_force_live_handler()
                    if self.current_force_live_handler is not None
                    else False
                )
            )
        else:
            self.routing_note.setText(
                "A saved character override takes priority over original, "
                "pregenerated, and automatic voice routing."
            )
            self.assign_button.setText("Use for this character")
            self.automatic_button.setText("Use automatic voice routing")
            self.force_live.setVisible(False)
        self.select_current_assignment()

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
            if (
                character.casefold() == "narrator"
                and self.force_live_handler is not None
            ):
                self.force_live_handler(self.force_live.isChecked())
        except Exception as error:
            self.status.setText(f"Voice assignment failed: {error}")
            return
        if character.casefold() == "narrator":
            message = f"Saved {self.voice.currentText()} as the Narrator live fallback"
            if self.force_live.isChecked():
                message += " (force-live)"
        else:
            message = (
                f"Saved {self.voice.currentText()} for "
                f"{character or 'the selected target'}"
            )
        self.status.setText(message)

    def clear_assignment(self):
        if self.clear_assignment_handler is None:
            return
        character = self.character.currentText().strip()
        try:
            self.clear_assignment_handler(character)
        except Exception as error:
            self.status.setText(f"Unable to restore automatic routing: {error}")
            return
        if character.casefold() == "narrator":
            self.force_live.setChecked(False)
        self.select_current_assignment()
        self.status.setText(
            "Default Narrator voice and generated-first routing restored"
            if character.casefold() == "narrator"
            else f"Automatic voice routing restored for {character}"
        )

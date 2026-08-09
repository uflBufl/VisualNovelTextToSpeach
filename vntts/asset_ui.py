from pathlib import Path
from threading import Event, Thread

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from vntts.assets import (
    ModelAssetManager,
    ModelDownloadCancelled,
    VoicePackManager,
)

default_model = "tts_models/multilingual/multi-dataset/xtts_v2"


class AssetSignals(QObject):
    progress = Signal(object, str)
    model_finished = Signal(bool, str)
    voice_imported = Signal(str, str)


class VoiceImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add character voice")
        self.character = QLineEdit()
        self.aliases = QLineEdit()
        self.references = []
        self.reference_label = QLabel("No files selected")
        choose_button = QPushButton("Choose audio files...")
        choose_button.clicked.connect(self.choose_references)

        form = QFormLayout()
        form.addRow("Character", self.character)
        form.addRow("Aliases (comma-separated)", self.aliases)
        form.addRow("References", self.reference_label)
        form.addRow("", choose_button)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def choose_references(self):
        files, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "Choose local voice references",
            "",
            "Audio files (*.wav *.ogg *.flac *.mp3 *.m4a);;All files (*)",
        )
        if files:
            self.references = files
            self.reference_label.setText(
                f"{len(files)} file(s): {', '.join(Path(path).name for path in files)}"
            )

    def validate_and_accept(self):
        if not self.character.text().strip():
            QMessageBox.warning(self, "Missing character", "Enter a character name.")
            return
        if not self.references:
            QMessageBox.warning(
                self,
                "Missing references",
                "Select at least one local audio reference.",
            )
            return
        self.accept()

    def values(self):
        aliases = [
            alias.strip() for alias in self.aliases.text().split(",") if alias.strip()
        ]
        return self.character.text().strip(), self.references, aliases


class AssetManagerDialog(QDialog):
    def __init__(
        self,
        settings,
        *,
        model_manager=None,
        voice_manager=None,
        parent=None,
    ):
        super().__init__(parent)
        self.settings_value = settings
        self.model_manager = model_manager or ModelAssetManager()
        self.voice_manager = voice_manager or VoicePackManager()
        self.signals = AssetSignals()
        self.cancel_event = Event()
        self.operation_running = False
        self.setWindowTitle("Models and character voices")
        self.setMinimumSize(680, 440)

        tabs = QTabWidget()
        tabs.addTab(self._create_models_tab(), "Speech model")
        tabs.addTab(self._create_voices_tab(), "Character voices")
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept_settings)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(self.buttons)

        self.signals.progress.connect(self.update_progress)
        self.signals.model_finished.connect(self.model_finished)
        self.signals.voice_imported.connect(self.voice_imported)

    def _create_models_tab(self):
        tab = QWidget()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItem(default_model)
        if self.settings_value.tts_model:
            self.model.setCurrentText(self.settings_value.tts_model)
        self.model_path = QLabel(str(self.model_manager.model_path(self.model_name())))
        self.model_path.setWordWrap(True)
        self.model.currentTextChanged.connect(
            lambda _text: self.model_path.setText(
                str(self.model_manager.model_path(self.model_name()))
            )
        )
        self.model_status = QLabel("Use Verify to check an existing download.")
        self.model_status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.download_button = QPushButton("Download / Retry")
        self.cancel_button = QPushButton("Cancel download")
        self.verify_button = QPushButton("Verify checksums")
        self.cancel_button.setEnabled(False)
        self.download_button.clicked.connect(self.download_model)
        self.cancel_button.clicked.connect(self.cancel_download)
        self.verify_button.clicked.connect(self.verify_model)
        actions = QHBoxLayout()
        actions.addWidget(self.download_button)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.verify_button)
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("Model"))
        layout.addWidget(self.model)
        layout.addWidget(QLabel("Application-owned model directory"))
        layout.addWidget(self.model_path)
        layout.addWidget(self.model_status)
        layout.addWidget(self.progress)
        layout.addLayout(actions)
        layout.addStretch()
        return tab

    def _create_voices_tab(self):
        tab = QWidget()
        self.voice_manifest = QLineEdit(self.settings_value.voice_manifest or "")
        self.voice_status = QLabel(
            "Import an existing manifest or add local references for one character."
        )
        self.voice_status.setWordWrap(True)
        import_pack = QPushButton("Import voice pack...")
        add_voice = QPushButton("Add character voice...")
        import_pack.clicked.connect(self.import_voice_pack)
        add_voice.clicked.connect(self.add_character_voice)
        actions = QHBoxLayout()
        actions.addWidget(import_pack)
        actions.addWidget(add_voice)
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("Active voice manifest"))
        layout.addWidget(self.voice_manifest)
        layout.addWidget(self.voice_status)
        layout.addLayout(actions)
        layout.addStretch()
        return tab

    def model_name(self):
        return self.model.currentText().strip()

    def download_model(self):
        if self.operation_running:
            return
        if not self.model_name():
            QMessageBox.warning(self, "No model", "Choose a model to download.")
            return
        if (
            "xtts" in self.model_name().casefold()
            and not self.settings_value.xtts_terms_accepted
        ):
            QMessageBox.warning(
                self,
                "Model license not accepted",
                "Accept the CPML terms in Settings or setup before downloading XTTS.",
            )
            return
        self.cancel_event = Event()
        self.set_operation_running(True)
        self.model_status.setText("Preparing model download...")
        model_name = self.model_name()
        Thread(target=self._download_model, args=(model_name,), daemon=True).start()

    def _download_model(self, model_name):
        try:
            path = self.model_manager.download(
                model_name,
                progress=self.signals.progress.emit,
                cancel_event=self.cancel_event,
            )
        except ModelDownloadCancelled as error:
            self.signals.model_finished.emit(False, str(error))
        except Exception as error:
            self.signals.model_finished.emit(False, f"Model download failed: {error}")
        else:
            self.signals.model_finished.emit(True, f"Model ready at {path}")

    def cancel_download(self):
        self.cancel_event.set()
        self.model_status.setText("Cancelling after the current network chunk...")

    def verify_model(self):
        if self.operation_running:
            return
        self.set_operation_running(True)
        self.model_status.setText("Verifying model checksums...")
        model_name = self.model_name()

        def verify():
            try:
                path = self.model_manager.validate(model_name)
            except Exception as error:
                self.signals.model_finished.emit(False, f"Verification failed: {error}")
            else:
                self.signals.model_finished.emit(True, f"Checksums passed at {path}")

        Thread(target=verify, daemon=True).start()

    def update_progress(self, percent, message):
        if percent is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
        self.model_status.setText(message)

    def model_finished(self, successful, message):
        self.set_operation_running(False)
        self.progress.setRange(0, 100)
        if successful:
            self.progress.setValue(100)
            self.settings_value = self.settings_value.updated(
                tts_model=self.model_name()
            )
        self.model_status.setText(message)

    def set_operation_running(self, running):
        self.operation_running = running
        self.download_button.setEnabled(not running)
        self.verify_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.buttons.setEnabled(not running)

    def import_voice_pack(self):
        source, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Import local voice manifest",
            "",
            "JSON files (*.json);;All files (*)",
        )
        if not source:
            return
        try:
            manifest = self.voice_manager.import_pack(source)
        except Exception as error:
            QMessageBox.warning(self, "Voice pack import failed", str(error))
            return
        self.signals.voice_imported.emit(str(manifest), "Voice pack imported")

    def add_character_voice(self):
        dialog = VoiceImportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        character, references, aliases = dialog.values()
        try:
            manifest = self.voice_manager.import_voice(
                character,
                references,
                aliases=aliases,
            )
        except Exception as error:
            QMessageBox.warning(self, "Voice import failed", str(error))
            return
        self.signals.voice_imported.emit(
            str(manifest),
            f"Imported {len(references)} reference(s) for {character}",
        )

    def voice_imported(self, manifest, message):
        self.voice_manifest.setText(manifest)
        self.voice_status.setText(message)
        self.settings_value = self.settings_value.updated(voice_manifest=manifest)

    def accept_settings(self):
        if self.operation_running:
            return
        manifest = self.voice_manifest.text().strip() or None
        if manifest:
            try:
                self.voice_manager.validate(manifest)
            except Exception as error:
                QMessageBox.warning(self, "Invalid voice manifest", str(error))
                return
        self.settings_value = self.settings_value.updated(
            tts_model=self.model_name() or None,
            voice_manifest=manifest,
        )
        self.accept()

    def settings(self):
        return self.settings_value

    def reject(self):
        if self.operation_running:
            self.cancel_download()
            QMessageBox.information(
                self,
                "Download cancellation requested",
                "Wait for the current network chunk to stop before closing.",
            )
            return
        super().reject()

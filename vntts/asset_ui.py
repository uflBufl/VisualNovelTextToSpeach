from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Protocol, TypeAlias

from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtGui import QCloseEvent, QPalette
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
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.file_integrity import sha256_file

from vntts.assets import (
    ModelAssetManager,
    ModelDownloadCancelled,
    VoicePackManager,
)
from vntts.async_ui import LatestTaskRunner
from vntts.settings import AppSettings

default_model = "tts_models/multilingual/multi-dataset/xtts_v2"

ManifestIdentity: TypeAlias = tuple[str, str]
ManifestValidationResult: TypeAlias = tuple[str, str, str]
ProgressCallback: TypeAlias = Callable[[int | None, str], None]
VoiceImportOperation: TypeAlias = Callable[..., object]


class _ModelManager(Protocol):
    def model_path(self, model_name: str) -> Path: ...

    def download(
        self,
        model_name: str,
        *,
        progress: ProgressCallback,
        cancel_event: Event,
    ) -> Path: ...

    def validate(self, model_name: str) -> Path: ...


class _VoiceManager(Protocol):
    def import_pack(self, source_manifest: str) -> Path: ...

    def import_voice(
        self,
        character: str,
        reference_files: tuple[str, ...],
        *,
        aliases: tuple[str, ...],
    ) -> Path: ...

    def validate(self, manifest_path: Path) -> Path: ...


class AssetSignals(QObject):
    progress = Signal(object, str)
    voice_imported = Signal(str, str)


class VoiceImportDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add character voice")
        self.character = QLineEdit()
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("Comma-separated names")
        self.references: list[str] = []
        self.reference_files = QPlainTextEdit("No files selected")
        self.reference_files.setReadOnly(True)
        self.reference_files.setFixedHeight(76)
        self.reference_files.setAccessibleName("Selected audio references")
        choose_button = QPushButton("Choose audio files...")
        choose_button.clicked.connect(self.choose_references)

        form = QFormLayout()
        form.addRow("Character (required)", self.character)
        form.addRow("Aliases (optional)", self.aliases)
        reference_controls = QVBoxLayout()
        reference_controls.addWidget(self.reference_files)
        picker_row = QHBoxLayout()
        picker_row.addWidget(choose_button)
        picker_row.addStretch()
        reference_controls.addLayout(picker_row)
        form.addRow("Audio references (required)", reference_controls)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.add_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.add_button.setText("Add voice")
        self.add_button.setEnabled(False)
        self.character.textChanged.connect(self._update_add_button)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def choose_references(self) -> None:
        files, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "Choose local voice references",
            str(Path(self.references[0]).parent) if self.references else "",
            "Audio files (*.wav *.ogg *.flac *.mp3 *.m4a);;All files (*)",
        )
        if files:
            self.set_references(files)

    def set_references(self, files: list[str]) -> None:
        self.references = list(files)
        self.reference_files.setPlainText(
            "\n".join(Path(path).name for path in files)
            if files
            else "No files selected"
        )
        self.reference_files.setToolTip("\n".join(files))
        self._update_add_button()

    def _update_add_button(self) -> None:
        self.add_button.setEnabled(
            bool(self.character.text().strip() and self.references)
        )

    def validate_and_accept(self) -> None:
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

    def values(self) -> tuple[str, list[str], list[str]]:
        aliases = [
            alias.strip() for alias in self.aliases.text().split(",") if alias.strip()
        ]
        return self.character.text().strip(), self.references, aliases


class AssetManagerDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        *,
        model_manager: _ModelManager | None = None,
        voice_manager: _VoiceManager | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings_value: AppSettings = settings
        self.model_manager: _ModelManager = model_manager or ModelAssetManager()
        self.voice_manager: _VoiceManager = voice_manager or VoicePackManager()
        self.voice_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.voice_runner.finished.connect(self._voice_import_finished)
        self.model_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.model_runner.finished.connect(self._model_operation_finished)
        self.manifest_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.manifest_runner.finished.connect(self._manifest_validation_finished)
        self.signals = AssetSignals()
        self.cancel_event = Event()
        self.operation_running = False
        self.operation_kind: str | None = None
        self._operation_model_name: str | None = None
        self._close_pending = False
        self._voice_import_message: str | None = None
        self._voice_import_from_audio = False
        self._voice_import_draft: tuple[str, list[str], list[str]] | None = None
        self._validated_manifest_identity: ManifestIdentity | None = None
        self._accept_after_manifest_validation = False
        self.model_management_available = settings.speech_backend == "coqui-xtts"
        self.setWindowTitle(
            "Models and character voices"
            if self.model_management_available
            else "Character voices"
        )
        self.setMinimumSize(680, 300)

        self.tabs = QTabWidget()
        self.models_tab = self._create_models_tab()
        if self.model_management_available:
            self.tabs.addTab(self.models_tab, "Speech model")
        self.tabs.addTab(self._create_voices_tab(), "Character voices")
        self.tabs.tabBar().setVisible(self.model_management_available)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        for button in (
            self.download_button,
            self.verify_button,
            self.import_pack_button,
            self.add_voice_button,
            self.validate_manifest_button,
            self.buttons.button(QDialogButtonBox.StandardButton.Save),
        ):
            palette = button.palette()
            palette.setColor(
                QPalette.ColorGroup.Disabled,
                QPalette.ColorRole.ButtonText,
                palette.color(QPalette.ColorRole.Mid),
            )
            button.setPalette(palette)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            "Save selection"
        )
        self.buttons.accepted.connect(self.accept_settings)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(self.buttons)

        self.signals.progress.connect(self.update_progress)
        self.signals.voice_imported.connect(self.voice_imported)

    def _create_models_tab(self) -> QWidget:
        tab = QWidget()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItem(default_model)
        if self.settings_value.tts_model:
            self.model.setCurrentText(self.settings_value.tts_model)
        self.model_path = QLabel(str(self.model_manager.model_path(self.model_name())))
        self.model_path.setWordWrap(True)
        self.model.currentTextChanged.connect(self._model_changed)
        self.model_status = QLabel(
            "Installation status not checked. Verify checksums to confirm this model."
        )
        self.model_status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        self.download_button = QPushButton("Download model")
        self.cancel_button = QPushButton("Cancel download")
        self.cancel_button.hide()
        self.verify_button = QPushButton("Verify checksums")
        self.cancel_button.setEnabled(False)
        self.download_button.clicked.connect(self.download_model)
        self.cancel_button.clicked.connect(self.cancel_download)
        self.verify_button.clicked.connect(self.verify_model)
        actions = QHBoxLayout()
        actions.addWidget(self.verify_button)
        actions.addWidget(self.download_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
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

    def _model_changed(self, _text: str) -> None:
        self.model_path.setText(str(self.model_manager.model_path(self.model_name())))
        if not self.operation_running:
            self.model_status.setText(
                "Installation status not checked. Verify checksums to confirm this model."
            )
            self.model_status.setStyleSheet("")
            self.download_button.setText("Download model")

    def _create_voices_tab(self) -> QWidget:
        tab = QWidget()
        self.voice_manifest = QLineEdit(self.settings_value.voice_manifest or "")
        self.voice_manifest.setAccessibleName("Active voice manifest")
        self.voice_manifest.setAccessibleDescription(
            "Path to the active character voice manifest JSON file"
        )
        self.voice_status = QLabel(
            "Files not checked. Verify files to confirm this manifest."
            if self.voice_manifest.text().strip()
            else "No active voice manifest selected. Live voice assignments are disabled."
        )
        self.voice_status.setWordWrap(True)
        self.voice_status.setAccessibleName("Voice manifest and import status")
        self.voice_progress = QProgressBar()
        self.voice_progress.setRange(0, 100)
        self.voice_progress.setValue(0)
        self.voice_progress.hide()
        self.import_pack_button = QPushButton("Import manifest into library...")
        self.add_voice_button = QPushButton("Add voice from audio...")
        self.browse_manifest_button = QPushButton("Choose existing...")
        self.validate_manifest_button = QPushButton("Verify files")
        self.browse_manifest_button.setAccessibleName(
            "Browse for active voice manifest"
        )
        self.browse_manifest_button.setAccessibleDescription(
            "Choose an existing voice manifest JSON file"
        )
        self.validate_manifest_button.setAccessibleName(
            "Validate active voice manifest"
        )
        self.validate_manifest_button.setAccessibleDescription(
            "Checksum-validate the selected manifest and all referenced voice files"
        )
        self.import_pack_button.clicked.connect(self.import_voice_pack)
        self.add_voice_button.clicked.connect(self.add_character_voice)
        self.browse_manifest_button.clicked.connect(self.browse_voice_manifest)
        self.validate_manifest_button.clicked.connect(self.validate_voice_manifest)
        self.voice_manifest.textChanged.connect(self._voice_manifest_edited)
        manifest_selector = QHBoxLayout()
        manifest_selector.addWidget(self.voice_manifest, 1)
        manifest_selector.addWidget(self.browse_manifest_button)
        manifest_selector.addWidget(self.validate_manifest_button)
        actions = QHBoxLayout()
        actions.addWidget(self.import_pack_button)
        actions.addWidget(self.add_voice_button)
        actions.addStretch()
        layout = QVBoxLayout(tab)
        manifest_label = QLabel("Active voice manifest")
        manifest_label.setBuddy(self.voice_manifest)
        layout.addWidget(manifest_label)
        layout.addLayout(manifest_selector)
        layout.addWidget(self.voice_status)
        layout.addWidget(self.voice_progress)
        layout.addLayout(actions)
        layout.addStretch()
        self.validate_manifest_button.setEnabled(
            bool(self.voice_manifest.text().strip())
        )
        self.setTabOrder(self.voice_manifest, self.browse_manifest_button)
        self.setTabOrder(self.browse_manifest_button, self.validate_manifest_button)
        self.setTabOrder(self.validate_manifest_button, self.import_pack_button)
        self.setTabOrder(self.import_pack_button, self.add_voice_button)
        return tab

    def _voice_manifest_edited(self) -> None:
        manifest = self.voice_manifest.text().strip()
        self.manifest_runner.cancel()
        self._validated_manifest_identity = None
        self._accept_after_manifest_validation = False
        self._set_manifest_validation_pending(False)
        self.voice_progress.setValue(0)
        self.voice_status.setStyleSheet("")
        self.validate_manifest_button.setEnabled(
            bool(manifest) and not self.operation_running
        )
        if manifest:
            self.voice_status.setText(
                "Manifest path changed. Validate it before saving."
            )
        else:
            self.voice_status.setText(
                "No active voice manifest selected. Live voice assignments are disabled."
            )

    def browse_voice_manifest(self) -> None:
        current = self.voice_manifest.text().strip()
        start = ""
        if current:
            candidate = Path(current).expanduser()
            start = str(candidate if candidate.is_dir() else candidate.parent)
        source, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose active voice manifest",
            start,
            "JSON files (*.json);;All files (*)",
        )
        if not source:
            return
        self.voice_manifest.setText(source)
        self.validate_voice_manifest()

    def validate_voice_manifest(self) -> bool:
        manifest = self.voice_manifest.text().strip()
        if not manifest:
            self.voice_status.setText(
                "No active voice manifest selected. Live voice assignments are disabled."
            )
            return True
        self._set_manifest_validation_pending(True)
        self.voice_status.setText(
            "Checksum-validating the selected manifest and voice files..."
        )
        self.manifest_runner.start(self._validate_manifest_snapshot, manifest)
        return False

    def _validate_manifest_snapshot(self, manifest: str) -> ManifestValidationResult:
        path = Path(manifest).expanduser().resolve()
        before = sha256_file(path)
        validated = self.voice_manager.validate(path)
        after = sha256_file(path)
        if after != before:
            raise ValueError("Voice manifest changed while validation was running")
        return str(path), after, str(validated)

    def _manifest_validation_finished(
        self, result: ManifestValidationResult, error: Exception | None
    ) -> None:
        self._set_manifest_validation_pending(False)
        selected = self.voice_manifest.text().strip()
        if error is not None:
            self._validated_manifest_identity = None
            self._accept_after_manifest_validation = False
            self.voice_status.setText(
                f"Voice manifest is invalid: {error}. Check its audio paths or "
                "choose another manifest, then Verify files."
            )
            self.voice_status.setStyleSheet("font-weight: 600;")
            self.voice_manifest.setFocus()
            self.voice_manifest.selectAll()
            self.voice_progress.setValue(0)
            return
        path, digest, validated = result
        try:
            selected_path = str(Path(selected).expanduser().resolve())
            selected_digest = sha256_file(selected_path)
        except OSError:
            return
        if selected_path != path or selected_digest != digest:
            return
        self._validated_manifest_identity = (path, digest)
        self.voice_progress.setValue(100)
        self.voice_status.setStyleSheet("")
        self.voice_status.setText(
            f"Voice manifest passed checksum validation: {validated}"
        )
        if self._accept_after_manifest_validation:
            self._accept_after_manifest_validation = False
            self._accept_validated_settings()

    def _set_manifest_validation_pending(self, pending: bool) -> None:
        pending = bool(pending)
        self.voice_progress.setRange(0, 0 if pending else 100)
        self.voice_progress.setVisible(pending)
        self.validate_manifest_button.setText(
            "Validating..." if pending else "Verify files"
        )
        self.validate_manifest_button.setEnabled(
            not pending
            and not self.operation_running
            and bool(self.voice_manifest.text().strip())
        )
        self.import_pack_button.setEnabled(not pending and not self.operation_running)
        self.add_voice_button.setEnabled(not pending and not self.operation_running)
        if hasattr(self, "buttons"):
            save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
            save.setEnabled(not pending and not self.operation_running)

    def model_name(self) -> str:
        return self.model.currentText().strip()

    def download_model(self) -> None:
        if self.operation_running or not self.model_management_available:
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
        self.set_operation_running(True, "download")
        self.model_status.setStyleSheet("")
        self.model_status.setText("Preparing model download...")
        self.model_runner.start(self._download_model, self.model_name())

    def _download_model(self, model_name: str) -> Path:
        return self.model_manager.download(
            model_name,
            progress=self.signals.progress.emit,
            cancel_event=self.cancel_event,
        )

    def cancel_download(self) -> None:
        self.cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.model_status.setText("Cancelling after the current network chunk...")

    def verify_model(self) -> None:
        if self.operation_running or not self.model_management_available:
            return
        if not self.model_name():
            QMessageBox.warning(self, "No model", "Choose a model to verify.")
            return
        self.set_operation_running(True, "verify")
        self.model_status.setStyleSheet("")
        self.model_status.setText("Verifying model checksums...")
        self.model_runner.start(self.model_manager.validate, self.model_name())

    def _model_operation_finished(self, path: object, error: Exception | None) -> None:
        if self.operation_kind == "download":
            message = (
                f"Model ready at {path}"
                if error is None
                else str(error)
                if isinstance(error, ModelDownloadCancelled)
                else f"Model download failed: {error}"
            )
        else:
            message = (
                f"Model verified and ready at {path}"
                if error is None
                else f"Verification failed: {error}"
            )
        self.model_finished(error is None, message)

    def update_progress(self, percent: int | None, message: str) -> None:
        if percent is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
        self.model_status.setText(message)

    def model_finished(self, successful: bool, message: str) -> None:
        operation_kind = self.operation_kind
        if self.model_name() != self._operation_model_name:
            successful = False
            message = "Selected model changed during the operation"
        self.set_operation_running(False)
        self.progress.setRange(0, 100)
        if successful:
            self.progress.setValue(100)
            self.settings_value = self.settings_value.updated(
                tts_model=self.model_name()
            )
        self.download_button.setText(
            "Retry download"
            if not successful and operation_kind == "download"
            else "Download model"
        )
        self.model_status.setStyleSheet("" if successful else "font-weight: 600;")
        self.model_status.setText(
            f"{message}. Model not ready; Save only records the selection. "
            "Resolve the issue, then Retry download."
            if not successful and operation_kind == "download"
            else f"{message}. Model not ready; Save only records the selection."
            if not successful
            else message
        )
        if self._close_pending:
            self._close_pending = False
            self.close()

    def set_operation_running(self, running: bool, kind: str | None = None) -> None:
        self.operation_running = running
        self.operation_kind = kind if running else None
        self._operation_model_name = (
            self.model_name() if running and kind in {"download", "verify"} else None
        )
        self.model.setEnabled(not running)
        self.progress.setVisible(running and kind in {"download", "verify"})
        if running and kind in {"download", "verify"}:
            self.progress.setRange(0, 0)
        self.download_button.setEnabled(not running)
        self.verify_button.setEnabled(not running)
        self.cancel_button.setEnabled(running and kind == "download")
        self.cancel_button.setVisible(running and kind == "download")
        self.import_pack_button.setEnabled(not running)
        self.add_voice_button.setEnabled(not running)
        self.voice_manifest.setEnabled(not running)
        self.browse_manifest_button.setEnabled(not running)
        self.validate_manifest_button.setEnabled(
            not running and bool(self.voice_manifest.text().strip())
        )
        self.buttons.setEnabled(not running)
        self._set_manifest_validation_pending(self.manifest_runner.active)

    def import_voice_pack(self) -> None:
        source, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Import local voice manifest",
            "",
            "JSON files (*.json);;All files (*)",
        )
        if not source:
            return
        self._start_voice_import(
            self.voice_manager.import_pack,
            source,
            message="Voice pack imported",
        )

    def add_character_voice(self) -> None:
        dialog = VoiceImportDialog(self)
        if self._voice_import_draft is not None:
            character, references, aliases = self._voice_import_draft
            dialog.character.setText(character)
            dialog.aliases.setText(", ".join(aliases))
            dialog.set_references(references)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        character, references, aliases = dialog.values()
        self._voice_import_draft = character, references, aliases
        self._start_voice_import(
            self.voice_manager.import_voice,
            character,
            tuple(references),
            aliases=tuple(aliases),
            message=f"Imported {len(references)} reference(s) for {character}",
            from_audio=True,
        )

    def _start_voice_import(
        self,
        operation: VoiceImportOperation,
        *arguments: object,
        message: str,
        from_audio: bool = False,
        **keyword_arguments: object,
    ) -> None:
        if self.operation_running:
            return
        self._voice_import_message = message
        self._voice_import_from_audio = from_audio
        self.set_operation_running(True, "voice-import")
        self.voice_progress.setRange(0, 0)
        self.voice_progress.show()
        self.voice_status.setStyleSheet("")
        self.voice_status.setText(
            "Importing and checksum-validating voice files in the background..."
        )
        self.voice_runner.start(operation, *arguments, **keyword_arguments)

    def _voice_import_finished(self, manifest: object, error: Exception | None) -> None:
        message = self._voice_import_message
        from_audio = self._voice_import_from_audio
        self._voice_import_message = None
        self._voice_import_from_audio = False
        self.set_operation_running(False)
        self.voice_progress.setRange(0, 100)
        if error is not None:
            self.voice_progress.setValue(0)
            self.voice_status.setText(
                f"Voice import failed: {error}. "
                + (
                    "Choose Add voice from audio to revise and retry."
                    if from_audio
                    else "Choose the source again to retry."
                )
            )
            self.voice_status.setStyleSheet("font-weight: 600;")
        else:
            self.voice_imported(str(manifest), message or "Voice import complete")
            if from_audio:
                self._voice_import_draft = None
            self.voice_progress.setValue(100)
        self.voice_progress.hide()
        if self._close_pending:
            self._close_pending = False
            self.close()

    def voice_imported(self, manifest: str, message: str) -> None:
        self.voice_manifest.setText(manifest)
        self.voice_status.setStyleSheet("")
        self.voice_status.setText(message)
        self.settings_value = self.settings_value.updated(voice_manifest=manifest)

    def accept_settings(self) -> None:
        if self.operation_running:
            return
        manifest = self.voice_manifest.text().strip() or None
        if manifest is None:
            self._accept_validated_settings()
            return
        try:
            identity = (
                str(Path(manifest).expanduser().resolve()),
                sha256_file(manifest),
            )
        except OSError:
            identity = None
        if identity != self._validated_manifest_identity:
            self._accept_after_manifest_validation = True
            if not self.manifest_runner.active:
                self.validate_voice_manifest()
            return
        self._accept_validated_settings()

    def _accept_validated_settings(self) -> None:
        manifest = self.voice_manifest.text().strip() or None
        model = (
            self.model_name() or None
            if self.model_management_available
            else self.settings_value.tts_model
        )
        self.settings_value = self.settings_value.updated(
            tts_model=model,
            voice_manifest=manifest,
        )
        self.accept()

    def settings(self) -> AppSettings:
        return self.settings_value

    def reject(self) -> None:
        if self.manifest_runner.active:
            self.manifest_runner.cancel()
            self._accept_after_manifest_validation = False
        if self.operation_running:
            if self.operation_kind == "verify":
                self.model_runner.cancel()
                self.set_operation_running(False)
                super().reject()
                return
            self._close_pending = True
            if self.operation_kind == "download":
                self.cancel_download()
                QMessageBox.information(
                    self,
                    "Download cancellation requested",
                    "Wait for the current network chunk to stop before closing.",
                )
            else:
                self.voice_status.setText(
                    "Close is deferred until the current verification or import finishes."
                )
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.manifest_runner.active:
            self.manifest_runner.cancel()
            self._accept_after_manifest_validation = False
        if self.operation_running:
            if self.operation_kind == "verify":
                self.model_runner.cancel()
                self.set_operation_running(False)
                super().closeEvent(event)
                return
            self._close_pending = True
            if self.operation_kind == "download":
                self.cancel_download()
            self.voice_status.setText(
                "Close is deferred until the current model or voice operation finishes."
            )
            event.ignore()
            return
        super().closeEvent(event)

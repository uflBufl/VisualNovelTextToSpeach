from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Literal, Protocol, TypeAlias

from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool, Signal
from PySide6.QtGui import QCloseEvent, QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from vntts.action_icons import set_action_icon
from vntts.asset_integrity import ModelIntegrityError
from vntts.async_ui import LatestTaskRunner
from vntts.document_identity import file_sha256
from vntts.model_assets import ModelAssetManager, ModelDownloadCancelled
from vntts.settings import AppSettings
from vntts.ui_text import make_text_copyable
from vntts.voice_packs import VoicePackManager, VoiceReplacementRequired
from vntts.voices import normalize_character_name

default_model = "tts_models/multilingual/multi-dataset/xtts_v2"

ManifestIdentity: TypeAlias = tuple[str, str]
ManifestValidationResult: TypeAlias = tuple[str, str, str]
ProgressCallback: TypeAlias = Callable[[int | None, str], None]
VoiceImportOperation: TypeAlias = Callable[..., Path]
AssetOperationKind: TypeAlias = Literal["download", "verify", "voice-import"]


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
        expected_manifest_sha256: str | None = None,
    ) -> Path: ...

    def validate(self, manifest_path: Path) -> Path: ...


class AssetSignals(QObject):
    progress = Signal(object, str)


def build_voice_replacement_prompt(
    character: str, parent: QWidget | None = None
) -> QMessageBox:
    dialog = QMessageBox(parent)
    dialog.setWindowTitle("Replace existing voice?")
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setText(f"Replace the voice for {character}?")
    dialog.setInformativeText(
        "This replaces all recordings and other names stored for this character "
        "in your custom library. The change is saved immediately."
    )
    dialog.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
    )
    dialog.button(QMessageBox.StandardButton.Yes).setText("Replace voice")
    set_action_icon(dialog.button(QMessageBox.StandardButton.Yes), "refresh-cw")
    set_action_icon(dialog.button(QMessageBox.StandardButton.Cancel), "x")
    dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
    dialog.setEscapeButton(QMessageBox.StandardButton.Cancel)
    return dialog


class VoiceImportDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add character voice")
        self.character = QLineEdit()
        self.aliases = QLineEdit()
        self.aliases.setPlaceholderText("Comma-separated names")
        self.references: list[str] = []
        self.reference_files = QPlainTextEdit(
            "No files selected", readOnly=True, tabChangesFocus=True
        )
        self.reference_files.setFixedHeight(
            self.reference_files.fontMetrics().lineSpacing() * 3 + 12
        )
        self.reference_files.installEventFilter(self)
        self.reference_files.setAccessibleName("Selected audio references")
        self.choose_button = QPushButton("Choose audio files...")
        set_action_icon(self.choose_button, "folder-open")
        self.choose_button.clicked.connect(self.choose_references)

        form = QFormLayout()
        form.addRow("Character (required)", self.character)
        form.addRow("Other names (optional)", self.aliases)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Recordings (required)"))
        picker_row.addStretch()
        picker_row.addWidget(self.choose_button)
        self.validation_error = QLabel()
        self.validation_error.setWordWrap(True)
        self.validation_error.setAccessibleName("Voice input problem")
        self.validation_error.hide()
        import_help = QLabel(
            "Adds recordings to your custom library immediately. Cancel in the "
            "library window won't undo the import."
        )
        import_help.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.add_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.add_button.setText("Add voice")
        set_action_icon(self.add_button, "check")
        set_action_icon(buttons.button(QDialogButtonBox.StandardButton.Cancel), "x")
        self.add_button.setEnabled(False)
        self.character.textChanged.connect(self._update_add_button)
        self.aliases.textChanged.connect(self._update_add_button)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addLayout(form)
        layout.addLayout(picker_row)
        layout.addWidget(self.reference_files)
        layout.addWidget(self.validation_error)
        layout.addWidget(import_help)
        layout.addWidget(buttons)
        self.setTabOrder(self.aliases, self.choose_button)
        self.setTabOrder(self.choose_button, self.reference_files)
        self.setTabOrder(self.reference_files, self.add_button)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.reference_files and event.type() == QEvent.Type.FontChange:
            self.reference_files.setFixedHeight(
                self.reference_files.fontMetrics().lineSpacing() * 3 + 12
            )
        return super().eventFilter(watched, event)

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
            "\n".join(files) if files else "No files selected"
        )
        self._update_add_button()

    def _input_problem(self) -> tuple[str, QWidget] | None:
        name, references, aliases = self.values()
        if not normalize_character_name(name):
            return (
                "Enter a character name containing a letter or number.",
                self.character,
            )
        if any(not normalize_character_name(alias) for alias in aliases):
            return "Each other name must contain a letter or number.", self.aliases
        if not references:
            return "Choose at least one recording.", self.choose_button
        return None

    def _update_add_button(self) -> None:
        problem = self._input_problem()
        self.add_button.setEnabled(problem is None)
        show_problem = problem is not None and (
            (problem[1] is self.character and bool(self.character.text().strip()))
            or problem[1] is self.aliases
        )
        self.validation_error.setText(problem[0] if show_problem and problem else "")
        self.validation_error.setVisible(show_problem)

    def validate_and_accept(self) -> None:
        problem = self._input_problem()
        if problem is not None:
            self.validation_error.setText(problem[0])
            self.validation_error.show()
            problem[1].setFocus()
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
        self.operation_kind: AssetOperationKind | None = None
        self._operation_model_name: str | None = None
        self._close_pending = False
        self._voice_import_message: str | None = None
        self._voice_import_from_audio = False
        self._voice_import_draft: tuple[str, list[str], list[str]] | None = None
        self._validated_manifest_identity: ManifestIdentity | None = None
        self._accept_after_manifest_validation = False
        self.model_management_available = settings.speech_backend == "coqui-xtts"
        self.setWindowTitle(
            "Models and voice libraries"
            if self.model_management_available
            else "Voice libraries"
        )
        self.setMinimumSize(680, 300)

        self.tabs = QTabWidget()
        self.models_tab = self._create_models_tab()
        if self.model_management_available:
            self.tabs.addTab(self.models_tab, "Speech model")
        else:
            self.models_tab.setParent(self)
            self.models_tab.hide()
        self.tabs.addTab(self._create_voices_tab(), "Voice libraries")
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
        set_action_icon(
            self.buttons.button(QDialogButtonBox.StandardButton.Save), "check"
        )
        set_action_icon(
            self.buttons.button(QDialogButtonBox.StandardButton.Cancel), "x"
        )
        self.buttons.accepted.connect(self.accept_settings)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(self.tabs)
        layout.addWidget(self.buttons)
        make_text_copyable(self)

        self.signals.progress.connect(self.update_progress)

    def _status_field(self, name: str, text: str) -> QPlainTextEdit:
        status = QPlainTextEdit(text, readOnly=True, tabChangesFocus=True)
        status.setAccessibleName(name)
        status.setFrameShape(QFrame.Shape.NoFrame)
        status.viewport().setAutoFillBackground(False)
        status.setFixedHeight(status.fontMetrics().lineSpacing() * 3 + 12)
        status.installEventFilter(self)
        return status

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(watched, QPlainTextEdit)
            and event.type() == QEvent.Type.FontChange
        ):
            watched.setFixedHeight(watched.fontMetrics().lineSpacing() * 3 + 12)
        return super().eventFilter(watched, event)

    @staticmethod
    def _scrollable_tab(body: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        return scroll

    def _create_models_tab(self) -> QWidget:
        tab = QWidget()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItem(default_model)
        if self.settings_value.tts_model:
            self.model.setCurrentText(self.settings_value.tts_model)
        self.model_path = QLabel(self._model_path_text())
        self.model_path.setWordWrap(True)
        self.model_path.setTextFormat(Qt.TextFormat.PlainText)
        self.model.currentTextChanged.connect(self._model_changed)
        self.model_status = self._status_field(
            "Speech model status",
            "Installation status not checked. Verify checksums to confirm this model.",
        )
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        self.download_button = QPushButton("Download model")
        set_action_icon(self.download_button, "download")
        self.cancel_button = QPushButton("Cancel download")
        set_action_icon(self.cancel_button, "x")
        self.cancel_button.hide()
        self.verify_button = QPushButton("Verify checksums")
        set_action_icon(self.verify_button, "circle-check")
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
        layout.addWidget(QLabel("Selected model"))
        layout.addWidget(self.model)
        layout.addLayout(actions)
        layout.addWidget(self.model_status)
        layout.addWidget(self.progress)
        layout.addWidget(QLabel("Model files"))
        layout.addWidget(self.model_path)
        layout.addStretch()
        return self._scrollable_tab(tab)

    def _model_path_text(self) -> str:
        name = self.model_name()
        if not name:
            return "Choose a model to see its download directory."
        try:
            return str(self.model_manager.model_path(name))
        except ModelIntegrityError as error:
            return f"Invalid model: {error}"

    def _model_changed(self, _text: str) -> None:
        self.model_path.setText(self._model_path_text())
        if not self.operation_running:
            self.model_status.setPlainText(
                "Installation status not checked. Verify checksums to confirm this model."
            )
            self.download_button.setText("Download model")

    def _create_voices_tab(self) -> QWidget:
        tab = QWidget()
        self.voice_manifest = QLineEdit(self.settings_value.voice_manifest or "")
        self.voice_manifest.setAccessibleName("Selected voice library")
        self.voice_manifest.setAccessibleDescription(
            "Manifest JSON file mapping character names to voice references; Save selection applies this library"
        )
        self.voice_status = self._status_field(
            "Voice library and import status",
            "Files not checked. Verify files to confirm this library."
            if self.voice_manifest.text().strip()
            else "No voice library selected. Save selection will disable live voice assignments.",
        )
        self.voice_progress = QProgressBar()
        self.voice_progress.setRange(0, 100)
        self.voice_progress.setValue(0)
        self.voice_progress.hide()
        self.import_pack_button = QPushButton("Import library...")
        set_action_icon(self.import_pack_button, "folder-open")
        self.add_voice_button = QPushButton("Add voice from audio...")
        set_action_icon(self.add_voice_button, "audio-lines")
        self.browse_manifest_button = QPushButton("Choose existing...")
        set_action_icon(self.browse_manifest_button, "folder-open")
        self.validate_manifest_button = QPushButton("Verify files")
        set_action_icon(self.validate_manifest_button, "circle-check")
        self.browse_manifest_button.setAccessibleName("Browse for a voice library")
        self.browse_manifest_button.setAccessibleDescription(
            "Choose an existing voice manifest JSON file"
        )
        self.validate_manifest_button.setAccessibleName("Verify selected voice library")
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
        validation = QHBoxLayout()
        validation.addWidget(self.voice_status, 1)
        validation.addWidget(
            self.validate_manifest_button, 0, Qt.AlignmentFlag.AlignTop
        )
        actions = QHBoxLayout()
        actions.addWidget(self.import_pack_button)
        actions.addWidget(self.add_voice_button)
        actions.addStretch()
        layout = QVBoxLayout(tab)
        manifest_label = QLabel("Selected voice library")
        manifest_label.setBuddy(self.voice_manifest)
        layout.addWidget(manifest_label)
        layout.addLayout(manifest_selector)
        layout.addLayout(validation)
        layout.addWidget(self.voice_progress)
        layout.addLayout(actions)
        import_help = QLabel(
            "Imports copy files into the app immediately, even if you cancel. "
            "Save selection applies the selected library."
        )
        import_help.setWordWrap(True)
        layout.addWidget(import_help)
        layout.addStretch()
        self.validate_manifest_button.setEnabled(
            bool(self.voice_manifest.text().strip())
        )
        self.setTabOrder(self.voice_manifest, self.browse_manifest_button)
        self.setTabOrder(self.browse_manifest_button, self.validate_manifest_button)
        self.setTabOrder(self.validate_manifest_button, self.voice_status)
        self.setTabOrder(self.voice_status, self.import_pack_button)
        self.setTabOrder(self.import_pack_button, self.add_voice_button)
        return self._scrollable_tab(tab)

    def _voice_manifest_edited(self) -> None:
        manifest = self.voice_manifest.text().strip()
        self.manifest_runner.cancel()
        self._validated_manifest_identity = None
        self._accept_after_manifest_validation = False
        self._set_manifest_validation_pending(False)
        self.voice_progress.setValue(0)
        self.validate_manifest_button.setEnabled(
            bool(manifest) and not self.operation_running
        )
        if manifest:
            self.voice_status.setPlainText(
                "Library selection changed. Verify files, or Save selection to check and apply it."
            )
        else:
            self.voice_status.setPlainText(
                "No voice library selected. Save selection will disable live voice assignments."
            )

    def browse_voice_manifest(self) -> None:
        current = self.voice_manifest.text().strip()
        start = ""
        if current:
            candidate = Path(current).expanduser()
            start = str(candidate if candidate.is_dir() else candidate.parent)
        source, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose voice library",
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
            self.voice_status.setPlainText(
                "No voice library selected. Save selection will disable live voice assignments."
            )
            return True
        self._set_manifest_validation_pending(True)
        self.voice_status.setPlainText(
            "Checksum-validating the selected manifest and voice files..."
        )
        self.manifest_runner.start(self._validate_manifest_snapshot, manifest)
        return False

    def _validate_manifest_snapshot(self, manifest: str) -> ManifestValidationResult:
        path = Path(manifest).expanduser().resolve()
        before = file_sha256(path, error_type=OSError)
        validated = self.voice_manager.validate(path)
        after = file_sha256(path, error_type=OSError)
        if after != before:
            raise ValueError("Voice manifest changed while validation was running")
        return str(path), after, str(validated)

    def _manifest_validation_finished(
        self, result: ManifestValidationResult | None, error: Exception | None
    ) -> None:
        self._set_manifest_validation_pending(False)
        selected = self.voice_manifest.text().strip()
        if error is not None:
            self._validated_manifest_identity = None
            self._accept_after_manifest_validation = False
            self.voice_status.setPlainText(
                f"Voice library is invalid: {error}. Check its audio paths or "
                "choose another library, then Verify files."
            )
            self.voice_manifest.setFocus()
            self.voice_manifest.selectAll()
            self.voice_progress.setValue(0)
            return
        assert result is not None
        path, digest, _validated = result
        try:
            selected_path = str(Path(selected).expanduser().resolve())
            selected_digest = file_sha256(selected_path, error_type=OSError)
        except OSError:
            self._voice_manifest_edited()
            self.voice_status.setPlainText(
                "Voice library is unavailable. Restore it or choose another "
                "library, then Verify files."
            )
            return
        if selected_path != path or selected_digest != digest:
            if selected_path == path:
                self._voice_manifest_edited()
                self.voice_status.setPlainText(
                    "Voice library changed after validation. Verify files again "
                    "before saving."
                )
            return
        self._validated_manifest_identity = (path, digest)
        self.voice_progress.setValue(100)
        self.voice_status.setPlainText(
            "Voice library passed checksum validation. Save selection to apply it."
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
        self.model_status.setPlainText("Preparing model download...")
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
        self.model_status.setPlainText("Cancelling after the current network chunk...")

    def verify_model(self) -> None:
        if self.operation_running or not self.model_management_available:
            return
        if not self.model_name():
            QMessageBox.warning(self, "No model", "Choose a model to verify.")
            return
        self.set_operation_running(True, "verify")
        self.model_status.setPlainText("Verifying model checksums...")
        self.model_runner.start(self.model_manager.validate, self.model_name())

    def _model_operation_finished(self, path: object, error: Exception | None) -> None:
        if self.operation_kind == "download":
            message = (
                "Model ready. Save selection to use it."
                if error is None
                else str(error)
                if isinstance(error, ModelDownloadCancelled)
                else f"Model download failed: {error}"
            )
        else:
            message = (
                "Model verified and ready. Save selection to use it."
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
        self.model_status.setPlainText(message)

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
        self.model_status.setPlainText(
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

    def set_operation_running(
        self, running: bool, kind: AssetOperationKind | None = None
    ) -> None:
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
        self._start_drafted_voice_import()

    def _start_drafted_voice_import(
        self, expected_manifest_sha256: str | None = None
    ) -> None:
        assert self._voice_import_draft is not None
        character, references, aliases = self._voice_import_draft
        self._start_voice_import(
            self.voice_manager.import_voice,
            character,
            tuple(references),
            aliases=tuple(aliases),
            expected_manifest_sha256=expected_manifest_sha256,
            message=f"Imported {len(references)} recording(s) for {character}",
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
        self.voice_status.setPlainText("Importing voice files...")
        self.voice_runner.start(operation, *arguments, **keyword_arguments)

    def _voice_import_finished(self, manifest: object, error: Exception | None) -> None:
        message = self._voice_import_message
        from_audio = self._voice_import_from_audio
        self._voice_import_message = None
        self._voice_import_from_audio = False
        self.set_operation_running(False)
        self.voice_progress.setRange(0, 100)
        self.voice_progress.hide()
        if isinstance(error, VoiceReplacementRequired) and from_audio:
            self.voice_progress.setValue(0)
            if not self._close_pending:
                prompt = build_voice_replacement_prompt(error.character, self)
                if prompt.exec() == QMessageBox.StandardButton.Yes:
                    self._start_drafted_voice_import(error.manifest_sha256)
                    return
            self.voice_status.setPlainText(
                "Existing voice kept. Choose Add voice from audio to revise and retry."
            )
        elif error is not None:
            self.voice_progress.setValue(0)
            self.voice_status.setPlainText(
                f"Voice import failed: {error}. "
                + (
                    "Choose Add voice from audio to revise and retry."
                    if from_audio
                    else "Choose the source again to retry."
                )
            )
        else:
            self.voice_imported(str(manifest), message or "Voice import complete")
            if from_audio:
                self._voice_import_draft = None
            self.voice_progress.setValue(100)
        if self._close_pending:
            self._close_pending = False
            self.close()

    def voice_imported(self, manifest: str, message: str) -> None:
        self.voice_manifest.setText(manifest)
        self.voice_status.setPlainText(message)
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
                file_sha256(manifest, error_type=OSError),
            )
        except OSError:
            identity = None
        if identity is None or identity != self._validated_manifest_identity:
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
                self.voice_status.setPlainText(
                    "Finishing the voice import before closing. Imported files will be kept."
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
            else:
                self.voice_status.setPlainText(
                    "Finishing the voice import before closing. Imported files will be kept."
                )
            event.ignore()
            return
        super().closeEvent(event)

import sys
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QWizardPage,
)

from vntts.asset_ui import AssetManagerDialog
from vntts.async_ui import LatestTaskRunner
from vntts.calibration import show_calibration_overlay
from vntts.game_pack import GamePackError, apply_game_pack
from vntts.hotkey_ui import HotkeyRecorder
from vntts.hotkeys import (
    HotkeyValidationError,
    macos_hotkey_limitation,
    validate_hotkey_assignments,
)
from vntts.macos_ui import MacOSPermissionsDialog
from vntts.onboarding import OnboardingDiagnostics
from vntts.release_backends import (
    packaged_speech_backend_available,
    speech_backend_options,
)
from vntts.settings import AppSettings
from vntts.speech_backend import default_moss_tts_model
from vntts.voices import find_default_voice_manifest, find_voice_assignment
from vntts.window_capture import WindowCaptureError, WindowCaptureTarget, list_windows

default_onboarding_model = "tts_models/multilingual/multi-dataset/xtts_v2"


def _add_composite_form_row(form, label_text, field, field_layout):
    label = QLabel(label_text)
    label.setBuddy(field)
    form.addRow(label, field_layout)
    return label


class ConfigurationPage(QWizardPage):
    def __init__(self, settings, *, window_loader=list_windows):
        super().__init__()
        self.original_settings = settings
        self.flow = None
        self.window_loader = window_loader
        self.windows_refreshed = False
        self.setTitle("Configure the game and speech engine")
        self.setSubTitle(
            "Recommended setup only needs the running game window. "
            "Technical controls are available under Advanced options."
        )

        self.capture_mode = QComboBox()
        self.capture_mode.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.capture_mode.addItem("Selected game window", "window")
        self.capture_mode.addItem("Calibrated screen region", "screen")
        initial_capture_mode = (
            settings.capture_mode if settings.onboarding_completed else "window"
        )
        self.capture_mode.setCurrentIndex(
            max(0, self.capture_mode.findData(initial_capture_mode))
        )
        self.game_window = QComboBox()
        self.game_window.setEditable(True)
        self.game_window.setAccessibleName("Game window")
        self.game_window.setAccessibleDescription(
            "Window title captured for dialogue recognition"
        )
        if settings.game_window_title:
            self.game_window.addItem(settings.game_window_title)
            self.game_window.setCurrentText(settings.game_window_title)
        self.refresh_button = QPushButton("Refresh...")
        self.refresh_button.setAccessibleName("Refresh game windows")
        self.refresh_button.setAccessibleDescription(
            "Reload the list of capturable game windows"
        )
        self.refresh_button.setMinimumWidth(120)
        self.refresh_button.clicked.connect(self.refresh_windows)
        self.window_layout = QHBoxLayout()
        self.window_layout.setContentsMargins(0, 0, 0, 0)
        self.window_layout.addWidget(self.game_window, 1)
        self.window_layout.addWidget(self.refresh_button)
        self.window_help = QLabel(
            "Start the game in windowed or borderless mode, then select it here."
        )
        self.window_help.setWordWrap(True)
        self.game_pack = QLineEdit(settings.game_pack or "")
        self.game_pack.setAccessibleName("Game pack")
        self.game_pack.setAccessibleDescription(
            "Optional verified pack that configures story, voices and generated audio"
        )
        self.game_pack_button = QPushButton("Browse...")
        self.game_pack_button.setAccessibleName("Browse for game pack")
        self.game_pack_button.setAccessibleDescription(
            "Choose an optional verified VNTTS game-pack JSON file"
        )
        self.game_pack_button.setMinimumWidth(120)
        self.game_pack_button.clicked.connect(self.browse_game_pack)
        self.game_pack_layout = QHBoxLayout()
        self.game_pack_layout.setContentsMargins(0, 0, 0, 0)
        self.game_pack_layout.addWidget(self.game_pack, 1)
        self.game_pack_layout.addWidget(self.game_pack_button)
        self.game_pack_help = QLabel(
            "Optional: one verified game pack configures character voices, "
            "pregenerated audio and story-aware reading."
        )
        self.game_pack_help.setWordWrap(True)

        self.read_hotkey = HotkeyRecorder(settings.read_hotkey)
        self.live_hotkey = HotkeyRecorder(settings.live_hotkey)
        self.macos_hotkey_notice = QLabel(macos_hotkey_limitation)
        self.macos_hotkey_notice.setWordWrap(True)
        self.macos_hotkey_notice.setVisible(sys.platform == "darwin")
        if sys.platform == "darwin":
            self.read_hotkey.setEnabled(False)
            self.live_hotkey.setEnabled(False)
        self.tts_model = QLineEdit(settings.tts_model or default_onboarding_model)
        self.speech_backend = QComboBox()
        for label, backend, available in speech_backend_options(
            settings.speech_backend
        ):
            self.speech_backend.addItem(label, backend)
            if not available:
                item = self.speech_backend.model().item(self.speech_backend.count() - 1)
                item.setEnabled(False)
        self.speech_backend.setCurrentIndex(
            max(0, self.speech_backend.findData(settings.speech_backend))
        )
        self.tts_language = QLineEdit(settings.tts_language or "en")
        self.narrator_reference = QLineEdit(settings.tts_speaker_wav or "")
        self.narrator_reference.setAccessibleName("Narrator reference")
        self.narrator_reference.setAccessibleDescription(
            "Audio reference used for the narrator voice"
        )
        self.narrator_reference_button = QPushButton("Browse...")
        self.narrator_reference_button.setAccessibleName(
            "Browse for narrator reference"
        )
        self.narrator_reference_button.setAccessibleDescription(
            "Choose a narrator audio reference file"
        )
        self.narrator_reference_button.setMinimumWidth(120)
        self.narrator_reference_button.clicked.connect(self.browse_narrator_reference)
        self.narrator_reference_layout = QHBoxLayout()
        self.narrator_reference_layout.setContentsMargins(0, 0, 0, 0)
        self.narrator_reference_layout.addWidget(self.narrator_reference, 1)
        self.narrator_reference_layout.addWidget(self.narrator_reference_button)
        self.ocr_language = QLineEdit(settings.ocr_language)
        default_voice_manifest = find_default_voice_manifest()
        self.voice_manifest = QLineEdit(
            settings.voice_manifest
            or (str(default_voice_manifest) if default_voice_manifest else "")
        )
        self.voice_manifest.setAccessibleName("Voice manifest")
        self.voice_manifest.setAccessibleDescription(
            "Character voice manifest JSON file"
        )
        self.browse_manifest_button = QPushButton("Browse...")
        self.browse_manifest_button.setAccessibleName("Browse for voice manifest")
        self.browse_manifest_button.setAccessibleDescription(
            "Choose a character voice manifest JSON file"
        )
        self.browse_manifest_button.setMinimumWidth(120)
        self.browse_manifest_button.clicked.connect(self.browse_voice_manifest)
        self.manifest_layout = QHBoxLayout()
        self.manifest_layout.setContentsMargins(0, 0, 0, 0)
        self.manifest_layout.addWidget(self.voice_manifest, 1)
        self.manifest_layout.addWidget(self.browse_manifest_button)
        self.narrator_speaker = QLineEdit(
            settings.narrator_speaker or "Claribel Dervla"
        )
        self.terms = QCheckBox("I agree to the non-commercial CPML terms used by XTTS")
        self.terms.setChecked(settings.xtts_terms_accepted)
        self.license_label = QLabel(
            '<a href="https://coqui.ai/cpml">Read the Coqui Public Model License</a>'
        )
        self.license_label.setOpenExternalLinks(True)
        self.pocket_gated_model = QCheckBox(
            "Enable authenticated Pocket voice cloning after accepting upstream terms"
        )
        self.pocket_gated_model.setChecked(settings.pocket_gated_model_accepted)
        self.pocket_gated_model.setAccessibleDescription(
            "Unchecked uses the public preset-only Pocket model. Checked permits "
            "the isolated worker to use explicitly configured Hugging Face credentials."
        )
        self.pocket_terms_label = QLabel(
            "Public presets need no account. Voice cloning requires access to the "
            '<a href="https://huggingface.co/kyutai/pocket-tts">Pocket model terms</a>.'
        )
        self.pocket_terms_label.setWordWrap(True)
        self.pocket_terms_label.setOpenExternalLinks(True)
        self.manage_assets_button = QPushButton("Download model or import voices...")
        self.manage_assets_button.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.manage_assets_button.clicked.connect(self.manage_assets)
        self.macos_permissions_button = QPushButton("Check macOS permissions...")
        self.macos_permissions_button.setVisible(sys.platform == "darwin")
        self.macos_permissions_button.clicked.connect(self.open_macos_permissions)
        self.validation_summary = QLabel()
        self.validation_summary.setWordWrap(True)
        self.validation_summary.setAccessibleName(
            "Onboarding configuration validation summary"
        )
        self.validation_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        recommended_form = QFormLayout()
        recommended_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        recommended_form.addRow("Capture source", self.capture_mode)
        _add_composite_form_row(
            recommended_form, "Game window", self.game_window, self.window_layout
        )
        recommended_form.addRow("", self.window_help)
        _add_composite_form_row(
            recommended_form,
            "Game pack (optional)",
            self.game_pack,
            self.game_pack_layout,
        )
        recommended_form.addRow("", self.game_pack_help)

        advanced_form = QFormLayout()
        advanced_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        advanced_form.addRow("Read once hotkey", self.read_hotkey)
        advanced_form.addRow("Live reading hotkey", self.live_hotkey)
        if sys.platform == "darwin":
            advanced_form.addRow("macOS controls", self.macos_hotkey_notice)
        advanced_form.addRow("Speech engine", self.speech_backend)
        advanced_form.addRow("TTS model", self.tts_model)
        advanced_form.addRow("OCR language", self.ocr_language)
        advanced_form.addRow("TTS language", self.tts_language)
        _add_composite_form_row(
            advanced_form,
            "Narrator reference",
            self.narrator_reference,
            self.narrator_reference_layout,
        )
        _add_composite_form_row(
            advanced_form,
            "Voice manifest",
            self.voice_manifest,
            self.manifest_layout,
        )
        advanced_form.addRow("Narrator speaker", self.narrator_speaker)
        advanced_form.addRow("", self.terms)
        advanced_form.addRow("", self.license_label)
        advanced_form.addRow("", self.pocket_gated_model)
        advanced_form.addRow("", self.pocket_terms_label)
        advanced_form.addRow("Assets", self.manage_assets_button)
        if sys.platform == "darwin":
            advanced_form.addRow("Permissions", self.macos_permissions_button)

        self.advanced_content = QWidget()
        self.advanced_content.setLayout(advanced_form)
        self.advanced_toggle = QPushButton("Show advanced options")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setAccessibleDescription(
            "Show or hide speech engine, language, voice and keyboard settings"
        )
        self.advanced_toggle.toggled.connect(self._set_advanced_expanded)
        form_content = QWidget()
        form_layout = QVBoxLayout(form_content)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.addLayout(recommended_form)
        form_layout.addWidget(self.advanced_toggle)
        form_layout.addWidget(self.advanced_content)
        form_layout.addStretch()
        self.configuration_scroll = QScrollArea()
        self.configuration_scroll.setWidgetResizable(True)
        self.configuration_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.configuration_scroll.setWidget(form_content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.validation_summary)
        layout.addWidget(self.configuration_scroll, 1)

        self.capture_mode.currentIndexChanged.connect(self.update_capture_controls)
        self.tts_model.textChanged.connect(self.update_terms_control)
        self.speech_backend.currentIndexChanged.connect(self.update_backend_controls)
        self._connect_validation_updates()
        self.update_capture_controls()
        self.update_backend_controls()
        self.update_terms_control()
        self._set_advanced_expanded(settings.speech_backend != "pocket-tts")
        self.update_validation_summary()

    def initializePage(self):
        if self.windows_refreshed:
            return
        self.windows_refreshed = True
        self.refresh_windows(show_error=False)

    def refresh_windows(self, _checked=False, *, show_error=True):
        selected = self.game_window.currentText().strip()
        try:
            windows = self.window_loader()
        except WindowCaptureError as error:
            self.window_help.setText(
                "Windows could not be listed automatically. Enter the exact game "
                "window title or use Refresh after granting capture permission."
            )
            if show_error:
                QMessageBox.warning(self, "Window capture unavailable", str(error))
            return
        titles = tuple(
            dict.fromkeys(window.title for window in windows if window.title)
        )
        self.game_window.blockSignals(True)
        self.game_window.clear()
        self.game_window.addItems(titles)
        if selected:
            self.game_window.setCurrentText(selected)
        self.game_window.blockSignals(False)
        self.window_help.setText(
            f"Found {len(titles)} capturable window(s). Select the running game."
            if titles
            else "No capturable windows found. Start the game in windowed or "
            "borderless mode, then press Refresh."
        )
        self.update_validation_summary()

    def browse_game_pack(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose optional game pack",
            self.game_pack.text(),
            "VNTTS game packs (*.json);;All files (*)",
        )
        if path:
            self.game_pack.setText(path)

    def _set_advanced_expanded(self, expanded):
        expanded = bool(expanded)
        self.advanced_toggle.blockSignals(True)
        self.advanced_toggle.setChecked(expanded)
        self.advanced_toggle.setText(
            "Hide advanced options" if expanded else "Show advanced options"
        )
        self.advanced_toggle.blockSignals(False)
        self.advanced_content.setVisible(expanded)

    def browse_voice_manifest(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose character voice manifest",
            self.voice_manifest.text(),
            "JSON files (*.json);;All files (*)",
        )
        if path:
            self.voice_manifest.setText(path)

    def browse_narrator_reference(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose narrator voice reference",
            self.narrator_reference.text(),
            "Audio files (*.flac *.m4a *.mp3 *.ogg *.wav);;All files (*)",
        )
        if path:
            self.narrator_reference.setText(path)

    def manage_assets(self):
        dialog = AssetManagerDialog(self.settings(), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        settings = dialog.settings()
        self.tts_model.setText(settings.tts_model or "")
        self.voice_manifest.setText(settings.voice_manifest or "")

    def open_macos_permissions(self):
        MacOSPermissionsDialog(self).exec()

    def update_capture_controls(self):
        self.game_window.setEnabled(self.capture_mode.currentData() == "window")

    def update_terms_control(self):
        backend = self.speech_backend.currentData()
        uses_xtts = backend == "coqui-xtts"
        uses_pocket = backend == "pocket-tts"
        self.terms.setEnabled(uses_xtts)
        self.terms.setVisible(uses_xtts)
        self.license_label.setVisible(uses_xtts)
        self.pocket_gated_model.setEnabled(uses_pocket)
        self.pocket_gated_model.setVisible(uses_pocket)
        self.pocket_terms_label.setVisible(uses_pocket)

    def update_backend_controls(self):
        backend = self.speech_backend.currentData()
        uses_xtts = backend == "coqui-xtts"
        uses_moss = backend == "moss-tts"
        if uses_moss and self.tts_model.text().strip() in {
            "",
            default_onboarding_model,
        }:
            self.tts_model.setText(default_moss_tts_model)
        elif uses_xtts and self.tts_model.text().strip() in {
            "",
            default_moss_tts_model,
        }:
            self.tts_model.setText(default_onboarding_model)
        self.tts_model.setEnabled(uses_xtts or uses_moss)
        self.tts_language.setEnabled(uses_xtts or uses_moss)
        self.narrator_speaker.setEnabled(uses_xtts)
        self.update_terms_control()

    def _connect_validation_updates(self):
        for recorder in (self.read_hotkey, self.live_hotkey):
            recorder.keySequenceChanged.connect(self.update_validation_summary)
        for field in (
            self.tts_model,
            self.ocr_language,
            self.tts_language,
            self.narrator_reference,
            self.voice_manifest,
            self.narrator_speaker,
        ):
            field.textChanged.connect(self.update_validation_summary)
        self.capture_mode.currentIndexChanged.connect(self.update_validation_summary)
        self.game_window.currentTextChanged.connect(self.update_validation_summary)
        self.game_pack.textChanged.connect(self.update_validation_summary)
        self.speech_backend.currentIndexChanged.connect(self.update_validation_summary)
        self.terms.toggled.connect(self.update_validation_summary)

    def validation_errors(self):
        errors = []

        def add(widget, message):
            if message:
                errors.append((widget, message))

        try:
            validate_hotkey_assignments(self.hotkey_assignments())
        except HotkeyValidationError as error:
            add(self.read_hotkey, f"Keyboard shortcuts: {error}.")
        if (
            self.capture_mode.currentData() == "window"
            and not self.game_window.currentText().strip()
        ):
            add(
                self.game_window,
                "Game window: start the game, refresh the list, and select its window.",
            )
        backend = self.speech_backend.currentData()
        if not packaged_speech_backend_available(backend):
            add(
                self.speech_backend,
                f"Speech engine: {backend} is not included in this application "
                "package. Choose Pocket TTS or XTTS.",
            )
        if backend in {"coqui-xtts", "moss-tts"} and not self.tts_model.text().strip():
            add(self.tts_model, "Speech model: choose a model.")
        if (
            backend in {"coqui-xtts", "moss-tts"}
            and not self.tts_language.text().strip()
        ):
            add(self.tts_language, "TTS language: enter a language code.")
        if not self.ocr_language.text().strip():
            add(self.ocr_language, "OCR language: enter a Tesseract language code.")
        if backend == "coqui-xtts" and not self.terms.isChecked():
            add(self.terms, "XTTS license: accept the CPML terms.")
        narrator_reference = self.narrator_reference.text().strip()
        if narrator_reference and not Path(narrator_reference).expanduser().is_file():
            add(
                self.narrator_reference,
                "Narrator reference: the selected file does not exist.",
            )
        if (
            backend == "moss-tts"
            and not narrator_reference
            and find_voice_assignment(
                self.original_settings.voice_assignments,
                "Narrator",
            )
            is None
        ):
            add(
                self.narrator_reference,
                "Narrator reference: choose a recording or assign an imported "
                "character voice to Narrator before using MOSS-TTS.",
            )
        game_pack = self.game_pack.text().strip()
        manifest = self.voice_manifest.text().strip()
        if not game_pack and manifest and not Path(manifest).expanduser().is_file():
            add(
                self.voice_manifest,
                "Voice manifest: the selected file does not exist.",
            )
        if game_pack:
            try:
                apply_game_pack(self._base_settings(), game_pack)
            except (GamePackError, OSError) as error:
                add(self.game_pack, f"Game pack: {error}.")
        return tuple(errors)

    def update_validation_summary(self, *_args):
        errors = self.validation_errors()
        if errors:
            self.validation_summary.setText(
                f"Fix {len(errors)} setting(s) before continuing:\n"
                + "\n".join(f"- {message}" for _widget, message in errors)
            )
            self.validation_summary.setStyleSheet("color: #b3261e; font-weight: 600;")
        else:
            self.validation_summary.setText("Configuration is ready.")
            self.validation_summary.setStyleSheet("")
        return errors

    def validatePage(self):
        errors = self.update_validation_summary()
        if errors:
            widget, _message = errors[0]
            if self.advanced_content.isAncestorOf(widget):
                self._set_advanced_expanded(True)
            self.configuration_scroll.ensureWidgetVisible(widget, 0, 16)
            widget.setFocus(Qt.FocusReason.OtherFocusReason)
            return False

        self.flow.draft_settings = self.settings()
        return True

    def _base_settings(self):
        def optional_text(widget):
            return widget.text().strip() or None

        hotkeys = self.hotkey_assignments()
        return AppSettings.from_mapping(
            {
                **asdict(self.original_settings),
                "capture_mode": self.capture_mode.currentData(),
                "game_window_title": self.game_window.currentText().strip() or None,
                "game_pack": optional_text(self.game_pack),
                "read_hotkey": hotkeys["Read once"],
                "live_hotkey": hotkeys["Live reading"],
                "speech_backend": self.speech_backend.currentData(),
                "tts_model": optional_text(self.tts_model),
                "ocr_language": self.ocr_language.text().strip(),
                "tts_language": optional_text(self.tts_language),
                "tts_speaker_wav": optional_text(self.narrator_reference),
                "voice_manifest": optional_text(self.voice_manifest),
                "narrator_speaker": optional_text(self.narrator_speaker),
                "xtts_terms_accepted": self.terms.isChecked(),
                "pocket_gated_model_accepted": (self.pocket_gated_model.isChecked()),
            }
        )

    def settings(self):
        settings = self._base_settings()
        return apply_game_pack(settings) if settings.game_pack else settings

    def hotkey_assignments(self):
        return {
            "Read once": self.read_hotkey.hotkey(),
            "Live reading": self.live_hotkey.hotkey(),
        }


class DiagnosticsPage(QWizardPage):
    def __init__(self, diagnostics):
        super().__init__()
        self.diagnostics = diagnostics
        self.flow = None
        self.complete = False
        self.diagnostic_results = ()
        self.runner = LatestTaskRunner(self)
        self.runner.finished.connect(self._checks_finished)
        self.setTitle("Check required components")
        self.setSubTitle("Errors must be fixed before setup can continue.")
        self.status = QLabel("Checks have not run.")
        self.status.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.results = QListWidget()
        self.results.setAccessibleName("Setup diagnostic results")
        self.results.itemSelectionChanged.connect(self._update_remediation)
        self.remediation_reason = QLabel()
        self.remediation_reason.setWordWrap(True)
        self.remediation_reason.setAccessibleName("Selected setup issue guidance")
        self.remediation_button = QPushButton("Fix selected issue")
        self.remediation_button.setEnabled(False)
        self.remediation_button.setAccessibleDescription(
            "Open the most direct available action for the selected setup issue"
        )
        self.remediation_button.clicked.connect(self._run_remediation)
        remediation = QHBoxLayout()
        remediation.addWidget(self.remediation_reason, 1)
        remediation.addWidget(self.remediation_button)
        actions = QHBoxLayout()
        self.retry_button = QPushButton("Run checks again")
        self.retry_button.clicked.connect(self.start_checks)
        self.cancel_button = QPushButton("Cancel checks")
        self.cancel_button.clicked.connect(self.cancel_checks)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.retry_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.progress)
        layout.addWidget(self.results)
        layout.addLayout(remediation)
        layout.addLayout(actions)

    def initializePage(self):
        self.start_checks()

    def start_checks(self):
        self.runner.cancel()
        self.results.clear()
        self.diagnostic_results = ()
        self.complete = False
        self.status.setText("Checking OCR, audio, permissions, and speech assets...")
        self.progress.show()
        self.retry_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._update_remediation()
        self.completeChanged.emit()
        self.runner.start(self.diagnostics.run, self.flow.draft_settings)

    def cancel_checks(self):
        if not self.runner.cancel():
            return
        self.complete = False
        self.diagnostic_results = ()
        self.progress.hide()
        self.retry_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.status.setText("Checks cancelled. Run them again to continue.")
        self._update_remediation()
        self.completeChanged.emit()

    def cleanupPage(self):
        self.cancel_checks()

    def _checks_finished(self, diagnostics, error):
        self.progress.hide()
        self.retry_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if error is not None:
            self.complete = False
            self.diagnostic_results = ()
            self.results.clear()
            self.results.addItem(f"[ERROR] Diagnostics failed: {error}")
            self.status.setText("Checks failed. Fix the error or run them again.")
            self._update_remediation()
            self.completeChanged.emit()
            return
        self.diagnostic_results = tuple(diagnostics)
        self.complete = all(result.passed for result in self.diagnostic_results)
        for result in self.diagnostic_results:
            prefix = {
                "ok": "[OK]",
                "warning": "[WARNING]",
                "error": "[ERROR]",
            }[result.status]
            self.results.addItem(f"{prefix} {result.name}: {result.message}")
        self.status.setText(
            "Checks complete."
            if self.complete
            else "Checks complete with errors that must be fixed."
        )
        preferred_row = next(
            (
                row
                for row, result in enumerate(self.diagnostic_results)
                if result.status == "error"
            ),
            next(
                (
                    row
                    for row, result in enumerate(self.diagnostic_results)
                    if result.status == "warning" and result.remediation
                ),
                0 if self.diagnostic_results else None,
            ),
        )
        if preferred_row is not None:
            self.results.setCurrentRow(preferred_row)
        self._update_remediation()
        self.completeChanged.emit()

    def _selected_result(self):
        row = self.results.currentRow()
        if row < 0 or row >= len(self.diagnostic_results):
            return None
        return self.diagnostic_results[row]

    def _update_remediation(self):
        result = self._selected_result()
        if result is None or result.status == "ok":
            self.remediation_reason.setText(
                "Select a warning or error to see the next action."
            )
            self.remediation_button.setEnabled(False)
            self.remediation_button.setText("Fix selected issue")
            return
        self.remediation_reason.setText(result.message)
        labels = {
            "settings": "Open setup options",
            "voices": "Manage models and voices",
            "permissions": "Open macOS permissions",
        }
        self.remediation_button.setText(
            labels.get(result.remediation, "Show installation help")
        )
        self.remediation_button.setEnabled(True)

    def _run_remediation(self):
        result = self._selected_result()
        if result is None or result.status == "ok":
            return
        if result.remediation == "permissions":
            self.flow.configuration_page.open_macos_permissions()
            return
        if result.remediation in {"settings", "voices"}:
            self.flow.show_page(1)
            self.flow.configuration_page._set_advanced_expanded(True)
            if result.remediation == "voices":
                QTimer.singleShot(0, self.flow.configuration_page.manage_assets)
            return
        self.remediation_reason.setText(
            f"{result.message} Install the missing component using the "
            "Requirements section in the VNTTS README, then run checks again."
        )
        self.remediation_button.setEnabled(False)

    def isComplete(self):
        return self.complete


class CalibrationPage(QWizardPage):
    def __init__(self, capture_target_factory):
        super().__init__()
        self.capture_target_factory = capture_target_factory
        self.flow = None
        self.calibrated = False
        self.overlay = None
        self.setTitle("Calibrate the dialogue area")
        self.instructions = QLabel(
            "Open a scene with dialogue, click Calibrate, then drag over the "
            "speaker name and dialogue text. Keyboard: press Enter for a "
            "suggested region, use arrows to move it, Shift plus arrows to "
            "resize it, and Enter again to review. Press Escape to cancel."
        )
        self.instructions.setWordWrap(True)
        self.button = QPushButton("Calibrate...")
        self.button.clicked.connect(self.calibrate)
        self.status = QLabel("Calibration has not been completed.")
        layout = QVBoxLayout(self)
        layout.addWidget(self.instructions)
        layout.addWidget(self.button)
        layout.addWidget(self.status)
        layout.addStretch()

    def initializePage(self):
        self.calibrated = False
        self.status.setText("Calibration has not been completed.")
        self.completeChanged.emit()

    def calibrate(self):
        settings = self.flow.draft_settings
        geometry = None
        if settings.capture_mode == "window":
            try:
                geometry = self.capture_target_factory(
                    settings.game_window_title
                ).get_geometry()
            except WindowCaptureError as error:
                QMessageBox.warning(self, "Unable to calibrate", str(error))
                return
        self.pending_geometry = geometry
        self.flow.hide()
        QTimer.singleShot(200, self.open_overlay)

    def open_overlay(self):
        self.overlay = show_calibration_overlay(self.pending_geometry)
        self.overlay.selected.connect(self.finish_calibration)
        self.overlay.closed.connect(self.restore_wizard)

    def finish_calibration(self, _region):
        self.calibrated = True
        self.status.setText("Dialogue area saved.")
        self.completeChanged.emit()

    def restore_wizard(self):
        self.flow.show()
        self.flow.raise_()
        self.flow.activateWindow()

    def isComplete(self):
        return self.calibrated


class EndToEndTestPage(QWizardPage):
    test_requested = Signal(object)
    cancel_requested = Signal()

    def __init__(self):
        super().__init__()
        self.flow = None
        self.successful = False
        self.running = False
        self.setTitle("Test OCR and speech")
        instructions = QLabel(
            "Keep dialogue visible in the calibrated area. The first test can "
            "download and load the speech model, then reads the detected line aloud."
        )
        instructions.setWordWrap(True)
        self.button = QPushButton("Run OCR-to-speech test")
        self.button.clicked.connect(self.run_test)
        self.cancel_button = QPushButton("Cancel test")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.request_cancel)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status = QLabel("The test has not run.")
        self.status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(instructions)
        layout.addWidget(self.button)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addStretch()

    def initializePage(self):
        self.successful = False
        self.running = False
        self.button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancel test")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText("The test has not run.")
        self.completeChanged.emit()

    def run_test(self):
        self.successful = False
        self.running = True
        self.button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText("Loading the model and testing OCR and audio...")
        self.completeChanged.emit()
        self.test_requested.emit(self.flow.draft_settings)

    def request_cancel(self):
        if not self.running:
            return
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling...")
        self.status.setText("Cancelling the OCR-to-speech test...")
        self.completeChanged.emit()
        self.cancel_requested.emit()

    def set_progress(self, percent, message):
        if percent is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
        self.status.setText(message)

    def set_result(self, successful, message):
        self.successful = successful
        self.running = False
        self.button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancel test")
        self.progress.setRange(0, 100)
        if successful:
            self.progress.setValue(100)
        self.status.setText(
            f"{message}\n\nSetup is ready. Finish setup, then use Start live reading."
            if successful
            else message
        )
        self.button.setText(
            "Run test again" if successful else "Run OCR-to-speech test"
        )
        self.completeChanged.emit()

    def isComplete(self):
        return self.successful


class OnboardingWizard(QDialog):
    test_requested = Signal(object)
    cancel_requested = Signal()

    def __init__(
        self,
        settings,
        *,
        diagnostics=None,
        capture_target_factory=WindowCaptureTarget,
        window_loader=list_windows,
        auto_discover_windows=None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Visual Novel Text to Speech setup")
        self.setMinimumSize(520, 420)
        self.resize(820, 620)
        self.draft_settings = settings
        self.completed_settings = None
        self.pages = []
        self.current_page_index = 0
        self.auto_discover_windows = (
            QApplication.platformName() != "offscreen"
            if auto_discover_windows is None
            else bool(auto_discover_windows)
        )

        welcome = QWizardPage()
        welcome.setTitle("Set up Visual Novel Text to Speech")
        welcome_text = QLabel(
            "This wizard selects the game, verifies OCR and audio, calibrates "
            "the dialogue area, and runs a complete speech test."
        )
        welcome_text.setWordWrap(True)
        welcome_layout = QVBoxLayout(welcome)
        welcome_layout.addWidget(welcome_text)
        welcome_layout.addStretch()

        self.configuration_page = ConfigurationPage(
            settings,
            window_loader=window_loader,
        )
        self.diagnostics_page = DiagnosticsPage(diagnostics or OnboardingDiagnostics())
        self.calibration_page = CalibrationPage(capture_target_factory)
        self.test_page = EndToEndTestPage()
        self.test_page.test_requested.connect(self.test_requested.emit)
        self.test_page.cancel_requested.connect(self.cancel_requested.emit)

        self.stack = QStackedWidget()
        for page in (
            welcome,
            self.configuration_page,
            self.diagnostics_page,
            self.calibration_page,
            self.test_page,
        ):
            page.flow = self
            self.pages.append(page)
            self.stack.addWidget(page)
            page.completeChanged.connect(self.update_navigation)

        self.step_label = QLabel()
        self.step_label.setAccessibleName("Onboarding progress")
        self.step_label.setStyleSheet("font-weight: 600;")
        self.page_title = QLabel()
        self.page_title.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.page_subtitle = QLabel()
        self.page_subtitle.setWordWrap(True)
        self.back_button = QPushButton("Back")
        self.next_button = QPushButton("Next")
        self.finish_button = QPushButton("Finish setup")
        self.finish_button.setDefault(True)
        self.finish_button.setAccessibleDescription(
            "Save setup and return to the dashboard without starting playback"
        )
        self.cancel_button = QPushButton("Cancel")
        self.back_button.clicked.connect(self.previous_page)
        self.next_button.clicked.connect(self.next_page)
        self.finish_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        navigation = QHBoxLayout()
        navigation.addWidget(self.cancel_button)
        navigation.addStretch()
        navigation.addWidget(self.back_button)
        navigation.addWidget(self.next_button)
        navigation.addWidget(self.finish_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.step_label)
        layout.addWidget(self.page_title)
        layout.addWidget(self.page_subtitle)
        layout.addWidget(self.stack, 1)
        layout.addLayout(navigation)
        self.show_page(0)

    def show_page(self, index):
        previous = self.pages[self.current_page_index]
        if previous is not self.pages[max(0, min(index, len(self.pages) - 1))]:
            cleanup = getattr(previous, "cleanupPage", None)
            if callable(cleanup):
                cleanup()
        self.current_page_index = max(0, min(index, len(self.pages) - 1))
        page = self.pages[self.current_page_index]
        self.stack.setCurrentWidget(page)
        self.step_label.setText(
            f"Step {self.current_page_index + 1} of {len(self.pages)}"
        )
        self.page_title.setText(page.title())
        self.page_subtitle.setText(page.subTitle())
        initializer = getattr(page, "initializePage", None)
        if (
            callable(initializer)
            and self.current_page_index
            and (page is not self.configuration_page or self.auto_discover_windows)
        ):
            initializer()
        self.update_navigation()

    def previous_page(self):
        if self.current_page_index:
            self.show_page(self.current_page_index - 1)

    def next_page(self):
        page = self.pages[self.current_page_index]
        validator = getattr(page, "validatePage", None)
        if callable(validator) and validator() is False:
            return
        if self.current_page_index < len(self.pages) - 1:
            self.show_page(self.current_page_index + 1)

    def update_navigation(self):
        final = self.current_page_index == len(self.pages) - 1
        page = self.pages[self.current_page_index]
        complete = getattr(page, "isComplete", lambda: True)()
        test_running = self.test_page.running
        self.back_button.setEnabled(self.current_page_index > 0 and not test_running)
        self.cancel_button.setEnabled(not test_running)
        self.next_button.setVisible(not final)
        self.next_button.setEnabled(bool(complete))
        self.finish_button.setVisible(final)
        self.finish_button.setEnabled(bool(complete))

    def accept(self):
        if not self.test_page.successful:
            return
        self.completed_settings = self.draft_settings.updated(onboarding_completed=True)
        super().accept()

    def reject(self):
        if self.test_page.running:
            self.test_page.request_cancel()
            return
        self.diagnostics_page.runner.cancel()
        super().reject()

    def settings(self):
        return self.completed_settings or self.draft_settings

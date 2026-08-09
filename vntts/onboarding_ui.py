from dataclasses import asdict

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
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
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from vntts.asset_ui import AssetManagerDialog
from vntts.calibration import show_calibration_overlay
from vntts.hotkey_ui import HotkeyRecorder
from vntts.hotkeys import HotkeyValidationError, validate_hotkey_assignments
from vntts.onboarding import OnboardingDiagnostics
from vntts.settings import AppSettings
from vntts.window_capture import WindowCaptureError, WindowCaptureTarget, list_windows

default_onboarding_model = "tts_models/multilingual/multi-dataset/xtts_v2"


class ConfigurationPage(QWizardPage):
    def __init__(self, settings):
        super().__init__()
        self.original_settings = settings
        self.setTitle("Configure the game and speech engine")
        self.setSubTitle("These values can be changed later from the tray menu.")

        self.capture_mode = QComboBox()
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
        if settings.game_window_title:
            self.game_window.addItem(settings.game_window_title)
            self.game_window.setCurrentText(settings.game_window_title)
        refresh_button = QPushButton("Refresh...")
        refresh_button.clicked.connect(self.refresh_windows)
        window_layout = QHBoxLayout()
        window_layout.addWidget(self.game_window)
        window_layout.addWidget(refresh_button)

        self.read_hotkey = HotkeyRecorder(settings.read_hotkey)
        self.live_hotkey = HotkeyRecorder(settings.live_hotkey)
        self.tts_model = QLineEdit(settings.tts_model or default_onboarding_model)
        self.tts_language = QLineEdit(settings.tts_language or "en")
        self.voice_manifest = QLineEdit(settings.voice_manifest or "")
        browse_manifest = QPushButton("Browse...")
        browse_manifest.clicked.connect(self.browse_voice_manifest)
        manifest_layout = QHBoxLayout()
        manifest_layout.addWidget(self.voice_manifest)
        manifest_layout.addWidget(browse_manifest)
        self.narrator_speaker = QLineEdit(
            settings.narrator_speaker or "Claribel Dervla"
        )
        self.terms = QCheckBox("I agree to the non-commercial CPML terms used by XTTS")
        self.terms.setChecked(settings.xtts_terms_accepted)

        license_label = QLabel(
            '<a href="https://coqui.ai/cpml">Read the Coqui Public Model License</a>'
        )
        license_label.setOpenExternalLinks(True)
        manage_assets = QPushButton("Download model or import voices...")
        manage_assets.clicked.connect(self.manage_assets)
        form = QFormLayout()
        form.addRow("Capture source", self.capture_mode)
        form.addRow("Game window", window_layout)
        form.addRow("Read once hotkey", self.read_hotkey)
        form.addRow("Live reading hotkey", self.live_hotkey)
        form.addRow("TTS model", self.tts_model)
        form.addRow("TTS language", self.tts_language)
        form.addRow("Voice manifest", manifest_layout)
        form.addRow("Narrator speaker", self.narrator_speaker)
        form.addRow("", self.terms)
        form.addRow("", license_label)
        form.addRow("Assets", manage_assets)
        self.setLayout(form)

        self.capture_mode.currentIndexChanged.connect(self.update_capture_controls)
        self.tts_model.textChanged.connect(self.update_terms_control)
        self.update_capture_controls()
        self.update_terms_control()

    def refresh_windows(self):
        selected = self.game_window.currentText().strip()
        try:
            windows = list_windows()
        except WindowCaptureError as error:
            QMessageBox.warning(self, "Window capture unavailable", str(error))
            return
        self.game_window.clear()
        self.game_window.addItems(window.title for window in windows)
        if selected:
            self.game_window.setCurrentText(selected)

    def browse_voice_manifest(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose character voice manifest",
            self.voice_manifest.text(),
            "JSON files (*.json);;All files (*)",
        )
        if path:
            self.voice_manifest.setText(path)

    def manage_assets(self):
        dialog = AssetManagerDialog(self.settings(), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        settings = dialog.settings()
        self.tts_model.setText(settings.tts_model or "")
        self.voice_manifest.setText(settings.voice_manifest or "")

    def update_capture_controls(self):
        self.game_window.setEnabled(self.capture_mode.currentData() == "window")

    def update_terms_control(self):
        uses_xtts = "xtts" in self.tts_model.text().casefold()
        self.terms.setEnabled(uses_xtts)

    def validatePage(self):
        try:
            validate_hotkey_assignments(self.hotkey_assignments())
        except HotkeyValidationError as error:
            QMessageBox.warning(self, "Invalid hotkey", str(error))
            return False
        if (
            self.capture_mode.currentData() == "window"
            and not self.game_window.currentText().strip()
        ):
            QMessageBox.warning(
                self,
                "No game window selected",
                "Start the game, refresh the list, and select its window.",
            )
            return False
        if not self.tts_model.text().strip():
            QMessageBox.warning(self, "No speech model", "Configure a TTS model.")
            return False
        if "xtts" in self.tts_model.text().casefold() and not self.terms.isChecked():
            QMessageBox.warning(
                self,
                "Model license not accepted",
                "Accept the CPML terms before using XTTS.",
            )
            return False

        self.wizard().draft_settings = self.settings()
        return True

    def settings(self):
        def optional_text(widget):
            return widget.text().strip() or None

        hotkeys = self.hotkey_assignments()
        return AppSettings.from_mapping(
            {
                **asdict(self.original_settings),
                "capture_mode": self.capture_mode.currentData(),
                "game_window_title": self.game_window.currentText().strip() or None,
                "read_hotkey": hotkeys["Read once"],
                "live_hotkey": hotkeys["Live reading"],
                "tts_model": optional_text(self.tts_model),
                "tts_language": optional_text(self.tts_language),
                "voice_manifest": optional_text(self.voice_manifest),
                "narrator_speaker": optional_text(self.narrator_speaker),
                "xtts_terms_accepted": self.terms.isChecked(),
            }
        )

    def hotkey_assignments(self):
        return {
            "Read once": self.read_hotkey.hotkey(),
            "Live reading": self.live_hotkey.hotkey(),
        }


class DiagnosticsPage(QWizardPage):
    def __init__(self, diagnostics):
        super().__init__()
        self.diagnostics = diagnostics
        self.complete = False
        self.setTitle("Check required components")
        self.setSubTitle("Errors must be fixed before setup can continue.")
        self.results = QListWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self.results)

    def initializePage(self):
        self.results.clear()
        diagnostics = self.diagnostics.run(self.wizard().draft_settings)
        self.complete = all(result.passed for result in diagnostics)
        for result in diagnostics:
            prefix = {
                "ok": "[OK]",
                "warning": "[WARNING]",
                "error": "[ERROR]",
            }[result.status]
            self.results.addItem(f"{prefix} {result.name}: {result.message}")
        self.completeChanged.emit()

    def isComplete(self):
        return self.complete


class CalibrationPage(QWizardPage):
    def __init__(self, capture_target_factory):
        super().__init__()
        self.capture_target_factory = capture_target_factory
        self.calibrated = False
        self.overlay = None
        self.setTitle("Calibrate the dialogue area")
        self.instructions = QLabel(
            "Open a scene with dialogue, click Calibrate, then drag over the "
            "speaker name and dialogue text. Press Escape to cancel selection."
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
        settings = self.wizard().draft_settings
        geometry = None
        if settings.capture_mode == "window":
            try:
                geometry = self.capture_target_factory(
                    settings.game_window_title
                ).get_geometry()
            except WindowCaptureError as error:
                QMessageBox.warning(self, "Unable to calibrate", str(error))
                return
        self.wizard().hide()
        self.overlay = show_calibration_overlay(geometry)
        self.overlay.selected.connect(self.finish_calibration)
        self.overlay.closed.connect(self.restore_wizard)

    def finish_calibration(self, _region):
        self.calibrated = True
        self.status.setText("Dialogue area saved.")
        self.completeChanged.emit()

    def restore_wizard(self):
        self.wizard().show()
        self.wizard().raise_()
        self.wizard().activateWindow()

    def isComplete(self):
        return self.calibrated


class EndToEndTestPage(QWizardPage):
    test_requested = Signal(object)
    cancel_requested = Signal()

    def __init__(self):
        super().__init__()
        self.successful = False
        self.setTitle("Test OCR and speech")
        instructions = QLabel(
            "Keep dialogue visible in the calibrated area. The first test can "
            "download and load the speech model, then reads the detected line aloud."
        )
        instructions.setWordWrap(True)
        self.button = QPushButton("Run OCR-to-speech test")
        self.button.clicked.connect(self.run_test)
        self.cancel_button = QPushButton("Cancel model download")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
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
        self.button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText("The test has not run.")
        self.completeChanged.emit()

    def run_test(self):
        self.successful = False
        self.button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText("Loading the model and testing OCR and audio...")
        self.completeChanged.emit()
        self.test_requested.emit(self.wizard().draft_settings)

    def set_progress(self, percent, message):
        if percent is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
        self.status.setText(message)

    def set_result(self, successful, message):
        self.successful = successful
        self.button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 100)
        if successful:
            self.progress.setValue(100)
        self.status.setText(message)
        self.completeChanged.emit()

    def isComplete(self):
        return self.successful


class OnboardingWizard(QWizard):
    test_requested = Signal(object)
    cancel_requested = Signal()

    def __init__(
        self,
        settings,
        *,
        diagnostics=None,
        capture_target_factory=WindowCaptureTarget,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Visual Novel Text to Speech setup")
        self.setMinimumSize(700, 520)
        self.draft_settings = settings
        self.completed_settings = None

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

        self.configuration_page = ConfigurationPage(settings)
        self.diagnostics_page = DiagnosticsPage(diagnostics or OnboardingDiagnostics())
        self.calibration_page = CalibrationPage(capture_target_factory)
        self.test_page = EndToEndTestPage()
        self.test_page.test_requested.connect(self.test_requested.emit)
        self.test_page.cancel_requested.connect(self.cancel_requested.emit)

        self.addPage(welcome)
        self.addPage(self.configuration_page)
        self.addPage(self.diagnostics_page)
        self.addPage(self.calibration_page)
        self.addPage(self.test_page)

    def accept(self):
        if not self.test_page.successful:
            return
        self.completed_settings = self.draft_settings.updated(onboarding_completed=True)
        super().accept()

    def settings(self):
        return self.completed_settings or self.draft_settings

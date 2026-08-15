import argparse
import sys
from dataclasses import asdict
from multiprocessing import freeze_support
from threading import Event, Thread

from pynput import keyboard
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from vntts.asset_ui import AssetManagerDialog
from vntts.assets import ModelDownloadCancelled
from vntts.calibration import show_calibration_overlay
from vntts.controller import AppController
from vntts.dashboard_ui import CompactController, ControlDashboard
from vntts.diagnostics import diagnostic_error_guidance, macos_permission_warnings
from vntts.diagnostics_ui import DiagnosticsDialog
from vntts.dialog_capture import format_runtime_error
from vntts.history_ui import DialogueHistoryDialog
from vntts.hotkey_ui import HotkeyRecorder
from vntts.hotkeys import (
    HotkeyValidationError,
    macos_hotkey_limitation,
    validate_hotkey_assignments,
)
from vntts.macos import (
    configure_macos_launch_at_login,
    get_macos_permission_status,
)
from vntts.macos_ui import MacOSPermissionsDialog
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.ocr_corrections_ui import OCRCorrectionsDialog
from vntts.ocr_review_ui import OCRReviewDialog
from vntts.onboarding import OnboardingDiagnostics
from vntts.onboarding_ui import OnboardingWizard
from vntts.package_self_test import run_package_self_test
from vntts.profiles import GameProfileStore
from vntts.profiles_ui import GameProfilesDialog
from vntts.readiness_ui import ReadinessDialog
from vntts.release_smoke_test import (
    default_smoke_test_model,
    run_release_smoke_test,
)
from vntts.runtime_config import (
    get_clear_queue_hotkey,
    get_emergency_stop_hotkey,
    get_hotkey,
    get_live_hotkey,
    get_pause_hotkey,
    get_repeat_hotkey,
    get_skip_hotkey,
)
from vntts.runtime_paths import configure_bundled_dependencies
from vntts.settings import (
    AppSettings,
    get_local_data_directory,
    get_settings_path,
    load_app_settings,
)
from vntts.speech_backend import default_moss_tts_model
from vntts.support import RuntimeSupportLog, SupportBundleBuilder
from vntts.support_ui import SupportCenterDialog
from vntts.voice_preview_ui import VoicePreviewDialog
from vntts.voices import find_default_voice_manifest, find_voice_assignment
from vntts.window_capture import (
    WindowCaptureError,
    enable_windows_dpi_awareness,
    list_windows,
)

application_name = "Visual Novel Text to Speech"
default_xtts_model = "tts_models/multilingual/multi-dataset/xtts_v2"


def create_application_icon(style, *, platform=None):
    platform = sys.platform if platform is None else platform
    if platform != "darwin":
        return style.standardIcon(QStyle.StandardPixmap.SP_MediaVolume)

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.black)
    painter.drawRoundedRect(QRectF(4, 5, 56, 45), 12, 12)
    tail = QPainterPath()
    tail.moveTo(QPointF(17, 46))
    tail.lineTo(QPointF(11, 60))
    tail.lineTo(QPointF(31, 48))
    tail.closeSubpath()
    painter.drawPath(tail)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(29)
    painter.setFont(font)
    painter.drawText(
        QRectF(4, 5, 56, 45),
        Qt.AlignmentFlag.AlignCenter,
        "V",
    )
    painter.end()
    icon = QIcon(pixmap)
    icon.setIsMask(True)
    return icon


class AppSignals(QObject):
    status_changed = Signal(str)
    dialog_changed = Signal(str, str)
    ready_changed = Signal(bool)
    live_changed = Signal(bool)
    speech_paused_changed = Signal(bool)
    error_reported = Signal(str)
    onboarding_test_finished = Signal(bool, str)
    onboarding_test_progress = Signal(object, str)
    diagnostics_changed = Signal(object)
    diagnostics_failed = Signal(str)
    hotkeys_requested = Signal()
    support_export_finished = Signal(bool, str)
    unknown_speaker = Signal(str)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.original_settings = settings
        self.setWindowTitle(f"{application_name} settings")

        self.read_hotkey = HotkeyRecorder(settings.read_hotkey)
        self.live_hotkey = HotkeyRecorder(settings.live_hotkey)
        self.pause_hotkey = HotkeyRecorder(settings.pause_hotkey)
        self.skip_hotkey = HotkeyRecorder(settings.skip_hotkey)
        self.repeat_hotkey = HotkeyRecorder(settings.repeat_hotkey)
        self.clear_queue_hotkey = HotkeyRecorder(settings.clear_queue_hotkey)
        self.emergency_stop_hotkey = HotkeyRecorder(settings.emergency_stop_hotkey)
        self.hotkey_recorders = (
            self.read_hotkey,
            self.live_hotkey,
            self.pause_hotkey,
            self.skip_hotkey,
            self.repeat_hotkey,
            self.clear_queue_hotkey,
            self.emergency_stop_hotkey,
        )
        self.macos_hotkey_notice = QLabel(
            f"<b>macOS controls</b><br>{macos_hotkey_limitation}"
        )
        self.macos_hotkey_notice.setWordWrap(True)
        self.macos_hotkey_notice.setAccessibleName("macOS controls")
        self.macos_hotkey_notice.setVisible(sys.platform == "darwin")
        if sys.platform == "darwin":
            for recorder in self.hotkey_recorders:
                recorder.setEnabled(False)
        self.screenshot_directory = QLineEdit(settings.screenshot_directory)
        self.retain_uncertain_frames = QCheckBox(
            "Save uncertain frames for OCR diagnostics"
        )
        self.retain_uncertain_frames.setChecked(settings.retain_uncertain_frames)
        self.ocr_diagnostics_directory = QLineEdit(settings.ocr_diagnostics_directory)
        self.capture_mode = QComboBox()
        self.capture_mode.addItem("Calibrated screen region", "screen")
        self.capture_mode.addItem("Selected game window", "window")
        self.capture_mode.setCurrentIndex(
            max(0, self.capture_mode.findData(settings.capture_mode))
        )
        self.game_window = QComboBox()
        self.game_window.setEditable(True)
        if settings.game_window_title:
            self.game_window.addItem(settings.game_window_title)
            self.game_window.setCurrentText(settings.game_window_title)
        refresh_windows_button = QPushButton("Refresh...")
        refresh_windows_button.clicked.connect(self.refresh_windows)
        window_layout = QHBoxLayout()
        window_layout.addWidget(self.game_window)
        window_layout.addWidget(refresh_windows_button)
        self.tts_model = QLineEdit(settings.tts_model or "")
        self.speech_backend = QComboBox()
        self.speech_backend.addItem(
            "Pocket TTS (default streaming)",
            "pocket-tts",
        )
        self.speech_backend.addItem("XTTS (compatible)", "coqui-xtts")
        self.speech_backend.addItem(
            "Chatterbox Nano (faster English CPU)",
            "chatterbox-nano",
        )
        self.speech_backend.addItem(
            "MOSS-TTS v1.5 (high quality, Apple Silicon)",
            "moss-tts",
        )
        self.speech_backend.setCurrentIndex(
            max(0, self.speech_backend.findData(settings.speech_backend))
        )
        self.ocr_minimum_confidence = QSpinBox()
        self.ocr_minimum_confidence.setRange(0, 100)
        self.ocr_minimum_confidence.setSuffix("%")
        self.ocr_minimum_confidence.setValue(settings.ocr_minimum_confidence)
        self.ocr_language = QLineEdit(settings.ocr_language)
        self.tts_language = QLineEdit(settings.tts_language or "")
        self.narrator_reference = QLineEdit(settings.tts_speaker_wav or "")
        default_voice_manifest = find_default_voice_manifest()
        self.voice_manifest = QLineEdit(
            settings.voice_manifest
            or (str(default_voice_manifest) if default_voice_manifest else "")
        )
        self.story_index = QLineEdit(settings.story_index or "")
        self.generated_audio_manifest = QLineEdit(
            settings.generated_audio_manifest or ""
        )
        self.narrator_speaker = QLineEdit(settings.narrator_speaker or "")
        self.tts_profile = QComboBox()
        self.tts_profile.addItems(["stable", "natural", "expressive"])
        self.tts_profile.setCurrentText(settings.tts_profile)
        self.output_volume = QSpinBox()
        self.output_volume.setRange(0, 100)
        self.output_volume.setSuffix("%")
        self.output_volume.setValue(settings.output_volume_percent)
        self.speech_rate = QSpinBox()
        self.speech_rate.setRange(50, 150)
        self.speech_rate.setSuffix("%")
        self.speech_rate.setValue(settings.speech_rate_percent)
        self.auto_advance = QCheckBox("Advance after the spoken dialogue finishes")
        self.auto_advance.setChecked(settings.auto_advance_enabled)
        self.auto_advance_key = QComboBox()
        self.auto_advance_key.addItem("Space", "space")
        self.auto_advance_key.addItem("Enter", "enter")
        self.auto_advance_key.addItem("Right arrow", "right")
        self.auto_advance_key.addItem("Down arrow", "down")
        self.auto_advance_key.setCurrentIndex(
            max(0, self.auto_advance_key.findData(settings.auto_advance_key))
        )
        self.auto_advance_delay = QSpinBox()
        self.auto_advance_delay.setRange(0, 5000)
        self.auto_advance_delay.setSuffix(" ms")
        self.auto_advance_delay.setValue(settings.auto_advance_delay_ms)
        self.warm_up_voices = QCheckBox("Warm up model and voices before gameplay")
        self.warm_up_voices.setChecked(settings.warm_up_voices)
        self.launch_at_login = QCheckBox("Launch automatically when I sign in")
        self.launch_at_login.setChecked(settings.launch_at_login)
        self.launch_at_login.setEnabled(sys.platform == "darwin")
        self.keep_running_on_close = QCheckBox(
            "Keep reading in the background when the control window closes"
        )
        self.keep_running_on_close.setChecked(settings.keep_running_on_close)
        self.xtts_terms = QCheckBox("I agree to the non-commercial CPML terms")
        self.xtts_terms.setChecked(settings.xtts_terms_accepted)

        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self.browse_screenshot_directory)
        screenshot_layout = QHBoxLayout()
        screenshot_layout.addWidget(self.screenshot_directory)
        screenshot_layout.addWidget(browse_button)
        diagnostics_browse_button = QPushButton("Browse...")
        diagnostics_browse_button.clicked.connect(self.browse_ocr_diagnostics_directory)
        diagnostics_layout = QHBoxLayout()
        diagnostics_layout.addWidget(self.ocr_diagnostics_directory)
        diagnostics_layout.addWidget(diagnostics_browse_button)
        self.diagnostics_browse_button = diagnostics_browse_button
        narrator_reference_button = QPushButton("Browse...")
        narrator_reference_button.clicked.connect(self.browse_narrator_reference)
        narrator_reference_layout = QHBoxLayout()
        narrator_reference_layout.addWidget(self.narrator_reference)
        narrator_reference_layout.addWidget(narrator_reference_button)
        self.narrator_reference_button = narrator_reference_button

        shortcuts_form = QFormLayout()
        shortcuts_form.addRow("Read once hotkey", self.read_hotkey)
        shortcuts_form.addRow("Live reading hotkey", self.live_hotkey)
        shortcuts_form.addRow("Pause or resume hotkey", self.pause_hotkey)
        shortcuts_form.addRow("Skip speech hotkey", self.skip_hotkey)
        shortcuts_form.addRow("Repeat speech hotkey", self.repeat_hotkey)
        shortcuts_form.addRow("Clear queue hotkey", self.clear_queue_hotkey)
        shortcuts_form.addRow("Emergency stop hotkey", self.emergency_stop_hotkey)
        if sys.platform == "darwin":
            shortcuts_form.addRow(self.macos_hotkey_notice)

        capture_form = QFormLayout()
        capture_form.addRow("Screenshot directory", screenshot_layout)
        capture_form.addRow("Capture source", self.capture_mode)
        capture_form.addRow("Game window", window_layout)
        capture_form.addRow("Minimum OCR confidence", self.ocr_minimum_confidence)
        capture_form.addRow("OCR language", self.ocr_language)
        capture_form.addRow("OCR diagnostics", self.retain_uncertain_frames)
        capture_form.addRow("Diagnostics directory", diagnostics_layout)

        speech_form = QFormLayout()
        speech_form.addRow("Speech engine", self.speech_backend)
        speech_form.addRow("Speech model", self.tts_model)
        speech_form.addRow("TTS language", self.tts_language)
        speech_form.addRow("Narrator reference", narrator_reference_layout)
        speech_form.addRow("Voice manifest", self.voice_manifest)
        speech_form.addRow("Story index", self.story_index)
        speech_form.addRow("Generated audio manifest", self.generated_audio_manifest)
        speech_form.addRow("Narrator speaker", self.narrator_speaker)
        speech_form.addRow("Voice profile", self.tts_profile)
        speech_form.addRow("XTTS license", self.xtts_terms)

        playback_form = QFormLayout()
        playback_form.addRow("Output volume", self.output_volume)
        playback_form.addRow("Speaking speed", self.speech_rate)
        playback_form.addRow("Auto advance", self.auto_advance)
        playback_form.addRow("Advance key", self.auto_advance_key)
        playback_form.addRow("Advance delay", self.auto_advance_delay)

        application_form = QFormLayout()
        application_form.addRow("Startup readiness", self.warm_up_voices)
        application_form.addRow("macOS startup", self.launch_at_login)
        application_form.addRow("Closing the window", self.keep_running_on_close)

        self.settings_regions = (
            self._settings_region("Keyboard shortcuts", shortcuts_form),
            self._settings_region("Capture and OCR", capture_form),
            self._settings_region("Speech and voices", speech_form),
            self._settings_region("Playback and automation", playback_form),
            self._settings_region("Application behavior", application_form),
        )
        settings_content = QWidget()
        settings_content_layout = QVBoxLayout(settings_content)
        settings_content_layout.setContentsMargins(0, 0, 0, 0)
        settings_content_layout.setSpacing(14)
        for region in self.settings_regions:
            settings_content_layout.addWidget(region)
        settings_content_layout.addStretch()

        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.settings_scroll.setWidget(settings_content)

        note_text = (
            "Voice and model changes take effect after restarting the application."
        )
        if sys.platform != "darwin":
            note_text = f"Hotkey changes take effect immediately. {note_text}"
        note = QLabel(note_text)
        note.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.settings_scroll, 1)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self._resize_for_available_screen()
        self.capture_mode.currentIndexChanged.connect(self.update_capture_controls)
        self.tts_model.textChanged.connect(self.update_terms_control)
        self.speech_backend.currentIndexChanged.connect(
            self.update_speech_backend_controls
        )
        self.retain_uncertain_frames.toggled.connect(
            self.update_ocr_diagnostics_controls
        )
        self.auto_advance.toggled.connect(self.update_auto_advance_controls)
        self.update_capture_controls()
        self.update_speech_backend_controls()
        self.update_ocr_diagnostics_controls()
        self.update_auto_advance_controls()

    @staticmethod
    def _settings_region(title, form):
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        region = QGroupBox(title)
        region.setLayout(form)
        return region

    def _resize_for_available_screen(self):
        available = self.screen().availableGeometry()
        horizontal_margin = 64
        vertical_margin = 64
        available_width = max(320, available.width() - horizontal_margin)
        available_height = max(320, available.height() - vertical_margin)
        self.resize(
            min(760, available_width),
            min(800, available_height),
        )

    def browse_screenshot_directory(self):
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose screenshot directory",
            self.screenshot_directory.text(),
        )
        if selected:
            self.screenshot_directory.setText(selected)

    def browse_ocr_diagnostics_directory(self):
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose OCR diagnostics directory",
            self.ocr_diagnostics_directory.text(),
        )
        if selected:
            self.ocr_diagnostics_directory.setText(selected)

    def browse_narrator_reference(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose narrator voice reference",
            self.narrator_reference.text(),
            "Audio files (*.flac *.m4a *.mp3 *.ogg *.wav);;All files (*)",
        )
        if path:
            self.narrator_reference.setText(path)

    def update_ocr_diagnostics_controls(self):
        enabled = self.retain_uncertain_frames.isChecked()
        self.ocr_diagnostics_directory.setEnabled(enabled)
        self.diagnostics_browse_button.setEnabled(enabled)

    def update_auto_advance_controls(self):
        enabled = self.auto_advance.isChecked()
        self.auto_advance_key.setEnabled(enabled)
        self.auto_advance_delay.setEnabled(enabled)

    def refresh_windows(self):
        selected_title = self.game_window.currentText().strip()
        try:
            windows = list_windows()
        except WindowCaptureError as error:
            QMessageBox.warning(self, "Window capture unavailable", str(error))
            return
        self.game_window.clear()
        self.game_window.addItems(window.title for window in windows)
        if selected_title:
            self.game_window.setCurrentText(selected_title)

    def update_capture_controls(self):
        self.game_window.setEnabled(self.capture_mode.currentData() == "window")

    def update_terms_control(self):
        uses_xtts = self.speech_backend.currentData() == "coqui-xtts"
        self.xtts_terms.setEnabled(
            uses_xtts and "xtts" in self.tts_model.text().casefold()
        )

    def update_speech_backend_controls(self):
        backend = self.speech_backend.currentData()
        uses_xtts = backend == "coqui-xtts"
        uses_moss = backend == "moss-tts"
        if uses_moss and self.tts_model.text().strip() in {
            "",
            default_xtts_model,
        }:
            self.tts_model.setText(default_moss_tts_model)
        elif uses_xtts and self.tts_model.text().strip() in {
            "",
            default_moss_tts_model,
        }:
            self.tts_model.setText(default_xtts_model)
        self.tts_model.setEnabled(uses_xtts or uses_moss)
        self.tts_language.setEnabled(uses_xtts or uses_moss)
        self.narrator_reference.setEnabled(True)
        self.narrator_reference_button.setEnabled(True)
        self.narrator_speaker.setEnabled(uses_xtts)
        self.tts_profile.setEnabled(uses_xtts)
        self.speech_rate.setEnabled(uses_xtts)
        self.update_terms_control()

    def validate_and_accept(self):
        try:
            validate_hotkey_assignments(self.hotkey_assignments())
        except HotkeyValidationError as error:
            QMessageBox.warning(self, "Invalid hotkey", str(error))
            return
        if not self.screenshot_directory.text().strip():
            QMessageBox.warning(
                self,
                "Invalid screenshot directory",
                "Choose a directory for captured screenshots.",
            )
            return
        if (
            self.retain_uncertain_frames.isChecked()
            and not self.ocr_diagnostics_directory.text().strip()
        ):
            QMessageBox.warning(
                self,
                "Invalid OCR diagnostics directory",
                "Choose where uncertain OCR frames should be stored.",
            )
            return
        if (
            self.capture_mode.currentData() == "window"
            and not self.game_window.currentText().strip()
        ):
            QMessageBox.warning(
                self,
                "No game window selected",
                "Select the game window to capture.",
            )
            return
        if (
            self.speech_backend.currentData() == "coqui-xtts"
            and "xtts" in self.tts_model.text().casefold()
            and not self.xtts_terms.isChecked()
        ):
            QMessageBox.warning(
                self,
                "Model license not accepted",
                "Accept the CPML terms before using XTTS.",
            )
            return
        if (
            self.speech_backend.currentData() == "moss-tts"
            and not self.narrator_reference.text().strip()
            and find_voice_assignment(
                self.original_settings.voice_assignments,
                "Narrator",
            )
            is None
        ):
            QMessageBox.warning(
                self,
                "Narrator reference required",
                "Choose a narrator reference recording or assign an imported "
                "character voice to Narrator before using MOSS-TTS.",
            )
            return
        self.accept()

    def settings(self):
        def optional_text(widget):
            return widget.text().strip() or None

        hotkeys = self.hotkey_assignments()
        return AppSettings.from_mapping(
            {
                **asdict(self.original_settings),
                "read_hotkey": hotkeys["Read once"],
                "live_hotkey": hotkeys["Live reading"],
                "pause_hotkey": hotkeys["Pause or resume"],
                "skip_hotkey": hotkeys["Skip speech"],
                "repeat_hotkey": hotkeys["Repeat speech"],
                "clear_queue_hotkey": hotkeys["Clear queue"],
                "emergency_stop_hotkey": hotkeys["Emergency stop"],
                "screenshot_directory": self.screenshot_directory.text().strip(),
                "ocr_diagnostics_directory": (
                    self.ocr_diagnostics_directory.text().strip()
                ),
                "retain_uncertain_frames": self.retain_uncertain_frames.isChecked(),
                "capture_mode": self.capture_mode.currentData(),
                "game_window_title": self.game_window.currentText().strip() or None,
                "ocr_minimum_confidence": self.ocr_minimum_confidence.value(),
                "ocr_language": self.ocr_language.text().strip(),
                "speech_backend": self.speech_backend.currentData(),
                "tts_model": optional_text(self.tts_model),
                "tts_language": optional_text(self.tts_language),
                "tts_speaker_wav": optional_text(self.narrator_reference),
                "voice_manifest": optional_text(self.voice_manifest),
                "story_index": optional_text(self.story_index),
                "generated_audio_manifest": optional_text(
                    self.generated_audio_manifest
                ),
                "narrator_speaker": optional_text(self.narrator_speaker),
                "tts_profile": self.tts_profile.currentText(),
                "output_volume_percent": self.output_volume.value(),
                "speech_rate_percent": self.speech_rate.value(),
                "auto_advance_enabled": self.auto_advance.isChecked(),
                "auto_advance_key": self.auto_advance_key.currentData(),
                "auto_advance_delay_ms": self.auto_advance_delay.value(),
                "warm_up_voices": self.warm_up_voices.isChecked(),
                "launch_at_login": self.launch_at_login.isChecked(),
                "keep_running_on_close": self.keep_running_on_close.isChecked(),
                "xtts_terms_accepted": self.xtts_terms.isChecked(),
            }
        )

    def hotkey_assignments(self):
        return {
            "Read once": self.read_hotkey.hotkey(),
            "Live reading": self.live_hotkey.hotkey(),
            "Pause or resume": self.pause_hotkey.hotkey(),
            "Skip speech": self.skip_hotkey.hotkey(),
            "Repeat speech": self.repeat_hotkey.hotkey(),
            "Clear queue": self.clear_queue_hotkey.hotkey(),
            "Emergency stop": self.emergency_stop_hotkey.hotkey(),
        }


class TrayApplication(QObject):
    def __init__(
        self,
        application,
        settings=None,
        controller_factory=AppController,
        profile_store=None,
        correction_store=None,
    ):
        super().__init__()
        self.application = application
        uses_saved_settings = settings is None
        self.settings = settings or load_app_settings()
        self.signals = AppSignals()
        self.last_controller_error = None
        self.controller = controller_factory(
            self.settings,
            status_handler=self.signals.status_changed.emit,
            dialog_handler=self.signals.dialog_changed.emit,
            diagnostic_handler=self.signals.diagnostics_changed.emit,
            unknown_speaker_handler=self.signals.unknown_speaker.emit,
            error_handler=self.report_controller_error,
        )
        self.profile_store = profile_store or GameProfileStore.load()
        self.correction_store = correction_store or OCRCorrectionStore.load()
        self.support_log = RuntimeSupportLog(
            path=(
                get_local_data_directory() / "runtime.log"
                if uses_saved_settings
                else None
            )
        )
        self.hotkey_listener = None
        self.calibration_overlay = None
        self.onboarding_wizard = None
        self.diagnostics_dialog = None
        self.readiness_dialog = None
        self.support_dialog = None
        self.onboarding_cancel_event = Event()
        self.pending_unknown_speaker = None
        self.restore_compact_after_calibration = False
        self.dashboard = ControlDashboard(self.settings)
        self.compact_controller = CompactController()

        self.tray = QSystemTrayIcon(self._application_icon(), application)
        self.menu = QMenu()
        self.status_action = QAction("Starting...")
        self.status_action.setEnabled(False)
        self.dialog_action = QAction("No dialog detected")
        self.dialog_action.setEnabled(False)
        self.read_action = QAction("Read current dialog")
        self.show_dashboard_action = QAction("Open control window")
        self.show_compact_action = QAction("Compact floating controls")
        self.live_action = QAction("Start live reading")
        self.auto_advance_action = QAction("Auto advance dialogue")
        self.auto_advance_action.setCheckable(True)
        self.auto_advance_action.setChecked(self.settings.auto_advance_enabled)
        self.pause_action = QAction("Pause speech")
        self.skip_action = QAction("Skip current speech")
        self.repeat_action = QAction("Repeat last speech")
        self.clear_queue_action = QAction("Clear speech queue")
        self.emergency_stop_action = QAction("Emergency stop")
        self.calibrate_action = QAction("Calibrate dialog region")
        self.diagnostics_action = QAction("Live diagnostics...")
        self.settings_action = QAction("Settings...")
        self.profiles_action = QAction("Game profiles...")
        self.corrections_action = QAction("OCR corrections...")
        self.ocr_review_action = QAction("Review uncertain OCR...")
        self.setup_action = QAction("Run setup...")
        self.assets_action = QAction("Manage models and voices...")
        self.voice_preview_action = QAction("Choose voices...")
        self.speaker_mapping_action = QAction("Manage character voices...")
        self.history_action = QAction("Dialogue history...")
        self.support_action = QAction("Diagnostics and logs...")
        self.macos_permissions_action = QAction("macOS permissions...")
        self.macos_permissions_action.setVisible(sys.platform == "darwin")
        self.settings_folder_action = QAction("Open settings folder")
        self.quit_action = QAction("Quit")

        self.read_action.setEnabled(False)
        self.live_action.setEnabled(False)
        self.pause_action.setEnabled(False)
        self.skip_action.setEnabled(False)
        self.repeat_action.setEnabled(False)
        self.clear_queue_action.setEnabled(False)
        self.emergency_stop_action.setEnabled(False)
        self.voice_preview_action.setEnabled(False)
        self.menu.addAction(self.show_dashboard_action)
        self.menu.addAction(self.show_compact_action)
        self.menu.addAction(self.status_action)
        self.menu.addAction(self.dialog_action)
        self.menu.addSeparator()
        self.menu.addAction(self.read_action)
        self.menu.addAction(self.live_action)
        self.menu.addAction(self.auto_advance_action)
        self.menu.addAction(self.pause_action)
        self.menu.addAction(self.skip_action)
        self.menu.addAction(self.repeat_action)
        self.menu.addAction(self.clear_queue_action)
        self.menu.addAction(self.emergency_stop_action)
        self.menu.addAction(self.calibrate_action)
        self.menu.addAction(self.diagnostics_action)
        self.menu.addSeparator()
        self.menu.addAction(self.settings_action)
        self.menu.addAction(self.profiles_action)
        self.menu.addAction(self.corrections_action)
        self.menu.addAction(self.ocr_review_action)
        self.menu.addAction(self.setup_action)
        self.menu.addAction(self.assets_action)
        self.menu.addAction(self.voice_preview_action)
        self.menu.addAction(self.speaker_mapping_action)
        self.menu.addAction(self.history_action)
        self.menu.addAction(self.support_action)
        self.menu.addAction(self.macos_permissions_action)
        self.menu.addAction(self.settings_folder_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip(application_name)

        self.read_action.triggered.connect(self.read_once)
        self.show_dashboard_action.triggered.connect(self.show_dashboard)
        self.show_compact_action.triggered.connect(self.show_compact_controls)
        self.live_action.triggered.connect(self.toggle_live)
        self.auto_advance_action.toggled.connect(self.toggle_auto_advance)
        self.pause_action.triggered.connect(self.toggle_speech_pause)
        self.skip_action.triggered.connect(self.skip_current_speech)
        self.repeat_action.triggered.connect(self.repeat_last_speech)
        self.clear_queue_action.triggered.connect(self.clear_speech_queue)
        self.emergency_stop_action.triggered.connect(self.emergency_stop)
        self.calibrate_action.triggered.connect(self.calibrate)
        self.diagnostics_action.triggered.connect(self.open_diagnostics)
        self.settings_action.triggered.connect(self.open_settings)
        self.profiles_action.triggered.connect(self.open_profiles)
        self.corrections_action.triggered.connect(self.open_corrections)
        self.ocr_review_action.triggered.connect(self.open_ocr_review)
        self.setup_action.triggered.connect(self.run_onboarding)
        self.assets_action.triggered.connect(self.open_assets)
        self.voice_preview_action.triggered.connect(self.open_voice_previews)
        self.speaker_mapping_action.triggered.connect(self.open_speaker_mapping)
        self.history_action.triggered.connect(self.open_history)
        self.support_action.triggered.connect(self.open_support_center)
        self.macos_permissions_action.triggered.connect(self.open_macos_permissions)
        self.settings_folder_action.triggered.connect(self.open_settings_folder)
        self.quit_action.triggered.connect(self.application.quit)
        self.signals.status_changed.connect(self.set_status)
        self.signals.dialog_changed.connect(self.set_dialog)
        self.signals.ready_changed.connect(self.set_ready)
        self.signals.live_changed.connect(self.set_live)
        self.signals.speech_paused_changed.connect(self.set_speech_paused)
        self.signals.error_reported.connect(self.show_error)
        self.signals.diagnostics_changed.connect(self.update_diagnostics_snapshot)
        self.signals.diagnostics_failed.connect(self.set_diagnostics_error)
        self.signals.hotkeys_requested.connect(self.schedule_hotkeys)
        self.signals.support_export_finished.connect(self.support_export_finished)
        self.signals.unknown_speaker.connect(self.offer_speaker_mapping)
        self.application.aboutToQuit.connect(self.shutdown)
        self.dashboard.read_requested.connect(self.read_once)
        self.dashboard.live_requested.connect(self.toggle_live)
        self.dashboard.pause_requested.connect(self.toggle_speech_pause)
        self.dashboard.skip_requested.connect(self.skip_current_speech)
        self.dashboard.repeat_requested.connect(self.repeat_last_speech)
        self.dashboard.stop_requested.connect(self.emergency_stop)
        self.dashboard.readiness_requested.connect(self.open_readiness)
        self.dashboard.calibration_requested.connect(self.calibrate)
        self.dashboard.voices_requested.connect(self.open_speaker_mapping)
        self.dashboard.diagnostics_requested.connect(self.open_support_center)
        self.dashboard.settings_requested.connect(self.open_settings)
        self.dashboard.compact_requested.connect(self.show_compact_controls)
        self.dashboard.quit_requested.connect(self.application.quit)
        self.dashboard.hidden_to_background.connect(self.notify_background_mode)
        self.compact_controller.read_requested.connect(self.read_once)
        self.compact_controller.live_requested.connect(self.toggle_live)
        self.compact_controller.pause_requested.connect(self.toggle_speech_pause)
        self.compact_controller.skip_requested.connect(self.skip_current_speech)
        self.compact_controller.stop_requested.connect(self.emergency_stop)
        self.compact_controller.full_requested.connect(self.show_dashboard)

    def _application_icon(self):
        return create_application_icon(self.application.style())

    def start(self):
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        else:
            self.settings = self.settings.updated(keep_running_on_close=False)
            self.dashboard.set_configuration(self.settings)
        if self.settings.compact_controls and self.settings.onboarding_completed:
            self.show_compact_controls()
        else:
            self.show_dashboard()
        if self.settings.launch_at_login:
            try:
                configure_macos_launch_at_login(True)
            except OSError as error:
                self.show_error(f"Unable to configure launch at login: {error}")
        if self.settings.onboarding_completed:
            Thread(target=self._initialize_controller, daemon=True).start()
        else:
            self.set_status("Setup required")
            QTimer.singleShot(0, self.run_onboarding)

    def _initialize_controller(self):
        ready = self.controller.start()
        self.signals.ready_changed.emit(ready)
        if ready:
            self.signals.hotkeys_requested.emit()

    def schedule_hotkeys(self):
        QTimer.singleShot(250, self._start_hotkeys_safely)

    def _start_hotkeys_safely(self):
        if sys.platform == "darwin":
            self.support_log.add(
                "warning",
                "Global hotkeys are disabled on macOS because the current native "
                "listener is unstable. Use the control window.",
            )
            self.set_status("Ready; use the control window (macOS hotkeys disabled)")
            return
        try:
            self.start_hotkeys()
        except (TypeError, ValueError) as error:
            self.show_error(f"Unable to register hotkeys: {error}")

    def start_hotkeys(self):
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
        read_hotkey = get_hotkey(self.settings)
        live_hotkey = get_live_hotkey(self.settings)
        pause_hotkey = get_pause_hotkey(self.settings)
        skip_hotkey = get_skip_hotkey(self.settings)
        repeat_hotkey = get_repeat_hotkey(self.settings)
        clear_queue_hotkey = get_clear_queue_hotkey(self.settings)
        emergency_stop_hotkey = get_emergency_stop_hotkey(self.settings)
        validate_hotkey_assignments(
            {
                "Read once": read_hotkey,
                "Live reading": live_hotkey,
                "Pause or resume": pause_hotkey,
                "Skip speech": skip_hotkey,
                "Repeat speech": repeat_hotkey,
                "Clear queue": clear_queue_hotkey,
                "Emergency stop": emergency_stop_hotkey,
            }
        )
        self.hotkey_listener = keyboard.GlobalHotKeys(
            {
                read_hotkey: self.read_once,
                live_hotkey: self.toggle_live,
                pause_hotkey: self.toggle_speech_pause,
                skip_hotkey: self.skip_current_speech,
                repeat_hotkey: self.repeat_last_speech,
                clear_queue_hotkey: self.clear_speech_queue,
                emergency_stop_hotkey: self.emergency_stop,
            }
        )
        self.hotkey_listener.start()

    def read_once(self):
        self.controller.read_once()

    def toggle_live(self):
        self.signals.live_changed.emit(self.controller.toggle_live())

    def toggle_auto_advance(self, enabled):
        self.settings = self.settings.updated(auto_advance_enabled=bool(enabled))
        self.settings.save()
        self.controller.set_auto_advance_enabled(enabled)

    def toggle_speech_pause(self):
        self.signals.speech_paused_changed.emit(self.controller.toggle_speech_pause())

    def skip_current_speech(self):
        self.controller.skip_current_speech()

    def repeat_last_speech(self):
        self.controller.repeat_last_speech()

    def clear_speech_queue(self):
        self.controller.clear_speech_queue()

    def emergency_stop(self):
        self.controller.emergency_stop()
        self.signals.live_changed.emit(False)
        self.signals.speech_paused_changed.emit(False)

    def calibrate(self):
        try:
            geometry = self.controller.get_capture_geometry()
        except WindowCaptureError as error:
            self.show_error(str(error))
            return
        self.restore_compact_after_calibration = self.compact_controller.isVisible()
        self.dashboard.hide()
        self.compact_controller.hide()
        if self.readiness_dialog is not None:
            self.readiness_dialog.hide()
        QTimer.singleShot(200, lambda: self._open_calibration_overlay(geometry))

    def _open_calibration_overlay(self, geometry):
        try:
            self.calibration_overlay = show_calibration_overlay(geometry)
        except Exception as error:
            self.restore_control_window()
            self.show_error(f"Unable to capture a calibration preview: {error}")
            return
        self.calibration_overlay.closed.connect(self.restore_control_window)
        if self.settings.active_profile_id:
            self.calibration_overlay.selected.connect(self.update_profile_region)

    def restore_control_window(self):
        if self.restore_compact_after_calibration:
            self.show_compact_controls()
        else:
            self.show_dashboard()
        self.restore_compact_after_calibration = False

    def update_profile_region(self, region):
        profile_id = self.settings.active_profile_id
        if profile_id and self.profile_store.get(profile_id) is not None:
            self.profile_store.update_region(profile_id, region)

    def open_diagnostics(self):
        if self.diagnostics_dialog is None:
            self.diagnostics_dialog = DiagnosticsDialog()
            self.diagnostics_dialog.refresh_requested.connect(self.refresh_diagnostics)
        self.diagnostics_dialog.set_permission_warnings(macos_permission_warnings())
        snapshot = self.controller.get_latest_diagnostic()
        if snapshot is not None:
            self.diagnostics_dialog.set_snapshot(snapshot)
        self.diagnostics_dialog.show()
        self.diagnostics_dialog.raise_()
        self.diagnostics_dialog.activateWindow()

    def refresh_diagnostics(self):
        if self.controller.is_live_running:
            snapshot = self.controller.get_latest_diagnostic()
            if snapshot is not None:
                self.signals.diagnostics_changed.emit(snapshot)
                return

        permission_status = get_macos_permission_status()
        if permission_status["screen_capture"] is False:
            self.signals.diagnostics_failed.emit(
                "Screen Recording permission is missing. Open System Settings -> "
                "Privacy & Security -> Screen & System Audio Recording, allow the "
                "terminal or VNTTS, then quit and reopen it."
            )
            return

        if self.diagnostics_dialog is not None:
            self.diagnostics_dialog.conceal_for_capture()

        QTimer.singleShot(200, self._capture_diagnostic_snapshot)

    def _capture_diagnostic_snapshot(self):
        def inspect():
            try:
                self.controller.inspect_current_dialog()
            except Exception as error:
                self.signals.diagnostics_failed.emit(diagnostic_error_guidance(error))

        Thread(target=inspect, daemon=True).start()

    def update_diagnostics_snapshot(self, snapshot):
        self.dashboard.set_diagnostic(snapshot)
        if self.diagnostics_dialog is not None:
            self.diagnostics_dialog.set_snapshot(snapshot)
            self.diagnostics_dialog.restore_after_capture()

    def set_diagnostics_error(self, message):
        if self.diagnostics_dialog is not None:
            self.diagnostics_dialog.set_warning(message)
            self.diagnostics_dialog.restore_after_capture()

    def run_onboarding(self):
        if self.onboarding_wizard is not None:
            self.onboarding_wizard.show()
            self.onboarding_wizard.raise_()
            self.onboarding_wizard.activateWindow()
            return

        wizard = OnboardingWizard(self.settings)
        self.onboarding_wizard = wizard
        wizard.test_requested.connect(self.run_onboarding_test)
        wizard.cancel_requested.connect(self.cancel_onboarding_download)
        self.signals.onboarding_test_finished.connect(wizard.test_page.set_result)
        self.signals.onboarding_test_progress.connect(wizard.test_page.set_progress)
        wizard.finished.connect(
            lambda result, active_wizard=wizard: self.finish_onboarding(
                active_wizard,
                result,
            )
        )
        wizard.show()
        wizard.raise_()
        wizard.activateWindow()

    def finish_onboarding(self, wizard, result):
        if wizard is not self.onboarding_wizard:
            return
        self.onboarding_cancel_event.set()
        self.signals.onboarding_test_finished.disconnect(wizard.test_page.set_result)
        self.signals.onboarding_test_progress.disconnect(wizard.test_page.set_progress)
        self.onboarding_wizard = None

        if result != QDialog.DialogCode.Accepted:
            self.set_status("Setup required")
            wizard.deleteLater()
            return

        self.settings = wizard.settings()
        path = self.settings.save()
        self.controller.apply_settings(self.settings)
        self.set_ready(self.controller.is_ready)
        self.set_status(f"Setup completed; settings saved to {path}")
        wizard.deleteLater()
        self.signals.hotkeys_requested.emit()

    def run_onboarding_test(self, settings):
        self.onboarding_cancel_event = Event()

        def run_test():
            self.controller.apply_settings(settings)
            if settings.speech_backend == "coqui-xtts":
                try:
                    self.controller.model_assets.download(
                        settings.tts_model,
                        progress=self.signals.onboarding_test_progress.emit,
                        cancel_event=self.onboarding_cancel_event,
                    )
                except ModelDownloadCancelled as error:
                    self.signals.onboarding_test_finished.emit(False, str(error))
                    return
                except Exception as error:
                    self.signals.onboarding_test_finished.emit(
                        False,
                        f"Model download or verification failed: {error}",
                    )
                    return
            self.last_controller_error = None
            if not self.controller.start():
                self.signals.onboarding_test_finished.emit(
                    False,
                    self.last_controller_error
                    or "The speech engine could not be initialized.",
                )
                return
            try:
                character, text = self.controller.test_current_dialog()
            except Exception as error:
                self.signals.onboarding_test_finished.emit(
                    False,
                    format_runtime_error(error),
                )
                return
            preview = " ".join(text.split())
            if len(preview) > 160:
                preview = f"{preview[:157]}..."
            self.signals.onboarding_test_finished.emit(
                True,
                f"Success. Recognized {character}: {preview}",
            )

        Thread(target=run_test, daemon=True).start()

    def cancel_onboarding_download(self):
        self.onboarding_cancel_event.set()

    def open_settings(self):
        dialog = SettingsDialog(self.settings)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated_settings = dialog.settings()
        if updated_settings.launch_at_login != self.settings.launch_at_login:
            try:
                configure_macos_launch_at_login(updated_settings.launch_at_login)
            except OSError as error:
                self.show_error(f"Unable to configure launch at login: {error}")
                return
        self.settings = updated_settings
        self.dashboard.set_configuration(self.settings)
        self.auto_advance_action.blockSignals(True)
        self.auto_advance_action.setChecked(self.settings.auto_advance_enabled)
        self.auto_advance_action.blockSignals(False)
        path = self.settings.save()
        self._sync_active_profile()
        self.controller.apply_settings(self.settings)
        self.signals.hotkeys_requested.emit()
        self.set_status(f"Settings saved to {path}")
        if self.readiness_dialog is not None:
            self.readiness_dialog.update_settings(self.settings)

    def show_dashboard(self):
        self.compact_controller.hide()
        self.dashboard.show()
        self.dashboard.raise_()
        self.dashboard.activateWindow()
        self._save_compact_preference(False)

    def show_compact_controls(self):
        geometry = None
        try:
            geometry = self.controller.get_capture_geometry()
        except WindowCaptureError:
            pass
        self.dashboard.hide()
        self.compact_controller.show_for_game(geometry)
        self._save_compact_preference(True)

    def _save_compact_preference(self, enabled):
        enabled = bool(enabled)
        if self.settings.compact_controls == enabled:
            return
        self.settings = self.settings.updated(compact_controls=enabled)
        self.settings.save()

    def notify_background_mode(self):
        self.tray.showMessage(
            application_name,
            "VNTTS is still running in the background. Use the tray/menu-bar icon "
            "to reopen it or quit.",
            QSystemTrayIcon.MessageIcon.Information,
        )

    def open_readiness(self):
        if self.readiness_dialog is None:
            self.readiness_dialog = ReadinessDialog(
                self.settings,
                OnboardingDiagnostics(),
                self.dashboard,
            )
            self.readiness_dialog.settings_requested.connect(self.open_settings)
            self.readiness_dialog.permissions_requested.connect(
                self.open_macos_permissions
            )
            self.readiness_dialog.calibration_requested.connect(self.calibrate)
            self.readiness_dialog.voices_requested.connect(self.open_speaker_mapping)
        else:
            self.readiness_dialog.update_settings(self.settings)
        self.readiness_dialog.show()
        self.readiness_dialog.raise_()
        self.readiness_dialog.activateWindow()

    def open_profiles(self):
        dialog = GameProfilesDialog(
            self.settings,
            self.profile_store,
            self.correction_store,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings = dialog.settings()
        path = self.settings.save()
        self.controller.shutdown()
        self.controller.apply_settings(self.settings)
        ready = self.controller.start()
        self.set_ready(ready)
        if not ready:
            self.set_status("Unable to load the selected profile")
            return
        self.signals.hotkeys_requested.emit()
        profile = self.profile_store.get(self.settings.active_profile_id)
        self.set_status(f"Profile {profile.name!r} selected; settings saved to {path}")

    def open_corrections(self):
        profile = self.profile_store.get(self.settings.active_profile_id)
        dialog = OCRCorrectionsDialog(
            self.settings.active_profile_id,
            profile.name if profile is not None else None,
            self.correction_store,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.controller.refresh_corrections()
        self.set_status("OCR corrections saved")

    def open_ocr_review(self):
        profile = self.profile_store.get(self.settings.active_profile_id)
        dialog = OCRReviewDialog(
            self.settings.ocr_diagnostics_directory,
            self.correction_store,
            self.settings.active_profile_id,
            profile.name if profile is not None else None,
            self.controller.refresh_corrections,
        )
        dialog.exec()
        self.set_status("OCR review closed")

    def open_assets(self):
        dialog = AssetManagerDialog(self.settings)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings = dialog.settings()
        path = self.settings.save()
        self._sync_active_profile()
        self.controller.apply_settings(self.settings)
        self.set_status(
            f"Assets updated; restart to load voice or model changes. Saved to {path}"
        )

    def open_voice_previews(self):
        dialog = VoicePreviewDialog(
            self.controller.available_voice_characters(),
            self.controller.available_voice_choices(),
            self.controller.preview_voice_choice,
            self.assign_voice,
            self.controller.voice_assignment_for,
            initial_character="Narrator",
        )
        dialog.exec()

    def offer_speaker_mapping(self, speaker):
        self.pending_unknown_speaker = speaker
        self.speaker_mapping_action.setText(f"Manage voice for {speaker}...")
        self.tray.showMessage(
            "Character voice not mapped",
            f"{speaker} is using the narrator voice. Open the tray menu to map it.",
            QSystemTrayIcon.MessageIcon.Information,
        )

    def open_speaker_mapping(self):
        initial_character = self.pending_unknown_speaker or "Narrator"
        dialog = VoicePreviewDialog(
            self.controller.available_voice_characters(),
            self.controller.available_voice_choices(),
            self.controller.preview_voice_choice,
            self.assign_voice,
            self.controller.voice_assignment_for,
            initial_character=initial_character,
        )
        dialog.exec()
        self.pending_unknown_speaker = None
        self.speaker_mapping_action.setText("Manage character voices...")

    def assign_voice(self, character, source_id):
        self.settings = self.controller.assign_voice(character, source_id)
        path = self.settings.save()
        self._sync_active_profile()
        self.set_status(f"Voice for {character} saved to {path}")
        return self.settings

    def open_support_center(self):
        if self.support_dialog is None:
            self.support_dialog = SupportCenterDialog(self.support_log, self.dashboard)
            self.support_dialog.diagnostics_requested.connect(self.open_diagnostics)
            self.support_dialog.export_requested.connect(self.export_support_bundle)
            self.support_dialog.settings_folder_requested.connect(
                self.open_settings_folder
            )
        self.support_dialog.show()
        self.support_dialog.raise_()
        self.support_dialog.activateWindow()

    def open_history(self):
        dialog = DialogueHistoryDialog(
            self.controller.history,
            self.controller.replay_dialog,
        )
        dialog.exec()

    def export_support_bundle(self):
        path, _selected_filter = QFileDialog.getSaveFileName(
            None,
            "Export support bundle",
            "vntts-support.zip",
            "ZIP archives (*.zip)",
        )
        if not path:
            return
        self.set_status("Creating support bundle...")

        def export():
            try:
                output = SupportBundleBuilder(
                    self.settings,
                    self.support_log,
                    diagnostic=self.controller.get_latest_diagnostic(),
                ).build(path)
            except Exception as error:
                self.signals.support_export_finished.emit(False, str(error))
            else:
                self.signals.support_export_finished.emit(True, str(output))

        Thread(target=export, daemon=True).start()

    def support_export_finished(self, successful, message):
        if successful:
            self.set_status(f"Support bundle saved to {message}")
        else:
            self.show_error(f"Support bundle export failed: {message}")

    def open_macos_permissions(self):
        MacOSPermissionsDialog().exec()

    def _sync_active_profile(self):
        profile_id = self.settings.active_profile_id
        if profile_id and self.profile_store.get(profile_id) is not None:
            self.profile_store.update_from_settings(profile_id, self.settings)

    def open_settings_folder(self):
        path = get_settings_path().parent
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def set_status(self, message):
        self.support_log.add("status", message)
        self.status_action.setText(message)
        self.tray.setToolTip(f"{application_name}\n{message}")
        self.dashboard.set_status(message)
        self.compact_controller.set_status(message)

    def set_dialog(self, character, text):
        if not text:
            self.dialog_action.setText("No dialog detected")
            self.dashboard.set_dialogue(character, "")
            self.compact_controller.set_dialogue(character, "")
            return
        self.dialog_action.setText(f"{character}: {text}")
        self.dashboard.set_dialogue(character, text)
        self.compact_controller.set_dialogue(character, text)

    def set_ready(self, ready):
        self.read_action.setEnabled(ready)
        self.live_action.setEnabled(ready)
        self.pause_action.setEnabled(ready)
        self.skip_action.setEnabled(ready)
        self.repeat_action.setEnabled(ready)
        self.clear_queue_action.setEnabled(ready)
        self.emergency_stop_action.setEnabled(ready)
        self.voice_preview_action.setEnabled(ready)
        self.dashboard.set_ready(ready)
        self.compact_controller.set_ready(ready)
        if not ready:
            self.set_status("Unable to start")

    def set_live(self, running):
        self.live_action.setText(
            "Stop live reading" if running else "Start live reading"
        )
        self.dashboard.set_live(running)
        self.compact_controller.set_live(running)

    def set_speech_paused(self, paused):
        self.pause_action.setText("Resume speech" if paused else "Pause speech")
        self.dashboard.set_paused(paused)
        self.compact_controller.set_paused(paused)

    def show_error(self, message):
        self.support_log.add("error", message)
        self.set_status(message)
        self.tray.showMessage(
            f"{application_name} error",
            message,
            QSystemTrayIcon.MessageIcon.Critical,
        )

    def report_controller_error(self, error):
        message = format_runtime_error(error)
        self.last_controller_error = message
        self.signals.error_reported.emit(message)

    def shutdown(self):
        self.dashboard.keep_running_on_close = False
        self.dashboard._quitting = True
        self.dashboard.close()
        self.compact_controller.close()
        if self.diagnostics_dialog is not None:
            self.diagnostics_dialog.close()
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
            self.hotkey_listener = None
        self.controller.shutdown()


def main(argv=None):
    freeze_support()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--package-self-test", action="store_true")
    parser.add_argument("--package-self-test-report")
    parser.add_argument("--release-smoke-test-image")
    parser.add_argument("--release-smoke-test-window-title")
    parser.add_argument("--release-smoke-test-report")
    parser.add_argument(
        "--release-smoke-test-model",
        default=default_smoke_test_model,
    )
    parser.add_argument("--release-smoke-test-expected-speaker")
    arguments, qt_arguments = parser.parse_known_args(
        sys.argv[1:] if argv is None else argv
    )
    configure_bundled_dependencies()
    if arguments.package_self_test:
        return run_package_self_test(arguments.package_self_test_report).exit_code
    if arguments.release_smoke_test_image or arguments.release_smoke_test_window_title:
        return run_release_smoke_test(
            image_path=arguments.release_smoke_test_image,
            window_title=arguments.release_smoke_test_window_title,
            report_path=arguments.release_smoke_test_report,
            model_name=arguments.release_smoke_test_model,
            expected_speaker=arguments.release_smoke_test_expected_speaker,
        ).exit_code

    enable_windows_dpi_awareness()
    application_arguments = [sys.argv[0], *qt_arguments]
    application = QApplication.instance() or QApplication(application_arguments)
    application.setApplicationName(application_name)
    application.setQuitOnLastWindowClosed(False)
    tray_application = TrayApplication(application)
    tray_application.start()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())

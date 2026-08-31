import argparse
import sys
from dataclasses import asdict
from multiprocessing import freeze_support
from pathlib import Path
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
    QInputDialog,
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
from vntts.async_ui import LatestTaskRunner
from vntts.calibration import show_calibration_overlay
from vntts.configuration_apply import ConfigurationApplyMixin
from vntts.controller import AppController
from vntts.dashboard_ui import (
    CompactController,
    ControlDashboard,
    configure_floating_window,
)
from vntts.diagnostics import diagnostic_error_guidance, macos_permission_warnings
from vntts.diagnostics_ui import DiagnosticsDialog
from vntts.dialog_capture import format_runtime_error
from vntts.durable_settings import DurableSettingsMixin
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
    is_live_sequence_audio_mode,
    load_app_settings,
)
from vntts.speech_backend import default_moss_tts_model
from vntts.support import (
    GenerationTimelineLog,
    RuntimeSupportLog,
    SupportBundleBuilder,
)
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


def _add_composite_form_row(form, label_text, field, field_layout):
    label = QLabel(label_text)
    label.setBuddy(field)
    form.addRow(label, field_layout)
    return label


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
    diagnostics_refresh_finished = Signal(int, object)
    diagnostics_refresh_failed = Signal(int, str)
    sequence_status_changed = Signal(object)
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
        self.game_window.setAccessibleName("Game window")
        self.game_window.setAccessibleDescription(
            "Window title captured for dialogue recognition"
        )
        if settings.game_window_title:
            self.game_window.addItem(settings.game_window_title)
            self.game_window.setCurrentText(settings.game_window_title)
        refresh_windows_button = QPushButton("Refresh...")
        refresh_windows_button.setAccessibleName("Refresh game windows")
        refresh_windows_button.setAccessibleDescription(
            "Reload the list of capturable game windows"
        )
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
        self.audio_source_policy = QComboBox()
        self.audio_source_policy.addItem(
            "Live TTS only (selected speech engine)",
            "live-tts-only",
        )
        self.audio_source_policy.addItem(
            "Prefer generated audio, then live TTS",
            "prefer-generated",
        )
        self.audio_source_policy.addItem(
            "Prefer original game audio, then generated/live TTS",
            "prefer-game-audio",
        )
        self.audio_source_policy.setCurrentIndex(
            max(
                0,
                self.audio_source_policy.findData(settings.audio_source_policy),
            )
        )
        self.ocr_minimum_confidence = QSpinBox()
        self.ocr_minimum_confidence.setRange(0, 100)
        self.ocr_minimum_confidence.setSuffix("%")
        self.ocr_minimum_confidence.setValue(settings.ocr_minimum_confidence)
        self.ocr_language = QLineEdit(settings.ocr_language)
        self.tts_language = QLineEdit(settings.tts_language or "")
        self.narrator_reference = QLineEdit(settings.tts_speaker_wav or "")
        self.narrator_reference.setAccessibleName("Narrator reference")
        self.narrator_reference.setAccessibleDescription(
            "Audio reference used for the narrator voice"
        )
        default_voice_manifest = find_default_voice_manifest()
        self.game_pack = QLineEdit(settings.game_pack or "")
        self.voice_manifest = QLineEdit(
            settings.voice_manifest
            or (str(default_voice_manifest) if default_voice_manifest else "")
        )
        self.story_index = QLineEdit(settings.story_index or "")
        self.live_sequence_plan = QLineEdit(settings.live_sequence_plan or "")
        self.live_sequence_mode = QComboBox()
        self.live_sequence_mode.addItem("Disabled", "off")
        self.live_sequence_mode.addItem(
            "Shadow diagnostics (does not control speech)", "shadow"
        )
        self.live_sequence_mode.addItem(
            "Canonical audio routing (manual advancement)", "audio-manual"
        )
        self.live_sequence_mode.addItem(
            "Canonical audio + guarded auto advance (experimental)", "audio-auto"
        )
        self.live_sequence_mode.setCurrentIndex(
            max(0, self.live_sequence_mode.findData(settings.live_sequence_mode))
        )
        self.live_speaker_corpus = QLineEdit(settings.live_speaker_corpus or "")
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
        self.speaker_announcement_mode = QComboBox()
        self.speaker_announcement_mode.addItem("Disabled", "off")
        self.speaker_announcement_mode.addItem(
            "Narrator fallback roles only", "narrator-fallback-roles"
        )
        self.speaker_announcement_mode.addItem("All speaker changes", "all-speakers")
        self.speaker_announcement_mode.setCurrentIndex(
            max(
                0,
                self.speaker_announcement_mode.findData(
                    settings.effective_speaker_announcement_mode
                ),
            )
        )
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

        (
            screenshot_layout,
            self.screenshot_browse_button,
        ) = self._path_selector(
            self.screenshot_directory,
            "Screenshot directory",
            "Choose where captured screenshots are stored",
            directory=True,
        )
        (
            diagnostics_layout,
            self.diagnostics_browse_button,
        ) = self._path_selector(
            self.ocr_diagnostics_directory,
            "Diagnostics directory",
            "Choose where uncertain OCR frames are stored",
            directory=True,
        )
        (
            narrator_reference_layout,
            self.narrator_reference_button,
        ) = self._path_selector(
            self.narrator_reference,
            "Narrator reference",
            "Choose an audio reference used for the narrator voice",
            file_filter="Audio files (*.flac *.m4a *.mp3 *.ogg *.wav);;All files (*)",
        )
        game_pack_layout, self.game_pack_button = self._path_selector(
            self.game_pack,
            "Game pack",
            "Choose a verified game-pack manifest",
            file_filter="JSON files (*.json);;All files (*)",
        )
        voice_manifest_layout, self.voice_manifest_button = self._path_selector(
            self.voice_manifest,
            "Voice manifest",
            "Choose the character voice manifest",
            file_filter="JSON files (*.json);;All files (*)",
        )
        story_index_layout, self.story_index_button = self._path_selector(
            self.story_index,
            "Story index",
            "Choose the story index used for source and generated audio routing",
            file_filter="Story indexes (*.jsonl *.json);;All files (*)",
        )
        (
            live_sequence_plan_layout,
            self.live_sequence_plan_button,
        ) = self._path_selector(
            self.live_sequence_plan,
            "Live sequence plan",
            "Choose the checksum-bound live story sequence plan",
            file_filter="JSON files (*.json);;All files (*)",
        )
        (
            live_speaker_corpus_layout,
            self.live_speaker_corpus_button,
        ) = self._path_selector(
            self.live_speaker_corpus,
            "Live speaker corpus",
            "Choose the explicit speaker corpus for sessions without a story index",
            file_filter="JSON files (*.json);;All files (*)",
        )
        (
            generated_audio_manifest_layout,
            self.generated_audio_manifest_button,
        ) = self._path_selector(
            self.generated_audio_manifest,
            "Generated audio manifest",
            "Choose the generated-audio manifest used during live routing",
            file_filter="JSON files (*.json);;All files (*)",
        )
        for field in (
            self.speech_backend,
            self.tts_model,
            self.tts_language,
            self.narrator_reference,
            self.voice_manifest,
            self.narrator_speaker,
        ):
            current = field.accessibleDescription().strip()
            restart_description = (
                "Changes to this setting require an application restart."
            )
            field.setAccessibleDescription(f"{current} {restart_description}".strip())
        self.refresh_windows_button = refresh_windows_button

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
        _add_composite_form_row(
            capture_form,
            "Screenshot directory",
            self.screenshot_directory,
            screenshot_layout,
        )
        capture_form.addRow("Capture source", self.capture_mode)
        _add_composite_form_row(
            capture_form, "Game window", self.game_window, window_layout
        )
        capture_form.addRow("Minimum OCR confidence", self.ocr_minimum_confidence)
        capture_form.addRow("OCR language", self.ocr_language)
        capture_form.addRow("OCR diagnostics", self.retain_uncertain_frames)
        _add_composite_form_row(
            capture_form,
            "Diagnostics directory",
            self.ocr_diagnostics_directory,
            diagnostics_layout,
        )

        speech_form = QFormLayout()
        speech_form.addRow("Speech engine (restart required)", self.speech_backend)
        speech_form.addRow("Audio source policy", self.audio_source_policy)
        speech_form.addRow("Speech model (restart required)", self.tts_model)
        speech_form.addRow("TTS language (restart required)", self.tts_language)
        _add_composite_form_row(
            speech_form,
            "Narrator reference (restart required)",
            self.narrator_reference,
            narrator_reference_layout,
        )
        _add_composite_form_row(
            speech_form, "Game pack", self.game_pack, game_pack_layout
        )
        _add_composite_form_row(
            speech_form,
            "Voice manifest (restart required)",
            self.voice_manifest,
            voice_manifest_layout,
        )
        _add_composite_form_row(
            speech_form, "Story index", self.story_index, story_index_layout
        )
        _add_composite_form_row(
            speech_form,
            "Live sequence plan",
            self.live_sequence_plan,
            live_sequence_plan_layout,
        )
        _add_composite_form_row(
            speech_form,
            "Live speaker corpus",
            self.live_speaker_corpus,
            live_speaker_corpus_layout,
        )
        _add_composite_form_row(
            speech_form,
            "Generated audio manifest",
            self.generated_audio_manifest,
            generated_audio_manifest_layout,
        )
        speech_form.addRow("Narrator speaker (restart required)", self.narrator_speaker)
        speech_form.addRow("Voice profile", self.tts_profile)
        speech_form.addRow("XTTS license", self.xtts_terms)

        playback_form = QFormLayout()
        playback_form.addRow("Output volume", self.output_volume)
        playback_form.addRow("Speaking speed", self.speech_rate)
        playback_form.addRow("Auto advance", self.auto_advance)
        playback_form.addRow("Sequence-first rollout", self.live_sequence_mode)
        playback_form.addRow("Speaker announcements", self.speaker_announcement_mode)
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

        self.section_navigation = QComboBox()
        self.section_navigation.addItems(
            region.title() for region in self.settings_regions
        )
        self.section_navigation.setAccessibleName("Settings section")
        self.section_navigation.setAccessibleDescription(
            "Choose a settings section and scroll directly to it"
        )
        section_label = QLabel("Section")
        section_label.setBuddy(self.section_navigation)
        section_navigation_layout = QHBoxLayout()
        section_navigation_layout.addWidget(section_label)
        section_navigation_layout.addWidget(self.section_navigation, 1)

        self.validation_summary = QLabel()
        self.validation_summary.setWordWrap(True)
        self.validation_summary.setAccessibleName("Settings validation summary")
        self.validation_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        note_text = (
            "Fields marked 'restart required' are saved immediately and take "
            "effect after restarting the application."
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
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)

        layout = QVBoxLayout(self)
        layout.addLayout(section_navigation_layout)
        layout.addWidget(self.validation_summary)
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
        self.live_sequence_mode.currentIndexChanged.connect(
            self.update_auto_advance_controls
        )
        self.section_navigation.currentIndexChanged.connect(self.scroll_to_section)
        self._connect_validation_updates()
        self.update_capture_controls()
        self.update_speech_backend_controls()
        self.update_ocr_diagnostics_controls()
        self.update_auto_advance_controls()
        self.update_validation_summary()

    @staticmethod
    def _settings_region(title, form):
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        region = QGroupBox(title)
        region.setLayout(form)
        return region

    def _path_selector(
        self,
        field,
        name,
        description,
        *,
        directory=False,
        file_filter="All files (*)",
    ):
        field.setAccessibleName(name)
        field.setAccessibleDescription(description)
        button = QPushButton("Browse...")
        button.setAccessibleName(f"Browse for {name.casefold()}")
        button.setAccessibleDescription(description)
        button.clicked.connect(
            lambda _checked=False: self._browse_path(
                field,
                name,
                directory=directory,
                file_filter=file_filter,
            )
        )
        field_layout = QHBoxLayout()
        field_layout.addWidget(field)
        field_layout.addWidget(button)
        return field_layout, button

    def _browse_path(
        self,
        field,
        name,
        *,
        directory=False,
        file_filter="All files (*)",
    ):
        if directory:
            selected = QFileDialog.getExistingDirectory(
                self,
                f"Choose {name.casefold()}",
                field.text(),
            )
        else:
            selected, _selected_filter = QFileDialog.getOpenFileName(
                self,
                f"Choose {name.casefold()}",
                field.text(),
                file_filter,
            )
        if selected:
            field.setText(selected)

    def scroll_to_section(self, index):
        if 0 <= index < len(self.settings_regions):
            self.settings_scroll.ensureWidgetVisible(
                self.settings_regions[index],
                0,
                12,
            )

    def _connect_validation_updates(self):
        for recorder in self.hotkey_recorders:
            recorder.keySequenceChanged.connect(self.update_validation_summary)
        for field in (
            self.screenshot_directory,
            self.ocr_diagnostics_directory,
            self.tts_model,
            self.tts_language,
            self.narrator_reference,
            self.game_pack,
            self.voice_manifest,
            self.story_index,
            self.live_sequence_plan,
            self.live_speaker_corpus,
            self.generated_audio_manifest,
            self.narrator_speaker,
        ):
            field.textChanged.connect(self.update_validation_summary)
        for field in (
            self.capture_mode,
            self.game_window,
            self.speech_backend,
            self.live_sequence_mode,
        ):
            field.currentIndexChanged.connect(self.update_validation_summary)
        self.game_window.currentTextChanged.connect(self.update_validation_summary)
        for field in (
            self.retain_uncertain_frames,
            self.xtts_terms,
        ):
            field.toggled.connect(self.update_validation_summary)

    @staticmethod
    def _directory_validation_error(label, value):
        text = value.strip()
        if not text:
            return f"{label}: choose a directory."
        path = Path(text).expanduser()
        if path.exists() and not path.is_dir():
            return f"{label}: the selected path is not a directory."
        parent = path if path.exists() else path.parent
        if not parent.exists() or not parent.is_dir():
            return f"{label}: the parent directory does not exist."
        return None

    @staticmethod
    def _file_validation_error(label, value):
        text = value.strip()
        if not text:
            return None
        if not Path(text).expanduser().is_file():
            return f"{label}: the selected file does not exist."
        return None

    def validation_errors(self):
        errors = []

        def add(section, widget, message):
            if message:
                errors.append((section, widget, message))

        try:
            validate_hotkey_assignments(self.hotkey_assignments())
        except HotkeyValidationError as error:
            add(0, self.read_hotkey, f"Keyboard shortcuts: {error}.")
        add(
            1,
            self.screenshot_directory,
            self._directory_validation_error(
                "Screenshot directory", self.screenshot_directory.text()
            ),
        )
        if self.retain_uncertain_frames.isChecked():
            add(
                1,
                self.ocr_diagnostics_directory,
                self._directory_validation_error(
                    "OCR diagnostics directory",
                    self.ocr_diagnostics_directory.text(),
                ),
            )
        if (
            self.capture_mode.currentData() == "window"
            and not self.game_window.currentText().strip()
        ):
            add(1, self.game_window, "Capture source: select the game window.")
        if (
            self.speech_backend.currentData() == "coqui-xtts"
            and "xtts" in self.tts_model.text().casefold()
            and not self.xtts_terms.isChecked()
        ):
            add(2, self.xtts_terms, "XTTS license: accept the CPML terms.")
        narrator_assignment = find_voice_assignment(
            self.original_settings.voice_assignments,
            "Narrator",
        )
        if (
            self.speech_backend.currentData() == "moss-tts"
            and not self.narrator_reference.text().strip()
            and narrator_assignment is None
        ):
            add(
                2,
                self.narrator_reference,
                "Narrator reference: choose a recording or assign an imported "
                "character voice to Narrator before using MOSS-TTS.",
            )
        for label, field in (
            ("Narrator reference", self.narrator_reference),
            ("Game pack", self.game_pack),
            ("Voice manifest", self.voice_manifest),
            ("Story index", self.story_index),
            ("Live sequence plan", self.live_sequence_plan),
            ("Live speaker corpus", self.live_speaker_corpus),
            ("Generated audio manifest", self.generated_audio_manifest),
        ):
            add(2, field, self._file_validation_error(label, field.text()))
        if self.live_sequence_mode.currentData() != "off":
            if not self.live_sequence_plan.text().strip():
                add(
                    2,
                    self.live_sequence_plan,
                    "Sequence-first rollout: choose a live sequence plan for "
                    "the selected mode.",
                )
            if not self.story_index.text().strip():
                add(
                    2,
                    self.story_index,
                    "Story index: choose the exact index bound by the live "
                    "sequence plan.",
                )
        return tuple(errors)

    def update_validation_summary(self, *_args):
        errors = self.validation_errors()
        if errors:
            self.validation_summary.setText(
                f"Fix {len(errors)} setting(s) before saving:\n"
                + "\n".join(f"- {message}" for _section, _widget, message in errors)
            )
            self.validation_summary.setStyleSheet("color: #b3261e; font-weight: 600;")
        else:
            self.validation_summary.setText("All settings are valid.")
            self.validation_summary.setStyleSheet("")
        return errors

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
        self._browse_path(
            self.screenshot_directory,
            "Screenshot directory",
            directory=True,
        )

    def browse_ocr_diagnostics_directory(self):
        self._browse_path(
            self.ocr_diagnostics_directory,
            "OCR diagnostics directory",
            directory=True,
        )

    def browse_narrator_reference(self):
        self._browse_path(
            self.narrator_reference,
            "Narrator reference",
            file_filter="Audio files (*.flac *.m4a *.mp3 *.ogg *.wav);;All files (*)",
        )

    def update_ocr_diagnostics_controls(self):
        enabled = self.retain_uncertain_frames.isChecked()
        self.ocr_diagnostics_directory.setEnabled(enabled)
        self.diagnostics_browse_button.setEnabled(enabled)

    def update_auto_advance_controls(self):
        sequence_mode = self.live_sequence_mode.currentData()
        manual_sequence = sequence_mode == "audio-manual"
        self.auto_advance.setEnabled(not manual_sequence)
        self.auto_advance.setToolTip(
            "Sequence-first canonical routing never sends advance keys in the "
            "manual rollout phase."
            if manual_sequence
            else (
                "Experimental sequence control sends at most one key for the "
                "current automatic event, only while the selected game window is "
                "focused and its dialogue frame remains visible and stable."
                if sequence_mode == "audio-auto"
                else ""
            )
        )
        enabled = self.auto_advance.isChecked() and not manual_sequence
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
        self.tts_profile.setEnabled(uses_xtts or uses_moss)
        self.speech_rate.setEnabled(uses_xtts)
        self.update_terms_control()

    def validate_and_accept(self):
        errors = self.update_validation_summary()
        if errors:
            section, widget, _message = errors[0]
            self.section_navigation.setCurrentIndex(section)
            self.scroll_to_section(section)
            widget.setFocus(Qt.FocusReason.OtherFocusReason)
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
                "audio_source_policy": self.audio_source_policy.currentData(),
                "tts_model": optional_text(self.tts_model),
                "tts_language": optional_text(self.tts_language),
                "tts_speaker_wav": optional_text(self.narrator_reference),
                "game_pack": optional_text(self.game_pack),
                "voice_manifest": optional_text(self.voice_manifest),
                "story_index": optional_text(self.story_index),
                "live_sequence_plan": optional_text(self.live_sequence_plan),
                "live_sequence_mode": self.live_sequence_mode.currentData(),
                "live_speaker_corpus": optional_text(self.live_speaker_corpus),
                "generated_audio_manifest": optional_text(
                    self.generated_audio_manifest
                ),
                "narrator_speaker": optional_text(self.narrator_speaker),
                "tts_profile": self.tts_profile.currentText(),
                "output_volume_percent": self.output_volume.value(),
                "speech_rate_percent": self.speech_rate.value(),
                "auto_advance_enabled": self.auto_advance.isChecked(),
                "speaker_announcement_mode": self.speaker_announcement_mode.currentData(),
                "announce_speaker_changes": (
                    self.speaker_announcement_mode.currentData() == "all-speakers"
                ),
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


class TrayApplication(ConfigurationApplyMixin, DurableSettingsMixin, QObject):
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
        self.support_log = RuntimeSupportLog(
            path=(
                get_local_data_directory() / "runtime.log"
                if uses_saved_settings
                else None
            )
        )
        self.generation_timelines = GenerationTimelineLog(
            path=(
                get_local_data_directory() / "generation-timelines.json"
                if uses_saved_settings
                else None
            )
        )
        self.controller = controller_factory(
            self.settings,
            status_handler=self.signals.status_changed.emit,
            dialog_handler=self.signals.dialog_changed.emit,
            diagnostic_handler=self.signals.diagnostics_changed.emit,
            sequence_status_handler=self.signals.sequence_status_changed.emit,
            unknown_speaker_handler=self.signals.unknown_speaker.emit,
            error_handler=self.report_controller_error,
            route_trace_handler=self.record_audio_route,
            pipeline_event_handler=self.generation_timelines.record,
        )
        self.live_stop_runner = LatestTaskRunner(self)
        self.live_stop_runner.finished.connect(self._live_stop_finished)
        self._live_stop_continuation = None
        self._live_stop_generation = None
        self.profile_restart_runner = LatestTaskRunner(self)
        self.profile_restart_runner.finished.connect(self._profile_restart_finished)
        self._setup_configuration_apply()
        self.initial_start_runner = LatestTaskRunner(self)
        self.initial_start_runner.finished.connect(self._initial_start_finished)
        self._initial_start_generation = None
        self.live_scope_runner = LatestTaskRunner(self)
        self.live_scope_runner.finished.connect(self._live_scope_finished)
        self._live_scope_generation = None
        self._pending_profile_name = self._profile_restart_generation = None
        self._lifecycle_generation = 0
        self._controller_ready = False
        self._controller_busy = False
        self._shutting_down = False
        self._onboarding_test_active = False
        self.profile_store = profile_store or GameProfileStore.load()
        self.correction_store = correction_store or OCRCorrectionStore.load()
        self.hotkey_listener = None
        self.calibration_overlay = None
        self.onboarding_wizard = None
        self.diagnostics_dialog = None
        self.diagnostics_refresh_generation = 0
        self.readiness_dialog = None
        self.support_dialog = None
        self.unknown_speaker_prompt = None
        self.unknown_speaker_choose_button = None
        self.unknown_speaker_continue_button = None
        self.unknown_speaker_mapping_in_progress = None
        self.resume_live_after_unknown_mapping = False
        self.onboarding_cancel_event = Event()
        self.pending_unknown_speaker = None
        self.live_voice_preflight_prompt = None
        self.live_voice_preflight_assign_button = None
        self.live_voice_preflight_narrator_button = None
        self.live_voice_preflight_cancel_button = None
        self.live_voice_preflight_action_prompt = None
        self.pending_live_voice_preflight_speakers = ()
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
        self.sequence_resync_action = QAction("Set story position / resync...")
        self.sequence_expected_action = QAction("Use expected next line")
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
        self.voice_preview_action = QAction("Choose narrator voice...")
        self.speaker_mapping_action = QAction("Manage character voices...")
        self.history_action = QAction("Dialogue history...")
        self.support_action = QAction("Diagnostics and logs...")
        self.macos_permissions_action = QAction("macOS permissions...")
        self.macos_permissions_action.setVisible(sys.platform == "darwin")
        self.settings_folder_action = QAction("Open settings folder")
        self.quit_action = QAction("Quit")

        self.read_action.setEnabled(False)
        self.live_action.setEnabled(False)
        self.sequence_resync_action.setEnabled(False)
        self.sequence_expected_action.setEnabled(False)
        self.sequence_resync_action.setVisible(
            is_live_sequence_audio_mode(self.settings.live_sequence_mode)
        )
        self.sequence_expected_action.setVisible(
            is_live_sequence_audio_mode(self.settings.live_sequence_mode)
        )
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
        self.menu.addAction(self.sequence_resync_action)
        self.menu.addAction(self.sequence_expected_action)
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
        self.sequence_resync_action.triggered.connect(self.choose_sequence_position)
        self.sequence_expected_action.triggered.connect(
            self.choose_expected_sequence_event
        )
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
        self.signals.diagnostics_refresh_finished.connect(
            self._diagnostics_refresh_finished
        )
        self.signals.diagnostics_refresh_failed.connect(
            self._diagnostics_refresh_failed
        )
        self.signals.sequence_status_changed.connect(self.set_sequence_status)
        self.signals.diagnostics_failed.connect(self.set_diagnostics_error)
        self.signals.hotkeys_requested.connect(self.schedule_hotkeys)
        self.signals.support_export_finished.connect(self.support_export_finished)
        self.signals.unknown_speaker.connect(self.offer_speaker_mapping)
        self.application.aboutToQuit.connect(self.shutdown)
        current_sequence_status = getattr(
            self.controller, "get_live_sequence_status", None
        )
        if callable(current_sequence_status):
            sequence_status = current_sequence_status()
            if getattr(sequence_status, "mode", None) in {
                "off",
                "shadow",
                "audio-manual",
                "audio-auto",
            }:
                self.set_sequence_status(sequence_status)
        self.dashboard.read_requested.connect(self.read_once)
        self.dashboard.live_requested.connect(self.toggle_live)
        self.dashboard.sequence_resync_requested.connect(self.choose_sequence_position)
        self.dashboard.sequence_expected_requested.connect(
            self.choose_expected_sequence_event
        )
        self.dashboard.pause_requested.connect(self.toggle_speech_pause)
        self.dashboard.skip_requested.connect(self.skip_current_speech)
        self.dashboard.repeat_requested.connect(self.repeat_last_speech)
        self.dashboard.stop_requested.connect(self.emergency_stop)
        self.dashboard.readiness_requested.connect(self.open_readiness)
        self.dashboard.calibration_requested.connect(self.calibrate)
        self.dashboard.voices_requested.connect(self.open_voice_previews)
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
        self.compact_controller.sequence_expected_requested.connect(
            self.choose_expected_sequence_event
        )
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
            generation = self._begin_controller_lifecycle()
            self._initial_start_generation = generation
            self.initial_start_runner.start(self._initialize_controller, generation)
        else:
            self.set_status("Setup required")
            QTimer.singleShot(0, self.run_onboarding)

    def _initialize_controller(self, generation):
        try:
            ready = self.controller.start()
        except Exception:
            self.controller.shutdown()
            raise
        if not self._lifecycle_is_current(generation):
            self.controller.shutdown()
            return False
        return ready

    def _initial_start_finished(self, ready, error):
        generation = self._initial_start_generation
        self._initial_start_generation = None
        if not self._lifecycle_is_current(generation):
            return
        self._finish_controller_lifecycle()
        if error is not None:
            self.show_error(f"Unable to initialize controller: {error}")
            ready = False
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
        if not self.controller.is_live_running:
            return self._start_live_with_preflight()
        return self._toggle_controller_live()

    def choose_sequence_position(self):
        options = self.controller.live_sequence_anchor_options()
        if not options:
            self.set_status(
                "Story position is unavailable: configure a sequence-first manual pack"
            )
            return False
        labels = [label for label, _event_id in options]
        sequence_status = self.controller.get_live_sequence_status()
        current_event_id = getattr(sequence_status, "event_id", None)
        current_index = next(
            (
                index
                for index, (_label, event_id) in enumerate(options)
                if event_id == current_event_id
            ),
            0,
        )
        selected, accepted = QInputDialog.getItem(
            self.dashboard,
            "Set story position / resync",
            "Choose the dialogue box currently visible in the game. This stops "
            "stale queued speech and makes the selected event authoritative.",
            labels,
            current_index,
            False,
        )
        if not accepted:
            return False
        event_id = dict(options)[selected]
        return self.controller.resync_live_sequence(event_id)

    def choose_expected_sequence_event(self):
        options = self.controller.live_sequence_expected_options()
        if not options:
            self.set_status(
                "No expected story candidate is currently available; use resync if "
                "the game moved outside the bounded path"
            )
            return False
        if len(options) == 1:
            return self.controller.select_expected_live_sequence_event(options[0][1])
        labels = [label for label, _event_id in options]
        selected, accepted = QInputDialog.getItem(
            self.dashboard,
            "Choose expected story event",
            "Choose only the dialogue box currently visible. Candidates are limited "
            "to the current explicit sequence path.",
            labels,
            0,
            False,
        )
        if not accepted:
            return False
        return self.controller.select_expected_live_sequence_event(
            dict(options)[selected]
        )

    def set_sequence_status(self, status):
        self.dashboard.set_sequence_status(status)
        self.compact_controller.set_sequence_status(status)
        manual = is_live_sequence_audio_mode(getattr(status, "mode", "off"))
        candidate_count = int(getattr(status, "expected_candidate_count", 0))
        self.sequence_expected_action.setVisible(manual)
        controls_available = (
            self._controller_ready
            and not self._controller_busy
            and not self._shutting_down
        )
        self.sequence_expected_action.setEnabled(
            candidate_count > 0 and controls_available
        )
        self.sequence_expected_action.setText(
            "Use expected next line"
            if candidate_count == 1
            else (
                f"Choose among {candidate_count} expected lines..."
                if candidate_count > 1
                else "No expected next line"
            )
        )

    def _toggle_controller_live(self):
        running = self.controller.toggle_live()
        self.signals.live_changed.emit(running)
        return running

    def _start_live_with_preflight(
        self,
        *,
        narrator_approval=None,
        allow_scope_bootstrap=True,
    ):
        unresolved = getattr(self.controller, "unresolved_live_speakers", None)
        result = unresolved() if callable(unresolved) else ()
        if result is None:
            self.pending_live_voice_preflight_speakers = ()
            self.signals.live_changed.emit(False)
            if allow_scope_bootstrap:
                self._identify_live_scope_then_start()
            else:
                self.set_status(
                    "Live reading could not start: the current story line was "
                    "not identified"
                )
            return False
        speakers = tuple(result)
        if narrator_approval is not None:
            approved = tuple(narrator_approval)
            if speakers != approved:
                self.pending_live_voice_preflight_speakers = speakers
                if speakers:
                    self._show_live_voice_preflight(speakers)
                else:
                    self.set_status("Voice preflight changed; start live reading again")
                self.signals.live_changed.emit(False)
                return False
            self.controller.approve_live_narrator_fallbacks(approved)
            return self._toggle_controller_live()
        if speakers:
            self.pending_live_voice_preflight_speakers = speakers
            self._show_live_voice_preflight(speakers)
            self.signals.live_changed.emit(False)
            return False
        return self._toggle_controller_live()

    def _identify_live_scope_then_start(self):
        if self.live_scope_runner.active or self._live_scope_generation is not None:
            self.set_status("Identifying the current story chapter...")
            return False
        identify = getattr(self.controller, "identify_live_scope", None)
        if not callable(identify):
            self.set_status(
                "Live reading could not start: automatic story identification "
                "is unavailable"
            )
            return False
        self._live_scope_generation = self._lifecycle_generation
        self.set_status("Identifying the current story chapter...")
        self.live_scope_runner.start(identify)
        return False

    def _live_scope_finished(self, identified, error):
        generation = self._live_scope_generation
        self._live_scope_generation = None
        if not self._lifecycle_is_current(generation):
            return
        if error is not None:
            self.show_error(f"Unable to identify the current story chapter: {error}")
            self.signals.live_changed.emit(False)
            return
        if identified is not True:
            failure = getattr(
                self.controller,
                "live_scope_identification_failure",
                None,
            )
            if failure == "no-dialog-text":
                message = (
                    "Live reading could not start: no dialog text was recognized "
                    "in the selected game window"
                )
            elif failure == "story-line-no-match":
                message = (
                    "Live reading could not start: the visible text did not match "
                    "one unambiguous line in the configured story"
                )
            else:
                message = (
                    "Live reading could not start: keep a complete dialog line "
                    "visible and try again"
                )
            self.set_status(message)
            self.signals.live_changed.emit(False)
            return
        self._start_live_with_preflight(allow_scope_bootstrap=False)

    def _show_live_voice_preflight(self, speakers):
        if self.live_voice_preflight_prompt is not None:
            self.live_voice_preflight_prompt.setProperty(
                "vntts_live_voice_preflight_handled",
                True,
            )
            self.live_voice_preflight_prompt.close()
        prompt = QMessageBox()
        prompt.setWindowModality(Qt.WindowModality.NonModal)
        prompt.setWindowFlag(Qt.WindowType.Tool, True)
        prompt.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        if sys.platform == "darwin":
            prompt.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        prompt.setIcon(QMessageBox.Icon.Warning)
        prompt.setWindowTitle("Choose voices before live reading")
        prompt.setText(f"{len(speakers)} named story speaker(s) need a voice decision.")
        preview = ", ".join(speakers[:8])
        if len(speakers) > 8:
            preview = f"{preview}, and {len(speakers) - 8} more"
        prompt.setInformativeText(
            f"{preview}\n\nThese upcoming named speakers have at least one line "
            "with no assigned voice and no eligible original game-audio, "
            "verified generated, omission, or compatible live-fallback route. "
            "A speaker may be "
            "listed even when some of their other lines are already covered.\n\n"
            "Assign distinct voices, explicitly approve the narrator for this "
            "live session, or cancel. Live reading will not start until you "
            "decide."
        )
        assign = prompt.addButton(
            "Assign voices...",
            QMessageBox.ButtonRole.ActionRole,
        )
        narrator = prompt.addButton(
            "Use narrator for all",
            QMessageBox.ButtonRole.AcceptRole,
        )
        cancel = prompt.addButton(
            "Cancel live reading",
            QMessageBox.ButtonRole.RejectRole,
        )
        prompt.setEscapeButton(cancel)
        self.live_voice_preflight_prompt = prompt
        self.live_voice_preflight_assign_button = assign
        self.live_voice_preflight_narrator_button = narrator
        self.live_voice_preflight_cancel_button = cancel
        prompt.buttonClicked.connect(self._live_voice_preflight_clicked)
        prompt.finished.connect(self._live_voice_preflight_finished)
        prompt.open()
        QTimer.singleShot(0, lambda: configure_floating_window(prompt))

    def _live_voice_preflight_clicked(self, button):
        prompt = self.sender()
        if not isinstance(prompt, QMessageBox):
            return
        prompt.setProperty("vntts_live_voice_preflight_handled", True)
        speakers = self.pending_live_voice_preflight_speakers
        prompt.setEnabled(False)
        self.live_voice_preflight_action_prompt = prompt
        if button is self.live_voice_preflight_assign_button:
            action = "assign"
        elif button is self.live_voice_preflight_narrator_button:
            action = "narrator"
        else:
            action = "cancel"
        # QDialogButtonBox continues native mouse-release handling after
        # emitting clicked(). On macOS, synchronously destroying the last-owned
        # non-modal dialog here can leave standardButton() with a stale pointer.
        QTimer.singleShot(
            0,
            lambda active_prompt=prompt, selected_action=action, scope=speakers: (
                self._complete_live_voice_preflight_action(
                    active_prompt,
                    selected_action,
                    scope,
                )
            ),
        )

    def _complete_live_voice_preflight_action(self, prompt, action, speakers):
        if self._shutting_down or prompt is not self.live_voice_preflight_action_prompt:
            return
        self.live_voice_preflight_action_prompt = None
        if prompt.isVisible():
            prompt.close()
        if action == "assign":
            self._review_live_voice_preflight()
        elif action == "narrator":
            self._start_live_with_preflight(narrator_approval=speakers)
        else:
            self.set_status("Live reading cancelled: character voices need a decision")

    def _review_live_voice_preflight(self):
        speakers = self.pending_live_voice_preflight_speakers
        if not speakers:
            return
        self.pending_unknown_speaker = speakers[0]
        self.open_speaker_mapping()
        self._start_live_with_preflight()

    def _live_voice_preflight_finished(self, _result):
        prompt = self.sender()
        if isinstance(prompt, QMessageBox) and not prompt.property(
            "vntts_live_voice_preflight_handled"
        ):
            self.set_status("Live reading cancelled: character voices need a decision")
        if prompt is self.live_voice_preflight_prompt:
            self.live_voice_preflight_prompt = None
            self.live_voice_preflight_assign_button = None
            self.live_voice_preflight_narrator_button = None
            self.live_voice_preflight_cancel_button = None

    def toggle_speech_pause(self):
        self.signals.speech_paused_changed.emit(self.controller.toggle_speech_pause())

    def skip_current_speech(self):
        self.controller.skip_current_speech()

    def repeat_last_speech(self):
        self.controller.repeat_last_speech()

    def clear_speech_queue(self):
        self.controller.clear_speech_queue()

    def emergency_stop(self):
        self._live_scope_generation = None
        self.live_scope_runner.cancel()
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

        self.diagnostics_refresh_generation += 1
        generation = self.diagnostics_refresh_generation
        QTimer.singleShot(
            200,
            lambda: self._capture_diagnostic_snapshot(generation),
        )

    def _capture_diagnostic_snapshot(self, generation):
        def inspect():
            try:
                snapshot = self.controller.inspect_current_dialog(notify=False)
            except Exception as error:
                self.signals.diagnostics_refresh_failed.emit(
                    generation,
                    diagnostic_error_guidance(error),
                )
            else:
                self.signals.diagnostics_refresh_finished.emit(generation, snapshot)

        Thread(target=inspect, daemon=True).start()

    def _diagnostics_refresh_is_current(self, generation):
        return bool(
            generation == self.diagnostics_refresh_generation
            and self.diagnostics_dialog is not None
            and self.diagnostics_dialog.refresh_in_flight
        )

    def _diagnostics_refresh_finished(self, generation, snapshot):
        if not self._diagnostics_refresh_is_current(generation):
            return
        self.update_diagnostics_snapshot(snapshot)

    def _diagnostics_refresh_failed(self, generation, message):
        if not self._diagnostics_refresh_is_current(generation):
            return
        self.set_diagnostics_error(message)

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

    def run_onboarding_test(self, settings):
        cancel_event = Event()
        self.onboarding_cancel_event = cancel_event
        self._onboarding_test_active = True

        def run_test():
            def cancelled():
                if not cancel_event.is_set():
                    return False
                self.signals.onboarding_test_finished.emit(
                    False, "OCR-to-speech test cancelled."
                )
                return True

            try:
                if cancelled():
                    return
                self.controller.apply_settings(settings)
                if cancelled():
                    return
                if settings.speech_backend == "coqui-xtts":
                    try:
                        self.controller.model_assets.download(
                            settings.tts_model,
                            progress=self.signals.onboarding_test_progress.emit,
                            cancel_event=cancel_event,
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
                if cancelled():
                    return
                self.last_controller_error = None
                started = self.controller.start()
                if cancelled():
                    return
                if not started:
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
                if cancelled():
                    return
                preview = " ".join(text.split())
                if len(preview) > 160:
                    preview = f"{preview[:157]}..."
                self.signals.onboarding_test_finished.emit(
                    True,
                    f"Success. Recognized {character}: {preview}",
                )
            finally:
                if cancel_event.is_set():
                    self.controller.shutdown()
                self._onboarding_test_active = False

        Thread(target=run_test, daemon=True).start()

    def cancel_onboarding_download(self):
        self.onboarding_cancel_event.set()
        self.set_status("Cancelling setup test in background...")

    def _create_settings_dialog(self):
        return SettingsDialog(self.settings)

    def _create_asset_manager_dialog(self):
        return AssetManagerDialog(self.settings)

    def _configure_macos_launch_at_login(self, enabled):
        configure_macos_launch_at_login(enabled)

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
        if self._controller_busy or self._shutting_down:
            self.set_status("Controller reconfiguration is already in progress")
            return
        dialog = GameProfilesDialog(
            self.settings,
            self.profile_store,
            self.correction_store,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        candidate = dialog.settings()
        try:
            path = candidate.save()
        except OSError as error:
            self.show_error(f"Unable to save the selected profile: {error}")
            return
        self.settings = candidate
        self.dashboard.set_configuration(self.settings)
        self.sequence_resync_action.setVisible(
            is_live_sequence_audio_mode(self.settings.live_sequence_mode)
        )
        self.sequence_expected_action.setVisible(
            is_live_sequence_audio_mode(self.settings.live_sequence_mode)
        )
        profile = self.profile_store.get(self.settings.active_profile_id)
        self._pending_profile_name = profile.name if profile is not None else None
        generation = self._begin_controller_lifecycle()
        self._profile_restart_generation = generation
        self.set_status(
            f"Applying profile {self._pending_profile_name!r} in background; "
            f"settings saved to {path}"
        )
        self.profile_restart_runner.start(
            self._restart_controller_for_profile,
            self.settings,
            generation,
        )

    def _restart_controller_for_profile(self, settings, generation):
        self.controller.shutdown()
        if not self._lifecycle_is_current(generation):
            return False
        self.controller.apply_settings(settings)
        if not self._lifecycle_is_current(generation):
            return False
        ready = self.controller.start()
        if not self._lifecycle_is_current(generation):
            self.controller.shutdown()
            return False
        return ready

    def _profile_restart_finished(self, ready, error):
        generation = self._profile_restart_generation
        self._profile_restart_generation = None
        if not self._lifecycle_is_current(generation):
            return
        self._finish_controller_lifecycle()
        profile_name = self._pending_profile_name
        self._pending_profile_name = None
        if error is not None:
            self.set_ready(False)
            self.set_status(f"Profile restart failed: {error}")
            return
        self.set_ready(ready)
        if not ready:
            self.set_status("Unable to load the selected profile")
            return
        self.signals.hotkeys_requested.emit()
        self.set_status(f"Profile {profile_name!r} selected")

    def _stop_live_then(self, continuation, status):
        if self._shutting_down:
            return False
        if self.live_stop_runner.active:
            self.set_status("Live capture is already stopping; please wait")
            return False
        reader = self.controller.live_reader
        running = self.controller.toggle_live()
        self.signals.live_changed.emit(running)
        if running:
            self.set_status("Unable to stop live capture for this action")
            return False
        self._lifecycle_generation += 1
        self._live_stop_generation = self._lifecycle_generation
        self._live_stop_continuation = continuation
        self._set_modal_launchers_enabled(False)
        self.set_status(status)
        if reader is None:
            QTimer.singleShot(0, lambda: self._live_stop_finished(True, None))
        else:
            self.live_stop_runner.start(self._wait_for_live_reader, reader)
        return True

    @staticmethod
    def _wait_for_live_reader(reader):
        reader.wait()
        return True

    def _live_stop_finished(self, _result, error):
        generation = self._live_stop_generation
        self._live_stop_generation = None
        continuation = self._live_stop_continuation
        self._live_stop_continuation = None
        if self._shutting_down or generation != self._lifecycle_generation:
            return
        self._set_modal_launchers_enabled(self._controller_ready)
        if error is not None:
            self.set_status(f"Unable to stop live capture: {error}")
            return
        if continuation is not None:
            QTimer.singleShot(0, continuation)

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

    def open_voice_previews(self):
        resume_live = bool(self.controller.is_live_running)
        if resume_live:
            self._stop_live_then(
                lambda: self._open_voice_previews_dialog(True),
                "Stopping live capture before voice preview...",
            )
            return
        self._open_voice_previews_dialog(False)

    def _open_voice_previews_dialog(self, resume_live):
        if self._shutting_down:
            return
        dialog = VoicePreviewDialog(
            self.controller.available_voice_characters(),
            self.controller.available_voice_choices(),
            self.controller.preview_voice_choice,
            self.assign_voice,
            self.controller.voice_assignment_for,
            self.clear_voice_assignment,
            force_live_handler=self.set_force_live_narrator,
            current_force_live_handler=lambda: self.settings.force_live_narrator,
            preview_stop_handler=self.controller.stop_voice_preview,
            initial_character="Narrator",
        )
        try:
            dialog.exec()
        finally:
            if (
                resume_live
                and not self._shutting_down
                and not self.controller.is_live_running
            ):
                self.toggle_live()

    def offer_speaker_mapping(self, speaker):
        self.pending_unknown_speaker = speaker
        self.speaker_mapping_action.setText(f"Manage voice for {speaker}...")
        message = (
            f"No voice is assigned to {speaker}. Speech is waiting for you to "
            "choose a voice or allow the narrator voice."
        )
        self.set_status(message)
        self.compact_controller.set_warning(f"Voice needed: {speaker}")
        self.tray.showMessage(
            "Character voice not mapped",
            message,
            QSystemTrayIcon.MessageIcon.Warning,
        )
        self._show_unknown_speaker_prompt(speaker)

    def _show_unknown_speaker_prompt(self, speaker):
        if self.unknown_speaker_prompt is not None:
            self.unknown_speaker_prompt.close()
        # A QMessageBox parented to a hidden dashboard becomes a macOS sheet.
        # Qt then exposes and dims the whole dashboard behind it, which looks
        # like a large empty window. Keep this prompt as a small independent
        # tool window instead.
        prompt = QMessageBox()
        prompt.setWindowModality(Qt.WindowModality.NonModal)
        prompt.setWindowFlag(Qt.WindowType.Tool, True)
        prompt.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        if sys.platform == "darwin":
            prompt.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        prompt.setIcon(QMessageBox.Icon.Warning)
        prompt.setWindowTitle("Character voice not mapped")
        prompt.setText(f"Choose a voice for {speaker}?")
        prompt.setInformativeText(
            "VNTTS can continue with the narrator voice, but this character will "
            "not have a distinct voice until you assign one."
        )
        choose = prompt.addButton("Choose voice...", QMessageBox.ButtonRole.ActionRole)
        continue_button = prompt.addButton(
            "Continue with narrator", QMessageBox.ButtonRole.AcceptRole
        )
        prompt.setEscapeButton(continue_button)
        prompt.setProperty("vntts_unknown_speaker", speaker)
        self.unknown_speaker_prompt = prompt
        self.unknown_speaker_choose_button = choose
        self.unknown_speaker_continue_button = continue_button
        prompt.buttonClicked.connect(self._unknown_speaker_prompt_clicked)
        prompt.finished.connect(self._unknown_speaker_prompt_finished)
        prompt.open()
        # Keep the prompt visible above a fullscreen game and exclude it from
        # screen-region OCR so the warning cannot become dialogue itself.
        QTimer.singleShot(0, lambda: configure_floating_window(prompt))

    def _unknown_speaker_prompt_clicked(self, button):
        prompt = self.sender()
        speaker = (
            prompt.property("vntts_unknown_speaker")
            if isinstance(prompt, QMessageBox)
            else self.pending_unknown_speaker
        )
        if button is self.unknown_speaker_choose_button:
            prompt.setProperty("vntts_unknown_resolved", True)
            self.unknown_speaker_mapping_in_progress = speaker
            QTimer.singleShot(0, lambda: self._open_pending_speaker_mapping(speaker))
        elif button is self.unknown_speaker_continue_button:
            prompt.setProperty("vntts_unknown_resolved", True)
            self._continue_unknown_with_narrator(speaker)

    def _unknown_speaker_prompt_finished(self, _result):
        prompt = self.sender()
        speaker = (
            prompt.property("vntts_unknown_speaker")
            if isinstance(prompt, QMessageBox)
            else self.pending_unknown_speaker
        )
        if (
            speaker
            and not prompt.property("vntts_unknown_resolved")
            and speaker != self.unknown_speaker_mapping_in_progress
        ):
            # Closing the prompt is equivalent to its escape button: never
            # leave the current dialogue silently deferred.
            self._continue_unknown_with_narrator(speaker)
        if prompt is self.unknown_speaker_prompt:
            self.unknown_speaker_prompt = None
            self.unknown_speaker_choose_button = None
            self.unknown_speaker_continue_button = None

    def _continue_unknown_with_narrator(self, speaker):
        self.controller.allow_narrator_fallback(speaker)
        if (
            self.resume_live_after_unknown_mapping
            and not self.controller.is_live_running
        ):
            self.toggle_live()
        self.resume_live_after_unknown_mapping = False

    def _open_pending_speaker_mapping(self, speaker=None):
        if self._shutting_down:
            return
        speaker = speaker or self.pending_unknown_speaker
        self.resume_live_after_unknown_mapping = bool(
            self.resume_live_after_unknown_mapping or self.controller.is_live_running
        )
        if self.controller.is_live_running:
            self._stop_live_then(
                lambda: self._open_pending_speaker_mapping(speaker),
                "Stopping live capture before speaker mapping...",
            )
            return
        self.pending_unknown_speaker = speaker
        assigned = self.open_speaker_mapping()
        self.unknown_speaker_mapping_in_progress = None
        if not assigned:
            self.pending_unknown_speaker = speaker
            self._show_unknown_speaker_prompt(speaker)
            return
        if self.resume_live_after_unknown_mapping:
            self.toggle_live()
        self.resume_live_after_unknown_mapping = False

    def open_speaker_mapping(self):
        initial_character = self.pending_unknown_speaker or "Narrator"
        dialog = VoicePreviewDialog(
            self.controller.available_voice_characters(),
            self.controller.available_voice_choices(),
            self.controller.preview_voice_choice,
            self.assign_voice,
            self.controller.voice_assignment_for,
            self.clear_voice_assignment,
            force_live_handler=self.set_force_live_narrator,
            current_force_live_handler=lambda: self.settings.force_live_narrator,
            preview_stop_handler=self.controller.stop_voice_preview,
            initial_character=initial_character,
        )
        dialog.exec()
        assigned = self.controller.voice_assignment_for(initial_character) is not None
        self.pending_unknown_speaker = None
        self.speaker_mapping_action.setText("Manage character voices...")
        return assigned

    def open_support_center(self):
        if self.support_dialog is None:
            self.support_dialog = SupportCenterDialog(self.support_log, self.dashboard)
            self.support_dialog.diagnostics_requested.connect(
                self.open_support_diagnostics
            )
            self.support_dialog.export_requested.connect(self.export_support_bundle)
            self.support_dialog.settings_folder_requested.connect(
                self.open_support_settings_folder
            )
        self.support_dialog.show()
        self.support_dialog.raise_()
        self.support_dialog.activateWindow()

    def open_support_diagnostics(self):
        try:
            self.open_diagnostics()
        except Exception as error:
            if self.support_dialog is not None:
                self.support_dialog.set_launcher_result(
                    "diagnostics", False, f"Unable to open live diagnostics: {error}"
                )
            return
        if self.support_dialog is not None:
            self.support_dialog.set_launcher_result(
                "diagnostics", True, "Live diagnostics opened in a separate window."
            )

    def open_support_settings_folder(self):
        try:
            path = self.open_settings_folder()
        except Exception as error:
            if self.support_dialog is not None:
                self.support_dialog.set_launcher_result(
                    "settings-folder",
                    False,
                    f"Unable to open the settings folder: {error}",
                )
            return
        if self.support_dialog is not None:
            self.support_dialog.set_launcher_result(
                "settings-folder", True, f"Settings folder opened: {path}"
            )

    def open_history(self):
        # Region capture has no focus probe. A modal over the calibrated region
        # makes OCR append the history list repeatedly. Stop
        # capture for the modal session, then restore the previous live state.
        resume_live = bool(self.controller.is_live_running)
        if resume_live:
            self._stop_live_then(
                lambda: self._open_history_dialog(True),
                "Stopping live capture before dialogue history...",
            )
            return
        self._open_history_dialog(False)

    def _open_history_dialog(self, resume_live):
        if self._shutting_down:
            return
        dialog = DialogueHistoryDialog(
            self.controller.history,
            self.controller.replay_dialog,
            stop_handler=self.controller.stop_voice_preview,
        )
        try:
            dialog.exec()
        finally:
            if (
                resume_live
                and not self._shutting_down
                and not self.controller.is_live_running
            ):
                self.toggle_live()

    def export_support_bundle(self):
        path, _selected_filter = QFileDialog.getSaveFileName(
            None,
            "Export support bundle",
            "vntts-support.zip",
            "ZIP archives (*.zip)",
        )
        if not path:
            if self.support_dialog is not None:
                self.support_dialog.set_export_result(
                    None,
                    "Support report export cancelled.",
                )
            return
        self.set_status("Creating support bundle...")

        def export():
            try:
                output = SupportBundleBuilder(
                    self.settings,
                    self.support_log,
                    diagnostic=self.controller.get_latest_diagnostic(),
                    generation_timelines=self.generation_timelines,
                ).build(path)
            except Exception as error:
                self.signals.support_export_finished.emit(False, str(error))
            else:
                self.signals.support_export_finished.emit(True, str(output))

        Thread(target=export, daemon=True).start()

    def support_export_finished(self, successful, message):
        if self.support_dialog is not None:
            self.support_dialog.set_export_result(successful, message)
        if successful:
            self.set_status(f"Support bundle saved to {message}")
        else:
            self.show_error(f"Support bundle export failed: {message}")

    def open_macos_permissions(self):
        MacOSPermissionsDialog().exec()

    def open_settings_folder(self):
        path = get_settings_path().parent
        path.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            raise OSError("the operating system refused the folder-open request")
        return path

    def set_status(self, message):
        self.support_log.add("status", message)
        self.status_action.setText(message)
        self.tray.setToolTip(f"{application_name}\n{message}")
        self.dashboard.set_status(message)
        self.compact_controller.set_status(message)

    def record_audio_route(self, trace):
        self.support_log.add(
            "audio-route",
            trace.message(),
            **trace.support_fields(),
        )

    def set_dialog(self, character, text):
        if not text:
            self.dialog_action.setText("No dialog detected")
            self.dashboard.set_dialogue(character, "")
            self.compact_controller.set_dialogue(character, "")
            return
        self.dialog_action.setText(f"{character}: {text}")
        self.dashboard.set_dialogue(character, text)
        self.compact_controller.set_dialogue(character, text)

    def _controller_actions(self):
        return (
            self.read_action,
            self.live_action,
            self.sequence_resync_action,
            self.pause_action,
            self.skip_action,
            self.repeat_action,
            self.clear_queue_action,
            self.emergency_stop_action,
            self.voice_preview_action,
        )

    def _controller_configuration_actions(self):
        return (
            self.calibrate_action,
            self.diagnostics_action,
            self.settings_action,
            self.profiles_action,
            self.corrections_action,
            self.ocr_review_action,
            self.setup_action,
            self.assets_action,
            self.speaker_mapping_action,
            self.history_action,
        )

    def _apply_controller_action_state(self):
        enabled = (
            self._controller_ready
            and not self._controller_busy
            and not self._shutting_down
        )
        for action in self._controller_actions():
            action.setEnabled(enabled)
        configuration_enabled = not self._controller_busy and not self._shutting_down
        for action in self._controller_configuration_actions():
            action.setEnabled(configuration_enabled)
        self.dashboard.set_ready(enabled)
        self.compact_controller.set_ready(enabled)
        current_sequence_status = getattr(
            self.controller,
            "get_live_sequence_status",
            None,
        )
        if callable(current_sequence_status):
            sequence_status = current_sequence_status()
            if getattr(sequence_status, "mode", None) in {
                "off",
                "shadow",
                "audio-manual",
                "audio-auto",
            }:
                self.set_sequence_status(sequence_status)

    def _set_modal_launchers_enabled(self, enabled):
        available = (
            bool(enabled) and not self._controller_busy and not self._shutting_down
        )
        self.voice_preview_action.setEnabled(available and self._controller_ready)
        self.speaker_mapping_action.setEnabled(available)
        self.history_action.setEnabled(available)

    def _begin_controller_lifecycle(self):
        self._live_scope_generation = None
        self.live_scope_runner.cancel()
        self._lifecycle_generation += 1
        self._controller_busy = True
        self._apply_controller_action_state()
        return self._lifecycle_generation

    def _finish_controller_lifecycle(self):
        self._controller_busy = False
        self._apply_controller_action_state()

    def _lifecycle_is_current(self, generation):
        return (
            isinstance(generation, int)
            and generation == self._lifecycle_generation
            and not self._shutting_down
        )

    def set_ready(self, ready):
        self._controller_ready = bool(ready)
        self._apply_controller_action_state()
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
        if self._shutting_down:
            return
        self._shutting_down = True
        self._lifecycle_generation += 1
        self._controller_busy = True
        self._live_stop_continuation = None
        self._live_stop_generation = None
        self.live_stop_runner.cancel()
        self._live_scope_generation = None
        self.live_scope_runner.cancel()
        initial_shutdown_owned = self.initial_start_runner.active
        self.initial_start_runner.cancel()
        profile_shutdown_owned = self.profile_restart_runner.active
        self.profile_restart_runner.cancel()
        self.configuration_runner.cancel()
        onboarding_shutdown_owned = self._onboarding_test_active
        self.onboarding_cancel_event.set()
        self._apply_controller_action_state()
        self.resume_live_after_unknown_mapping = False
        self.live_voice_preflight_action_prompt = None
        if self.live_voice_preflight_prompt is not None:
            self.live_voice_preflight_prompt.setProperty(
                "vntts_live_voice_preflight_handled",
                True,
            )
            self.live_voice_preflight_prompt.close()
            self.live_voice_preflight_prompt = None
            self.live_voice_preflight_assign_button = None
            self.live_voice_preflight_narrator_button = None
            self.live_voice_preflight_cancel_button = None
        if self.unknown_speaker_prompt is not None:
            self.unknown_speaker_prompt.close()
            self.unknown_speaker_prompt = None
            self.unknown_speaker_choose_button = None
            self.unknown_speaker_continue_button = None
        self.dashboard.keep_running_on_close = False
        self.dashboard._quitting = True
        self.dashboard.close()
        self.compact_controller.close()
        if self.diagnostics_dialog is not None:
            self.diagnostics_dialog.close()
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
            self.hotkey_listener = None
        if (
            not initial_shutdown_owned
            and not profile_shutdown_owned
            and not onboarding_shutdown_owned
        ):
            self.controller.shutdown()


def main(argv=None):
    freeze_support()
    parser = argparse.ArgumentParser(
        prog="vntts-app",
        description="Run the VNTTS desktop application.",
    )
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

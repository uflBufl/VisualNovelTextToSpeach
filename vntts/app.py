import argparse
import sys
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from json import JSONDecodeError
from multiprocessing import freeze_support
from pathlib import Path
from threading import Event, Lock, Thread
from time import perf_counter

# Frozen multiprocessing children must dispatch before importing the desktop UI.
if getattr(sys, "frozen", False):
    freeze_support()

from PIL import Image
from pynput import keyboard
from PySide6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QRectF,
    QSignalBlocker,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from vntts.action_icons import set_action_icon
from vntts.asset_ui import AssetManagerDialog
from vntts.async_ui import LatestTaskRunner
from vntts.auto_advance_policy import (
    auto_advance_control_state,
    guard_auto_advance_settings,
)
from vntts.calibration import (
    CaptureWindowVisibility,
    DialogRegionOverlay,
    capture_calibration_background,
    show_calibration_overlay,
)
from vntts.cleanup import attempt_cleanup, cleanup_on_exit
from vntts.configuration_apply import ConfigurationApplyMixin
from vntts.controller import AppController, LiveSequenceStatus
from vntts.dashboard_ui import (
    CompactController,
    ControlDashboard,
    RuntimeControlState,
    configure_floating_window,
)
from vntts.diagnostics import (
    DiagnosticSnapshot,
    diagnostic_error_guidance,
    diagnostic_remediation,
    macos_permission_warnings,
)
from vntts.diagnostics_ui import DiagnosticsDialog
from vntts.dialog_capture import format_runtime_error
from vntts.durable_settings import DurableSettingsMixin
from vntts.game_narrator_ui import GameNarratorDialog
from vntts.game_pack import GamePackError, apply_game_pack, import_game_pack
from vntts.generated_audio import AudioRouteTrace
from vntts.history_ui import DialogueHistoryDialog
from vntts.hotkey_ui import HotkeyRecorder, hotkey_recording_errors
from vntts.hotkeys import macos_hotkey_limitation, validate_hotkey_assignments
from vntts.live import LiveDialogReader
from vntts.macos import (
    configure_macos_launch_at_login,
    get_macos_permission_status,
)
from vntts.macos_ui import MacOSPermissionsDialog
from vntts.model_assets import ModelDownloadCancelled
from vntts.moss_runtime import RetainedMossRuntime
from vntts.ocr import (
    DialogRegion,
    get_dialog_region_file,
    save_dialog_region,
)
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.ocr_corrections_ui import OCRCorrectionsDialog
from vntts.ocr_review_ui import OCRReviewDialog
from vntts.onboarding import OnboardingDiagnostics
from vntts.onboarding_ui import OnboardingWizard
from vntts.package_self_test import run_package_self_test
from vntts.player_session import PlayerSessionOwner
from vntts.pregeneration_activation import (
    OfflinePackActivationResult,
    OfflinePackActivator,
)
from vntts.pregeneration_audition import VoiceAuditionPreviewService
from vntts.pregeneration_audition_queue import VoiceAuditionCoordinator
from vntts.pregeneration_generation import OfflineGenerationWorker
from vntts.pregeneration_pack import OfflinePackResult
from vntts.pregeneration_setup import GameContent, PregenerationJob
from vntts.pregeneration_ui import OfflineAudioPreparationDialog
from vntts.pregeneration_voices import VoicePlan, resolve_pregeneration_settings
from vntts.profiles import GameProfileStore
from vntts.profiles_ui import GameProfilesDialog
from vntts.readiness_ui import ReadinessDialog
from vntts.reading_diagnostics import (
    ReadingDiagnosticSession,
    SavedDiagnosticSession,
    latest_finished_diagnostic_session,
    record_diagnostic_event,
    selected_recording_target,
)
from vntts.release_backends import (
    packaged_speech_backend_available,
    speech_backend_options,
)
from vntts.release_smoke_test import (
    configure_release_smoke_arguments,
    default_smoke_test_model,
    run_release_smoke_test,
)
from vntts.runtime_config import get_hotkey, get_live_hotkey
from vntts.runtime_paths import configure_bundled_dependencies
from vntts.rust_audio import check_audio_runtime
from vntts.settings import (
    AppSettings,
    get_local_data_directory,
    get_settings_path,
    is_live_sequence_audio_mode,
    load_app_settings,
)
from vntts.speech_backend import default_moss_tts_model
from vntts.speech_presentation import speech_runtime_label
from vntts.speech_worker import RetainedWorkerRuntime
from vntts.support import (
    GenerationTimelineLog,
    RuntimeSupportLog,
    SupportBundleBuilder,
    SupportDocument,
    collect_active_content_identity,
    configure_audio_lifecycle_log,
    configure_game_import_log,
    configure_generation_stall_log,
    configure_native_speech_log,
    configure_performance_log,
    configure_pregeneration_support,
    preserve_previous_session,
    record_background_operation,
)
from vntts.support_ui import SupportCenterDialog
from vntts.ui_text import add_composite_form_row as _add_composite_form_row
from vntts.ui_text import make_text_copyable
from vntts.versioned_json import file_revision
from vntts.voice_library import VoiceLibrary, VoiceLibraryError
from vntts.voices import (
    application_voice_library,
    find_default_voice_manifest,
    normalize_character_name,
)
from vntts.window_capture import (
    WindowCaptureError,
    WindowGeometry,
    enable_windows_dpi_awareness,
    list_windows,
)

application_name = "Visual Novel Text to Speech"
default_xtts_model = "tts_models/multilingual/multi-dataset/xtts_v2"
ValidationError = tuple[int, QWidget, str]


@dataclass(frozen=True)
class _ReadingPackAdmission:
    identity: tuple[str | None, ...]
    files: tuple[Path, ...]
    stamps: tuple[tuple[int, ...], ...] | None
    playable: bool

    @staticmethod
    def file_stamps(files: tuple[Path, ...]) -> tuple[tuple[int, ...], ...] | None:
        try:
            return tuple(
                (
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
                for path in files
                for stat in (path.stat(follow_symlinks=False),)
            )
        except OSError:
            return None


@dataclass
class _OnboardingCaptureRequest:
    generation: int
    cancelled: Event
    ready: Event = dataclass_field(default_factory=Event)
    finished: Event = dataclass_field(default_factory=Event)
    restored: Event = dataclass_field(default_factory=Event)
    visibility: CaptureWindowVisibility | None = None
    wizard: QWidget | None = None


def create_application_icon(style: QStyle, *, platform: str | None = None) -> QIcon:
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
    onboarding_capture_requested = Signal(object)
    onboarding_capture_released = Signal(object)
    diagnostics_changed = Signal(object)
    sequence_status_changed = Signal(object)
    diagnostics_failed = Signal(str)
    hotkeys_requested = Signal()
    read_requested = Signal()
    live_requested = Signal()
    unknown_speaker = Signal(str)


def build_unknown_speaker_prompt(
    speaker: str,
) -> tuple[QMessageBox, QPushButton, QPushButton, QPushButton]:
    """Build the shared unknown-speaker recovery prompt."""
    prompt = QMessageBox()
    prompt.setWindowModality(Qt.WindowModality.NonModal)
    prompt.setWindowFlag(Qt.WindowType.Tool, True)
    prompt.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    if sys.platform == "darwin":
        prompt.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
    prompt.setIcon(QMessageBox.Icon.Warning)
    prompt.setWindowTitle("Voice needed")
    prompt.setText(f"{speaker} has no assigned voice.")
    prompt.setInformativeText(
        "Live reading is paused for voice editing. Choose and save a voice, "
        f"or resume reading with the narrator for {speaker} this session."
    )
    choose = prompt.addButton(
        "Choose and save voice...", QMessageBox.ButtonRole.AcceptRole
    )
    continue_button = prompt.addButton(
        "Resume with narrator",
        QMessageBox.ButtonRole.ActionRole,
    )
    cancel_button = prompt.addButton(
        "Keep reading paused",
        QMessageBox.ButtonRole.RejectRole,
    )
    assert choose is not None
    assert continue_button is not None
    assert cancel_button is not None
    prompt.setDefaultButton(choose)
    prompt.setEscapeButton(cancel_button)
    prompt.setProperty("vntts_unknown_speaker", speaker)
    return prompt, choose, continue_button, cancel_button


def build_story_match_recovery_prompt(
    message: str,
    parent: QWidget | None = None,
    *,
    can_read_visible_dialogue: bool = True,
) -> tuple[QMessageBox, QPushButton, QPushButton, QPushButton]:
    """Offer the existing recovery routes when a live story line cannot match."""
    prompt = QMessageBox(parent)
    prompt.setIcon(QMessageBox.Icon.Warning)
    prompt.setWindowTitle("Story line not matched")
    prompt.setText(message)
    prompt.setInformativeText(
        "You can start reading the dialogue visible on screen now. Prepared "
        "recordings remain available if a later line matches."
        if can_read_visible_dialogue
        else "This reading mode needs a matching story line. Keep a complete "
        "dialogue line visible and try again, or open Stories to check the selected story."
    )
    continue_button = prompt.addButton(
        "Start live reading" if can_read_visible_dialogue else "Try matching again",
        QMessageBox.ButtonRole.AcceptRole,
    )
    stories_button = prompt.addButton(
        "Open Stories...", QMessageBox.ButtonRole.ActionRole
    )
    cancel_button = prompt.addButton("Stay stopped", QMessageBox.ButtonRole.RejectRole)
    assert continue_button is not None
    assert stories_button is not None
    assert cancel_button is not None
    prompt.setDefaultButton(continue_button)
    prompt.setEscapeButton(cancel_button)
    return prompt, continue_button, stories_button, cancel_button


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        *,
        voice_library: VoiceLibrary | None = None,
    ) -> None:
        super().__init__(parent)
        self.voice_library = voice_library or application_voice_library()
        self._voice_binding_rollback = self.voice_library.binding_rollback()
        self._initialize_settings(settings)
        window_layout = self._build_capture_controls(settings)
        self._build_engine_controls(settings)
        self._build_speech_routing_controls(settings)
        self._build_playback_controls(settings)
        self._build_application_controls(settings)
        (
            screenshot_layout,
            diagnostics_layout,
            narrator_reference_layout,
            game_pack_layout,
            voice_manifest_layout,
            story_index_layout,
            live_sequence_plan_layout,
            live_speaker_corpus_layout,
            generated_audio_manifest_layout,
        ) = self._build_path_selectors()
        shortcuts_form = self._build_shortcuts_form()
        capture_form = self._build_capture_form(
            window_layout, screenshot_layout, diagnostics_layout
        )
        speech_form = self._build_speech_form(
            narrator_reference_layout,
            game_pack_layout,
            voice_manifest_layout,
            story_index_layout,
            live_sequence_plan_layout,
            live_speaker_corpus_layout,
            generated_audio_manifest_layout,
        )
        playback_form = self._build_playback_form(
            capture_form,
            speech_form,
            screenshot_layout,
            diagnostics_layout,
            game_pack_layout,
            voice_manifest_layout,
            story_index_layout,
            live_sequence_plan_layout,
            live_speaker_corpus_layout,
            generated_audio_manifest_layout,
        )
        application_form = self._build_application_form()
        content_layout = self._build_settings_content(
            shortcuts_form,
            capture_form,
            speech_form,
            playback_form,
            application_form,
        )
        self._build_dialog_layout(content_layout)
        self._connect_controls()

    def _initialize_settings(self, settings: AppSettings) -> None:
        self.original_settings = settings
        self._game_pack_validation: (
            tuple[tuple[str | None, bool, str | None, str, str | None], AppSettings]
            | None
        ) = None
        self._game_pack_error: str | None = None
        self._pending_pack_settings: AppSettings | None = None
        self.game_pack_runner = LatestTaskRunner(self)
        self.game_pack_runner.finished.connect(self._game_pack_checked)
        self.setWindowTitle(f"{application_name} settings")

    def _build_capture_controls(self, settings: AppSettings) -> QHBoxLayout:
        self.read_hotkey = HotkeyRecorder(settings.read_hotkey)
        self.live_hotkey = HotkeyRecorder(settings.live_hotkey)
        self.hotkey_recorders = (
            self.read_hotkey,
            self.live_hotkey,
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
        self.capture_mode.addItem("Screen region (no auto advance)", "screen")
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
        refresh_windows_button = QPushButton("Refresh")
        set_action_icon(refresh_windows_button, "refresh-cw")
        refresh_windows_button.setToolTip("Refresh game windows")
        refresh_windows_button.setAccessibleName("Refresh game windows")
        refresh_windows_button.setAccessibleDescription(
            "Reload the list of capturable game windows"
        )
        refresh_windows_button.clicked.connect(self.refresh_windows)
        window_layout = QHBoxLayout()
        window_layout.addWidget(self.game_window, 1)
        refresh_windows_button.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        window_layout.addWidget(refresh_windows_button)
        self.refresh_windows_button = refresh_windows_button
        return window_layout

    def _build_engine_controls(self, settings: AppSettings) -> None:
        self.tts_model = QLineEdit(settings.tts_model or "")
        self.offline_tts_model = QLineEdit(settings.offline_tts_model or "")
        self.offline_tts_model.setAccessibleName("Offline preparation model")
        self.speech_backend = QComboBox()
        self.offline_speech_backend = QComboBox()
        self.offline_speech_backend.setAccessibleName("Offline preparation engine")
        for choice, current in (
            (self.speech_backend, settings.speech_backend),
            (self.offline_speech_backend, settings.offline_speech_backend),
        ):
            for label, backend, available in speech_backend_options(current):
                choice.addItem(label, backend)
                if not available:
                    model = choice.model()
                    if isinstance(model, QStandardItemModel):
                        model.item(choice.count() - 1).setEnabled(False)
        self.speech_backend.setCurrentIndex(
            max(0, self.speech_backend.findData(settings.speech_backend))
        )
        self.offline_speech_backend.setCurrentIndex(
            max(
                0, self.offline_speech_backend.findData(settings.offline_speech_backend)
            )
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

    def _build_speech_routing_controls(self, settings: AppSettings) -> None:
        self.ocr_language = QLineEdit(settings.ocr_language)
        self.tts_language = QLineEdit(settings.tts_language or "")
        self.narrator_reference = QLineEdit(settings.tts_speaker_wav or "")
        self.narrator_reference.setAccessibleName("Narrator reference")
        self.narrator_reference.setAccessibleDescription(
            "Audio reference used for the narrator voice"
        )
        default_voice_manifest = (
            None if settings.voice_manifest else find_default_voice_manifest()
        )
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
            "Canonical audio + guarded auto advance (recommended)", "audio-auto"
        )
        self.live_sequence_mode.addItem(
            "Canonical audio routing (manual recovery)", "audio-manual"
        )
        self.live_sequence_mode.setCurrentIndex(
            max(0, self.live_sequence_mode.findData(settings.live_sequence_mode))
        )
        self.live_speaker_corpus = QLineEdit(settings.live_speaker_corpus or "")
        self.generated_audio_manifest = QLineEdit(
            settings.generated_audio_manifest or ""
        )
        self.narrator_speaker = QLineEdit(settings.narrator_speaker or "")

    def _build_playback_controls(self, settings: AppSettings) -> None:
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
        self.speech_rate.setToolTip(
            "Speed changes are supported by Coqui XTTS. Prepared audio requires 100%."
        )
        self.speech_rate.setAccessibleDescription(self.speech_rate.toolTip())
        self.reset_speech_rate = QPushButton("Reset to 100%")
        set_action_icon(self.reset_speech_rate, "refresh-cw")
        self.reset_speech_rate.setAutoDefault(False)
        self.reset_speech_rate.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.reset_speech_rate.setToolTip(
            "Use the normal speed required for prepared audio"
        )
        self.reset_speech_rate.setEnabled(self.speech_rate.value() != 100)
        self.reset_speech_rate.clicked.connect(lambda: self.speech_rate.setValue(100))
        self.speech_rate_notice = QLabel()
        self.speech_rate_notice.setWordWrap(True)
        self.speech_rate.valueChanged.connect(self.update_speech_rate_controls)
        self.auto_advance = QCheckBox("Advance after the spoken dialogue finishes")
        self.auto_advance.setChecked(settings.auto_advance_enabled)
        self.auto_advance_reason = QLabel()
        self.auto_advance_reason.setWordWrap(True)
        self.auto_advance_reason.setAccessibleName("Auto advance availability")
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

    def _build_application_controls(self, settings: AppSettings) -> None:
        self.warm_up_voices = QCheckBox("Warm up voices before reading")
        self.warm_up_voices.setChecked(settings.warm_up_voices)
        self.launch_at_login = QCheckBox("Launch at sign-in (macOS)")
        self.launch_at_login.setChecked(settings.launch_at_login)
        self.launch_at_login.setEnabled(sys.platform == "darwin")
        self.keep_running_on_close = QCheckBox("Keep reading when Full controls closes")
        self.keep_running_on_close.setChecked(settings.keep_running_on_close)
        self.xtts_terms = QCheckBox("I agree to the non-commercial CPML terms")
        self.xtts_terms.setChecked(settings.xtts_terms_accepted)
        self.pocket_gated_model = QCheckBox("I accepted Pocket's terms")
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

    def _build_path_selectors(
        self,
    ) -> tuple[QHBoxLayout, ...]:
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
            restart_description = "Changes to this setting reload speech when saved."
            field.setAccessibleDescription(f"{current} {restart_description}".strip())
        return (
            screenshot_layout,
            diagnostics_layout,
            narrator_reference_layout,
            game_pack_layout,
            voice_manifest_layout,
            story_index_layout,
            live_sequence_plan_layout,
            live_speaker_corpus_layout,
            generated_audio_manifest_layout,
        )

    def _build_shortcuts_form(self) -> QFormLayout:
        shortcuts_form = QFormLayout()
        if sys.platform == "darwin":
            shortcuts_form.addRow(self.macos_hotkey_notice)
        shortcuts_form.addRow("Read current dialogue hotkey", self.read_hotkey)
        shortcuts_form.addRow("Start / stop reading hotkey", self.live_hotkey)
        return shortcuts_form

    def _build_capture_form(
        self,
        window_layout: QHBoxLayout,
        screenshot_layout: QHBoxLayout,
        diagnostics_layout: QHBoxLayout,
    ) -> QFormLayout:
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
        return capture_form

    def _build_speech_form(
        self,
        narrator_reference_layout: QHBoxLayout,
        game_pack_layout: QHBoxLayout,
        voice_manifest_layout: QHBoxLayout,
        story_index_layout: QHBoxLayout,
        live_sequence_plan_layout: QHBoxLayout,
        live_speaker_corpus_layout: QHBoxLayout,
        generated_audio_manifest_layout: QHBoxLayout,
    ) -> QFormLayout:
        speech_form = QFormLayout()
        self.speech_form = speech_form
        speech_form.addRow("Live reading engine", self.speech_backend)
        speech_form.addRow("Offline preparation engine", self.offline_speech_backend)
        speech_form.addRow("Offline preparation model", self.offline_tts_model)
        self.narrator_voice = QLabel()
        self.narrator_voice.setWordWrap(True)
        self.narrator_voice.setAccessibleName("Selected narrator voice")
        self.choose_narrator_button = QPushButton("Narrator...")
        set_action_icon(self.choose_narrator_button, "audio-lines")
        self.choose_narrator_button.setToolTip("Choose narrator voice")
        self.choose_narrator_button.setAccessibleName("Choose narrator voice")
        self.choose_narrator_button.setAccessibleDescription(
            "Listen to game or built-in voices and save a narrator"
        )
        self.choose_narrator_button.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.choose_narrator_button.clicked.connect(self.choose_narrator)
        speech_form.addRow("Narrator voice", self.narrator_voice)
        speech_form.addRow("", self.choose_narrator_button)
        self.advanced_narrator = QCheckBox("Advanced: audio file")
        speech_form.addRow(self.advanced_narrator)
        speech_form.addRow("Audio source policy", self.audio_source_policy)
        speech_form.addRow("Live reading model", self.tts_model)
        speech_form.addRow("TTS language", self.tts_language)
        _add_composite_form_row(
            speech_form,
            "Narrator reference",
            self.narrator_reference,
            narrator_reference_layout,
        )
        self.advanced_narrator.toggled.connect(
            lambda visible: speech_form.setRowVisible(
                narrator_reference_layout, visible
            )
        )
        speech_form.setRowVisible(narrator_reference_layout, False)
        self.narrator_reference_layout = narrator_reference_layout
        _add_composite_form_row(
            speech_form, "Game pack", self.game_pack, game_pack_layout
        )
        _add_composite_form_row(
            speech_form,
            "Voice manifest",
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
        speech_form.addRow("Narrator speaker", self.narrator_speaker)
        speech_form.addRow("Voice profile", self.tts_profile)
        speech_form.addRow("XTTS license", self.xtts_terms)
        speech_form.addRow("", self.pocket_gated_model)
        speech_form.addRow("", self.pocket_terms_label)
        return speech_form

    def _build_playback_form(
        self,
        capture_form: QFormLayout,
        speech_form: QFormLayout,
        screenshot_layout: QHBoxLayout,
        diagnostics_layout: QHBoxLayout,
        game_pack_layout: QHBoxLayout,
        voice_manifest_layout: QHBoxLayout,
        story_index_layout: QHBoxLayout,
        live_sequence_plan_layout: QHBoxLayout,
        live_speaker_corpus_layout: QHBoxLayout,
        generated_audio_manifest_layout: QHBoxLayout,
    ) -> QFormLayout:
        playback_form = QFormLayout()
        playback_form.addRow("Output volume", self.output_volume)
        speed_layout = QHBoxLayout()
        speed_layout.addWidget(self.speech_rate, 1)
        speed_layout.addWidget(self.reset_speech_rate)
        _add_composite_form_row(
            playback_form, "Speaking speed", self.speech_rate, speed_layout
        )
        playback_form.addRow(self.speech_rate_notice)
        playback_form.addRow("Auto advance", self.auto_advance)
        playback_form.addRow("", self.auto_advance_reason)
        playback_form.addRow("Sequence-first rollout", self.live_sequence_mode)
        playback_form.addRow("Speaker announcements", self.speaker_announcement_mode)
        playback_form.addRow("Advance key", self.auto_advance_key)
        playback_form.addRow("Advance delay", self.auto_advance_delay)

        self.advanced_settings = QCheckBox("Show advanced settings")
        self._advanced_rows = (
            (capture_form, screenshot_layout),
            (capture_form, self.ocr_minimum_confidence),
            (capture_form, self.ocr_language),
            (capture_form, self.retain_uncertain_frames),
            (capture_form, diagnostics_layout),
            (speech_form, self.tts_language),
            (speech_form, self.advanced_narrator),
            (speech_form, game_pack_layout),
            (speech_form, voice_manifest_layout),
            (speech_form, story_index_layout),
            (speech_form, live_sequence_plan_layout),
            (speech_form, live_speaker_corpus_layout),
            (speech_form, generated_audio_manifest_layout),
            (speech_form, self.tts_profile),
            (playback_form, self.live_sequence_mode),
            (playback_form, self.auto_advance_key),
            (playback_form, self.auto_advance_delay),
        )
        self.advanced_settings.toggled.connect(self._set_advanced_settings)
        return playback_form

    def _build_application_form(self) -> QFormLayout:
        application_form = QFormLayout()
        application_form.addRow(self.warm_up_voices)
        application_form.addRow(self.launch_at_login)
        application_form.addRow(self.keep_running_on_close)
        return application_form

    def _build_settings_content(
        self,
        shortcuts_form: QFormLayout,
        capture_form: QFormLayout,
        speech_form: QFormLayout,
        playback_form: QFormLayout,
        application_form: QFormLayout,
    ) -> QVBoxLayout:
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
        return settings_content_layout

    def _build_dialog_layout(self, settings_content_layout: QVBoxLayout) -> None:
        self.section_navigation = QComboBox()
        self.section_navigation.addItems(
            [region.title() for region in self.settings_regions]
        )
        self.section_navigation.setAccessibleName("Settings section")
        self.section_navigation.setAccessibleDescription(
            "Choose the settings category to display"
        )
        section_label = QLabel("Section")
        section_label.setBuddy(self.section_navigation)
        section_navigation_layout = QHBoxLayout()
        section_navigation_layout.addWidget(section_label)
        section_navigation_layout.addWidget(self.section_navigation, 1)

        self.validation_summary = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.validation_summary.setFrameShape(QFrame.Shape.NoFrame)
        self.validation_summary.viewport().setAutoFillBackground(False)
        self.validation_summary.setFixedHeight(
            self.validation_summary.fontMetrics().lineSpacing() * 3 + 12
        )
        self.validation_summary.installEventFilter(self)
        self.validation_summary.setAccessibleName("Settings validation summary")
        self.restart_note = QLabel(
            "Speech engine and voice changes reload speech when saved."
        )
        self.restart_note.setWordWrap(True)
        settings_content_layout.insertWidget(
            settings_content_layout.count() - 1, self.restart_note
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        set_action_icon(self.save_button, "save")
        set_action_icon(buttons.button(QDialogButtonBox.StandardButton.Cancel), "x")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addLayout(section_navigation_layout)
        layout.addWidget(self.advanced_settings)
        layout.addWidget(self.validation_summary)
        layout.addWidget(self.settings_scroll, 1)
        layout.addWidget(buttons)
        self.setStyleSheet(
            "QCheckBox::indicator:unchecked:enabled { border: 1px solid #8b8b8b; }"
            "QCheckBox::indicator:unchecked:disabled { border: 1px solid #777777; }"
            "QCheckBox::indicator:unchecked:focus { border: 2px solid #2ab044; }"
        )
        self._resize_for_available_screen()

    def _connect_controls(self) -> None:
        self.capture_mode.currentIndexChanged.connect(self.update_capture_controls)
        self.tts_model.textChanged.connect(self.update_terms_control)
        self.speech_backend.currentIndexChanged.connect(
            self.update_speech_backend_controls
        )
        self.offline_speech_backend.currentIndexChanged.connect(
            self._offline_backend_changed
        )
        self.retain_uncertain_frames.toggled.connect(
            self.update_ocr_diagnostics_controls
        )
        self.auto_advance.toggled.connect(self.update_auto_advance_controls)
        self.live_sequence_mode.currentIndexChanged.connect(
            self.update_auto_advance_controls
        )
        self.section_navigation.currentIndexChanged.connect(self.show_settings_section)
        self._connect_validation_updates()
        self.update_capture_controls()
        self.update_speech_backend_controls()
        self.update_ocr_diagnostics_controls()
        self.update_auto_advance_controls()
        self.update_validation_summary()
        self.section_navigation.setCurrentIndex(1)
        self._set_advanced_settings(False)
        self.settings_scroll.viewport().installEventFilter(self)
        make_text_copyable(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.validation_summary
            and event.type() == QEvent.Type.FontChange
        ):
            self.validation_summary.setFixedHeight(
                self.validation_summary.fontMetrics().lineSpacing() * 3 + 12
            )
        viewport = self.settings_scroll.viewport()
        if watched is viewport and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.FontChange,
        }:
            stacked = viewport.width() < viewport.fontMetrics().horizontalAdvance(
                "Offline preparation engine   A useful editable value"
            )
            for region in self.settings_regions:
                form = region.layout()
                assert isinstance(form, QFormLayout)
                form.setRowWrapPolicy(
                    QFormLayout.RowWrapPolicy.WrapAllRows
                    if stacked
                    else QFormLayout.RowWrapPolicy.WrapLongRows
                )
        return super().eventFilter(watched, event)

    def _set_advanced_settings(self, visible: bool) -> None:
        for form, field in self._advanced_rows:
            form.setRowVisible(field, visible)
        self.speech_form.setRowVisible(
            self.narrator_reference_layout,
            visible and self.advanced_narrator.isChecked(),
        )
        self.update_speech_backend_controls()

    @staticmethod
    def _settings_region(title: str, form: QFormLayout) -> QGroupBox:
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        region = QGroupBox(title)
        region.setLayout(form)
        for row in range(form.rowCount()):
            label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if label is not None:
                widget = label.widget()
                if isinstance(widget, QLabel):
                    widget.setWordWrap(True)
        for choice in region.findChildren(QComboBox):
            choice.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            choice.setMinimumContentsLength(16)
            choice.setToolTip(choice.currentText())
            choice.currentTextChanged.connect(choice.setToolTip)
        return region

    def _path_selector(
        self,
        field: QLineEdit,
        name: str,
        description: str,
        *,
        directory: bool = False,
        file_filter: str = "All files (*)",
    ) -> tuple[QHBoxLayout, QPushButton]:
        field.setAccessibleName(name)
        field.setAccessibleDescription(description)
        button = QPushButton("Browse...")
        set_action_icon(button, "folder-open")
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
        field: QLineEdit,
        name: str,
        *,
        directory: bool = False,
        file_filter: str = "All files (*)",
    ) -> str:
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
        return selected

    def show_settings_section(self, index: int) -> None:
        if 0 <= index < len(self.settings_regions):
            for position, region in enumerate(self.settings_regions):
                region.setVisible(position == index)
            self.advanced_settings.setVisible(index in {1, 2, 3})
            self.restart_note.setText(
                "Hotkey changes take effect when saved."
                if index == 0
                else "Speech engine and voice changes reload speech when saved."
            )
            self.restart_note.setVisible(
                index == 2 or (index == 0 and sys.platform != "darwin")
            )
            self.settings_scroll.verticalScrollBar().setValue(0)

    def _connect_validation_updates(self) -> None:
        self.narrator_reference.textEdited.connect(self._use_narrator_file)
        for recorder in self.hotkey_recorders:
            recorder.keySequenceChanged.connect(self.update_validation_summary)
        for text_field in (
            self.screenshot_directory,
            self.ocr_diagnostics_directory,
            self.tts_model,
            self.offline_tts_model,
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
            text_field.textChanged.connect(self.update_validation_summary)
        for combo_box in (
            self.capture_mode,
            self.game_window,
            self.speech_backend,
            self.offline_speech_backend,
            self.live_sequence_mode,
        ):
            combo_box.currentIndexChanged.connect(self.update_validation_summary)
        self.game_window.currentTextChanged.connect(self.update_validation_summary)
        for check_box in (
            self.retain_uncertain_frames,
            self.xtts_terms,
        ):
            check_box.toggled.connect(self.update_validation_summary)

    @staticmethod
    def _directory_validation_error(label: str, value: str) -> str | None:
        text = value.strip()
        if not text:
            return f"{label}: choose a directory."
        path = Path(text).expanduser()
        if path.exists() and not path.is_dir():
            return f"{label}: the selected path is not a directory."
        return None

    @staticmethod
    def _file_validation_error(label: str, value: str) -> str | None:
        text = value.strip()
        if not text:
            return None
        if not Path(text).expanduser().is_file():
            return f"{label}: the selected file does not exist."
        return None

    def _validate_game_pack(self) -> tuple[AppSettings | None, str | None]:
        path_error = self._file_validation_error("Game pack", self.game_pack.text())
        if path_error:
            return None, path_error
        if self._game_pack_error is not None:
            return None, self._game_pack_error
        settings = self._raw_settings()
        if self._game_pack_validation is not None and self._game_pack_validation[
            0
        ] == self._game_pack_key(settings):
            return self._settings_with_game_pack(settings), None
        return None, None

    def _game_pack_checked(
        self, settings: AppSettings | None, error: Exception | None
    ) -> None:
        pending = self._pending_pack_settings
        self._pending_pack_settings = None
        self.save_button.setEnabled(True)
        if pending is None:
            return
        if error is not None:
            cause = error.__cause__
            self._game_pack_error = (
                f"Game pack: invalid JSON at line {cause.lineno}, "
                f"column {cause.colno}. Choose a valid game pack."
                if isinstance(cause, JSONDecodeError)
                else f"Game pack: {error}."
            )
            self._focus_validation_errors(self.update_validation_summary())
            return
        assert settings is not None
        self._game_pack_validation = self._game_pack_key(pending), settings
        self.validate_and_accept()

    def _cancel_pack_check(self) -> None:
        self.game_pack_runner.cancel()
        self._pending_pack_settings = None
        self.save_button.setEnabled(True)

    def validation_errors(self) -> tuple[ValidationError, ...]:
        errors: list[ValidationError] = []

        def add(section: int, widget: QWidget, message: str | None) -> None:
            if message:
                errors.append((section, widget, message))

        for recorder, message in hotkey_recording_errors(
            {"Read once": self.read_hotkey, "Live reading": self.live_hotkey}
        ):
            add(0, recorder, message)
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
            or self.offline_speech_backend.currentData() == "coqui-xtts"
        ) and not self.xtts_terms.isChecked():
            add(2, self.xtts_terms, "XTTS license: accept the CPML terms.")
        backend = self.speech_backend.currentData()
        if not packaged_speech_backend_available(backend):
            add(
                2,
                self.speech_backend,
                f"Speech engine: {backend} is not included in this application "
                "package. Choose an available engine.",
            )
        offline_backend = self.offline_speech_backend.currentData()
        if not packaged_speech_backend_available(offline_backend):
            add(
                2,
                self.offline_speech_backend,
                f"Offline preparation engine: {offline_backend} is not included in this application package.",
            )
        narrator_binding = self.voice_library.binding("Narrator")
        if "qwen-tts" in {backend, offline_backend} and (
            not self.narrator_reference.text().strip()
            and not (narrator_binding and narrator_binding.source_sha256s)
        ):
            add(
                2,
                self.choose_narrator_button,
                "Narrator voice: choose a game voice or reference recording for Qwen3-TTS.",
            )
        if (
            backend == "moss-tts"
            and not self.narrator_reference.text().strip()
            and narrator_binding is None
        ):
            add(
                2,
                self.choose_narrator_button,
                "Narrator voice: choose a game voice for MOSS-TTS. "
                "You can listen before choosing.",
            )
        game_pack = self.game_pack.text().strip()
        effective_settings: AppSettings | None = None
        if game_pack:
            effective_settings, pack_error = self._validate_game_pack()
            add(2, self.game_pack, pack_error)

        for label, field in (
            ("Narrator reference", self.narrator_reference),
            ("Live speaker corpus", self.live_speaker_corpus),
        ):
            if field is self.narrator_reference and narrator_binding is not None:
                continue
            add(2, field, self._file_validation_error(label, field.text()))
        if game_pack and effective_settings is None:
            return tuple(errors)
        derived_paths = (
            (
                "Voice manifest",
                self.voice_manifest,
                effective_settings.voice_manifest
                if effective_settings is not None
                else self.voice_manifest.text(),
            ),
            (
                "Story index",
                self.story_index,
                effective_settings.story_index
                if effective_settings is not None
                else self.story_index.text(),
            ),
            (
                "Live sequence plan",
                self.live_sequence_plan,
                effective_settings.live_sequence_plan
                if effective_settings is not None
                else self.live_sequence_plan.text(),
            ),
            (
                "Generated audio manifest",
                self.generated_audio_manifest,
                effective_settings.generated_audio_manifest
                if effective_settings is not None
                else self.generated_audio_manifest.text(),
            ),
        )
        for label, field, value in derived_paths:
            add(2, field, self._file_validation_error(label, value or ""))
        sequence_mode = (
            effective_settings.live_sequence_mode
            if effective_settings is not None
            else self.live_sequence_mode.currentData()
        )
        sequence_plan = (
            effective_settings.live_sequence_plan
            if effective_settings is not None
            else self.live_sequence_plan.text().strip()
        )
        story_index = (
            effective_settings.story_index
            if effective_settings is not None
            else self.story_index.text().strip()
        )
        sequence_configured = bool(sequence_plan or story_index)
        if sequence_mode != "off" and (
            sequence_mode != "audio-auto" or sequence_configured
        ):
            if not sequence_plan:
                add(
                    2,
                    self.live_sequence_plan,
                    "Sequence-first rollout: choose a live sequence plan for "
                    "the selected mode.",
                )
            if not story_index:
                add(
                    2,
                    self.story_index,
                    "Story index: choose the exact index bound by the live "
                    "sequence plan.",
                )
        return tuple(errors)

    def update_validation_summary(self, *_args: object) -> tuple[ValidationError, ...]:
        if _args:
            self._cancel_pack_check()
            self._game_pack_error = None
        from vntts.speech_presentation import narrator_voice_label

        self.narrator_voice.setText(
            narrator_voice_label(self._raw_settings(), self.voice_library)
        )
        errors = self.validation_errors()
        message = (
            "Check these settings before saving:\n"
            + "\n".join(f"- {message}" for _section, _widget, message in errors)
            if errors
            else ""
        )
        self._show_validation_message(message)
        return errors

    def _show_validation_message(self, message: str) -> None:
        if self.validation_summary.toPlainText() != message:
            self.validation_summary.setPlainText(message)
        self.validation_summary.setVisible(bool(message))

    def _resize_for_available_screen(self) -> None:
        available = self.screen().availableGeometry()
        horizontal_margin = 64
        vertical_margin = 64
        available_width = max(320, available.width() - horizontal_margin)
        available_height = max(320, available.height() - vertical_margin)
        self.resize(
            min(760, available_width),
            min(600, available_height),
        )

    def browse_screenshot_directory(self) -> None:
        self._browse_path(
            self.screenshot_directory,
            "Screenshot directory",
            directory=True,
        )

    def browse_ocr_diagnostics_directory(self) -> None:
        self._browse_path(
            self.ocr_diagnostics_directory,
            "OCR diagnostics directory",
            directory=True,
        )

    def choose_narrator(self) -> None:
        offline_qwen = (
            self.offline_speech_backend.currentData() == "qwen-tts"
            and self.speech_backend.currentData() != "qwen-tts"
        )
        dialog = GameNarratorDialog(
            self._raw_settings(),
            self,
            voice_library=self.voice_library,
            use_offline_engine=offline_qwen,
            voice_binding_rollback=self._voice_binding_rollback,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        candidate = dialog.result_settings
        if candidate is None:
            return
        if not offline_qwen:
            self.speech_backend.setCurrentIndex(
                self.speech_backend.findData(candidate.speech_backend)
            )
            self.tts_model.setText(candidate.tts_model or "")
            self.tts_profile.setCurrentText(candidate.tts_profile)
            self.speaker_announcement_mode.setCurrentIndex(
                self.speaker_announcement_mode.findData(
                    candidate.effective_speaker_announcement_mode
                )
            )
        self.voice_manifest.setText(candidate.voice_manifest or "")
        if not offline_qwen:
            self.pocket_gated_model.setChecked(candidate.pocket_gated_model_accepted)
            with QSignalBlocker(self.narrator_reference):
                self.narrator_reference.setText(candidate.tts_speaker_wav or "")
            if not candidate.tts_speaker_wav:
                self.advanced_narrator.setChecked(False)
        self.update_validation_summary()

    def _use_narrator_file(self, path: str) -> None:
        if path.strip():
            self.update_validation_summary()

    def browse_narrator_reference(self) -> None:
        selected = self._browse_path(
            self.narrator_reference,
            "Narrator reference",
            file_filter="Audio files (*.flac *.m4a *.mp3 *.ogg *.wav);;All files (*)",
        )
        if selected:
            self._use_narrator_file(selected)

    def update_ocr_diagnostics_controls(self) -> None:
        enabled = self.retain_uncertain_frames.isChecked()
        self.ocr_diagnostics_directory.setEnabled(enabled)
        self.diagnostics_browse_button.setEnabled(enabled)

    def update_auto_advance_controls(self) -> None:
        allowed, enabled, tooltip = auto_advance_control_state(
            self.capture_mode.currentData(),
            self.live_sequence_mode.currentData(),
            self.auto_advance.isChecked(),
        )
        if self.auto_advance.isChecked() != enabled:
            self.auto_advance.blockSignals(True)
            self.auto_advance.setChecked(enabled)
            self.auto_advance.blockSignals(False)
        self.auto_advance.setEnabled(allowed)
        self.auto_advance.setToolTip(tooltip)
        self.auto_advance_reason.setText(tooltip)
        self.auto_advance_reason.setVisible(not allowed)
        self.auto_advance_key.setEnabled(enabled)
        self.auto_advance_delay.setEnabled(enabled)

    def refresh_windows(self) -> None:
        selected_title = self.game_window.currentText().strip()
        try:
            windows = list_windows()
        except WindowCaptureError as error:
            QMessageBox.warning(self, "Window capture unavailable", str(error))
            return
        self.game_window.clear()
        self.game_window.addItems([window.title for window in windows])
        self.game_window.setCurrentText(selected_title)

    def update_capture_controls(self) -> None:
        window_capture = self.capture_mode.currentData() == "window"
        self.game_window.setEnabled(window_capture)
        self.refresh_windows_button.setEnabled(window_capture)
        self.update_auto_advance_controls()

    def update_terms_control(self) -> None:
        backend = self.speech_backend.currentData()
        uses_xtts = (
            backend == "coqui-xtts"
            or self.offline_speech_backend.currentData() == "coqui-xtts"
        )
        uses_pocket = (
            backend == "pocket-tts"
            or self.offline_speech_backend.currentData() == "pocket-tts"
        )
        self.xtts_terms.setEnabled(uses_xtts)
        self.xtts_terms.setVisible(uses_xtts)
        self.speech_form.setRowVisible(self.xtts_terms, uses_xtts)
        self.pocket_gated_model.setEnabled(uses_pocket)
        self.pocket_gated_model.setVisible(uses_pocket)
        self.pocket_terms_label.setVisible(uses_pocket)

    def _offline_backend_changed(self) -> None:
        self.offline_tts_model.clear()
        self.update_speech_backend_controls()

    def update_speech_backend_controls(self) -> None:
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
        offline_model = self.offline_speech_backend.currentData() in {
            "coqui-xtts",
            "moss-tts",
        }
        self.offline_tts_model.setEnabled(offline_model)
        advanced = self.advanced_settings.isChecked()
        self.speech_form.setRowVisible(
            self.offline_tts_model, advanced and offline_model
        )
        self.speech_form.setRowVisible(
            self.tts_model, advanced and (uses_xtts or uses_moss)
        )
        self.tts_language.setEnabled(uses_xtts or uses_moss or offline_model)
        self.narrator_reference.setEnabled(True)
        self.narrator_reference_button.setEnabled(True)
        self.narrator_speaker.setEnabled(uses_xtts)
        self.speech_form.setRowVisible(self.narrator_speaker, advanced and uses_xtts)
        self.tts_profile.setEnabled(uses_xtts or uses_moss)
        self.update_speech_rate_controls()
        self.update_terms_control()

    def update_speech_rate_controls(self) -> None:
        uses_xtts = self.speech_backend.currentData() == "coqui-xtts"
        changed_speed = self.speech_rate.value() != 100
        self.speech_rate.setEnabled(uses_xtts)
        self.reset_speech_rate.setEnabled(changed_speed)
        self.speech_rate_notice.setText(
            "Prepared audio requires 100% speed."
            if uses_xtts
            else "This engine uses normal speed. Reset to 100% to use prepared audio."
        )
        self.speech_rate_notice.setVisible(changed_speed)

    def restore_initial_voice_bindings(self) -> None:
        restored = self.voice_library.rollback_bindings(self._voice_binding_rollback)
        self._voice_binding_rollback = self.voice_library.binding_rollback()
        if not restored:
            raise ValueError(
                "A newer voice selection was kept. Other voice changes from these settings were undone."
            )

    def reject(self) -> None:
        self._cancel_pack_check()
        if self.result() == QDialog.DialogCode.Accepted:
            super().reject()
            return
        try:
            self.restore_initial_voice_bindings()
        except (OSError, VoiceLibraryError) as error:
            self._show_validation_message(
                f"Unable to undo voice changes: {error}. Retry Cancel to restore them."
            )
            return
        except ValueError as error:
            QMessageBox.warning(self, "Newer voice selection kept", str(error))
        super().reject()

    def validate_and_accept(self) -> None:
        if self.game_pack_runner.active:
            return
        self._game_pack_error = None
        errors = self.update_validation_summary()
        if errors:
            self._focus_validation_errors(errors)
            return
        settings = self._raw_settings()
        if settings.game_pack and (
            self._game_pack_validation is None
            or self._game_pack_validation[0] != self._game_pack_key(settings)
        ):
            self._pending_pack_settings = settings
            self.save_button.setEnabled(False)
            self._show_validation_message("Checking game pack before saving...")
            self.game_pack_runner.start(
                self._apply_game_pack,
                settings,
                settings.game_pack != self.original_settings.game_pack,
            )
            return
        self.accept()

    def _focus_validation_errors(self, errors: tuple[ValidationError, ...]) -> None:
        if not errors:
            return
        section, widget, _message = errors[0]
        self.advanced_settings.setChecked(True)
        if widget is self.narrator_reference:
            self.advanced_narrator.setChecked(True)
        self.section_navigation.setCurrentIndex(section)
        self.show_settings_section(section)
        widget.setFocus(Qt.FocusReason.OtherFocusReason)
        self.settings_scroll.ensureWidgetVisible(widget, 0, 12)

    def _raw_settings(self) -> AppSettings:
        """Project draft controls; recorded shortcuts are applied on save."""

        def optional_text(widget: QLineEdit) -> str | None:
            return widget.text().strip() or None

        return AppSettings.from_mapping(
            {
                **asdict(self.original_settings),
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
                "offline_speech_backend": self.offline_speech_backend.currentData(),
                "audio_source_policy": self.audio_source_policy.currentData(),
                "tts_model": optional_text(self.tts_model),
                "offline_tts_model": optional_text(self.offline_tts_model),
                "offline_tts_profile": (
                    "default"
                    if self.offline_speech_backend.currentData() == "pocket-tts"
                    else self.original_settings.offline_tts_profile
                    if self.offline_speech_backend.currentData()
                    == self.original_settings.offline_speech_backend
                    else "stable"
                ),
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
                "tts_profile": (
                    "default"
                    if self.speech_backend.currentData() == "pocket-tts"
                    else self.tts_profile.currentText()
                ),
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
                "pocket_gated_model_accepted": (self.pocket_gated_model.isChecked()),
            }
        )

    def _game_pack_key(
        self, settings: AppSettings
    ) -> tuple[str | None, bool, str | None, str, str | None]:
        return (
            settings.game_pack,
            settings.game_pack != self.original_settings.game_pack,
            settings.live_sequence_plan,
            settings.live_sequence_mode,
            settings.voice_manifest,
        )

    @staticmethod
    def _apply_game_pack(settings: AppSettings, selected_new_pack: bool) -> AppSettings:
        started = perf_counter()
        try:
            validated = apply_game_pack(
                settings, settings.game_pack if selected_new_pack else None
            )
        except Exception:
            record_background_operation(
                "settings-game-pack-validation",
                (perf_counter() - started) * 1000,
                "failed",
            )
            raise
        record_background_operation(
            "settings-game-pack-validation",
            (perf_counter() - started) * 1000,
            "complete",
        )
        return validated

    def _settings_with_game_pack(self, settings: AppSettings) -> AppSettings:
        if not settings.game_pack:
            return settings
        key = self._game_pack_key(settings)
        if self._game_pack_validation is None or self._game_pack_validation[0] != key:
            self._game_pack_validation = key, self._apply_game_pack(settings, key[1])
        validated = self._game_pack_validation[1]
        return settings.updated(
            game_pack=validated.game_pack,
            story_index=validated.story_index,
            voice_manifest=validated.voice_manifest,
            generated_audio_manifest=validated.generated_audio_manifest,
            live_sequence_plan=validated.live_sequence_plan,
            live_sequence_mode=validated.live_sequence_mode,
        )

    def settings(self) -> AppSettings:
        hotkeys = self.hotkey_assignments()
        settings = self._raw_settings().updated(
            read_hotkey=hotkeys["Read once"], live_hotkey=hotkeys["Live reading"]
        )
        return guard_auto_advance_settings(self._settings_with_game_pack(settings))

    def hotkey_assignments(self) -> dict[str, str]:
        return {
            "Read once": self.read_hotkey.hotkey(),
            "Live reading": self.live_hotkey.hotkey(),
        }


class TrayApplication(ConfigurationApplyMixin, DurableSettingsMixin, QObject):
    controller: AppController
    dashboard: ControlDashboard
    diagnostics_dialog: DiagnosticsDialog | None
    signals: AppSignals

    def __init__(
        self,
        application: QApplication,
        settings: AppSettings | None = None,
        controller_factory: Callable[..., AppController] = AppController,
        profile_store: GameProfileStore | None = None,
        correction_store: OCRCorrectionStore | None = None,
        pregeneration_activator: OfflinePackActivator | None = None,
        moss_runtime: RetainedMossRuntime | None = None,
        pocket_runtime: RetainedWorkerRuntime | None = None,
    ) -> None:
        super().__init__()
        self._initialize_runtime(
            application,
            settings,
            controller_factory,
            moss_runtime,
            pocket_runtime,
            profile_store,
        )
        self._initialize_runners(pregeneration_activator)
        self._initialize_session_state(profile_store, correction_store)
        self._build_actions()
        self._build_main_menu()
        self._build_setup_and_support_menus()
        self._connect_actions()
        self._connect_application_signals()
        self._connect_dashboard()
        self._audio_runtime_error: str | None = None
        self._start_runtime_timer()

    def _initialize_runtime(
        self,
        application: QApplication,
        settings: AppSettings | None,
        controller_factory: Callable[..., AppController],
        moss_runtime: RetainedMossRuntime | None,
        pocket_runtime: RetainedWorkerRuntime | None,
        profile_store: GameProfileStore | None,
    ) -> None:
        self.application = application
        self._settings_path = get_settings_path().expanduser().absolute()
        self._settings_commit_lock = Lock()
        self._settings_revision: bytes | None = None
        self._last_saved_settings: AppSettings | None = None
        uses_saved_settings = settings is None
        self.previous_session: SupportDocument = (
            preserve_previous_session(get_local_data_directory())
            if uses_saved_settings
            else {"available": False}
        )
        if uses_saved_settings:
            configure_performance_log(get_local_data_directory() / "performance.log")
        self._startup_game_pack_errors: list[Exception] = []
        settings_started = perf_counter()

        def remember_settings_snapshot(
            saved_settings: AppSettings, revision: bytes | None
        ) -> None:
            self._settings_revision = revision
            self._last_saved_settings = saved_settings

        if settings is None:
            self.settings = load_app_settings(
                self._settings_path,
                on_game_pack_error=self._startup_game_pack_errors.append,
                on_snapshot=remember_settings_snapshot,
            )
        else:
            self.settings = settings
            try:
                self._settings_revision = file_revision(self._settings_path)
            except OSError:
                pass
        self.profile_store = profile_store or GameProfileStore.load()
        if uses_saved_settings:
            record_background_operation(
                "application-settings-and-pack-load",
                (perf_counter() - settings_started) * 1000,
                "complete",
            )
        self.signals = AppSignals()
        self.last_controller_error: str | None = None
        self.support_log = RuntimeSupportLog(
            path=(
                get_local_data_directory() / "runtime.log"
                if uses_saved_settings
                else None
            )
        )
        configure_game_import_log(
            get_local_data_directory() / "game-import.log"
            if uses_saved_settings
            else None
        )
        configure_native_speech_log(
            get_local_data_directory() / "native-speech.log"
            if uses_saved_settings
            else None
        )
        configure_generation_stall_log(
            get_local_data_directory() / "generation-stalls.log"
            if uses_saved_settings
            else None
        )
        self.audio_lifecycle = configure_audio_lifecycle_log(
            get_local_data_directory() / "audio-lifecycle.log"
            if uses_saved_settings
            else None
        )
        configure_pregeneration_support(
            get_local_data_directory() / "pregeneration-support.json"
            if uses_saved_settings
            else None
        )
        if not uses_saved_settings:
            configure_performance_log()
        self.generation_timelines = GenerationTimelineLog(
            path=(
                get_local_data_directory() / "generation-timelines.json"
                if uses_saved_settings
                else None
            )
        )
        self.reading_diagnostic_session: (
            ReadingDiagnosticSession | SavedDiagnosticSession | None
        ) = (
            latest_finished_diagnostic_session(
                get_local_data_directory() / "reading-diagnostics"
            )
            if uses_saved_settings
            else None
        )
        self.moss_runtime = moss_runtime or RetainedMossRuntime()
        self.pocket_runtime = pocket_runtime or RetainedWorkerRuntime("pocket-tts")
        self.voice_auditions = VoiceAuditionCoordinator(
            VoiceAuditionPreviewService(
                backend_factory=self.moss_runtime.benchmark_backend
            ),
            background_available=self._voice_audition_background_available,
        )
        self.controller = controller_factory(
            self.settings,
            profile_store=self.profile_store,
            status_handler=self.signals.status_changed.emit,
            dialog_handler=self.signals.dialog_changed.emit,
            diagnostic_handler=self.signals.diagnostics_changed.emit,
            sequence_status_handler=self.signals.sequence_status_changed.emit,
            unknown_speaker_handler=self.signals.unknown_speaker.emit,
            error_handler=self.report_controller_error,
            route_trace_handler=self.record_audio_route,
            pipeline_event_handler=self.generation_timelines.record,
        )
        if isinstance(self.controller, AppController):
            self.controller.moss_backend_factory = self.moss_runtime
            self.controller.pocket_backend_factory = self.pocket_runtime
        self.session_owner = PlayerSessionOwner(self.controller)

    def _initialize_runners(
        self, pregeneration_activator: OfflinePackActivator | None
    ) -> None:
        self.live_stop_runner = LatestTaskRunner(self)
        self.live_stop_runner.finished.connect(self._live_stop_finished)
        self._live_stop_continuation: Callable[[], object] | None = None
        self._live_stop_generation: int | None = None
        self._modal_stop_pending = False
        self._live_stop_wait_for_speech = True
        self.calibration_capture_runner = LatestTaskRunner(self)
        self.calibration_capture_runner.finished.connect(
            self._calibration_capture_finished
        )
        self.profile_restart_runner = LatestTaskRunner(self)
        self.profile_restart_runner.finished.connect(self._profile_restart_finished)
        self.initial_start_runner = LatestTaskRunner(self)
        self.initial_start_runner.finished.connect(self._initial_start_finished)
        self._initial_start_generation: int | None = None
        self.onboarding_test_runner = LatestTaskRunner(self)
        self.onboarding_test_runner.finished.connect(self._onboarding_test_finished)
        self._onboarding_test_generation: int | None = None
        self._onboarding_capture_request: _OnboardingCaptureRequest | None = None
        self._onboarding_preview_settings: AppSettings | None = None
        self.live_scope_runner = LatestTaskRunner(self)
        self.live_scope_runner.finished.connect(self._live_scope_finished)
        self._live_scope_generation: int | None = None
        self._live_scope_cancellation: Event | None = None
        self._live_scope_target: object | None = None
        self._live_scope_settings: object | None = None
        self._live_scope_observation_serial: object | None = None
        self._live_scope_window: object | None = None
        self.live_scope_focus_timer = QTimer(self)
        self.live_scope_focus_timer.setSingleShot(True)
        self.live_scope_focus_timer.setInterval(200)
        self.live_scope_focus_timer.timeout.connect(self._retry_live_scope_when_focused)
        self.diagnostics_refresh_runner = LatestTaskRunner(self)
        self.diagnostics_refresh_runner.finished.connect(
            self._diagnostics_refresh_finished
        )
        self.moss_runtime_runner = LatestTaskRunner(self)
        self.moss_runtime_runner.finished.connect(self._moss_runtime_finished)
        self.pregeneration_activator = pregeneration_activator or OfflinePackActivator(
            save_settings=self._save_settings_candidate
        )
        self.pregeneration_activation_runner = LatestTaskRunner(self)
        self.pregeneration_activation_runner.finished.connect(
            self._pregeneration_activation_finished
        )
        self.support_export_runner = LatestTaskRunner(self)
        self.support_export_runner.finished.connect(self._support_export_finished)
        self._pregeneration_activation_generation: int | None = None
        self._pregeneration_activation_cancellation: Event | None = None
        self._pregeneration_activation_restore_runtime = Event()
        self._pregeneration_activation_status: str | None = None
        self._pending_profile_name: str | None = None
        self._profile_restart_generation: int | None = None

    def _initialize_session_state(
        self,
        profile_store: GameProfileStore | None,
        correction_store: OCRCorrectionStore | None,
    ) -> None:
        self._lifecycle_generation = 0
        self._controller_ready = False
        self._controller_busy = False
        self._start_live_after_initial_start = False
        self._shutting_down = False
        self._quit_requested = False
        self._reported_live = False
        self._reported_speech_paused = False
        self._reported_diagnostic_failure: str | None = None
        self.correction_store = correction_store or OCRCorrectionStore.load()
        self.hotkey_listener = None
        self.calibration_overlay: DialogRegionOverlay | None = None
        self.onboarding_wizard: OnboardingWizard | None = None
        self.diagnostics_dialog: DiagnosticsDialog | None = None
        self._onboarding_voice_wizard: OnboardingWizard | None = None
        self.diagnostics_refresh_generation = 0
        self.readiness_dialog = None
        self.pregeneration_dialog: OfflineAudioPreparationDialog | None = None
        self.support_dialog: SupportCenterDialog | None = None
        self.narrator_dialog: GameNarratorDialog | None = None
        self._narrator_preparation: OfflineAudioPreparationDialog | None = None
        self._narrator_offline = False
        self._narrator_return_to_stories = False
        self._resume_live_after_narrator = False
        self._preparation_activation_settings: AppSettings | None = None
        self._reading_pack_admission: _ReadingPackAdmission | None = None
        self.unknown_speaker_prompt: QMessageBox | None = None
        self.unknown_speaker_choose_button: QPushButton | None = None
        self.unknown_speaker_continue_button: QPushButton | None = None
        self.unknown_speaker_cancel_button: QPushButton | None = None
        self.pending_unknown_speaker: str | None = None
        self._queued_unknown_speakers: deque[str] = deque()
        self.unknown_speaker_mapping_in_progress: str | None = None
        self.resume_live_after_unknown_mapping = False
        self.onboarding_cancel_event = Event()
        self._calibration_visibility: CaptureWindowVisibility | None = None
        self._calibration_capture_pending = False
        self._notification_recovery: str | None = None
        self._background_notification_shown = False
        self.dashboard = ControlDashboard(self.settings)
        self.compact_controller = CompactController()

    def _build_actions(self) -> None:
        self.tray = QSystemTrayIcon(self._application_icon(), self)
        self.menu: QMenu = QMenu(self.dashboard)
        self.status_action = QAction("Starting...", self)
        self.status_action.setEnabled(False)
        self.dialog_action = QAction("No dialogue detected", self)
        self.dialog_action.setEnabled(False)
        self.read_action = QAction("Read current dialogue", self)
        self.show_dashboard_action = QAction("Full controls", self)
        self.show_compact_action = QAction("Compact controls", self)
        self.live_action = QAction("Start reading", self)
        self.pause_action = QAction("Pause speech", self)
        self.sequence_resync_action = QAction("Set story position / resync...", self)
        self.sequence_expected_action = QAction("Use expected next line", self)
        self.auto_advance_action = QAction("Auto advance dialogue", self)
        self.auto_advance_action.setCheckable(True)
        self.auto_advance_action.setChecked(self.settings.auto_advance_enabled)
        self.auto_advance_reason_action = QAction(self)
        self.auto_advance_reason_action.setEnabled(False)
        self.calibrate_action = QAction("Calibrate dialogue region...", self)
        self.diagnostics_action = QAction("Live diagnostics", self)
        self.readiness_action = QAction("Check readiness", self)
        self.settings_action = QAction("Settings...", self)
        self.profiles_action = QAction("Game profiles...", self)
        self.corrections_action = QAction("OCR corrections...", self)
        self.ocr_review_action = QAction("Review uncertain OCR...", self)
        self.pregeneration_action = QAction("Prepare offline audio...", self)
        self.setup_action = QAction("Run setup", self)
        self.assets_action = QAction("Manage models and voices...", self)
        self.moss_runtime_action = QAction("Load OpenMOSS", self)
        self.voice_preview_action = QAction("Choose narrator voice...", self)
        self.speaker_mapping_action = QAction("Manage character voices...", self)
        self.history_action = QAction("Dialogue history...", self)
        self.support_action = QAction("Support and logs", self)
        self.macos_permissions_action = QAction("macOS permissions...", self)
        self.macos_permissions_action.setVisible(sys.platform == "darwin")
        self.settings_folder_action = QAction("Open settings folder", self)
        self.quit_action = QAction("Quit", self)

    def _build_main_menu(self) -> None:
        self.read_action.setEnabled(False)
        self.live_action.setEnabled(False)
        self.pause_action.setEnabled(False)
        self.sequence_resync_action.setEnabled(False)
        self.sequence_expected_action.setEnabled(False)
        sequence_controls_visible = bool(
            is_live_sequence_audio_mode(self.settings.live_sequence_mode)
            and self.settings.live_sequence_plan
            and self.settings.story_index
        )
        self.sequence_resync_action.setVisible(sequence_controls_visible)
        self.sequence_expected_action.setVisible(sequence_controls_visible)
        self.voice_preview_action.setEnabled(False)
        self.menu.addAction(self.show_dashboard_action)
        self.menu.addAction(self.show_compact_action)
        self.menu.addAction(self.status_action)
        self.menu.addAction(self.dialog_action)
        self.menu.addSeparator()
        self.menu.addAction(self.read_action)
        self.menu.addAction(self.live_action)
        self.menu.addAction(self.pause_action)
        self.playback_menu = self.menu.addMenu("Reading")
        self.playback_menu.addAction(self.sequence_resync_action)
        self.playback_menu.addAction(self.sequence_expected_action)
        self.playback_menu.addAction(self.auto_advance_action)
        self.playback_menu.addAction(self.auto_advance_reason_action)
        self.playback_menu.addAction(self.history_action)

    def _build_setup_and_support_menus(self) -> None:
        self.setup_menu = self.menu.addMenu("Setup")
        self.setup_menu.addAction(self.readiness_action)
        self.setup_menu.addAction(self.setup_action)
        self.setup_menu.addAction(self.pregeneration_action)
        self.setup_menu.addSeparator()
        self.setup_menu.addAction(self.calibrate_action)
        self.setup_menu.addAction(self.settings_action)
        self.setup_menu.addAction(self.profiles_action)
        self.setup_menu.addAction(self.assets_action)
        self.setup_menu.addAction(self.moss_runtime_action)
        self.setup_menu.addAction(self.voice_preview_action)
        self.setup_menu.addAction(self.speaker_mapping_action)
        self.setup_menu.addAction(self.corrections_action)
        self.setup_menu.addAction(self.ocr_review_action)
        self.support_menu = self.menu.addMenu("Support")
        self.support_menu.addAction(self.diagnostics_action)
        self.support_menu.addAction(self.support_action)
        self.support_menu.addAction(self.macos_permissions_action)
        self.support_menu.addAction(self.settings_folder_action)
        self._setup_configuration_apply()
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip(application_name)

    def _connect_actions(self) -> None:
        self.read_action.triggered.connect(self.read_once)
        self.show_dashboard_action.triggered.connect(self.show_dashboard)
        self.show_compact_action.triggered.connect(self.show_compact_controls)
        self.live_action.triggered.connect(self._toggle_presented_reading)
        self.pause_action.triggered.connect(self.toggle_speech_pause)
        self.sequence_resync_action.triggered.connect(self.choose_sequence_position)
        self.sequence_expected_action.triggered.connect(
            self.choose_expected_sequence_event
        )
        self.auto_advance_action.toggled.connect(self.toggle_auto_advance)
        self.calibrate_action.triggered.connect(self.calibrate)
        self.diagnostics_action.triggered.connect(self.open_diagnostics)
        self.readiness_action.triggered.connect(self.open_readiness)
        self.settings_action.triggered.connect(self.open_settings)
        self.profiles_action.triggered.connect(self.open_profiles)
        self.corrections_action.triggered.connect(self.open_corrections)
        self.ocr_review_action.triggered.connect(self.open_ocr_review)
        self.pregeneration_action.triggered.connect(self.open_pregeneration)
        self.setup_action.triggered.connect(self.run_onboarding)
        self.assets_action.triggered.connect(self.open_assets)
        self.moss_runtime_action.triggered.connect(self.toggle_moss_runtime)
        self.voice_preview_action.triggered.connect(self.open_voice_previews)
        self.speaker_mapping_action.triggered.connect(self.open_speaker_mapping)
        self.history_action.triggered.connect(self.open_history)
        self.support_action.triggered.connect(self.open_support_center)
        self.macos_permissions_action.triggered.connect(self.open_macos_permissions)
        self.settings_folder_action.triggered.connect(self.open_settings_folder)
        self.quit_action.triggered.connect(self.request_quit)
        self.tray.messageClicked.connect(self._activate_notification_recovery)
        self._update_auto_advance_action()

    def _connect_application_signals(self) -> None:
        self.signals.onboarding_capture_requested.connect(
            self._hide_onboarding_capture_windows
        )
        self.signals.onboarding_capture_released.connect(
            self._restore_onboarding_capture_windows
        )
        self.signals.status_changed.connect(self.set_status)
        self.signals.dialog_changed.connect(self.set_dialog)
        self.signals.ready_changed.connect(self.set_ready)
        self.signals.live_changed.connect(self.set_live)
        self.signals.speech_paused_changed.connect(self.set_speech_paused)
        self.signals.error_reported.connect(self.show_error)
        self.signals.diagnostics_changed.connect(self.update_diagnostics_snapshot)
        self.signals.sequence_status_changed.connect(self.set_sequence_status)
        self.signals.diagnostics_failed.connect(self.set_diagnostics_error)
        self.signals.hotkeys_requested.connect(self._refresh_hotkeys)
        self.signals.read_requested.connect(
            self.read_once, Qt.ConnectionType.QueuedConnection
        )
        self.signals.live_requested.connect(
            self.toggle_live, Qt.ConnectionType.QueuedConnection
        )
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

    def _connect_dashboard(self) -> None:
        self.dashboard.main_section_changed.connect(self._save_main_section)
        self.dashboard.read_requested.connect(self.read_once)
        self.dashboard.live_requested.connect(self._toggle_presented_reading)
        self.dashboard.pause_requested.connect(self.toggle_speech_pause)
        self.dashboard.auto_advance_requested.connect(self.toggle_auto_advance)
        self.dashboard.reading_setup_requested.connect(self.prepare_reading)
        self.dashboard.sequence_resync_requested.connect(self.choose_sequence_position)
        self.dashboard.sequence_expected_requested.connect(
            self.choose_expected_sequence_event
        )
        self.dashboard.pregeneration_requested.connect(self.open_pregeneration)
        self.dashboard.readiness_requested.connect(self.open_readiness)
        self.dashboard.calibration_requested.connect(self.calibrate)
        self.dashboard.voices_requested.connect(self.open_voice_previews)
        self.dashboard.diagnostics_requested.connect(self.open_support_center)
        self.dashboard.settings_requested.connect(self.open_settings)
        self.dashboard.compact_requested.connect(self.show_compact_controls)
        self.dashboard.moss_runtime_requested.connect(self.toggle_moss_runtime)
        self.dashboard.quit_requested.connect(self.request_quit)
        self.dashboard.hidden_to_background.connect(self.notify_background_mode)
        self.compact_controller.read_requested.connect(self.read_once)
        self.compact_controller.live_requested.connect(self._toggle_presented_reading)
        self.compact_controller.pause_requested.connect(self.toggle_speech_pause)
        self.compact_controller.sequence_expected_requested.connect(
            self.choose_expected_sequence_event
        )
        self.compact_controller.full_requested.connect(self.show_dashboard)

    def _start_runtime_timer(self) -> None:
        self.speech_runtime_timer = QTimer(self)
        self.speech_runtime_timer.setInterval(500)
        self.speech_runtime_timer.timeout.connect(self._refresh_speech_runtime)
        self.speech_runtime_timer.start()
        self._refresh_speech_runtime()

    def _speech_runtime_label(self) -> str:
        retained = self.moss_runtime.backend
        return speech_runtime_label(
            None
            if self._shutting_down
            else retained or getattr(self.controller, "speech_backend", None)
        )

    def _refresh_speech_runtime(self) -> None:
        self._refresh_reading_diagnostic_status()
        self._apply_runtime_control_state(self._runtime_control_state())
        self.dashboard.set_speech_runtime(self._speech_runtime_label())
        loaded = self.moss_runtime.loaded
        label = (
            "Loading OpenMOSS..."
            if self.moss_runtime_runner.active
            else "Unload OpenMOSS"
            if loaded
            else "Load OpenMOSS"
        )
        self.dashboard.moss_runtime_button.setText(label)
        self.moss_runtime_action.setText(label)
        visible = self.settings.speech_backend == "moss-tts"
        self.dashboard.moss_runtime_button.setVisible(visible)
        self.moss_runtime_action.setVisible(visible)
        preparing = bool(
            self.pregeneration_dialog is not None
            and self.pregeneration_dialog.has_pending_work()
        )
        enabled = not (
            self.moss_runtime_runner.active
            or self._controller_busy
            or self.controller.is_live_running is True
            or preparing
            or self._shutting_down
        )
        self.dashboard.moss_runtime_button.setEnabled(enabled)
        self.moss_runtime_action.setEnabled(enabled)

    def _voice_audition_background_available(self) -> bool:
        dialog = self.pregeneration_dialog
        reading = self._runtime_control_state()
        return not (
            self._shutting_down
            or self._controller_busy
            or reading.active
            or reading.transition is not None
            or dialog is not None
            and (dialog.generating or dialog.recovering)
        )

    def toggle_moss_runtime(self) -> None:
        if self.settings.speech_backend != "moss-tts" or self._shutting_down:
            return
        if self.moss_runtime.loaded:
            self.voice_auditions.suspend()
            self.moss_runtime.unload()
            self.set_status("OpenMOSS unloaded. It will load again on the next speech.")
            self._refresh_speech_runtime()
            return
        if self.moss_runtime_runner.active:
            return
        download = OnboardingDiagnostics().moss_installation_space(self.settings)
        if download is not None:
            remaining, required, free = download
            if free < required:
                self.show_error(
                    "OpenMOSS cannot be installed yet: "
                    f"{required / 1e9:.1f} GB free is required, but "
                    f"{free / 1e9:.1f} GB is available."
                )
                return
            amount = (
                f"{remaining / 1e9:.1f} GB"
                if remaining >= 1_000_000_000
                else f"{remaining / 1_000_000:.1f} MB"
            )
            answer = QMessageBox.question(
                self.dashboard,
                "Install OpenMOSS",
                f"OpenMOSS needs a {amount} download before it can load. "
                "Download and install it now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        registry = self.controller.voice_registry_initializer(
            self.settings, self.report_controller_error
        )
        if registry is None:
            return
        self.set_status("Loading OpenMOSS in the background...")
        self.moss_runtime_runner.start(
            self.moss_runtime.backend_for,
            registry,
            model_name=self.settings.tts_model,
            language=self.settings.tts_language or "English",
            generation_profile=self.settings.tts_profile,
            volume=self.settings.output_volume_percent / 100,
            startup_progress=self.signals.status_changed.emit,
            allow_download=download is not None,
        )

    def _moss_runtime_finished(
        self, _backend: object | None, error: Exception | None
    ) -> None:
        if error is not None:
            self.show_error(f"Unable to load OpenMOSS: {error}")
        elif not self._shutting_down:
            self.set_status("OpenMOSS is loaded and ready.")
        self._refresh_speech_runtime()

    def _update_auto_advance_action(self) -> None:
        allowed, enabled, reason = auto_advance_control_state(
            self.settings.capture_mode,
            self.settings.live_sequence_mode,
            self.settings.auto_advance_enabled,
        )
        self.auto_advance_action.blockSignals(True)
        self.auto_advance_action.setChecked(enabled)
        self.auto_advance_action.blockSignals(False)
        availability_reason = (
            reason
            if not allowed
            else (
                "Reading controls are updating. Try again when ready."
                if self._controller_busy
                else ""
            )
        )
        self.auto_advance_action.setEnabled(allowed and not self._controller_busy)
        self.auto_advance_action.setToolTip(availability_reason or reason)
        self.auto_advance_reason_action.setText(availability_reason)
        self.auto_advance_reason_action.setVisible(bool(availability_reason))
        self.dashboard.set_auto_advance_configuration(
            self.settings, busy=self._controller_busy
        )

    def _application_icon(self) -> QIcon:
        return create_application_icon(self.application.style())

    def start(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        else:
            self.settings = self.settings.updated(keep_running_on_close=False)
            self.dashboard.set_configuration(self.settings)
        if self._startup_game_pack_errors:
            self.show_dashboard()
            self.last_controller_error = (
                "Unable to open the saved game pack. Open Settings to select a "
                "valid game pack, then restart VNTTS. Your saved paths were kept. "
                f"{self._startup_game_pack_errors[0]}"
            )
            self.set_ready(False)
            self.show_error(self.last_controller_error)
            return
        self.dashboard.show_main_section(self.settings.last_main_section)
        self.show_dashboard()
        if self.settings.launch_at_login:
            try:
                configure_macos_launch_at_login(True)
            except OSError as error:
                self.show_error(f"Unable to configure launch at login: {error}")
        self.set_status("")
        QTimer.singleShot(0, self, self._load_initial_library)

    def _load_initial_library(self) -> None:
        section = self.dashboard.sections.currentIndex()
        with QSignalBlocker(self.dashboard.sections):
            try:
                self.open_pregeneration()
            finally:
                self.dashboard.sections.setCurrentIndex(section)
        self._audio_runtime_available()

    def _audio_runtime_available(self) -> bool:
        try:
            check_audio_runtime()
        except (OSError, RuntimeError) as error:
            self._audio_runtime_error = f"Audio playback unavailable: {error}"
            self.show_error(self._audio_runtime_error)
            return False
        if self._audio_runtime_error is not None:
            self._audio_runtime_error = None
            self.set_status("Audio playback is available. Retry playback to continue.")
        return True

    def _preparation_runtime_settings(self) -> AppSettings | None:
        dialog = self.pregeneration_dialog
        factory = getattr(dialog, "runtime_playback_settings", None)
        if not callable(factory):
            return None
        candidate = factory()
        return candidate if isinstance(candidate, AppSettings) else None

    @staticmethod
    def _reading_pack_identity(settings: AppSettings) -> tuple[str | None, ...]:
        return (
            settings.game_pack,
            settings.story_index,
            settings.voice_manifest,
            settings.generated_audio_manifest,
            settings.live_sequence_plan,
        )

    def _active_pack_reading_available(self) -> bool:
        identity = self._reading_pack_identity(self.settings)
        if not self.settings.game_pack or self._startup_game_pack_errors:
            return False
        cached = self._reading_pack_admission
        if (
            cached is not None
            and cached.identity == identity
            and (cached.stamps == _ReadingPackAdmission.file_stamps(cached.files))
        ):
            return cached.playable
        files = (
            cached.files
            if cached is not None and cached.identity == identity
            else tuple(Path(path) for path in identity if path)
        )
        playable = False
        try:
            imported = import_game_pack(self.settings.game_pack)
            projected = imported.apply_to(
                self.settings, preserve_external_sequence=True
            )
            playable = bool(imported.pack.generated_wavs) and (
                self._reading_pack_identity(projected) == identity
            )
            bindings = (
                imported.pack.story_index,
                imported.pack.voice_manifest,
                *imported.pack.voice_wavs,
                *imported.pack.generated_wavs,
                *(
                    (imported.pack.generated_audio,)
                    if imported.pack.generated_audio
                    else ()
                ),
                *(
                    (imported.pack.live_sequence_plan,)
                    if imported.pack.live_sequence_plan
                    else ()
                ),
            )
            files = tuple(
                dict.fromkeys(
                    (
                        Path(self.settings.game_pack),
                        *(binding.path for binding in bindings),
                        *(
                            (imported.source_audio_semantic_evidence,)
                            if imported.source_audio_semantic_evidence is not None
                            else ()
                        ),
                    )
                )
            )
        except OSError, ValueError, GamePackError:
            pass
        self._reading_pack_admission = _ReadingPackAdmission(
            identity, files, _ReadingPackAdmission.file_stamps(files), playable
        )
        return playable

    def _reading_source_choice(
        self, *, prefer_preparation: bool = False
    ) -> tuple[AppSettings | None, bool]:
        active_pack = self._active_pack_reading_available()
        if active_pack and not prefer_preparation:
            return None, True
        candidate = self._preparation_runtime_settings()
        return candidate, active_pack or candidate is not None

    def _preparation_blocks_reading(self, *, loaded: bool) -> bool:
        candidate, available = self._reading_source_choice()
        return not available or (
            loaded and candidate is not None and not self._preparation_playback_active()
        )

    def _preparation_playback_active(self) -> bool:
        candidate = self._preparation_runtime_settings()
        controller_settings = getattr(self.controller, "settings", None)
        return bool(
            candidate is not None
            and isinstance(controller_settings, AppSettings)
            and controller_settings.generated_audio_manifest
            == candidate.generated_audio_manifest
            and controller_settings.story_index == candidate.story_index
        )

    def _show_preparation_reading(self, runtime_settings: AppSettings | None) -> None:
        if runtime_settings is None:
            self.dashboard.show_reading()
            return
        with QSignalBlocker(self.dashboard.sections):
            self.dashboard.show_reading()

    def prepare_reading(
        self, *, start_live: bool = False, prefer_preparation: bool = False
    ) -> None:
        if self._controller_busy or self._shutting_down:
            return
        if self.narrator_dialog is not None:
            self.dashboard.show_voices()
            return
        if not self._audio_runtime_available():
            return
        runtime_settings, source_available = self._reading_source_choice(
            prefer_preparation=prefer_preparation
        )
        if self.pregeneration_dialog is not None and (
            self.pregeneration_dialog.has_pending_work() and not source_available
        ):
            self.set_status(
                "Preparation is still working on its first dialogue. Start reading "
                "when the ready-dialogue button appears."
            )
            self.dashboard.show_stories()
            return
        self.voice_auditions.suspend()
        self._show_preparation_reading(runtime_settings)
        if not self.settings.onboarding_completed:
            self.run_onboarding()
            return
        if runtime_settings is not None:
            generation = self._begin_controller_lifecycle()
            applied = self.session_owner.attach(generation, runtime_settings)
            self._finish_controller_lifecycle()
            if applied is False:
                self.show_error("Unable to connect in-progress prepared audio")
                return
            if self.controller.is_ready:
                self._controller_ready = True
                backend = getattr(self.controller, "speech_backend", None)
                dialog = self.pregeneration_dialog
                if backend is not None and dialog is not None:
                    backend.progress_wait_request = dialog.prioritize_line
                    backend.progress_line_observed = dialog.readingLineObserved.emit
                self._apply_controller_action_state()
                self.set_status(
                    "Reading uses finished recordings while preparation continues. "
                    "If you catch up, it waits for that dialogue instead of starting "
                    "a second TTS generation."
                )
                if start_live and not self.controller.is_live_running:
                    self.toggle_live()
                return
        generation = self._begin_controller_lifecycle()
        self._start_live_after_initial_start = bool(start_live)
        self.set_status(
            "Loading the speech model and voices; controls will unlock "
            "automatically when ready"
        )
        self._initial_start_generation = generation
        self.initial_start_runner.start(self._initialize_controller, generation)

    def _initialize_controller(self, generation: int) -> bool:
        return self.session_owner.start(generation)

    def _initial_start_finished(
        self, ready: bool | None, error: Exception | None
    ) -> None:
        generation = self._initial_start_generation
        self._initial_start_generation = None
        if not self._lifecycle_is_current(generation):
            return
        self._finish_controller_lifecycle()
        if error is not None:
            self.last_controller_error = f"Unable to initialize controller: {error}"
            self.show_error(self.last_controller_error)
            ready = False
        self.signals.ready_changed.emit(ready)
        if ready:
            self.schedule_hotkeys()
            if self._start_live_after_initial_start:
                self.toggle_live()
        self._start_live_after_initial_start = False

    def _refresh_hotkeys(self) -> None:
        if sys.platform != "darwin":
            self.schedule_hotkeys()

    def schedule_hotkeys(self) -> None:
        QTimer.singleShot(250, self, self._start_hotkeys_safely)

    def _start_hotkeys_safely(self) -> None:
        if self._shutting_down:
            return
        if sys.platform == "darwin":
            self.support_log.add(
                "warning",
                "Global hotkeys are disabled on macOS because the current native "
                "listener is unstable. Compact controls were opened instead.",
            )
            self.show_compact_controls(persist=False)
            self.set_status("Ready; use compact controls (macOS hotkeys disabled)")
            return
        try:
            self.start_hotkeys()
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self.show_error(f"Unable to register hotkeys: {error}")

    def start_hotkeys(self) -> None:
        read_hotkey = get_hotkey(self.settings)
        live_hotkey = get_live_hotkey(self.settings)
        validate_hotkey_assignments(
            {
                "Read once": read_hotkey,
                "Live reading": live_hotkey,
            }
        )
        listener = keyboard.GlobalHotKeys(
            {
                read_hotkey: self.signals.read_requested.emit,
                live_hotkey: self.signals.live_requested.emit,
            }
        )
        try:
            listener.start()
        except BaseException as error:
            attempt_cleanup(
                listener.stop,
                description="Hotkey listener cleanup",
                primary_error=error,
            )
            raise
        previous = self.hotkey_listener
        self.hotkey_listener = listener
        if previous is not None:
            previous.stop()

    def read_once(self) -> None:
        if (
            QApplication.activeModalWidget() is not None
            or not self._runtime_control_state().can_read
        ):
            return
        if not self._audio_runtime_available():
            return
        self.voice_auditions.suspend()
        if self._runtime_control_state().indexed_current:
            self.controller.read_indexed_current_dialogue()
        else:
            self.controller.read_once()
        self._apply_controller_action_state()

    def _toggle_presented_reading(self) -> bool:
        if self._presented_runtime_state.transition == "stopping":
            return False
        if self._presented_runtime_state.active:
            self._request_stop_reading()
            return False
        return self.toggle_live()

    def toggle_speech_pause(self) -> None:
        if (
            QApplication.activeModalWidget() is not None
            or not self._presented_runtime_state.can_pause
            or not self._runtime_control_state().can_pause
        ):
            return
        self.set_speech_paused(self.controller.toggle_speech_pause())

    def toggle_live(self) -> bool:
        if self._shutting_down:
            return False
        state = self._runtime_control_state()
        if state.transition == "stopping":
            return False
        if state.active or state.transition == "starting":
            self._request_stop_reading()
            return False
        if self._controller_busy or self.narrator_dialog is not None:
            return False
        if QApplication.activeModalWidget() is not None:
            self.set_status("Close the dialog before reading")
            return False
        if not self._audio_runtime_available():
            return False
        return self._start_live_with_available_scope()

    def choose_sequence_position(self) -> bool:
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

    def choose_expected_sequence_event(self) -> bool:
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

    def set_sequence_status(self, status: LiveSequenceStatus) -> None:
        self.dashboard.set_sequence_status(status)
        self.compact_controller.set_sequence_status(status)
        manual = bool(
            is_live_sequence_audio_mode(getattr(status, "mode", "off"))
            and getattr(status, "state", "unavailable") != "unavailable"
        )
        candidate_count = int(getattr(status, "expected_candidate_count", 0))
        self.sequence_resync_action.setVisible(manual)
        self.sequence_expected_action.setVisible(manual)
        controls_available = (
            self._controller_ready
            and not self._controller_busy
            and not self._shutting_down
            and self.narrator_dialog is None
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

    def _toggle_controller_live(self) -> bool:
        self.voice_auditions.suspend()
        running = self.controller.toggle_live()
        self.signals.live_changed.emit(running)
        return running

    def _start_live_with_available_scope(
        self, *, allow_scope_bootstrap: bool = True
    ) -> bool:
        unresolved = getattr(self.controller, "unresolved_live_speakers", None)
        result = unresolved() if callable(unresolved) else ()
        if result is None:
            self.signals.live_changed.emit(False)
            if allow_scope_bootstrap:
                self._identify_live_scope_then_start()
            else:
                self.set_status(
                    "Live reading could not start: the current story line was "
                    "not identified"
                )
            return False
        return self._toggle_controller_live()

    def _identify_live_scope_then_start(self) -> bool:
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
        self.voice_auditions.suspend()
        self._live_scope_generation = self._lifecycle_generation
        self._live_scope_cancellation = Event()
        self._live_scope_target = self.controller.capture_target
        self._live_scope_settings = self.controller.settings
        self._live_scope_observation_serial = self.controller.observation_serial
        self._live_scope_window = getattr(self._live_scope_target, "last_window", None)
        self.set_status("Identifying the current story chapter...")
        self.live_scope_runner.start(
            identify, cancellation=self._live_scope_cancellation
        )
        self._apply_controller_action_state()
        return False

    def _cancel_live_scope(self) -> None:
        pending = self._live_scope_generation is not None
        if self._live_scope_cancellation is not None:
            self._live_scope_cancellation.set()
        self._live_scope_cancellation = None
        self._live_scope_generation = None
        self._live_scope_target = None
        self._live_scope_settings = None
        self._live_scope_observation_serial = None
        self._live_scope_window = None
        self.live_scope_focus_timer.stop()
        if pending or self.live_scope_runner.active:
            self.live_scope_runner.cancel()

    def _wait_for_live_scope_focus(self) -> None:
        self.set_status(
            "Waiting for the selected game window: switch to the game to start reading."
        )
        self.live_scope_focus_timer.start()
        self._apply_controller_action_state()

    def _live_scope_request_refusal(self) -> str | None:
        if (
            self._live_scope_cancellation is None
            or self._live_scope_cancellation.is_set()
            or self.controller.observation_serial != self._live_scope_observation_serial
        ):
            return "cancelled"
        if (
            self.controller.capture_target is not self._live_scope_target
            or self.controller.settings is not self._live_scope_settings
            or (
                self._live_scope_window is not None
                and getattr(self._live_scope_target, "last_window", None)
                != self._live_scope_window
            )
        ):
            return "target-changed"
        return None

    def _retry_live_scope_when_focused(self) -> None:
        if not self._lifecycle_is_current(self._live_scope_generation):
            self._cancel_live_scope()
            self._apply_controller_action_state()
            return
        refusal = self._live_scope_request_refusal()
        if refusal is not None:
            self.controller.live_scope_identification_failure = refusal
            self._live_scope_finished(False, None)
            return
        try:
            focused = self.controller._is_game_focused()
            refusal = self._live_scope_request_refusal()
            if refusal is not None:
                self.controller.live_scope_identification_failure = refusal
                self._live_scope_finished(False, None)
                return
            self._live_scope_window = getattr(
                self._live_scope_target, "last_window", None
            )
        except Exception as error:
            self._live_scope_finished(None, error)
            return
        if not focused:
            self.live_scope_focus_timer.start()
            return
        self.set_status("Identifying the current story chapter...")
        self.live_scope_runner.start(
            self.controller.identify_live_scope,
            cancellation=self._live_scope_cancellation,
        )

    def _live_scope_finished(
        self, identified: bool | None, error: Exception | None
    ) -> None:
        generation = self._live_scope_generation
        if not self._lifecycle_is_current(generation):
            self._cancel_live_scope()
            self._apply_controller_action_state()
            return
        failure = getattr(self.controller, "live_scope_identification_failure", None)
        if error is None and identified is True:
            refusal = self._live_scope_request_refusal()
            if refusal is not None:
                identified, failure = False, refusal
            else:
                try:
                    focused = self.controller._is_game_focused()
                    refusal = self._live_scope_request_refusal()
                    if refusal is not None:
                        identified, failure = False, refusal
                    elif not focused:
                        identified, failure = False, "focus-lost"
                        record_diagnostic_event(
                            "runtime",
                            "live-scope-bootstrap",
                            outcome="focus-lost",
                            phase="before-start",
                        )
                except Exception as focus_error:
                    error = focus_error
        if error is None and failure in {"focus-wait", "focus-lost"}:
            self._wait_for_live_scope_focus()
            return
        self._cancel_live_scope()
        self._apply_controller_action_state()
        if error is not None:
            self.show_error(f"Unable to identify the current story chapter: {error}")
            self.signals.live_changed.emit(False)
            return
        if identified is not True:
            match_result = getattr(
                self.controller,
                "live_scope_identification_match_result",
                None,
            )
            if match_result:
                details = getattr(
                    self.controller,
                    "live_scope_identification_diagnostics",
                    {},
                )
                details = dict(details) if isinstance(details, dict) else {}
                details["match_result"] = match_result
                self.support_log.add(
                    "live-scope",
                    f"Initial story matcher result: {match_result}",
                    **details,
                    **collect_active_content_identity(self.settings),
                )
            if failure == "no-dialog-text":
                message = (
                    "Live reading could not start: no dialog text was recognized "
                    "in the selected game window"
                )
            elif failure == "capture-target-unavailable":
                message = (
                    "Live reading could not start: select a game window in Settings"
                )
            elif failure == "target-changed":
                message = "Live reading cancelled: the selected game window changed"
            elif failure == "cancelled":
                message = "Live reading start cancelled"
            elif failure == "story-line-no-match":
                message = (
                    "The visible dialogue does not match the prepared story "
                    "at this point."
                )
                self._offer_story_match_recovery(message)
                return
            else:
                message = (
                    "Live reading could not start: keep a complete dialog line "
                    "visible and try again"
                )
            self.set_status(message)
            self.signals.live_changed.emit(False)
            return
        self._start_live_with_available_scope(allow_scope_bootstrap=False)

    def _offer_story_match_recovery(self, message: str) -> bool:
        generation = self._lifecycle_generation
        self.show_dashboard()
        self.dashboard.show_reading()
        can_read_visible_dialogue = self.controller.can_start_live_from_ocr()
        prompt, continue_button, stories_button, _cancel_button = (
            build_story_match_recovery_prompt(
                message,
                self.dashboard,
                can_read_visible_dialogue=can_read_visible_dialogue,
            )
        )
        cancelled = False

        def reading_changed(_running: bool) -> None:
            nonlocal cancelled
            cancelled = True
            prompt.reject()

        self.signals.live_changed.connect(reading_changed)
        try:
            prompt.exec()
            clicked = prompt.clickedButton()
        finally:
            self.signals.live_changed.disconnect(reading_changed)
            prompt.deleteLater()
        if cancelled or not self._lifecycle_is_current(generation):
            return False
        if clicked is continue_button:
            if not can_read_visible_dialogue:
                return self._identify_live_scope_then_start()
            self.voice_auditions.suspend()
            running = bool(self.controller.start_live_from_ocr())
            self.signals.live_changed.emit(running)
            self.set_status(
                "Reading visible dialogue. Prepared recordings remain available "
                "for later matching lines."
                if running
                else "Live reading could not start from the visible dialogue."
            )
            return running
        if clicked is stories_button:
            self.dashboard.show_stories()
            self.set_status(
                "Stories is open: select and prepare the story currently shown in "
                "the game, then return to Reading and start again."
            )
        else:
            self.set_status(
                "Live reading remains stopped: current story line was not matched."
            )
        self.signals.live_changed.emit(False)
        return False

    def emergency_stop(self) -> None:
        self.resume_live_after_unknown_mapping = False
        self._configuration_after_apply = None
        if self.unknown_speaker_prompt is not None:
            self.unknown_speaker_prompt.close()
        if self.narrator_dialog is not None:
            self._resume_live_after_narrator = False
            self.narrator_dialog.reject()
        self._cancel_live_scope()
        self.controller.emergency_stop()
        self.signals.live_changed.emit(False)
        self.signals.speech_paused_changed.emit(False)

    def _request_stop_reading(self) -> None:
        self.resume_live_after_unknown_mapping = False
        self._configuration_after_apply = None
        if self.unknown_speaker_prompt is not None:
            self.unknown_speaker_prompt.close()
        if self.narrator_dialog is not None:
            self._resume_live_after_narrator = False
            self.narrator_dialog.reject()
        self._cancel_live_scope()
        reader = self.controller.live_reader
        if reader is not None:
            reader.stop()
        self._live_stop_generation = self._lifecycle_generation
        self._live_stop_continuation = None
        self._live_stop_wait_for_speech = True
        self._modal_stop_pending = True
        self.signals.live_changed.emit(False)
        self.signals.speech_paused_changed.emit(False)
        self.set_status("Stopping reading and speech...")
        self.live_stop_runner.start(
            self.session_owner.run,
            self._lifecycle_generation,
            lambda _cancellation: self._stop_reader_and_wait(reader),
            False,
        )
        self._apply_controller_action_state()

    def _stop_reader_and_wait(self, reader: LiveDialogReader | None) -> bool:
        started = perf_counter()
        outcome = "failed"
        try:
            self.controller.emergency_stop()
            outcome = "complete"
        finally:
            record_background_operation(
                "live-stop-interrupt", (perf_counter() - started) * 1000, outcome
            )
        if reader is not None:
            started = perf_counter()
            outcome = "failed"
            try:
                if callable(getattr(type(self.controller), "wait_for_reading", None)):
                    self.controller.wait_for_reading(timeout_seconds=5.0)
                else:
                    reader.wait(timeout_seconds=5.0)
                outcome = "complete"
            finally:
                record_background_operation(
                    "live-stop-quiescence", (perf_counter() - started) * 1000, outcome
                )
        return True

    def calibrate(self) -> None:
        if self._calibration_capture_pending:
            return
        if self.calibration_overlay is not None:
            self.set_status("Calibration is already open; finish or cancel it first")
            return
        if self._controller_busy or self._shutting_down:
            self.set_status("Controller reconfiguration is already in progress")
            return
        if self.live_stop_runner.active:
            self.set_status("Live capture is already stopping; please wait")
            return
        if self.controller.is_live_running is True:
            if self.diagnostics_dialog is not None:
                self.diagnostics_dialog.set_calibration_pending(True)
            started = self._stop_live_then(
                self.calibrate,
                "Stopping reading before capture calibration...",
                wait_for_speech=False,
            )
            if not started and self.diagnostics_dialog is not None:
                self.diagnostics_dialog.set_calibration_pending(False)
            return
        self._calibration_capture_pending = True
        self._cancel_diagnostics_refresh()
        self._calibration_visibility = CaptureWindowVisibility(
            self, may_restore=lambda _window: not self._shutting_down
        )
        self.set_status("Capturing game window for calibration...")
        visibility = self._calibration_visibility
        QTimer.singleShot(
            200, self, lambda: self._start_calibration_capture(visibility)
        )

    def _start_calibration_capture(self, visibility: CaptureWindowVisibility) -> None:
        if (
            self._shutting_down
            or not self._calibration_capture_pending
            or self._calibration_visibility is not visibility
        ):
            return
        self.calibration_capture_runner.start(
            lambda: (
                self._capture_calibration_background()
                if not visibility.finished.is_set()
                else None
            )
        )

    def _capture_calibration_background(
        self,
    ) -> tuple[WindowGeometry | None, Image.Image]:
        geometry = self.controller.get_capture_geometry()
        return geometry, capture_calibration_background(geometry)

    def _calibration_capture_finished(
        self,
        result: tuple[WindowGeometry | None, Image.Image] | None,
        error: Exception | None,
    ) -> None:
        self._calibration_capture_pending = False
        if self._shutting_down:
            return
        if error is not None or result is None:
            self.restore_control_window()
            self.show_error(f"Unable to capture a calibration preview: {error}")
            return
        self._open_calibration_overlay(*result)

    def _open_calibration_overlay(
        self, geometry: WindowGeometry | None, background: Image.Image
    ) -> None:
        try:
            overlay = show_calibration_overlay(
                geometry,
                background=background,
                save_region=self._save_calibration_region,
                capture_region=self.controller._capture_region(),
            )
        except Exception as error:
            self.restore_control_window()
            self.show_error(f"Unable to capture a calibration preview: {error}")
            return
        self.calibration_overlay = overlay
        overlay.closed.connect(
            lambda closed_overlay=overlay: self._calibration_overlay_closed(
                closed_overlay
            )
        )
        overlay.save_failed.connect(self.set_status)

    def _calibration_overlay_closed(self, overlay: DialogRegionOverlay) -> None:
        is_current = self.calibration_overlay is overlay
        if is_current:
            self.calibration_overlay = None
        overlay.deleteLater()
        if is_current and not self._shutting_down:
            self.restore_control_window()

    def _cancel_calibration(self) -> None:
        self._calibration_capture_pending = False
        self.calibration_capture_runner.cancel()
        if self.calibration_overlay is not None:
            self.calibration_overlay.close()
        if self._calibration_visibility is not None:
            self._calibration_visibility.restore()
            self._calibration_visibility = None

    def _save_calibration_region(self, region: DialogRegion) -> None:
        profile_id = self.settings.active_profile_id
        profile = self.profile_store.get(profile_id) if profile_id else None
        if profile is not None:
            self.profile_store.update_region(profile.id, region)
        else:
            save_dialog_region(region, get_dialog_region_file())

    def restore_control_window(self) -> None:
        visibility = self._calibration_visibility
        self._calibration_visibility = None
        if visibility is None:
            return
        visibility.restore()
        dialog = self.diagnostics_dialog
        if dialog is not None and isValid(dialog) and dialog.isVisible():
            dialog.request_refresh()

    def open_diagnostics(self) -> None:
        if self.diagnostics_dialog is None:
            self.diagnostics_dialog = DiagnosticsDialog()
            self.diagnostics_dialog.refresh_requested.connect(self.refresh_diagnostics)
            self.diagnostics_dialog.calibration_requested.connect(self.calibrate)
            self.diagnostics_dialog.remediation_requested.connect(
                self._run_diagnostics_remediation
            )
            self.diagnostics_dialog.closed.connect(
                lambda dialog=self.diagnostics_dialog: self._diagnostics_closed(dialog)
            )
        if self.diagnostics_dialog.refresh_in_flight:
            return
        snapshot = self.controller.get_latest_diagnostic()
        if snapshot is not None:
            self.diagnostics_dialog.set_snapshot(snapshot)
        warnings = macos_permission_warnings()
        self.diagnostics_dialog.set_permission_warnings(
            warnings,
            remediation=("macos-permissions", "Open macOS permissions")
            if warnings
            else None,
        )
        self.diagnostics_dialog.show()
        self.diagnostics_dialog.raise_()
        self.diagnostics_dialog.activateWindow()

    def refresh_diagnostics(self) -> None:
        self._cancel_diagnostics_refresh()
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

        generation = self.diagnostics_refresh_generation
        QTimer.singleShot(
            200,
            self,
            lambda: self._capture_diagnostic_snapshot(generation),
        )

    def _capture_diagnostic_snapshot(self, generation: int) -> None:
        if self._shutting_down or not self._diagnostics_refresh_is_current(generation):
            return
        self.diagnostics_refresh_runner.start(
            self.controller.inspect_current_dialog,
            notify=False,
            publish=False,
        )

    def _diagnostics_closed(self, dialog: DiagnosticsDialog) -> None:
        if self.diagnostics_dialog is dialog:
            self._cancel_diagnostics_refresh()

    def _cancel_diagnostics_refresh(self) -> None:
        self.diagnostics_refresh_generation += 1
        self.diagnostics_refresh_runner.cancel()

    def _diagnostics_refresh_is_current(self, generation: int) -> bool:
        return bool(
            generation == self.diagnostics_refresh_generation
            and self.diagnostics_dialog is not None
            and self.diagnostics_dialog.refresh_in_flight
        )

    def _diagnostics_refresh_finished(
        self, snapshot: DiagnosticSnapshot | None, error: Exception | None
    ) -> None:
        if not self._diagnostics_refresh_is_current(
            self.diagnostics_refresh_generation
        ):
            return
        if error is not None:
            self.set_diagnostics_error(diagnostic_error_guidance(error))
        elif snapshot is None:
            self.set_diagnostics_error("Diagnostic capture returned no result")
        else:
            self.update_diagnostics_snapshot(
                self.controller.commit_diagnostic_snapshot(snapshot), manual=True
            )

    def update_diagnostics_snapshot(
        self, snapshot: DiagnosticSnapshot, *, manual: bool = False
    ) -> None:
        self.dashboard.set_diagnostic(snapshot)
        if self.diagnostics_dialog is not None and (
            manual or not self.diagnostics_dialog.refresh_in_flight
        ):
            self.diagnostics_dialog.set_snapshot(snapshot)

    def set_diagnostics_error(self, message: str) -> None:
        if self.diagnostics_dialog is not None:
            self.diagnostics_dialog.set_warning(
                message,
                remediation=diagnostic_remediation(message),
            )

    def _run_diagnostics_remediation(self, remediation: str) -> None:
        if remediation == "macos-permissions":
            self.open_macos_permissions()
        elif remediation == "settings":
            self.open_settings()

    def run_onboarding(self) -> None:
        if self._controller_busy or self._shutting_down or self.live_stop_runner.active:
            self.set_status(
                "Reading controls are updating. Try setup again when ready."
            )
            return
        if self.narrator_dialog is not None:
            self.dashboard.show_voices()
            return
        if (
            self.pregeneration_dialog is not None
            and self.pregeneration_dialog.has_pending_work()
        ):
            self.set_status("Finish or cancel story preparation before reading setup.")
            self.dashboard.show_stories()
            return
        if self.onboarding_wizard is not None:
            self.onboarding_wizard.show()
            self.onboarding_wizard.raise_()
            self.onboarding_wizard.activateWindow()
            return

        wizard = OnboardingWizard(
            self.settings,
            reading_setup=True,
            save_region=self._save_calibration_region,
            capture_region=self.controller._capture_region(),
        )
        self.onboarding_wizard = wizard
        self._onboarding_preview_settings = None
        wizard.voices_requested.connect(lambda: self._open_onboarding_voices(wizard))
        wizard.settings_requested.connect(
            lambda: self._open_onboarding_settings(wizard)
        )
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

    def _open_onboarding_voices(self, wizard: OnboardingWizard) -> None:
        self._onboarding_voice_wizard = wizard
        self.open_voice_previews()
        if self.narrator_dialog is None:
            self._resume_onboarding_after_voices()

    def _resume_onboarding_after_voices(self) -> None:
        wizard = self._onboarding_voice_wizard
        self._onboarding_voice_wizard = None
        if wizard is not None and wizard is self.onboarding_wizard:
            wizard.resume_after_editor(self.settings)

    def _open_onboarding_settings(self, wizard: OnboardingWizard) -> None:
        self.open_settings()
        if wizard is self.onboarding_wizard:
            wizard.resume_after_editor(self.settings)

    def run_onboarding_test(self, settings: AppSettings) -> None:
        if self._controller_busy or self._shutting_down or self.live_stop_runner.active:
            self.signals.onboarding_test_finished.emit(
                False, "Reading controls are updating. Try the test again when ready."
            )
            return
        if self._calibration_capture_pending or self.calibration_overlay is not None:
            self.signals.onboarding_test_finished.emit(
                False, "Finish or cancel calibration before testing speech."
            )
            return
        model_name = settings.tts_model
        if settings.speech_backend == "coqui-xtts" and not model_name:
            self.signals.onboarding_test_finished.emit(
                False, "Select a Coqui model before testing speech."
            )
            return
        cancel_event = Event()
        generation = self._begin_controller_lifecycle(cancel_event)
        self._onboarding_test_generation = generation
        self.onboarding_cancel_event = cancel_event
        self.onboarding_test_runner.start(
            self._run_owned_onboarding_test,
            generation,
            settings,
            model_name,
            cancel_event,
        )

    def _run_onboarding_check(
        self,
        settings: AppSettings,
        model_name: str | None,
        cancel_event: Event,
        generation: int,
    ) -> tuple[bool, str]:
        started = preview_succeeded = False

        try:
            if cancel_event.is_set():
                return False, "OCR-to-speech test cancelled."
            preparation_error = self._prepare_onboarding_controller(
                settings, model_name, cancel_event
            )
            if preparation_error is not None:
                return False, preparation_error
            started = bool(self.controller.start())
            if cancel_event.is_set():
                return False, "OCR-to-speech test cancelled."
            if not started:
                return (
                    False,
                    self.last_controller_error
                    or "The speech engine could not be initialized.",
                )
            character, text = self.controller.test_current_dialog(
                capture_context=lambda: self._onboarding_capture(
                    generation, cancel_event
                )
            )
            if cancel_event.is_set():
                return False, "OCR-to-speech test cancelled."
            preview_succeeded = True
            return (
                True,
                f"Success. Recognized {character}: {text}",
            )
        except Exception as error:
            if cancel_event.is_set():
                return False, "OCR-to-speech test cancelled."
            return False, format_runtime_error(error)
        finally:
            try:
                if cancel_event.is_set() or (started and not preview_succeeded):
                    self.controller.shutdown()
            except Exception as error:
                self.report_controller_error(error)

    def _prepare_onboarding_controller(
        self,
        settings: AppSettings,
        model_name: str | None,
        cancel_event: Event,
    ) -> str | None:
        previous_settings = self.controller.settings
        try:
            self.controller.apply_settings(settings)
        finally:
            if self.controller.settings is not previous_settings:
                self._onboarding_preview_settings = self.controller.settings
        if cancel_event.is_set():
            return "OCR-to-speech test cancelled."
        if settings.speech_backend == "coqui-xtts" and model_name:
            download_error = self._download_onboarding_model(model_name, cancel_event)
            if download_error is not None:
                return download_error
        if cancel_event.is_set():
            return "OCR-to-speech test cancelled."
        self.last_controller_error = None
        self.controller.prepare_startup()
        if cancel_event.is_set():
            self.controller.request_shutdown()
            return "OCR-to-speech test cancelled."
        return None

    def _download_onboarding_model(
        self, model_name: str, cancel_event: Event
    ) -> str | None:
        try:
            self.controller.model_assets.download(
                model_name,
                progress=self.signals.onboarding_test_progress.emit,
                cancel_event=cancel_event,
            )
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, ModelDownloadCancelled)
                else f"Model download or verification failed: {error}"
            )
            return message
        return None

    def _run_owned_onboarding_test(
        self,
        generation: int,
        settings: AppSettings,
        model_name: str | None,
        cancel_event: Event,
    ) -> tuple[bool, str]:
        return self.session_owner.run(
            generation,
            lambda _cancellation: self._run_onboarding_check(
                settings, model_name, cancel_event, generation
            ),
            (False, "OCR-to-speech test cancelled."),
        )

    @contextmanager
    def _onboarding_capture(self, generation: int, cancelled: Event) -> Iterator[None]:
        if QThread.currentThread() == self.thread():
            raise RuntimeError("Setup capture must run in a background worker")
        request = _OnboardingCaptureRequest(generation, cancelled)
        self.signals.onboarding_capture_requested.emit(request)
        deadline = perf_counter() + 5.0
        with cleanup_on_exit(
            lambda: self._release_onboarding_capture(request),
            description="Setup capture control restoration",
        ):
            while not request.ready.wait(0.05):
                if cancelled.is_set() or not self.session_owner.is_current(generation):
                    raise RuntimeError("OCR-to-speech test cancelled.")
                if perf_counter() >= deadline:
                    raise RuntimeError(
                        "Timed out preparing unobstructed dialogue capture"
                    )
            if cancelled.is_set() or not self.session_owner.is_current(generation):
                raise RuntimeError("OCR-to-speech test cancelled.")
            yield
        if cancelled.is_set() or not self.session_owner.is_current(generation):
            raise RuntimeError("OCR-to-speech test cancelled.")

    def _release_onboarding_capture(self, request: _OnboardingCaptureRequest) -> None:
        request.finished.set()
        self.signals.onboarding_capture_released.emit(request)
        if not request.restored.wait(5.0):
            raise RuntimeError("Timed out restoring setup controls after capture")

    def _hide_onboarding_capture_windows(
        self, request: _OnboardingCaptureRequest
    ) -> None:
        if (
            request.finished.is_set()
            or request.cancelled.is_set()
            or not self._lifecycle_is_current(request.generation)
        ):
            request.ready.set()
            return
        request.wizard = self.onboarding_wizard
        request.visibility = CaptureWindowVisibility(
            self,
            may_restore=lambda window: (
                not self._shutting_down
                and (window is not request.wizard or window is self.onboarding_wizard)
            ),
        )
        self._onboarding_capture_request = request
        if request.visibility.windows:
            QTimer.singleShot(200, self, request.ready.set)
        else:
            request.ready.set()

    def _restore_onboarding_capture_windows(
        self, request: _OnboardingCaptureRequest
    ) -> None:
        try:
            if self._onboarding_capture_request is not request:
                return
            self._onboarding_capture_request = None
            if request.visibility is not None:
                request.visibility.restore()
        finally:
            request.restored.set()

    def _onboarding_test_finished(
        self, result: tuple[bool, str] | None, error: Exception | None
    ) -> None:
        generation = self._onboarding_test_generation
        self._onboarding_test_generation = None
        if not self._lifecycle_is_current(generation):
            return
        self._finish_controller_lifecycle()
        if error is not None:
            result = False, format_runtime_error(error)
        self.set_ready(self.controller.is_ready)
        assert result is not None
        self.signals.onboarding_test_finished.emit(*result)

    def cancel_onboarding_download(self) -> None:
        self.session_owner.cancel()
        self.set_status("Cancelling setup test in background...")

    def _create_settings_dialog(self) -> SettingsDialog:
        return SettingsDialog(
            self.settings, voice_library=self.controller.voice_library
        )

    def _create_asset_manager_dialog(self) -> AssetManagerDialog:
        return AssetManagerDialog(self.settings)

    def _configure_macos_launch_at_login(self, enabled: bool) -> None:
        configure_macos_launch_at_login(enabled)

    def show_dashboard(self) -> None:
        self.compact_controller.hide()
        self.dashboard.show()
        self.dashboard.raise_()
        self.dashboard.activateWindow()
        self._save_compact_preference(False)

    def show_compact_controls(self, *, persist: bool = True) -> None:
        geometry = None
        try:
            geometry = self.controller.get_capture_geometry()
        except WindowCaptureError:
            pass
        if not isinstance(geometry, WindowGeometry):
            geometry = None
        self.dashboard.hide()
        self.compact_controller.show_for_game(geometry)
        if persist:
            self._save_compact_preference(True)

    def notify_background_mode(self) -> None:
        if self._background_notification_shown:
            return
        self._background_notification_shown = True
        self._show_tray_notification(
            application_name,
            "VNTTS is still running in the background. Use the tray/menu-bar icon "
            "to reopen it or quit.",
            QSystemTrayIcon.MessageIcon.Information,
            recovery="full-controls",
        )

    def open_pregeneration(
        self, *, show: bool = True
    ) -> OfflineAudioPreparationDialog | None:
        if self._shutting_down or self._quit_requested:
            return None
        if (
            show
            and self.narrator_dialog is not None
            and self.pregeneration_dialog is None
        ):
            self.dashboard.show_voices()
            return None
        if self._needs_modal_stop():
            self._stop_live_then(
                self.open_pregeneration, "Stopping reading before story preparation..."
            )
            return None
        if self.pregeneration_dialog is not None:
            self._refresh_preparation_settings()
            self.show_dashboard()
            self.dashboard.show_stories()
            return self.pregeneration_dialog
        if self._controller_busy or self._shutting_down:
            self.set_status("Controller reconfiguration is already in progress")
            return None
        audition_session = self.voice_auditions.session()
        dialog = OfflineAudioPreparationDialog(
            self.settings,
            audition_service=audition_session,
            generator=OfflineGenerationWorker(
                backend_factory=self.moss_runtime.benchmark_backend,
                retain_isolated_backend=True,
            ),
            game_narrator_chooser=self._open_preparation_narrator,
            automatic_activation=True,
            parent=self.dashboard,
        )
        self.pregeneration_dialog = dialog
        dialog.offlinePreferencesChanged.connect(self._save_offline_preferences)
        dialog.finished.connect(self._pregeneration_finished)
        dialog.readingRequested.connect(self._read_prepared_story)
        dialog.preparationRequested.connect(self._remember_preparation_context)
        dialog.preparationRequested.connect(
            lambda: audition_session.warmup(
                resolve_pregeneration_settings(dialog.settings)
            )
        )
        dialog.packReady.connect(self._activate_ready_preparation)
        dialog.activityChanged.connect(
            self._preparation_activity_changed, Qt.ConnectionType.QueuedConnection
        )
        self.dashboard.embed_preparation(dialog, show=show)
        return dialog

    def _save_offline_preferences(self, selected: AppSettings) -> None:
        self.voice_auditions.cancel_warmup()
        candidate = self.settings.updated(
            offline_speech_backend=selected.offline_speech_backend,
            offline_tts_model=selected.offline_tts_model,
            offline_tts_profile=selected.offline_tts_profile,
        )
        try:
            self._save_settings_candidate(candidate)
        except OSError as error:
            self.show_error(f"Unable to save offline engine choice: {error}")
            if self.pregeneration_dialog is not None:
                self.pregeneration_dialog.apply_narrator_settings(self.settings)
            return
        self.settings = candidate
        self.dashboard.set_configuration(candidate)

    def _read_prepared_story(self) -> None:
        if self._controller_ready and self._preparation_runtime_settings() is None:
            self.dashboard.show_reading()
            if not self.controller.is_live_running:
                self.toggle_live()
            return
        self.prepare_reading(start_live=True, prefer_preparation=True)

    def _remember_preparation_context(self) -> None:
        self._preparation_activation_settings = self.settings

    def _activate_ready_preparation(self) -> None:
        dialog = self.pregeneration_dialog
        if dialog is None or self._shutting_down or self._quit_requested:
            return
        if (
            self._controller_busy
            or self.narrator_dialog is not None
            or self.controller.is_live_running is True
        ):
            dialog.defer_activation(
                "Finish the current reading or configuration operation first."
            )
        elif (
            self._preparation_activation_settings is None
            or self._preparation_activation_settings.updated(
                last_main_section=self.settings.last_main_section
            )
            != self.settings
        ):
            dialog.defer_activation(
                "Reading settings changed after this preparation was requested."
            )
        else:
            dialog.accept()

    def _preparation_activity_changed(self, _active: bool) -> None:
        if self._shutting_down:
            return
        if not self._voice_audition_background_available():
            self.voice_auditions.cancel_warmup()
        self._apply_controller_action_state()

    def _pregeneration_finished(self, result: int) -> PregenerationJob | None:
        dialog = self.pregeneration_dialog
        if dialog is None:
            return None
        used_runtime_progress = self._preparation_playback_active()
        self.pregeneration_dialog = None
        self._preparation_activation_settings = None
        self.dashboard.remove_preparation(dialog)
        dialog.deleteLater()
        self._apply_controller_action_state()
        if self._quit_requested:
            # Let dialog completion and native close events unwind before quitting.
            QTimer.singleShot(0, self.application, self.application.quit)
            return None
        if result != QDialog.DialogCode.Accepted:
            if used_runtime_progress:
                self._start_configuration_apply(
                    self.settings,
                    progress_status="Restoring the saved Reading setup...",
                    success_status="Saved Reading setup restored.",
                    restart=self._controller_ready,
                )
            return None
        job = dialog.job()
        pack_result = dialog.pack_result()
        if not isinstance(pack_result, OfflinePackResult):
            pack_result = None
        if pack_result is None:
            self.show_error(
                "Offline preparation finished without a validated game pack"
            )
            return None
        status = (
            f"Prepared audio is active for {pack_result.story_lines} dialogue lines."
        )
        self._start_pregeneration_activation(pack_result, status)
        return job

    def _reload_game_narrator(
        self,
        profile_synced: bool,
        saved_assignment: str | None = None,
        *,
        after_apply: Callable[[], object] | None = None,
    ) -> None:
        suffix = "" if profile_synced else " Active profile could not be updated."
        saved = (
            f"Voice saved for {saved_assignment}."
            if saved_assignment
            else "Voices saved."
        )
        self._start_configuration_apply(
            self.settings,
            progress_status="Applying your selected voices...",
            success_status=f"{saved} Prepared recordings are unchanged.{suffix}",
            restart=self._controller_ready,
            after_apply=after_apply,
        )

    def _start_pregeneration_activation(
        self,
        pack_result: OfflinePackResult,
        success_status: str,
    ) -> None:
        cancellation = Event()
        generation = self._begin_controller_lifecycle(cancellation)
        self._pregeneration_activation_generation = generation
        self._pregeneration_activation_cancellation = cancellation
        self._pregeneration_activation_restore_runtime.set()
        self._pregeneration_activation_status = success_status
        self.set_status("Activating prepared offline audio in the background...")
        self.pregeneration_activation_runner.start(
            self.session_owner.activate,
            generation,
            self.pregeneration_activator,
            self.settings,
            pack_result,
            cancellation,
            self._pregeneration_activation_restore_runtime,
        )

    def _pregeneration_activation_finished(
        self, result: object, error: Exception | None
    ) -> None:
        generation = self._pregeneration_activation_generation
        self._pregeneration_activation_generation = None
        self._pregeneration_activation_cancellation = None
        if not self._lifecycle_is_current(generation):
            return
        self._finish_controller_lifecycle()
        if error is not None:
            if getattr(error, "rollback_failed", False):
                message = (
                    "Unable to activate prepared offline audio or restore the running "
                    "configuration; restart VNTTS to load the saved previous pack. "
                )
            else:
                message = (
                    "Unable to activate prepared offline audio; the previous pack "
                    "remains active. "
                )
            self.show_error(
                f"{message}{error}. Your audio remains saved. Open Stories and continue preparation to retry activation."
            )
            return
        if not isinstance(result, OfflinePackActivationResult):
            self.show_error(
                "Unable to activate prepared offline audio: activation result is invalid"
            )
            return
        self.settings = result.settings
        self.dashboard.set_configuration(self.settings)
        self._update_auto_advance_action()
        sequence_controls_visible = bool(
            is_live_sequence_audio_mode(self.settings.live_sequence_mode)
            and self.settings.live_sequence_plan
            and self.settings.story_index
        )
        self.sequence_resync_action.setVisible(sequence_controls_visible)
        self.sequence_expected_action.setVisible(sequence_controls_visible)
        self._controller_ready = bool(self.controller.is_ready)
        self._apply_controller_action_state()
        if self.readiness_dialog is not None:
            self.readiness_dialog.update_settings(self.settings)
        status = self._pregeneration_activation_status
        self._pregeneration_activation_status = None
        self.show_dashboard()
        self.dashboard.show_reading()
        next_action = (
            self.dashboard.live_button
            if self._controller_ready
            else self.dashboard.prepare_reading_button
        )
        self.set_status(
            f"{status or 'Prepared audio is active.'} "
            f"Next: click {next_action.text()} with the story open in the game."
        )
        next_action.setFocus()

        self._save_main_section("reading")

    def open_readiness(self) -> None:
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
            self.readiness_dialog.reading_requested.connect(
                self._open_reading_from_readiness
            )
        else:
            self.readiness_dialog.update_settings(self.settings)
        self.readiness_dialog.show()
        self.readiness_dialog.raise_()
        self.readiness_dialog.activateWindow()

    def _open_reading_from_readiness(self) -> None:
        self.dashboard.show_reading()
        self.show_dashboard()

    def open_profiles(self) -> None:
        if self._controller_busy or self._shutting_down:
            self.set_status("Controller reconfiguration is already in progress")
            return
        if not self._recover_active_profile():
            return
        dialog = GameProfilesDialog(
            self.settings,
            self.profile_store,
            self.correction_store,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        candidate = dialog.settings()
        profile = self.profile_store.get(candidate.active_profile_id)
        if profile is None:
            self.show_error("The selected game profile is no longer available.")
            return
        try:
            path = self._save_settings_candidate(candidate)
        except OSError as error:
            self.show_error(f"Unable to apply the selected profile: {error}")
            return
        self.settings = candidate
        self.dashboard.set_configuration(self.settings)
        self._refresh_preparation_settings()
        self._update_auto_advance_action()
        sequence_controls_visible = bool(
            is_live_sequence_audio_mode(self.settings.live_sequence_mode)
            and self.settings.live_sequence_plan
            and self.settings.story_index
        )
        self.sequence_resync_action.setVisible(sequence_controls_visible)
        self.sequence_expected_action.setVisible(sequence_controls_visible)
        profile_id = self.settings.active_profile_id
        profile = self.profile_store.get(profile_id) if profile_id else None
        self._pending_profile_name = profile.name if profile is not None else None
        generation = self._begin_controller_lifecycle()
        self._profile_restart_generation = generation
        self.set_status(
            f"Applying profile {self._pending_profile_name!r} in background; "
            f"settings saved to {path}"
        )
        self.profile_restart_runner.start(
            self.session_owner.restart,
            generation,
            self.settings,
        )

    def _profile_restart_finished(
        self, ready: bool | None, error: Exception | None
    ) -> None:
        generation = self._profile_restart_generation
        self._profile_restart_generation = None
        if not self._lifecycle_is_current(generation):
            return
        self._finish_controller_lifecycle()
        profile_name = self._pending_profile_name
        self._pending_profile_name = None
        if error is not None:
            self.last_controller_error = f"Profile restart failed: {error}"
            self.set_ready(False)
            return
        self.set_ready(bool(ready))
        if not ready:
            self.set_status("Unable to load the selected profile")
            return
        self.signals.hotkeys_requested.emit()
        self.set_status(f"Profile {profile_name!r} selected")

    def _needs_modal_stop(self) -> bool:
        state = self._runtime_control_state()
        return bool(
            self._modal_stop_pending
            or self.controller.is_live_running is True
            or self.controller.is_one_shot_read_running is True
            or state.speaking
            or state.queued
        )

    def _stop_live_then(
        self,
        continuation: Callable[[], object],
        status: str,
        *,
        wait_for_speech: bool = True,
    ) -> bool:
        if self._shutting_down:
            return False
        if self.live_stop_runner.active:
            self.set_status("Live capture is already stopping; please wait")
            return False
        reader = self.controller.live_reader
        self._lifecycle_generation = self.session_owner.begin()
        self._live_stop_generation = self._lifecycle_generation
        self._live_stop_continuation = continuation
        self._live_stop_wait_for_speech = wait_for_speech
        self._modal_stop_pending = self._modal_stop_pending or wait_for_speech
        self._set_modal_launchers_enabled(False)
        self.set_status(status)
        self.live_stop_runner.start(
            self.session_owner.run,
            self._live_stop_generation,
            lambda _cancellation: self._wait_for_live_reader(reader, wait_for_speech),
            False,
        )
        return True

    def _wait_for_live_reader(
        self, reader: LiveDialogReader | None, wait_for_speech: bool = True
    ) -> bool:
        self.controller.stop_live()
        if callable(getattr(type(self.controller), "wait_for_reading", None)):
            self.controller.wait_for_reading(
                timeout_seconds=5.0, include_speech=wait_for_speech
            )
            return True
        if reader is None:
            return True
        if wait_for_speech:
            reader.wait(timeout_seconds=5.0)
        else:
            reader.wait(timeout_seconds=5.0, include_speech=False)
        return True

    def _live_stop_finished(self, result: object, error: Exception | None) -> None:
        generation = self._live_stop_generation
        self._live_stop_generation = None
        continuation = self._live_stop_continuation
        self._live_stop_continuation = None
        if not self._shutting_down:
            self._apply_controller_action_state()
        if not self._lifecycle_is_current(generation):
            return
        if self.diagnostics_dialog is not None:
            self.diagnostics_dialog.set_calibration_pending(False)
        self._set_modal_launchers_enabled(self._controller_ready)
        if error is not None:
            message = f"Unable to stop live capture: {error}"
            self.set_status(message)
            if continuation == self.calibrate and self.diagnostics_dialog is not None:
                self.diagnostics_dialog.set_warning(message)
            return
        if result is not True:
            return
        if self._live_stop_wait_for_speech:
            self._modal_stop_pending = False
        self.signals.live_changed.emit(False)
        if continuation is not None:
            QTimer.singleShot(
                0,
                self,
                lambda: (
                    continuation() if self._lifecycle_is_current(generation) else None
                ),
            )
        else:
            self.set_status("Reading and speech stopped")

    def open_corrections(self) -> None:
        profile_id = self.settings.active_profile_id
        profile = self.profile_store.get(profile_id) if profile_id else None
        self.correction_store = OCRCorrectionStore.load(self.correction_store.path)
        dialog = OCRCorrectionsDialog(
            self.settings.active_profile_id,
            profile.name if profile is not None else None,
            self.correction_store,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.controller.refresh_corrections()
        self.set_status("OCR corrections saved")

    def open_ocr_review(self) -> None:
        profile_id = self.settings.active_profile_id
        profile = self.profile_store.get(profile_id) if profile_id else None
        self.correction_store = OCRCorrectionStore.load(self.correction_store.path)
        dialog = OCRReviewDialog(
            self.settings.ocr_diagnostics_directory,
            self.correction_store,
            self.settings.active_profile_id,
            profile.name if profile is not None else None,
            self.controller.refresh_corrections,
        )
        dialog.exec()
        self.set_status("OCR review closed")

    def open_voice_previews(
        self,
        *,
        character: str | None = None,
        resume_live: bool | None = None,
        recovery: bool = False,
        for_preparation: bool = False,
    ) -> None:
        if self._controller_busy or self._shutting_down:
            return
        if self.narrator_dialog is not None:
            if character is not None:
                self.narrator_dialog.select_role(character)
                if recovery:
                    self.narrator_dialog.set_recovery_context(
                        character, resume_live=bool(resume_live)
                    )
            if resume_live is not None:
                self._resume_live_after_narrator = bool(
                    self._resume_live_after_narrator or resume_live
                )
            self.show_dashboard()
            self.dashboard.show_voices()
            return
        if self.pregeneration_dialog is not None:
            if self._preparation_blocks_voice_changes() or (
                for_preparation and self.pregeneration_dialog.has_pending_work()
            ):
                self.set_status(
                    "Finish or cancel story preparation before changing voices."
                )
                self.dashboard.show_stories()
                return
            self._refresh_preparation_settings()
        should_resume_live = (
            bool(self.controller.is_live_running)
            if resume_live is None
            else bool(resume_live)
        )
        if self._needs_modal_stop():
            self._stop_live_then(
                lambda: self._open_narrator_picker(
                    should_resume_live,
                    character=character,
                    recovery=recovery,
                    for_preparation=for_preparation,
                ),
                "Stopping live capture before voice preview...",
            )
            return
        self._open_narrator_picker(
            should_resume_live,
            character=character,
            recovery=recovery,
            for_preparation=for_preparation,
        )

    def _open_narrator_picker(
        self,
        resume_live: bool,
        *,
        character: str | None = None,
        recovery: bool = False,
        for_preparation: bool = False,
    ) -> None:
        if self._shutting_down or self._quit_requested:
            return
        self.emergency_stop()
        if recovery:
            self.resume_live_after_unknown_mapping = resume_live
        self._narrator_return_to_stories = (
            self.dashboard.sections.currentWidget() is self.dashboard.stories_stack
        )
        self._narrator_preparation = self.pregeneration_dialog
        self._narrator_offline = for_preparation
        settings = (
            resolve_pregeneration_settings(self.pregeneration_dialog.settings)
            if for_preparation and self.pregeneration_dialog is not None
            else self.settings
        )
        dialog = GameNarratorDialog(
            settings,
            self.dashboard,
            preview_service=self.voice_auditions.session(),
            use_offline_engine=for_preparation,
        )
        if for_preparation and self.pregeneration_dialog is not None:
            plan = self.pregeneration_dialog.voice_plan()
            content = self.pregeneration_dialog.current_content()
            if isinstance(content, GameContent):
                selected_ids = set(self.pregeneration_dialog.selected_story_ids())
                selected = [
                    item
                    for item in content.selections
                    if item.selection_id in selected_ids
                ]
                dialog.set_voice_context(
                    plan if isinstance(plan, VoicePlan) else None,
                    character=character,
                    roles=tuple(
                        {speaker for item in selected for speaker in item.speakers}
                    ),
                    story_titles=tuple(item.title for item in selected),
                )
            elif isinstance(plan, VoicePlan):
                dialog.set_voice_context(plan, character=character)
            elif character is not None:
                dialog.set_voice_context(character=character)
        elif character is not None:
            dialog.set_voice_context(character=character)
        if recovery and character is not None:
            dialog.set_recovery_context(character, resume_live=bool(resume_live))
        self.narrator_dialog = dialog
        self._resume_live_after_narrator = resume_live
        dialog.finished.connect(self._narrator_finished)
        dialog.busyChanged.connect(self.dashboard.set_voice_editor_busy)
        dialog.settingsChanged.connect(self.dashboard.set_voice_editor_dirty)
        self.show_dashboard()
        self.dashboard.embed_narrator(dialog)
        self._apply_controller_action_state()
        self.set_status(
            "Current story preparation keeps its confirmed voices."
            if self.pregeneration_dialog is not None
            and self.pregeneration_dialog.has_pending_work()
            else ""
        )

    def _open_preparation_narrator(
        self,
        _settings: AppSettings,
        _parent: QWidget,
        *,
        character: str | None = None,
    ) -> AppSettings | None:
        self.open_voice_previews(character=character, for_preparation=True)
        return None

    def _save_narrator_candidate(
        self, dialog: GameNarratorDialog, *, offline: bool = False
    ) -> AppSettings | None:
        if dialog.result_settings is None:
            self.show_error("Voice selection finished without saved settings")
            return None
        candidate = dialog.result_settings.updated(
            speech_backend=self.settings.speech_backend,
            tts_model=self.settings.tts_model,
            tts_profile=self.settings.tts_profile,
            tts_speaker_wav=(
                self.settings.tts_speaker_wav
                if offline
                else dialog.result_settings.tts_speaker_wav
            ),
            pocket_gated_model_accepted=(
                self.settings.pocket_gated_model_accepted
                if offline and self.settings.offline_speech_backend != "pocket-tts"
                else dialog.result_settings.pocket_gated_model_accepted
            ),
            last_main_section=self.settings.last_main_section,
        )
        try:
            self._save_settings_candidate(candidate)
        except OSError as error:
            try:
                dialog.restore_initial_voice_bindings()
            except (OSError, ValueError) as rollback_error:
                self.show_error(
                    f"Unable to save voices: {error}. Restoring the previous "
                    f"voice selection also failed: {rollback_error}."
                )
            else:
                self.show_error(
                    f"Unable to save voices: {error}. Previous settings and "
                    "voices are unchanged."
                )
            return None
        self.settings = candidate
        self.dashboard.set_configuration(candidate)
        return candidate

    def _apply_narrator_to_preparation(
        self,
        candidate: AppSettings,
        preparation: OfflineAudioPreparationDialog | None,
    ) -> None:
        if preparation is not self.pregeneration_dialog or preparation is None:
            return
        if preparation.has_pending_work():
            return
        preparation.apply_narrator_settings(candidate, voice_changed=True)

    def _narrator_finished(self, result: int) -> None:
        dialog = self.narrator_dialog
        if dialog is None:
            return
        saved_assignment = (
            dialog.saved_assignment_summary()
            if result == QDialog.DialogCode.Accepted
            else None
        )
        if not isinstance(saved_assignment, str) or not saved_assignment:
            saved_assignment = None
        mapping_speaker = self.unknown_speaker_mapping_in_progress
        mapping_resolved = False
        self.narrator_dialog = None
        preparation = self._narrator_preparation
        self._narrator_preparation = None
        offline = self._narrator_offline
        self._narrator_offline = False
        return_to_stories = self._narrator_return_to_stories
        self._narrator_return_to_stories = False
        resume_live = self._resume_live_after_narrator
        completion_deferred = False
        self._resume_live_after_narrator = False
        self.dashboard.remove_narrator(dialog)
        dialog.deleteLater()
        self._apply_controller_action_state()
        if self._quit_requested:
            self.request_quit()
            return
        if self._shutting_down:
            return

        def finish_mapping() -> None:
            self._finish_narrator_mapping(
                mapping_speaker, resolved=mapping_resolved, resume_live=resume_live
            )

        try:
            if result == QDialog.DialogCode.Accepted:
                candidate = self._save_narrator_candidate(dialog, offline=offline)
                if candidate is None:
                    resume_live = False
                    return
                mapping_resolved = bool(
                    mapping_speaker
                    and dialog.voice_library.binding(mapping_speaker) is not None
                )
                profile_synced = self._sync_active_profile()
                self._apply_narrator_to_preparation(candidate, preparation)
                completion_deferred = bool(mapping_speaker or resume_live)
                self.unknown_speaker_mapping_in_progress = None
                self._reload_game_narrator(
                    profile_synced,
                    saved_assignment,
                    after_apply=finish_mapping if completion_deferred else None,
                )
            else:
                self.set_status(
                    "Voice selection cancelled. Your saved voices are unchanged."
                )
        finally:
            if not completion_deferred:
                finish_mapping()
            if (
                return_to_stories
                and preparation is self.pregeneration_dialog
                and preparation is not None
            ):
                self.dashboard.show_stories()
            if self._onboarding_voice_wizard is not None:
                self._resume_onboarding_after_voices()

    def _finish_narrator_mapping(
        self, speaker: str | None, *, resolved: bool, resume_live: bool
    ) -> None:
        resume_live = self._finish_unknown_speaker_mapping(
            speaker, resolved=resolved, resume_live=resume_live
        )
        if (
            resume_live
            and not self._shutting_down
            and not self._controller_busy
            and not self.controller.is_live_running
        ):
            self.toggle_live()

    def _finish_unknown_speaker_mapping(
        self,
        speaker: str | None,
        *,
        resolved: bool,
        resume_live: bool,
    ) -> bool:
        if not speaker:
            return resume_live
        self.unknown_speaker_mapping_in_progress = None
        self.resume_live_after_unknown_mapping = resume_live
        if not resolved:
            self.pending_unknown_speaker = speaker
            if resume_live:
                QTimer.singleShot(
                    0,
                    self,
                    lambda current=speaker: self._show_unknown_speaker_prompt(current),
                )
            return False
        if self._queued_unknown_speakers:
            next_speaker = self._queued_unknown_speakers.popleft()
            self.pending_unknown_speaker = next_speaker
            self.speaker_mapping_action.setText(f"Manage voice for {next_speaker}...")
            if resume_live:
                QTimer.singleShot(
                    0,
                    self,
                    lambda current=next_speaker: self._show_unknown_speaker_prompt(
                        current
                    ),
                )
            return False
        self.pending_unknown_speaker = None
        self.resume_live_after_unknown_mapping = False
        self.speaker_mapping_action.setText("Manage character voices...")
        return resume_live

    def offer_speaker_mapping(self, speaker: str) -> None:
        active_speaker = (
            self.unknown_speaker_mapping_in_progress or self.pending_unknown_speaker
        )
        if active_speaker and normalize_character_name(
            active_speaker
        ) == normalize_character_name(speaker):
            return
        if active_speaker:
            queued = {
                normalize_character_name(candidate)
                for candidate in self._queued_unknown_speakers
            }
            if normalize_character_name(speaker) not in queued:
                self._queued_unknown_speakers.append(speaker)
            self.set_status(
                f"Using the narrator for {speaker}. Manage its voice after "
                f"{active_speaker} if needed."
            )
            return
        self.pending_unknown_speaker = speaker
        self.speaker_mapping_action.setText(f"Manage voice for {speaker}...")
        self.set_status(
            f"Using the narrator for {speaker}. Manage the voice later if needed."
        )
        self.compact_controller.set_warning(f"Narrator used for {speaker}")

    def _show_unknown_speaker_prompt(self, speaker: str) -> None:
        if self._shutting_down or not self.resume_live_after_unknown_mapping:
            return
        if self.unknown_speaker_prompt is not None:
            self.unknown_speaker_prompt.close()
        # A QMessageBox parented to a hidden dashboard becomes a macOS sheet.
        # Qt then exposes and dims the whole dashboard behind it, which looks
        # like a large empty window. Keep this prompt as a small independent
        # tool window instead.
        prompt, choose, continue_button, cancel_button = build_unknown_speaker_prompt(
            speaker
        )
        self.unknown_speaker_prompt = prompt
        self.unknown_speaker_choose_button = choose
        self.unknown_speaker_continue_button = continue_button
        self.unknown_speaker_cancel_button = cancel_button
        prompt.buttonClicked.connect(self._unknown_speaker_prompt_clicked)
        prompt.finished.connect(self._unknown_speaker_prompt_finished)
        prompt.open()
        # Keep the prompt visible above a fullscreen game and exclude it from
        # screen-region OCR so the warning cannot become dialogue itself.
        QTimer.singleShot(0, prompt, lambda: configure_floating_window(prompt))

    def _unknown_speaker_prompt_clicked(self, button: QAbstractButton) -> None:
        prompt = self.sender()
        if not isinstance(prompt, QMessageBox):
            return
        value = prompt.property("vntts_unknown_speaker")
        speaker = value if isinstance(value, str) else self.pending_unknown_speaker
        if not speaker:
            return
        if button is self.unknown_speaker_choose_button:
            prompt.setProperty("vntts_unknown_resolved", True)
            self.unknown_speaker_mapping_in_progress = speaker
            QTimer.singleShot(
                0, self, lambda: self._open_pending_speaker_mapping(speaker)
            )
        elif button is self.unknown_speaker_continue_button:
            prompt.setProperty("vntts_unknown_resolved", True)
            QTimer.singleShot(
                0, self, lambda: self._continue_unknown_with_narrator(speaker)
            )
        elif button is self.unknown_speaker_cancel_button:
            prompt.setProperty("vntts_unknown_resolved", True)
            self._cancel_unknown_speaker(speaker)

    def _unknown_speaker_prompt_finished(self, _result: int) -> None:
        prompt = self.sender()
        if not isinstance(prompt, QMessageBox):
            return
        value = prompt.property("vntts_unknown_speaker")
        speaker = value if isinstance(value, str) else self.pending_unknown_speaker
        if (
            speaker
            and not prompt.property("vntts_unknown_resolved")
            and speaker != self.unknown_speaker_mapping_in_progress
        ):
            self._cancel_unknown_speaker(speaker)
        if prompt is self.unknown_speaker_prompt:
            self.unknown_speaker_prompt = None
            self.unknown_speaker_choose_button = None
            self.unknown_speaker_continue_button = None
            self.unknown_speaker_cancel_button = None

    def _cancel_unknown_speaker(self, speaker: str | None) -> None:
        if self._shutting_down or not speaker:
            return
        self.pending_unknown_speaker = speaker
        self.resume_live_after_unknown_mapping = False
        message = (
            "Live reading remains paused after voice editing. "
            f"Manage the voice for {speaker} or start reading when ready."
        )
        if self.controller.is_live_running:
            self._stop_live_then(
                lambda: self.set_status(message),
                f"Pausing live reading until a voice is chosen for {speaker}...",
            )
            return
        self.set_status(message)

    def _continue_unknown_with_narrator(self, speaker: str) -> None:
        self.controller.allow_narrator_fallback(speaker)
        if self._queued_unknown_speakers:
            next_speaker = self._queued_unknown_speakers.popleft()
            self.pending_unknown_speaker = next_speaker
            self.speaker_mapping_action.setText(f"Manage voice for {next_speaker}...")
            self._show_unknown_speaker_prompt(next_speaker)
            return
        self.pending_unknown_speaker = None
        self.speaker_mapping_action.setText("Manage character voices...")
        if (
            self.resume_live_after_unknown_mapping
            and not self.controller.is_live_running
        ):
            self.toggle_live()
        self.resume_live_after_unknown_mapping = False

    def _open_pending_speaker_mapping(self, speaker: str | None = None) -> None:
        if self._shutting_down:
            return
        speaker = speaker or self.pending_unknown_speaker
        self.resume_live_after_unknown_mapping = bool(
            self.resume_live_after_unknown_mapping or self.controller.is_live_running
        )
        if self._needs_modal_stop():
            self._stop_live_then(
                lambda: self._open_pending_speaker_mapping(speaker),
                "Stopping live capture before speaker mapping...",
            )
            return
        self.pending_unknown_speaker = speaker
        self.unknown_speaker_mapping_in_progress = speaker
        self.open_voice_previews(
            character=speaker,
            resume_live=self.resume_live_after_unknown_mapping,
            recovery=True,
        )

    def open_speaker_mapping(self) -> None:
        if self.pending_unknown_speaker:
            self._open_pending_speaker_mapping(self.pending_unknown_speaker)
        else:
            self.open_voice_previews()

    def open_support_center(self) -> None:
        if self.support_dialog is None:
            self.support_dialog = SupportCenterDialog(self.support_log, self.dashboard)
            self.support_dialog.reading_status_provider = lambda: (
                self.reading_diagnostic_session.status()
                if self.reading_diagnostic_session
                else None
            )
            self.support_dialog.reading_start_requested.connect(
                self.start_reading_diagnostics
            )
            self.support_dialog.reading_stop_requested.connect(
                self.stop_reading_diagnostics
            )
            self.support_dialog.reading_export_requested.connect(
                self.export_reading_diagnostics
            )
            self.support_dialog.reading_folder_requested.connect(
                self.open_reading_diagnostics_folder
            )
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

    def start_reading_diagnostics(self, media: bool, seconds: int) -> None:
        try:
            target = (
                selected_recording_target(self.controller.capture_target)
                if media
                else None
            )
            self.reading_diagnostic_session = ReadingDiagnosticSession(
                get_local_data_directory() / "reading-diagnostics",
                self.generation_timelines,
                seconds=seconds,
                target=target,
            )
            if self.support_dialog is not None:
                self.support_dialog.set_reading_start_error("")
                self.support_dialog.refresh_reading_session()
            identity = (
                f" Window {target['window_id']}, PID {target['pid']}: {target['title']}."
                if target
                else ""
            )
            self.set_status("Diagnostic session started." + identity)
        except Exception as error:
            if self.support_dialog is not None:
                self.support_dialog.set_reading_start_error(
                    f"Unable to start diagnostics: {error}"
                )

    def _refresh_reading_diagnostic_status(self) -> None:
        session = self.reading_diagnostic_session
        if session is None or isinstance(session, SavedDiagnosticSession):
            return
        status = session.status()
        session_id = status.get("session_id")
        if (
            not isinstance(session_id, str)
            or status.get("media_status") not in {"degraded", "failed"}
            or session_id == self._reported_diagnostic_failure
        ):
            return
        self._reported_diagnostic_failure = session_id
        message = (
            "Diagnostic media recording is incomplete. Reading logs continue until "
            "Stop or the session deadline; open Support to inspect the capture failure."
            if status.get("status") == "recording"
            else "Diagnostic media recording is incomplete; open Support to inspect the capture failure."
        )
        self.support_log.add(
            "diagnostic-recording",
            message,
            session_id=status.get("session_id"),
            media_status=status.get("media_status"),
        )
        self.set_status(message)

    def stop_reading_diagnostics(self) -> None:
        if self.reading_diagnostic_session is not None and not isinstance(
            self.reading_diagnostic_session, SavedDiagnosticSession
        ):
            self.reading_diagnostic_session.stop()
            if self.support_dialog is not None:
                self.support_dialog.refresh_reading_session()

    def open_reading_diagnostics_folder(self) -> None:
        path = get_local_data_directory() / "reading-diagnostics"
        path.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.show_error("Unable to open the diagnostic sessions folder")

    def export_reading_diagnostics(self) -> None:
        session = self.reading_diagnostic_session
        if session is None or (
            not isinstance(session, SavedDiagnosticSession)
            and not session.finished.is_set()
        ):
            return
        path = session.directory.parent / (session.session_id + ".zip")
        if self.support_dialog is not None:
            self.support_dialog.set_export_started()
        self.support_export_runner.start(
            self._build_support_bundle,
            str(path),
            self.controller.get_latest_diagnostic(),
            session,
        )

    def open_support_diagnostics(self) -> None:
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

    def open_support_settings_folder(self) -> None:
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

    def open_history(self) -> None:
        # Region capture has no focus probe. A modal over the calibrated region
        # makes OCR append the history list repeatedly. Stop
        # capture for the modal session, then restore the previous live state.
        if getattr(self.controller, "is_one_shot_read_running", False) is True:
            self.set_status("Finish the current dialog read before opening history")
            return
        resume_live = bool(self.controller.is_live_running)
        if self._needs_modal_stop():
            self._stop_live_then(
                lambda: self._open_history_dialog(resume_live),
                "Stopping live capture before dialogue history...",
            )
            return
        self._open_history_dialog(False)

    def _open_history_dialog(self, resume_live: bool) -> None:
        if self._shutting_down:
            return
        try:
            dialog = DialogueHistoryDialog(
                self.controller.history,
                self.controller.replay_dialog,
                stop_handler=self.controller.stop_voice_preview,
            )
            dialog.exec()
        finally:
            if (
                resume_live
                and not self._shutting_down
                and not self.controller.is_live_running
            ):
                self.toggle_live()

    def export_support_bundle(self) -> None:
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
        diagnostic = self.controller.get_latest_diagnostic()
        if self.support_dialog is not None:
            self.support_dialog.set_export_started()
        self.support_export_runner.start(self._build_support_bundle, path, diagnostic)

    def _build_support_bundle(
        self,
        path: str,
        diagnostic: DiagnosticSnapshot | None,
        diagnostic_session: ReadingDiagnosticSession
        | SavedDiagnosticSession
        | None = None,
    ) -> str:
        return str(
            SupportBundleBuilder(
                self.settings,
                self.support_log,
                diagnostic=diagnostic,
                diagnostic_session=diagnostic_session,
                generation_timelines=self.generation_timelines,
                previous_session=self.previous_session,
                audio_lifecycle=self.audio_lifecycle,
                voice_library=self.controller.voice_library,
            ).build(path)
        )

    def _support_export_finished(
        self, output: str | None, error: Exception | None
    ) -> None:
        if self._shutting_down:
            return
        if error is None and output is not None:
            self.support_export_finished(True, output)
        else:
            self.support_export_finished(
                False, str(error or "No support bundle returned")
            )

    def support_export_finished(self, successful: bool, message: str) -> None:
        if self.support_dialog is not None:
            self.support_dialog.set_export_result(successful, message)
        if successful:
            self.set_status(f"Support bundle saved to {message}")
        else:
            self.show_error(f"Support bundle export failed: {message}")

    def open_macos_permissions(self) -> None:
        MacOSPermissionsDialog().exec()

    def open_settings_folder(self) -> Path:
        path = self._settings_path.parent
        path.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            raise OSError("the operating system refused the folder-open request")
        return path

    def set_status(self, message: str | None) -> None:
        message = message or ""
        self.support_log.add("status", message)
        bounded = self._bounded_menu_text(message)
        self.status_action.setText(bounded)
        self.status_action.setToolTip(message)
        self.tray.setToolTip(f"{application_name}\n{bounded}")
        self.dashboard.set_status(message)
        self._refresh_speech_runtime()
        self.compact_controller.set_status(message)
        self._apply_runtime_control_state(self._runtime_control_state())

    def record_audio_route(self, trace: AudioRouteTrace) -> None:
        self.support_log.add(
            "audio-route",
            trace.message(),
            **trace.support_fields(),
        )

    def set_dialog(self, character: str, text: str) -> None:
        if not text:
            self.dialog_action.setText("No dialogue detected")
            self.dialog_action.setToolTip("")
            self.dashboard.set_dialogue(character, "")
            self.compact_controller.set_dialogue(character, "")
            return
        dialogue = f"{character}: {text}"
        self.dialog_action.setText(self._bounded_menu_text(dialogue))
        self.dialog_action.setToolTip(dialogue)
        self.dashboard.set_dialogue(character, text)
        self.compact_controller.set_dialogue(character, text)
        self._apply_runtime_control_state(self._runtime_control_state())

    @staticmethod
    def _bounded_menu_text(message: str, *, limit: int = 96) -> str:
        text = " ".join(message.split())
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3].rstrip()}..."

    def _show_tray_notification(
        self,
        title: str,
        message: str,
        icon: QSystemTrayIcon.MessageIcon,
        *,
        recovery: str,
    ) -> None:
        self._notification_recovery = recovery
        self.tray.showMessage(
            self._bounded_menu_text(title, limit=64),
            self._bounded_menu_text(message, limit=180),
            icon,
        )

    def _activate_notification_recovery(self) -> None:
        recovery = self._notification_recovery
        self._notification_recovery = None
        if recovery == "full-controls":
            self.show_dashboard()
        elif recovery == "support":
            self.open_support_center()

    def _controller_configuration_actions(self) -> tuple[QAction, ...]:
        return (
            self.pregeneration_action,
            self.calibrate_action,
            self.diagnostics_action,
            self.readiness_action,
            self.settings_action,
            self.profiles_action,
            self.corrections_action,
            self.ocr_review_action,
            self.setup_action,
            self.assets_action,
            self.speaker_mapping_action,
            self.history_action,
        )

    def _runtime_control_state(
        self,
        *,
        enabled: bool | None = None,
        unavailable_reason: str | None = None,
    ) -> RuntimeControlState:
        if enabled is None:
            enabled = (
                self._controller_ready
                and not self._controller_busy
                and not self._shutting_down
            )
        snapshot = None
        reader = getattr(self.controller, "live_reader", None)
        snapshot_loader = getattr(self.controller, "runtime_control_snapshot", None)
        if callable(snapshot_loader):
            loaded = snapshot_loader()
            if isinstance(loaded, dict):
                snapshot = loaded
        snapshot_loader = getattr(reader, "runtime_control_snapshot", None)
        if snapshot is None and callable(snapshot_loader):
            loaded = snapshot_loader()
            if isinstance(loaded, dict):
                snapshot = loaded
        snapshot = snapshot or {}
        live = getattr(self.controller, "is_live_running", False)
        one_shot = getattr(self.controller, "is_one_shot_read_running", False)
        preparing = bool(
            self.pregeneration_dialog is not None
            and self.pregeneration_dialog.has_pending_work()
        )
        if preparing and self._preparation_blocks_reading(loaded=True):
            enabled = False
            unavailable_reason = (
                "Story preparation has not finished its first playable dialogue yet."
            )
        if self.narrator_dialog is not None:
            enabled = False
            unavailable_reason = (
                "Save or cancel your narrator selection in Voices before reading."
            )
        sequence_status = self.controller.get_live_sequence_status()
        indexed_current = isinstance(
            getattr(sequence_status, "indexed_current_event_id", None), str
        )
        return RuntimeControlState(
            ready=bool(enabled),
            indexed_current=indexed_current,
            live=live if isinstance(live, bool) else self._reported_live,
            starting=self._live_scope_generation is not None
            or bool(snapshot.get("starting", one_shot is True)),
            paused=bool(snapshot.get("paused", self._reported_speech_paused)),
            speaking=bool(snapshot.get("speaking", False)),
            queued=bool(snapshot.get("queued", False)),
            replayable=bool(snapshot.get("replayable", False)),
            transition=(
                "stopping"
                if self.live_stop_runner.active
                else "starting"
                if self._live_scope_generation is not None
                else None
            ),
            needs_readiness_check=bool(self.last_controller_error),
            unavailable_reason=(
                unavailable_reason
                or "Speech model and voices are loading. Controls will unlock "
                "automatically."
                if self._controller_busy
                else unavailable_reason or "VNTTS is shutting down."
                if self._shutting_down
                else unavailable_reason
                or (
                    f"{self.last_controller_error} Select Check readiness to recover."
                    if self.last_controller_error
                    else "Select Full controls, then Reading and Load reading engine "
                    "to start."
                    if not self._controller_ready and self.settings.onboarding_completed
                    else "Select Full controls, then Reading and Set up reading to start."
                    if not self._controller_ready
                    else None
                )
            ),
        )

    def _apply_runtime_control_state(self, state: RuntimeControlState) -> None:
        self._presented_runtime_state = state
        if self.pregeneration_dialog is not None:
            self.pregeneration_dialog.setEnabled(
                self.pregeneration_dialog.has_pending_work()
                or not (self._controller_busy or self.narrator_dialog is not None)
            )
        controls = (
            (self.read_action, state.can_read, "read"),
            (self.live_action, state.can_toggle_live, "live"),
            (self.pause_action, state.can_pause, "pause"),
        )
        for action, enabled, control in controls:
            action.setEnabled(enabled)
            action.setToolTip("" if enabled else state.reason_for(control))
        self.read_action.setText(state.read_label)
        self.pause_action.setText(state.pause_label)
        if state.can_pause:
            self.pause_action.setToolTip(state.pause_description)
        if state.can_read:
            self.read_action.setToolTip(state.read_tooltip)
        self.live_action.setText(
            "Cancel start"
            if state.transition == "starting"
            else "Stopping reading..."
            if state.transition == "stopping"
            else "Stop reading"
            if state.active
            else "Start reading"
        )
        self.sequence_resync_action.setEnabled(state.ready)
        self.dashboard.set_runtime_controls(state)
        self.compact_controller.set_runtime_controls(state)

    def _preparation_blocks_voice_changes(self) -> bool:
        preparation = self.pregeneration_dialog
        return bool(
            preparation is not None
            and preparation.has_pending_work()
            and not (
                preparation.generating is True
                or preparation.recovering is True
                or preparation.publishing_pack is True
            )
        )

    def _apply_preparation_reading_state(
        self, preparing: bool, choosing_voice: bool
    ) -> None:
        preparation_blocks = self._preparation_blocks_reading(loaded=False)
        self.dashboard.prepare_reading_button.setEnabled(
            not (
                self._controller_busy
                or self._shutting_down
                or (preparing and preparation_blocks)
                or choosing_voice
            )
        )
        if preparing or choosing_voice:
            for button in self.dashboard.loading_blocked_buttons:
                if (
                    preparing
                    and not choosing_voice
                    and (
                        button
                        in (
                            self.dashboard.calibrate_button,
                            self.dashboard.settings_button,
                        )
                        or (
                            button is self.dashboard.narrator_voice_button
                            and not self._preparation_blocks_voice_changes()
                        )
                    )
                ):
                    continue
                button.setEnabled(False)

    def _apply_controller_action_state(self) -> None:
        enabled = (
            self._controller_ready
            and not self._controller_busy
            and not self._shutting_down
        )
        self._apply_runtime_control_state(self._runtime_control_state(enabled=enabled))
        self.dashboard.set_loading(self._controller_busy)
        self._refresh_speech_runtime()
        preparing = (
            self.pregeneration_dialog is not None
            and self.pregeneration_dialog.has_pending_work()
        )
        choosing_voice = self.narrator_dialog is not None
        self._apply_preparation_reading_state(preparing, choosing_voice)
        if enabled:
            resolve_voice = getattr(self.controller, "_resolve_voice_label", None)
            if callable(resolve_voice):
                try:
                    narrator = resolve_voice("Narrator")
                except Exception:
                    narrator = None
                if isinstance(narrator, str):
                    self.dashboard.set_speech_identity(self.settings, narrator)
        else:
            from vntts.speech_presentation import narrator_voice_label

            library = getattr(self.controller, "voice_library", None)
            if isinstance(library, VoiceLibrary):
                self.dashboard.set_speech_identity(
                    self.settings, narrator_voice_label(self.settings, library)
                )
        configuration_enabled = not (
            self._controller_busy or self._shutting_down or preparing or choosing_voice
        )
        for action in self._controller_configuration_actions():
            action.setEnabled(configuration_enabled)
        voice_configuration_enabled = not (
            self._controller_busy
            or self._shutting_down
            or choosing_voice
            or self._preparation_blocks_voice_changes()
        )
        self.voice_preview_action.setEnabled(voice_configuration_enabled)
        self.speaker_mapping_action.setEnabled(voice_configuration_enabled)
        capture_configuration_enabled = not (
            self._controller_busy
            or self._shutting_down
            or choosing_voice
            or self.live_stop_runner.active
        )
        self.calibrate_action.setEnabled(capture_configuration_enabled)
        self.settings_action.setEnabled(capture_configuration_enabled)
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

    def _set_modal_launchers_enabled(self, enabled: bool) -> None:
        available = (
            bool(enabled)
            and not self._controller_busy
            and not self._shutting_down
            and self.narrator_dialog is None
        )
        self.voice_preview_action.setEnabled(available)
        self.speaker_mapping_action.setEnabled(available)
        self.history_action.setEnabled(available)
        self.calibrate_action.setEnabled(available)
        self.settings_action.setEnabled(available)
        self.dashboard.calibrate_button.setEnabled(available)
        self.dashboard.settings_button.setEnabled(available)

    def _begin_controller_lifecycle(self, cancellation: Event | None = None) -> int:
        self.resume_live_after_unknown_mapping = False
        self._configuration_after_apply = None
        self._resume_live_after_narrator = False
        if self.unknown_speaker_prompt is not None:
            self.unknown_speaker_prompt.close()
        self.voice_auditions.cancel_warmup()
        self._cancel_live_scope()
        self._cancel_diagnostics_refresh()
        self._pregeneration_activation_restore_runtime.clear()
        self._lifecycle_generation = self.session_owner.begin(cancellation)
        self.last_controller_error = None
        self._controller_busy = True
        self._apply_controller_action_state()
        self._update_auto_advance_action()
        return self._lifecycle_generation

    def _finish_controller_lifecycle(self) -> None:
        self._controller_busy = False
        self._apply_controller_action_state()
        self._update_auto_advance_action()

    def _lifecycle_is_current(self, generation: int | None) -> bool:
        return (
            isinstance(generation, int)
            and generation == self._lifecycle_generation
            and self.session_owner.is_current(generation)
            and not self._shutting_down
        )

    def set_ready(self, ready: bool) -> None:
        self._controller_ready = bool(ready)
        if ready:
            self.last_controller_error = None
        self._apply_controller_action_state()
        if not ready:
            self.set_status(self.last_controller_error or "Unable to start")

    def set_live(self, running: bool) -> None:
        self._reported_live = bool(running)
        self.live_action.setText("Stop reading" if running else "Start reading")
        self.dashboard.set_live(running)
        self.compact_controller.set_live(running)
        if running:
            self._apply_runtime_control_state(self._runtime_control_state())
        else:
            self.set_speech_paused(False)
        dialog = self.pregeneration_dialog
        if (
            not running
            and not self.live_stop_runner.active
            and dialog is not None
            and isinstance(dialog.pack_result(), OfflinePackResult)
        ):
            QTimer.singleShot(0, self, self._activate_ready_preparation)

    def set_speech_paused(self, paused: bool) -> None:
        self._reported_speech_paused = bool(paused)
        self.compact_controller.set_paused(paused)
        self._apply_runtime_control_state(self._runtime_control_state())

    def show_error(self, message: str) -> None:
        self.support_log.add("error", message)
        self.set_status(message)
        self._show_tray_notification(
            f"{application_name} needs attention",
            "An error needs attention. Open Support and logs for details.",
            QSystemTrayIcon.MessageIcon.Critical,
            recovery="support",
        )

    def report_controller_error(self, error: BaseException) -> None:
        message = format_runtime_error(error)
        self.last_controller_error = message
        self.signals.error_reported.emit(message)

    def _dispose_tray(self) -> None:
        self.tray.hide()
        self.menu.close()
        self.tray.deleteLater()
        self.menu.deleteLater()

    def _close_retained_runtimes(self) -> None:
        self.controller.shutdown_complete.wait()
        self.voice_auditions.close()
        self.moss_runtime.shutdown()
        self.pocket_runtime.shutdown()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self.speech_runtime_timer.stop()
        self._shutting_down = True
        self.voice_auditions.suspend()
        if self.narrator_dialog is not None:
            self.narrator_dialog.close()
        if self.pregeneration_dialog is not None:
            self.pregeneration_dialog.close()
        self._pregeneration_activation_restore_runtime.clear()
        self._lifecycle_generation = self.session_owner.close()
        if self.reading_diagnostic_session is not None and not isinstance(
            self.reading_diagnostic_session, SavedDiagnosticSession
        ):
            self.reading_diagnostic_session.stop(timeout=15)
        self._controller_busy = True
        self._live_stop_continuation = None
        self._live_stop_generation = None
        self.live_stop_runner.cancel()
        self._cancel_calibration()
        self._cancel_live_scope()
        self._cancel_diagnostics_refresh()
        self.initial_start_runner.cancel()
        self.profile_restart_runner.cancel()
        self.configuration_runner.cancel()
        self.onboarding_test_runner.cancel()
        self._onboarding_test_generation = None
        self.moss_runtime_runner.cancel()
        if self._pregeneration_activation_cancellation is not None:
            self._pregeneration_activation_cancellation.set()
        self.pregeneration_activation_runner.cancel()
        self.onboarding_cancel_event.set()
        self._apply_controller_action_state()
        self.resume_live_after_unknown_mapping = False
        self._queued_unknown_speakers.clear()
        if self.unknown_speaker_prompt is not None:
            self.unknown_speaker_prompt.close()
            self.unknown_speaker_prompt = None
            self.unknown_speaker_choose_button = None
            self.unknown_speaker_continue_button = None
            self.unknown_speaker_cancel_button = None
        self.dashboard.keep_running_on_close = False
        self.dashboard._quitting = True
        self._dispose_tray()
        self.dashboard.close()
        self.compact_controller.close()
        if self.diagnostics_dialog is not None:
            self.diagnostics_dialog.close()
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
            self.hotkey_listener = None
        shutdown_complete = self.controller.shutdown_complete
        if shutdown_complete.is_set():
            self._close_retained_runtimes()
        else:
            Thread(
                target=self._close_retained_runtimes,
                name="retained-runtime-shutdown",
                daemon=True,
            ).start()

    def request_quit(self) -> None:
        self._quit_requested = True
        # Embedded dialogs can be hidden by navigation or dashboard.close().
        # close() may not emit finished then; reject() runs their cancellation
        # lifecycle regardless of visibility and still waits for active work.
        if self.narrator_dialog is not None:
            # Reopen after the native close event finishes, so cancellation
            # progress stays visible while the worker winds down.
            QTimer.singleShot(0, self.dashboard, self.dashboard.show)
            self.dashboard.show_voices()
            self.set_status("Cancelling voice preview before quitting...")
            self.narrator_dialog.reject()
            return
        if self.pregeneration_dialog is not None:
            QTimer.singleShot(0, self.dashboard, self.dashboard.show)
            self.dashboard.show_stories()
            self.set_status(
                "Closing story preparation safely; completed audio stays saved."
            )
            self.pregeneration_dialog.reject()
            return
        QTimer.singleShot(0, self.application, self.application.quit)


def _worker_exit_code(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Worker returned a non-integer exit code")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    freeze_support()
    parser = argparse.ArgumentParser(
        prog="vntts-app",
        description="Run the VNTTS desktop application.",
    )
    parser.add_argument(
        "--timeline-journal",
        metavar="NEW_JSONL",
        help="Opt in to a bounded identity/timing journal in a new file (no dialogue or pixels).",
    )
    parser.add_argument("--package-self-test", action="store_true")
    parser.add_argument("--package-self-test-report")
    configure_release_smoke_arguments(parser)
    parser.add_argument(
        "--game-content-import-worker",
        choices=("reverse1999",),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--source-audio-publisher-worker",
        choices=("duration", "semantics"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--offline-generation-worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--prepared-sequence-worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    arguments, qt_arguments = parser.parse_known_args(
        sys.argv[1:] if argv is None else argv
    )
    configure_bundled_dependencies()
    if arguments.game_content_import_worker == "reverse1999":
        from r1999extractor.bootstrap import main as reverse1999_bootstrap_main

        return _worker_exit_code(reverse1999_bootstrap_main(qt_arguments))
    if arguments.source_audio_publisher_worker == "duration":
        from r1999extractor.source_audio_duration import main as duration_main

        return _worker_exit_code(duration_main(qt_arguments))
    if arguments.source_audio_publisher_worker == "semantics":
        from r1999extractor.source_audio_semantics import main as semantics_main

        return _worker_exit_code(semantics_main(qt_arguments))
    if arguments.offline_generation_worker:
        from vntts.authoring.cli_generation import main as generation_main

        return generation_main(qt_arguments)
    if arguments.prepared_sequence_worker:
        from r1999extractor.live_sequence import main as live_sequence_main

        return _worker_exit_code(live_sequence_main(qt_arguments))
    if arguments.package_self_test:
        return run_package_self_test(arguments.package_self_test_report).exit_code
    if any(
        value is not None
        for name, value in vars(arguments).items()
        if name.startswith("release_smoke_test_")
    ):
        return run_release_smoke_test(
            image_path=arguments.release_smoke_test_image,
            window_title=arguments.release_smoke_test_window_title,
            report_path=arguments.release_smoke_test_report,
            model_name=(
                arguments.release_smoke_test_model
                if arguments.release_smoke_test_model is not None
                else default_smoke_test_model
            ),
            expected_speaker=arguments.release_smoke_test_expected_speaker,
            auto_advance_expected_text=(
                arguments.release_smoke_test_auto_advance_expected_text
            ),
        ).exit_code

    enable_windows_dpi_awareness()
    application = QApplication.instance()
    if not isinstance(application, QApplication):
        application = QApplication([sys.argv[0], *qt_arguments])
    application.setApplicationName(application_name)
    application.setQuitOnLastWindowClosed(False)
    from vntts.system_audio import initialize_audio_device_monitor

    initialize_audio_device_monitor()
    tray_application = TrayApplication(application)
    with (
        cleanup_on_exit(
            lambda: tray_application.generation_timelines.stop_acquisition(
                timeout=0.25
            ),
            description="Timeline acquisition stop",
        ),
        cleanup_on_exit(tray_application.shutdown, description="Application shutdown"),
    ):
        if arguments.timeline_journal is not None:
            tray_application.generation_timelines.start_acquisition(
                arguments.timeline_journal
            )
        tray_application.start()
        return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())

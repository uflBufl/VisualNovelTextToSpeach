from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFocusFrame,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from vntts.action_icons import set_action_icon
from vntts.auto_advance_policy import auto_advance_control_state
from vntts.settings import AppSettings, is_live_sequence_audio_mode, main_sections
from vntts.speech_presentation import (
    compact_runtime_label,
    engine_model_label,
    playback_labels,
    playback_status_label,
    reading_policy_label,
    speech_configuration_label,
    speech_configuration_rows,
    speech_runtime_label,
)
from vntts.ui_text import (
    copy_text_button,
    make_text_copyable,
    plain_label_text,
    set_labeled_text,
)

if TYPE_CHECKING:
    from vntts.controller import LiveSequenceStatus
    from vntts.diagnostics import DiagnosticSnapshot
    from vntts.voices import CharacterVoice
    from vntts.window_capture import WindowGeometry


_INITIALIZE_READING_REASON = "Read prepares the speech engine, then starts reading."


@dataclass(frozen=True)
class RuntimeControlState:
    ready: bool = False
    can_initialize: bool = False
    live: bool = False
    starting: bool = False
    paused: bool = False
    speaking: bool = False
    queued: bool = False
    replayable: bool = False
    transition: str | None = None
    unavailable_reason: str | None = None
    needs_readiness_check: bool = False
    indexed_current: bool = False

    @property
    def can_read(self) -> bool:
        return (
            self.ready
            and (not self.active or self.indexed_current)
            and self.transition is None
            and not self.starting
        )

    @property
    def read_label(self) -> str:
        return (
            "Read indexed current line"
            if self.indexed_current
            else "Read current dialogue"
        )

    @property
    def read_tooltip(self) -> str:
        return (
            "Use original game audio, prepared audio or the configured live fallback "
            "for the indexed line; continue manually in the game."
            if self.indexed_current
            else ""
        )

    @property
    def active(self) -> bool:
        return self.starting or self.live or self.paused or self.speaking or self.queued

    @property
    def can_toggle_live(self) -> bool:
        return (
            self.ready or self.active or self.can_initialize
        ) and self.transition != "stopping"

    @property
    def can_pause(self) -> bool:
        return (
            not self.starting
            and self.transition is None
            and (self.paused or self.live or self.speaking or self.queued)
        )

    @property
    def pause_label(self) -> str:
        return "Resume speech" if self.paused else "Pause speech"

    @property
    def pause_description(self) -> str:
        return (
            "Resume VNTTS speech and automatic dialogue continuation."
            if self.paused
            else "Pause VNTTS speech and automatic dialogue continuation until resumed."
        )

    def reason_for(self, control: str) -> str:
        if self.transition == "stopping":
            return (
                "Reading and speech are stopping; controls will unlock automatically."
            )
        if self.transition == "starting":
            return "Reading is starting; choose Cancel start to stop."
        if not self.ready:
            return self.unavailable_reason or (
                _INITIALIZE_READING_REASON
                if self.can_initialize
                else "VNTTS is not ready. Select Check readiness."
            )
        reasons = {
            "read": "Stop reading and speech before reading the visible dialogue once.",
            "pause": "Start reading or speech before pausing.",
        }
        return reasons.get(control, "Control is unavailable in the current state.")


def _apply_reading_capabilities(
    state: RuntimeControlState,
    *,
    read_button: QPushButton,
    live_button: QPushButton,
    pause_button: QPushButton,
) -> None:
    read_button.setText("Indexed line" if state.indexed_current else "Dialogue")
    read_button.setAccessibleName(state.read_label)
    read_button.setAccessibleDescription(state.read_tooltip)
    read_button.setEnabled(state.can_read)
    set_action_icon(pause_button, "play" if state.paused else "pause")
    pause_button.setText("")
    pause_button.setAccessibleName(state.pause_label)
    pause_button.setEnabled(state.can_pause)
    pause_reason = (
        state.pause_description if state.can_pause else state.reason_for("pause")
    )
    pause_button.setToolTip(pause_reason)
    pause_button.setAccessibleDescription(pause_reason)
    live_button.setEnabled(state.can_toggle_live)
    _set_live_button_presentation(live_button, state.active)
    live_button.setToolTip(
        state.reason_for("live")
        if not state.can_toggle_live or (state.can_initialize and not state.ready)
        else live_button.accessibleName()
    )
    if state.can_initialize and not state.ready:
        live_button.setAccessibleDescription(state.reason_for("live"))
    if state.transition is not None:
        set_action_icon(
            live_button, "x" if state.transition == "starting" else "square"
        )
        live_button.setText(
            "Cancel" if state.transition == "starting" else "Stopping..."
        )
        live_button.setAccessibleName(
            "Cancel start" if state.transition == "starting" else "Stopping reading..."
        )
        live_button.setAccessibleDescription(
            "Cancel the pending start"
            if state.transition == "starting"
            else "Reading and speech are stopping; controls will unlock automatically"
        )
        live_button.setToolTip(
            "Cancel the pending start"
            if state.transition == "starting"
            else "Wait for reading and speech to stop"
        )
    read_button.setToolTip(
        (state.read_tooltip or state.read_label)
        if state.can_read
        else state.reason_for("read")
    )


def _set_live_button_presentation(button: QPushButton, running: bool) -> None:
    set_action_icon(button, "square" if running else "play")
    button.setText("Stop" if running else "Read")
    button.setAccessibleName("Stop reading" if running else "Start reading")
    button.setToolTip(button.accessibleName())
    button.setDefault(True)
    button.setAccessibleDescription(
        "Primary action: immediately stop reading, current speech, queued speech and auto advance"
        if running
        else "Primary action: start following dialogue in the game"
    )


class ControlDashboard(QMainWindow):
    main_section_changed = Signal(str)
    reading_setup_requested = Signal()
    read_requested = Signal()
    live_requested = Signal()
    pause_requested = Signal()
    auto_advance_requested = Signal(bool)
    sequence_resync_requested = Signal()
    sequence_expected_requested = Signal()
    pregeneration_requested = Signal()
    readiness_requested = Signal()
    calibration_requested = Signal()
    voices_requested = Signal()
    diagnostics_requested = Signal()
    settings_requested = Signal()
    compact_requested = Signal()
    moss_runtime_requested = Signal()
    quit_requested = Signal()
    hidden_to_background = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._initialize_state(settings)
        self._build_status_widgets()
        self._build_detail_widgets()
        reading_actions = self._build_action_buttons()
        self._build_sequence_widgets()
        setup_group = self._build_setup_group()
        card = self._build_dialogue_card()
        reading_page = self._build_reading_page(card, reading_actions)
        stories_page = self._build_stories_page()
        voices_page = self._build_voices_page()
        self._build_shell(reading_page, stories_page, voices_page, setup_group)
        self._finish_setup(settings)

    def _initialize_state(self, settings: AppSettings) -> None:
        self.keep_running_on_close = settings.keep_running_on_close
        self._quitting = False
        self._live = False
        self._ready = False
        self._can_initialize = False
        self._dialogue_current = False
        self._dialogue_speaker: str | None = None
        self._dialogue_text = ""
        self._unavailable_reason: str | None = None
        self._reading_setup_label = (
            "Prepare reading" if settings.onboarding_completed else "Set up reading"
        )
        self._sequence_expected_candidate_count = 0
        self._sequence_controls_available = False
        self._window_problem = False
        self._needs_readiness_check = False
        self._voice_dirty = False
        self._voice_busy = False
        self.setWindowTitle("Visual Novel Text to Speech")
        self.resize(860, 560)

    def _build_status_widgets(self) -> None:
        self.status = QLabel("Starting...")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight: 600;")
        self.loading_panel = QFrame()
        self.loading_panel.setFrameShape(QFrame.Shape.StyledPanel)
        loading_layout = QVBoxLayout(self.loading_panel)
        self.loading_message = QLabel(
            "Loading the speech model and voices. Please wait; controls will "
            "unlock automatically."
        )
        self.loading_message.setWordWrap(True)
        self.loading_message.setStyleSheet("font-weight: 700;")
        self.loading_message.setAccessibleName("Speech engine loading")
        self.loading_progress = QProgressBar()
        self.loading_progress.setRange(0, 0)
        self.loading_progress.setTextVisible(False)
        self.loading_progress.setAccessibleName("Speech engine loading progress")
        loading_layout.addWidget(self.loading_message)
        loading_layout.addWidget(self.loading_progress)
        self.action_reason = QLabel()
        self.action_reason.setWordWrap(True)
        self.action_reason.setAccessibleName("Reading control availability")
        self.action_reason.setAccessibleDescription(
            "Explains why reading controls are available or unavailable"
        )
        self.reading_state = QLabel("Reading stopped")
        self.reading_state.setAccessibleName("Reading state")
        self.reading_state.setStyleSheet("font-weight: 600;")
        self.capture_target = QLabel("Capture: Not selected")
        self.capture_target.setTextFormat(Qt.TextFormat.PlainText)
        self.capture_target.setWordWrap(True)
        self.capture_target.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.capture_target.setAccessibleName("Reading capture source")
        self.speaker = QLabel()
        self.speaker.setAccessibleName("Current dialogue speaker")
        self.speaker.setWordWrap(True)
        self.voice = QLabel()
        self.audio_source = QLabel()
        self.voice.setWordWrap(True)
        self.audio_source.setWordWrap(True)
        self.speech_configuration = QLabel()
        self.speech_configuration.setWordWrap(True)
        self.speech_configuration.setAccessibleName("Narrator and speech engine")
        self.reading_policy = QLabel()
        self.reading_policy.setWordWrap(True)
        self.reading_policy.setAccessibleName("In-game audio policy")
        self.confidence = QLabel("-")
        self.latency = QLabel("-")
        self.configuration = QLabel()
        self.configuration.setWordWrap(True)
        self.configuration.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.configuration.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.configuration.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.configuration.setMinimumHeight(
            self.configuration.fontMetrics().lineSpacing() * 3
        )
        self.dialogue = QLabel("The detected dialogue will appear here.")
        self.dialogue.setWordWrap(True)
        self.dialogue.setMinimumHeight(52)
        dialogue_font = self.dialogue.font()
        dialogue_font.setPointSize(dialogue_font.pointSize() + 2)
        self.dialogue.setFont(dialogue_font)
        self.dialogue.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

    def _build_detail_widgets(self) -> None:
        details = QFormLayout()
        details.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        details.addRow("OCR confidence", self.confidence)
        details.addRow("Latest latency", self.latency)
        details.addRow("Configuration", self.configuration)
        self.details_content = QWidget()
        self.details_layout = QVBoxLayout(self.details_content)
        self.details_layout.setContentsMargins(0, 0, 0, 0)
        self.details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.details_layout.addLayout(details)
        self.playback_details = QLabel()
        self.playback_details.setWordWrap(True)
        self.playback_details.setTextFormat(Qt.TextFormat.PlainText)
        self.speech_details = QLabel()
        self.speech_details.setWordWrap(True)
        self.speech_details.setTextFormat(Qt.TextFormat.PlainText)
        self.details_layout.addWidget(self.speech_details)
        self.details_layout.addWidget(self.playback_details)
        self.details_toggle = QPushButton("Details")
        set_action_icon(self.details_toggle, "list")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setAccessibleDescription(
            "Show or hide voice, audio source, OCR, latency and configuration details"
        )
        self.details_toggle.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.details_toggle.toggled.connect(self._set_details_expanded)

    def _build_action_buttons(self) -> QHBoxLayout:
        self.read_button = QPushButton("Dialogue")
        set_action_icon(self.read_button, "volume-2")
        self.live_button = QPushButton("Read")
        set_action_icon(self.live_button, "play")
        self.pause_button = QPushButton("")
        set_action_icon(self.pause_button, "pause")
        self.sequence_resync_button = QPushButton("Set story position / resync")
        set_action_icon(self.sequence_resync_button, "locate-fixed")
        self.sequence_expected_button = QPushButton("Use expected next line")
        set_action_icon(self.sequence_expected_button, "skip-forward")
        self.auto_advance_check = QCheckBox()
        self.auto_advance_check.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.auto_advance_reason = QLabel()
        self.auto_advance_reason.setWordWrap(True)
        self.auto_advance_check.toggled.connect(self.auto_advance_requested.emit)
        self.live_button.setDefault(True)
        self.live_button.setAccessibleDescription(
            "Primary action: start or stop continuous live reading"
        )
        self.read_button.setAccessibleDescription(
            "Read the currently visible dialogue once"
        )
        self.read_button.clicked.connect(self.read_requested.emit)
        self.live_button.clicked.connect(self.live_requested.emit)
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.sequence_resync_button.clicked.connect(self.sequence_resync_requested.emit)
        self.sequence_expected_button.clicked.connect(
            self.sequence_expected_requested.emit
        )

        reading_actions = QHBoxLayout()
        reading_actions.addWidget(self.live_button)
        reading_actions.addWidget(self.pause_button)
        reading_actions.addWidget(self.read_button)
        self.prepare_reading_button = QPushButton("Set up reading")
        set_action_icon(self.prepare_reading_button, "settings")
        self.prepare_reading_button.clicked.connect(
            lambda: (
                self.settings_requested.emit()
                if self._window_problem
                else self.readiness_requested.emit()
                if self._needs_readiness_check
                else self.reading_setup_requested.emit()
            )
        )
        self.prepare_audio_button = QPushButton("Prepare offline audio...")
        set_action_icon(self.prepare_audio_button, "audio-lines")
        self.prepare_audio_button.setAccessibleDescription(
            "Choose stories and prepare their voices locally with guided defaults"
        )
        self.prepare_audio_button.clicked.connect(self.pregeneration_requested.emit)
        self.loading_blocked_buttons = [self.prepare_audio_button]
        return reading_actions

    def _build_sequence_widgets(self) -> None:
        self.sequence_state = QLabel("Unavailable")
        self.story_title = QLabel("No story position yet")
        self.story_title.setWordWrap(True)
        self.story_title.setTextFormat(Qt.TextFormat.PlainText)
        self.story_title.setAccessibleName("Current story")
        self.sequence_position = QLabel("Not located")
        self.sequence_position.setWordWrap(True)
        self.sequence_position.setTextFormat(Qt.TextFormat.PlainText)
        self.sequence_position.setAccessibleName("Current story position")
        self.sequence_identity = QLabel("-")
        self.sequence_canonical = QLabel("-")
        self.sequence_canonical.setWordWrap(True)
        self.sequence_expected_audio = QLabel("-")
        self.sequence_actual_audio = QLabel("-")
        self.sequence_ocr = QLabel("-")
        self.sequence_ocr.setWordWrap(True)
        sequence_form = QFormLayout()
        sequence_form.addRow("Cursor state", self.sequence_state)
        sequence_form.addRow("Event / line", self.sequence_identity)
        sequence_form.addRow("Canonical dialogue", self.sequence_canonical)
        sequence_form.addRow("Expected audio", self.sequence_expected_audio)
        sequence_form.addRow("Actual audio", self.sequence_actual_audio)
        sequence_form.addRow("OCR activity", self.sequence_ocr)
        self.sequence_group = QGroupBox("Sequence-first story cursor")
        sequence_layout = QVBoxLayout(self.sequence_group)
        sequence_layout.addLayout(sequence_form)
        self.details_layout.addWidget(self.sequence_group)

    def _build_setup_group(self) -> QWidget:
        setup_group = QWidget()
        setup = QVBoxLayout(setup_group)
        setup.setContentsMargins(0, 0, 0, 0)
        setup_primary = QHBoxLayout()
        self.setup_primary_button = QPushButton("Check readiness")
        set_action_icon(self.setup_primary_button, "circle-check")
        self.setup_primary_button.setAccessibleDescription(
            "Check readiness and open the direct fix for any setup problem"
        )
        self.setup_primary_button.clicked.connect(self.readiness_requested.emit)
        setup_primary.addWidget(self.setup_primary_button)
        self.loading_blocked_buttons.append(self.setup_primary_button)
        self.setup_more_button = QPushButton("Tools")
        set_action_icon(self.setup_more_button, "list")
        self.setup_more_button.setCheckable(True)
        self.setup_more_button.setAccessibleDescription(
            "Show or hide calibration, support and logs, and quit actions"
        )
        setup_primary.addWidget(self.setup_more_button)
        setup_primary.addStretch()
        setup.addLayout(setup_primary)
        self.setup_secondary_content = QWidget()
        setup_secondary = QHBoxLayout(self.setup_secondary_content)
        setup_secondary.setContentsMargins(0, 0, 0, 0)
        self.setup_buttons = [self.setup_primary_button, self.setup_more_button]
        for label, icon, signal in (
            ("Calibrate capture", "locate-fixed", self.calibration_requested),
            ("Narrator voice", "audio-lines", self.voices_requested),
            ("Support and logs", "circle-help", self.diagnostics_requested),
            ("Settings", "settings", self.settings_requested),
        ):
            button = QPushButton(label)
            set_action_icon(button, icon)
            button.clicked.connect(signal.emit)
            if label == "Settings":
                setup_primary.insertWidget(1, button)
            elif label != "Narrator voice":
                setup_secondary.addWidget(button)
            self.setup_buttons.append(button)
            if label != "Support and logs":
                self.loading_blocked_buttons.append(button)
            if label == "Narrator voice":
                self.narrator_voice_button = button
            elif label == "Calibrate capture":
                self.calibrate_button = button
            elif label == "Settings":
                self.settings_button = button
        self.setTabOrder(self.setup_primary_button, self.settings_button)
        self.setTabOrder(self.settings_button, self.setup_more_button)
        self.quit_button = QPushButton("Quit VNTTS")
        set_action_icon(self.quit_button, "log-out")
        self.quit_button.clicked.connect(self.request_quit)
        setup_secondary.addWidget(self.quit_button)
        setup_secondary.addStretch()
        self.setup_buttons.append(self.quit_button)
        setup.addWidget(self.setup_secondary_content)
        self.setup_more_button.toggled.connect(self._set_setup_expanded)
        return setup_group

    def _build_dialogue_card(self) -> QGroupBox:
        card = QGroupBox("Current dialogue")
        self.dialogue_card = card
        card_layout = QVBoxLayout(card)
        self.speaker.setStyleSheet("font-weight: 600;")
        card_layout.addWidget(self.speaker)
        card_layout.addWidget(self.dialogue)
        self.story_context = QWidget()
        story_context = QFormLayout(self.story_context)
        story_context.setContentsMargins(0, 0, 0, 0)
        story_context.addRow("Story", self.story_title)
        story_context.addRow("Position", self.sequence_position)
        card_layout.addWidget(self.story_context)
        self.story_context.hide()
        self.sequence_notice = QLabel()
        self.sequence_notice.setWordWrap(True)
        self.sequence_notice.setAccessibleName("Story position recovery")
        self.sequence_notice.hide()
        card_layout.addWidget(self.sequence_notice)
        self.story_actions = QWidget()
        story_actions_layout = QHBoxLayout(self.story_actions)
        story_actions_layout.setContentsMargins(0, 0, 0, 0)
        story_actions_layout.addWidget(self.sequence_resync_button)
        story_actions_layout.addWidget(self.sequence_expected_button)
        story_actions_layout.addStretch()
        self.sequence_expected_button.hide()
        self.story_actions.hide()
        card_layout.addWidget(self.story_actions)
        self.current_audio = QWidget()
        current_audio = QFormLayout(self.current_audio)
        current_audio.setContentsMargins(0, 0, 0, 0)
        current_audio.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        current_audio.addRow("Voice", self.voice)
        current_audio.addRow("Audio", self.audio_source)
        self.current_audio.hide()
        self.speaker.hide()
        card_layout.addWidget(self.current_audio)
        return card

    def _build_reading_page(
        self,
        card: QGroupBox,
        reading_actions: QHBoxLayout,
    ) -> QWidget:
        reading_content = QWidget()
        layout = QVBoxLayout(reading_content)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.compact_button = QPushButton("Compact")
        set_action_icon(self.compact_button, "shrink")
        self.compact_button.clicked.connect(self.compact_requested.emit)
        self.reading_defaults = QLabel()
        self.reading_defaults.setWordWrap(True)
        self.reading_defaults.setAccessibleName("Live fallback voice and engine")
        self.speech_runtime = QLabel(speech_runtime_label(None))
        self.speech_runtime.setWordWrap(True)
        self.speech_runtime.setTextFormat(Qt.TextFormat.PlainText)
        self.speech_runtime.setAccessibleName("Live speech compute device")
        self.moss_runtime_button = QPushButton("Load OpenMOSS")
        set_action_icon(self.moss_runtime_button, "audio-lines")
        self.moss_runtime_button.setAccessibleName("Load or unload OpenMOSS model")
        self.moss_runtime_button.clicked.connect(self.moss_runtime_requested.emit)
        layout.addWidget(card)
        layout.addWidget(self.reading_defaults)
        self.details_layout.addWidget(self.speech_runtime)
        self.details_layout.addWidget(
            self.moss_runtime_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        self.details_layout.addWidget(self.reading_policy)
        detail_actions = QHBoxLayout()
        detail_actions.addStretch(1)
        detail_actions.addWidget(self.details_toggle)
        self.copy_details_button = copy_text_button("Copy details", self._copy_details)
        self.copy_details_button.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.details_layout.addWidget(
            self.copy_details_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        layout.addLayout(detail_actions)
        layout.addWidget(self.details_content)
        layout.addStretch()
        self.content_scroll = QScrollArea()
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.content_scroll.setWidget(reading_content)
        reading_page = QWidget()
        reading_layout = QVBoxLayout(reading_page)
        reading_layout.setContentsMargins(6, 6, 6, 6)
        reading_layout.addWidget(self.content_scroll, 1)
        reading_layout.addWidget(self._build_reading_controls(reading_actions))
        return reading_page

    def _build_reading_controls(self, reading_actions: QHBoxLayout) -> QGroupBox:
        controls = QGroupBox("Reading controls")
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(9, 6, 9, 6)
        controls_layout.setSpacing(4)
        control_status = QHBoxLayout()
        control_status.addWidget(self.reading_state)
        control_status.addWidget(self.capture_target, 1)
        controls_layout.addLayout(control_status)
        controls_layout.addWidget(self.action_reason)
        controls_layout.addWidget(
            self.prepare_reading_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        reading_actions.addWidget(self.compact_button)
        reading_actions.addStretch()
        controls_layout.addLayout(reading_actions)
        controls_layout.addWidget(self.auto_advance_check)
        self.auto_advance_focus_frame = QFocusFrame(controls)
        self.auto_advance_focus_frame.setWidget(self.auto_advance_check)
        controls_layout.addWidget(self.auto_advance_reason)
        return controls

    def _build_stories_page(self) -> QWidget:
        stories_page = QWidget()
        stories_layout = QVBoxLayout(stories_page)
        stories_title = QLabel("Prepare stories before opening the game")
        stories_title.setStyleSheet("font-weight: 600;")
        stories_layout.addWidget(stories_title)
        self.stories_guidance = QLabel(
            "Choose installed game content and the stories you want to hear. "
            "No game window, OCR, or speech setup is needed yet."
        )
        self.stories_guidance.setWordWrap(True)
        stories_layout.addWidget(self.stories_guidance)
        self.prepare_audio_button.setText("Choose stories and prepare audio...")
        self.prepare_audio_button.setDefault(True)
        stories_layout.addWidget(
            self.prepare_audio_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        self.stories_next = QLabel(
            "To read without preparation, open the Reading tab and start reading. "
            "Prepared audio remains available for matching lines."
        )
        self.stories_next.setWordWrap(True)
        stories_layout.addWidget(self.stories_next)
        stories_layout.addStretch()
        return stories_page

    def _build_voices_page(self) -> QWidget:
        voices_page = QWidget()
        voices_layout = QVBoxLayout(voices_page)
        voices_title = QLabel("Narrator and character voices")
        voices_title.setStyleSheet("font-weight: 600;")
        voices_layout.addWidget(voices_title)
        voices_layout.addWidget(self.speech_configuration)
        self.narrator_voice_button.setText("Edit voices...")
        self.narrator_voice_button.setDefault(True)
        voices_layout.addWidget(
            self.narrator_voice_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        voices_layout.addStretch()
        return voices_page

    def _build_shell(
        self,
        reading_page: QWidget,
        stories_page: QWidget,
        voices_page: QWidget,
        setup_group: QWidget,
    ) -> None:
        self.sections = QTabWidget()
        self.sections.setAccessibleName("Main application sections")
        self.stories_stack = QStackedWidget()
        self.stories_stack.addWidget(stories_page)
        self.sections.addTab(self.stories_stack, "Stories")
        self.voices_stack = QStackedWidget()
        self.voices_stack.addWidget(voices_page)
        self.sections.addTab(self.voices_stack, "Voices")
        self.sections.addTab(reading_page, "Reading")
        self.sections.currentChanged.connect(
            lambda index: self.main_section_changed.emit(main_sections[index])
        )
        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(12, 12, 12, 12)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        self.copy_status_button = copy_text_button(
            "Copy status", lambda: self.status.toolTip() or self.status.text()
        )
        self.copy_status_button.setAccessibleName("Copy status")
        self.copy_status_button.setToolTip("Copy status")
        self.copy_status_button.setText("")
        status_row.addWidget(self.copy_status_button)
        shell_layout.addLayout(status_row)
        shell_layout.addWidget(self.loading_panel)
        self.voice_edit_status = QPushButton("Voices: unsaved selection - Show")
        set_action_icon(self.voice_edit_status, "audio-lines")
        self.voice_edit_status.setAccessibleName("Pending voice selection")
        self.voice_edit_status.clicked.connect(self.show_voices)
        self.voice_edit_status.hide()
        self.sections.currentChanged.connect(
            lambda _index: self.set_voice_editor_busy(self._voice_busy)
        )
        shell_layout.addWidget(self.voice_edit_status)
        shell_layout.addWidget(self.sections, 1)
        shell_layout.addWidget(setup_group)
        self.setCentralWidget(shell)

    def _finish_setup(self, settings: AppSettings) -> None:
        self._set_details_expanded(False)
        self._set_setup_expanded(False)
        self.set_loading(False)
        self.set_ready(False)
        self.set_configuration(settings)
        self.set_status("")
        make_text_copyable(self)
        for form in self.findChildren(QFormLayout):
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            )

    def _copy_details(self) -> str:
        return (
            "\n\n".join(
                plain_label_text(label)
                for label in (
                    self.story_title,
                    self.sequence_position,
                    self.speaker,
                    self.dialogue,
                    self.speech_details,
                    self.playback_details,
                    self.voice,
                    self.audio_source,
                    self.reading_policy,
                    self.configuration,
                    self.sequence_identity,
                    self.sequence_notice,
                    self.reading_defaults,
                    self.reading_state,
                    self.capture_target,
                )
                if label.text()
            )
            + "\n\n"
            + (self.status.toolTip() or self.status.text())
            + "\n\n"
            + (self.speech_runtime.toolTip() or self.speech_runtime.text())
        )

    def set_speech_runtime(self, message: str) -> None:
        self.speech_runtime.setText(compact_runtime_label(message).replace("; ", "\n"))
        self.speech_runtime.setToolTip(message)

    def show_reading(self) -> None:
        self.sections.setCurrentIndex(2)

    def show_main_section(self, name: str) -> None:
        self.sections.setCurrentIndex(main_sections.index(name))

    def show_stories(self) -> None:
        self.sections.setCurrentIndex(0)

    def show_voices(self) -> None:
        self.sections.setCurrentIndex(1)

    def embed_narrator(self, panel: QWidget) -> None:
        panel.setWindowFlags(Qt.WindowType.Widget)
        panel.setMinimumSize(0, 0)
        self.voices_stack.addWidget(panel)
        self.voices_stack.setCurrentWidget(panel)
        self._voice_dirty = False
        self._voice_busy = False
        self.set_voice_editor_busy(False)
        self.show_voices()
        panel.show()

    def remove_narrator(self, panel: QWidget) -> None:
        self.voices_stack.removeWidget(panel)
        self.voices_stack.setCurrentIndex(0)
        self._voice_dirty = False
        self.set_voice_editor_busy(False)

    def set_voice_editor_busy(self, busy: bool) -> None:
        self._voice_busy = bool(busy)
        self.voice_edit_status.setText(
            "Voice editor is working - Show / cancel"
            if busy
            else "Voices: unsaved selection - Show"
        )
        self.voice_edit_status.setVisible(
            self.sections.currentWidget() is not self.voices_stack
            and (self._voice_busy or self._voice_dirty)
        )

    def set_voice_editor_dirty(self, dirty: bool) -> None:
        self._voice_dirty = bool(dirty)
        self.set_voice_editor_busy(self._voice_busy)

    def embed_preparation(self, panel: QWidget, *, show: bool = True) -> None:
        panel.setWindowFlags(Qt.WindowType.Widget)
        panel.setMinimumSize(0, 0)
        self.stories_stack.addWidget(panel)
        self.stories_stack.setCurrentWidget(panel)
        if show:
            self.show_stories()
        panel.show()

    def remove_preparation(self, panel: QWidget) -> None:
        self.stories_stack.removeWidget(panel)
        self.stories_stack.setCurrentIndex(0)

    def _set_details_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        self.details_toggle.blockSignals(True)
        self.details_toggle.setChecked(expanded)
        self.details_toggle.setText("Details")
        self.details_toggle.setAccessibleName(
            "Hide technical details" if expanded else "Show technical details"
        )
        self.details_toggle.setToolTip(self.details_toggle.accessibleName())
        self.details_toggle.blockSignals(False)
        self.details_content.setVisible(expanded)

    def _set_setup_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        self.setup_more_button.blockSignals(True)
        self.setup_more_button.setChecked(expanded)
        self.setup_more_button.setText("Tools")
        self.setup_more_button.setAccessibleName(
            "Hide tools" if expanded else "Show tools"
        )
        self.setup_more_button.setToolTip(self.setup_more_button.accessibleName())
        self.setup_more_button.blockSignals(False)
        self.setup_secondary_content.setVisible(expanded)

    def set_configuration(self, settings: AppSettings) -> None:
        self._reading_setup_label = (
            "Prepare reading" if settings.onboarding_completed else "Set up reading"
        )
        self.prepare_reading_button.setText(self._reading_setup_action())
        if not self._ready:
            self._set_action_reason(self._reading_setup_reason())
        self.keep_running_on_close = settings.keep_running_on_close
        self.set_speech_identity(settings)
        self.reading_policy.setText(reading_policy_label(settings))
        self._set_capture_configuration(settings)
        self.set_auto_advance_configuration(settings)

    def set_auto_advance_configuration(
        self, settings: AppSettings, *, busy: bool = False
    ) -> None:
        allowed, enabled, reason = auto_advance_control_state(
            settings.capture_mode,
            settings.live_sequence_mode,
            settings.auto_advance_enabled,
        )
        key = settings.auto_advance_key.replace("-", " ").title()
        self.auto_advance_check.setText(f"Auto advance after speech using {key}")
        was_blocked = self.auto_advance_check.blockSignals(True)
        self.auto_advance_check.setChecked(enabled)
        self.auto_advance_check.blockSignals(was_blocked)
        self.auto_advance_check.setEnabled(allowed and not busy)
        if settings.capture_mode != "window":
            visible_reason = (
                "Auto advance requires a game window selected in Settings > Capture."
            )
        elif not allowed:
            visible_reason = "Manual story mode never sends advance keys."
        elif busy:
            visible_reason = "Reading controls are updating. Try again when ready."
        else:
            visible_reason = ""
        self.auto_advance_reason.setText(visible_reason)
        self.auto_advance_reason.setVisible(bool(visible_reason))
        if not allowed:
            explanation = reason
        elif busy:
            explanation = "Reading controls are updating. Try again when ready."
        else:
            explanation = reason or (
                "Press the configured key in the selected game window after speech "
                "finishes, when the window and dialogue are safe. Change the key "
                "and delay in Settings > Playback > Show advanced settings."
            )
        self.auto_advance_check.setToolTip(explanation)
        self.auto_advance_check.setAccessibleDescription(explanation)

    def set_speech_identity(
        self, settings: AppSettings, narrator: str | CharacterVoice | None = None
    ) -> None:
        summary = speech_configuration_label(settings, narrator=narrator, compact=True)
        self.speech_configuration.setText(summary)
        rows = speech_configuration_rows(settings, narrator=narrator)
        set_labeled_text(self.reading_defaults, (("New speech narrator", rows[0][1]),))
        self.reading_defaults.setAccessibleName("Voice for new speech")
        self.speech_details.setText(
            engine_model_label(
                settings.speech_backend,
                settings.tts_model,
                pocket_cloning=settings.pocket_gated_model_accepted,
            )
            + (
                f"\nConfigured model: {settings.tts_model}"
                if settings.tts_model
                else ""
            )
            + (
                f"\nNarrator: {narrator}"
                if narrator and str(narrator) != rows[0][1]
                else ""
            )
        )
        self.speech_configuration.setToolTip(reading_policy_label(settings))

    def _set_capture_configuration(self, settings: AppSettings) -> None:
        capture = (
            settings.game_window_title or "No game window selected"
            if settings.capture_mode == "window"
            else "Calibrated screen region"
        )
        self.capture_target.setText(f"Capture: {capture}")
        sequence = {
            "off": "Disabled",
            "shadow": "Shadow diagnostics",
            "audio-manual": "Canonical audio; manual advance",
            "audio-auto": "Canonical audio; guarded automatic advance",
        }.get(settings.live_sequence_mode, settings.live_sequence_mode)
        manifest = settings.generated_audio_manifest
        generated_audio = (
            "not configured"
            if not manifest
            else "available"
            if Path(manifest).expanduser().is_file()
            else "missing; open Settings"
        )
        sequence_audio = bool(
            is_live_sequence_audio_mode(settings.live_sequence_mode)
            and settings.live_sequence_plan
            and settings.story_index
        )
        self._sequence_controls_available = sequence_audio
        self.sequence_group.setVisible(sequence_audio)
        self.story_actions.setVisible(sequence_audio)
        self.sequence_resync_button.setAccessibleDescription(
            "Choose the visible story event to anchor or recover sequence-first reading"
        )
        self.configuration.setText(
            f"Prepared recordings: {generated_audio}\n"
            f"Sequence: {sequence}\n"
            f"OCR: {settings.ocr_language}"
        )

    def set_sequence_status(self, status: LiveSequenceStatus) -> None:
        sequence_audio = is_live_sequence_audio_mode(status.mode)
        self._sequence_controls_available = (
            sequence_audio and status.state != "unavailable"
        )
        self.sequence_group.setVisible(sequence_audio)
        self.story_actions.setVisible(self._sequence_controls_available)
        self.sequence_resync_button.setEnabled(
            self._ready and self._sequence_controls_available
        )
        if not sequence_audio:
            self.sequence_notice.hide()
            self.sequence_expected_button.setEnabled(False)
            self.sequence_expected_button.hide()
            self.sequence_notice.clear()
            self.story_title.setText("No story position yet")
            self.sequence_position.setText("Not located")
            self.story_context.hide()
            return
        state = status.state
        reason = status.reason
        self.sequence_state.setText(state if not reason else f"{state} ({reason})")
        chapter = status.chapter
        sequence = status.sequence
        self.story_title.setText(
            status.story_title
            or (
                "Story title not recorded"
                if chapter is not None
                else "No story position yet"
            )
        )
        self.sequence_position.setText(
            "Not located"
            if chapter is None
            else f"Line {sequence if sequence is not None else '-'}"
        )
        self.story_context.setVisible(bool(status.story_title) or chapter is not None)
        event_id = status.event_id
        line_id = status.line_id
        self.sequence_identity.setText(
            f"Chapter {chapter}; {event_id or '-'} / {line_id or '-'}; "
            f"{status.next_event_count} next candidate(s)"
        )
        speaker = status.speaker
        text = status.text
        self.sequence_canonical.setText(
            "-" if not text else f"{speaker or 'Narrator'}: {text}"
        )
        self.sequence_expected_audio.setText(status.expected_audio_route)
        self.sequence_actual_audio.setText(status.actual_audio_route)
        self.sequence_ocr.setText(status.ocr_activity)
        guidance = status.guidance
        recovery = status.recovery_required
        self.sequence_notice.setText(
            guidance
            or (
                "Story position needs attention. Set the visible story line."
                if recovery
                else ""
            )
        )
        self.sequence_notice.setVisible(recovery)
        candidate_count = status.expected_candidate_count
        self._sequence_expected_candidate_count = candidate_count
        self.sequence_expected_button.setVisible(
            candidate_count > 0 and self._sequence_controls_available
        )
        self.sequence_expected_button.setEnabled(candidate_count > 0 and self._ready)
        self.sequence_expected_button.setText(
            "Use expected next line"
            if candidate_count == 1
            else (
                f"Choose among {candidate_count} expected lines..."
                if candidate_count > 1
                else "No expected next line"
            )
        )
        self.sequence_expected_button.setToolTip(
            "Advance only to a currently allowed sequence candidate; useful when "
            "two consecutive dialogue boxes look identical"
        )
        self.sequence_resync_button.setStyleSheet(
            "font-weight: 700;" if recovery else ""
        )
        self.sequence_resync_button.setToolTip(guidance)

    def set_status(self, message: str) -> None:
        self.status.setVisible(bool(message))
        self.copy_status_button.setVisible(bool(message))
        self.status.setToolTip(message)
        summary = " ".join(playback_status_label(message).split())
        self.status.setText(
            summary
            if len(summary) <= 240
            else summary[:240] + "… Copy status for the full message."
        )

    def set_dialogue(self, speaker: str | None, text: str | None) -> None:
        text = text or ""
        if (speaker, text) != (self._dialogue_speaker, self._dialogue_text):
            self.current_audio.hide()
            self.voice.clear()
            self.audio_source.clear()
            self.playback_details.clear()
        self._dialogue_speaker = speaker
        self._dialogue_text = text
        self.speaker.setText(speaker or "Narrator")
        self.speaker.setVisible(bool(text))
        self._update_dialogue_title()

    def _update_dialogue_title(self) -> None:
        self.dialogue.setText(
            self._dialogue_text
            or (
                "Waiting for dialogue in the game."
                if self._live
                else "Start reading to follow dialogue in the game."
                if self._ready
                else "The detected dialogue will appear here."
            )
        )
        self.dialogue_card.setTitle(
            "Current dialogue"
            if self._dialogue_current or not self._dialogue_text
            else "Last detected dialogue"
        )

    def set_loading(self, loading: bool) -> None:
        loading = bool(loading)
        self.loading_panel.setVisible(loading)
        self.prepare_reading_button.setEnabled(not loading)
        for button in self.loading_blocked_buttons:
            button.setEnabled(not loading)
        if loading:
            self.reading_state.setText("Loading speech engine")
            self._set_action_reason(
                "Reading controls unlock automatically when the speech engine "
                "finishes loading."
            )
        elif not self._ready:
            self.reading_state.setText(
                "Ready to start" if self._can_initialize else "Not ready"
            )
            self._set_action_reason(self._reading_setup_reason())

    def set_ready(self, ready: bool, *, reason: str | None = None) -> None:
        self.set_runtime_controls(
            RuntimeControlState(
                ready=bool(ready),
                unavailable_reason=reason,
            )
        )

    def set_runtime_controls(self, state: RuntimeControlState) -> None:
        self._ready = state.ready
        self._can_initialize = state.can_initialize
        self._unavailable_reason = state.unavailable_reason
        self._needs_readiness_check = state.needs_readiness_check
        self._dialogue_current = state.speaking or state.paused
        self._update_dialogue_title()
        self.reading_state.setText(
            "Loading speech engine"
            if not self.loading_panel.isHidden()
            else "Ready to start"
            if state.can_initialize
            else "Not ready"
            if not state.ready
            else "Finding current dialogue"
            if state.starting
            else "Speech paused"
            if state.paused
            else "Speaking current dialogue"
            if state.speaking
            else "Speech queued"
            if state.queued
            else "Waiting for dialogue"
            if state.live
            else "Reading stopped"
        )
        self.prepare_reading_button.setVisible(
            not state.ready and not state.can_initialize
        )
        window_problem = "window" in (state.unavailable_reason or "").casefold()
        self._window_problem = window_problem
        self.prepare_reading_button.setText(self._reading_setup_action())
        self.sequence_resync_button.setEnabled(
            state.ready and self._sequence_controls_available
        )
        self.sequence_expected_button.setEnabled(
            state.ready and self._sequence_expected_candidate_count > 0
        )
        if not self.loading_panel.isHidden():
            self._set_action_reason(
                "Reading controls unlock automatically when the speech engine "
                "finishes loading."
            )
        elif state.ready:
            self._set_action_reason(
                "Ready. Start reading, or read the current dialogue once."
            )
        else:
            self._set_action_reason(self._reading_setup_reason())
        _apply_reading_capabilities(
            state,
            read_button=self.read_button,
            live_button=self.live_button,
            pause_button=self.pause_button,
        )
        self.prepare_reading_button.setDefault(
            not state.ready and not state.active and not state.can_initialize
        )
        self.live_button.setDefault(state.ready or state.active or state.can_initialize)

    def _reading_setup_action(self) -> str:
        if self._window_problem:
            return "Select game window"
        if self._needs_readiness_check:
            return "Check readiness"
        return self._reading_setup_label

    def _reading_setup_reason(self) -> str:
        return self._unavailable_reason or (
            _INITIALIZE_READING_REASON
            if self._can_initialize
            else f"{self._reading_setup_label} to start."
        )

    def _set_action_reason(self, message: str) -> None:
        self.action_reason.setText(message)
        self.action_reason.setVisible(not self._ready and self.loading_panel.isHidden())
        if self._ready:
            self.live_button.setToolTip("")
            return
        description = message
        for button in (
            self.read_button,
            self.live_button,
            self.sequence_resync_button,
            self.sequence_expected_button,
        ):
            button.setToolTip(description)

    def set_live(self, running: bool) -> None:
        self._live = bool(running)
        if running:
            self.show_reading()
        self._update_dialogue_title()
        _set_live_button_presentation(self.live_button, running)
        if self._ready:
            self._set_action_reason(
                "Reading is active; Stop reading ends speech immediately."
                if running
                else "Ready: start reading in the game, or read the current dialogue once."
            )

    def set_diagnostic(self, snapshot: DiagnosticSnapshot) -> None:
        self.speaker.setText(snapshot.character or "Narrator")
        source = snapshot.audio_source or "Not selected"
        voice, audio = playback_labels(source, snapshot.voice or "Not resolved yet")
        self.voice.setText(voice)
        self.audio_source.setText(audio)
        self.playback_details.setText(
            (f"Reported voice: {snapshot.voice}\n" if snapshot.voice != voice else "")
            + source
        )
        self.current_audio.setVisible(source not in {"", "Not selected"})
        self.confidence.setText(f"{snapshot.confidence:.1f}%")
        parts: list[str] = []
        if snapshot.capture_ms is not None:
            parts.append(f"capture {snapshot.capture_ms:.0f} ms")
        if snapshot.ocr_ms is not None:
            parts.append(f"OCR {snapshot.ocr_ms:.0f} ms")
        if snapshot.synthesis_ms is not None:
            parts.append(f"speech {snapshot.synthesis_ms:.0f} ms")
        if snapshot.last_first_audio_ms is not None:
            parts.append(f"first audio {snapshot.last_first_audio_ms:.0f} ms")
        if snapshot.speech_queue_depth:
            parts.append(f"queue {snapshot.speech_queue_depth}")
        self.latency.setText(", ".join(parts) or "-")

    def request_quit(self) -> None:
        self._quitting = True
        self.close()
        self.quit_requested.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.keep_running_on_close and not self._quitting:
            event.ignore()
            self.hide()
            self.hidden_to_background.emit()
            return
        if not self._quitting:
            self._quitting = True
            self.quit_requested.emit()
        super().closeEvent(event)


class CompactController(QWidget):
    """Small floating controller that can accompany a fullscreen game."""

    read_requested = Signal()
    live_requested = Signal()
    pause_requested = Signal()
    full_requested = Signal()
    sequence_expected_requested = Signal()

    def __init__(
        self, parent: QWidget | None = None, *, platform: str | None = None
    ) -> None:
        super().__init__(parent)
        self._configure_window(platform)
        self._build_status_labels()
        self._build_buttons()
        self._build_layout()
        self.set_ready(False)

    def _configure_window(self, platform: str | None) -> None:
        platform = sys.platform if platform is None else platform
        self._live = False
        self._ready = False
        self._sequence_expected_candidate_count = 0
        self.setWindowTitle("VNTTS controls")
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        if platform == "darwin":
            # Qt hides tool windows when their application becomes inactive on
            # macOS unless this is set before the native window is shown. The
            # game taking focus must not make the in-game controls disappear.
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMinimumWidth(540)

    def _build_status_labels(self) -> None:
        self.message_focus_frame = QFocusFrame(self)
        self.mode = QLabel("Starting")
        mode_font = QFont()
        mode_font.setBold(True)
        self.mode.setFont(mode_font)
        self.mode.setAccessibleName("Reading state")
        self.mode.setContentsMargins(0, 4, 0, 0)
        self.mode.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Preferred,
        )
        self.status = self._message_field("Latest reading message", "Initializing...")
        self.action_reason = self._message_field("Compact control availability")
        self.action_reason.setAccessibleDescription(
            "Explains why compact reading controls are available or unavailable"
        )
        self.sequence_recovery = QLabel()
        self.sequence_recovery.setWordWrap(True)
        self.sequence_recovery.setAccessibleName("Story position recovery")
        self.sequence_recovery.hide()
        self.speaker_label = QLabel("Last speaker:")
        self.speaker_label.setContentsMargins(0, 4, 0, 0)
        self.speaker = self._message_field("Last dialogue speaker")
        self.speaker_label.setBuddy(self.speaker)
        self.speaker_label.hide()
        self.speaker.hide()

    def _message_field(self, name: str, text: str = "") -> QPlainTextEdit:
        field = QPlainTextEdit(text, readOnly=True, tabChangesFocus=True)
        field.setAccessibleName(name)
        field.setFrameShape(QFrame.Shape.NoFrame)
        field.viewport().setAutoFillBackground(False)
        field.setFixedHeight(field.fontMetrics().lineSpacing() * 2 + 12)
        field.installEventFilter(self)
        return field

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(watched, QPlainTextEdit)
            and event.type() == QEvent.Type.FontChange
        ):
            watched.setFixedHeight(watched.fontMetrics().lineSpacing() * 2 + 12)
            QTimer.singleShot(0, self, self._fit_content)
        if isinstance(watched, QPlainTextEdit):
            if event.type() == QEvent.Type.FocusIn:
                self.message_focus_frame.setWidget(watched)
                self.message_focus_frame.show()
            elif event.type() == QEvent.Type.FocusOut:
                self.message_focus_frame.hide()
        return super().eventFilter(watched, event)

    def _build_buttons(self) -> None:
        self.live_button = QPushButton("Read")
        set_action_icon(self.live_button, "play")
        self.pause_button = QPushButton("")
        set_action_icon(self.pause_button, "pause")
        self.read_button = QPushButton("Dialogue")
        set_action_icon(self.read_button, "volume-2")
        self.sequence_expected_button = QPushButton("Use expected next line")
        set_action_icon(self.sequence_expected_button, "skip-forward")
        self.sequence_expected_button.setVisible(False)
        self.full_button = QPushButton("Expand")
        set_action_icon(self.full_button, "expand")
        self.live_button.setDefault(True)
        self.live_button.setAccessibleDescription(
            "Start reading or immediately stop reading and speech"
        )
        for button in (
            self.read_button,
            self.live_button,
            self.pause_button,
            self.sequence_expected_button,
            self.full_button,
        ):
            button.setSizePolicy(
                QSizePolicy.Policy.Fixed,
                QSizePolicy.Policy.Fixed,
            )
        self.read_button.clicked.connect(self.read_requested.emit)
        self.live_button.clicked.connect(self.live_requested.emit)
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.sequence_expected_button.clicked.connect(
            self.sequence_expected_requested.emit
        )
        self.full_button.clicked.connect(self.full_requested.emit)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        information = QHBoxLayout()
        information.setSpacing(12)
        information.addWidget(self.mode, 0, Qt.AlignmentFlag.AlignTop)
        information.addWidget(self.speaker_label, 0, Qt.AlignmentFlag.AlignTop)
        information.addWidget(self.speaker, 1)
        information.addStretch()
        primary_controls = QHBoxLayout()
        primary_controls.setSpacing(6)
        primary_controls.addWidget(self.live_button)
        primary_controls.addWidget(self.pause_button)
        primary_controls.addWidget(self.read_button)
        primary_controls.addStretch(1)
        primary_controls.addWidget(self.full_button)
        layout.addLayout(information)
        layout.addWidget(self.status)
        layout.addWidget(self.action_reason)
        layout.addWidget(self.sequence_recovery)
        layout.addLayout(primary_controls)
        layout.addWidget(self.sequence_expected_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.setTabOrder(self.live_button, self.pause_button)
        self.setTabOrder(self.pause_button, self.read_button)
        self.setTabOrder(self.read_button, self.full_button)
        self.setTabOrder(self.full_button, self.sequence_expected_button)
        self.setTabOrder(self.sequence_expected_button, self.status)
        self.setTabOrder(self.status, self.action_reason)
        self.setTabOrder(self.action_reason, self.speaker)

    def set_sequence_status(self, status: LiveSequenceStatus) -> None:
        manual = is_live_sequence_audio_mode(status.mode)
        candidate_count = status.expected_candidate_count
        recovery = manual and status.recovery_required
        self.sequence_recovery.setText(
            "Story position needs attention. Choose an expected line here or "
            "open Full controls to resync."
            if candidate_count > 0
            else "Story position needs attention. Open Full controls to resync."
        )
        self.sequence_recovery.setVisible(recovery)
        self._sequence_expected_candidate_count = candidate_count
        self.sequence_expected_button.setVisible(manual and candidate_count > 0)
        self.sequence_expected_button.setEnabled(
            self._ready and manual and candidate_count > 0
        )
        self.sequence_expected_button.setText(
            "Use expected next line"
            if candidate_count == 1
            else f"Choose among {candidate_count} expected lines..."
        )
        self._fit_content()

    def show_for_game(self, geometry: WindowGeometry | None = None) -> None:
        self.show()
        QTimer.singleShot(0, self, lambda: self._finish_show(geometry))

    def _finish_show(self, geometry: WindowGeometry | None) -> None:
        configure_floating_window(self)
        self._fit_content()
        screen = None
        if geometry is not None:
            center = QPoint(
                geometry.left + geometry.width // 2,
                geometry.top + geometry.height // 2,
            )
            screen = QApplication.screenAt(center)
        screen = screen or self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        right = (
            min(geometry.left + geometry.width, available.right() + 1)
            if geometry is not None
            else available.right() + 1
        )
        top = (
            max(geometry.top, available.top())
            if geometry is not None
            else available.top()
        )
        x = max(available.left(), right - self.width() - 12)
        y = min(max(available.top(), top + 12), available.bottom() - self.height())
        self.move(x, y)
        self.raise_()

    def set_status(self, message: str) -> None:
        self.status.setPlainText(playback_status_label(message))
        self.status.setToolTip(message)
        self.status.setFont(QFont())
        self._fit_content()

    def set_warning(self, message: str) -> None:
        self.status.setPlainText(message)
        self.status.setToolTip(message)
        warning_font = QFont()
        warning_font.setBold(True)
        self.status.setFont(warning_font)
        self._fit_content()

    def set_dialogue(self, speaker: str | None, text: str | None) -> None:
        if not text:
            return
        self.speaker.setPlainText(speaker or "Narrator")
        self.speaker_label.show()
        self.speaker.show()
        self._fit_content()

    def set_ready(self, ready: bool, *, reason: str | None = None) -> None:
        self.set_runtime_controls(
            RuntimeControlState(
                ready=bool(ready),
                unavailable_reason=reason,
            )
        )

    def set_runtime_controls(self, state: RuntimeControlState) -> None:
        self._ready = state.ready
        self._live = state.live
        self.mode.setText(
            "Stopping"
            if state.transition == "stopping"
            else "Starting reading"
            if state.starting or state.transition == "starting"
            else "Paused"
            if state.paused
            else "Reading"
            if state.live
            else "Speaking"
            if state.speaking or state.queued
            else "Ready"
            if state.ready
            else "Ready to start"
            if state.can_initialize
            else "Unavailable"
            if state.unavailable_reason
            else "Not ready"
        )
        _apply_reading_capabilities(
            state,
            read_button=self.read_button,
            live_button=self.live_button,
            pause_button=self.pause_button,
        )
        self.sequence_expected_button.setEnabled(
            state.ready
            and not self.sequence_expected_button.isHidden()
            and self._sequence_expected_candidate_count > 0
        )
        reason = (
            state.unavailable_reason
            or "VNTTS is not ready. Select Full controls, then Check readiness."
        )
        self.action_reason.setPlainText(
            state.reason_for("live")
            if state.can_initialize
            else f"Reading unavailable: {reason}"
        )
        self.sequence_expected_button.setToolTip("" if state.ready else reason)
        self._fit_content()

    def set_live(self, running: bool) -> None:
        self._live = bool(running)
        self.mode.setText("Reading" if running else "Stopped")
        _set_live_button_presentation(self.live_button, running)
        self._fit_content()

    def set_paused(self, paused: bool) -> None:
        self.mode.setText(
            "Paused" if paused else ("Reading" if self._live else "Stopped")
        )
        self._fit_content()

    def _fit_content(self) -> None:
        self.action_reason.setVisible(
            not self._ready
            and self.action_reason.toPlainText().removeprefix("Reading unavailable: ")
            != self.status.toPlainText()
        )
        right = self.x() + self.width()
        top = self.y()
        self.adjustSize()
        layout = self.layout()
        if layout is not None:
            self.resize(self.width(), layout.totalHeightForWidth(self.width()))
        if self.isVisible():
            self.move(right - self.width(), top)


def configure_floating_window(window: QWidget, *, platform: str | None = None) -> bool:
    """Keep compact controls usable in fullscreen and out of system capture."""
    platform = sys.platform if platform is None else platform
    try:
        native_id = int(window.winId())
        if platform == "darwin":
            if QApplication.platformName() != "cocoa":
                return False
            import AppKit
            import objc

            native_view = objc.objc_object(c_void_p=native_id)
            native_window = native_view.window()
            behavior = native_window.collectionBehavior()
            behavior &= ~AppKit.NSWindowCollectionBehaviorMoveToActiveSpace
            behavior &= ~AppKit.NSWindowCollectionBehaviorFullScreenPrimary
            behavior |= AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            behavior |= AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            native_window.setCollectionBehavior_(behavior)
            native_window.setLevel_(AppKit.NSFloatingWindowLevel)
            native_window.setSharingType_(
                AppKit.NSWindowSharingReadOnly
                if os.environ.get("VNTTS_ALLOW_SCREEN_CAPTURE") == "1"
                else AppKit.NSWindowSharingNone
            )
            return True
        if platform == "win32":
            if sys.platform != "win32":
                return False
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            affinity = getattr(user32, "SetWindowDisplayAffinity", None)
            if affinity is None:
                return False
            affinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            affinity.restype = ctypes.c_bool
            # Windows 10 2004+: omit the controller from screenshots/capture.
            return bool(affinity(ctypes.c_void_p(native_id), 0x11))
    except Exception:
        return False
    return False

from __future__ import annotations

import ctypes
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QFont,
    QFontMetricsF,
    QKeyEvent,
    QPalette,
    QStandardItem,
    QStandardItemModel,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFocusFrame,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from vntts.action_icons import set_action_icon
from vntts.auto_advance_policy import auto_advance_control_state
from vntts.pocket_model import pocket_model_ready
from vntts.settings import AppSettings, is_live_sequence_audio_mode
from vntts.speech_presentation import (
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
)
from vntts.window_capture import WindowCaptureError, WindowInfo, list_windows

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
    output_interrupted: bool = False
    output_available: bool = True
    transition: str | None = None
    unavailable_reason: str | None = None
    needs_readiness_check: bool = False
    indexed_current: bool = False
    window_hint: str | None = None
    waiting_for_game: bool = False
    speaking_character: str = ""
    speaking_text: str = ""
    audio_source: str = ""
    playback_voice: str = ""
    loading_voice: bool = False
    finding_current_line: bool = False
    reading_notice: str = ""

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
            else "Read the dialogue currently visible on screen once."
        )

    @property
    def active(self) -> bool:
        return self.starting or self.live or self.paused or self.speaking or self.queued

    @property
    def can_toggle_live(self) -> bool:
        return (
            self.active
            or self.transition == "starting"
            or (self.output_available and (self.ready or self.can_initialize))
        ) and self.transition != "stopping"

    @property
    def can_pause(self) -> bool:
        if self.starting or self.transition is not None:
            return False
        if self.output_interrupted:
            return self.ready and self.output_available
        if self.paused:
            return self.output_available
        return self.live or self.speaking or self.queued

    @property
    def pause_label(self) -> str:
        return (
            "Resume speech"
            if self.paused or self.output_interrupted
            else "Pause speech"
        )

    @property
    def pause_description(self) -> str:
        if self.output_interrupted:
            return "Resume the dialogue interrupted by an audio output change."
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
        if (
            control == "pause"
            and not self.output_available
            and (self.paused or self.output_interrupted)
        ):
            return "Connect an audio output device before resuming speech."
        if control == "live" and not self.output_available:
            return "Connect an audio output device before starting reading."
        if not self.ready:
            return (
                self.window_hint
                or self.unavailable_reason
                or (
                    _INITIALIZE_READING_REASON
                    if self.can_initialize
                    else "VNTTS is not ready. Select Check readiness."
                )
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
    icon_only: bool = False,
) -> None:
    read_button.setText("Indexed line" if state.indexed_current else "Read once")
    read_button.setAccessibleName(state.read_label)
    read_button.setAccessibleDescription(state.read_tooltip)
    read_button.setEnabled(state.can_read)
    set_action_icon(
        pause_button, "play" if state.paused or state.output_interrupted else "pause"
    )
    pause_button.setText("")
    pause_button.setAccessibleName(state.pause_label)
    pause_button.setEnabled(state.can_pause)
    pause_reason = (
        state.pause_description if state.can_pause else state.reason_for("pause")
    )
    pause_button.setToolTip(pause_reason)
    pause_button.setAccessibleDescription(pause_reason)
    live_button.setEnabled(state.can_toggle_live)
    _set_live_button_presentation(live_button, state.active, icon_only=icon_only)
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
            ""
            if icon_only
            else "Cancel"
            if state.transition == "starting"
            else "Stopping..."
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


def _set_live_button_presentation(
    button: QPushButton, running: bool, *, icon_only: bool = False
) -> None:
    set_action_icon(button, "square" if running else "play")
    button.setText("" if icon_only else "Stop" if running else "Read")
    button.setAccessibleName("Stop reading" if running else "Start reading")
    button.setToolTip(button.accessibleName())
    button.setDefault(True)
    button.setAccessibleDescription(
        "Primary action: immediately stop reading, current speech, queued speech and auto advance"
        if running
        else "Primary action: start following dialogue in the game"
    )


class _GameWindowComboBox(QComboBox):
    popup_requested = Signal()

    def showPopup(self) -> None:
        self.popup_requested.emit()
        super().showPopup()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self.view().isVisible() and (
            event.key()
            in (
                Qt.Key.Key_Up,
                Qt.Key.Key_Down,
                Qt.Key.Key_Home,
                Qt.Key.Key_End,
                Qt.Key.Key_PageUp,
                Qt.Key.Key_PageDown,
                Qt.Key.Key_Return,
                Qt.Key.Key_Enter,
                Qt.Key.Key_Space,
            )
            or (
                event.text().isprintable()
                and event.modifiers()
                in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier)
            )
        ):
            self.showPopup()
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


class ControlDashboard(QMainWindow):
    reading_setup_requested = Signal()
    game_window_requested = Signal(object)
    screen_capture_requested = Signal()
    read_requested = Signal()
    live_requested = Signal()
    pause_requested = Signal()
    auto_advance_requested = Signal(bool)
    sequence_resync_requested = Signal()
    sequence_expected_requested = Signal()
    pregeneration_requested = Signal()
    readiness_requested = Signal()
    audio_setup_requested = Signal()
    voices_requested = Signal()
    diagnostics_requested = Signal()
    settings_requested = Signal()
    compact_requested = Signal()
    check_dialogue_area_requested = Signal()
    quit_requested = Signal()
    hidden_to_background = Signal()

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        *,
        window_provider: Callable[[], Sequence[WindowInfo]] = list_windows,
    ) -> None:
        super().__init__(parent)
        self._window_provider = window_provider
        self._game_window_title = settings.game_window_title
        self._capture_mode = settings.capture_mode
        self._initialize_state(settings)
        self._build_status_widgets()
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
        self._sequence_expected_candidate_count = 0
        self._sequence_controls_available = False
        self._window_hint: str | None = None
        self._needs_readiness_check = False
        self._voice_dirty = False
        self._voice_busy = False
        self._report: dict[str, str] = {"Compute": speech_runtime_label(None)}
        self._recognition_text: str | None = None
        self._recognition_uncertain = False
        self._recognition_attempts = 0
        self._recognition_persistent = False
        self._recognition_allowed = False
        self._status_reading_detail = False
        self._recognition_timer = QTimer(self)
        self._recognition_timer.setSingleShot(True)
        self._recognition_timer.setInterval(1500)
        self._recognition_timer.timeout.connect(self._recognition_timeout)
        self._sequence_issue = False
        self.setWindowTitle("Visual Novel Text to Speech")
        self.resize(860, 560)

    def _build_status_widgets(self) -> None:
        self.status = QLabel("Starting...")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(False)
        status_policy = QSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        status_policy.setRetainSizeWhenHidden(True)
        self.status.setSizePolicy(status_policy)
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
        self.reading_state.setTextFormat(Qt.TextFormat.PlainText)
        self.reading_state.setWordWrap(True)
        self.reading_state.setStyleSheet("font-weight: 600;")
        self.speaker = QLabel()
        self.speaker.setTextFormat(Qt.TextFormat.PlainText)
        self.speaker.setAccessibleName("Current dialogue speaker")
        self.speaker.setWordWrap(True)
        self.voice = QLabel()
        self.audio_source = QLabel()
        self.voice.setWordWrap(True)
        self.audio_source.setWordWrap(True)
        self.voice.setTextFormat(Qt.TextFormat.PlainText)
        self.audio_source.setTextFormat(Qt.TextFormat.PlainText)
        self.speech_configuration = QLabel()
        self.speech_configuration.setWordWrap(True)
        self.speech_configuration.setAccessibleName("Narrator and speech engine")
        self.dialogue = QPlainTextEdit(
            "The detected dialogue will appear here.",
            readOnly=True,
            tabChangesFocus=True,
        )
        self.dialogue.setAccessibleName("Full dialogue text")
        dialogue_font = self.dialogue.font()
        dialogue_font.setPointSize(dialogue_font.pointSize() + 2)
        self.dialogue.setFont(dialogue_font)
        self._fit_text_height(self.dialogue, 4)
        self.dialogue.installEventFilter(self)

    def _fit_text_height(self, field: QPlainTextEdit, lines: int) -> None:
        line_height = ceil(QFontMetricsF(field.font(), field).lineSpacing())
        margins = 2 * (ceil(field.document().documentMargin()) + field.frameWidth())
        # Qt treats a line exactly on the viewport boundary as partially visible.
        field.setFixedHeight(line_height * lines + margins + 1)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.FontChange:
            if watched is self.dialogue:
                self._fit_text_height(self.dialogue, 4)
            elif watched is self.recognition:
                self._fit_text_height(self.recognition, 2)
            elif watched in (self.live_button, self.pause_button):
                self._fit_live_action_width()
        return super().eventFilter(watched, event)

    def _build_action_buttons(self) -> QHBoxLayout:
        self.read_button = QPushButton("Read once")
        set_action_icon(self.read_button, "volume-2")
        self.live_button = QPushButton("")
        set_action_icon(self.live_button, "play")
        self.pause_button = QPushButton("")
        set_action_icon(self.pause_button, "pause")
        self._fit_live_action_width()
        self.live_button.installEventFilter(self)
        self.pause_button.installEventFilter(self)
        self.sequence_resync_button = QPushButton("Choose story line...")
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
        reading_actions.addWidget(self.read_button)
        self.history_button = QToolButton()
        self.history_button.setText("History")
        self.history_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.history_button.setEnabled(False)
        reading_actions.addWidget(self.history_button)
        self.prepare_reading_button = QPushButton("Set up and start")
        set_action_icon(self.prepare_reading_button, "settings")
        self.prepare_reading_button.clicked.connect(self._request_reading_setup)
        self.prepare_audio_button = QPushButton("Prepare offline audio...")
        set_action_icon(self.prepare_audio_button, "audio-lines")
        self.prepare_audio_button.setAccessibleDescription(
            "Choose stories and prepare their voices locally with guided defaults"
        )
        self.prepare_audio_button.clicked.connect(self.pregeneration_requested.emit)
        self.loading_blocked_buttons = [self.prepare_audio_button]
        return reading_actions

    def _fit_live_action_width(self) -> None:
        for button in (self.live_button, self.pause_button):
            button.setFixedWidth(
                max(button.iconSize().width(), button.fontMetrics().height()) + 20
            )

    def _build_sequence_widgets(self) -> None:
        self.story_title = QLabel("No story position yet")
        self.story_title.setWordWrap(True)
        self.story_title.setTextFormat(Qt.TextFormat.PlainText)
        self.story_title.setAccessibleName("Current story")
        self.sequence_position = QLabel("Not located")
        self.sequence_position.setWordWrap(True)
        self.sequence_position.setTextFormat(Qt.TextFormat.PlainText)
        self.sequence_position.setAccessibleName("Current story position")

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
        self.loading_blocked_buttons.append(self.setup_primary_button)
        self.setup_more_button = QPushButton("Tools")
        set_action_icon(self.setup_more_button, "list")
        self.setup_more_button.setCheckable(True)
        self.setup_more_button.setAccessibleDescription(
            "Show or hide setup, OCR, support, and application tools"
        )
        setup_primary.addWidget(self.setup_more_button)
        setup_primary.addStretch()
        setup.addLayout(setup_primary)
        self.setup_secondary_content = QWidget()
        secondary = QVBoxLayout(self.setup_secondary_content)
        secondary.setContentsMargins(0, 0, 0, 0)
        setup_secondary = QHBoxLayout()
        secondary.addLayout(setup_secondary)
        setup_secondary.addWidget(self.setup_primary_button)
        self.setup_buttons = [self.setup_primary_button, self.setup_more_button]
        for label, icon, signal in (
            ("Narrator voice", "audio-lines", self.voices_requested),
            ("Support and logs", "circle-help", self.diagnostics_requested),
            ("Settings", "settings", self.settings_requested),
        ):
            button = QPushButton(label)
            set_action_icon(button, icon)
            button.clicked.connect(signal.emit)
            if label == "Settings":
                setup_primary.insertWidget(0, button)
            elif label != "Narrator voice":
                setup_secondary.addWidget(button)
            self.setup_buttons.append(button)
            if label != "Support and logs":
                self.loading_blocked_buttons.append(button)
            if label == "Narrator voice":
                self.narrator_voice_button = button
            elif label == "Settings":
                self.settings_button = button
        self.quit_button = QPushButton("Quit VNTTS")
        set_action_icon(self.quit_button, "log-out")
        self.quit_button.clicked.connect(self.request_quit)
        setup_secondary.addWidget(self.quit_button)
        setup_secondary.addStretch()
        self.tool_menus = QHBoxLayout()
        self.tool_menus.setAlignment(Qt.AlignmentFlag.AlignLeft)
        secondary.addLayout(self.tool_menus)
        self.setup_buttons.append(self.quit_button)
        setup.addWidget(self.setup_secondary_content)
        self.setup_more_button.toggled.connect(self._set_setup_expanded)
        return setup_group

    def add_tool_menu(self, title: str, actions: Sequence[QAction]) -> None:
        button = QPushButton(title)
        menu = QMenu(button)
        menu.addActions(actions)
        button.setMenu(menu)
        self.tool_menus.addWidget(button)

    def _build_dialogue_card(self) -> QGroupBox:
        card = QGroupBox("Current dialogue")
        self.dialogue_card = card
        card_layout = QVBoxLayout(card)
        self.speaker.setStyleSheet("font-weight: 600;")
        dialogue_header = QHBoxLayout()
        dialogue_header.addWidget(self.speaker, 1)
        self.dialogue_info_button = QToolButton()
        set_action_icon(self.dialogue_info_button, "info")
        self.dialogue_info_button.setCheckable(True)
        self.dialogue_info_button.setAccessibleName(
            "Show voice and audio for this line"
        )
        self.dialogue_info_button.setToolTip("Voice and audio for this line")
        self.dialogue_info_button.toggled.connect(self._set_current_audio_expanded)
        self.dialogue_info_button.hide()
        dialogue_header.addWidget(self.dialogue_info_button)
        card_layout.addLayout(dialogue_header)
        card_layout.addWidget(self.dialogue)
        self.story_context = QWidget()
        story_context = QFormLayout(self.story_context)
        story_context.setContentsMargins(0, 0, 0, 0)
        story_context.addRow("Story", self.story_title)
        story_context.addRow("Position", self.sequence_position)
        self.story_context.hide()
        self.sequence_notice = QLabel()
        self.sequence_notice.setWordWrap(True)
        self.sequence_notice.setAccessibleName("Reading notice")
        self.sequence_notice.hide()
        self.story_actions = QWidget()
        story_actions_layout = QHBoxLayout(self.story_actions)
        story_actions_layout.setContentsMargins(0, 0, 0, 0)
        story_actions_layout.addWidget(self.sequence_resync_button)
        story_actions_layout.addWidget(self.sequence_expected_button)
        story_actions_layout.addStretch()
        self.sequence_expected_button.hide()
        self.story_actions.hide()
        self.current_audio = QWidget()
        current_audio = QFormLayout(self.current_audio)
        current_audio.setContentsMargins(0, 0, 0, 0)
        current_audio.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        current_audio.addRow("Voice", self.voice)
        current_audio.addRow("Audio", self.audio_source)
        self.current_audio.hide()
        card.hide()
        card_layout.addWidget(self.current_audio)
        return card

    def _build_recognition_area(self) -> QWidget:
        self.recognition = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.recognition.setAccessibleName("Last recognized on screen")
        self.recognition.setPlaceholderText("No dialogue text was recognized.")
        self._fit_text_height(self.recognition, 2)
        self.recognition.installEventFilter(self)
        self.recognition_area = QWidget()
        recognition_layout = QVBoxLayout(self.recognition_area)
        recognition_layout.setContentsMargins(0, 0, 0, 0)
        recognition_label = QLabel("Last recognized on screen")
        recognition_label.setBuddy(self.recognition)
        self.recognition_note = QLabel(
            "Can’t read the dialogue reliably. Check that the dialogue area covers the text."
        )
        self.recognition_note.setWordWrap(True)
        recognition_layout.addWidget(self.recognition_note)
        recognition_layout.addWidget(recognition_label)
        recognition_layout.addWidget(self.recognition)
        self.check_dialogue_area_button = QPushButton("Check dialogue area")
        set_action_icon(self.check_dialogue_area_button, "scan")
        self.check_dialogue_area_button.clicked.connect(
            self.check_dialogue_area_requested.emit
        )
        recognition_layout.addWidget(
            self.check_dialogue_area_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        self.recognition_area.hide()
        return self.recognition_area

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
        layout.addWidget(card)
        layout.addWidget(self.story_context)
        layout.addWidget(self.sequence_notice)
        layout.addWidget(self.story_actions)
        layout.addWidget(self._build_recognition_area())
        layout.addStretch(1)
        self.content_scroll = QScrollArea()
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.content_scroll.setWidget(reading_content)
        self.content_scroll.viewport().setAutoFillBackground(False)
        reading_content.setAutoFillBackground(False)
        reading_page = QWidget()
        reading_layout = QVBoxLayout(reading_page)
        reading_layout.setContentsMargins(6, 6, 6, 6)
        reading_layout.addWidget(self._build_reading_controls(reading_actions))
        reading_layout.addWidget(self.content_scroll, 1)
        return reading_page

    def _build_reading_controls(self, reading_actions: QHBoxLayout) -> QGroupBox:
        controls = QGroupBox()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(9, 6, 9, 6)
        controls_layout.setSpacing(4)
        controls_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.game_window = _GameWindowComboBox()
        self.game_window_model = QStandardItemModel(self.game_window)
        self.game_window.setModel(self.game_window_model)
        self.game_window.setAccessibleName("Capture source")
        self.game_window.setAccessibleDescription(
            "Select a running game window or screen region for dialogue reading; the list refreshes when opened"
        )
        self.game_window.setMinimumContentsLength(12)
        self.game_window.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.game_window.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.game_window.popup_requested.connect(self.refresh_windows)
        self.game_window.activated.connect(self._request_game_window)
        self.capture_source_label = QLabel("Capture source")
        self.capture_source_label.setBuddy(self.game_window)
        primary_actions = QGridLayout()
        primary_actions.addWidget(self.capture_source_label, 0, 2)
        primary_actions.addWidget(self.live_button, 1, 0)
        primary_actions.addWidget(self.pause_button, 1, 1)
        primary_actions.addWidget(self.game_window, 1, 2)
        primary_actions.setColumnStretch(2, 1)
        controls_layout.addLayout(primary_actions)
        control_status = QHBoxLayout()
        control_status.addWidget(self.prepare_reading_button)
        control_status.addWidget(self.reading_state, 1)
        controls_layout.addLayout(control_status)
        controls_layout.addWidget(self.action_reason)
        reading_actions.addWidget(self.compact_button)
        reading_actions.addStretch()
        controls_layout.addLayout(reading_actions)
        controls_layout.addWidget(self.auto_advance_check)
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
        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(12, 12, 12, 12)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        self.audio_setup_button = QPushButton("Audio setup")
        self.audio_setup_button.setAccessibleName("Open audio setup instructions")
        self.audio_setup_button.setToolTip(
            "Check audio output and fix its installation"
        )
        self.audio_setup_button.clicked.connect(self.audio_setup_requested.emit)
        self.audio_setup_button.hide()
        status_row.addWidget(self.audio_setup_button)
        self.copy_status_button = copy_text_button(
            "Copy status", lambda: self.status.toolTip() or self.status.text()
        )
        self.copy_status_button.setAccessibleName("Copy status")
        self.copy_status_button.setToolTip("Copy status")
        self.copy_status_button.setText("")
        copy_policy = self.copy_status_button.sizePolicy()
        copy_policy.setRetainSizeWhenHidden(True)
        self.copy_status_button.setSizePolicy(copy_policy)
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
        self.sections.currentChanged.connect(self._update_status_visibility)
        shell_layout.addWidget(self.voice_edit_status)
        shell_layout.addWidget(self.sections, 1)
        shell_layout.addWidget(setup_group)
        self.setCentralWidget(shell)

    def _finish_setup(self, settings: AppSettings) -> None:
        self._set_setup_expanded(False)
        self.set_loading(False)
        self.set_ready(False)
        self.set_configuration(settings)
        self.set_status("")
        make_text_copyable(self)
        self.setTabOrder(self.live_button, self.pause_button)
        self.setTabOrder(self.pause_button, self.game_window)
        self.setTabOrder(self.game_window, self.read_button)
        self.setTabOrder(self.read_button, self.history_button)
        self.setTabOrder(self.history_button, self.compact_button)
        self.setTabOrder(self.compact_button, self.auto_advance_check)
        self.setTabOrder(self.auto_advance_check, self.dialogue_info_button)
        self.setTabOrder(self.settings_button, self.setup_more_button)
        self.setTabOrder(self.setup_more_button, self.setup_primary_button)
        for form in self.findChildren(QFormLayout):
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            )

    def reading_report(self) -> str:
        """Return current reading diagnostics for the support dialog on demand."""
        parts = [f"Reading: {self.reading_state.text()}"]
        if not self.story_context.isHidden():
            parts.append(
                f"Story: {self.story_title.text()}\nPosition: {self.sequence_position.text()}"
            )
        if self._dialogue_text:
            parts.append(f"Dialogue: {self.speaker.text()}\n{self._dialogue_text}")
        if self.audio_source.text():
            parts.append(
                f"Voice: {self.voice.text()}\nAudio: {self.audio_source.text()}"
            )
        if self.sequence_notice.text():
            parts.append(self.sequence_notice.text())
        parts.extend(
            f"{label}: {value}" for label, value in self._report.items() if value
        )
        if self._recognition_text is not None:
            parts.append(
                "Last recognized on screen:\n"
                + (self._recognition_text or self.recognition.placeholderText())
                + ("\nRecognition is uncertain." if self._recognition_uncertain else "")
            )
        status = self.status.toolTip() or self.status.text()
        if status:
            parts.append(status)
        return "\n\n".join(parts)

    def set_speech_runtime(self, message: str) -> None:
        self._report["Compute"] = message

    def show_reading(self) -> None:
        self.sections.setCurrentIndex(2)

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
        self.prepare_reading_button.setText(self._reading_setup_action())
        if not self._ready:
            self._set_action_reason(self._reading_setup_reason())
        self.keep_running_on_close = settings.keep_running_on_close
        self.set_speech_identity(settings)
        self._report["Audio policy"] = reading_policy_label(settings)
        self._game_window_title = (
            settings.game_window_title if settings.capture_mode == "window" else None
        )
        self._set_game_window_selection(settings)
        self._set_capture_configuration(settings)
        self.set_auto_advance_configuration(settings)

    def _set_game_window_selection(self, settings: AppSettings) -> None:
        self._capture_mode = settings.capture_mode
        title = self._game_window_title or ""
        was_blocked = self.game_window.blockSignals(True)
        screen_index = self.game_window.findData("screen")
        if screen_index < 0:
            self.game_window.addItem("Screen region", "screen")
            screen_index = self.game_window.count() - 1
        index = next(
            (
                index
                for index in range(self.game_window.count())
                if self.game_window.itemText(index) == title
                and self.game_window.itemData(index) != "screen"
            ),
            -1,
        )
        if title and index < 0:
            self.game_window.addItem(title)
            index = self.game_window.count() - 1
        if settings.capture_mode != "window":
            index = screen_index
        self.game_window.setCurrentIndex(index)
        self.game_window.setPlaceholderText("Select capture source")
        self.game_window.setToolTip(
            self.game_window.currentText() or self.game_window.placeholderText()
        )
        self.game_window.blockSignals(was_blocked)

    def refresh_windows(self) -> None:
        message = ""
        try:
            windows = [
                window for window in self._window_provider() if window.title.strip()
            ]
        except (WindowCaptureError, OSError) as error:
            windows = []
            message = (
                f"Windows could not be listed: {error}. "
                "Grant capture permission, then open this list again."
            )
        if not windows and not message:
            message = (
                "No game windows found. Start the game, then open this list again."
            )
        counts = Counter(window.title.strip().casefold() for window in windows)
        was_blocked = self.game_window.blockSignals(True)
        self.game_window_model.clear()
        selected = -1
        if message:
            item = QStandardItem(message)
            item.setEnabled(False)
            self.game_window_model.appendRow(item)
        for window in windows:
            index = self.game_window_model.rowCount()
            item = QStandardItem(window.title)
            item.setData(window, Qt.ItemDataRole.UserRole)
            if (
                window.title.strip().casefold()
                == (self._game_window_title or "").strip().casefold()
            ):
                selected = index
            if counts[window.title.strip().casefold()] > 1:
                item.setEnabled(False)
                item.setToolTip(
                    "More than one window has this title. Close the duplicate window before selecting it."
                )
            self.game_window_model.appendRow(item)
        if self._game_window_title and selected < 0:
            item = QStandardItem(self._game_window_title + " (unavailable)")
            item.setEnabled(False)
            selected = self.game_window_model.rowCount()
            self.game_window_model.appendRow(item)
        screen = QStandardItem("Screen region")
        screen.setData("screen", Qt.ItemDataRole.UserRole)
        if self._capture_mode != "window":
            selected = self.game_window_model.rowCount()
        self.game_window_model.appendRow(screen)
        self.game_window.setCurrentIndex(selected)
        self.game_window.blockSignals(was_blocked)
        self.game_window.setAccessibleDescription(
            message
            or "Select a running game window or screen region. Duplicate titles cannot be selected."
        )
        self.game_window.setToolTip(self.game_window.accessibleDescription())

    def _request_game_window(self, index: int) -> None:
        window = self.game_window.itemData(index)
        item = self.game_window_model.item(index)
        if item is None or not item.isEnabled():
            return
        if window == "screen":
            self.screen_capture_requested.emit()
        elif isinstance(window, WindowInfo):
            self.game_window_requested.emit(window)

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
            visible_reason = "Auto advance requires a game window selected above."
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
        self._report["New speech narrator"] = rows[0][1]
        self._report["Speech engine"] = (
            engine_model_label(
                settings.speech_backend,
                settings.tts_model,
                pocket_cloning=pocket_model_ready(),
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
            else "Screen region"
        )
        if self._report.get("Capture") != capture:
            self._clear_line_audio()
            self.set_recognition(None)
        self._report["Capture"] = capture
        sequence = {
            "off": "Matched from the screen",
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
        self.story_actions.setVisible(self._ready and sequence_audio)
        self.sequence_resync_button.setAccessibleDescription(
            "Choose the visible story event to anchor or recover sequence-first reading"
        )
        self._report["Configuration"] = (
            f"Prepared recordings: {generated_audio}\n"
            f"Story order: {sequence}\n"
            f"OCR: {settings.ocr_language}"
        )

    def set_sequence_status(self, status: LiveSequenceStatus) -> None:
        sequence_audio = is_live_sequence_audio_mode(status.mode)
        self._sequence_controls_available = (
            sequence_audio and status.state != "unavailable"
        )
        self.story_actions.setVisible(self._ready and self._sequence_controls_available)
        self.sequence_resync_button.setEnabled(
            self._ready and self._sequence_controls_available
        )
        self._sequence_issue = status.recovery_required or bool(
            status.reason
            and status.guidance
            and (not sequence_audio or status.state == "unavailable")
        )
        self._update_reading_issue()
        if not sequence_audio:
            for label in (
                "Cursor state",
                "Event / line",
                "Canonical dialogue",
                "Expected audio",
                "Actual audio",
                "OCR activity",
            ):
                self._report.pop(label, None)
            self.sequence_notice.setText(status.guidance if status.reason else "")
            self.sequence_notice.setVisible(bool(status.reason and status.guidance))
            self._sequence_expected_candidate_count = 0
            self.sequence_expected_button.setEnabled(False)
            self.sequence_expected_button.hide()
            self.story_title.setText("No story position yet")
            self.sequence_position.setText("Not located")
            self.story_context.hide()
            return
        state = status.state
        reason = status.reason
        self._report["Cursor state"] = state if not reason else f"{state} ({reason})"
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
        self._report["Event / line"] = (
            f"Chapter {chapter}; {event_id or '-'} / {line_id or '-'}; "
            f"{status.next_event_count} next candidate(s)"
        )
        speaker = status.speaker
        text = status.text
        self._report["Canonical dialogue"] = (
            "-" if not text else f"{speaker or 'Narrator'}: {text}"
        )
        self._report["Expected audio"] = status.expected_audio_route
        self._report["Actual audio"] = status.actual_audio_route
        self._report["OCR activity"] = status.ocr_activity
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
        self.sequence_notice.setVisible(
            recovery or bool(state == "unavailable" and reason and guidance)
        )
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
                "Choose next line..."
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

    def _update_status_visibility(self, _index: int = -1) -> None:
        visible = bool(self.status.text()) and not (
            self.sections.currentIndex() == 2 and self._status_reading_detail
        )
        self.status.setVisible(visible)
        self.copy_status_button.setVisible(visible)

    def set_status(self, message: str, *, details: str | None = None) -> None:
        self.status.setToolTip(message if details is None else details)
        presentation = playback_status_label(message)
        self._status_reading_detail = message.startswith("OCR uncertain:") or (
            message.startswith("Audio source for ") and presentation != message
        )
        summary = " ".join(presentation.split())
        self.status.setText(
            summary
            if len(summary) <= 240
            else summary[:240] + "… Copy status for the full message."
        )
        self._update_status_visibility()

    def set_dialogue(self, speaker: str | None, text: str | None) -> None:
        text = text or ""
        if (speaker, text) != (self._dialogue_speaker, self._dialogue_text):
            self._clear_line_audio()
        self._dialogue_speaker = speaker
        self._dialogue_text = text
        self.speaker.setText(speaker or "Narrator")
        self.dialogue_card.setVisible(bool(text))
        self._update_dialogue_title()

    def _clear_line_audio(self) -> None:
        self.dialogue_info_button.setChecked(False)
        self.dialogue_info_button.hide()
        self.current_audio.hide()
        self.voice.clear()
        self.audio_source.clear()
        self._report.pop("Playback", None)

    def _set_line_audio(self, state: RuntimeControlState) -> None:
        if not (state.speaking_text or state.speaking_character or state.audio_source):
            return

        def normalize(text: str) -> str:
            return " ".join(text.replace("’", "'").replace("‘", "'").split()).casefold()

        line = normalize(self._dialogue_text)
        spoken = normalize(state.speaking_text)
        speaker = normalize(self._dialogue_speaker or "Narrator")
        if (
            not line
            or not spoken
            or f" {spoken} " not in f" {line} "
            or speaker != normalize(state.speaking_character or "Narrator")
            or not state.audio_source
            or state.audio_source == "Not selected"
        ):
            self._clear_line_audio()
            return
        voice, audio = playback_labels(
            state.audio_source, state.playback_voice or "Not resolved yet"
        )
        self.voice.setText(voice)
        self.audio_source.setText(audio)
        self._report["Playback"] = (
            f"Reported voice: {state.playback_voice}\n"
            if state.playback_voice and state.playback_voice != voice
            else ""
        ) + state.audio_source
        self.dialogue_info_button.show()
        self._set_current_audio_expanded(self.dialogue_info_button.isChecked())

    def _set_current_audio_expanded(self, expanded: bool) -> None:
        self.current_audio.setVisible(expanded and bool(self._report.get("Playback")))
        name = "Hide" if expanded else "Show"
        self.dialogue_info_button.setAccessibleName(
            f"{name} voice and audio for this line"
        )

    def set_recognition(self, text: str | None, *, uncertain: bool = False) -> None:
        self._recognition_text = text
        self._recognition_uncertain = uncertain and text is not None
        if self.recognition.toPlainText() != (text or ""):
            self.recognition.setPlainText(text or "")
        problem = (
            text is not None
            and (uncertain or not text.strip())
            and self._recognition_allowed
            and self.loading_panel.isHidden()
        )
        if not problem:
            self._reset_recognition_warning()
        else:
            self._recognition_attempts += 1
            if self._recognition_attempts == 1:
                self._recognition_timer.start()
        description = "Recognition is uncertain." if self._recognition_uncertain else ""
        if text == "":
            description = (
                description + " " + self.recognition.placeholderText()
            ).strip()
        self.recognition.setAccessibleDescription(description)
        self.recognition_note.setText(
            "No dialogue text found. Check the dialogue area if dialogue is visible in the game."
            if text is not None and not text.strip()
            else "The dialogue is unclear. Check that the dialogue area covers the text."
        )
        self._update_reading_issue()

    def _reset_recognition_warning(self) -> None:
        self._recognition_timer.stop()
        self._recognition_attempts = 0
        self._recognition_persistent = False

    def _recognition_timeout(self) -> None:
        if self._recognition_attempts:
            self._recognition_persistent = True
        self._update_reading_issue()

    def _update_reading_issue(self) -> None:
        self.recognition_area.setVisible(
            self._recognition_allowed
            and self.loading_panel.isHidden()
            and self._recognition_persistent
            and self._recognition_attempts > 1
            and not self._sequence_issue
        )

    def _update_dialogue_title(self) -> None:
        text = self._dialogue_text or (
            "Waiting for dialogue in the game."
            if self._live
            else "Start reading to follow dialogue in the game."
            if self._ready
            else "The detected dialogue will appear here."
        )
        if self.dialogue.toPlainText() != text:
            self.dialogue.setPlainText(text)
        self.dialogue_card.setTitle(
            "Current dialogue"
            if self._dialogue_current or not self._dialogue_text
            else "Last detected dialogue"
        )

    def set_loading(self, loading: bool) -> None:
        loading = bool(loading)
        self.loading_panel.setVisible(loading)
        if loading:
            self._reset_recognition_warning()
        self._update_reading_issue()
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
                self._window_hint
                or ("Ready to start" if self._can_initialize else "Not ready")
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
        self._set_line_audio(state)
        self._recognition_allowed = (
            state.ready
            and state.live
            and not (
                state.paused
                or state.speaking
                or state.queued
                or state.starting
                or state.transition
                or state.waiting_for_game
                or state.window_hint
                or not state.output_available
            )
        )
        if not self._recognition_allowed:
            self._reset_recognition_warning()
        self._update_reading_issue()
        self._ready = state.ready
        self._can_initialize = state.can_initialize
        self._unavailable_reason = state.unavailable_reason
        self._window_hint = state.window_hint
        self._needs_readiness_check = state.needs_readiness_check
        self._dialogue_current = state.speaking or state.paused
        self._update_dialogue_title()
        loading = not self.loading_panel.isHidden()
        self.reading_state.setText(
            "Loading speech engine"
            if loading
            else "Stopping reading"
            if state.transition == "stopping"
            else "Output available. Resume to hear this line from the start."
            if state.output_interrupted and state.output_available
            else "Audio output disconnected. Reading paused."
            if not state.output_available and (state.output_interrupted or state.paused)
            else "Waiting for audio output."
            if not state.output_available and not state.active
            else state.window_hint
            if state.window_hint
            else "Speech paused"
            if state.paused
            else "Return to the game to continue reading"
            if state.waiting_for_game
            else "Ready to start"
            if state.can_initialize
            else "Not ready"
            if not state.ready
            else "Finding current dialogue"
            if state.starting
            else f"Speaking: {state.speaking_character}"
            if state.speaking and state.speaking_character
            else "Speaking current dialogue"
            if state.speaking
            else "Speech queued"
            if state.queued
            else "Waiting for dialogue"
            if state.live
            else "Reading stopped"
        )
        needs_setup = not loading and not (
            state.ready or state.can_initialize or state.active or state.transition
        )
        self.prepare_reading_button.setVisible(needs_setup)
        self.prepare_reading_button.setText(self._reading_setup_action())
        self.story_actions.setVisible(state.ready and self._sequence_controls_available)
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
            icon_only=True,
        )
        if loading and not state.active and not state.transition:
            self.live_button.setAccessibleName("Loading voice")
            self.live_button.setEnabled(False)
        self.prepare_reading_button.setDefault(
            not state.ready and not state.active and not state.can_initialize
        )
        self.live_button.setDefault(state.ready or state.active or state.can_initialize)

    def _reading_setup_action(self) -> str:
        if self._needs_readiness_check:
            return "Check readiness"
        return "Set up and start"

    def _request_reading_setup(self) -> None:
        if self._needs_readiness_check:
            self.readiness_requested.emit()
        else:
            self.reading_setup_requested.emit()

    def _reading_setup_reason(self) -> str:
        return self._unavailable_reason or (
            _INITIALIZE_READING_REASON
            if self._can_initialize
            else "Set up and start reading for the selected game."
        )

    def _set_action_reason(self, message: str) -> None:
        self.action_reason.setText(message)
        self.action_reason.setVisible(
            not self._ready
            and not self._window_hint
            and not self._can_initialize
            and self.loading_panel.isHidden()
        )
        if self._ready:
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
        if running and not self._live:
            self._clear_line_audio()
        if self._live and not running:
            self.set_recognition(None)
            self._recognition_allowed = False
        self._live = bool(running)
        if running:
            self.show_reading()
        self._update_dialogue_title()
        _set_live_button_presentation(self.live_button, running, icon_only=True)
        if self._ready:
            self._set_action_reason(
                "Reading is active; Stop reading ends speech immediately."
                if running
                else "Ready: start reading in the game, or read the current dialogue once."
            )

    def set_diagnostic(self, snapshot: DiagnosticSnapshot) -> None:
        self._report["Latest diagnostic audio"] = snapshot.audio_source
        self._report["Latest diagnostic voice"] = snapshot.voice
        self._report["OCR confidence"] = f"{snapshot.confidence:.1f}%"
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
        self._report["Latest latency"] = ", ".join(parts) or "-"

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
        self._state = RuntimeControlState()
        self._warning = ""
        self._sequence_notice = ""
        self._sequence_expected_candidate_count = 0
        self._last_speaker = ""
        self._last_text = ""
        self.setWindowTitle("VNTTS Reading")
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        if platform == "darwin":
            # Keep the panel visible when the game takes focus.
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMinimumWidth(420)

    def _build_status_labels(self) -> None:
        self.message_focus_frame = QFocusFrame(self)
        self.mode = QLabel()
        mode_font = QFont()
        mode_font.setBold(True)
        self.mode.setFont(mode_font)
        self.mode.setAccessibleName("Reading state")
        self.mode.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        self.last_line = QLabel("Last line")
        self.last_line.hide()
        self.speaker = self._message_field("Dialogue speaker", lines=1)
        self.dialogue = self._message_field("Spoken dialogue", lines=3)
        self.speaker.hide()
        self.dialogue.hide()
        self.action_reason = self._message_field("Reading notice", lines=2)
        self.action_reason.hide()

    def _message_field(self, name: str, *, lines: int) -> QPlainTextEdit:
        field = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        field.setAccessibleName(name)
        field.setProperty("visible_lines", lines)
        field.setFrameShape(QFrame.Shape.NoFrame)
        field.viewport().setAutoFillBackground(False)
        field.setFixedHeight(field.fontMetrics().lineSpacing() * lines + 12)
        field.installEventFilter(self)
        return field

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QPlainTextEdit):
            if event.type() == QEvent.Type.FontChange:
                watched.setFixedHeight(
                    watched.fontMetrics().lineSpacing()
                    * watched.property("visible_lines")
                    + 12
                )
                QTimer.singleShot(0, self, self._fit_content)
            elif event.type() == QEvent.Type.FocusIn:
                self.message_focus_frame.setWidget(watched)
                self.message_focus_frame.show()
            elif event.type() == QEvent.Type.FocusOut:
                self.message_focus_frame.hide()
        return super().eventFilter(watched, event)

    def _build_buttons(self) -> None:
        self.live_button = QPushButton("Start")
        set_action_icon(self.live_button, "play")
        self.pause_button = QPushButton()
        set_action_icon(self.pause_button, "pause")
        self.read_button = QPushButton("Read once")
        set_action_icon(self.read_button, "volume-2")
        self.sequence_expected_button = QPushButton("Use next line")
        set_action_icon(self.sequence_expected_button, "skip-forward")
        self.sequence_expected_button.hide()
        self.full_button = QPushButton()
        set_action_icon(self.full_button, "expand")
        self.full_button.setAccessibleName("Open Reading")
        self.full_button.setToolTip("Open Reading")
        self.full_button.setAccessibleDescription(
            "Open Reading to choose the game window, change settings or recover reading."
        )
        self.live_button.setDefault(True)
        for button in (
            self.read_button,
            self.live_button,
            self.pause_button,
            self.sequence_expected_button,
            self.full_button,
        ):
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
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
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.mode, 1)
        header.addWidget(self.live_button)
        header.addWidget(self.pause_button)
        header.addWidget(self.full_button)
        layout.addLayout(header)
        layout.addWidget(self.last_line)
        layout.addWidget(self.speaker)
        layout.addWidget(self.dialogue)
        layout.addWidget(self.action_reason)
        secondary = QHBoxLayout()
        secondary.addWidget(self.read_button)
        secondary.addWidget(self.sequence_expected_button)
        secondary.addStretch()
        layout.addLayout(secondary)
        self.setTabOrder(self.live_button, self.pause_button)
        self.setTabOrder(self.pause_button, self.full_button)
        self.setTabOrder(self.full_button, self.speaker)
        self.setTabOrder(self.speaker, self.dialogue)
        self.setTabOrder(self.dialogue, self.action_reason)
        self.setTabOrder(self.action_reason, self.read_button)
        self.setTabOrder(self.read_button, self.sequence_expected_button)

    def set_sequence_status(self, status: LiveSequenceStatus) -> None:
        manual = (
            is_live_sequence_audio_mode(status.mode) and status.state != "unavailable"
        )
        count = status.expected_candidate_count if manual else 0
        self._sequence_expected_candidate_count = count
        self.sequence_expected_button.setVisible(count > 0)
        self.sequence_expected_button.setText(
            "Use next line" if count == 1 else "Choose line..."
        )
        self.sequence_expected_button.setAccessibleDescription(
            "Use the expected next indexed line."
            if count == 1
            else "Choose the indexed line currently on screen."
        )
        self._sequence_notice = (
            "Choose the line on screen."
            if manual and status.recovery_required and count
            else "Open Reading to find the current line."
            if manual and status.recovery_required
            else status.guidance
            if status.reason and status.guidance
            else ""
        )
        self._update_notice()
        self._update_sequence_action()
        self._fit_content()

    def _update_sequence_action(self) -> None:
        self.sequence_expected_button.setEnabled(
            self._state.ready
            and self._state.transition is None
            and not self._state.starting
            and self._sequence_expected_candidate_count > 0
        )
        self.sequence_expected_button.setToolTip(
            self.sequence_expected_button.accessibleDescription()
            if self.sequence_expected_button.isEnabled()
            else self._state.reason_for("read")
        )

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
        # Cocoa already floats the panel above the game. Raising it takes keyboard focus.
        if QApplication.platformName() != "cocoa":
            self.raise_()

    def set_warning(self, message: str) -> None:
        self._warning = message
        self._update_notice()
        self._fit_content()

    def clear_warning(self, expected: str) -> None:
        if self._warning == expected:
            self.set_warning("")

    def reset_session(self) -> None:
        self._last_speaker = ""
        self._last_text = ""
        self._warning = ""
        self._sequence_notice = ""
        self._sequence_expected_candidate_count = 0
        self.sequence_expected_button.hide()
        self.speaker.clear()
        self.dialogue.clear()
        self.speaker.hide()
        self.dialogue.hide()
        self.last_line.hide()
        self._update_notice()
        self._fit_content()

    def set_ready(self, ready: bool, *, reason: str | None = None) -> None:
        self.set_runtime_controls(
            RuntimeControlState(ready=bool(ready), unavailable_reason=reason)
        )

    def _reading_status(self) -> tuple[str, str]:
        state = self._state
        if state.transition == "stopping":
            return "Stopping", ""
        if state.loading_voice:
            return "Loading voice", ""
        if not state.output_available:
            return "No audio output", "Connect an audio output device to continue."
        if state.output_interrupted:
            return "Paused", "Audio output is ready. Resume restarts this line."
        if state.window_hint:
            return "Game window unavailable", state.window_hint
        if state.paused:
            return "Paused", ""
        if state.waiting_for_game:
            return "Return to the game", ""
        if state.finding_current_line:
            return "Finding current line", "Keep the dialogue visible in the game."
        if state.starting or state.transition == "starting":
            return "Starting", ""
        if state.speaking:
            return "Speaking", ""
        if state.queued:
            return "Preparing line", ""
        if state.live:
            return "Waiting for next line", ""
        if state.ready or state.can_initialize:
            return "Ready", ""
        return "Not ready", state.unavailable_reason or "Open Reading to finish setup."

    def _update_notice(self) -> None:
        mode, reason = self._reading_status()
        self.mode.setText(mode)
        text = (
            reason
            or self._warning
            or self._state.reading_notice
            or self._sequence_notice
        )
        if self.action_reason.toPlainText() != text:
            self.action_reason.setPlainText(text)
        self.action_reason.setToolTip(text)
        font = QFont(self.font())
        font.setBold(bool(text and text == self._warning))
        self.action_reason.setFont(font)
        self.action_reason.setVisible(bool(text))

    def set_runtime_controls(self, state: RuntimeControlState) -> None:
        self._state = state
        _apply_reading_capabilities(
            state,
            read_button=self.read_button,
            live_button=self.live_button,
            pause_button=self.pause_button,
            icon_only=True,
        )
        self.live_button.setDefault(False)
        self.live_button.setAutoDefault(False)
        self.read_button.setVisible(state.can_read)
        self._update_sequence_action()
        if state.speaking_text:
            self._last_speaker = state.speaking_character or "Narrator"
            self._last_text = state.speaking_text
        has_line = bool(self._last_text)
        current = state.speaking or state.output_interrupted
        self.speaker.setVisible(has_line)
        self.dialogue.setVisible(has_line)
        self.last_line.setVisible(has_line and not current)
        if self.speaker.toPlainText() != self._last_speaker:
            self.speaker.setPlainText(self._last_speaker)
        if self.dialogue.toPlainText() != self._last_text:
            self.dialogue.setPlainText(self._last_text)
        for field in (self.speaker, self.dialogue):
            palette = QPalette(field.palette())
            palette.setColor(
                QPalette.ColorRole.Text,
                self.palette().color(
                    QPalette.ColorGroup.Active
                    if current
                    else QPalette.ColorGroup.Disabled,
                    QPalette.ColorRole.WindowText,
                ),
            )
            field.setPalette(palette)
        self._update_notice()
        self._fit_content()

    def _fit_content(self) -> None:
        side = max(self.fontMetrics().lineSpacing() + 16, 32)
        for button in (self.live_button, self.pause_button, self.full_button):
            button.setFixedSize(side, side)
        speaker_font = QFont(self.font())
        speaker_font.setBold(True)
        self.speaker.setFont(speaker_font)
        dialogue_font = QFont(self.font())
        if dialogue_font.pointSizeF() > 0:
            dialogue_font.setPointSizeF(dialogue_font.pointSizeF() * 1.08)
        self.dialogue.setFont(dialogue_font)
        for field in (self.speaker, self.dialogue, self.action_reason):
            lines = min(
                max(1, ceil(field.document().size().height())),
                field.property("visible_lines"),
            )
            field.setFixedHeight(field.fontMetrics().lineSpacing() * lines + 12)
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

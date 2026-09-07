import ctypes
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from vntts.settings import is_live_sequence_audio_mode
from vntts.speech_presentation import reading_policy_label, speech_configuration_label


@dataclass(frozen=True)
class RuntimeControlState:
    ready: bool = False
    live: bool = False
    paused: bool = False
    speaking: bool = False
    queued: bool = False
    replayable: bool = False
    unavailable_reason: str | None = None

    @property
    def can_read(self):
        return self.ready

    @property
    def can_toggle_live(self):
        return self.ready

    @property
    def can_pause(self):
        return self.ready and (self.live or self.paused or self.speaking or self.queued)

    @property
    def can_skip(self):
        return self.ready and self.speaking

    @property
    def can_clear_queue(self):
        return self.ready and (self.speaking or self.queued)

    @property
    def can_replay(self):
        return self.ready and self.replayable

    @property
    def can_emergency_stop(self):
        return self.ready and (self.live or self.paused or self.speaking or self.queued)

    def reason_for(self, control):
        if not self.ready:
            return (
                self.unavailable_reason or "VNTTS is not ready. Select Check readiness."
            )
        reasons = {
            "pause": "Pause is available during live reading or active playback.",
            "skip": "Skip is available while dialogue is speaking.",
            "queue": "The speech queue is empty.",
            "replay": "Replay becomes available after dialogue has been spoken.",
            "emergency": "Nothing is currently running or speaking.",
        }
        return reasons.get(control, "Control is unavailable in the current state.")


class ControlDashboard(QMainWindow):
    reading_setup_requested = Signal()
    read_requested = Signal()
    live_requested = Signal()
    sequence_resync_requested = Signal()
    sequence_expected_requested = Signal()
    pause_requested = Signal()
    skip_requested = Signal()
    repeat_requested = Signal()
    stop_requested = Signal()
    pregeneration_requested = Signal()
    readiness_requested = Signal()
    calibration_requested = Signal()
    voices_requested = Signal()
    diagnostics_requested = Signal()
    settings_requested = Signal()
    compact_requested = Signal()
    quit_requested = Signal()
    hidden_to_background = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.keep_running_on_close = settings.keep_running_on_close
        self._quitting = False
        self._live = False
        self._ready = False
        self._sequence_expected_candidate_count = 0
        self.setWindowTitle("Visual Novel Text to Speech")
        self.setMinimumWidth(620)
        self.setMinimumHeight(340)
        self.resize(860, 660)

        self.status = QLabel("Starting...")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight: 600; font-size: 15px;")
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
        self.mode = QLabel("Stopped")
        self.speaker = QLabel("Narrator")
        self.speaker.setAccessibleName("Current dialogue speaker")
        self.speaker.setWordWrap(True)
        self.voice = QLabel("Not loaded")
        self.audio_source = QLabel("Not selected")
        self.voice.setWordWrap(True)
        self.audio_source.setWordWrap(True)
        self.speech_configuration = QLabel()
        self.speech_configuration.setWordWrap(True)
        self.speech_configuration.setAccessibleName("Narrator and speech engine")
        self.reading_policy = QLabel()
        self.reading_policy.setWordWrap(True)
        self.reading_policy.setAccessibleName("In-game audio policy")
        self.reading_help = QLabel(
            "Start reading follows dialogue in the game. Saved recordings play "
            "without generation; the configured engine/model creates new speech."
        )
        self.reading_help.setWordWrap(True)
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
        self.dialogue = QLabel("No dialogue detected")
        self.dialogue.setWordWrap(True)
        self.dialogue.setMinimumHeight(52)
        self.dialogue.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        details = QFormLayout()
        details.addRow("Mode", self.mode)
        details.addRow("OCR confidence", self.confidence)
        details.addRow("Latest latency", self.latency)
        details.addRow("Configuration", self.configuration)
        self.details_content = QWidget()
        self.details_layout = QVBoxLayout(self.details_content)
        self.details_layout.setContentsMargins(0, 0, 0, 0)
        self.details_layout.addLayout(details)
        self.details_layout.addWidget(self.reading_help)
        self.details_toggle = QPushButton("Show technical details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setAccessibleDescription(
            "Show or hide voice, audio source, OCR, latency and configuration details"
        )
        self.details_toggle.toggled.connect(self._set_details_expanded)

        self.read_button = QPushButton("Read current dialogue")
        self.live_button = QPushButton("Start reading")
        self.sequence_resync_button = QPushButton("Set story position / resync")
        self.sequence_expected_button = QPushButton("Use expected next line")
        self.pause_button = QPushButton("Pause")
        self.skip_button = QPushButton("Skip")
        self.repeat_button = QPushButton("Replay")
        self.stop_button = QPushButton("Emergency stop")
        self.stop_button.setStyleSheet(
            "QPushButton { color: #a21818; font-weight: 600; }"
        )
        self.live_button.setDefault(True)
        self.live_button.setStyleSheet("font-weight: 700;")
        self.live_button.setAccessibleDescription(
            "Primary action: start or stop continuous live reading"
        )
        self.read_button.setAccessibleDescription(
            "Read the currently visible dialogue once"
        )
        self.stop_button.setAccessibleDescription(
            "Immediately stop live capture and queued speech"
        )
        self.read_button.clicked.connect(self.read_requested.emit)
        self.live_button.clicked.connect(self.live_requested.emit)
        self.sequence_resync_button.clicked.connect(self.sequence_resync_requested.emit)
        self.sequence_expected_button.clicked.connect(
            self.sequence_expected_requested.emit
        )
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.skip_button.clicked.connect(self.skip_requested.emit)
        self.repeat_button.clicked.connect(self.repeat_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)

        reading_actions = QHBoxLayout()
        reading_actions.addWidget(self.live_button, 2)
        reading_actions.addWidget(self.read_button)
        self.prepare_reading_button = QPushButton("Set up reading")
        self.prepare_reading_button.clicked.connect(self.reading_setup_requested.emit)
        self.prepare_audio_button = QPushButton("Prepare offline audio...")
        self.prepare_audio_button.setAccessibleDescription(
            "Choose stories and prepare their voices locally with guided defaults"
        )
        self.prepare_audio_button.clicked.connect(self.pregeneration_requested.emit)
        self.loading_blocked_buttons = [self.prepare_audio_button]

        self.sequence_state = QLabel("Unavailable")
        self.sequence_position = QLabel("-")
        self.sequence_identity = QLabel("-")
        self.sequence_canonical = QLabel("-")
        self.sequence_canonical.setWordWrap(True)
        self.sequence_expected_audio = QLabel("-")
        self.sequence_actual_audio = QLabel("-")
        self.sequence_ocr = QLabel("-")
        self.sequence_ocr.setWordWrap(True)
        self.sequence_guidance = QLabel()
        self.sequence_guidance.setWordWrap(True)
        sequence_form = QFormLayout()
        sequence_form.addRow("Cursor state", self.sequence_state)
        sequence_form.addRow("Story position", self.sequence_position)
        sequence_form.addRow("Event / line", self.sequence_identity)
        sequence_form.addRow("Canonical dialogue", self.sequence_canonical)
        sequence_form.addRow("Expected audio", self.sequence_expected_audio)
        sequence_form.addRow("Actual audio", self.sequence_actual_audio)
        sequence_form.addRow("OCR activity", self.sequence_ocr)
        self.sequence_group = QGroupBox("Sequence-first story cursor")
        sequence_layout = QVBoxLayout(self.sequence_group)
        sequence_layout.addLayout(sequence_form)
        sequence_layout.addWidget(self.sequence_guidance)
        sequence_layout.addWidget(self.sequence_expected_button)
        sequence_layout.addWidget(self.sequence_resync_button)
        self.details_layout.addWidget(self.sequence_group)

        transport_group = QGroupBox("Playback")
        transport = QHBoxLayout(transport_group)
        transport.addWidget(self.pause_button)
        transport.addWidget(self.skip_button)
        transport.addWidget(self.repeat_button)
        transport.addStretch()
        transport.addWidget(self.stop_button)

        setup_group = QWidget()
        setup = QVBoxLayout(setup_group)
        setup.setContentsMargins(0, 0, 0, 0)
        setup_primary = QHBoxLayout()
        self.setup_primary_button = QPushButton("Check readiness")
        self.setup_primary_button.setAccessibleDescription(
            "Check readiness and open the direct fix for any setup problem"
        )
        self.setup_primary_button.clicked.connect(self.readiness_requested.emit)
        setup_primary.addWidget(self.setup_primary_button, 1)
        self.loading_blocked_buttons.append(self.setup_primary_button)
        self.setup_more_button = QPushButton("More setup options")
        self.setup_more_button.setCheckable(True)
        self.setup_more_button.setAccessibleDescription(
            "Show or hide calibration, voice, support and settings shortcuts"
        )
        setup_primary.addWidget(self.setup_more_button)
        setup.addLayout(setup_primary)
        self.setup_secondary_content = QWidget()
        setup_secondary = QHBoxLayout(self.setup_secondary_content)
        setup_secondary.setContentsMargins(0, 0, 0, 0)
        self.setup_buttons = [self.setup_primary_button, self.setup_more_button]
        for label, signal in (
            ("Calibrate capture", self.calibration_requested),
            ("Narrator voice", self.voices_requested),
            ("Support and logs", self.diagnostics_requested),
            ("Settings", self.settings_requested),
        ):
            button = QPushButton(label)
            button.clicked.connect(signal.emit)
            if label != "Narrator voice":
                setup_secondary.addWidget(button)
            self.setup_buttons.append(button)
            if label != "Support and logs":
                self.loading_blocked_buttons.append(button)
            if label == "Narrator voice":
                self.narrator_voice_button = button
        self.quit_button = QPushButton("Quit VNTTS")
        self.quit_button.clicked.connect(self.request_quit)
        setup_secondary.addWidget(self.quit_button)
        self.setup_buttons.append(self.quit_button)
        setup.addWidget(self.setup_secondary_content)
        self.setup_more_button.toggled.connect(self._set_setup_expanded)

        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card_layout = QVBoxLayout(card)
        card_layout.addWidget(QLabel("Current dialogue"))
        card_layout.addWidget(self.speaker)
        card_layout.addWidget(self.dialogue)
        current_audio = QFormLayout()
        current_audio.addRow("Voice", self.voice)
        current_audio.addRow("Audio", self.audio_source)
        card_layout.addLayout(current_audio)

        reading_content = QWidget()
        layout = QVBoxLayout(reading_content)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.compact_button = QPushButton("Compact controls")
        self.compact_button.clicked.connect(self.compact_requested.emit)
        self.reading_defaults = QLabel()
        self.reading_defaults.setWordWrap(True)
        self.reading_defaults.setAccessibleName("Live fallback voice and engine")
        layout.addWidget(self.reading_defaults)
        layout.addWidget(self.reading_policy)
        layout.addWidget(card)
        layout.addWidget(self.details_toggle)
        layout.addWidget(self.details_content)
        layout.addWidget(self.compact_button, 0, Qt.AlignmentFlag.AlignRight)

        self.content_scroll = QScrollArea()
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.content_scroll.setWidget(reading_content)
        reading_page = QWidget()
        reading_layout = QVBoxLayout(reading_page)
        reading_layout.addWidget(self.content_scroll, 1)
        reading_layout.addWidget(self.action_reason)
        reading_layout.addWidget(self.prepare_reading_button)
        reading_layout.addLayout(reading_actions)
        reading_layout.addWidget(transport_group)

        stories_page = QWidget()
        stories_layout = QVBoxLayout(stories_page)
        stories_title = QLabel("Prepare stories for reading")
        stories_title.setStyleSheet("font-size: 20px; font-weight: 600;")
        stories_layout.addWidget(stories_title)
        self.stories_guidance = QLabel(
            "Choose installed game content and the stories you want to hear. "
            "The preparation window shows each story's saved audio and remaining work."
        )
        self.stories_guidance.setWordWrap(True)
        stories_layout.addWidget(self.stories_guidance)
        self.prepare_audio_button.setText("Choose stories and prepare audio...")
        self.prepare_audio_button.setDefault(True)
        stories_layout.addWidget(self.prepare_audio_button)
        self.stories_next = QLabel(
            "After preparation, use the saved audio, then open Reading. "
            "You can also read without preparation using live speech."
        )
        self.stories_next.setWordWrap(True)
        stories_layout.addWidget(self.stories_next)
        stories_layout.addStretch()
        self.open_reading_button = QPushButton("Open reading controls")
        stories_layout.addWidget(self.open_reading_button)

        voices_page = QWidget()
        voices_layout = QVBoxLayout(voices_page)
        voices_title = QLabel("Narrator and character voices")
        voices_title.setStyleSheet("font-size: 20px; font-weight: 600;")
        voices_layout.addWidget(voices_title)
        voices_layout.addWidget(self.speech_configuration)
        voice_help = QLabel(
            "Choose and preview your narrator below. Character voices and any "
            "narrator substitutions are shown before story generation. "
            "Changing a default voice does not change existing recordings."
        )
        voice_help.setWordWrap(True)
        voices_layout.addWidget(voice_help)
        self.narrator_voice_button.setText("Choose and preview narrator...")
        self.narrator_voice_button.setDefault(True)
        voices_layout.addWidget(self.narrator_voice_button)
        self.voice_availability = QLabel(
            "The preview loads its model when needed. Reading setup is not required."
        )
        self.voice_availability.setWordWrap(True)
        voices_layout.addWidget(self.voice_availability)
        voices_layout.addStretch()

        self.sections = QTabWidget()
        self.sections.setAccessibleName("Main application sections")
        self.stories_stack = QStackedWidget()
        self.stories_stack.addWidget(stories_page)
        self.sections.addTab(self.stories_stack, "Stories")
        self.sections.addTab(voices_page, "Voices")
        self.sections.addTab(reading_page, "Reading")
        self.open_reading_button.clicked.connect(self.show_reading)
        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(12, 12, 12, 12)
        shell_layout.addWidget(self.status)
        shell_layout.addWidget(self.loading_panel)
        self.preparation_status = QPushButton()
        self.preparation_status.setAccessibleName("Ongoing story preparation")
        self.preparation_status.clicked.connect(self.show_stories)
        self.preparation_status.hide()
        shell_layout.addWidget(self.preparation_status)
        shell_layout.addWidget(self.sections, 1)
        shell_layout.addWidget(setup_group)
        self.setCentralWidget(shell)
        self._set_details_expanded(False)
        self._set_setup_expanded(False)
        self.set_loading(False)
        self.set_ready(False)
        self.set_configuration(settings)

    def show_reading(self):
        self.sections.setCurrentIndex(2)

    def show_stories(self):
        self.sections.setCurrentIndex(0)

    def embed_preparation(self, panel):
        panel.setWindowFlags(Qt.WindowType.Widget)
        panel.setMinimumSize(0, 0)
        self.stories_stack.addWidget(panel)
        self.stories_stack.setCurrentWidget(panel)
        self.show_stories()
        panel.show()

    def remove_preparation(self, panel):
        self.stories_stack.removeWidget(panel)
        self.stories_stack.setCurrentIndex(0)
        self.preparation_status.hide()

    def set_preparation_phase(self, phase):
        self.preparation_status.setText(f"Story preparation: {phase} — Show")
        self.preparation_status.show()

    def _set_details_expanded(self, expanded):
        expanded = bool(expanded)
        self.details_toggle.blockSignals(True)
        self.details_toggle.setChecked(expanded)
        self.details_toggle.setText(
            "Hide technical details" if expanded else "Show technical details"
        )
        self.details_toggle.blockSignals(False)
        self.details_content.setVisible(expanded)

    def _set_setup_expanded(self, expanded):
        expanded = bool(expanded)
        self.setup_more_button.blockSignals(True)
        self.setup_more_button.setChecked(expanded)
        self.setup_more_button.setText(
            "Fewer setup options" if expanded else "More setup options"
        )
        self.setup_more_button.blockSignals(False)
        self.setup_secondary_content.setVisible(expanded)

    def set_configuration(self, settings):
        self.prepare_reading_button.setText(
            "Load reading engine" if settings.onboarding_completed else "Set up reading"
        )
        self.keep_running_on_close = settings.keep_running_on_close
        self.set_speech_identity(settings)
        self.reading_policy.setText(reading_policy_label(settings))
        self._set_capture_configuration(settings)

    def set_speech_identity(self, settings, narrator=None):
        summary = speech_configuration_label(settings, narrator=narrator)
        self.speech_configuration.setText(summary)
        self.reading_defaults.setText(f"Live speech defaults\n{summary}")
        self.speech_configuration.setToolTip(self.reading_help.text())

    def _set_capture_configuration(self, settings):
        capture = (
            settings.game_window_title or "No game window selected"
            if settings.capture_mode == "window"
            else "Calibrated screen region"
        )
        policy = {
            "live-tts-only": "Live TTS only",
            "prefer-generated": "Generated audio, then live TTS",
            "prefer-game-audio": "Original game audio, then generated/live TTS",
        }.get(settings.audio_source_policy, settings.audio_source_policy)
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
        self.sequence_group.setVisible(sequence_audio)
        self.sequence_resync_button.setAccessibleDescription(
            "Choose the visible story event to anchor or recover sequence-first reading"
        )
        self.configuration.setText(
            f"Backend: {settings.speech_backend}\n"
            f"Audio policy: {policy}\n"
            f"Generated audio: {generated_audio}\n"
            f"Sequence: {sequence}\n"
            f"Capture: {capture}\n"
            f"OCR: {settings.ocr_language}"
        )

    def set_sequence_status(self, status):
        sequence_audio = is_live_sequence_audio_mode(getattr(status, "mode", "off"))
        self.sequence_group.setVisible(sequence_audio)
        if not sequence_audio:
            return
        state = getattr(status, "state", "unavailable")
        reason = getattr(status, "reason", None)
        self.sequence_state.setText(state if not reason else f"{state} ({reason})")
        chapter = getattr(status, "chapter", None)
        sequence = getattr(status, "sequence", None)
        self.sequence_position.setText(
            "-" if chapter is None else f"Chapter {chapter}, sequence {sequence}"
        )
        event_id = getattr(status, "event_id", None)
        line_id = getattr(status, "line_id", None)
        self.sequence_identity.setText(
            f"{event_id or '-'} / {line_id or '-'}; "
            f"{getattr(status, 'next_event_count', 0)} next candidate(s)"
        )
        speaker = getattr(status, "speaker", None)
        text = getattr(status, "text", None)
        self.sequence_canonical.setText(
            "-" if not text else f"{speaker or 'Narrator'}: {text}"
        )
        self.sequence_expected_audio.setText(
            getattr(status, "expected_audio_route", "-")
        )
        self.sequence_actual_audio.setText(getattr(status, "actual_audio_route", "-"))
        self.sequence_ocr.setText(getattr(status, "ocr_activity", "-"))
        guidance = getattr(status, "guidance", "")
        self.sequence_guidance.setText(guidance)
        recovery = bool(getattr(status, "recovery_required", False))
        candidate_count = int(getattr(status, "expected_candidate_count", 0))
        self._sequence_expected_candidate_count = candidate_count
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

    def set_status(self, message):
        self.status.setText(message)
        if not self._ready:
            self._set_action_reason(
                f"Reading controls are unavailable: {message}. "
                "Select Check readiness to recover."
            )

    def set_dialogue(self, speaker, text):
        self.speaker.setText(speaker or "Narrator")
        self.dialogue.setText(text or "No dialogue detected")

    def set_loading(self, loading):
        loading = bool(loading)
        self.loading_panel.setVisible(loading)
        self.prepare_reading_button.setEnabled(not loading)
        for button in self.loading_blocked_buttons:
            button.setEnabled(not loading)

    def set_ready(self, ready, *, reason=None):
        self.set_runtime_controls(
            RuntimeControlState(
                ready=bool(ready),
                unavailable_reason=reason,
            )
        )

    def set_runtime_controls(self, state):
        self._ready = state.ready
        self.prepare_reading_button.setVisible(not state.ready)
        self.read_button.setEnabled(state.can_read)
        self.live_button.setEnabled(state.can_toggle_live)
        self.pause_button.setEnabled(state.can_pause)
        self.skip_button.setEnabled(state.can_skip)
        self.repeat_button.setEnabled(state.can_replay)
        self.stop_button.setEnabled(state.can_emergency_stop)
        self.sequence_resync_button.setEnabled(state.ready)
        self.sequence_expected_button.setEnabled(
            state.ready and self._sequence_expected_candidate_count > 0
        )
        disabled_controls = (
            (self.pause_button, state.can_pause, "pause"),
            (self.skip_button, state.can_skip, "skip"),
            (self.repeat_button, state.can_replay, "replay"),
            (self.stop_button, state.can_emergency_stop, "emergency"),
        )
        for button, enabled, control in disabled_controls:
            button.setToolTip("" if enabled else state.reason_for(control))
        if state.ready:
            self._set_action_reason(
                "Ready. Playback controls activate when dialogue is speaking, "
                "queued, paused, or available to replay."
            )
        else:
            self._set_action_reason(
                state.unavailable_reason
                or "Reading controls are unavailable while VNTTS is starting. "
                "Select Check readiness if this does not clear."
            )

    def _set_action_reason(self, message):
        self.action_reason.setText(message)
        self.action_reason.setVisible(not self._ready)
        description = message
        for button in (
            self.read_button,
            self.live_button,
            self.sequence_resync_button,
            self.sequence_expected_button,
        ):
            button.setToolTip(description)

    def set_live(self, running):
        self._live = bool(running)
        if running:
            self.show_reading()
        self.mode.setText("Reading in game" if running else "Stopped")
        self.live_button.setText("Stop reading" if running else "Start reading")
        if self._ready:
            self._set_action_reason(
                "Reading is active; use playback controls or stop reading."
                if running
                else "Ready: start reading in the game, or read the current dialogue once."
            )

    def set_paused(self, paused):
        self.mode.setText(
            "Paused" if paused else ("Reading in game" if self._live else "Stopped")
        )
        self.pause_button.setText("Resume" if paused else "Pause")

    def set_diagnostic(self, snapshot):
        self.speaker.setText(snapshot.character or "Narrator")
        source = snapshot.audio_source or "Not selected"
        self.voice.setText(
            "Voice embedded in the saved recording"
            if "Generated audio" in source
            else "Original game voice (not synthesized by VNTTS)"
            if source.startswith("Original game audio")
            else snapshot.voice or "Not resolved yet"
        )
        self.audio_source.setText(snapshot.audio_source or "Not selected")
        self.confidence.setText(f"{snapshot.confidence:.1f}%")
        parts = []
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

    def request_quit(self):
        self._quitting = True
        self.close()
        self.quit_requested.emit()

    def closeEvent(self, event):
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
    skip_requested = Signal()
    repeat_requested = Signal()
    stop_requested = Signal()
    full_requested = Signal()
    sequence_expected_requested = Signal()

    def __init__(self, parent=None, *, platform=None):
        super().__init__(parent)
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

        self.mode = QLabel("Starting")
        self.mode.setStyleSheet("font-weight: 600;")
        self.mode.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Preferred,
        )
        self.status = QLabel("Initializing...")
        self.status.setWordWrap(True)
        self.status.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.action_reason = QLabel()
        self.action_reason.setWordWrap(True)
        self.action_reason.setAccessibleName("Compact control availability")
        self.action_reason.setAccessibleDescription(
            "Explains why compact reading controls are available or unavailable"
        )
        self.speaker = QLabel("Narrator")
        self.speaker.setMinimumWidth(120)
        self.speaker.setWordWrap(True)
        self.speaker.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.speaker.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.speaker.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.read_button = QPushButton("Read")
        self.live_button = QPushButton("Start reading")
        self.pause_button = QPushButton("Pause")
        self.skip_button = QPushButton("Skip")
        self.repeat_button = QPushButton("Replay")
        self.stop_button = QPushButton("Emergency stop")
        self.sequence_expected_button = QPushButton("Use expected next line")
        self.sequence_expected_button.setVisible(False)
        self.full_button = QPushButton("Full controls")
        self.stop_button.setStyleSheet(
            "QPushButton { color: #a21818; font-weight: 600; }"
        )
        self.live_button.setDefault(True)
        self.live_button.setStyleSheet("font-weight: 700;")
        self.live_button.setAccessibleDescription(
            "Primary action: start or stop continuous live reading"
        )
        self.stop_button.setAccessibleDescription(
            "Immediately stop live capture and queued speech"
        )
        for button in (
            self.read_button,
            self.live_button,
            self.pause_button,
            self.skip_button,
            self.repeat_button,
            self.stop_button,
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
        self.skip_button.clicked.connect(self.skip_requested.emit)
        self.repeat_button.clicked.connect(self.repeat_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.sequence_expected_button.clicked.connect(
            self.sequence_expected_requested.emit
        )
        self.full_button.clicked.connect(self.full_requested.emit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        information = QHBoxLayout()
        information.setSpacing(8)
        information.addWidget(self.mode)
        information.addWidget(self.status, 4)
        information.addWidget(self.speaker, 2)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        for button in (
            self.live_button,
            self.read_button,
            self.pause_button,
            self.skip_button,
            self.repeat_button,
            self.stop_button,
            self.sequence_expected_button,
            self.full_button,
        ):
            controls.addWidget(button)
        controls.addStretch(1)
        layout.addLayout(information)
        layout.addWidget(self.action_reason)
        layout.addLayout(controls)
        self.set_ready(False)

    def set_sequence_status(self, status):
        manual = is_live_sequence_audio_mode(getattr(status, "mode", "off"))
        candidate_count = int(getattr(status, "expected_candidate_count", 0))
        self._sequence_expected_candidate_count = candidate_count
        self.sequence_expected_button.setVisible(manual and candidate_count > 0)
        self.sequence_expected_button.setEnabled(
            self._ready and manual and candidate_count > 0
        )
        self.sequence_expected_button.setText(
            "Use expected next line"
            if candidate_count == 1
            else f"Choose {candidate_count} expected lines..."
        )
        self._fit_content()

    def show_for_game(self, geometry=None):
        self.show()
        QTimer.singleShot(0, lambda: self._finish_show(geometry))

    def _finish_show(self, geometry):
        configure_floating_window(self)
        self.adjustSize()
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

    def set_status(self, message):
        self.status.setText(message)
        self.status.setStyleSheet("")
        self.setToolTip(message)
        if not self._ready:
            self._set_action_reason(
                f"Controls unavailable: {message}. Select Full controls, then "
                "Check readiness."
            )
        self._fit_content()

    def set_warning(self, message):
        self.status.setText(message)
        self.status.setStyleSheet("color: #a21818; font-weight: 600;")
        self.setToolTip(message)
        self._fit_content()

    def set_dialogue(self, speaker, _text):
        self.speaker.setText(speaker or "Narrator")
        self._fit_content()

    def set_ready(self, ready, *, reason=None):
        self.set_runtime_controls(
            RuntimeControlState(
                ready=bool(ready),
                unavailable_reason=reason,
            )
        )

    def set_runtime_controls(self, state):
        self._ready = state.ready
        self.read_button.setEnabled(state.can_read)
        self.live_button.setEnabled(state.can_toggle_live)
        self.pause_button.setEnabled(state.can_pause)
        self.skip_button.setEnabled(state.can_skip)
        self.repeat_button.setEnabled(state.can_replay)
        self.stop_button.setEnabled(state.can_emergency_stop)
        self.sequence_expected_button.setEnabled(
            state.ready
            and not self.sequence_expected_button.isHidden()
            and self._sequence_expected_candidate_count > 0
        )
        disabled_controls = (
            (self.pause_button, state.can_pause, "pause"),
            (self.skip_button, state.can_skip, "skip"),
            (self.repeat_button, state.can_replay, "replay"),
            (self.stop_button, state.can_emergency_stop, "emergency"),
        )
        for button, enabled, control in disabled_controls:
            button.setToolTip("" if enabled else state.reason_for(control))
        if state.ready:
            self._set_action_reason(
                "Ready. Playback controls activate as dialogue state changes."
            )
        else:
            self._set_action_reason(
                state.unavailable_reason
                or "Controls unavailable while VNTTS is starting. Select Full "
                "controls and Check readiness if this does not clear."
            )
        self._fit_content()

    def _set_action_reason(self, message):
        self.action_reason.setText(message)
        for button in (
            self.read_button,
            self.live_button,
            self.sequence_expected_button,
        ):
            button.setToolTip(message)
        self.action_reason.setVisible(not self._ready)

    def set_live(self, running):
        self._live = bool(running)
        self.mode.setText("Reading" if running else "Stopped")
        self.live_button.setText("Stop reading" if running else "Start reading")
        if self._ready:
            self._set_action_reason(
                "Reading active; playback controls are available."
                if running
                else "Ready: start reading or read once."
            )
        self._fit_content()

    def set_paused(self, paused):
        self.mode.setText(
            "Paused" if paused else ("Reading" if self._live else "Stopped")
        )
        self.pause_button.setText("Resume" if paused else "Pause")
        self._fit_content()

    def _fit_content(self):
        right = self.x() + self.width()
        top = self.y()
        for label in (self.status, self.speaker):
            label.setMinimumHeight(label.heightForWidth(label.width()))
        self.adjustSize()
        if self.isVisible():
            self.move(right - self.width(), top)


def configure_floating_window(window, *, platform=None):
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
            native_window.setSharingType_(AppKit.NSWindowSharingNone)
            return True
        if platform == "win32":
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

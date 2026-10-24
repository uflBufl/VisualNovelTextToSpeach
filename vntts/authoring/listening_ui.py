"""Qt workbench for generic blind model listening."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar, Literal, Protocol

from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeySequence, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from vntts.action_icons import set_action_icon
from vntts.async_ui import LatestTaskRunner
from vntts.authoring.listening import (
    ListeningReport,
    ListeningSession,
    ListeningTrial,
    Preference,
    ensure_listening_report,
    listening_progress,
    load_listening_session,
    next_pending_trial,
    record_trial_preference,
)
from vntts.authoring.pcm_playback import (
    PcmClip,
    PersistentPcmPlayer,
    PlaybackSnapshot,
)
from vntts.authoring.review_context_ui import ReviewDecisionContext, review_scroll_area
from vntts.qt_dialogs import CloseGuardedDialog

Side = Literal["a", "b"]
PlaybackState = Literal[
    "ready", "loading", "playing", "finished", "stopped", "complete"
]

_CHOICE_GUIDANCE = (
    "How to choose\n"
    "A / B: one sample is better for the stated goal.\n"
    "Both acceptable: both sound good with no meaningful preference.\n"
    "Neither acceptable: both have problems that make them unsuitable.\n\n"
    "Listen to both samples fully from the beginning, without skipping, before rating.\n"
    "Choices save immediately and cannot be changed in this window.\n\n"
    "Shortcuts: Ctrl+1 plays A; Ctrl+2 plays B; Ctrl+Space controls playback."
)
_GOAL_GUIDANCE = {
    "synthesis-quality": (
        "Synthesis quality",
        "Natural speech without audible defects.",
        "Listen for\n"
        "- Extra or missing words, cut-off speech and repetitions.\n"
        "- Noise, distortion and unnatural pronunciation or rhythm.\n"
        "Compare with the dialogue text.\n\n"
        "Different voice timbres can be equally good; character likeness and acting "
        "style are outside this evaluation.",
    ),
    "overall-preference": (
        "Overall preference",
        "Choose the voice you prefer overall.",
        "Choose the voice you prefer overall, considering timbre, naturalness and "
        "delivery. Character likeness is not scored without an original recording.",
    ),
}
_UNRECORDED_GOAL = (
    "Not recorded",
    "Confirm the intended criterion before rating.",
    "This session did not record an evaluation goal. Check the intended criterion "
    "with its author before rating. Existing choices do not say whether quality, "
    "voice preference or character fit was judged.",
)


class PreferenceRecorder(Protocol):
    def __call__(
        self,
        session_path: Path,
        trial_id: str,
        preference: Preference,
        /,
        *,
        report_path: Path,
    ) -> ListeningSession: ...


class _Playback(Protocol):
    def load(self, path: Path) -> PcmClip: ...

    def play(self, clip: PcmClip) -> int: ...

    def snapshot(self) -> PlaybackSnapshot: ...

    def pause(self) -> None: ...

    def resume(self) -> int: ...

    def seek(self, position_frames: int) -> int: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


SessionLoader = Callable[[Path], ListeningSession]
ProgressReader = Callable[[ListeningSession], tuple[int, int]]
PendingTrialReader = Callable[[ListeningSession], ListeningTrial | None]
ReportBuilder = Callable[[Path, Path], ListeningReport]
PlaybackFactory = Callable[[], _Playback]

_load_session: SessionLoader = load_listening_session
_listening_progress: ProgressReader = listening_progress
_next_pending_trial: PendingTrialReader = next_pending_trial
_record_preference: PreferenceRecorder = record_trial_preference
_ensure_report: ReportBuilder = ensure_listening_report
_playback_factory: PlaybackFactory = PersistentPcmPlayer


class SeekSlider(QSlider):
    seek_requested = Signal(int)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            option,
            QStyle.SubControl.SC_SliderHandle,
            self,
        )
        if handle.contains(event.position().toPoint()):
            super().mousePressEvent(event)
            return
        span = max(1, self.width() - handle.width())
        position = round(event.position().x() - handle.width() / 2)
        value = QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), position, span, option.upsideDown
        )
        self.setValue(value)
        self.seek_requested.emit(value)
        event.accept()


class ModelListeningDialog(CloseGuardedDialog):
    side_colors: ClassVar[Mapping[Side, Mapping[str, str]]] = {
        "a": {"normal": "#2563eb", "disabled": "#1e3a5f", "border": "#bfdbfe"},
        "b": {"normal": "#ea580c", "disabled": "#5f301f", "border": "#fed7aa"},
    }
    stop: QPushButton

    def __init__(
        self,
        session_path: str | Path,
        parent: QWidget | None = None,
        *,
        auto_play: bool = True,
        preference_recorder: PreferenceRecorder = _record_preference,
        playback_factory: PlaybackFactory = _playback_factory,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self.session_path = Path(session_path).expanduser().resolve()
        self.session: ListeningSession = _load_session(self.session_path)
        self.preference_recorder = preference_recorder
        self.preference_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.preference_runner.finished.connect(self._preference_finished)
        self._preference_active = False
        self._close_pending = False
        self.current_trial: ListeningTrial | None = None
        self.auto_play = auto_play
        self.auto_play_pending_b = False
        self.active_side: Side | None = None
        self.active_token: int | None = None
        self.active_started = False
        self.active_initial_natural = False
        self.completed_sides: set[Side] = set()
        self.report_path: Path | None = None
        self.setWindowTitle("Blind voice comparison")
        self.setMinimumSize(640, 400)
        self.resize(760, 460)

        self.progress = QLabel()
        self.progress.setAccessibleName("Blind listening progress")
        self.progress.setAccessibleDescription(
            "Current progress through the listening session"
        )
        self.decision_context = self.create_instruction_context()
        self.context_toggle = QToolButton()
        set_action_icon(self.context_toggle, "circle-help")
        self.context_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.context_toggle.setText("Detailed instructions")
        self.context_toggle.setCheckable(True)
        self.context_toggle.setAccessibleName("Show blind listening instructions")
        self.context_toggle.setToolTip(self.context_toggle.accessibleName())
        self.context_toggle.setAccessibleDescription(
            "Reveal comparison rules and technical provenance without unblinding A/B"
        )
        self.context_toggle.toggled.connect(self.decision_context.setVisible)
        self.context_toggle.toggled.connect(
            lambda visible: self.context_toggle.setText(
                "Hide instructions" if visible else "Detailed instructions"
            )
        )
        self.decision_context.hide()
        self.dialogue = QPlainTextEdit()
        self.dialogue.setReadOnly(True)
        self.dialogue.setAccessibleName("Current blind trial text")
        self.dialogue.setAccessibleDescription(
            "Exact dialogue text shared by anonymous samples A and B"
        )
        self.dialogue.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.dialogue.setFixedHeight(max(64, self.fontMetrics().height() + 20))
        current_trial_layout = QVBoxLayout()
        current_trial_layout.setContentsMargins(0, 0, 0, 0)
        current_trial_layout.setSpacing(2)
        current_trial_layout.addWidget(self.dialogue)
        self.current_trial_card = QWidget()
        self.current_trial_card.setAccessibleName("Current blind trial evidence")
        self.current_trial_card.setLayout(current_trial_layout)
        self.current_trial_card.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        self.play_a = QPushButton("A")
        set_action_icon(self.play_a, "play")
        self.play_b = QPushButton("B")
        set_action_icon(self.play_b, "play")
        self.stop = QPushButton("Pause")
        set_action_icon(self.stop, "pause")
        self.play_a.setShortcut(QKeySequence("Ctrl+1"))
        self.play_b.setShortcut(QKeySequence("Ctrl+2"))
        self.stop.setShortcut(QKeySequence("Ctrl+Space"))
        self.play_a.setAccessibleName("Play anonymous sample A")
        self.play_a.setToolTip(self.play_a.accessibleName())
        self.play_b.setAccessibleName("Play anonymous sample B")
        self.play_b.setToolTip(self.play_b.accessibleName())
        self.stop.setAccessibleName("Control active anonymous sample")
        self.stop.setToolTip(self.stop.accessibleName())
        self.play_a.setAccessibleDescription("Play anonymous sample A")
        self.play_b.setAccessibleDescription("Play anonymous sample B")
        self.stop.setAccessibleDescription(
            "Pause, continue or restart the active anonymous sample"
        )
        width = max(
            self.stop.fontMetrics().horizontalAdvance(label)
            for label in ("Pause", "Resume", "Replay")
        )
        self.stop.setFixedWidth(width + 36)
        self.play_a.clicked.connect(lambda: self.play("a"))
        self.play_b.clicked.connect(lambda: self.play("b"))
        self.stop.clicked.connect(self.toggle_playback)

        self.now_playing = QLabel()
        self.now_playing.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.now_playing.setAccessibleName("Blind sample playback status")
        playback = QHBoxLayout()
        playback.addWidget(self.play_a)
        playback.addWidget(self.play_b)
        self.playback_controls = playback

        self.skip_back = QPushButton("-5s")
        set_action_icon(self.skip_back, "skip-back")
        self.skip_back.setAccessibleName("Skip anonymous sample back five seconds")
        self.skip_back.setToolTip(self.skip_back.accessibleName())
        self.seek = SeekSlider(Qt.Orientation.Horizontal)
        self.seek.setAccessibleName("Anonymous sample playback position")
        self.skip_forward = QPushButton("+5s")
        set_action_icon(self.skip_forward, "skip-forward")
        self.skip_forward.setAccessibleName(
            "Skip anonymous sample forward five seconds"
        )
        self.skip_forward.setToolTip(self.skip_forward.accessibleName())
        self.time = QLabel("0:00 / 0:00")
        self.time.setAccessibleName("Anonymous sample playback time")
        self.time.setMinimumWidth(90)
        self.skip_back.clicked.connect(lambda: self.skip_by(-5_000))
        self.skip_forward.clicked.connect(lambda: self.skip_by(5_000))
        self.seek.sliderMoved.connect(self.seek_to)
        self.seek.seek_requested.connect(self.seek_to)
        seek_controls = QHBoxLayout()
        seek_controls.addWidget(self.skip_back)
        seek_controls.addWidget(self.seek, 1)
        seek_controls.addWidget(self.skip_forward)
        seek_controls.addWidget(self.time)
        seek_controls.addWidget(self.stop)
        self.seek_controls = seek_controls

        self.prefer_a = QPushButton("A is better")
        set_action_icon(self.prefer_a, "check")
        self.tie = QPushButton("Both acceptable")
        set_action_icon(self.tie, "check")
        self.neither = QPushButton("Neither acceptable")
        set_action_icon(self.neither, "x")
        self.prefer_b = QPushButton("B is better")
        set_action_icon(self.prefer_b, "check")
        self.prefer_a.setAccessibleName("Anonymous sample A is better")
        self.prefer_a.setToolTip(self.prefer_a.accessibleName())
        self.tie.setAccessibleName("Both anonymous samples are acceptable")
        self.tie.setToolTip(self.tie.accessibleName())
        self.prefer_a.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.tie.setShortcut(QKeySequence("Ctrl+Shift+T"))
        self.neither.setAccessibleName("Neither sample is acceptable")
        self.neither.setToolTip(self.neither.accessibleName())
        self.neither.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self.prefer_b.setShortcut(QKeySequence("Ctrl+Shift+B"))
        self.prefer_b.setAccessibleName("Anonymous sample B is better")
        self.prefer_b.setToolTip(self.prefer_b.accessibleName())
        self.apply_side_button_style(self.prefer_a, "a")
        self.tie.setStyleSheet(
            "QPushButton { background-color: #52525b; color: white;"
            " font-weight: 700; padding: 6px; }"
            "QPushButton:disabled { background-color: #3f3f46; color: #a1a1aa; }"
        )
        self.neither.setStyleSheet(
            "QPushButton { background-color: #991b1b; color: white;"
            " font-weight: 700; padding: 6px; }"
            "QPushButton:disabled { background-color: #4c1d1d; color: #a1a1aa; }"
        )
        self.apply_side_button_style(self.prefer_b, "b")
        self.prefer_a.clicked.connect(lambda: self.save_preference("a"))
        self.tie.clicked.connect(lambda: self.save_preference("tie"))
        self.neither.clicked.connect(lambda: self.save_preference("neither"))
        self.prefer_b.clicked.connect(lambda: self.save_preference("b"))
        decisions = QGridLayout()
        decisions.addWidget(self.prefer_a, 0, 0)
        decisions.addWidget(self.prefer_b, 0, 1)
        decisions.addWidget(self.tie, 1, 0)
        decisions.addWidget(self.neither, 1, 1)
        self.decision_reason = QLabel(wordWrap=True)
        self.decision_reason.setAccessibleName("Blind decision availability")
        self.decision_notice = QLabel(
            "Saves immediately; no undo.",
            wordWrap=True,
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        self.decision_notice.setAccessibleName("Blind decision permanence")
        self.decision_notice.setAccessibleDescription(
            "Choices save immediately and cannot be changed in this window."
        )
        self.decision_notice.setToolTip(self.decision_notice.accessibleDescription())
        self.status = QLabel(wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self.status.setAccessibleName("Blind listening operation status")
        self.goal_card = self.create_goal_card()
        self.open_report = QPushButton("Open report")
        set_action_icon(self.open_report, "file-text")
        self.open_report.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
        )
        self.open_report.setAccessibleName("Open completed blind listening report")
        self.open_report.setToolTip(self.open_report.accessibleName())
        self.open_report.setAccessibleDescription(
            "Open the saved aggregate report in the default application"
        )
        self.open_report.clicked.connect(self._open_report)
        self.open_report.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        set_action_icon(buttons.button(QDialogButtonBox.StandardButton.Close), "x")
        buttons.rejected.connect(self.close)

        self.review_scroll = self.create_review_scroll(playback, seek_controls)
        layout = QVBoxLayout(self)
        layout.addWidget(self.review_scroll, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.open_report, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.goal_card)
        layout.addWidget(self.decision_reason)
        layout.addLayout(decisions)
        layout.addWidget(buttons)
        self.setTabOrder(self.dialogue, self.play_a)
        self.setTabOrder(self.play_a, self.play_b)
        self.setTabOrder(self.play_b, self.skip_back)
        self.setTabOrder(self.skip_back, self.seek)
        self.setTabOrder(self.seek, self.skip_forward)
        self.setTabOrder(self.skip_forward, self.stop)
        self.setTabOrder(self.stop, self.context_toggle)
        self.setTabOrder(self.context_toggle, self.goal_guidance)
        self.setTabOrder(self.goal_guidance, self.decision_context.technical_toggle)
        self.setTabOrder(self.decision_context.technical_toggle, self.open_report)
        self.setTabOrder(self.open_report, self.prefer_a)
        self.setTabOrder(self.prefer_a, self.tie)
        self.setTabOrder(self.tie, self.neither)
        self.setTabOrder(self.neither, self.prefer_b)

        self.playback: _Playback = playback_factory()
        self.audio_clips: dict[Side, PcmClip] = {}
        self.playback_timer = QTimer(self)
        self.playback_timer.setInterval(20)
        self.playback_timer.timeout.connect(self.poll_playback)
        self.playback_timer.start()
        self.load_next_trial()

    def evaluation_goal(self) -> tuple[str, str, str]:
        return _GOAL_GUIDANCE.get(
            self.session.get("evaluation_goal", ""), _UNRECORDED_GOAL
        )

    def create_goal_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("listeningGoal")
        card.setAccessibleName("Blind listening evaluation goal")
        card.setStyleSheet(
            "QFrame#listeningGoal { background-color: palette(alternate-base);"
            " border: 1px solid palette(mid); border-left: 3px solid palette(highlight);"
            " border-radius: 6px; }"
        )
        self.goal_title = QLabel(textFormat=Qt.TextFormat.PlainText, wordWrap=True)
        self.goal_title.setAccessibleName("Evaluation goal title")
        self.goal_title.setStyleSheet("font-weight: 600;")
        self.goal_summary = QLabel(textFormat=Qt.TextFormat.PlainText, wordWrap=True)
        self.goal_summary.setAccessibleName("Evaluation goal summary")
        content = QVBoxLayout(card)
        content.setContentsMargins(12, 10, 12, 10)
        content.setSpacing(4)
        content.addWidget(self.goal_title)
        content.addWidget(self.goal_summary)
        return card

    def create_instruction_context(self) -> ReviewDecisionContext:
        context = ReviewDecisionContext()
        context.setTitle("Listening guide")
        for label in (
            context.purpose,
            context.identity,
            context.synthesis,
            context.effect,
        ):
            label.hide()
        context.technical_toggle.setText("Session details")
        context.technical_toggle.setAccessibleName("Show listening session details")
        context.technical_toggle.setToolTip(context.technical_toggle.accessibleName())
        self.goal_guidance = QLabel(wordWrap=True, textFormat=Qt.TextFormat.PlainText)
        self.goal_guidance.setAccessibleName("Blind listening evaluation instructions")
        self.goal_guidance.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.goal_guidance.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        content = context.layout()
        assert isinstance(content, QVBoxLayout)
        content.setContentsMargins(12, 20, 12, 12)
        content.setSpacing(12)
        content.insertWidget(0, self.goal_guidance)
        return context

    def create_review_scroll(
        self, playback: QHBoxLayout, seek_controls: QHBoxLayout
    ) -> QScrollArea:
        review_content = QWidget()
        review_layout = QVBoxLayout(review_content)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(8)
        header = QHBoxLayout()
        header.addWidget(self.progress, 1)
        header.addWidget(self.now_playing)
        review_layout.addLayout(header)
        review_layout.addWidget(self.current_trial_card)
        review_layout.addLayout(playback)
        review_layout.addLayout(seek_controls)
        help_row = QHBoxLayout()
        help_row.addWidget(self.context_toggle)
        help_row.addWidget(self.decision_notice, 1)
        review_layout.addLayout(help_row)
        review_layout.addWidget(self.decision_context)
        review_layout.addStretch(1)
        scroll = review_scroll_area(
            review_content, "Scrollable blind listening evidence"
        )
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        for widget in review_content.findChildren(QWidget):
            if widget.focusPolicy() != Qt.FocusPolicy.NoFocus:
                widget.installEventFilter(self)
        return scroll

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.FocusIn and isinstance(watched, QWidget):
            self.review_scroll.ensureWidgetVisible(watched)
        return super().eventFilter(watched, event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.FontChange and hasattr(self, "dialogue"):
            self.dialogue.setFixedHeight(max(64, self.fontMetrics().height() + 20))
            if hasattr(self, "goal_card"):
                self.goal_title.setFont(self.font())
                self.goal_summary.setFont(self.font())
                self.status.setFont(self.font())
        super().changeEvent(event)

    def set_status(
        self, message: str, *, error: bool = True, playback_error: bool = False
    ) -> None:
        self._playback_error = playback_error
        self.status.setText(message)
        self.status.setStyleSheet(
            "QLabel { background-color: palette(alternate-base); padding: 8px;"
            " border-left: 3px solid #d97706; }"
            if message and error
            else ""
        )
        self.status.setFont(self.font())
        self.status.setVisible(bool(message))

    def set_preference_buttons_enabled(
        self, enabled: bool, reason: str | None = None
    ) -> None:
        decisions = (
            (self.prefer_a, "Record anonymous sample A as better"),
            (self.tie, "Record both samples as acceptable with no preference"),
            (self.neither, "Record that neither anonymous sample is acceptable"),
            (self.prefer_b, "Record anonymous sample B as better"),
        )
        if self._preference_active:
            enabled = False
            reason = "Saving choice..."
        if reason is None:
            reason = (
                "Choose a result." if enabled else "Listen to A and B in full to rate."
            )
        self.decision_reason.setText(reason)
        for button, description in decisions:
            button.setEnabled(enabled)
            button.setAccessibleDescription(
                description if enabled else f"Unavailable. {reason}"
            )

    def update_trial_context(self) -> None:
        completed, total = _listening_progress(self.session)
        goal_title, summary, instructions = self.evaluation_goal()
        self.goal_title.setText(f"Evaluation goal: {goal_title}")
        self.goal_summary.setText(summary)
        self.goal_guidance.setText(f"{instructions}\n\n{_CHOICE_GUIDANCE}")
        if self.current_trial is None:
            self.progress.setText(f"{completed} / {total} rated")
            self.decision_context.set_context(
                {
                    "purpose": goal_title,
                    "effect": "Session complete; no further preference is required",
                }
            )
            return
        trial_id = self.current_trial["trial_id"]
        position = next(
            index
            for index, trial in enumerate(self.session["trials"], start=1)
            if trial["trial_id"] == trial_id
        )
        identity = self.current_trial.get("line_id") or self.current_trial["queue_id"]
        self.progress.setText(f"Comparison {position} of {total}")
        self.decision_context.set_context(
            {
                "purpose": goal_title,
                "game_speaker": "Unknown (not captured by the benchmark corpus)",
                "synthesis_voice": "Hidden to preserve the blind comparison",
                "reference": "Hidden to preserve the blind comparison",
                "backend": "Hidden to preserve the blind comparison",
                "model": "Hidden to preserve the blind comparison",
                "generation_profile": "Hidden to preserve the blind comparison",
                "controls": "Same exact text; anonymous samples A and B",
                "effect": (
                    "record a blind preference only; production selection remains "
                    "a separate approval"
                ),
            },
            technical=(
                f"Trial: {trial_id}\n"
                f"Line: {identity}\n"
                f"Queue identity: {self.current_trial['queue_id']}"
            ),
        )

    def apply_side_button_style(
        self, button: QPushButton, side: Side, *, active: bool = False
    ) -> None:
        colors = self.side_colors[side]
        border = colors["border"] if active else colors["normal"]
        button.setStyleSheet(
            "QPushButton {"
            f" background-color: {colors['normal']}; color: white; font-weight: 700;"
            f" border: 4px solid {border}; border-radius: 5px; padding: 6px;"
            "}"
            "QPushButton:disabled {"
            f" background-color: {colors['disabled']}; color: #a1a1aa;"
            " border: 4px solid #52525b;"
            "}"
        )

    def set_playback_indicator(
        self, state: PlaybackState, side: Side | None = None
    ) -> None:
        self.playback_control_state = state
        label = side.upper() if side else None
        self.play_a.setText("A")
        self.play_b.setText("B")
        self.apply_side_button_style(self.play_a, "a")
        self.apply_side_button_style(self.play_b, "b")
        labels = {
            "ready": "",
            "loading": f"Loading {label}...",
            "playing": f"Playing {label}",
            "finished": "",
            "stopped": f"{label} paused" if label else "",
            "complete": "",
        }
        self.now_playing.setText(labels[state])
        self.now_playing.setVisible(bool(labels[state]))
        self.stop.setText(
            {"stopped": "Resume", "finished": "Replay"}.get(state, "Pause")
        )
        set_action_icon(
            self.stop,
            {"stopped": "play", "finished": "refresh-cw"}.get(state, "pause"),
        )
        self.stop.setAccessibleName(f"{self.stop.text()} active anonymous sample")
        self.stop.setToolTip(self.stop.accessibleName())
        self.stop.setEnabled(state not in {"ready", "complete"})
        if side is not None and state in {"loading", "playing"}:
            button = self.play_a if side == "a" else self.play_b
            self.apply_side_button_style(button, side, active=True)

    def load_next_trial(self) -> None:
        self.session = _load_session(self.session_path)
        self.current_trial = _next_pending_trial(self.session)
        self.update_trial_context()
        self.set_status("")
        self.auto_play_pending_b = False
        self.active_side = None
        self.active_token = None
        self.active_started = False
        self.active_initial_natural = False
        self.completed_sides.clear()
        self.seek.setRange(0, 0)
        self.seek.setValue(0)
        self.time.setText("0:00 / 0:00")
        self.set_preference_buttons_enabled(False)
        if self.current_trial is None:
            self.set_playback_indicator("complete")
            self.set_preference_buttons_enabled(False, "Session complete.")
            report_path = self.session_path.with_name("report.json")
            _ensure_report(self.session_path, report_path)
            self.report_path = report_path
            self.set_status("Session complete. Results saved.", error=False)
            for widget in (
                self.current_trial_card,
                self.now_playing,
                self.play_a,
                self.play_b,
                self.skip_back,
                self.seek,
                self.skip_forward,
                self.time,
                self.stop,
                self.decision_reason,
                self.decision_notice,
                self.goal_card,
                self.prefer_a,
                self.prefer_b,
                self.tie,
                self.neither,
                self.context_toggle,
                self.decision_context,
            ):
                widget.hide()
            self.open_report.show()
            self.open_report.setFocus()
            completion_height = max(140, self.fontMetrics().height() * 7)
            self.setMinimumHeight(completion_height)
            self.resize(640, completion_height)
            return
        self.setMinimumHeight(400)
        for widget in (
            self.current_trial_card,
            self.play_a,
            self.play_b,
            self.skip_back,
            self.seek,
            self.skip_forward,
            self.time,
            self.stop,
            self.decision_reason,
            self.goal_card,
            self.prefer_a,
            self.prefer_b,
            self.tie,
            self.neither,
            self.context_toggle,
            self.decision_notice,
        ):
            widget.show()
        self.open_report.hide()
        self.set_playback_indicator("ready")
        for widget in (
            self.play_a,
            self.play_b,
            self.skip_back,
            self.seek,
            self.skip_forward,
        ):
            widget.setEnabled(True)
        self.dialogue.setPlainText(self.current_trial.get("text", ""))
        self.playback.stop()
        self.audio_clips = {}
        for side in ("a", "b"):
            path = self.session_path.parent / self.current_trial["audio"][side]
            self.audio_clips[side] = self.playback.load(path)
        if self.auto_play:
            QTimer.singleShot(0, self, self.start_auto_playback)

    def _open_report(self) -> None:
        if self.report_path is not None and not QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.report_path))
        ):
            self.set_status(f"Could not open report; saved at {self.report_path}.")

    def start_auto_playback(self) -> None:
        if self.current_trial is not None and self.playback_timer.isActive():
            self.auto_play_pending_b = True
            self.play("a")

    def play(self, side: Side) -> None:
        if self.current_trial is None or not self.playback_timer.isActive():
            return
        if side == "b":
            self.auto_play_pending_b = False
        self.active_side = side
        self.active_started = False
        self.active_initial_natural = True
        clip = self.audio_clips[side]
        self.active_token = self.playback.play(clip)
        self.seek.setRange(0, clip.duration_ms)
        self.seek.setValue(0)
        self.update_time_label(0, clip.duration_ms)
        self.set_playback_indicator("loading", side)
        if self._playback_error:
            self.set_status("")

    def poll_playback(self) -> None:
        side = self.active_side
        token = self.active_token
        if side is None or token is None:
            return
        snapshot = self.playback.snapshot()
        if snapshot.token != token:
            return
        clip = self.audio_clips[side]
        position = round(snapshot.position_frames * 1000 / clip.sample_rate)
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        self.update_time_label(position, clip.duration_ms)
        if snapshot.error:
            self.active_token = None
            self.active_initial_natural = False
            self.auto_play_pending_b = False
            self.set_playback_indicator("finished", side)
            self.set_status(
                f"Could not play {side.upper()}: {snapshot.error}. Replay to retry.",
                playback_error=True,
            )
            return
        if snapshot.started and not self.active_started:
            self.active_started = True
            self.set_playback_indicator("playing", side)
        if not snapshot.finished:
            return
        self.active_token = None
        if snapshot.underflowed:
            self.active_initial_natural = False
            self.auto_play_pending_b = False
            self.set_playback_indicator("finished", side)
            self.set_status(
                f"Audio interrupted for {side.upper()}. Replay it in full to rate.",
                playback_error=True,
            )
            return
        completed_naturally = snapshot.started and self.active_initial_natural
        self.active_initial_natural = False
        if completed_naturally:
            self.completed_sides.add(side)
        if self.completed_sides == {"a", "b"}:
            self.set_preference_buttons_enabled(
                True,
                "Choose a result.",
            )
        elif side not in self.completed_sides:
            self.set_preference_buttons_enabled(
                False,
                f"Replay {side.upper()} without skipping to rate.",
            )
        else:
            remaining = ({"a", "b"} - self.completed_sides).pop().upper()
            self.set_preference_buttons_enabled(
                False,
                f"Listen to {remaining} in full to rate.",
            )
        self.set_playback_indicator("finished", side)
        if side == "a" and self.auto_play_pending_b and side in self.completed_sides:
            self.auto_play_pending_b = False
            QTimer.singleShot(0, self, lambda: self.play("b"))
        elif side not in self.completed_sides:
            self.auto_play_pending_b = False

    @staticmethod
    def format_time(milliseconds: int) -> str:
        seconds = max(0, int(milliseconds)) // 1000
        return f"{seconds // 60}:{seconds % 60:02d}"

    def update_time_label(self, position: int, duration: int) -> None:
        self.time.setText(
            f"{self.format_time(position)} / {self.format_time(duration)}"
        )

    def seek_to(self, position: int) -> None:
        if self.active_side is None:
            return
        self.active_initial_natural = False
        clip = self.audio_clips[self.active_side]
        frame = round(max(0, position) * clip.sample_rate / 1000)
        self.active_token = self.playback.seek(frame)
        self.seek.setValue(position)
        self.update_time_label(position, clip.duration_ms)

    def skip_by(self, delta: int) -> None:
        if self.active_side is None:
            return
        clip = self.audio_clips[self.active_side]
        position = max(0, min(clip.duration_ms, self.seek.value() + delta))
        self.seek_to(position)

    def stop_audio(self) -> None:
        self.auto_play_pending_b = False
        self.active_token = None
        self.active_initial_natural = False
        self.playback.stop()
        self.set_playback_indicator("stopped", self.active_side)

    def toggle_playback(self) -> None:
        if self.current_trial is None or self.active_side is None:
            return
        if self.playback_control_state == "stopped":
            self.active_started = False
            self.active_token = self.playback.resume()
            self.set_playback_indicator("loading", self.active_side)
        elif self.playback_control_state == "finished":
            self.play(self.active_side)
        elif self.playback_control_state in {"loading", "playing"}:
            self.auto_play_pending_b = False
            self.active_token = self.playback.seek(
                round(
                    self.seek.value()
                    * self.audio_clips[self.active_side].sample_rate
                    / 1000
                )
            )
            self.playback.pause()
            self.set_playback_indicator("stopped", self.active_side)

    def save_preference(self, preference: Preference) -> None:
        if self.current_trial is None:
            return
        if self._preference_active:
            return
        if self.completed_sides != {"a", "b"}:
            self.set_preference_buttons_enabled(False)
            return
        trial_id = self.current_trial["trial_id"]
        self._preference_active = True
        self.set_preference_buttons_enabled(False)
        self.set_status("")
        self.preference_runner.start(
            self.preference_recorder,
            self.session_path,
            trial_id,
            preference,
            report_path=self.session_path.with_name("report.json"),
        )

    def _preference_finished(self, _result: object, error: Exception | None) -> None:
        self._preference_active = False
        if error is None:
            self.stop_audio()
            self.load_next_trial()
        elif "Preference was saved" in str(error):
            self._show_persisted_score_with_report_error(str(error))
        else:
            self.set_preference_buttons_enabled(
                self.current_trial is not None and self.completed_sides == {"a", "b"},
                "Choose again to retry.",
            )
            self.set_status(
                f"Could not save your choice: {error}. Choose again to retry."
            )
        if self._close_pending:
            self._close_pending = False
            self.close()

    def _show_persisted_score_with_report_error(self, message: str) -> None:
        self.stop_audio()
        self.session = _load_session(self.session_path)
        self.current_trial = _next_pending_trial(self.session)
        self.update_trial_context()
        if self.current_trial is not None:
            self.load_next_trial()
        else:
            self.set_playback_indicator("complete")
            self.set_preference_buttons_enabled(False, "Session complete.")
            for widget in (
                self.play_a,
                self.play_b,
                self.skip_back,
                self.seek,
                self.skip_forward,
            ):
                widget.setEnabled(False)
        self.set_status(message)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._preference_active:
            self._close_pending = True
            self.set_status("Finishing save before closing...", error=False)
            event.ignore()
            return
        self.playback_timer.stop()
        self.playback.close()
        super().closeEvent(event)


def launch_listening_workbench(session_path: str | Path) -> int:
    _application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = ModelListeningDialog(session_path)
    except Exception as error:
        QMessageBox.critical(None, "Unable to open listening workbench", str(error))
        return 1
    dialog.exec()
    return 0

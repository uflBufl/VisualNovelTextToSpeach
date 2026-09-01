"""Qt workbench for generic blind model listening."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
)

from vntts.async_ui import LatestTaskRunner
from vntts.authoring.listening import (
    ensure_listening_report,
    listening_progress,
    load_listening_session,
    next_pending_trial,
    record_trial_preference,
)
from vntts.authoring.review_context_ui import ReviewDecisionContext


class SeekSlider(QSlider):
    seek_requested = Signal(int)

    def mousePressEvent(self, event):
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


class ModelListeningDialog(QDialog):
    side_colors = {
        "a": {"normal": "#2563eb", "disabled": "#1e3a5f", "border": "#bfdbfe"},
        "b": {"normal": "#ea580c", "disabled": "#5f301f", "border": "#fed7aa"},
    }

    def __init__(
        self,
        session_path,
        parent=None,
        *,
        auto_play=True,
        preference_recorder=record_trial_preference,
        thread_pool=None,
    ):
        super().__init__(parent)
        self.session_path = Path(session_path).expanduser().resolve()
        self.session = load_listening_session(self.session_path)
        self.preference_recorder = preference_recorder
        self.preference_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.preference_runner.finished.connect(self._preference_finished)
        self._preference_active = False
        self._close_pending = False
        self.current_trial = None
        self.auto_play = auto_play
        self.auto_play_pending_b = False
        self.active_side = None
        self.started_sides = set()
        self.setWindowTitle("Blind voice-model listening workbench")
        self.setMinimumSize(640, 400)
        self.resize(900, 520)

        self.progress = QLabel()
        self.progress.setAccessibleName("Blind listening progress")
        self.progress.setAccessibleDescription(
            "Completed and remaining trials in the current blind session"
        )
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setAccessibleName("Completed blind listening trials")
        self.decision_context = ReviewDecisionContext()
        self.trial_heading = QLabel()
        self.trial_heading.setWordWrap(True)
        self.trial_heading.setAccessibleName("Current blind trial")
        self.dialogue = QPlainTextEdit()
        self.dialogue.setReadOnly(True)
        self.dialogue.setAccessibleName("Current blind trial text")
        self.dialogue.setAccessibleDescription(
            "Exact dialogue text shared by anonymous samples A and B"
        )
        self.dialogue.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.dialogue.setMinimumHeight(90)
        current_trial_layout = QVBoxLayout()
        current_trial_layout.addWidget(self.trial_heading)
        current_trial_layout.addWidget(self.dialogue, 1)
        self.current_trial_card = QGroupBox("Current blind trial")
        self.current_trial_card.setAccessibleName("Current blind trial evidence")
        self.current_trial_card.setLayout(current_trial_layout)
        self.play_a = QPushButton("Play A")
        self.play_b = QPushButton("Play B")
        self.stop = QPushButton("Stop")
        self.play_a.setShortcut(QKeySequence("Ctrl+1"))
        self.play_b.setShortcut(QKeySequence("Ctrl+2"))
        self.stop.setShortcut(QKeySequence("Ctrl+Space"))
        self.play_a.setAccessibleName("Play anonymous sample A")
        self.play_b.setAccessibleName("Play anonymous sample B")
        self.stop.setAccessibleName("Control active anonymous sample")
        self.play_a.setAccessibleDescription("Play anonymous sample A")
        self.play_b.setAccessibleDescription("Play anonymous sample B")
        self.stop.setAccessibleDescription(
            "Pause, continue or restart the active anonymous sample"
        )
        width = max(
            self.stop.fontMetrics().horizontalAdvance(label)
            for label in ("Stop", "Continue", "Start again")
        )
        self.stop.setFixedWidth(width + 36)
        self.play_a.clicked.connect(lambda: self.play("a"))
        self.play_b.clicked.connect(lambda: self.play("b"))
        self.stop.clicked.connect(self.toggle_playback)

        self.now_playing = QLabel()
        self.now_playing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.now_playing.setMinimumHeight(46)
        self.now_playing.setAccessibleName("Blind sample playback status")
        playback = QHBoxLayout()
        playback.addWidget(self.play_a)
        playback.addWidget(self.play_b)
        self.playback_controls = playback

        self.skip_back = QPushButton("-5s")
        self.skip_back.setAccessibleName("Skip anonymous sample back five seconds")
        self.seek = SeekSlider(Qt.Orientation.Horizontal)
        self.seek.setAccessibleName("Anonymous sample playback position")
        self.skip_forward = QPushButton("+5s")
        self.skip_forward.setAccessibleName(
            "Skip anonymous sample forward five seconds"
        )
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
        self.tie = QPushButton("Both acceptable / no preference")
        self.neither = QPushButton("Neither acceptable")
        self.prefer_b = QPushButton("B is better")
        self.prefer_a.setAccessibleName("Anonymous sample A is better")
        self.tie.setAccessibleName("Both anonymous samples are acceptable")
        self.prefer_a.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.tie.setShortcut(QKeySequence("Ctrl+Shift+T"))
        self.neither.setAccessibleName("Neither sample is acceptable")
        self.neither.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self.prefer_b.setShortcut(QKeySequence("Ctrl+Shift+B"))
        self.prefer_b.setAccessibleName("Anonymous sample B is better")
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
        self.decision_reason = QLabel()
        self.decision_reason.setWordWrap(True)
        self.decision_reason.setAccessibleName("Blind decision availability")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Blind listening operation status")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.progress)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.decision_context)
        layout.addWidget(self.current_trial_card, 1)
        layout.addWidget(self.now_playing)
        layout.addLayout(playback)
        layout.addLayout(seek_controls)
        layout.addWidget(self.decision_reason)
        layout.addLayout(decisions)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        self.setTabOrder(self.dialogue, self.play_a)
        self.setTabOrder(self.play_a, self.play_b)
        self.setTabOrder(self.play_b, self.skip_back)
        self.setTabOrder(self.skip_back, self.seek)
        self.setTabOrder(self.seek, self.skip_forward)
        self.setTabOrder(self.skip_forward, self.stop)
        self.setTabOrder(self.stop, self.prefer_a)
        self.setTabOrder(self.prefer_a, self.tie)
        self.setTabOrder(self.tie, self.neither)
        self.setTabOrder(self.neither, self.prefer_b)

        self.audio_output = QAudioOutput(self)
        self.players = {}
        for side in ("a", "b"):
            player = QMediaPlayer(self)
            player.mediaStatusChanged.connect(
                lambda status, source_side=side: self.media_status_changed(
                    source_side, status
                )
            )
            player.playbackStateChanged.connect(
                lambda state, source_side=side: self.playback_state_changed(
                    source_side, state
                )
            )
            player.durationChanged.connect(
                lambda duration, source_side=side: self.duration_changed(
                    source_side, duration
                )
            )
            player.positionChanged.connect(
                lambda position, source_side=side: self.position_changed(
                    source_side, position
                )
            )
            self.players[side] = player
        self.load_next_trial()

    def set_preference_buttons_enabled(self, enabled, reason=None):
        decisions = (
            (self.prefer_a, "Record anonymous sample A as better"),
            (self.tie, "Record both samples as acceptable with no preference"),
            (self.neither, "Record that neither anonymous sample is acceptable"),
            (self.prefer_b, "Record anonymous sample B as better"),
        )
        if reason is None:
            reason = (
                "Decision ready: choose one result."
                if enabled
                else "Decision locked: start both anonymous samples first."
            )
        self.decision_reason.setText(reason)
        for button, description in decisions:
            button.setEnabled(enabled)
            button.setAccessibleDescription(
                description if enabled else f"Unavailable. {reason}"
            )

    def update_trial_context(self):
        completed, total = listening_progress(self.session)
        remaining = total - completed
        self.progress.setText(
            f"Completed {completed} of {total} | Remaining {remaining}"
        )
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(completed)
        if self.current_trial is None:
            self.trial_heading.setText(f"All {total} trials reviewed")
            self.decision_context.set_context(
                {
                    "purpose": "Compare two anonymous synthesis outputs",
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
        self.trial_heading.setText(f"Trial {position} of {total} | {identity}")
        self.decision_context.set_context(
            {
                "purpose": "Compare two anonymous synthesis outputs",
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

    def apply_side_button_style(self, button, side, *, active=False):
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

    def set_playback_indicator(self, state, side=None):
        self.playback_control_state = state
        label = side.upper() if side else None
        self.play_a.setText("Play A")
        self.play_b.setText("Play B")
        self.apply_side_button_style(self.play_a, "a")
        self.apply_side_button_style(self.play_b, "b")
        labels = {
            "ready": "READY",
            "loading": f"LOADING: {label}",
            "playing": f"NOW PLAYING: {label}",
            "finished": f"FINISHED: {label}",
            "stopped": f"STOPPED: {label}" if label else "STOPPED",
            "complete": "SESSION COMPLETE",
        }
        background = self.side_colors.get(side, {}).get("normal", "#3f3f46")
        self.now_playing.setText(labels[state])
        self.now_playing.setStyleSheet(
            f"QLabel {{ background-color: {background}; color: white;"
            " font-size: 18px; font-weight: 700; border-radius: 6px; padding: 8px; }"
        )
        self.stop.setText(
            {"stopped": "Continue", "finished": "Start again"}.get(state, "Stop")
        )
        self.stop.setEnabled(state not in {"ready", "complete"})
        if side is not None and state in {"loading", "playing"}:
            button = self.play_a if side == "a" else self.play_b
            button.setText(f"{'LOADING' if state == 'loading' else 'PLAYING'} {label}")
            self.apply_side_button_style(button, side, active=True)

    def load_next_trial(self):
        self.session = load_listening_session(self.session_path)
        self.current_trial = next_pending_trial(self.session)
        self.update_trial_context()
        self.auto_play_pending_b = False
        self.active_side = None
        self.started_sides.clear()
        self.seek.setRange(0, 0)
        self.seek.setValue(0)
        self.time.setText("0:00 / 0:00")
        self.set_preference_buttons_enabled(
            False,
            "Decision locked: start both anonymous samples before choosing.",
        )
        if self.current_trial is None:
            self.set_playback_indicator("complete")
            self.set_preference_buttons_enabled(False, "Session complete.")
            report_path = self.session_path.with_name("report.json")
            report = ensure_listening_report(self.session_path, report_path)
            leader = report["models"][0]["model_id"] if report["models"] else "none"
            self.dialogue.setPlainText("Listening session complete.")
            self.status.setText(
                f"Unblinded aggregate report: {report_path}. Current leader: {leader}. "
                "Production selection still requires manual approval."
            )
            for widget in (
                self.play_a,
                self.play_b,
                self.skip_back,
                self.seek,
                self.skip_forward,
            ):
                widget.setEnabled(False)
            return
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
        for side, player in self.players.items():
            player.stop()
            path = self.session_path.parent / self.current_trial["audio"][side]
            player.setSource(QUrl.fromLocalFile(str(path)))
        self.status.setText(
            "Starting A, then B automatically. Shortcuts: Ctrl+1 plays A; "
            "Ctrl+2 plays B; Ctrl+Space controls the active sample."
        )
        if self.auto_play:
            QTimer.singleShot(0, self.start_auto_playback)

    def start_auto_playback(self):
        if self.current_trial is not None:
            self.auto_play_pending_b = True
            self.play("a", automatic=True)

    def play(self, side, *, automatic=False):
        if self.current_trial is None:
            return
        if side == "b":
            self.auto_play_pending_b = False
        other_side = "b" if side == "a" else "a"
        self.players[other_side].stop()
        player = self.players[side]
        player.setAudioOutput(self.audio_output)
        player.setPosition(0)
        self.active_side = side
        self.duration_changed(side, player.duration())
        self.position_changed(side, 0)
        self.set_playback_indicator("loading", side)
        player.play()
        mode = " automatically" if automatic else ""
        self.status.setText(f"Playing anonymous sample {side.upper()}{mode}.")

    def playback_state_changed(self, side, state):
        if (
            state == QMediaPlayer.PlaybackState.PlayingState
            and side == self.active_side
        ):
            self.set_playback_indicator("playing", side)
            self.started_sides.add(side)
            if self.started_sides == {"a", "b"}:
                self.set_preference_buttons_enabled(
                    True,
                    "Decision ready: choose A, both acceptable, neither acceptable, or B.",
                )
                self.status.setText("Both samples have started. Choose a preference.")

    def media_status_changed(self, side, status):
        if status != QMediaPlayer.MediaStatus.EndOfMedia or side != self.active_side:
            return
        self.position_changed(side, self.players[side].duration())
        self.set_playback_indicator("finished", side)
        if side == "a" and self.auto_play_pending_b:
            self.auto_play_pending_b = False
            QTimer.singleShot(0, lambda: self.play("b", automatic=True))

    @staticmethod
    def format_time(milliseconds):
        seconds = max(0, int(milliseconds)) // 1000
        return f"{seconds // 60}:{seconds % 60:02d}"

    def update_time_label(self, position, duration):
        self.time.setText(
            f"{self.format_time(position)} / {self.format_time(duration)}"
        )

    def duration_changed(self, side, duration):
        if side != self.active_side:
            return
        self.seek.setRange(0, max(0, int(duration)))
        self.update_time_label(self.players[side].position(), duration)

    def position_changed(self, side, position):
        if side != self.active_side:
            return
        position = max(0, int(position))
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        self.update_time_label(position, self.players[side].duration())

    def seek_to(self, position):
        if self.active_side is None:
            return
        player = self.players[self.active_side]
        player.setPosition(position)
        self.seek.setValue(position)
        self.update_time_label(position, player.duration())

    def skip_by(self, delta):
        if self.active_side is None:
            return
        player = self.players[self.active_side]
        duration = max(0, int(player.duration()))
        position = max(0, min(duration, int(player.position()) + delta))
        player.setPosition(position)
        self.update_time_label(position, duration)

    def stop_audio(self):
        self.auto_play_pending_b = False
        for player in self.players.values():
            player.stop()
        self.set_playback_indicator("stopped", self.active_side)

    def toggle_playback(self):
        if self.current_trial is None or self.active_side is None:
            return
        player = self.players[self.active_side]
        if self.playback_control_state == "stopped":
            self.set_playback_indicator("loading", self.active_side)
            player.play()
        elif self.playback_control_state == "finished":
            player.setPosition(0)
            self.set_playback_indicator("loading", self.active_side)
            player.play()
        elif self.playback_control_state in {"loading", "playing"}:
            player.pause()
            self.set_playback_indicator("stopped", self.active_side)

    def save_preference(self, preference):
        if self.current_trial is None:
            return
        if self._preference_active:
            self.status.setText("Wait for the current preference to finish saving.")
            return
        if self.started_sides != {"a", "b"}:
            self.status.setText("Start both samples before choosing a preference.")
            return
        trial_id = self.current_trial["trial_id"]
        self._preference_active = True
        self.set_preference_buttons_enabled(
            False,
            "Decision saving: playback remains available.",
        )
        self.status.setText(
            "Saving preference and updating the blinded report... "
            "Playback remains available."
        )
        self.preference_runner.start(
            self.preference_recorder,
            self.session_path,
            trial_id,
            preference,
            report_path=self.session_path.with_name("report.json"),
        )

    def _preference_finished(self, _result, error):
        self._preference_active = False
        if error is None:
            self.stop_audio()
            self.load_next_trial()
        elif "Preference was saved" in str(error):
            self._show_persisted_score_with_report_error(str(error))
        else:
            self.set_preference_buttons_enabled(
                self.current_trial is not None and self.started_sides == {"a", "b"},
                "Decision ready: the previous save failed; choose again to retry.",
            )
            self.status.setText(
                f"Preference was not saved: {error}. Choose again to retry."
            )
        if self._close_pending:
            self._close_pending = False
            self.close()

    def _show_persisted_score_with_report_error(self, message):
        self.stop_audio()
        self.session = load_listening_session(self.session_path)
        self.current_trial = next_pending_trial(self.session)
        self.update_trial_context()
        if self.current_trial is not None:
            self.load_next_trial()
        else:
            self.set_playback_indicator("complete")
            self.set_preference_buttons_enabled(False, "Session complete.")
            self.dialogue.setPlainText("Listening session complete.")
            for widget in (
                self.play_a,
                self.play_b,
                self.skip_back,
                self.seek,
                self.skip_forward,
            ):
                widget.setEnabled(False)
        self.status.setText(message)

    def closeEvent(self, event):
        if self._preference_active:
            self._close_pending = True
            self.status.setText(
                "Saving preference and report. Close is deferred until the "
                "authoritative write finishes."
            )
            event.ignore()
            return
        for player in self.players.values():
            player.stop()
        super().closeEvent(event)


def launch_listening_workbench(session_path):
    _application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = ModelListeningDialog(session_path)
    except Exception as error:
        QMessageBox.critical(None, "Unable to open listening workbench", str(error))
        return 1
    dialog.exec()
    return 0

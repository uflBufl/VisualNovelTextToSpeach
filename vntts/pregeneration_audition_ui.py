"""Minimal player-facing voice audition card for offline preparation."""

from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from vntts.async_ui import LatestTaskRunner
from vntts.pregeneration_audition import VoiceAuditionPreviewService
from vntts.pregeneration_voices import VoicePlan
from vntts.voices import default_voice_choice_id


class VoiceAuditionUIError(RuntimeError):
    """The player audition card received an invalid unresolved plan."""


class VoiceAuditionPanel(QGroupBox):
    completed = Signal()
    cancelled = Signal()

    def __init__(
        self,
        decisions,
        *,
        preview_service=None,
        thread_pool=None,
        player=None,
        parent=None,
    ):
        super().__init__("Choose a character voice", parent)
        self.decisions = decisions
        self.preview_service = preview_service or VoiceAuditionPreviewService()
        self.preview_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.preview_runner.finished.connect(self._preview_finished)
        self.decision_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.decision_runner.finished.connect(self._decision_finished)
        self.audio_output = None
        self.player = player
        self._plan = None
        self._groups = ()
        self._group_index = 0
        self._candidate_index = 0
        self._preview = None
        self._pending_source_id = None
        self._cancel_requested = False
        self._terminal_emitted = False

        self.character = QLabel()
        self.character.setAccessibleName("Character needing a voice choice")
        self.character.setWordWrap(True)
        self.progress = QLabel()
        self.progress.setAccessibleName("Voice choice progress")
        self.sample = QLabel()
        self.sample.setAccessibleName("Voice preview phrase")
        self.sample.setWordWrap(True)
        self.candidate = QLabel()
        self.candidate.setAccessibleName("Current voice candidate")
        self.status = QLabel()
        self.status.setAccessibleName("Voice preview status")
        self.status.setWordWrap(True)

        self.replay_button = QPushButton("Replay")
        self.use_button = QPushButton("Use this voice")
        self.next_button = QPushButton("Try another")
        self.narrator_button = QPushButton("Use narrator")
        self.replay_button.clicked.connect(self.replay)
        self.use_button.clicked.connect(self.use_current)
        self.next_button.clicked.connect(self.try_another)
        self.narrator_button.clicked.connect(self.use_narrator)

        actions = QHBoxLayout()
        actions.addWidget(self.replay_button)
        actions.addWidget(self.use_button)
        actions.addWidget(self.next_button)
        actions.addWidget(self.narrator_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.progress)
        layout.addWidget(self.character)
        layout.addWidget(self.sample)
        layout.addWidget(self.candidate)
        layout.addWidget(self.status)
        layout.addLayout(actions)
        self.setVisible(False)

    @property
    def active(self):
        return self.preview_runner.active or self.decision_runner.active

    def start(self, plan):
        if not isinstance(plan, VoicePlan):
            raise VoiceAuditionUIError("Voice audition plan is invalid")
        groups = tuple(
            group for group in plan.groups if group.route == "needs-audition"
        )
        if not groups:
            raise VoiceAuditionUIError("Voice audition plan has no unresolved voices")
        if any(not group.candidates for group in groups):
            raise VoiceAuditionUIError(
                "An unresolved voice has no candidate available to preview"
            )
        self._plan = plan
        self._groups = groups
        self._group_index = 0
        self._candidate_index = 0
        self._preview = None
        self._pending_source_id = None
        self._cancel_requested = False
        self._terminal_emitted = False
        self.setVisible(True)
        self._show_group()

    def current_group(self):
        return self._groups[self._group_index]

    def current_candidate(self):
        return self.current_group().candidates[self._candidate_index]

    def replay(self):
        if self.active or self._cancel_requested:
            return
        if self._preview is None:
            self._start_preview()
            return
        player = self._ensure_player()
        player.stop()
        player.setSource(QUrl.fromLocalFile(str(self._preview.path)))
        player.play()
        self.status.setText("Playing the same preview again...")

    def try_another(self):
        group = self.current_group()
        if self.active or self._cancel_requested or len(group.candidates) < 2:
            return
        self._stop_player()
        self._candidate_index = (self._candidate_index + 1) % len(group.candidates)
        self._preview = None
        self._show_candidate()
        self._start_preview()

    def use_current(self):
        if self._preview is None or self.active or self._cancel_requested:
            return
        self._save_decision(self.current_candidate().source_id)

    def use_narrator(self):
        if self.active or self._cancel_requested:
            return
        self._save_decision(default_voice_choice_id)

    def cancel(self):
        if self._terminal_emitted:
            return
        self._cancel_requested = True
        self._stop_player()
        self.preview_service.cancel()
        self._set_actions_enabled(False)
        self.status.setText("Cancelling voice preview...")
        if not self.active:
            self._emit_cancelled()

    def shutdown(self):
        self._stop_player()
        if not self.active:
            self.preview_service.close()

    def _show_group(self):
        self._candidate_index = 0
        self._preview = None
        group = self.current_group()
        self.progress.setText(f"Voice {self._group_index + 1} of {len(self._groups)}")
        speakers = tuple(value for value in group.speakers if value != group.character)
        context = f" (also shown as {', '.join(speakers)})" if speakers else ""
        self.character.setText(f"Which voice should {group.character}{context} use?")
        self.sample.setText(f'Preview phrase: "{group.sample_text}"')
        self._show_candidate()
        self._start_preview()

    def _show_candidate(self):
        group = self.current_group()
        candidate = self.current_candidate()
        self.candidate.setText(
            f"Candidate {self._candidate_index + 1} of {len(group.candidates)}: "
            f"{candidate.source_character}"
        )

    def _start_preview(self):
        self._set_actions_enabled(False)
        self.replay_button.setText("Replay")
        self.status.setText("Preparing a short voice preview...")
        self.preview_runner.start(
            self.preview_service.generate,
            self._plan,
            self.current_group(),
            self.current_candidate().source_id,
        )

    def _preview_finished(self, preview, error):
        if self._cancel_requested:
            self._emit_cancelled()
            return
        if error is not None:
            self._preview = None
            self._set_actions_enabled(True)
            self.use_button.setEnabled(False)
            self.replay_button.setText("Try preview again")
            self.status.setText(
                "This preview could not be generated. Try again, try another "
                "voice, or use narrator."
            )
            return
        self._preview = preview
        self._set_actions_enabled(True)
        self.replay_button.setText("Replay")
        self.replay()
        self.status.setText("Listen, then choose this voice or try another.")

    def _save_decision(self, source_id):
        self._stop_player()
        self._pending_source_id = source_id
        self._set_actions_enabled(False)
        self.status.setText("Saving this voice choice...")
        self.decision_runner.start(
            self.decisions.remember,
            self.current_group(),
            source_id,
        )

    def _decision_finished(self, _result, error):
        source_id = self._pending_source_id
        self._pending_source_id = None
        if self._cancel_requested:
            self._emit_cancelled()
            return
        if error is not None:
            self._set_actions_enabled(True)
            self.status.setText(
                "Unable to save this voice choice. Check that VNTTS can write "
                "its application data, then try again."
            )
            return
        if source_id is None:
            self._set_actions_enabled(True)
            self.status.setText("Unable to identify the saved voice choice.")
            return
        self._group_index += 1
        if self._group_index < len(self._groups):
            self._show_group()
            return
        self._terminal_emitted = True
        self.setVisible(False)
        self.preview_service.close()
        self.completed.emit()

    def _set_actions_enabled(self, enabled):
        enabled = bool(enabled) and not self._cancel_requested
        group = self.current_group() if self._groups else None
        self.replay_button.setEnabled(enabled)
        self.use_button.setEnabled(enabled and self._preview is not None)
        self.next_button.setEnabled(
            enabled and group is not None and len(group.candidates) > 1
        )
        self.narrator_button.setEnabled(enabled)

    def _emit_cancelled(self):
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        self.setVisible(False)
        self.preview_service.close()
        self.cancelled.emit()

    def _ensure_player(self):
        if self.player is None:
            self.audio_output = QAudioOutput(self)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio_output)
        return self.player

    def _stop_player(self):
        if self.player is not None:
            self.player.stop()


__all__ = ["VoiceAuditionPanel", "VoiceAuditionUIError"]

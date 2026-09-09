"""One-at-a-time voice verification for self-service pregeneration."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from vntts_artifacts.file_integrity import sha256_file

from vntts.async_ui import LatestTaskRunner
from vntts.pregeneration_audition import VoiceAuditionError, VoiceAuditionPreviewService
from vntts.pregeneration_voices import VoicePlan
from vntts.qt_audio import QtPcmPlayer as QMediaPlayer
from vntts.speech_presentation import engine_model_label, speech_runtime_label
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
        super().__init__("Verify character voices", parent)
        self.decisions = decisions
        self._owns_preview_service = preview_service is None
        self.preview_service = preview_service or VoiceAuditionPreviewService()
        self.preview_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.preview_runner.finished.connect(self._preview_finished)
        self.decision_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.decision_runner.finished.connect(self._decision_finished)
        self.player = player
        self._plan = None
        self._groups = ()
        self._group_index = 0
        self._candidate_offset = 0
        self._candidate_entries = ()
        self._previews = {}
        self._displayed = ()
        self._sample_text = None
        self._alternate_active = False
        self._pending_decisions = []
        self._save_succeeded = False
        self._cancel_requested = False
        self._terminal_emitted = False
        self._ignore_preview_result = False
        self._shutdown_requested = False

        self.summary = QLabel()
        self.summary.setAccessibleName("Voice choice estimate")
        self.summary.setWordWrap(True)
        self.choose_all_button = QPushButton("Choose all automatically")
        self.choose_all_button.clicked.connect(self.choose_all_automatically)
        summary_row = QHBoxLayout()
        summary_row.addWidget(self.summary, 1)
        summary_row.addWidget(self.choose_all_button)

        self.character = QLabel()
        self.character.setAccessibleName("Character needing a voice choice")
        self.character.setWordWrap(True)
        self.portrait_image = QLabel()
        self.portrait_image.setAccessibleName("Exact game character portrait")
        self.portrait_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.portrait_image.setVisible(False)
        self.scope = QLabel()
        self.scope.setAccessibleName("Voice choice scope")
        self.scope.setWordWrap(True)
        self.engine = QLabel()
        self.engine.setWordWrap(True)
        self.engine.setAccessibleName("Voice preview engine and model")
        self.runtime = QLabel()
        self.runtime.setWordWrap(True)
        self.runtime.setTextFormat(Qt.TextFormat.PlainText)
        self.runtime.setAccessibleName("Character voice preview compute device")
        self.question = QLabel()
        self.question.setAccessibleName("Voice verification question")
        self.question.setWordWrap(True)
        self.sample = QLabel()
        self.sample.setAccessibleName("Voice preview phrase")
        self.sample.setWordWrap(True)
        self.another_sample_button = QPushButton("Try another phrase")
        self.another_sample_button.clicked.connect(self.try_another_phrase)
        self.another_sample_button.setVisible(False)

        self.a_box = QGroupBox()
        self.a_box.setAccessibleName("Voice sample")
        self.a_title = QLabel()
        self.a_title.setAccessibleName("Voice sample heading")
        self.a_title.setWordWrap(True)
        self.a_reason = QLabel()
        self.a_reason.setAccessibleName("Voice sample details")
        self.a_reason.setWordWrap(True)
        self.a_play = QPushButton("Play generated preview")
        self.a_play.setAccessibleName("Play generated voice preview")
        self.a_play.clicked.connect(self.play_a)
        self.a_original = QPushButton("Play original")
        self.a_original.setAccessibleName("Play original reference")
        self.a_original.clicked.connect(self._play_original)
        self.a_use = QPushButton("Accept voice")
        self.a_use.setAccessibleName("Accept verified character voice")
        self.a_use.clicked.connect(self.use_a)
        candidate_actions = QHBoxLayout()
        candidate_actions.addWidget(self.a_original)
        candidate_actions.addWidget(self.a_play)
        candidate_actions.addWidget(self.a_use)
        candidate_layout = QVBoxLayout(self.a_box)
        candidate_layout.addWidget(self.a_title)
        candidate_layout.addWidget(self.a_reason)
        candidate_layout.addLayout(candidate_actions)

        self.neither_button = QPushButton("Try another voice")
        self.neither_button.setAccessibleName("Try another character voice")
        self.neither_button.clicked.connect(self.neither)
        self.auto_button = QPushButton("Choose for me")
        self.auto_button.setToolTip("Opt out of verifying this sample yourself")
        self.auto_button.clicked.connect(self.choose_for_me)
        self.retry_save_button = QPushButton("Retry saving choices")
        self.retry_save_button.clicked.connect(self._start_save)
        self.retry_save_button.setVisible(False)
        outcomes = QHBoxLayout()
        outcomes.addWidget(self.neither_button)
        outcomes.addWidget(self.auto_button)
        outcomes.addWidget(self.retry_save_button)

        self.status = QLabel()
        self.status.setAccessibleName("Voice preview status")
        self.status.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addLayout(summary_row)
        layout.addWidget(self.portrait_image)
        layout.addWidget(self.character)
        layout.addWidget(self.scope)
        layout.addWidget(self.engine)
        layout.addWidget(self.runtime)
        layout.addWidget(self.question)
        layout.addWidget(self.sample)
        layout.addWidget(self.another_sample_button)
        layout.addWidget(self.a_box)
        layout.addLayout(outcomes)
        layout.addWidget(self.status)
        self.runtime_timer = QTimer(self)
        self.runtime_timer.setInterval(500)
        self.runtime_timer.timeout.connect(self._refresh_runtime)
        self.preview_runner.activeChanged.connect(self._refresh_runtime)
        self._refresh_runtime()
        self.setVisible(False)

    def _refresh_runtime(self):
        if self.preview_runner.active and self._shutdown_requested:
            message = "Stopping voice preview preparation..."
        elif self.preview_runner.active:
            message = "Preparing this voice preview. " + speech_runtime_label(
                getattr(self.preview_service, "backend", None)
            )
        else:
            message = "No preview generation. Saved previews play without TTS."
        self.runtime.setText(message)

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
            raise VoiceAuditionUIError("An unresolved voice must have a candidate")
        if self._shutdown_requested and self._owns_preview_service:
            self.preview_service = VoiceAuditionPreviewService()
        self._plan = plan
        self.engine.setText(
            engine_model_label(
                plan.synthesis_backend,
                plan.synthesis_model,
                pocket_cloning=plan.pocket_voice_cloning,
            )
        )
        self._groups = groups
        self._group_index = 0
        self._pending_decisions = []
        self._save_succeeded = False
        self._cancel_requested = False
        self._terminal_emitted = False
        self._ignore_preview_result = False
        self._shutdown_requested = False
        self.retry_save_button.setVisible(False)
        self.runtime_timer.start()
        self.setVisible(True)
        self._show_group()

    def current_group(self):
        return self._groups[self._group_index]

    def play_a(self):
        if self.preview_runner.active or self.decision_runner.active:
            return
        candidate, _choice, _narrator = self._current_entry()
        preview = self._previews.get(self._preview_key(candidate))
        if preview is not None:
            self._displayed = ((candidate, preview, self._current_entry()[1]),)
            self._set_decision_actions(True)
            self._play_preview(preview)
            return
        self._stop_player()
        self._displayed = ()
        self._set_decision_actions(False)
        self.status.setText("Preparing this generated preview...")
        self.preview_runner.start(
            self.preview_service.generate,
            self._plan,
            self.current_group(),
            candidate.source_id,
            text=self._sample_text,
        )

    def use_a(self):
        if not self._displayed:
            return
        _candidate, _preview, choice = self._displayed[0]
        self._record_choice(choice)

    def choose_for_me(self):
        if self.preview_runner.active or self.decision_runner.active:
            return
        group = self.current_group()
        source_id = (
            group.candidates[0].source_id
            if group.candidates
            else default_voice_choice_id
        )
        self._record_choice(source_id)

    def choose_all_automatically(self):
        if self.decision_runner.active or self._cancel_requested:
            return
        for group in self._groups[self._group_index :]:
            source_id = (
                group.candidates[0].source_id
                if group.candidates
                else default_voice_choice_id
            )
            self._pending_decisions.append((group, source_id))
        self._group_index = len(self._groups)
        self._ignore_preview_result = self.preview_runner.active
        if self.preview_runner.active:
            self.preview_service.cancel()
        self._stop_player()
        self._start_save(
            "VNTTS selected the recommended voice for every remaining character."
        )

    def neither(self):
        if self.preview_runner.active or self.decision_runner.active:
            return
        if len(self._candidate_entries) <= 1:
            self.status.setText(
                "This is the only available sample. Retry it or choose automatically."
            )
            return
        self._candidate_offset = (self._candidate_offset + 1) % len(
            self._candidate_entries
        )
        self._show_current_candidate()

    def try_another_phrase(self):
        group = self.current_group()
        if (
            self.preview_runner.active
            or self.decision_runner.active
            or self._alternate_active
            or group.alternate_sample_text is None
        ):
            return
        self._alternate_active = True
        self._sample_text = group.alternate_sample_text
        self.sample.setText(f'Generated preview says: "{self._sample_text}"')
        self.another_sample_button.setEnabled(False)
        self._show_current_candidate()

    def cancel(self):
        if self._terminal_emitted:
            return
        self._cancel_requested = True
        self._stop_player()
        self.preview_service.cancel()
        self._set_decision_actions(False)
        self.choose_all_button.setEnabled(False)
        self.status.setText("Cancelling voice selection...")
        if not self.active:
            self._emit_cancelled()

    def shutdown(self):
        self._stop_player()
        self._shutdown_requested = True
        self.runtime_timer.stop()
        self._refresh_runtime()
        if self.preview_runner.active:
            self.preview_service.cancel()
            return
        if not self.active:
            self.preview_service.close()

    def _show_group(self):
        self._candidate_offset = 0
        self._previews = {}
        self._displayed = ()
        self._alternate_active = False
        group = self.current_group()
        entries = [
            (candidate, candidate.source_id, False) for candidate in group.candidates
        ]
        if group.narrator_candidate is not None:
            entries.append((group.narrator_candidate, default_voice_choice_id, True))
        self._candidate_entries = tuple(entries)
        self.summary.setText(
            f"Voice sample {self._group_index + 1} of {len(self._groups)}."
        )
        variant = f" ({group.age})" if group.age else ""
        self.character.setText(f"Choose a voice for {group.character}{variant}")
        self._show_portrait(group)
        count = len(group.line_ids)
        self.scope.setText(
            f"This voice will be used for {count} line{'s' if count != 1 else ''} "
            "in this selection and remembered for this character variant."
        )
        self.question.setText(
            "Listen to the original reference and accept it if suitable for voice "
            "generation. You can also generate a preview before deciding."
        )
        self._sample_text = group.sample_text
        self.sample.setText(f'Generated preview says: "{self._sample_text}"')
        self.another_sample_button.setVisible(group.alternate_sample_text is not None)
        self.another_sample_button.setEnabled(group.alternate_sample_text is not None)
        self.choose_all_button.setEnabled(True)
        self._show_current_candidate()

    def _current_entry(self):
        return self._candidate_entries[self._candidate_offset]

    def _show_current_candidate(self):
        self._stop_player()
        self._displayed = ()
        candidate, _choice, narrator = self._current_entry()
        self.a_title.setText(
            "Narrator fallback" if narrator else f"Voice: {candidate.source_character}"
        )
        self.a_reason.setText(
            f"Reference voice: {candidate.source_character} ({candidate.source_speaker})\n"
            + candidate.recommendation
        )
        preview = self._previews.get(self._preview_key(candidate))
        self.a_play.setText("Play generated preview")
        self.a_play.setEnabled(True)
        self.a_original.setEnabled(bool(candidate.reference_sha256s))
        self.a_original.setToolTip(
            "Listen to the original reference used for this voice."
            if candidate.reference_sha256s
            else "This voice has no recorded reference."
        )
        self.a_use.setEnabled(preview is not None)
        self.a_box.setVisible(True)
        self.neither_button.setText(
            "Try another voice"
            if len(self._candidate_entries) > 1
            else "Retry this voice"
        )
        self.neither_button.setEnabled(len(self._candidate_entries) > 1)
        self.auto_button.setEnabled(True)
        if preview is not None:
            self._displayed = ((candidate, preview, self._current_entry()[1]),)
            self.status.setText("Replay the preview or accept this verified voice.")
        else:
            self.status.setText(
                "Listen to the reference or generate a preview, then accept if suitable."
            )

    def _preview_finished(self, preview, error):
        if self._shutdown_requested:
            if not self.active:
                self.preview_service.close()
            return
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if self._ignore_preview_result:
            self._ignore_preview_result = False
            self._maybe_complete()
            return
        candidate, choice, _narrator = self._current_entry()
        if error is not None:
            self.a_play.setEnabled(True)
            self.a_original.setEnabled(bool(candidate.reference_sha256s))
            self.a_use.setEnabled(False)
            self.neither_button.setEnabled(len(self._candidate_entries) > 1)
            self.auto_button.setEnabled(True)
            self.another_sample_button.setEnabled(
                not self._alternate_active
                and self.current_group().alternate_sample_text is not None
            )
            self.status.setText(
                f"Could not prepare {candidate.source_character}'s preview: {error}. "
                "Try another voice, retry this sample, or choose automatically."
            )
            return
        self._previews[self._preview_key(candidate)] = preview
        self._displayed = ((candidate, preview, choice),)
        self._set_decision_actions(True)
        self._play_preview(preview)
        self.status.setText(
            "Playing the generated preview. Accept this voice only if the sample is suitable."
        )

    def _play_preview(self, preview):
        player = self._ensure_player()
        player.stop()
        player.setSource(QUrl.fromLocalFile(str(preview.path)))
        player.play()

    def _preview_key(self, candidate):
        return candidate.source_id, self._sample_text

    def _play_original(self):
        if self._cancel_requested or self._shutdown_requested or self.active:
            return
        candidate, _choice, _narrator = self._current_entry()
        self._stop_player()
        self._displayed = ()
        self.a_use.setEnabled(False)
        try:
            reference = self.preview_service.reference_audio(
                self._plan, self.current_group(), candidate.source_id
            )
        except (OSError, ValueError, VoiceAuditionError) as error:
            self.status.setText(f"Unable to play original reference: {error}")
            return
        if reference is None:
            self.status.setText("This voice has no recorded reference.")
            return
        player = self._ensure_player()
        player.setSource(QUrl.fromLocalFile(str(reference)))
        player.play()
        self._displayed = ((candidate, None, self._current_entry()[1]),)
        self.a_use.setEnabled(True)
        self.status.setText(
            f"Playing the original reference for {candidate.source_character}."
        )

    def _record_choice(self, source_id, status_message=None):
        if self._group_index >= len(self._groups) or self.decision_runner.active:
            return
        self._stop_player()
        self._pending_decisions.append((self.current_group(), source_id))
        self._group_index += 1
        if self._group_index < len(self._groups):
            self._show_group()
            return
        self._start_save(status_message)

    def _start_save(self, status_message=None):
        if self.decision_runner.active or not self._pending_decisions:
            return
        self.retry_save_button.setVisible(False)
        self.a_box.setVisible(False)
        self._set_decision_actions(False)
        self.choose_all_button.setEnabled(False)
        self.status.setText(
            f"{status_message} Saving in the background..."
            if status_message
            else "Saving voice choices in the background..."
        )
        self.decision_runner.start(
            self.decisions.remember_many,
            tuple(self._pending_decisions),
        )

    def _decision_finished(self, _result, error):
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if error is not None:
            self.retry_save_button.setVisible(True)
            self.retry_save_button.setEnabled(True)
            self.status.setText(
                "Unable to save the voice choices. Nothing was lost; retry when "
                "the application data directory is writable."
            )
            return
        self._save_succeeded = True
        self._maybe_complete()

    def _maybe_complete(self):
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if not self._save_succeeded or self.active:
            return
        self._terminal_emitted = True
        self.runtime_timer.stop()
        self.setVisible(False)
        self.completed.emit()

    def _set_decision_actions(self, enabled):
        enabled = bool(enabled) and not self._cancel_requested
        candidate = self._current_entry()[0] if self._candidate_entries else None
        self.a_play.setEnabled(enabled and self.a_box.isVisible())
        self.a_use.setEnabled(enabled and bool(self._displayed))
        self.a_original.setEnabled(
            enabled
            and self.a_box.isVisible()
            and candidate is not None
            and bool(candidate.reference_sha256s)
        )
        self.neither_button.setEnabled(enabled and len(self._candidate_entries) > 1)
        self.auto_button.setEnabled(enabled)
        group = self.current_group() if self._group_index < len(self._groups) else None
        self.another_sample_button.setEnabled(
            enabled
            and group is not None
            and group.alternate_sample_text is not None
            and not self._alternate_active
        )

    def _show_portrait(self, group):
        self.portrait_image.clear()
        self.portrait_image.setVisible(False)
        if not group.portrait_image or not group.portrait_image_sha256:
            return
        try:
            if sha256_file(group.portrait_image) != group.portrait_image_sha256:
                return
        except OSError:
            return
        pixmap = QPixmap(group.portrait_image)
        if pixmap.isNull():
            return
        self.portrait_image.setPixmap(
            pixmap.scaled(
                180,
                180,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.portrait_image.setVisible(True)

    def _emit_cancelled(self):
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        self._shutdown_requested = True
        self.runtime_timer.stop()
        self._refresh_runtime()
        self.setVisible(False)
        self.preview_service.close()
        self.cancelled.emit()

    def _ensure_player(self):
        if self.player is None:
            self.player = QMediaPlayer(self)
        return self.player

    def _stop_player(self):
        if self.player is not None:
            self.player.stop()


__all__ = ["VoiceAuditionPanel", "VoiceAuditionUIError"]

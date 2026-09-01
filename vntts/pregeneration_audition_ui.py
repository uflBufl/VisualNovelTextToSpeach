"""Informed, bounded voice comparisons for self-service pregeneration."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)
from vntts_artifacts.file_integrity import sha256_file

from vntts.async_ui import LatestTaskRunner
from vntts.pregeneration_audition import (
    VoiceAuditionCancelled,
    VoiceAuditionPreviewService,
)
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
        super().__init__("Choose character voices", parent)
        self.decisions = decisions
        self.preview_service = preview_service or VoiceAuditionPreviewService()
        self.preview_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.preview_runner.finished.connect(self._previews_finished)
        self.prefetch_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.prefetch_runner.finished.connect(self._prefetch_finished)
        self.decision_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.decision_runner.finished.connect(self._decision_finished)
        self.audio_output = None
        self.player = player
        self._plan = None
        self._groups = ()
        self._group_index = 0
        self._candidate_offset = 0
        self._previews = {}
        self._anchor_path = None
        self._viable_candidates = ()
        self._displayed = ()
        self._pending_decisions = []
        self._save_succeeded = False
        self._cancel_requested = False
        self._terminal_emitted = False
        self._ignore_preview_result = False
        self._ignore_prefetch_result = False
        self._prefetch_group_id = None
        self._prefetched = {}
        self._shutdown_requested = False
        self._loading_narrator = False
        self._narrator_companion = None
        self._alternate_active = False

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
        self.question = QLabel()
        self.question.setAccessibleName("Voice comparison question")
        self.question.setWordWrap(True)
        self.anchor_button = QPushButton("Play original game voice")
        self.anchor_button.setAccessibleName("Play original game voice anchor")
        self.anchor_button.clicked.connect(self.play_anchor)
        self.anchor_button.setVisible(False)
        self.sample = QLabel()
        self.sample.setAccessibleName("Voice preview phrase")
        self.sample.setWordWrap(True)
        self.another_sample_button = QPushButton("Try another phrase")
        self.another_sample_button.clicked.connect(self.try_another_phrase)
        self.another_sample_button.setVisible(False)

        self.a_box, self.a_title, self.a_reason, self.a_play, self.a_use = (
            self._candidate_box("Voice A", 0)
        )
        self.b_box, self.b_title, self.b_reason, self.b_play, self.b_use = (
            self._candidate_box("Voice B", 1)
        )
        comparison = QHBoxLayout()
        comparison.addWidget(self.a_box, 1)
        comparison.addWidget(self.b_box, 1)

        self.neither_button = QPushButton("Neither sounds right")
        self.neither_button.clicked.connect(self.neither)
        self.auto_button = QPushButton("Choose for me")
        self.auto_button.clicked.connect(self.choose_for_me)
        self.retry_save_button = QPushButton("Retry saving choices")
        self.retry_save_button.clicked.connect(lambda: self._start_save())
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
        layout.addWidget(self.question)
        layout.addWidget(self.anchor_button)
        layout.addWidget(self.sample)
        layout.addWidget(self.another_sample_button)
        layout.addLayout(comparison)
        layout.addLayout(outcomes)
        layout.addWidget(self.status)
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
        if any(len(group.candidates) < 2 for group in groups):
            raise VoiceAuditionUIError(
                "An unresolved voice must have at least two candidates"
            )
        self._plan = plan
        self._groups = groups
        self._group_index = 0
        self._pending_decisions = []
        self._save_succeeded = False
        self._cancel_requested = False
        self._terminal_emitted = False
        self._ignore_preview_result = False
        self._ignore_prefetch_result = False
        self._prefetch_group_id = None
        self._prefetched = {}
        self._shutdown_requested = False
        self._loading_narrator = False
        self._narrator_companion = None
        self._alternate_active = False
        self.retry_save_button.setVisible(False)
        self.setVisible(True)
        self._show_group()

    def current_group(self):
        return self._groups[self._group_index]

    def play_a(self):
        self._play_slot(0)

    def play_b(self):
        self._play_slot(1)

    def play_anchor(self):
        if self._anchor_path is None:
            return
        player = self._ensure_player()
        player.stop()
        player.setSource(QUrl.fromLocalFile(str(self._anchor_path)))
        player.play()
        self.status.setText(
            "Playing the verified original game voice. Candidate controls remain available."
        )

    def use_a(self):
        self._use_slot(0)

    def use_b(self):
        self._use_slot(1)

    def choose_for_me(self):
        if self.preview_runner.active or self.decision_runner.active:
            return
        source_id = (
            self._displayed[0][2] if self._displayed else default_voice_choice_id
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
        self._ignore_prefetch_result = self.prefetch_runner.active
        if self.preview_runner.active or self.prefetch_runner.active:
            self.preview_service.cancel()
        self._stop_player()
        self._start_save(
            "VNTTS selected the recommended voice for every remaining character."
        )

    def neither(self):
        if self.preview_runner.active or self.decision_runner.active:
            return
        next_offset = self._candidate_offset + 2
        if next_offset < len(self._viable_candidates):
            self._candidate_offset = next_offset
            self._show_candidate_pair()
            return
        self._show_narrator_fallback()

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
        self.sample.setText(f'Both voices say: "{group.alternate_sample_text}"')
        self.another_sample_button.setEnabled(False)
        self._hide_candidate_boxes()
        self._set_decision_actions(False)
        self.status.setText("Preparing the same voices with another phrase...")
        self.preview_runner.start(
            self._generate_candidate_previews,
            self._plan,
            group,
            group.candidates,
            group.alternate_sample_text,
        )

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
        if self.prefetch_runner.active:
            self.preview_service.cancel()
            return
        if not self.active:
            self.preview_service.close()

    def _candidate_box(self, title, slot):
        box = QGroupBox()
        box.setAccessibleName(title)
        heading = QLabel(title)
        heading.setAccessibleName(f"{title} heading")
        heading.setWordWrap(True)
        reason = QLabel()
        reason.setAccessibleName(f"{title} recommendation")
        reason.setWordWrap(True)
        play = QPushButton(f"Play {title}")
        use = QPushButton(f"Use {title}")
        play.clicked.connect(self.play_a if slot == 0 else self.play_b)
        use.clicked.connect(self.use_a if slot == 0 else self.use_b)
        actions = QHBoxLayout()
        actions.addWidget(play)
        actions.addWidget(use)
        layout = QVBoxLayout(box)
        layout.addWidget(heading)
        layout.addWidget(reason)
        layout.addLayout(actions)
        return box, heading, reason, play, use

    def _show_group(self):
        self._candidate_offset = 0
        self._previews = {}
        self._anchor_path = None
        self._viable_candidates = ()
        self._displayed = ()
        self._loading_narrator = False
        self._narrator_companion = None
        self._alternate_active = False
        group = self.current_group()
        remaining = len(self._groups) - self._group_index
        estimate = max(15, remaining * 15)
        self.summary.setText(
            f"Voice choice {self._group_index + 1} of {len(self._groups)}. "
            f"About {estimate} seconds remain."
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
            "No verified original voice anchor is available here. Compare the "
            "same generated line and choose the voice you prefer, or let VNTTS decide."
        )
        self.anchor_button.setVisible(False)
        self.sample.setText(f'Both voices say: "{group.sample_text}"')
        self.another_sample_button.setVisible(group.alternate_sample_text is not None)
        self.another_sample_button.setEnabled(False)
        self._hide_candidate_boxes()
        self._set_decision_actions(False)
        self.choose_all_button.setEnabled(True)
        self.status.setText("Preparing a short A/B comparison...")
        prefetched = self._prefetched.pop(group.group_id, None)
        if prefetched is not None:
            self._previews_finished(*prefetched)
            return
        if self.prefetch_runner.active and self._prefetch_group_id == group.group_id:
            return
        self.preview_runner.start(
            self._generate_candidate_previews,
            self._plan,
            group,
            group.candidates,
        )

    def _generate_candidate_previews(self, plan, group, candidates, text=None):
        results = []
        for candidate in candidates:
            try:
                preview = self.preview_service.generate(
                    plan,
                    group,
                    candidate.source_id,
                    **({} if text is None else {"text": text}),
                )
            except VoiceAuditionCancelled:
                raise
            except Exception as error:
                results.append((candidate.source_id, None, str(error)))
            else:
                results.append((candidate.source_id, preview, None))
        anchor = None
        if group.anchor_source_id is not None:
            try:
                anchor = self.preview_service.reference_audio(
                    plan,
                    group,
                    group.anchor_source_id,
                )
            except Exception:
                anchor = None
        return tuple(results), anchor

    def _previews_finished(self, results, error):
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if self._ignore_preview_result:
            self._ignore_preview_result = False
            self._maybe_complete()
            return
        if self._loading_narrator:
            self._narrator_preview_finished(results, error)
            return
        group = self.current_group()
        if error is not None:
            message = (
                "Voice previews could not be prepared. VNTTS selected the safest "
                "available fallback so offline preparation can continue."
            )
            self.status.setText(message)
            self._record_choice(self._automatic_fallback_source(group), message)
            return
        results, self._anchor_path = results
        if self._anchor_path is not None:
            self.question.setText(
                "Play the verified original game voice, then choose which generated "
                "candidate sounds closer to it."
            )
            self.anchor_button.setVisible(True)
            self.anchor_button.setEnabled(True)
        self._previews = {
            source_id: preview
            for source_id, preview, _message in results
            if preview is not None
        }
        self._viable_candidates = tuple(
            candidate
            for candidate in group.candidates
            if candidate.source_id in self._previews
        )
        if len(self._viable_candidates) == 0:
            message = (
                "None of the candidate previews worked. VNTTS selected the safest "
                "available fallback automatically."
            )
            self.status.setText(message)
            self._record_choice(self._automatic_fallback_source(group), message)
            return
        if len(self._viable_candidates) == 1:
            message = (
                "Only one candidate produced a usable preview. Using it "
                "automatically; no confirmation is needed."
            )
            self.status.setText(message)
            self._record_choice(self._viable_candidates[0].source_id, message)
            return
        self._show_candidate_pair()

    def _show_candidate_pair(self):
        group = self.current_group()
        pair = self._viable_candidates[
            self._candidate_offset : self._candidate_offset + 2
        ]
        if len(pair) == 1:
            self._show_narrator_fallback(companion=pair[0])
            return
        self._displayed = tuple(
            (candidate, self._previews[candidate.source_id], candidate.source_id)
            for candidate in pair
        )
        self._render_slot(
            0, self._displayed[0], recommended=self._candidate_offset == 0
        )
        self._render_slot(1, self._displayed[1], recommended=False)
        self.neither_button.setText("Neither sounds right")
        self.neither_button.setEnabled(True)
        self.auto_button.setEnabled(True)
        self.choose_all_button.setEnabled(True)
        self.another_sample_button.setEnabled(
            group.alternate_sample_text is not None and not self._alternate_active
        )
        self.status.setText(
            "Play either voice in any order. Playback does not disable your choices."
        )
        self._prefetch_next_group()

    def _prefetch_next_group(self):
        next_index = self._group_index + 1
        if next_index >= len(self._groups) or self.prefetch_runner.active:
            return
        group = self._groups[next_index]
        self._prefetch_group_id = group.group_id
        self.prefetch_runner.start(
            self._generate_candidate_previews,
            self._plan,
            group,
            group.candidates,
        )

    def _prefetch_finished(self, result, error):
        group_id = self._prefetch_group_id
        self._prefetch_group_id = None
        if self._shutdown_requested:
            self.preview_service.close()
            return
        if self._terminal_emitted:
            self.preview_service.close()
            return
        if self._ignore_prefetch_result:
            self._ignore_prefetch_result = False
            self._maybe_complete()
            return
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if self._group_index < len(self._groups) and (
            self.current_group().group_id == group_id
        ):
            self._previews_finished(result, error)
            return
        self._prefetched[group_id] = (result, error)

    def _show_narrator_fallback(self, *, companion=None):
        group = self.current_group()
        candidate = group.narrator_candidate
        if candidate is None:
            message = (
                "No narrator voice is available. VNTTS will use the best remaining "
                "candidate so preparation can continue."
            )
            self.status.setText(message)
            self._record_choice(self._automatic_fallback_source(group), message)
            return
        self._loading_narrator = True
        self._narrator_companion = companion
        self.another_sample_button.setEnabled(False)
        self._hide_candidate_boxes()
        self._set_decision_actions(False)
        self.status.setText("Preparing the narrator fallback preview...")
        self.preview_runner.start(
            self._generate_narrator_preview,
            self._plan,
            group,
            candidate,
            (
                group.alternate_sample_text
                if self._alternate_active
                else group.sample_text
            ),
        )

    def _generate_narrator_preview(self, plan, group, candidate, text):
        preview = self.preview_service.generate(
            plan, group, candidate.source_id, text=text
        )
        return candidate, preview

    def _narrator_preview_finished(self, result, error):
        self._loading_narrator = False
        companion = self._narrator_companion
        self._narrator_companion = None
        if error is not None:
            if companion is None:
                self._record_choice(default_voice_choice_id)
                return
            self._displayed = (
                (
                    companion,
                    self._previews[companion.source_id],
                    companion.source_id,
                ),
            )
            self._render_slot(0, self._displayed[0], recommended=True)
            self.b_box.setVisible(False)
            self.neither_button.setText("Use narrator without preview")
            self.neither_button.setEnabled(True)
            self.auto_button.setEnabled(True)
            self.status.setText(
                "The narrator preview failed. You can use the remaining voice or "
                "continue with the configured narrator."
            )
            return
        narrator, preview = result
        displayed = []
        if companion is not None:
            displayed.append(
                (
                    companion,
                    self._previews[companion.source_id],
                    companion.source_id,
                )
            )
        displayed.append((narrator, preview, default_voice_choice_id))
        self._displayed = tuple(displayed)
        self._render_slot(0, self._displayed[0], recommended=companion is not None)
        if len(self._displayed) == 2:
            self._render_slot(1, self._displayed[1], recommended=False)
        else:
            self.a_title.setText("Narrator fallback")
            self.a_reason.setText("Keeps every line available offline")
            self.b_box.setVisible(False)
        self.neither_button.setEnabled(False)
        self.auto_button.setEnabled(True)
        self.status.setText(
            "The narrator is the final offline fallback. Choose it, choose the "
            "remaining voice, or let VNTTS decide."
        )

    def _render_slot(self, slot, value, *, recommended):
        candidate, preview, _choice = value
        box, title, reason, play, use = self._slot_widgets(slot)
        label = f"Voice {'A' if slot == 0 else 'B'}"
        if recommended:
            label += " - Recommended"
        title.setText(label)
        reason.setText(candidate.recommendation)
        play.setText(f"Play {'A' if slot == 0 else 'B'}")
        play.setEnabled(preview is not None)
        use.setText(f"Use {'A' if slot == 0 else 'B'}")
        use.setEnabled(preview is not None)
        box.setVisible(True)

    def _slot_widgets(self, slot):
        if slot == 0:
            return self.a_box, self.a_title, self.a_reason, self.a_play, self.a_use
        return self.b_box, self.b_title, self.b_reason, self.b_play, self.b_use

    def _hide_candidate_boxes(self):
        self.a_box.setVisible(False)
        self.b_box.setVisible(False)

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

    def _play_slot(self, slot):
        if slot >= len(self._displayed):
            return
        _candidate, preview, _choice = self._displayed[slot]
        if preview is None:
            return
        player = self._ensure_player()
        player.stop()
        player.setSource(QUrl.fromLocalFile(str(preview.path)))
        player.play()
        self.status.setText(
            f"Playing voice {'A' if slot == 0 else 'B'}. You can replay or choose now."
        )

    def _use_slot(self, slot):
        if slot >= len(self._displayed):
            return
        _candidate, preview, choice = self._displayed[slot]
        if preview is None:
            return
        self._record_choice(choice)

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

    def _automatic_fallback_source(self, group):
        if group.narrator_candidate is not None:
            return default_voice_choice_id
        if self._viable_candidates:
            return self._viable_candidates[0].source_id
        return group.candidates[0].source_id

    def _start_save(self, status_message=None):
        if self.decision_runner.active or not self._pending_decisions:
            return
        self.retry_save_button.setVisible(False)
        self._hide_candidate_boxes()
        self.anchor_button.setVisible(False)
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
        self.setVisible(False)
        self.completed.emit()

    def _set_decision_actions(self, enabled):
        enabled = bool(enabled) and not self._cancel_requested
        self.a_play.setEnabled(enabled and self.a_box.isVisible())
        self.a_use.setEnabled(enabled and self.a_box.isVisible())
        self.b_play.setEnabled(enabled and self.b_box.isVisible())
        self.b_use.setEnabled(enabled and self.b_box.isVisible())
        self.neither_button.setEnabled(enabled)
        self.auto_button.setEnabled(enabled)

    def _emit_cancelled(self):
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        self.setVisible(False)
        if not self.prefetch_runner.active:
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

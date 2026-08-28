"""Qt review surface for blind missing-voice reuse evidence."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QCloseEvent, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from vntts.async_ui import LatestTaskRunner
from vntts.authoring.missing_voice_reuse_review import (
    AUTOMATIC_UNRESOLVED_ORIGIN,
    load_missing_voice_reuse_review,
    missing_voice_reuse_review_progress,
    record_missing_voice_reuse_decision,
    record_missing_voice_reuse_heard,
)


class MissingVoiceReuseReviewDialog(QDialog):
    """Review exact cohort samples while keeping failed arms visible."""

    def __init__(
        self,
        session_path,
        parent=None,
        *,
        thread_pool=None,
        heard_recorder=record_missing_voice_reuse_heard,
        decision_recorder=record_missing_voice_reuse_decision,
    ):
        super().__init__(parent)
        self.session_path = Path(session_path).expanduser().resolve()
        self.heard_recorder = heard_recorder
        self.decision_recorder = decision_recorder
        self.bundle, self.session = load_missing_voice_reuse_review(self.session_path)
        self.heard_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.heard_runner.finished.connect(self._heard_saved)
        self.decision_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.decision_runner.finished.connect(self._decision_saved)
        self._pending_heard = []
        self._saving_heard = None
        self._playing_key = None
        self._cohort = None
        self._sample_index = 0
        self._close_pending = False

        self.failed_control_mode = self.bundle.get("target_mode") == "failed"
        self.setWindowTitle(
            "Blind failed-line fallback review"
            if self.failed_control_mode
            else "Blind missing-voice reuse review"
        )
        self.setMinimumSize(820, 520)
        self.resize(1050, 650)

        self.progress = QLabel()
        self.progress.setAccessibleName("Missing voice review progress")
        instructions = (
            "The original production route failed its technical gate and has no "
            "playable WAV. Hear every available opaque fallback sample. Choose the "
            "fallback only if it completed every required sample and sounds "
            "acceptable; otherwise keep the exact lines unresolved."
            if self.failed_control_mode
            else "Compare opaque voices only within this family. Failed renders stay "
            "visible and cannot be selected. Finish every available sample, then "
            "choose one complete voice or Neither."
        )
        self.instructions = QLabel(instructions)
        self.instructions.setWordWrap(True)
        self.cohort_heading = QLabel()
        self.cohort_heading.setWordWrap(True)
        self.cohort_heading.setAccessibleName("Current missing voice family")

        self.previous = QPushButton("Previous sample")
        self.sample_selector = QComboBox()
        self.next = QPushButton("Next sample")
        self.previous.setShortcut(QKeySequence("Alt+Left"))
        self.next.setShortcut(QKeySequence("Alt+Right"))
        self.previous.clicked.connect(lambda: self._move_sample(-1))
        self.next.clicked.connect(lambda: self._move_sample(1))
        self.sample_selector.currentIndexChanged.connect(self._select_sample)
        navigation = QHBoxLayout()
        navigation.addWidget(self.previous)
        navigation.addWidget(self.sample_selector, 1)
        navigation.addWidget(self.next)

        self.sample_text = QLabel()
        self.sample_text.setWordWrap(True)
        self.sample_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.sample_text.setMinimumHeight(80)
        sample_layout = QVBoxLayout()
        sample_layout.addLayout(navigation)
        sample_layout.addWidget(self.sample_text)
        sample_box = QGroupBox("Current exact sample")
        sample_box.setLayout(sample_layout)

        self.play_grid = QGridLayout()
        self.play_buttons = {}
        self.arm_statuses = {}
        for column, candidate in enumerate(self.bundle["candidates"]):
            label = candidate["label"]
            button = QPushButton(f"Play {label}")
            button.setMinimumWidth(180)
            button.setShortcut(QKeySequence(f"Ctrl+{column + 1}"))
            button.clicked.connect(
                lambda _checked=False, value=label: self._play(value)
            )
            status = QLabel()
            status.setWordWrap(True)
            status.setAlignment(Qt.AlignmentFlag.AlignTop)
            self.play_grid.addWidget(button, 0, column)
            self.play_grid.addWidget(status, 1, column)
            self.play_buttons[label] = button
            self.arm_statuses[label] = status
        playback_box = QGroupBox("Opaque candidate evidence")
        playback_box.setLayout(self.play_grid)

        self.now_playing = QLabel("READY")
        self.now_playing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.now_playing.setMinimumHeight(42)
        self.now_playing.setStyleSheet(
            "QLabel { background-color: #3f3f46; color: white; "
            "font-weight: 700; border-radius: 5px; padding: 7px; }"
        )
        self.stop = QPushButton("Stop audio")
        self.stop.setShortcut(QKeySequence("Ctrl+Space"))
        self.stop.clicked.connect(self._stop)
        playback_controls = QHBoxLayout()
        playback_controls.addWidget(self.now_playing, 1)
        playback_controls.addWidget(self.stop)

        self.decision_reason = QLabel()
        self.decision_reason.setWordWrap(True)
        self.decision_buttons = {}
        decisions = QHBoxLayout()
        for candidate in self.bundle["candidates"]:
            label = candidate["label"]
            button = QPushButton(
                f"Use fallback {label} for these lines"
                if self.failed_control_mode
                else f"Choose {label} for this family"
            )
            button.clicked.connect(
                lambda _checked=False, value=label: self._save_decision(value)
            )
            decisions.addWidget(button)
            self.decision_buttons[label] = button
        self.neither = QPushButton(
            "Keep these lines unresolved"
            if self.failed_control_mode
            else "Neither voice is acceptable"
        )
        self.neither.setShortcut(QKeySequence("Alt+N"))
        self.neither.clicked.connect(lambda: self._save_decision("neither"))
        decisions.addWidget(self.neither)
        decision_layout = QVBoxLayout()
        decision_layout.addWidget(self.decision_reason)
        decision_layout.addLayout(decisions)
        decision_box = QGroupBox(
            "Failed-line decision" if self.failed_control_mode else "Family decision"
        )
        decision_box.setLayout(decision_layout)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Missing voice review operation status")
        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.progress)
        layout.addWidget(self.instructions)
        layout.addWidget(self.cohort_heading)
        layout.addWidget(sample_box)
        layout.addWidget(playback_box, 1)
        layout.addLayout(playback_controls)
        layout.addWidget(decision_box)
        layout.addWidget(self.status)
        layout.addWidget(close_buttons)

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(self._playback_error)
        self._load_next_cohort()

    def _load_next_cohort(self):
        self._stop()
        self.bundle, self.session = load_missing_voice_reuse_review(self.session_path)
        completed, total = missing_voice_reuse_review_progress(
            self.bundle, self.session
        )
        self.progress.setText(
            f"Completed {completed} of {total} families | Remaining {total - completed}"
        )
        decisions = {
            value["cohort_id"]: value["decision"] for value in self.session["decisions"]
        }
        self._cohort = next(
            (
                cohort
                for cohort in self.bundle["cohorts"]
                if decisions[cohort["cohort_id"]] is None
            ),
            None,
        )
        self._sample_index = 0
        self.sample_selector.blockSignals(True)
        self.sample_selector.clear()
        if self._cohort is None:
            automatic_count = sum(
                value.get("decision_origin") == AUTOMATIC_UNRESOLVED_ORIGIN
                for value in self.session["decisions"]
            )
            self.cohort_heading.setText("Review complete")
            if automatic_count:
                self.sample_text.setText(
                    f"{automatic_count} cohort(s) had no complete selectable candidate "
                    "and were kept unresolved automatically. No listening or human "
                    "confirmation is required."
                )
                self.status.setText(
                    "Any surviving WAV is optional diagnostic evidence only. "
                    "The automatic unresolved outcome is ready for decision import."
                )
            else:
                self.sample_text.setText("All exact families have a recorded decision.")
                self.status.setText(
                    "The blind key remains private until decision import."
                )
            self.sample_selector.blockSignals(False)
            self._set_all_actions(False)
            return
        for sample in self._cohort["samples"]:
            self.sample_selector.addItem(
                f"{sample['length_bucket'].title()} | {sample['line_id']}"
            )
        self.sample_selector.blockSignals(False)
        self.cohort_heading.setText(
            f"{'Failed-line group' if self.failed_control_mode else 'Family'} "
            f"{completed + 1} of {total} | "
            f"{self._cohort['sample_count']} required sample(s)"
        )
        self.status.setText(
            "Replay remains available while a family decision is saved in the background."
        )
        self._refresh_sample()

    def _select_sample(self, index):
        if self._cohort is None or index < 0:
            return
        self._stop()
        self._sample_index = index
        self._refresh_sample()

    def _move_sample(self, delta):
        if self._cohort is None:
            return
        index = max(
            0,
            min(len(self._cohort["samples"]) - 1, self._sample_index + delta),
        )
        self.sample_selector.setCurrentIndex(index)

    def _refresh_sample(self):
        if self._cohort is None:
            return
        sample = self._cohort["samples"][self._sample_index]
        self.sample_text.setText(sample["text"])
        heard = self._heard_keys()
        for candidate in self.bundle["candidates"]:
            label = candidate["label"]
            arm = next(
                value
                for value in candidate["samples"]
                if value["queue_id"] == sample["queue_id"]
            )
            button = self.play_buttons[label]
            if arm["status"] == "generated":
                was_heard = (sample["queue_id"], label) in heard
                button.setText(f"{'Replay' if was_heard else 'Play'} {label}")
                button.setEnabled(True)
                quality = arm.get("quality") or {}
                duration = quality.get("duration_seconds")
                duration_text = (
                    f"{float(duration):.2f}s"
                    if isinstance(duration, (int, float))
                    else "duration unknown"
                )
                repair = arm.get("repair_strategy") or "direct render"
                self.arm_statuses[label].setText(
                    f"AVAILABLE | {duration_text} | {repair}"
                )
            else:
                button.setText(f"{label} unavailable")
                button.setEnabled(False)
                self.arm_statuses[label].setText(
                    f"FAILED | {arm['failure_kind']} | attempts: {arm['attempt_count']}"
                )
        self.previous.setEnabled(self._sample_index > 0)
        self.next.setEnabled(self._sample_index + 1 < len(self._cohort["samples"]))
        self.stop.setEnabled(self._playing_key is not None)
        self._update_decisions()

    def _play(self, label):
        if self._cohort is None:
            return
        sample = self._cohort["samples"][self._sample_index]
        arm = next(
            value
            for candidate in self.bundle["candidates"]
            if candidate["label"] == label
            for value in candidate["samples"]
            if value["queue_id"] == sample["queue_id"]
        )
        if arm["status"] != "generated":
            return
        self._playing_key = (self._cohort["cohort_id"], sample["queue_id"], label)
        self.now_playing.setText(f"LOADING {label}")
        self.player.setSource(
            QUrl.fromLocalFile(str(self.session_path.parent / arm["audio"]))
        )
        self.player.play()

    def _stop(self):
        if hasattr(self, "player"):
            self.player.stop()
            self.player.setSource(QUrl())
        self._playing_key = None
        if hasattr(self, "now_playing"):
            self.now_playing.setText("READY")
        if hasattr(self, "stop"):
            self.stop.setEnabled(False)

    def _playback_state_changed(self, state):
        if (
            state == QMediaPlayer.PlaybackState.PlayingState
            and self._playing_key is not None
        ):
            self.now_playing.setText(f"PLAYING {self._playing_key[2]}")
            self.stop.setEnabled(True)

    def _media_status_changed(self, status):
        if status != QMediaPlayer.MediaStatus.EndOfMedia or self._playing_key is None:
            return
        key = self._playing_key
        self._playing_key = None
        self.now_playing.setText(f"FINISHED {key[2]}")
        self.stop.setEnabled(False)
        if key not in self._all_heard_records() and key not in self._pending_heard:
            self._pending_heard.append(key)
            self._start_next_heard_save()
        self._refresh_sample()

    def _start_next_heard_save(self):
        if self.heard_runner.active or not self._pending_heard:
            return
        self._saving_heard = self._pending_heard.pop(0)
        self.status.setText(
            "Saving heard evidence in background. Playback and replay remain available."
        )
        self.heard_runner.start(
            self.heard_recorder, self.session_path, *self._saving_heard
        )
        self._update_decisions()

    def _heard_saved(self, _result, error):
        saved = self._saving_heard
        self._saving_heard = None
        if error is not None:
            self.status.setText(f"HEARD SAVE FAILED: {error}. Replay to retry.")
        else:
            try:
                self.bundle, self.session = load_missing_voice_reuse_review(
                    self.session_path
                )
                self.status.setText(
                    "Heard evidence saved. Playback and replay remain available."
                )
            except Exception as refresh_error:
                self.status.setText(f"HEARD SAVED, REFRESH FAILED: {refresh_error}")
        if saved is not None and error is not None:
            self._pending_heard = [
                value for value in self._pending_heard if value != saved
            ]
        self._start_next_heard_save()
        self._refresh_sample()
        if (
            self._close_pending
            and not self.heard_runner.active
            and not self._pending_heard
        ):
            self._close_pending = False
            self.close()

    def _heard_keys(self):
        if self._cohort is None:
            return set()
        cohort_id = self._cohort["cohort_id"]
        return (
            {
                (value["queue_id"], value["label"])
                for value in self.session["heard"]
                if value["cohort_id"] == cohort_id
            }
            | {
                (queue_id, label)
                for current_cohort, queue_id, label in self._pending_heard
                if current_cohort == cohort_id
            }
            | (
                {(self._saving_heard[1], self._saving_heard[2])}
                if self._saving_heard is not None and self._saving_heard[0] == cohort_id
                else set()
            )
        )

    def _all_heard_records(self):
        return {
            (value["cohort_id"], value["queue_id"], value["label"])
            for value in self.session["heard"]
        }

    def _required_heard(self):
        if self._cohort is None:
            return set()
        queue_ids = {sample["queue_id"] for sample in self._cohort["samples"]}
        return {
            (sample["queue_id"], candidate["label"])
            for candidate in self.bundle["candidates"]
            for sample in candidate["samples"]
            if sample["queue_id"] in queue_ids and sample["status"] == "generated"
        }

    def _update_decisions(self):
        ready = (
            self._cohort is not None
            and not self.heard_runner.active
            and not self._pending_heard
            and self._heard_keys() == self._required_heard()
            and not self.decision_runner.active
        )
        complete = (
            set(self._cohort["complete_candidate_labels"])
            if self._cohort is not None
            else set()
        )
        for label, button in self.decision_buttons.items():
            button.setVisible(label in complete)
            button.setEnabled(ready and label in complete)
        self.neither.setEnabled(ready)
        if self._cohort is None:
            self.decision_reason.setText("Review complete.")
        elif self.decision_runner.active:
            self.decision_reason.setText(
                "Saving the family decision in background. Replay remains available."
            )
        elif not complete:
            self.decision_reason.setText(
                "No candidate completed every required sample. Hear available evidence, "
                "then choose Neither; incomplete candidates cannot win by omission."
            )
        elif ready:
            self.decision_reason.setText(
                "Decision ready. Choose one complete opaque voice or Neither."
            )
        else:
            remaining = len(self._required_heard() - self._heard_keys())
            self.decision_reason.setText(
                f"Decision locked: finish {remaining} available sample(s)."
            )

    def _save_decision(self, decision):
        if self._cohort is None or self.decision_runner.active:
            return
        self.status.setText(
            "Saving family decision in background. Replay remains available."
        )
        self.decision_runner.start(
            self.decision_recorder,
            self.session_path,
            self._cohort["cohort_id"],
            decision,
        )
        self._update_decisions()

    def _decision_saved(self, _result, error):
        if error is not None:
            self.status.setText(
                f"SAVE FAILED: {error}. Replay and retry remain available."
            )
            self._update_decisions()
        else:
            try:
                self._load_next_cohort()
            except Exception as refresh_error:
                self.status.setText(f"SAVED, BUT REFRESH FAILED: {refresh_error}")
                self._set_all_actions(False)
        if self._close_pending:
            self._close_pending = False
            self.close()

    def _playback_error(self, _error, error_string):
        self._playing_key = None
        self.now_playing.setText("PLAYBACK FAILED")
        self.stop.setEnabled(False)
        self.status.setText(f"PLAYBACK FAILED: {error_string}. Replay is available.")

    def _set_all_actions(self, enabled):
        for button in self.play_buttons.values():
            button.setEnabled(enabled)
        for button in self.decision_buttons.values():
            button.setEnabled(enabled)
        self.neither.setEnabled(enabled)
        self.previous.setEnabled(enabled)
        self.next.setEnabled(enabled)
        self.sample_selector.setEnabled(enabled)
        self.stop.setEnabled(enabled and self._playing_key is not None)

    def closeEvent(self, event: QCloseEvent):
        if (
            self.heard_runner.active
            or self._pending_heard
            or self.decision_runner.active
        ):
            self._close_pending = True
            self.status.setText(
                "Close requested; waiting for the current background save to finish."
            )
            event.ignore()
            return
        self._stop()
        super().closeEvent(event)


def launch_missing_voice_reuse_review(session_path):
    app = QApplication.instance() or QApplication(sys.argv)
    dialog = MissingVoiceReuseReviewDialog(session_path)
    dialog.show()
    return app.exec()


__all__ = ["MissingVoiceReuseReviewDialog", "launch_missing_voice_reuse_review"]

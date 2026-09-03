"""Qt review surface for bounded terminal authority conflicts."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.authoring.review_context_ui import ReviewDecisionContext
from vntts.authoring.terminal_conflict_review import (
    NEITHER_ACCEPTABLE,
    TerminalConflictReviewError,
    load_terminal_conflict_candidate_audio,
    load_terminal_conflict_review_document,
    load_terminal_conflict_review_progress,
    record_terminal_conflict_decision,
)
from vntts.qt_audio import QtPcmPlayer as QMediaPlayer
from vntts.qt_audio import play_audio_bytes, release_audio_buffer


class TerminalConflictReviewDialog(QDialog):
    """Play every distinct WAV and save one explicit winner per conflict."""

    def __init__(
        self,
        directory,
        parent=None,
        *,
        thread_pool=None,
        candidate_loader=load_terminal_conflict_candidate_audio,
        decision_recorder=record_terminal_conflict_decision,
        confirmer=None,
    ):
        super().__init__(parent)
        self.directory = Path(directory).expanduser().resolve()
        self.document = load_terminal_conflict_review_document(self.directory)
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._decision_finished)
        self.playback_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.playback_runner.finished.connect(self._playback_prepared)
        self.playback_runner.activeChanged.connect(
            lambda _active: self._set_actions(True)
        )
        self.candidate_loader = candidate_loader
        self.decision_recorder = decision_recorder
        self.confirmer = confirmer or self._confirm_decision
        self._active = False
        self._close_pending = False
        self._audio_buffer = None
        self._playing_candidate = None
        self._heard = set()
        self._current = None
        self._display_candidates = []

        self.setWindowTitle("Terminal audio conflict review")
        self.setMinimumSize(760, 420)
        self.progress = QLabel()
        self.progress.setAccessibleName("Terminal conflict review progress")
        self.decision_context = ReviewDecisionContext()
        self.identity = QLabel()
        self.identity.setWordWrap(True)
        self.identity.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.identity.setAccessibleName("Current terminal conflict identity")
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text.setAccessibleName("Current terminal conflict text")
        self.evidence = QLabel()
        self.evidence.setWordWrap(True)
        self.evidence.setAccessibleName("Required conflict listening evidence")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Terminal conflict review status")

        self.play_buttons = []
        playback = QGridLayout()
        for index in range(2):
            button = QPushButton(f"Play candidate {chr(65 + index)}")
            button.setAccessibleName(f"Play terminal conflict candidate {index + 1}")
            button.setAccessibleDescription(
                "Play this checksum-distinct blind candidate through to the end"
            )
            button.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            button.clicked.connect(
                lambda _checked=False, value=index: self._play(value)
            )
            playback.addWidget(button, 0, index)
            self.play_buttons.append(button)
        self.stop = QPushButton("Stop audio")
        self.stop.setAccessibleName("Stop terminal conflict audio")
        self.stop.setAccessibleDescription("Stop blind candidate playback")
        self.stop.setShortcut(QKeySequence("Ctrl+Space"))
        self.stop.clicked.connect(self._stop)
        self.stop.setEnabled(False)
        playback.addWidget(self.stop, 1, 0, 1, 2)

        self.choose_buttons = []
        decisions = QGridLayout()
        for index in range(2):
            button = QPushButton(f"Choose candidate {chr(65 + index)}")
            button.setAccessibleName(f"Choose terminal conflict candidate {index + 1}")
            button.setAccessibleDescription(
                "Keep this candidate as the terminal authority after both are heard"
            )
            button.setShortcut(QKeySequence(f"Alt+{index + 1}"))
            button.clicked.connect(
                lambda _checked=False, value=index: self._choose(value)
            )
            decisions.addWidget(button, 0, index)
            self.choose_buttons.append(button)
        self.neither = QPushButton("Neither candidate is acceptable")
        self.neither.setAccessibleName("Reject both terminal conflict candidates")
        self.neither.setAccessibleDescription(
            "Require repair instead of keeping either terminal candidate"
        )
        self.neither.setShortcut(QKeySequence("Alt+N"))
        self.neither.clicked.connect(self._choose_neither)
        decisions.addWidget(self.neither, 1, 0, 1, 2)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setAccessibleName("Close terminal conflict review")
        self.close_button.setAccessibleDescription(
            "Close this review without resolving the current conflict"
        )
        review_content = QWidget()
        review_layout = QVBoxLayout(review_content)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.addWidget(self.progress)
        review_layout.addWidget(self.decision_context)
        review_layout.addWidget(self.identity)
        review_layout.addWidget(self.text)
        review_layout.addLayout(playback)
        review_layout.addWidget(self.evidence)
        review_layout.addWidget(self.status)
        review_layout.addLayout(decisions)
        self.review_scroll = QScrollArea()
        self.review_scroll.setAccessibleName("Scrollable terminal conflict review")
        self.review_scroll.setWidgetResizable(True)
        self.review_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.review_scroll.setWidget(review_content)
        layout = QVBoxLayout(self)
        layout.addWidget(self.review_scroll, 1)
        layout.addWidget(buttons)

        self.setTabOrder(self.decision_context.technical_toggle, self.play_buttons[0])
        self.setTabOrder(self.play_buttons[0], self.play_buttons[1])
        self.setTabOrder(self.play_buttons[1], self.stop)
        self.setTabOrder(self.stop, self.choose_buttons[0])
        self.setTabOrder(self.choose_buttons[0], self.choose_buttons[1])
        self.setTabOrder(self.choose_buttons[1], self.neither)
        self.setTabOrder(self.neither, self.close_button)

        self.player = QMediaPlayer(self)
        self.player.errorOccurred.connect(self._playback_error)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self._load_next()

    def _decisions(self):
        progress = self.directory / "progress.json"
        if not progress.is_file():
            return {}
        document = load_terminal_conflict_review_progress(self.directory)
        return {value["case_id"]: value["decision"] for value in document["decisions"]}

    def _load_next(self):
        self._stop()
        self.document = load_terminal_conflict_review_document(self.directory)
        decisions = self._decisions()
        total = len(self.document["cases"])
        self.progress.setText(f"Progress: {len(decisions)}/{total}")
        self._current = next(
            (
                case
                for case in self.document["cases"]
                if case["case_id"] not in decisions
            ),
            None,
        )
        self._heard.clear()
        if self._current is None:
            self.decision_context.set_context(
                {
                    "purpose": "Resolve contradictory terminal WAV authorities",
                    "effect": "Review complete; no further decision is required",
                }
            )
            self.identity.setText("All terminal conflicts have an explicit decision.")
            self.text.clear()
            self.evidence.setText("No additional listening is required in this bundle.")
            self.status.setText(
                "Decisions are saved as review evidence. Source workspaces remain unchanged."
            )
            self._set_actions(False)
            return
        candidates = self._current["candidates"]
        if len(candidates) != 2:
            raise TerminalConflictReviewError(
                "The current UI supports exactly two distinct candidates per conflict"
            )
        self._display_candidates = sorted(
            candidates,
            key=lambda candidate: hashlib.sha256(
                (
                    self.document["review_id"]
                    + ":"
                    + self._current["case_id"]
                    + ":"
                    + candidate["candidate_id"]
                ).encode("utf-8")
            ).hexdigest(),
        )
        for index, button in enumerate(self.choose_buttons):
            button.setText(f"Choose candidate {chr(65 + index)}")
            button.setAccessibleName(f"Choose terminal conflict candidate {index + 1}")
        self.identity.setText(
            f"Line: {self._current['line_id']} | Speaker: {self._current['speaker']} | "
            f"Voice: {self._current['voice_character']}"
        )
        self.decision_context.set_context(
            {
                "purpose": "Resolve two contradictory historical WAV decisions",
                "game_speaker": self._current["speaker"],
                "synthesis_voice": self._current["voice_character"],
                "reference": "Hidden because the two candidates are compared blind",
                "backend": "Hidden with candidate authority until both are heard",
                "model": "Hidden with candidate authority until both are heard",
                "generation_profile": (
                    "Hidden with candidate authority until both are heard"
                ),
                "controls": "Two checksum-distinct historical WAV candidates",
                "effect": (
                    "keep one terminal authority, or require repair if neither is "
                    "acceptable"
                ),
            },
            technical=(
                f"Review: {self.document['review_id']}\n"
                f"Conflict: {self._current['case_id']}\n"
                f"Line: {self._current['line_id']}"
            ),
        )
        self.text.setText(self._current["text"])
        self.evidence.setText("Listen to both blind candidates before choosing.")
        self.status.setText("No source workspace will be changed by this decision.")
        self._set_actions(True)

    def _play(self, index):
        if self._active or self._current is None:
            return
        candidate = self._display_candidates[index]
        self._stop()
        self.status.setText(
            f"Preparing checksum-verified candidate {chr(65 + index)} in background..."
        )
        self.playback_runner.start(
            self._load_candidate_payload,
            self.candidate_loader,
            self.directory,
            self._current["case_id"],
            candidate["candidate_id"],
            candidate["audio_sha256"],
            index,
        )

    @staticmethod
    def _load_candidate_payload(
        loader, directory, case_id, candidate_id, expected_sha256, index
    ):
        payload = loader(directory, case_id, candidate_id)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise TerminalConflictReviewError("Terminal conflict candidate WAV changed")
        return case_id, candidate_id, expected_sha256, index, payload

    def _playback_prepared(self, result, error):
        if error is not None:
            self.status.setText(f"PLAYBACK BLOCKED: {error}")
            self._set_actions(True)
            return
        case_id, candidate_id, expected_sha256, index, payload = result
        if self._current is None or self._current["case_id"] != case_id:
            self.status.setText("PLAYBACK CANCELLED: conflict selection changed")
            self._set_actions(True)
            return
        candidate = self._display_candidates[index]
        if (
            candidate["candidate_id"] != candidate_id
            or candidate["audio_sha256"] != expected_sha256
        ):
            self.status.setText("PLAYBACK CANCELLED: candidate selection changed")
            self._set_actions(True)
            return
        self._audio_buffer = play_audio_bytes(
            self.player,
            self,
            payload,
            f"memory:terminal-candidate-{index + 1}.wav",
        )
        if self._audio_buffer is None:
            self.status.setText("PLAYBACK BLOCKED: immutable audio buffer failed")
            self._set_actions(True)
            return
        self._playing_candidate = candidate["candidate_id"]
        self.stop.setEnabled(True)
        self.evidence.setText(
            f"Playing candidate {chr(65 + index)}. It counts only after audio ends."
        )

    def _stop(self):
        if hasattr(self, "playback_runner"):
            self.playback_runner.cancel()
        self.player.stop()
        release_audio_buffer(self.player, self._audio_buffer)
        self._audio_buffer = None
        self._playing_candidate = None
        self.stop.setEnabled(False)

    def _choose(self, index):
        if self._current is None:
            return
        self._save(self._display_candidates[index]["candidate_id"])

    def _choose_neither(self):
        self._save(NEITHER_ACCEPTABLE)

    def _save(self, decision):
        if self._active or self._current is None or len(self._heard) != 2:
            return
        if not self.confirmer(decision):
            self.status.setText("Decision cancelled; conflict evidence is unchanged.")
            return
        self._stop()
        self._active = True
        self._set_actions(False)
        self.status.setText(
            "Saving in background: rechecking report, state, queue and both WAVs..."
        )
        self.runner.start(
            self.decision_recorder,
            self.directory,
            self._current["case_id"],
            decision,
        )

    def _confirm_decision(self, _decision):
        return (
            QMessageBox.question(
                self,
                "Save irreversible conflict decision?",
                "Save this terminal conflict decision? This review window cannot "
                "revise it afterward.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _decision_finished(self, _result, error):
        self._active = False
        if error is not None:
            self.status.setText(
                f"SAVE FAILED: {error}. Replay and retry are available."
            )
            self._set_actions(True)
        else:
            try:
                self._load_next()
            except Exception as refresh_error:
                self.status.setText(f"SAVED, BUT REFRESH FAILED: {refresh_error}")
                self._set_actions(False)
        if self._close_pending:
            self._close_pending = False
            self.close()

    def _set_actions(self, enabled):
        enabled = bool(
            enabled
            and self._current is not None
            and not self._active
            and not self.playback_runner.active
        )
        for button in self.play_buttons:
            button.setEnabled(enabled)
        self.stop.setEnabled(
            self._current is not None
            and not self._active
            and (self.playback_runner.active or self._playing_candidate is not None)
        )
        self._update_decision_buttons()

    def _update_decision_buttons(self):
        enabled = (
            self._current is not None
            and not self._active
            and len(self._heard) == 2
            and not self.playback_runner.active
        )
        for button in self.choose_buttons:
            button.setEnabled(enabled)
        self.neither.setEnabled(enabled)

        if enabled:
            consequences = []
            for index, (button, candidate) in enumerate(
                zip(self.choose_buttons, self._display_candidates, strict=True)
            ):
                authority = candidate["authority"]
                label = chr(65 + index)
                button.setText(f"Keep {authority.title()} candidate {label}")
                button.setAccessibleName(
                    f"Keep historically {authority} terminal candidate {index + 1}"
                )
                consequence = (
                    "enters the approved manifest"
                    if authority == "approved"
                    else "remains rejected outside the manifest"
                )
                consequences.append(f"{label} was {authority} and {consequence}")
            self.evidence.setText(
                "Both candidates finished. " + "; ".join(consequences) + "."
            )

    def _media_status_changed(self, status):
        if (
            status != QMediaPlayer.MediaStatus.EndOfMedia
            or self._playing_candidate is None
        ):
            return
        self._heard.add(self._playing_candidate)
        self._playing_candidate = None
        self.stop.setEnabled(False)
        self.evidence.setText(
            f"Heard {len(self._heard)}/2 candidates. Replay remains available."
        )
        self._update_decision_buttons()

    def _playback_error(self, _error, error_string):
        self._playing_candidate = None
        self.stop.setEnabled(False)
        self.status.setText(f"PLAYBACK FAILED: {error_string}")
        self._update_decision_buttons()

    def closeEvent(self, event: QCloseEvent):
        if self._active:
            self._close_pending = True
            self.status.setText(
                "Close requested; waiting for the current save to finish."
            )
            event.ignore()
            return
        self._stop()
        super().closeEvent(event)


def launch_terminal_conflict_review(directory):
    app = QApplication.instance() or QApplication(sys.argv)
    dialog = TerminalConflictReviewDialog(directory)
    dialog.show()
    return app.exec()


def create_parser():
    parser = argparse.ArgumentParser(
        description="Review exact competing terminal authoring WAVs"
    )
    parser.add_argument("directory", type=Path)
    return parser


def main(argv=None):
    options = create_parser().parse_args(argv)
    try:
        return launch_terminal_conflict_review(options.directory)
    except TerminalConflictReviewError as error:
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "Unable to open terminal conflict review",
            f"Review directory: {options.directory.expanduser()}\n\n{error}",
        )
        app.processEvents()
        return 2


__all__ = [
    "TerminalConflictReviewDialog",
    "launch_terminal_conflict_review",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())

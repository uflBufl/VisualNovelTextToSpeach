"""Qt review surface for bounded terminal authority conflicts."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from vntts.async_ui import LatestTaskRunner
from vntts.authoring.terminal_conflict_review import (
    NEITHER_ACCEPTABLE,
    TerminalConflictReviewError,
    load_terminal_conflict_candidate_audio,
    load_terminal_conflict_review_document,
    load_terminal_conflict_review_progress,
    record_terminal_conflict_decision,
)


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
    ):
        super().__init__(parent)
        self.directory = Path(directory).expanduser().resolve()
        self.document = load_terminal_conflict_review_document(self.directory)
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._decision_finished)
        self.candidate_loader = candidate_loader
        self.decision_recorder = decision_recorder
        self._active = False
        self._close_pending = False
        self._audio_buffer = None
        self._playing_candidate = None
        self._heard = set()
        self._current = None

        self.setWindowTitle("Terminal audio conflict review")
        self.setMinimumSize(760, 420)
        self.progress = QLabel()
        self.progress.setAccessibleName("Terminal conflict review progress")
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
        playback = QHBoxLayout()
        for index in range(2):
            button = QPushButton(f"Play candidate {chr(65 + index)}")
            button.setAccessibleName(f"Play terminal conflict candidate {index + 1}")
            button.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            button.clicked.connect(
                lambda _checked=False, value=index: self._play(value)
            )
            playback.addWidget(button)
            self.play_buttons.append(button)
        self.stop = QPushButton("Stop audio")
        self.stop.setShortcut(QKeySequence("Ctrl+Space"))
        self.stop.clicked.connect(self._stop)
        playback.addWidget(self.stop)

        self.choose_buttons = []
        decisions = QHBoxLayout()
        for index in range(2):
            button = QPushButton(f"Choose candidate {chr(65 + index)}")
            button.setAccessibleName(f"Choose terminal conflict candidate {index + 1}")
            button.setShortcut(QKeySequence(f"Alt+{index + 1}"))
            button.clicked.connect(
                lambda _checked=False, value=index: self._choose(value)
            )
            decisions.addWidget(button)
            self.choose_buttons.append(button)
        self.neither = QPushButton("Neither candidate is acceptable")
        self.neither.setAccessibleName("Reject both terminal conflict candidates")
        self.neither.setShortcut(QKeySequence("Alt+N"))
        self.neither.clicked.connect(self._choose_neither)
        decisions.addWidget(self.neither)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.progress)
        layout.addWidget(self.identity)
        layout.addWidget(self.text, 1)
        layout.addLayout(playback)
        layout.addWidget(self.evidence)
        layout.addWidget(self.status)
        layout.addLayout(decisions)
        layout.addWidget(buttons)

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.errorOccurred.connect(self._playback_error)
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
        self.identity.setText(
            f"Line: {self._current['line_id']} | Speaker: {self._current['speaker']} | "
            f"Voice: {self._current['voice_character']}"
        )
        self.text.setText(self._current["text"])
        self.evidence.setText("Listen to both blind candidates before choosing.")
        self.status.setText("No source workspace will be changed by this decision.")
        self._set_actions(True)

    def _play(self, index):
        if self._active or self._current is None:
            return
        candidate = self._current["candidates"][index]
        try:
            payload = self.candidate_loader(
                self.directory, self._current["case_id"], candidate["candidate_id"]
            )
        except TerminalConflictReviewError as error:
            self.status.setText(f"PLAYBACK BLOCKED: {error}")
            return
        if hashlib.sha256(payload).hexdigest() != candidate["audio_sha256"]:
            self.status.setText("PLAYBACK BLOCKED: candidate WAV changed")
            return
        self._stop()
        self._audio_buffer = QBuffer(self)
        self._audio_buffer.setData(QByteArray(payload))
        self._audio_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self._playing_candidate = candidate["candidate_id"]
        self.player.setSourceDevice(
            self._audio_buffer, QUrl(f"memory:terminal-candidate-{index + 1}.wav")
        )
        self.player.play()
        self._heard.add(candidate["candidate_id"])
        self.evidence.setText(
            f"Heard {len(self._heard)}/2 candidates. Replay remains available."
        )
        self._update_decision_buttons()

    def _stop(self):
        self.player.stop()
        self.player.setSource(QUrl())
        if self._audio_buffer is not None:
            self._audio_buffer.close()
        self._audio_buffer = None
        self._playing_candidate = None

    def _choose(self, index):
        if self._current is None:
            return
        self._save(self._current["candidates"][index]["candidate_id"])

    def _choose_neither(self):
        self._save(NEITHER_ACCEPTABLE)

    def _save(self, decision):
        if self._active or self._current is None or len(self._heard) != 2:
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
        enabled = bool(enabled and self._current is not None and not self._active)
        for button in self.play_buttons:
            button.setEnabled(enabled)
        self.stop.setEnabled(enabled)
        self._update_decision_buttons()

    def _update_decision_buttons(self):
        enabled = (
            self._current is not None and not self._active and len(self._heard) == 2
        )
        for button in self.choose_buttons:
            button.setEnabled(enabled)
        self.neither.setEnabled(enabled)

    def _playback_error(self, _error, error_string):
        self.status.setText(f"PLAYBACK FAILED: {error_string}")

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
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


__all__ = [
    "TerminalConflictReviewDialog",
    "launch_terminal_conflict_review",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())

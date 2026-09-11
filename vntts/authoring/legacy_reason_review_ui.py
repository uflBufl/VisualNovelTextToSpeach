"""Compact player for classifying legacy WAVs already known to be bad."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from vntts.authoring.legacy_reason_review import (
    LegacyReasonReviewError,
    build_legacy_reason_review,
    load_reason_review_progress,
    publish_reason_review_decisions,
    write_reason_review_progress,
)
from vntts.qt_audio import QtPcmPlayer

_REASON_LABELS = {
    "pause_or_pacing": "Bad pause or pacing",
    "repetition": "Repeated words or phrases",
    "truncation_or_missing_words": "Truncated or missing words",
    "pronunciation_or_wrong_words": "Pronunciation or wrong words",
    "timbre_or_audio_artifact": "Distortion, timbre, or audio artifact",
    "speaker_identity": "Wrong speaker or voice identity",
    "other_or_unclear": "Other or unclear defect",
}


class LegacyReasonReviewDialog(QDialog):
    def __init__(
        self,
        review,
        progress_path,
        *,
        player=None,
        progress_writer=write_reason_review_progress,
        publisher=publish_reason_review_decisions,
        parent=None,
    ):
        super().__init__(parent)
        self.review = review
        self.progress_path = Path(progress_path)
        self.player = player or QtPcmPlayer(self)
        self.progress_writer = progress_writer
        self.publisher = publisher
        self.selections = load_reason_review_progress(review, self.progress_path)
        self.index = next(
            (
                index
                for index, item in enumerate(review.items)
                if item.item_id not in self.selections
            ),
            0,
        )
        self._syncing = False
        self.setWindowTitle("Classify rejected speech")
        self.setMinimumWidth(700)

        self.progress = QLabel()
        self.progress.setStyleSheet("font-weight: 700;")
        self.context = QLabel(
            "Every item here was already rejected. Play it and mark why; good WAVs "
            "are intentionally excluded."
        )
        self.context.setWordWrap(True)
        self.speaker = QLabel()
        self.speaker.setStyleSheet("font-weight: 600;")
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.play = QPushButton("Play / replay")
        self.play.clicked.connect(self.play_current)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.player.errorOccurred.connect(
            lambda _error, message: self.status.setText(f"Playback failed: {message}")
        )

        reasons = QGridLayout()
        self.reason_controls = {}
        for index, (reason, label) in enumerate(_REASON_LABELS.items()):
            control = QCheckBox(label)
            control.toggled.connect(self._reasons_changed)
            reasons.addWidget(control, index // 2, index % 2)
            self.reason_controls[reason] = control

        self.previous = QPushButton("Previous")
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next = QPushButton("Next")
        self.next.clicked.connect(lambda: self._move(1))
        self.finish = QPushButton("Publish reason labels")
        self.finish.clicked.connect(self.publish)
        actions = QHBoxLayout()
        actions.addWidget(self.previous)
        actions.addWidget(self.next)
        actions.addStretch()
        actions.addWidget(self.finish)

        layout = QVBoxLayout(self)
        layout.addWidget(self.progress)
        layout.addWidget(self.context)
        layout.addWidget(self.speaker)
        layout.addWidget(self.text)
        layout.addWidget(self.play)
        layout.addLayout(reasons)
        layout.addWidget(self.status)
        layout.addLayout(actions)

        QShortcut(QKeySequence("Space"), self, activated=self.play_current)
        QShortcut(QKeySequence("Left"), self, activated=lambda: self._move(-1))
        QShortcut(QKeySequence("Right"), self, activated=lambda: self._move(1))
        self._show_current()

    def current_item(self):
        return self.review.items[self.index]

    def play_current(self):
        item = self.current_item()
        self.status.setText("Playing rejected WAV...")
        self.player.setSource(QUrl.fromLocalFile(str(item.audio)))
        self.player.play()

    def _reasons_changed(self, _checked=False):
        if self._syncing:
            return
        reasons = tuple(
            reason
            for reason, control in self.reason_controls.items()
            if control.isChecked()
        )
        item = self.current_item()
        if reasons:
            self.selections[item.item_id] = reasons
        else:
            self.selections.pop(item.item_id, None)
        try:
            self.progress_writer(self.review, self.progress_path, self.selections)
        except (OSError, LegacyReasonReviewError) as error:
            self.status.setText(f"Could not save progress: {error}")
            return
        self.status.setText("Saved. Choose Next or replay this WAV.")
        self._update_actions()

    def _move(self, offset):
        self.player.stop()
        self.index = max(0, min(len(self.review.items) - 1, self.index + offset))
        self._show_current()

    def _show_current(self):
        item = self.current_item()
        classified = len(self.selections)
        self.progress.setText(
            f"Rejected WAV {self.index + 1} of {len(self.review.items)} - "
            f"{classified} classified"
        )
        self.speaker.setText(f"Speaker: {item.speaker} | Line: {item.line_id}")
        self.text.setText(item.text)
        selected = set(self.selections.get(item.item_id, ()))
        self._syncing = True
        try:
            for reason, control in self.reason_controls.items():
                control.setChecked(reason in selected)
        finally:
            self._syncing = False
        self.status.setText(
            "This WAV still needs a reason."
            if not selected
            else "Reason saved; replay or continue."
        )
        self._update_actions()

    def _update_actions(self):
        self.previous.setEnabled(self.index > 0)
        self.next.setEnabled(self.index + 1 < len(self.review.items))
        self.finish.setEnabled(len(self.selections) == len(self.review.items))

    def publish(self):
        try:
            paths = self.publisher(self.review, self.selections)
        except (OSError, LegacyReasonReviewError) as error:
            QMessageBox.critical(self, "Reason labels were not published", str(error))
            return
        self.status.setText(f"Published {len(paths)} additive cohort decisions.")
        self.accept()

    def closeEvent(self, event):
        self.player.stop()
        super().closeEvent(event)


def default_progress_path(corpus):
    corpus = Path(corpus).expanduser().resolve()
    return corpus.parent / f"{corpus.name}-reason-review-progress.json"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Classify only legacy rejected WAVs that lack defect reasons"
    )
    parser.add_argument("corpus", type=Path)
    parser.add_argument("decision_root", type=Path)
    parser.add_argument("--progress", type=Path)
    arguments = parser.parse_args(argv)
    try:
        review = build_legacy_reason_review(arguments.corpus, arguments.decision_root)
    except LegacyReasonReviewError as error:
        print(f"Unable to open reason review: {error}", file=sys.stderr)
        return 2
    if not review.items:
        print("No rejected WAVs need reason labels.")
        return 0
    _application = QApplication.instance() or QApplication(sys.argv[:1])
    dialog = LegacyReasonReviewDialog(
        review,
        arguments.progress or default_progress_path(arguments.corpus),
    )
    return 0 if dialog.exec() == QDialog.DialogCode.Accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())

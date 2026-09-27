"""Compact player for classifying legacy WAVs already known to be bad."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QEvent, QObject, Qt, QUrl, SignalInstance
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vntts.authoring.legacy_reason_review import (
    LegacyReasonReview,
    LegacyReasonReviewError,
    LegacyReasonReviewItem,
    build_legacy_reason_review,
    load_reason_review_progress,
    publish_reason_review_decisions,
    write_reason_review_progress,
)
from vntts.authoring.review_context_ui import review_scroll_area
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

ReasonSelections = dict[str, tuple[str, ...]]


class _AudioPlayer(Protocol):
    errorOccurred: SignalInstance
    mediaStatusChanged: SignalInstance

    def setSource(self, source: QUrl) -> None: ...

    def play(self) -> None: ...

    def stop(self) -> None: ...


class LegacyReasonReviewDialog(QDialog):
    def __init__(
        self,
        review: LegacyReasonReview,
        progress_path: str | Path,
        *,
        player: _AudioPlayer | None = None,
        progress_writer: Callable[
            [LegacyReasonReview, str | Path, Mapping[str, tuple[str, ...]]], object
        ] = write_reason_review_progress,
        publisher: Callable[
            [LegacyReasonReview, Mapping[str, tuple[str, ...]]], tuple[Path, ...]
        ] = publish_reason_review_decisions,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.review = review
        self.progress_path = Path(progress_path)
        self.player = player or QtPcmPlayer(self)
        self.progress_writer = progress_writer
        self.publisher = publisher
        self.selections: ReasonSelections = load_reason_review_progress(
            review, self.progress_path
        )
        self._persisted_selections = dict(self.selections)
        self._heard_items: set[str] = set()
        self._playing_item_id: str | None = None
        self.index = next(
            (
                index
                for index, item in enumerate(review.items)
                if item.item_id not in self.selections
            ),
            0,
        )
        self._syncing = False
        self.setWindowTitle("Reassess rejected recordings")
        self.setMinimumSize(640, 380)

        self.heading = QLabel("Rejected recording reassessment")
        bold = QFont()
        bold.setBold(True)
        self.heading.setFont(bold)
        self.progress = QLabel()
        self.context = QLabel(
            "Listen to each previously rejected recording. Choices save automatically. "
            "After all are assessed, publishing adds new decisions without changing "
            "earlier reviews."
        )
        self.context.setWordWrap(True)
        self.speaker = QLabel()
        self.speaker.setFont(bold)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text.setFrameShape(QLabel.Shape.StyledPanel)
        self.text.setMargin(8)
        self.play = QPushButton("Play recording")
        self.play.clicked.connect(self.play_current)
        self.stop = QPushButton("Stop")
        self.stop.clicked.connect(self.stop_current)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Recording and save status")
        self.player.errorOccurred.connect(self._playback_failed)
        self.player.mediaStatusChanged.connect(self._media_status_changed)

        self.acceptable = QCheckBox(
            "Sounds acceptable now (minor imperfections are okay)"
        )
        checkbox_style = (
            "QCheckBox { border: 1px solid transparent; }"
            "QCheckBox:focus { border-color: palette(highlight); }"
            "QCheckBox::indicator { width: 16px; height: 16px; }"
            "QCheckBox::indicator:enabled { border: 2px solid palette(text); }"
            "QCheckBox::indicator:enabled:checked { background: palette(highlight); }"
            "QCheckBox::indicator:disabled { border: 1px solid palette(mid); }"
        )
        self.acceptable.setStyleSheet(checkbox_style)
        self.acceptable.toggled.connect(self._acceptable_changed)
        reasons = QVBoxLayout()
        self.reason_controls: dict[str, QCheckBox] = {}
        for reason, label in _REASON_LABELS.items():
            control = QCheckBox(label)
            control.setStyleSheet(checkbox_style)
            control.toggled.connect(self._reasons_changed)
            reasons.addWidget(control)
            self.reason_controls[reason] = control

        self.previous = QPushButton("Previous")
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next = QPushButton("Next")
        self.next.clicked.connect(lambda: self._move(1))
        self.finish = QPushButton("Publish assessments")
        self.finish.clicked.connect(self.publish)
        actions = QHBoxLayout()
        actions.addWidget(self.previous)
        actions.addWidget(self.next)
        actions.addStretch()
        actions.addWidget(self.finish)

        audio_actions = QHBoxLayout()
        audio_actions.addWidget(self.play)
        audio_actions.addWidget(self.stop)
        audio_actions.addStretch()
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.addWidget(self.context)
        content_layout.addWidget(self.speaker)
        content_layout.addWidget(self.text)
        content_layout.addLayout(audio_actions)
        assessment_hint = QLabel("Choose acceptable, or select all defects you hear.")
        assessment_hint.setWordWrap(True)
        content_layout.addWidget(assessment_hint)
        content_layout.addWidget(self.acceptable)
        content_layout.addWidget(QLabel("Defects"))
        content_layout.addLayout(reasons)
        content_layout.addStretch()
        self.review_scroll = review_scroll_area(content, "Recording and assessment")
        for widget in content.findChildren(QWidget):
            if widget.focusPolicy() != Qt.FocusPolicy.NoFocus:
                widget.installEventFilter(self)
        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.progress)
        layout.addWidget(self.review_scroll, 1)
        layout.addWidget(self.status)
        layout.addLayout(actions)

        self._show_current()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.FocusIn and isinstance(watched, QWidget):
            self.review_scroll.ensureWidgetVisible(watched)
        return super().eventFilter(watched, event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "reason_controls"):
            for control in (self.acceptable, *self.reason_controls.values()):
                control.setFont(self.font())

    def current_item(self) -> LegacyReasonReviewItem:
        return self.review.items[self.index]

    def play_current(self) -> None:
        item = self.current_item()
        self._playing_item_id = item.item_id
        self.status.setText("Playing recording. Listen to the end before assessing.")
        self.player.setSource(QUrl.fromLocalFile(str(item.audio)))
        self.player.play()

    def stop_current(self) -> None:
        self.player.stop()
        self._playing_item_id = None
        self.status.setText("Playback stopped. Play the recording to the end.")
        self._update_actions()

    def _media_status_changed(self, status: object) -> None:
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        playing_item_id = self._playing_item_id
        if (
            playing_item_id is not None
            and playing_item_id == self.current_item().item_id
        ):
            self._heard_items.add(playing_item_id)
            self.status.setText("Recording heard. Choose an assessment.")
        self._playing_item_id = None
        self._update_actions()

    def _playback_failed(self, _error: object, message: str) -> None:
        self._playing_item_id = None
        self.status.setText(f"Playback failed: {message}")
        self._update_actions()

    def _reasons_changed(self, _checked: bool = False) -> None:
        if self._syncing:
            return
        reasons = tuple(
            reason
            for reason, control in self.reason_controls.items()
            if control.isChecked()
        )
        item = self.current_item()
        if reasons:
            self._syncing = True
            try:
                self.acceptable.setChecked(False)
            finally:
                self._syncing = False
            self.selections[item.item_id] = reasons
        else:
            self.selections.pop(item.item_id, None)
        self._save_current()

    def _acceptable_changed(self, checked: bool) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            for control in self.reason_controls.values():
                control.setChecked(False)
                control.setEnabled(not checked)
        finally:
            self._syncing = False
        item = self.current_item()
        if checked:
            self.selections[item.item_id] = ()
        else:
            self.selections.pop(item.item_id, None)
        self._save_current()

    def _save_current(self) -> None:
        try:
            self.progress_writer(self.review, self.progress_path, self.selections)
        except (OSError, LegacyReasonReviewError) as error:
            self.selections = dict(self._persisted_selections)
            self._show_current()
            self.status.setText(
                f"Assessment not saved; last change reverted: {error}. "
                "Fix the storage problem, then choose again."
            )
            return
        self._persisted_selections = dict(self.selections)
        self.status.setText(
            "Assessment saved. Choose Publish assessments or replay this recording."
            if len(self.selections) == len(self.review.items)
            else "Assessment saved. Choose Next or replay this recording."
        )
        self._update_actions()

    def _move(self, offset: int) -> None:
        self.player.stop()
        self._playing_item_id = None
        self.index = max(0, min(len(self.review.items) - 1, self.index + offset))
        self._show_current()

    def _show_current(self) -> None:
        item = self.current_item()
        self.speaker.setText(f"Speaker: {item.speaker}")
        self.text.setText(item.text)
        selected = set(self.selections.get(item.item_id, ()))
        classified = item.item_id in self.selections
        acceptable = classified and not selected
        self._syncing = True
        try:
            self.acceptable.setChecked(acceptable)
            for reason, control in self.reason_controls.items():
                control.setChecked(reason in selected)
                control.setEnabled(not acceptable)
        finally:
            self._syncing = False
        self.status.setText(
            "Current assessment saved; replay or continue."
            if classified
            else "Recording heard. Choose an assessment."
            if item.item_id in self._heard_items
            else "Play this recording to the end before assessing."
        )
        self._update_actions()

    def _update_actions(self) -> None:
        item = self.current_item()
        self.progress.setText(
            f"Recording {self.index + 1} of {len(self.review.items)} | "
            f"{len(self.selections)} assessed"
        )
        assessed = item.item_id in self.selections
        can_assess = assessed or item.item_id in self._heard_items
        self.acceptable.setEnabled(can_assess)
        for control in self.reason_controls.values():
            control.setEnabled(can_assess and not self.acceptable.isChecked())
        self.stop.setEnabled(self._playing_item_id is not None)
        self.previous.setEnabled(self.index > 0)
        self.next.setEnabled(self.index + 1 < len(self.review.items))
        self.finish.setEnabled(len(self.selections) == len(self.review.items))

    def publish(self) -> None:
        try:
            paths = self.publisher(self.review, self.selections)
        except (OSError, LegacyReasonReviewError) as error:
            self.status.setText(
                f"Assessments not published: {error}. Resolve the problem, then "
                "choose Publish assessments again."
            )
            return
        self.status.setText(f"Published {len(paths)} additive cohort decisions.")
        self.accept()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.player.stop()
        self._playing_item_id = None
        super().closeEvent(event)


def default_progress_path(corpus: str | Path) -> Path:
    corpus = Path(corpus).expanduser().resolve()
    return corpus.parent / f"{corpus.name}-reason-review-progress.json"


def main(argv: Sequence[str] | None = None) -> int:
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

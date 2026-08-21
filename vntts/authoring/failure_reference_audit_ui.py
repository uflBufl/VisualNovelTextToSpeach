"""Qt operator interface for checksum-bound failed-reference audits."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QRunnable,
    QThreadPool,
    QUrl,
    Signal,
)
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from vntts.authoring.bulk_generation import _canonical_sha256
from vntts.authoring.failure_reference_audit import (
    load_failure_reference_audit,
    load_failure_reference_decisions,
    prepare_failure_reference_audio,
    record_failure_reference_decision,
)


class _TaskSignals(QObject):
    finished = Signal(int, object, object)


class _Task(QRunnable):
    def __init__(self, serial, operation, arguments, signals):
        super().__init__()
        self.serial = serial
        self.operation = operation
        self.arguments = arguments
        self.signals = signals

    def run(self):
        try:
            result = self.operation(*self.arguments)
        except Exception as error:
            self.signals.finished.emit(self.serial, None, error)
        else:
            self.signals.finished.emit(self.serial, result, None)


def _load_public_document(audit):
    validated = load_failure_reference_audit(audit)
    path = validated.directory / "audit.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    actual = _canonical_sha256(
        {name: value for name, value in document.items() if name != "audit_id"}
    )
    if actual != validated.audit_id:
        raise RuntimeError("Reference audit changed while opening the interface")
    return validated, document, load_failure_reference_decisions(validated.directory)


class FailureReferenceAuditDialog(QDialog):
    """Review four exact control groups without revealing private source names."""

    def __init__(
        self,
        audit,
        parent=None,
        *,
        audio_preparer=prepare_failure_reference_audio,
        decision_recorder=record_failure_reference_decision,
    ):
        super().__init__(parent)
        self.audit, self.document, decisions = _load_public_document(audit)
        self.audio_preparer = audio_preparer
        self.decision_recorder = decision_recorder
        self.decisions = {value["group_id"]: value for value in decisions["decisions"]}
        self._playback_serial = 0
        self._save_serial = 0
        self._playback_active = False
        self._save_active = False
        self._playback_buffer = None
        self._playback_target = None

        self.setWindowTitle("VNTTS failed-reference audit")
        self.resize(1040, 650)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.status = QLabel(
            "READY: compare copied reference audio. Decisions do not approve speech."
        )
        self.status.setWordWrap(True)
        self.group_choice = QComboBox()
        self.group_choice.setAccessibleName("Failed-reference control group")
        self.candidate_choice = QComboBox()
        self.candidate_choice.setAccessibleName("Blinded reference candidate")
        self.group_choice.currentIndexChanged.connect(self._show_group)

        self.cases = QTableWidget(0, 3)
        self.cases.setHorizontalHeaderLabels(["Line", "Speaker", "Failed text"])
        self.cases.setAccessibleName("Lines affected by this reference decision")
        self.cases.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cases.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.cases.verticalHeader().setVisible(False)
        self.cases.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.cases.setColumnWidth(0, 260)
        self.cases.setColumnWidth(1, 150)

        self.play = QPushButton("Play selected candidate")
        self.stop = QPushButton("Stop")
        self.choose = QPushButton("Use selected candidate")
        self.neither = QPushButton("Neither candidate is acceptable")
        self.previous = QPushButton("Previous group")
        self.next = QPushButton("Next group")
        self.play.clicked.connect(self.play_selected)
        self.stop.clicked.connect(self.stop_playback)
        self.choose.clicked.connect(self.choose_selected)
        self.neither.clicked.connect(lambda: self.save_decision("neither_acceptable"))
        self.previous.clicked.connect(lambda: self._move_group(-1))
        self.next.clicked.connect(lambda: self._move_group(1))

        playback = QHBoxLayout()
        playback.addWidget(self.candidate_choice, 1)
        playback.addWidget(self.play)
        playback.addWidget(self.stop)
        decisions_row = QHBoxLayout()
        decisions_row.addWidget(self.previous)
        decisions_row.addWidget(self.choose)
        decisions_row.addWidget(self.neither)
        decisions_row.addWidget(self.next)

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.status)
        layout.addWidget(self.group_choice)
        layout.addWidget(self.cases, 1)
        layout.addLayout(playback)
        layout.addLayout(decisions_row)

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(self._media_error)

        self._playback_signals = _TaskSignals(self)
        self._playback_signals.finished.connect(self._playback_finished)
        self._save_signals = _TaskSignals(self)
        self._save_signals.finished.connect(self._save_finished)
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(2)

        QShortcut(QKeySequence("Ctrl+Alt+R"), self, activated=self.play_selected)
        QShortcut(QKeySequence("Ctrl+Alt+S"), self, activated=self.stop_playback)
        QShortcut(
            QKeySequence("Ctrl+Alt+Left"), self, activated=lambda: self._move_group(-1)
        )
        QShortcut(
            QKeySequence("Ctrl+Alt+Right"), self, activated=lambda: self._move_group(1)
        )

        for index, group in enumerate(self.document["groups"], start=1):
            voice = group["synthesis_voice_character"]
            self.group_choice.addItem(
                f"{index}/{len(self.document['groups'])}: {voice} "
                f"({group['case_count']} failed lines)",
                group["group_id"],
            )
        self._show_group()

    def _current_group(self):
        index = self.group_choice.currentIndex()
        if index < 0:
            return None
        return self.document["groups"][index]

    def _show_group(self):
        group = self._current_group()
        self.stop_playback()
        self.candidate_choice.clear()
        self.cases.setRowCount(0)
        if group is None:
            return
        for index, candidate in enumerate(group["candidates"], start=1):
            self.candidate_choice.addItem(
                f"Candidate {index} of {len(group['candidates'])}",
                candidate["candidate_id"],
            )
        self.cases.setRowCount(len(group["cases"]))
        for row, case in enumerate(group["cases"]):
            for column, value in enumerate(
                (case["line_id"], case["speaker"], case["text"])
            ):
                self.cases.setItem(row, column, QTableWidgetItem(str(value)))
        decision = self.decisions.get(group["group_id"])
        decision_text = decision["decision"] if decision is not None else "not decided"
        completed = len(self.decisions)
        self.summary.setText(
            f"Reference group {self.group_choice.currentIndex() + 1}/"
            f"{self.group_choice.count()} | {completed}/{self.group_choice.count()} "
            f"decided | Current decision: {decision_text}.\n"
            "Listen to the opaque candidates, then choose the best exact reference "
            "or Neither. This records reference evidence only."
        )
        self._update_actions()

    def play_selected(self):
        group = self._current_group()
        candidate_id = self.candidate_choice.currentData()
        if group is None or not isinstance(candidate_id, str):
            return
        self.stop_playback()
        self._playback_serial += 1
        serial = self._playback_serial
        self._playback_active = True
        self.status.setText("Loading and verifying copied reference audio...")
        self.thread_pool.start(
            _Task(
                serial,
                self.audio_preparer,
                (self.audit.directory, group["group_id"], candidate_id),
                self._playback_signals,
            )
        )
        self._update_actions()

    def _playback_finished(self, serial, audio, error):
        if serial != self._playback_serial:
            return
        self._playback_active = False
        if error is not None:
            self.status.setText(f"BLOCKED: {error}")
            self._update_actions()
            return
        playback = QBuffer(self)
        playback.setData(QByteArray(audio.payload))
        if not playback.open(QIODevice.OpenModeFlag.ReadOnly):
            self.status.setText("BLOCKED: unable to open immutable audio buffer")
            self._update_actions()
            return
        self._playback_buffer = playback
        self._playback_target = (audio.group_id, audio.candidate_id, audio.sha256)
        self.player.setSourceDevice(playback, QUrl(f"memory:{audio.path.name}"))
        self.player.play()
        self.status.setText(
            f"PLAYING: {self.candidate_choice.currentText()} (checksum verified)"
        )
        self._update_actions()

    def stop_playback(self):
        self.player.stop() if hasattr(self, "player") else None
        self._playback_buffer = None
        self._playback_target = None
        if hasattr(self, "play"):
            self._update_actions()

    def choose_selected(self):
        candidate_id = self.candidate_choice.currentData()
        if isinstance(candidate_id, str):
            self.save_decision(candidate_id)

    def save_decision(self, decision):
        group = self._current_group()
        if group is None or self._save_active:
            return
        self._save_serial += 1
        serial = self._save_serial
        self._save_active = True
        self.status.setText(
            "Saving checksum-bound reference decision in the background; playback "
            "remains available."
        )
        self.thread_pool.start(
            _Task(
                serial,
                self.decision_recorder,
                (self.audit.directory, group["group_id"], decision),
                self._save_signals,
            )
        )
        self._update_actions()

    def _save_finished(self, serial, document, error):
        if serial != self._save_serial:
            return
        self._save_active = False
        if error is not None:
            self.status.setText(f"BLOCKED: decision was not saved: {error}")
            self._update_actions()
            return
        self.decisions = {value["group_id"]: value for value in document["decisions"]}
        self.status.setText(
            "SAVED: reference evidence recorded. No generation state was changed."
        )
        current = self.group_choice.currentIndex()
        undecided = [
            index
            for index, group in enumerate(self.document["groups"])
            if group["group_id"] not in self.decisions
        ]
        if undecided:
            later = [index for index in undecided if index > current]
            self.group_choice.setCurrentIndex(later[0] if later else undecided[0])
        else:
            self._show_group()

    def _move_group(self, offset):
        count = self.group_choice.count()
        if count:
            self.group_choice.setCurrentIndex(
                (self.group_choice.currentIndex() + offset) % count
            )

    def _media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.status.setText(
                "HEARD: choose this candidate, replay another, or choose Neither."
            )
            self._playback_buffer = None
            self._playback_target = None
            self._update_actions()

    def _media_error(self, _error, error_string):
        if error_string:
            self.status.setText(f"BLOCKED: audio playback failed: {error_string}")
        self._playback_buffer = None
        self._playback_target = None
        self._update_actions()

    def _update_actions(self):
        has_group = self._current_group() is not None
        has_candidate = has_group and self.candidate_choice.currentIndex() >= 0
        self.play.setEnabled(has_candidate and not self._playback_active)
        self.stop.setEnabled(self._playback_buffer is not None)
        self.choose.setEnabled(has_candidate and not self._save_active)
        self.neither.setEnabled(has_group and not self._save_active)
        self.previous.setEnabled(self.group_choice.count() > 1)
        self.next.setEnabled(self.group_choice.count() > 1)

    def closeEvent(self, event: QCloseEvent):
        if self._playback_active or self._save_active:
            self.status.setText(
                "Close deferred until the current checksum-bound task finishes."
            )
            event.ignore()
            return
        self.stop_playback()
        event.accept()


def launch_failure_reference_audit(audit_directory):
    application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = FailureReferenceAuditDialog(audit_directory)
    except Exception as error:
        QMessageBox.critical(None, "Unable to open failed-reference audit", str(error))
        return 1
    dialog.show()
    return application.exec()


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: vntts-reference-audit AUDIT_DIRECTORY", file=sys.stderr)
        return 2
    return launch_failure_reference_audit(Path(arguments[0]))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FailureReferenceAuditDialog",
    "launch_failure_reference_audit",
    "main",
]

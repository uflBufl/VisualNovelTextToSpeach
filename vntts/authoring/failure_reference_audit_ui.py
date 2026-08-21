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
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
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
        self._heard_candidates = {}

        self.setWindowTitle("VNTTS failed-reference audit")
        self.setMinimumSize(720, 460)
        self.resize(900, 560)
        self.heading = QLabel("Choose the best source reference for failed speech")
        self.heading.setAccessibleName("Failed-reference review task")
        self.heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.heading.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, len(self.document["groups"]))
        self.progress.setAccessibleName("Reference groups decided")
        self.progress.setFormat("%v of %m groups decided")
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
        self.candidate_choice.currentIndexChanged.connect(self._candidate_changed)

        self.candidate_heading = QLabel()
        self.candidate_heading.setAccessibleName("Current blinded candidate")
        self.candidate_heading.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.candidate_heard = QLabel()
        self.candidate_heard.setAccessibleName("Candidate listening progress")
        self.candidate_heard.setWordWrap(True)

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
        self.technical_details = QCheckBox("Show affected failed lines")
        self.technical_details.setAccessibleName("Show affected failed line details")
        self.technical_details.toggled.connect(self.cases.setVisible)
        self.cases.setVisible(False)

        self.play = QPushButton("Play selected candidate")
        self.stop = QPushButton("Stop")
        self.choose = QPushButton("Use selected candidate")
        self.neither = QPushButton("Neither candidate is acceptable")
        self.previous = QPushButton("Previous group")
        self.next = QPushButton("Next group")
        self.action_reason = QLabel()
        self.action_reason.setAccessibleName("Reference decision availability")
        self.action_reason.setWordWrap(True)
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
        decisions_row.addStretch()
        decisions_row.addWidget(self.choose)
        decisions_row.addWidget(self.neither)
        decisions_row.addStretch()
        decisions_row.addWidget(self.next)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.progress)
        layout.addWidget(self.summary)
        layout.addWidget(self.status)
        layout.addWidget(self.group_choice)
        layout.addWidget(self.candidate_heading)
        layout.addWidget(self.candidate_heard)
        layout.addLayout(playback)
        layout.addWidget(self.action_reason)
        layout.addLayout(decisions_row)
        layout.addWidget(self.technical_details)
        layout.addWidget(self.cases, 1)
        layout.addWidget(buttons)

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

        self.setTabOrder(self.group_choice, self.candidate_choice)
        self.setTabOrder(self.candidate_choice, self.play)
        self.setTabOrder(self.play, self.stop)
        self.setTabOrder(self.stop, self.choose)
        self.setTabOrder(self.choose, self.neither)
        self.setTabOrder(self.neither, self.previous)
        self.setTabOrder(self.previous, self.next)
        self.setTabOrder(self.next, self.technical_details)

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
        self.candidate_choice.blockSignals(True)
        self.candidate_choice.clear()
        self.cases.setRowCount(0)
        if group is None:
            self.candidate_choice.blockSignals(False)
            return
        for index, candidate in enumerate(group["candidates"], start=1):
            self.candidate_choice.addItem(
                f"Candidate {index} of {len(group['candidates'])}",
                candidate["candidate_id"],
            )
        self.candidate_choice.blockSignals(False)
        self.cases.setRowCount(len(group["cases"]))
        for row, case in enumerate(group["cases"]):
            for column, value in enumerate(
                (case["line_id"], case["speaker"], case["text"])
            ):
                self.cases.setItem(row, column, QTableWidgetItem(str(value)))
        decision = self.decisions.get(group["group_id"])
        decision_text = decision["decision"] if decision is not None else "not decided"
        completed = len(self.decisions)
        self.progress.setValue(completed)
        self.technical_details.setText(
            f"Show {len(group['cases'])} affected failed line(s)"
        )
        self.summary.setText(
            f"Reference group {self.group_choice.currentIndex() + 1}/"
            f"{self.group_choice.count()} | {completed}/{self.group_choice.count()} "
            f"decided | Current decision: {decision_text}.\n"
            "This records reference evidence only; it does not approve generated speech."
        )
        self._update_candidate_card()
        self._update_actions()

    def _candidate_changed(self):
        self.stop_playback()
        self._update_candidate_card()
        self._update_actions()

    def _candidate_position(self):
        index = self.candidate_choice.currentIndex()
        return (index + 1, self.candidate_choice.count())

    def _current_heard_candidates(self):
        group = self._current_group()
        if group is None:
            return set()
        return self._heard_candidates.setdefault(group["group_id"], set())

    def _all_current_candidates_heard(self):
        group = self._current_group()
        if group is None:
            return False
        expected = {candidate["candidate_id"] for candidate in group["candidates"]}
        return expected.issubset(self._current_heard_candidates())

    def _update_candidate_card(self):
        group = self._current_group()
        candidate_id = self.candidate_choice.currentData()
        if group is None or not isinstance(candidate_id, str):
            self.candidate_heading.setText("No candidate selected")
            self.candidate_heard.clear()
            return
        position, total = self._candidate_position()
        heard = self._current_heard_candidates()
        self.candidate_heading.setText(f"Candidate {position} of {total}")
        state = "heard" if candidate_id in heard else "not heard"
        self.candidate_heard.setText(
            f"Current candidate: {state}. Group listening progress: "
            f"{len(heard)}/{total}. Listen through every candidate before deciding."
        )
        self.choose.setText(f"Use Candidate {position}")

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
        group = self._current_group()
        if (
            group is None
            or group["group_id"] != audio.group_id
            or self.candidate_choice.currentData() != audio.candidate_id
        ):
            self.status.setText(
                "BLOCKED: candidate selection changed while audio was prepared"
            )
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
        candidate = next(
            (
                (index, value)
                for index, value in enumerate(group["candidates"], 1)
                if value["candidate_id"] == audio.candidate_id
            ),
            None,
        )
        candidate_label = (
            f"Candidate {candidate[0]} of {len(group['candidates'])}"
            if candidate is not None
            else "checksum-bound candidate"
        )
        self.status.setText(f"PLAYING: {candidate_label} (checksum verified)")
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
        if not self._all_current_candidates_heard():
            heard = len(self._current_heard_candidates())
            total = len(group["candidates"])
            self.status.setText(
                f"BLOCKED: listen through every candidate first ({heard}/{total} heard)."
            )
            self._update_actions()
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
        if (
            status == QMediaPlayer.MediaStatus.EndOfMedia
            and self._playback_target is not None
        ):
            group_id, candidate_id, _sha256 = self._playback_target
            self._heard_candidates.setdefault(group_id, set()).add(candidate_id)
            self.status.setText(
                "HEARD: choose this candidate, replay another, or choose Neither."
            )
            self._playback_buffer = None
            self._playback_target = None
            self._update_candidate_card()
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
        all_heard = has_group and self._all_current_candidates_heard()
        self.play.setEnabled(has_candidate and not self._playback_active)
        self.stop.setEnabled(self._playback_buffer is not None)
        self.choose.setEnabled(has_candidate and all_heard and not self._save_active)
        self.neither.setEnabled(has_group and all_heard and not self._save_active)
        navigation_enabled = self.group_choice.count() > 1 and not self._save_active
        self.previous.setEnabled(navigation_enabled)
        self.next.setEnabled(navigation_enabled)
        self.group_choice.setEnabled(not self._save_active)
        if self._save_active:
            self.action_reason.setText(
                "Saving the group decision. Playback remains available; group "
                "navigation waits for the authoritative write."
            )
        elif has_group and not all_heard:
            heard = len(self._current_heard_candidates())
            total = len(self._current_group()["candidates"])
            self.action_reason.setText(
                f"Decision locked: listen through every candidate ({heard}/{total} heard)."
            )
        elif has_group:
            self.action_reason.setText(
                "All candidates heard. Choose the best candidate or Neither acceptable."
            )
        else:
            self.action_reason.setText("No reference group is available.")

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

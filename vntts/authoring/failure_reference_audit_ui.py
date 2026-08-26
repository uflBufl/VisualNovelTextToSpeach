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
from vntts.authoring.failure_reference_preview import (
    FailureReferencePreviewCancelled,
    FailureReferencePreviewService,
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
        preview_service_factory=FailureReferencePreviewService,
    ):
        super().__init__(parent)
        self.audit, self.document, decisions = _load_public_document(audit)
        self.audio_preparer = audio_preparer
        self.decision_recorder = decision_recorder
        self.preview_service = preview_service_factory(self.audit.directory)
        self.decisions = {value["group_id"]: value for value in decisions["decisions"]}
        self._playback_serial = 0
        self._save_serial = 0
        self._playback_active = False
        self._save_active = False
        self._preview_serial = 0
        self._preview_active = False
        self._preview_result = None
        self._playback_buffer = None
        self._playback_target = None
        self._playback_kind = None
        self._heard_candidates = {}

        self.setWindowTitle("VNTTS failed-reference audit")
        self.setMinimumSize(720, 460)
        self.resize(900, 560)
        self.heading = QLabel("Choose a source recording for voice generation")
        self.heading.setAccessibleName("Failed-reference review task")
        self.heading.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.heading.setWordWrap(True)
        self.explanation = QLabel(
            "This task selects voice-cloning source audio. It does not approve or "
            "reject a character or generated line. Listen for the correct speaker, "
            "one clear voice, natural pacing, enough clean speech and little noise. "
            "The optional generated sample lets you hear this candidate through the "
            "workspace's current model before deciding."
        )
        self.explanation.setAccessibleName("Reference selection explanation")
        self.explanation.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, len(self.document["groups"]))
        self.progress.setAccessibleName("Reference groups decided")
        self.progress.setFormat("%v of %m groups decided")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.status = QLabel(
            "READY: compare source audio or generate a non-authoritative voice sample."
        )
        self.status.setWordWrap(True)
        self.group_choice = QComboBox()
        self.group_choice.setAccessibleName("Failed-reference control group")
        self.candidate_choice = QComboBox()
        self.candidate_choice.setAccessibleName("Blinded reference candidate")
        self.group_choice.currentIndexChanged.connect(self._show_group)
        self.candidate_choice.currentIndexChanged.connect(self._candidate_changed)
        self.preview_text_choice = QComboBox()
        self.preview_text_choice.setAccessibleName("Generated preview phrase")
        self.preview_text_choice.setAccessibleDescription(
            "Choose one affected line to synthesize with the selected reference"
        )
        self.preview_text_choice.currentIndexChanged.connect(self._preview_text_changed)

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
        self.generate_preview = QPushButton("Generate voice sample")
        self.replay_preview = QPushButton("Replay generated sample")
        self.cancel_preview = QPushButton("Cancel generation")
        self.generate_preview.setAccessibleDescription(
            "Render the selected affected phrase with this reference without saving "
            "authoring state or making a reference decision"
        )
        self.replay_preview.setAccessibleDescription(
            "Replay the current dialog-lifetime generated sample from immutable bytes"
        )
        self.cancel_preview.setAccessibleDescription(
            "Cancel only the active optional reference-preview render"
        )
        self.choose = QPushButton("Use selected candidate")
        self.neither = QPushButton("None of these references is suitable")
        self.choose.setAccessibleDescription(
            "Select this source recording for later explicit voice binding"
        )
        self.neither.setAccessibleDescription(
            "Record that the available source recordings are unsuitable; this does "
            "not reject the character"
        )
        self.previous = QPushButton("Previous group")
        self.next = QPushButton("Next group")
        self.action_reason = QLabel()
        self.action_reason.setAccessibleName("Reference decision availability")
        self.action_reason.setWordWrap(True)
        self.play.clicked.connect(self.play_selected)
        self.stop.clicked.connect(self.stop_playback)
        self.generate_preview.clicked.connect(self.generate_selected_preview)
        self.replay_preview.clicked.connect(self.replay_generated_preview)
        self.cancel_preview.clicked.connect(self.cancel_preview_generation)
        self.choose.clicked.connect(self.choose_selected)
        self.neither.clicked.connect(lambda: self.save_decision("neither_acceptable"))
        self.previous.clicked.connect(lambda: self._move_group(-1))
        self.next.clicked.connect(lambda: self._move_group(1))

        playback = QHBoxLayout()
        playback.addWidget(self.candidate_choice, 1)
        playback.addWidget(self.play)
        playback.addWidget(self.stop)
        preview = QHBoxLayout()
        preview.addWidget(self.preview_text_choice, 1)
        preview.addWidget(self.generate_preview)
        preview.addWidget(self.replay_preview)
        preview.addWidget(self.cancel_preview)
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
        layout.addWidget(self.explanation)
        layout.addWidget(self.progress)
        layout.addWidget(self.summary)
        layout.addWidget(self.status)
        layout.addWidget(self.group_choice)
        layout.addWidget(self.candidate_heading)
        layout.addWidget(self.candidate_heard)
        layout.addLayout(playback)
        layout.addLayout(preview)
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
        self._preview_signals = _TaskSignals(self)
        self._preview_signals.finished.connect(self._preview_finished)
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(2)

        QShortcut(QKeySequence("Ctrl+Alt+R"), self, activated=self.play_selected)
        QShortcut(QKeySequence("Ctrl+Alt+S"), self, activated=self.stop_playback)
        QShortcut(
            QKeySequence("Ctrl+Alt+G"), self, activated=self.generate_selected_preview
        )
        QShortcut(
            QKeySequence("Ctrl+Alt+P"), self, activated=self.replay_generated_preview
        )
        QShortcut(
            QKeySequence("Ctrl+Alt+Left"), self, activated=lambda: self._move_group(-1)
        )
        QShortcut(
            QKeySequence("Ctrl+Alt+Right"), self, activated=lambda: self._move_group(1)
        )

        self.setTabOrder(self.group_choice, self.candidate_choice)
        self.setTabOrder(self.candidate_choice, self.play)
        self.setTabOrder(self.play, self.stop)
        self.setTabOrder(self.stop, self.preview_text_choice)
        self.setTabOrder(self.preview_text_choice, self.generate_preview)
        self.setTabOrder(self.generate_preview, self.replay_preview)
        self.setTabOrder(self.replay_preview, self.cancel_preview)
        self.setTabOrder(self.cancel_preview, self.choose)
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
        self.preview_text_choice.blockSignals(True)
        self.preview_text_choice.clear()
        self.cases.setRowCount(0)
        if group is None:
            self.candidate_choice.blockSignals(False)
            self.preview_text_choice.blockSignals(False)
            return
        for index, candidate in enumerate(group["candidates"], start=1):
            self.candidate_choice.addItem(
                f"Candidate {index} of {len(group['candidates'])}",
                candidate["candidate_id"],
            )
        self.candidate_choice.blockSignals(False)
        for case in group["cases"]:
            text = str(case["text"])
            summary = " ".join(text.split())
            if len(summary) > 92:
                summary = summary[:89].rstrip() + "..."
            self.preview_text_choice.addItem(
                f"{case['line_id']}: {summary}",
                text,
            )
        if group["cases"]:
            shortest = min(
                range(len(group["cases"])),
                key=lambda index: (
                    len(str(group["cases"][index]["text"]).split()),
                    len(str(group["cases"][index]["text"])),
                    str(group["cases"][index]["queue_id"]),
                ),
            )
            self.preview_text_choice.setCurrentIndex(shortest)
        self.preview_text_choice.blockSignals(False)
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
            f"decided | Voice target: {group['synthesis_voice_character']} | "
            f"Current decision: {decision_text}.\n"
            "This records reference evidence only; it does not approve generated speech."
        )
        self._update_candidate_card()
        self._update_actions()

    def _candidate_changed(self):
        self.stop_playback()
        self._update_candidate_card()
        self._update_actions()

    def _preview_text_changed(self):
        self.stop_playback()
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
        if total == 1:
            self.choose.setText("Use this reference")
            self.neither.setText("This reference is unsuitable")
        else:
            self.choose.setText(f"Use Candidate {position}")
            self.neither.setText("None of these references is suitable")

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
        self._playback_kind = "reference"
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
        self._playback_kind = None
        if hasattr(self, "play"):
            self._update_actions()

    def generate_selected_preview(self):
        group = self._current_group()
        candidate_id = self.candidate_choice.currentData()
        text = self.preview_text_choice.currentData()
        if (
            group is None
            or not isinstance(candidate_id, str)
            or not isinstance(text, str)
            or self._preview_active
        ):
            return
        self.stop_playback()
        self._preview_serial += 1
        serial = self._preview_serial
        self._preview_active = True
        self.status.setText(
            "GENERATING: loading the workspace model if needed and rendering one "
            "deterministic sample in the background. No authoring state is written."
        )
        self.thread_pool.start(
            _Task(
                serial,
                self.preview_service.generate,
                (group["group_id"], candidate_id, text),
                self._preview_signals,
            )
        )
        self._update_actions()

    def cancel_preview_generation(self):
        if not self._preview_active:
            return
        self.preview_service.cancel()
        self.status.setText(
            "CANCELLING: waiting for the preview worker to stop safely."
        )
        self._update_actions()

    def _preview_finished(self, serial, preview, error):
        if serial != self._preview_serial:
            return
        self._preview_active = False
        if error is not None:
            if isinstance(error, FailureReferencePreviewCancelled):
                self.status.setText(
                    "CANCELLED: no generated preview or authoring state was saved."
                )
            else:
                self.status.setText(f"BLOCKED: generated preview failed: {error}")
            self._update_actions()
            return
        self._preview_result = preview
        group = self._current_group()
        if (
            group is None
            or group["group_id"] != preview.group_id
            or self.candidate_choice.currentData() != preview.candidate_id
            or self.preview_text_choice.currentData() != preview.text
        ):
            self.status.setText(
                "PREVIEW READY: cached for the earlier candidate/phrase. Return to "
                "that selection and generate again to replay it instantly."
            )
            self._update_actions()
            return
        self._play_generated_preview(preview)

    def replay_generated_preview(self):
        if self._preview_matches_selection():
            self._play_generated_preview(self._preview_result)

    def _play_generated_preview(self, preview):
        self.stop_playback()
        playback = QBuffer(self)
        playback.setData(QByteArray(preview.payload))
        if not playback.open(QIODevice.OpenModeFlag.ReadOnly):
            self.status.setText("BLOCKED: unable to open immutable generated preview")
            self._update_actions()
            return
        self._playback_buffer = playback
        self._playback_target = (
            preview.group_id,
            preview.candidate_id,
            preview.audio_sha256,
        )
        self._playback_kind = "generated"
        self.player.setSourceDevice(playback, QUrl("memory:generated-preview.wav"))
        self.player.play()
        self.status.setText(
            f"PLAYING GENERATED SAMPLE: {preview.backend}, "
            f"{preview.generation_profile}, seed {preview.seed}. This is optional "
            "evidence and does not select the reference."
        )
        self._update_actions()

    def _preview_matches_selection(self):
        preview = self._preview_result
        group = self._current_group()
        return bool(
            preview is not None
            and group is not None
            and preview.group_id == group["group_id"]
            and preview.candidate_id == self.candidate_choice.currentData()
            and preview.text == self.preview_text_choice.currentData()
        )

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
            if self._playback_kind == "generated":
                self.status.setText(
                    "GENERATED SAMPLE HEARD: replay it, compare the source, or make "
                    "the separate reference decision."
                )
                self._playback_buffer = None
                self._playback_target = None
                self._playback_kind = None
                self._update_actions()
                return
            group_id, candidate_id, _sha256 = self._playback_target
            self._heard_candidates.setdefault(group_id, set()).add(candidate_id)
            self.status.setText(
                "HEARD: choose this candidate, replay another, or choose Neither."
            )
            self._playback_buffer = None
            self._playback_target = None
            self._playback_kind = None
            self._update_candidate_card()
            self._update_actions()

    def _media_error(self, _error, error_string):
        if error_string:
            self.status.setText(f"BLOCKED: audio playback failed: {error_string}")
        self._playback_buffer = None
        self._playback_target = None
        self._playback_kind = None
        self._update_actions()

    def _update_actions(self):
        has_group = self._current_group() is not None
        has_candidate = has_group and self.candidate_choice.currentIndex() >= 0
        all_heard = has_group and self._all_current_candidates_heard()
        self.play.setEnabled(has_candidate and not self._playback_active)
        self.stop.setEnabled(self._playback_buffer is not None)
        self.generate_preview.setEnabled(has_candidate and not self._preview_active)
        self.replay_preview.setEnabled(
            self._preview_matches_selection() and not self._preview_active
        )
        self.cancel_preview.setEnabled(self._preview_active)
        self.choose.setEnabled(has_candidate and all_heard and not self._save_active)
        self.neither.setEnabled(has_group and all_heard and not self._save_active)
        navigation_enabled = self.group_choice.count() > 1 and not self._save_active
        self.previous.setEnabled(navigation_enabled)
        self.next.setEnabled(navigation_enabled)
        self.group_choice.setEnabled(not self._save_active)
        if self._preview_active:
            self.action_reason.setText(
                "Optional generated sample is running in the background. Source "
                "playback and the separate reference decision remain available."
            )
        elif self._save_active:
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
                "All source candidates heard. Select the best source reference or "
                "declare that none is suitable. Generated samples are optional evidence."
            )
        else:
            self.action_reason.setText("No reference group is available.")

    def closeEvent(self, event: QCloseEvent):
        if self._playback_active or self._save_active or self._preview_active:
            self.status.setText(
                "Close deferred until the current checksum-bound task finishes."
            )
            event.ignore()
            return
        self.stop_playback()
        self.preview_service.close()
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


def failure_reference_audit_status(audit_directory):
    """Return validated progress without creating Qt state or writing decisions."""
    audit, document, decisions = _load_public_document(audit_directory)
    completed = len(decisions["decisions"])
    total = len(document["groups"])
    return {
        "audit": str(audit.directory),
        "audit_id": audit.audit_id,
        "completed_groups": completed,
        "remaining_groups": total - completed,
        "total_groups": total,
        "decision_set_id": decisions["decision_set_id"],
    }


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(value in {"-h", "--help"} for value in arguments):
        print("usage: vntts-reference-audit AUDIT_DIRECTORY [--status]")
        return 0
    status = "--status" in arguments
    if status:
        arguments.remove("--status")
    if len(arguments) != 1 or any(value.startswith("-") for value in arguments):
        print(
            "usage: vntts-reference-audit AUDIT_DIRECTORY [--status]",
            file=sys.stderr,
        )
        return 2
    if status:
        try:
            progress = failure_reference_audit_status(Path(arguments[0]))
        except Exception as error:
            print(f"Unable to inspect failed-reference audit: {error}", file=sys.stderr)
            return 1
        print(json.dumps(progress, indent=2, sort_keys=True))
        return 0
    return launch_failure_reference_audit(Path(arguments[0]))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FailureReferenceAuditDialog",
    "failure_reference_audit_status",
    "launch_failure_reference_audit",
    "main",
]

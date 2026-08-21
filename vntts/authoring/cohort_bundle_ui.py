"""Qt operator interface for checksum-bound multi-workspace cohort review."""

from __future__ import annotations

import hashlib
import sys
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QRunnable,
    Qt,
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

from vntts.authoring.cohort_bundle import (
    CohortBundleSample,
    CohortReviewBundle,
    execute_cohort_bundle_decision,
    load_cohort_review_bundle,
    load_cohort_review_bundle_samples,
)
from vntts.authoring.workbench import prepare_review_audio, review_technical_summary


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


def _prepare_sample(sample):
    return sample, prepare_review_audio(sample.item)


def _execute_bundle_decision_task(
    bundle,
    workspace_id,
    cohort_id,
    decision,
    reviewed,
    assessments,
    next_clean_samples_per_bucket,
):
    return execute_cohort_bundle_decision(
        bundle,
        workspace_id,
        cohort_id,
        decision,
        reviewed_queue_ids=reviewed,
        sample_assessments=assessments,
        next_clean_samples_per_bucket=next_clean_samples_per_bucket,
    )


class CohortReviewBundleDialog(QDialog):
    """One non-blocking review surface over several immutable workspaces."""

    def __init__(
        self,
        bundle,
        parent=None,
        *,
        sample_loader=load_cohort_review_bundle_samples,
        playback_preparer=_prepare_sample,
        decision_executor=_execute_bundle_decision_task,
        confirmer=None,
    ):
        super().__init__(parent)
        self.bundle = (
            load_cohort_review_bundle(bundle)
            if not isinstance(bundle, CohortReviewBundle)
            else bundle
        )
        self.sample_loader = sample_loader
        self.playback_preparer = playback_preparer
        self.decision_executor = decision_executor
        self.confirmer = confirmer or self._confirm_decision
        self.samples = ()
        self.samples_by_cohort = {}
        self.heard = defaultdict(set)
        self.bad = defaultdict(set)
        self._load_active = False
        self._playback_prepare_active = False
        self._decision_active = False
        self._load_serial = 0
        self._playback_serial = 0
        self._decision_serial = 0
        self._playback_target = None
        self._playback_buffer = None

        self.setWindowTitle("VNTTS specialist cohort review")
        self.resize(1180, 720)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.status = QLabel("Loading exact review authorities...")
        self.status.setWordWrap(True)
        self.cohort_choice = QComboBox()
        self.cohort_choice.setAccessibleName("Specialist review cohort")
        self.cohort_choice.currentIndexChanged.connect(self._show_current_cohort)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "Heard",
                "Assessment",
                "Source speaker",
                "Effective voice",
                "Required because",
                "Quality",
                "Line",
                "Text",
            ]
        )
        self.table.setAccessibleName("Checksum-bound specialist review samples")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            7, QHeaderView.ResizeMode.Stretch
        )
        for column, width in enumerate((65, 100, 120, 130, 240, 220, 190)):
            self.table.setColumnWidth(column, width)
        self.table.itemSelectionChanged.connect(self._selection_changed)

        self.previous = QPushButton("Previous sample")
        self.replay = QPushButton("Replay selected sample")
        self.stop = QPushButton("Stop sample")
        self.next = QPushButton("Next sample")
        self.mark_bad = QPushButton("Mark sample bad")
        self.need_another = QPushButton("Need another sample")
        self.accept = QPushButton("Accept cohort")
        self.reject = QPushButton("Reject cohort")
        self.retry_load = QPushButton("Retry bundle load")
        self.previous.clicked.connect(lambda: self._move(-1))
        self.replay.clicked.connect(self.play_selected)
        self.stop.clicked.connect(self.stop_playback)
        self.next.clicked.connect(lambda: self._move(1))
        self.mark_bad.clicked.connect(self.toggle_bad)
        self.need_another.clicked.connect(lambda: self.apply_decision("expand"))
        self.accept.clicked.connect(lambda: self.apply_decision("accepted"))
        self.reject.clicked.connect(lambda: self.apply_decision("rejected"))
        self.retry_load.clicked.connect(self.reload_bundle)

        navigation = QHBoxLayout()
        for widget in (self.previous, self.replay, self.stop, self.next):
            navigation.addWidget(widget)
        decisions = QHBoxLayout()
        for widget in (
            self.mark_bad,
            self.need_another,
            self.accept,
            self.reject,
            self.retry_load,
        ):
            decisions.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.status)
        layout.addWidget(self.cohort_choice)
        layout.addWidget(self.table, 1)
        layout.addLayout(navigation)
        layout.addLayout(decisions)

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(self._media_error)

        self._load_signals = _TaskSignals(self)
        self._load_signals.finished.connect(self._load_finished)
        self._playback_signals = _TaskSignals(self)
        self._playback_signals.finished.connect(self._playback_finished)
        self._decision_signals = _TaskSignals(self)
        self._decision_signals.finished.connect(self._decision_finished)
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(2)

        QShortcut(QKeySequence("Ctrl+Alt+Left"), self, activated=lambda: self._move(-1))
        QShortcut(QKeySequence("Ctrl+Alt+R"), self, activated=self.play_selected)
        QShortcut(QKeySequence("Ctrl+Alt+S"), self, activated=self.stop_playback)
        QShortcut(QKeySequence("Ctrl+Alt+Right"), self, activated=lambda: self._move(1))
        QShortcut(
            QKeySequence("Ctrl+Return"),
            self,
            activated=lambda: self.apply_decision("accepted"),
        )
        QShortcut(
            QKeySequence("Ctrl+Backspace"),
            self,
            activated=lambda: self.apply_decision("rejected"),
        )
        self.reload_bundle()

    def reload_bundle(self):
        if self._load_active or self._decision_active:
            return
        self._load_active = True
        self._load_serial += 1
        serial = self._load_serial
        self.status.setText("Loading and checksum-validating all bundle sources...")
        self._update_actions()
        self.thread_pool.start(
            _Task(serial, self.sample_loader, (self.bundle,), self._load_signals)
        )

    def _load_finished(self, serial, result, error):
        if serial != self._load_serial or not self._load_active:
            return
        self._load_active = False
        if error is not None:
            self.samples = ()
            self.samples_by_cohort = {}
            self.status.setText(f"BLOCKED: {error}")
            self._populate_cohorts()
            self._update_actions()
            return
        self.bundle, self.samples = result
        grouped = defaultdict(list)
        for sample in self.samples:
            grouped[(sample.workspace_id, sample.cohort_id)].append(sample)
        self.samples_by_cohort = {key: tuple(values) for key, values in grouped.items()}
        valid = {
            (sample.workspace_id, sample.cohort_id, sample.item.queue_id)
            for sample in self.samples
        }
        self.heard = defaultdict(
            set,
            {
                key: {value for value in values if (*key, value) in valid}
                for key, values in self.heard.items()
            },
        )
        self.bad = defaultdict(
            set,
            {
                key: {value for value in values if (*key, value) in valid}
                for key, values in self.bad.items()
            },
        )
        self.summary.setText(
            f"{self.bundle.document['workspace_count']} workspaces | "
            f"{self.bundle.document['cohort_count']} cohorts | "
            f"{self.bundle.document['sample_item_count']} required samples for "
            f"{self.bundle.document['pending_item_count']} pending WAVs | "
            f"{self.bundle.document['blocked_item_count']} unique inherited blocked "
            "items excluded"
        )
        self.status.setText("READY: select a cohort and play its exact samples")
        self._populate_cohorts()

    def _populate_cohorts(self):
        previous = self.cohort_choice.currentData()
        self.cohort_choice.blockSignals(True)
        self.cohort_choice.clear()
        for cohort in self.bundle.document["cohorts"]:
            key = (cohort["workspace_id"], cohort["cohort_id"])
            if key not in self.samples_by_cohort:
                continue
            identity = cohort["identity"]
            self.cohort_choice.addItem(
                f"{identity['provider']} | {identity['voice_character']} | "
                f"{len(cohort['samples'])} samples / {cohort['item_count']} WAVs | "
                f"{cohort['workspace_id'][-8:]}",
                key,
            )
        index = self.cohort_choice.findData(previous)
        self.cohort_choice.setCurrentIndex(index if index >= 0 else 0)
        self.cohort_choice.blockSignals(False)
        self._show_current_cohort()

    def _current_key(self):
        value = self.cohort_choice.currentData()
        return tuple(value) if isinstance(value, (tuple, list)) else None

    def _current_samples(self):
        return self.samples_by_cohort.get(self._current_key(), ())

    def _show_current_cohort(self, *_arguments):
        self.stop_playback()
        samples = self._current_samples()
        self.table.setRowCount(len(samples))
        key = self._current_key()
        for row, sample in enumerate(samples):
            item = sample.item
            values = (
                "yes" if item.queue_id in self.heard[key] else "no",
                "bad"
                if item.queue_id in self.bad[key]
                else "acceptable"
                if item.queue_id in self.heard[key]
                else "not heard",
                item.speaker,
                item.voice_character,
                sample.required_reason,
                review_technical_summary(item),
                item.line_id,
                item.text,
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, sample)
        if samples:
            self.table.selectRow(0)
        self._update_actions()

    def _selected_sample(self):
        row = self.table.currentRow()
        if row < 0 or row >= self.table.rowCount():
            return None
        cell = self.table.item(row, 0)
        value = cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None
        return value if isinstance(value, CohortBundleSample) else None

    def _selection_changed(self):
        if self._playback_target is not None:
            self.stop_playback()
        self._update_actions()

    def _move(self, offset):
        count = self.table.rowCount()
        if not count:
            return
        row = self.table.currentRow()
        self.table.selectRow((max(row, 0) + offset) % count)

    def play_selected(self):
        if self._playback_prepare_active:
            return
        sample = self._selected_sample()
        if sample is None:
            self.status.setText("Select one sample to replay")
            return
        self._playback_prepare_active = True
        self._playback_serial += 1
        serial = self._playback_serial
        self.status.setText(f"Preparing exact WAV: {sample.item.line_id}")
        self._update_actions()
        self.thread_pool.start(
            _Task(
                serial,
                self.playback_preparer,
                (sample,),
                self._playback_signals,
            )
        )

    def _playback_finished(self, serial, result, error):
        if serial != self._playback_serial or not self._playback_prepare_active:
            return
        self._playback_prepare_active = False
        if error is not None:
            self.status.setText(f"REPLAY BLOCKED: {error}")
            self._update_actions()
            return
        sample, audio_bytes = result
        selected = self._selected_sample()
        if selected != sample or selected.item.authority is None:
            self.status.setText("REPLAY BLOCKED: sample selection changed")
            self._update_actions()
            return
        if (
            hashlib.sha256(audio_bytes).hexdigest()
            != selected.item.authority.audio_sha256
        ):
            self.status.setText("REPLAY BLOCKED: WAV checksum changed")
            self._update_actions()
            return
        self._discard_playback_buffer()
        playback = QBuffer(self)
        playback.setData(QByteArray(audio_bytes))
        if not playback.open(QIODevice.OpenModeFlag.ReadOnly):
            playback.deleteLater()
            self.status.setText("REPLAY BLOCKED: immutable audio buffer failed")
            self._update_actions()
            return
        self._playback_buffer = playback
        self._playback_target = (
            sample.workspace_id,
            sample.cohort_id,
            sample.item.queue_id,
            sample.item.authority.audio_sha256,
        )
        self.player.setSourceDevice(playback, QUrl("vntts-bundle-review.wav"))
        self.player.play()
        self.status.setText(f"PLAYING: {sample.item.line_id}")
        self._update_actions()

    def _media_status_changed(self, status):
        if (
            status != QMediaPlayer.MediaStatus.EndOfMedia
            or self._playback_target is None
        ):
            return
        workspace_id, cohort_id, queue_id, audio_sha256 = self._playback_target
        selected = self._selected_sample()
        if (
            selected is not None
            and selected.workspace_id == workspace_id
            and selected.cohort_id == cohort_id
            and selected.item.queue_id == queue_id
            and selected.item.authority is not None
            and selected.item.authority.audio_sha256 == audio_sha256
        ):
            self.heard[(workspace_id, cohort_id)].add(queue_id)
            self.status.setText(f"HEARD: {selected.item.line_id}")
        self._playback_target = None
        self._discard_playback_buffer()
        self._show_current_cohort()
        self._select_queue_id(queue_id)

    def _media_error(self, _error, message=""):
        self._playback_target = None
        self._discard_playback_buffer()
        self.status.setText("AUDIO ERROR: " + (message or self.player.errorString()))
        self._update_actions()

    def stop_playback(self):
        self.player.stop()
        self._playback_target = None
        self._discard_playback_buffer()
        self._update_actions()

    def _discard_playback_buffer(self):
        playback = self._playback_buffer
        self._playback_buffer = None
        if playback is not None:
            playback.close()
            playback.deleteLater()

    def _select_queue_id(self, queue_id):
        for row in range(self.table.rowCount()):
            sample = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if sample.item.queue_id == queue_id:
                self.table.selectRow(row)
                return

    def toggle_bad(self):
        sample = self._selected_sample()
        key = self._current_key()
        if sample is None or sample.item.queue_id not in self.heard[key]:
            return
        if sample.item.queue_id in self.bad[key]:
            self.bad[key].remove(sample.item.queue_id)
        else:
            self.bad[key].add(sample.item.queue_id)
        queue_id = sample.item.queue_id
        self._show_current_cohort()
        self._select_queue_id(queue_id)

    def apply_decision(self, decision):
        if self._decision_active:
            return
        key = self._current_key()
        samples = self._current_samples()
        if key is None or not samples:
            return
        reviewed = [
            sample.item.queue_id
            for sample in samples
            if sample.item.queue_id in self.heard[key]
        ]
        bad = self.bad[key]
        if decision in {"accepted", "expand"} and len(reviewed) != len(samples):
            self.status.setText("BLOCKED: hear every current sample first")
            return
        if decision == "accepted" and bad:
            self.status.setText("BLOCKED: clear bad markers or reject/expand")
            return
        if decision == "rejected" and not reviewed:
            self.status.setText("BLOCKED: hear at least one sample first")
            return
        cohort = next(
            value
            for value in self.bundle.document["cohorts"]
            if (value["workspace_id"], value["cohort_id"]) == key
        )
        if not self.confirmer(decision, cohort, len(reviewed), len(bad)):
            return
        source = next(
            value
            for value in self.bundle.document["sources"]
            if value["workspace_id"] == key[0]
        )
        current_clean = source["plan"]["policy"]["clean_samples_per_bucket"]
        assessments = [
            {
                "queue_id": queue_id,
                "assessment": "bad" if queue_id in bad else "acceptable",
            }
            for queue_id in reviewed
        ]
        self._decision_active = True
        self._decision_serial += 1
        serial = self._decision_serial
        self.status.setText(
            f"SAVING {decision}: audio replay and navigation remain available"
        )
        self._update_actions()
        self.thread_pool.start(
            _Task(
                serial,
                self.decision_executor,
                (
                    self.bundle,
                    key[0],
                    key[1],
                    decision,
                    reviewed,
                    assessments,
                    current_clean + 1 if decision == "expand" else None,
                ),
                self._decision_signals,
            )
        )

    def _decision_finished(self, serial, result, error):
        if serial != self._decision_serial or not self._decision_active:
            return
        self._decision_active = False
        if error is not None:
            self.status.setText(
                f"SAVE FAILED: {error}. Review evidence was not assumed."
            )
            self._update_actions()
            return
        self.bundle = result.next_bundle
        self.status.setText(
            "SAVED: source-local authority committed; refreshing exact bundle"
        )
        self.reload_bundle()

    def _confirm_decision(self, decision, cohort, reviewed, bad):
        answer = QMessageBox.question(
            self,
            f"{decision.title()} cohort",
            f"{decision.title()} {cohort['item_count']} exact WAVs from one source?\n"
            f"Heard samples: {reviewed}; marked bad: {bad}.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _update_actions(self):
        sample = self._selected_sample()
        samples = self._current_samples()
        key = self._current_key()
        heard = self.heard[key] if key is not None else set()
        bad = self.bad[key] if key is not None else set()
        ready = sample is not None and not self._load_active
        self.previous.setEnabled(ready and len(samples) > 1)
        self.replay.setEnabled(ready and not self._playback_prepare_active)
        self.stop.setEnabled(self._playback_target is not None)
        self.next.setEnabled(ready and len(samples) > 1)
        self.mark_bad.setEnabled(
            ready and sample.item.queue_id in heard and not self._decision_active
        )
        self.mark_bad.setText(
            "Clear bad marker"
            if ready and sample.item.queue_id in bad
            else "Mark sample bad"
        )
        all_heard = bool(samples) and len(heard) == len(samples)
        self.accept.setEnabled(all_heard and not bad and not self._decision_active)
        self.reject.setEnabled(bool(heard) and not self._decision_active)
        current_clean = 5
        if key is not None:
            source = next(
                (
                    value
                    for value in self.bundle.document["sources"]
                    if value["workspace_id"] == key[0]
                ),
                None,
            )
            if source is not None:
                current_clean = source["plan"]["policy"]["clean_samples_per_bucket"]
        cohort = next(
            (
                value
                for value in self.bundle.document["cohorts"]
                if key is not None
                and (value["workspace_id"], value["cohort_id"]) == key
            ),
            None,
        )
        sampled = (
            {sample["queue_id"] for sample in cohort["samples"]}
            if cohort is not None
            else set()
        )
        source_plan_cohort = None
        if key is not None and source is not None:
            source_plan_cohort = next(
                (
                    value
                    for value in source["plan"]["cohorts"]
                    if value["cohort_id"] == key[1]
                ),
                None,
            )
        has_more_clean = bool(
            source_plan_cohort
            and any(
                not item["technical_flags"] and item["queue_id"] not in sampled
                for item in source_plan_cohort["items"]
            )
        )
        self.need_another.setEnabled(
            all_heard
            and has_more_clean
            and current_clean < 5
            and not self._decision_active
        )
        self.retry_load.setEnabled(
            not self._load_active and not self.samples and not self._decision_active
        )
        if key is not None and samples:
            self.summary.setToolTip(
                f"Current cohort: {len(heard)}/{len(samples)} heard; "
                f"{len(bad)} marked bad"
            )

    def closeEvent(self, event: QCloseEvent):
        if self._load_active or self._playback_prepare_active or self._decision_active:
            self.status.setText(
                "Close deferred until the current authority task finishes"
            )
            event.ignore()
            return
        self.stop_playback()
        event.accept()


def launch_cohort_review_bundle(bundle_path):
    application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = CohortReviewBundleDialog(bundle_path)
    except Exception as error:
        QMessageBox.critical(None, "Unable to open review bundle", str(error))
        return 1
    dialog.show()
    return application.exec()


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: vntts-review-bundle BUNDLE.json", file=sys.stderr)
        return 2
    return launch_cohort_review_bundle(Path(arguments[0]))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CohortReviewBundleDialog",
    "launch_cohort_review_bundle",
    "main",
]

"""Qt operator interface for checksum-bound multi-workspace cohort review."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
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
    QGridLayout,
    QGroupBox,
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

from vntts.authoring.cohort_bundle import (
    CohortBundleSample,
    CohortReviewBundle,
    execute_cohort_bundle_decision,
    load_cohort_review_bundle,
    load_cohort_review_bundle_samples,
    load_resumable_cohort_review_bundle,
    load_resumable_cohort_review_bundle_samples,
    load_resumable_cohort_review_session,
    write_cohort_review_progress,
)
from vntts.authoring.workbench import prepare_review_audio, review_technical_summary


def _display_required_reason(reason):
    prefix = "technical-attention: "
    if reason.startswith(prefix):
        return "advisory measurement; listening decides: " + reason.removeprefix(prefix)
    return reason


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


@dataclass(frozen=True)
class _DecisionTaskResult:
    projection: object
    checkpoint_error: Exception | None
    bundle: CohortReviewBundle | None
    samples: tuple[CohortBundleSample, ...]
    refresh_error: Exception | None
    commit_seconds: float
    checkpoint_seconds: float
    refresh_seconds: float


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


def _execute_and_checkpoint_bundle_decision(
    publication,
    original,
    bundle,
    workspace_id,
    cohort_id,
    decision,
    reviewed,
    assessments,
    next_clean_samples_per_bucket,
):
    started = time.perf_counter()
    projection = _execute_bundle_decision_task(
        bundle,
        workspace_id,
        cohort_id,
        decision,
        reviewed,
        assessments,
        next_clean_samples_per_bucket,
    )
    commit_seconds = time.perf_counter() - started
    checkpoint_started = time.perf_counter()
    checkpoint_error = None
    try:
        write_cohort_review_progress(
            publication,
            original,
            projection.next_bundle,
        )
    except Exception as error:
        checkpoint_error = error
    checkpoint_seconds = time.perf_counter() - checkpoint_started
    refresh_started = time.perf_counter()
    try:
        if checkpoint_error is None:
            bundle, samples = load_cohort_review_bundle_samples(projection.next_bundle)
        else:
            _resume, bundle, samples = load_resumable_cohort_review_bundle_samples(
                publication,
                persist=False,
            )
    except Exception as error:
        return _DecisionTaskResult(
            projection,
            checkpoint_error,
            None,
            (),
            error,
            commit_seconds,
            checkpoint_seconds,
            time.perf_counter() - refresh_started,
        )
    return _DecisionTaskResult(
        projection,
        checkpoint_error,
        bundle,
        tuple(samples),
        None,
        commit_seconds,
        checkpoint_seconds,
        time.perf_counter() - refresh_started,
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
        self.bundle_path = None
        self.original_bundle = None
        if isinstance(bundle, CohortReviewBundle):
            self.bundle = bundle
        else:
            self.bundle_path = Path(bundle).expanduser().resolve()
            self.original_bundle = load_cohort_review_bundle(self.bundle_path)
            self.bundle = self.original_bundle
        self.sample_loader = sample_loader
        self.playback_preparer = playback_preparer
        self.decision_executor = decision_executor
        self._resumable_load = (
            self.bundle_path is not None
            and sample_loader is load_cohort_review_bundle_samples
        )
        self._checkpoint_decisions = (
            self.bundle_path is not None
            and decision_executor is _execute_bundle_decision_task
        )
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
        self._decision_started_at = None
        self._playback_target = None
        self._playback_buffer = None
        self._initial_cohort_count = self.bundle.document["cohort_count"]

        self.setWindowTitle("VNTTS specialist cohort review")
        self.resize(1280, 820)
        self.setMinimumSize(900, 820)
        self.heading = QLabel("Specialist voice review")
        self.heading.setObjectName("reviewHeading")
        self.heading.setAccessibleName("Specialist voice review")
        self.guide = QLabel(
            "1. Play every required sample   2. Mark any bad sample   "
            "3. Accept, reject, or request more evidence. Technical attention "
            "only selects samples for listening; it is not a rejection verdict."
        )
        self.guide.setWordWrap(True)
        self.guide.setObjectName("reviewGuide")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("reviewSummary")
        self.overall_progress = QProgressBar()
        self.overall_progress.setAccessibleName("Overall cohort review progress")
        self.overall_progress.setTextVisible(True)
        self.status = QLabel("Loading exact review authorities...")
        self.status.setWordWrap(True)
        self.status.setObjectName("reviewStatus")
        self.operation = QLabel()
        self.operation.setWordWrap(True)
        self.operation.setAccessibleName("Cohort review operation status")
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Cohort review authority progress")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.cohort_choice = QComboBox()
        self.cohort_choice.setAccessibleName("Specialist review cohort")
        self.cohort_choice.currentIndexChanged.connect(self._show_current_cohort)
        self.technical_details = QCheckBox("Show technical details")
        self.technical_details.setAccessibleName("Show cohort technical details")
        self.technical_details.toggled.connect(self._toggle_technical_details)
        self.cohort_audit = QLabel()
        self.cohort_audit.setWordWrap(True)
        self.cohort_audit.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.cohort_audit.hide()

        self.sample_position = QLabel("No sample selected")
        self.sample_position.setObjectName("samplePosition")
        self.sample_identity = QLabel()
        self.sample_identity.setWordWrap(True)
        self.sample_identity.setObjectName("sampleIdentity")
        self.sample_text = QLabel()
        self.sample_text.setWordWrap(True)
        self.sample_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.sample_text.setObjectName("sampleText")
        self.sample_text.setMinimumHeight(48)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "Heard",
                "Assessment",
                "Source label",
                "Generated role",
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
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(140)
        self.table.horizontalHeader().setSectionResizeMode(
            7, QHeaderView.ResizeMode.Stretch
        )
        for column, width in enumerate((65, 100, 120, 130, 240, 220, 190)):
            self.table.setColumnWidth(column, width)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _index: self.play_selected())

        self.previous = QPushButton("Previous sample")
        self.replay = QPushButton("Play selected sample")
        self.stop = QPushButton("Stop sample")
        self.next = QPushButton("Next sample")
        self.mark_bad = QPushButton("Sample sounds bad")
        self.need_another = QPushButton("Need more evidence")
        self.accept = QPushButton("Accept cohort")
        self.accept.setObjectName("acceptCohort")
        self.reject = QPushButton("Reject cohort")
        self.reject.setObjectName("rejectCohort")
        self.retry_load = QPushButton("Retry bundle load")
        self.retry_load.hide()
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
        ):
            decisions.addWidget(widget)
        self.decision_help = QLabel()
        self.decision_help.setWordWrap(True)
        self.decision_help.setAccessibleName("Cohort decision requirements")
        self.decision_help.setObjectName("decisionHelp")
        self.shortcuts_help = QLabel(
            "Double-click a row or press Space to play/replay | Left/Right sample | "
            "B mark bad | Ctrl+Enter accept | Ctrl+Backspace reject"
        )
        self.shortcuts_help.setWordWrap(True)
        self.shortcuts_help.setObjectName("shortcutHelp")

        progress_layout = QGridLayout()
        progress_layout.addWidget(self.summary, 0, 0)
        progress_layout.addWidget(self.overall_progress, 1, 0)
        progress_layout.addWidget(self.status, 2, 0)
        progress_layout.addWidget(self.operation, 3, 0)
        progress_layout.addWidget(self.progress, 4, 0)
        progress_layout.addWidget(self.retry_load, 5, 0)
        progress_group = QGroupBox("Review progress")
        progress_group.setLayout(progress_layout)

        cohort_header = QHBoxLayout()
        cohort_header.addWidget(self.cohort_choice, 1)
        cohort_header.addWidget(self.technical_details)
        cohort_layout = QVBoxLayout()
        cohort_layout.addLayout(cohort_header)
        cohort_layout.addWidget(self.cohort_audit)
        cohort_group = QGroupBox("Required cohort")
        cohort_group.setLayout(cohort_layout)

        sample_layout = QVBoxLayout()
        sample_layout.addWidget(self.sample_position)
        sample_layout.addWidget(self.sample_identity)
        sample_layout.addWidget(self.sample_text)
        sample_layout.addLayout(navigation)
        sample_group = QGroupBox("Current sample")
        sample_group.setLayout(sample_layout)

        decision_layout = QVBoxLayout()
        decision_layout.addWidget(self.decision_help)
        decision_layout.addLayout(decisions)
        decision_layout.addWidget(self.shortcuts_help)
        decision_group = QGroupBox("Cohort decision")
        decision_group.setLayout(decision_layout)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(self.heading)
        layout.addWidget(self.guide)
        layout.addWidget(progress_group)
        layout.addWidget(cohort_group)
        layout.addWidget(sample_group)
        layout.addWidget(self.table, 1)
        layout.addWidget(decision_group)

        self.setStyleSheet(
            "QLabel#reviewHeading { font-size: 22px; font-weight: 700; }"
            "QLabel#reviewGuide { font-size: 14px; }"
            "QLabel#reviewStatus { font-weight: 600; }"
            "QLabel#samplePosition { font-weight: 600; }"
            "QLabel#sampleIdentity { font-size: 13px; }"
            "QLabel#sampleText { font-size: 17px; padding: 8px; "
            "  background: palette(base); border: 1px solid palette(mid); "
            "  border-radius: 4px; }"
            "QLabel#decisionHelp { padding: 4px; }"
            "QLabel#shortcutHelp { font-size: 12px; }"
            "QPushButton { min-height: 30px; padding: 4px 10px; }"
            "QPushButton#acceptCohort { font-weight: 700; }"
            "QPushButton#rejectCohort { font-weight: 700; }"
        )

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
        self._operation_timer = QTimer(self)
        self._operation_timer.setInterval(250)
        self._operation_timer.timeout.connect(self._update_operation_status)

        previous_shortcut = QShortcut(
            QKeySequence("Left"), self.table, activated=lambda: self._move(-1)
        )
        previous_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Ctrl+Alt+R"), self, activated=self.play_selected)
        replay_shortcut = QShortcut(
            QKeySequence("Space"), self.table, activated=self.play_selected
        )
        replay_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Ctrl+Alt+S"), self, activated=self.stop_playback)
        next_shortcut = QShortcut(
            QKeySequence("Right"), self.table, activated=lambda: self._move(1)
        )
        next_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        bad_shortcut = QShortcut(
            QKeySequence("B"), self.table, activated=self.toggle_bad
        )
        bad_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
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
        operation = self.sample_loader
        arguments = (self.bundle,)
        if self._resumable_load:
            operation = load_resumable_cohort_review_session
            arguments = (self.bundle_path,)
        self.thread_pool.start(_Task(serial, operation, arguments, self._load_signals))

    def _load_finished(self, serial, result, error):
        if serial != self._load_serial or not self._load_active:
            return
        self._load_active = False
        if error is not None:
            self.samples = ()
            self.samples_by_cohort = {}
            self.status.setText(f"BLOCKED: {error}")
            self.retry_load.show()
            self._populate_cohorts()
            self._update_actions()
            return
        self.retry_load.hide()
        if self._resumable_load:
            _resume, bundle, samples, assessments = result
            for assessment in assessments:
                key = (assessment.workspace_id, assessment.cohort_id)
                self.heard[key].add(assessment.queue_id)
                if assessment.assessment == "bad":
                    self.bad[key].add(assessment.queue_id)
            self._resumable_load = False
        else:
            bundle, samples = result
        self._apply_loaded_bundle(bundle, samples)

    def _apply_loaded_bundle(self, bundle, samples, *, status=None):
        self.bundle = bundle
        self.samples = tuple(samples)
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
        remaining = self.bundle.document["cohort_count"]
        total = max(self._initial_cohort_count, remaining)
        completed = total - remaining
        self.overall_progress.setRange(0, total)
        self.overall_progress.setValue(completed)
        self.overall_progress.setFormat(
            f"{completed} of {total} cohorts completed in this review session"
        )
        self.summary.setText(
            f"{remaining} required cohorts remain. Hear "
            f"{self.bundle.document['sample_item_count']} samples to decide "
            f"{self.bundle.document['pending_item_count']} pending WAVs."
        )
        self.summary.setToolTip(
            f"{self.bundle.document['workspace_count']} source workspaces; "
            f"{self.bundle.document['blocked_item_count']} inherited blocked items "
            "are excluded from this review."
        )
        self.status.setText(status or "READY: play the selected sample")
        self._populate_cohorts()

    def _populate_cohorts(self):
        previous = self.cohort_choice.currentData()
        self.cohort_choice.blockSignals(True)
        self.cohort_choice.clear()
        available = [
            cohort
            for cohort in self.bundle.document["cohorts"]
            if (cohort["workspace_id"], cohort["cohort_id"]) in self.samples_by_cohort
        ]
        for position, cohort in enumerate(available, start=1):
            key = (cohort["workspace_id"], cohort["cohort_id"])
            identity = cohort["identity"]
            self.cohort_choice.addItem(
                f"Required {position}/{len(available)} - "
                f"{identity['voice_character']} role - "
                f"{len(cohort['samples'])} samples decide "
                f"{cohort['item_count']} WAVs",
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

    def _current_cohort(self):
        key = self._current_key()
        return next(
            (
                value
                for value in self.bundle.document["cohorts"]
                if key is not None
                and (value["workspace_id"], value["cohort_id"]) == key
            ),
            None,
        )

    def _toggle_technical_details(self, visible):
        self.table.setColumnHidden(5, not visible)
        self.table.setColumnHidden(6, not visible)
        self.cohort_audit.setVisible(bool(visible and self._current_key()))
        self._update_selected_sample_details()

    def _update_selected_sample_details(self):
        sample = self._selected_sample()
        samples = self._current_samples()
        key = self._current_key()
        if sample is None or key is None:
            self.sample_position.setText("No sample selected")
            self.sample_identity.clear()
            self.sample_text.setText("Select a cohort and sample to begin.")
            self.cohort_audit.clear()
            return
        row = self.table.currentRow() + 1
        heard = len(self.heard[key])
        bad = len(self.bad[key])
        self.sample_position.setText(
            f"Sample {row} of {len(samples)} | {heard} heard | {bad} marked bad"
        )
        self.sample_identity.setText(
            f"Source label: {sample.item.speaker} | Generated role: "
            f"{sample.item.voice_character} | Required sample: "
            f"{_display_required_reason(sample.required_reason)}"
        )
        self.sample_text.setText(sample.item.text)
        cohort = self._current_cohort()
        if cohort is not None:
            identity = cohort["identity"]
            self.cohort_audit.setText(
                f"Provider: {identity['provider']} | Profile: "
                f"{identity['generation_profile']} | Repair: "
                f"{identity['repair_strategy']} | Seed: {identity['seed']}\n"
                f"Line: {sample.item.line_id} | Workspace: "
                f"{cohort['workspace_id']}"
            )
        self.cohort_audit.setVisible(self.technical_details.isChecked())

    def _show_current_cohort(self, *_arguments):
        self.stop_playback()
        samples = self._current_samples()
        self.table.setRowCount(len(samples))
        key = self._current_key()
        for row, sample in enumerate(samples):
            item = sample.item
            values = (
                "Heard" if item.queue_id in self.heard[key] else "Not heard",
                "Sounds bad"
                if item.queue_id in self.bad[key]
                else "Sounds acceptable"
                if item.queue_id in self.heard[key]
                else "Waiting",
                item.speaker,
                item.voice_character,
                _display_required_reason(sample.required_reason),
                review_technical_summary(item),
                item.line_id,
                item.text,
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, sample)
        if samples:
            self.table.selectRow(0)
        self._toggle_technical_details(self.technical_details.isChecked())
        self._update_selected_sample_details()
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
        self._update_selected_sample_details()
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
        assessments = {
            queue_id: "bad" if queue_id in bad else "acceptable"
            for queue_id in reviewed
        }
        self._decision_active = True
        self._decision_serial += 1
        self._decision_started_at = time.perf_counter()
        self._operation_timer.start()
        serial = self._decision_serial
        self.status.setText(
            f"SAVING {decision}: audio replay and navigation remain available"
        )
        self._update_actions()
        operation = self.decision_executor
        arguments = (
            self.bundle,
            key[0],
            key[1],
            decision,
            reviewed,
            assessments,
            current_clean + 1 if decision == "expand" else None,
        )
        if self._checkpoint_decisions:
            operation = _execute_and_checkpoint_bundle_decision
            arguments = (
                self.bundle_path,
                self.original_bundle,
                *arguments,
            )
        self.thread_pool.start(
            _Task(serial, operation, arguments, self._decision_signals)
        )

    def _decision_finished(self, serial, result, error):
        if serial != self._decision_serial or not self._decision_active:
            return
        self._decision_active = False
        self._operation_timer.stop()
        self._decision_started_at = None
        if error is not None:
            self.status.setText(
                f"SAVE FAILED: {error}. Review evidence was not assumed."
            )
            self._update_actions()
            return
        if self._checkpoint_decisions:
            task_result = result
            if task_result.refresh_error is not None or task_result.bundle is None:
                self.bundle = task_result.projection.next_bundle
                self.samples = ()
                self.samples_by_cohort = {}
                self._resumable_load = True
                self.status.setText(
                    "SAVED, BUT REFRESH BLOCKED: source authority committed; "
                    f"press Retry after resolving {task_result.refresh_error}"
                )
                self.retry_load.show()
                self._populate_cohorts()
                self._update_actions()
                return
            total = (
                task_result.commit_seconds
                + task_result.checkpoint_seconds
                + task_result.refresh_seconds
            )
            timing = (
                f"{total:.2f}s total: commit {task_result.commit_seconds:.2f}s, "
                f"checkpoint {task_result.checkpoint_seconds:.2f}s, "
                f"next cohort {task_result.refresh_seconds:.2f}s"
            )
            status = (
                f"SAVED: {timing}"
                if task_result.checkpoint_error is None
                else "SAVED: source authority committed; progress checkpoint will "
                f"be recovered on reopen ({task_result.checkpoint_error}); {timing}"
            )
            self._resumable_load = task_result.checkpoint_error is not None
            self.retry_load.hide()
            self._apply_loaded_bundle(
                task_result.bundle,
                task_result.samples,
                status=status,
            )
            return
        self.bundle = result.next_bundle
        self.status.setText(
            "SAVED: source-local authority committed; refreshing exact bundle"
        )
        self.reload_bundle()

    def _confirm_decision(self, decision, cohort, reviewed, bad):
        item_count = cohort["item_count"]
        if decision == "expand":
            title = "Request more evidence?"
            prompt = (
                "Add another clean checksum-bound sample for this cohort?\n"
                f"You heard {reviewed} samples; {bad} are marked bad. "
                "No WAV will be accepted or rejected."
            )
        else:
            verb = "Accept" if decision == "accepted" else "Reject"
            title = f"{verb} {item_count} WAVs?"
            prompt = (
                f"{verb} all {item_count} WAVs generated with this exact "
                "voice/configuration?\n"
                f"You heard {reviewed} required samples; {bad} are marked bad. "
                "Other cohorts are not affected."
            )
        answer = QMessageBox.question(
            self,
            title,
            prompt,
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
        authority_ready = (
            not self._load_active
            and not self._decision_active
            and not self._playback_prepare_active
        )
        self.previous.setEnabled(ready and len(samples) > 1)
        self.replay.setEnabled(ready and not self._playback_prepare_active)
        self.replay.setText(
            "Replay selected sample"
            if sample is not None and sample.item.queue_id in heard
            else "Play selected sample"
        )
        self.stop.setEnabled(self._playback_target is not None)
        self.next.setEnabled(ready and len(samples) > 1)
        self.mark_bad.setEnabled(
            authority_ready and sample is not None and sample.item.queue_id in heard
        )
        self.mark_bad.setText(
            "Clear sample bad mark"
            if ready and sample.item.queue_id in bad
            else "Sample sounds bad"
        )
        all_heard = bool(samples) and len(heard) == len(samples)
        self.accept.setEnabled(authority_ready and all_heard and not bad)
        self.reject.setEnabled(authority_ready and bool(heard))
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
            authority_ready and all_heard and has_more_clean and current_clean < 5
        )
        item_count = cohort["item_count"] if cohort is not None else 0
        self.accept.setText(
            f"Accept all {item_count} WAVs" if item_count else "Accept cohort"
        )
        self.reject.setText(
            f"Reject all {item_count} WAVs" if item_count else "Reject cohort"
        )
        self.retry_load.setEnabled(
            not self._load_active and not self.samples and not self._decision_active
        )
        self.retry_load.setVisible(not self.samples and not self._load_active)
        if key is not None and samples:
            self.cohort_choice.setToolTip(
                f"Current cohort: {len(heard)}/{len(samples)} heard; "
                f"{len(bad)} marked bad"
            )
        remaining = max(0, len(samples) - len(heard))
        if self._decision_active:
            decision_text = (
                "Decision is saving. Playback and navigation remain available; "
                "a second decision is blocked until the transaction finishes."
            )
        elif self._load_active:
            decision_text = (
                "Checksum authority is refreshing. All decisions are blocked until "
                "the new state and WAV identities are verified."
            )
        elif self._playback_prepare_active:
            decision_text = (
                "The selected checksum-bound WAV is being prepared. Decisions resume "
                "as soon as its immutable playback buffer is ready."
            )
        elif not samples:
            decision_text = "No reviewable cohort is currently loaded."
        elif remaining:
            decision_text = (
                f"Play {remaining} remaining sample{'s' if remaining != 1 else ''}. "
                "A sample-level bad mark does not reject anything by itself."
            )
        elif bad:
            decision_text = (
                f"All samples are heard; {len(bad)} are marked bad. Clear the marks, "
                "request more evidence, or reject the whole cohort."
            )
        else:
            decision_text = (
                f"All {len(samples)} required samples sound acceptable. Accept will "
                f"apply this decision to exactly {item_count} checksum-bound WAVs."
            )
        self.decision_help.setText(decision_text)
        self.replay.setToolTip(
            "Play immutable checksum-verified bytes. Shortcut: Space."
            if ready
            else "Playback is unavailable while checksum authority is refreshing."
        )
        self.mark_bad.setToolTip(
            "Mark only this heard sample as bad evidence; this does not reject the cohort."
            if sample is not None and sample.item.queue_id in heard
            else "Listen to the selected sample completely before marking it bad."
        )
        self.accept.setToolTip(
            "Accept is available after every required sample is heard and none is marked bad."
        )
        self.reject.setToolTip(
            "Reject applies to every WAV in this exact cohort; hear at least one sample first."
        )
        self.need_another.setToolTip(
            "Request another clean checksum-bound sample when the current evidence is inconclusive."
        )
        self._update_selected_sample_details()
        self._update_operation_status()

    def _update_operation_status(self):
        if self._decision_active:
            self.progress.show()
            elapsed = (
                time.perf_counter() - self._decision_started_at
                if self._decision_started_at is not None
                else 0.0
            )
            self.operation.setText(
                f"Saving in background ({elapsed:.1f}s): Accept, Reject and Mark "
                "bad are disabled "
                "to prevent a second state mutation. Replay and sample navigation "
                "remain available until the decision commits."
            )
            return
        if self._load_active:
            self.progress.show()
            self.operation.setText(
                "Refreshing checksum authority: replay and review actions are "
                "temporarily disabled because the state hash changed. The completed "
                "cohort will disappear and the next required cohort will be selected."
            )
            return
        if self._playback_prepare_active:
            self.progress.show()
            self.operation.setText(
                "Preparing one checksum-bound WAV; review decisions remain disabled "
                "until the immutable playback buffer is ready."
            )
            return
        self.progress.hide()
        if self.retry_load.isVisible():
            self.operation.setText(
                "The saved or loaded authority could not be projected. Press Retry "
                "bundle load; no additional cohort decision is allowed meanwhile."
            )
            return
        remaining = self.cohort_choice.count()
        if remaining:
            position = self.cohort_choice.currentIndex() + 1
            self.operation.setText(
                f"Review every listed cohort. Current required cohort: "
                f"{position}/{remaining}. A completed cohort disappears after its "
                "authority refresh."
            )
        else:
            self.operation.setText("All cohorts in this bundle are complete.")

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


def build_parser():
    parser = argparse.ArgumentParser(
        description="Review or inspect a checksum-bound specialist cohort bundle"
    )
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "--status",
        action="store_true",
        help="print reconciled review progress without opening Qt",
    )
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    if arguments.status:
        try:
            status = load_resumable_cohort_review_bundle(
                arguments.bundle,
                persist=False,
            )
        except Exception as error:
            print(str(error), file=sys.stderr)
            return 1
        print(
            json.dumps(status.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
        return 0
    return launch_cohort_review_bundle(arguments.bundle)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CohortReviewBundleDialog",
    "build_parser",
    "launch_cohort_review_bundle",
    "main",
]

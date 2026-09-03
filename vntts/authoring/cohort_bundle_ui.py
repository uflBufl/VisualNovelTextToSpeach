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
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
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
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.authoring.cohort_bundle import (
    CohortBundleSample,
    CohortReviewBundle,
    execute_cohort_bundle_decision,
    load_cohort_review_bundle,
    load_cohort_review_bundle_samples,
    load_resumable_cohort_review_bundle,
    load_resumable_cohort_review_bundle_samples,
    load_resumable_cohort_review_session,
    write_cohort_review_observations,
    write_cohort_review_progress,
)
from vntts.authoring.cohort_review import (
    COHORT_REVIEW_DEFECT_REASONS,
    CohortReviewError,
)
from vntts.authoring.review_context_ui import ReviewDecisionContext
from vntts.authoring.voice_quality_gate import (
    inspect_voice_quality_cohort,
    load_voice_quality_gate,
)
from vntts.authoring.workbench import prepare_review_audio, review_technical_summary


def _display_required_reason(reason):
    prefix = "technical-attention: "
    if reason.startswith(prefix):
        return "advisory measurement; listening decides: " + reason.removeprefix(prefix)
    return reason


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


@dataclass(frozen=True)
class _QualityGateContext:
    gate_id: str
    resolved_voice_character: str
    voice_speaker: str
    cohort_count: int


def _load_quality_gated_review_session(bundle_path, gate_path, persist=True):
    gate = load_voice_quality_gate(gate_path)
    session = load_resumable_cohort_review_session(bundle_path, persist=False)
    resume, bundle, _samples, _assessments = session
    cached = {}
    compatibilities = []
    for cohort in bundle.document["cohorts"]:
        identity = cohort["identity"]
        reusable_key = json.dumps(
            {
                key: value
                for key, value in identity.items()
                if key
                not in {
                    "seed",
                    "synthesis_provenance_sha256",
                    "workspace_config_fingerprint",
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        key = (cohort["workspace"], reusable_key)
        compatibility = cached.get(key)
        if compatibility is None:
            compatibility = inspect_voice_quality_cohort(
                gate,
                cohort["workspace"],
                identity,
            )
            cached[key] = compatibility
        compatibilities.append(compatibility)
    mismatches = [
        value
        for value in compatibilities
        if value.status != "control_match_story_sample_required"
    ]
    if mismatches:
        differences = sorted(
            {item for value in mismatches for item in value.differences}
        )
        raise CohortReviewError(
            "Voice-quality gate does not match every remaining cohort: "
            + ", ".join(differences)
        )
    if not compatibilities:
        context = None
    else:
        resolved = {value.resolved_voice_character for value in compatibilities}
        speakers = {value.voice_speaker for value in compatibilities}
        if len(resolved) != 1 or len(speakers) != 1:
            raise CohortReviewError(
                "Voice-quality gate resolves remaining cohorts to different voices"
            )
        context = _QualityGateContext(
            gate.gate_id,
            next(iter(resolved)),
            next(iter(speakers)),
            len(compatibilities),
        )
    if persist and not resume.progress_current:
        write_cohort_review_progress(
            resume.publication,
            resume.original,
            resume.current,
        )
    return (*session, context)


def _prepare_sample(sample):
    return sample, prepare_review_audio(sample.item)


_DEFECT_REASON_LABELS = {
    "pause_or_pacing": "Pause or pacing",
    "repetition": "Repeated words or phrases",
    "truncation_or_missing_words": "Truncated or missing words",
    "pronunciation_or_wrong_words": "Pronunciation or wrong words",
    "timbre_or_audio_artifact": "Timbre or audio artifact",
    "speaker_identity": "Wrong speaker or voice identity",
    "other_or_unclear": "Other or unclear defect",
}


def _write_observation_task(
    bundle_path, original_bundle, bundle, heard, bad, bad_reasons
):
    return write_cohort_review_observations(
        bundle_path,
        original_bundle,
        bundle,
        heard,
        bad,
        bad_reasons,
    )


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
        observation_writer=_write_observation_task,
        confirmer=None,
        quality_gate=None,
    ):
        super().__init__(parent)
        self.bundle_path = None
        self.original_bundle = None
        self.quality_gate_path = (
            None if quality_gate is None else Path(quality_gate).expanduser().resolve()
        )
        self.quality_gate_context = None
        if isinstance(bundle, CohortReviewBundle):
            if self.quality_gate_path is not None:
                raise CohortReviewError(
                    "A reusable voice-quality gate requires a published bundle path"
                )
            self.bundle = bundle
        else:
            self.bundle_path = Path(bundle).expanduser().resolve()
            self.original_bundle = load_cohort_review_bundle(self.bundle_path)
            self.bundle = self.original_bundle
        self.sample_loader = sample_loader
        self.playback_preparer = playback_preparer
        self.decision_executor = decision_executor
        self.observation_writer = observation_writer
        self._resumable_load = (
            self.bundle_path is not None
            and sample_loader is load_cohort_review_bundle_samples
        )
        self._checkpoint_decisions = (
            self.bundle_path is not None
            and decision_executor is _execute_bundle_decision_task
        )
        self._checkpoint_observations_enabled = self._checkpoint_decisions
        self.confirmer = confirmer or self._confirm_decision
        self.samples = ()
        self.samples_by_cohort = {}
        self.heard = defaultdict(set)
        self.bad = defaultdict(set)
        self.bad_reasons = defaultdict(dict)
        self._updating_defect_controls = False
        self._load_active = False
        self._load_failed = False
        self._playback_prepare_active = False
        self._decision_active = False
        self._observation_active = False
        self._playback_serial = 0
        self._pending_observation = None
        self._close_after_observation = False
        self._decision_started_at = None
        self._decision_scope_text = ""
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
            "3. Repair marked, accept, reject all, or request more evidence. "
            "Technical attention "
            "only selects samples for listening; it is not a rejection verdict."
        )
        self.guide.setWordWrap(True)
        self.guide.setObjectName("reviewGuide")
        self.decision_context = ReviewDecisionContext()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("reviewSummary")
        self.summary.setAccessibleName("Cohort review summary")
        self.quality_baseline = QLabel()
        self.quality_baseline.setWordWrap(True)
        self.quality_baseline.setObjectName("qualityBaseline")
        self.quality_baseline.setAccessibleName("Reusable voice quality baseline")
        self.quality_baseline.hide()
        self.overall_progress = QProgressBar()
        self.overall_progress.setAccessibleName("Overall cohort review progress")
        self.overall_progress.setTextVisible(True)
        self.status = QLabel("Loading exact review authorities...")
        self.status.setWordWrap(True)
        self.status.setObjectName("reviewStatus")
        self.status.setAccessibleName("Cohort review status")
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
        self.cohort_choice.setAccessibleDescription(
            "Choose one checksum-bound cohort from the current review bundle"
        )
        self.cohort_choice.currentIndexChanged.connect(self._show_current_cohort)
        self.technical_details = QCheckBox("Show technical details")
        self.technical_details.setAccessibleName("Show cohort technical details")
        self.technical_details.setAccessibleDescription(
            "Reveal technical sample columns and cohort authority details"
        )
        self.technical_details.toggled.connect(self._toggle_technical_details)
        self.cohort_audit = QLabel()
        self.cohort_audit.setWordWrap(True)
        self.cohort_audit.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.cohort_audit.hide()

        self.sample_position = QLabel("No sample selected")
        self.sample_position.setObjectName("samplePosition")
        self.sample_position.setAccessibleName("Selected sample position")
        self.sample_identity = QLabel()
        self.sample_identity.setWordWrap(True)
        self.sample_identity.setObjectName("sampleIdentity")
        self.sample_identity.setAccessibleName("Selected sample identity")
        self.sample_text = QLabel()
        self.sample_text.setWordWrap(True)
        self.sample_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.sample_text.setObjectName("sampleText")
        self.sample_text.setAccessibleName("Selected sample text")
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
        self.table.setAccessibleDescription(
            "Select one exact generated sample for playback and assessment"
        )
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
        self.leave_undecided = QPushButton("Leave undecided")
        self.repair_marked = QPushButton("Send marked WAVs to repair and continue")
        self.repair_marked.setObjectName("repairMarkedWavs")
        self.accept = QPushButton("Accept cohort")
        self.accept.setObjectName("acceptCohort")
        self.reject = QPushButton("Reject cohort")
        self.reject.setObjectName("rejectCohort")
        self.retry_load = QPushButton("Retry bundle load")
        self.retry_load.hide()
        for button, name, description in (
            (self.previous, "Previous cohort sample", "Select the previous sample"),
            (
                self.replay,
                "Play selected cohort sample",
                "Play the selected checksum-bound sample through to the end",
            ),
            (self.stop, "Stop cohort sample", "Stop current sample playback"),
            (self.next, "Next cohort sample", "Select the next sample"),
            (
                self.mark_bad,
                "Mark selected cohort sample bad",
                "Mark or clear speech-defect evidence for the selected sample",
            ),
            (
                self.need_another,
                "Request more cohort evidence",
                "Keep this cohort unresolved and request more evidence",
            ),
            (
                self.leave_undecided,
                "Leave cohort review undecided",
                "Close without making another cohort decision",
            ),
            (
                self.repair_marked,
                "Repair marked cohort samples",
                "Repair marked WAVs while leaving unsampled items pending",
            ),
            (
                self.accept,
                "Accept current cohort",
                "Accept every fully heard acceptable sample in this exact cohort",
            ),
            (
                self.reject,
                "Reject current cohort",
                "Reject every WAV in this exact cohort after required listening",
            ),
            (
                self.retry_load,
                "Retry cohort bundle load",
                "Retry loading and validating the current checksum-bound bundle",
            ),
        ):
            button.setAccessibleName(name)
            button.setAccessibleDescription(description)
        self.previous.clicked.connect(lambda: self._move(-1))
        self.replay.clicked.connect(self.play_selected)
        self.stop.clicked.connect(self.stop_playback)
        self.next.clicked.connect(lambda: self._move(1))
        self.mark_bad.clicked.connect(self.toggle_bad)
        self.need_another.clicked.connect(lambda: self.apply_decision("expand"))
        self.leave_undecided.clicked.connect(self.close)
        self.repair_marked.clicked.connect(lambda: self.apply_decision("split"))
        self.accept.clicked.connect(lambda: self.apply_decision("accepted"))
        self.reject.clicked.connect(lambda: self.apply_decision("rejected"))
        self.retry_load.clicked.connect(self.reload_bundle)

        navigation = QHBoxLayout()
        for widget in (self.previous, self.replay, self.stop, self.next):
            navigation.addWidget(widget)
        evidence_actions = QHBoxLayout()
        evidence_actions.addWidget(self.mark_bad)
        evidence_actions.addWidget(self.need_another)
        evidence_actions.addWidget(self.leave_undecided)
        terminal_actions = QHBoxLayout()
        terminal_actions.addWidget(self.repair_marked)
        terminal_actions.addWidget(self.accept)
        terminal_actions.addWidget(self.reject)
        decisions = QVBoxLayout()
        decisions.addLayout(evidence_actions)
        decisions.addLayout(terminal_actions)
        self.defect_checks = {}
        defect_layout = QGridLayout()
        for index, (reason, label) in enumerate(_DEFECT_REASON_LABELS.items()):
            control = QCheckBox(label)
            control.setAccessibleName(f"Bad sample reason: {label}")
            control.setAccessibleDescription(
                "Record this speech defect for the selected heard sample"
            )
            control.toggled.connect(self._defect_reasons_changed)
            self.defect_checks[reason] = control
            defect_layout.addWidget(control, index // 2, index % 2)
        defect_group = QGroupBox("Why the selected sample sounds bad")
        defect_group.setAccessibleName("Selected sample speech defect reasons")
        defect_group.setLayout(defect_layout)
        self.decision_help = QLabel()
        self.decision_help.setWordWrap(True)
        self.decision_help.setAccessibleName("Cohort decision requirements")
        self.decision_help.setObjectName("decisionHelp")
        self.shortcuts_help = QLabel(
            "Double-click a row or press Space to play/replay | Left/Right sample | "
            "B mark bad | Ctrl+Shift+Enter repair marked | Ctrl+Enter accept | "
            "Ctrl+Backspace reject all"
        )
        self.shortcuts_help.setWordWrap(True)
        self.shortcuts_help.setObjectName("shortcutHelp")
        self.shortcuts_help.setAccessibleName("Cohort review keyboard shortcuts")

        progress_layout = QGridLayout()
        progress_layout.addWidget(self.summary, 0, 0)
        progress_layout.addWidget(self.quality_baseline, 1, 0)
        progress_layout.addWidget(self.overall_progress, 2, 0)
        progress_layout.addWidget(self.status, 3, 0)
        progress_layout.addWidget(self.operation, 4, 0)
        progress_layout.addWidget(self.progress, 5, 0)
        progress_layout.addWidget(self.retry_load, 6, 0)
        progress_group = QGroupBox("Review progress")
        progress_group.setLayout(progress_layout)

        cohort_header = QHBoxLayout()
        self.cohort_choice_label = QLabel("Cohort")
        self.cohort_choice_label.setBuddy(self.cohort_choice)
        cohort_header.addWidget(self.cohort_choice_label)
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
        decision_layout.addWidget(defect_group)
        decision_layout.addLayout(decisions)
        decision_layout.addWidget(self.shortcuts_help)
        decision_group = QGroupBox("Cohort decision")
        decision_group.setLayout(decision_layout)

        review_content = QWidget()
        review_layout = QVBoxLayout(review_content)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(10)
        review_layout.addWidget(self.heading)
        review_layout.addWidget(self.guide)
        review_layout.addWidget(self.decision_context)
        review_layout.addWidget(progress_group)
        review_layout.addWidget(cohort_group)
        review_layout.addWidget(sample_group)
        review_layout.addWidget(self.table, 1)
        self.review_scroll = QScrollArea()
        self.review_scroll.setAccessibleName("Scrollable cohort review context")
        self.review_scroll.setWidgetResizable(True)
        self.review_scroll.setMinimumHeight(240)
        self.review_scroll.setWidget(review_content)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(self.review_scroll, 1)
        layout.addWidget(decision_group)

        self.setStyleSheet(
            "QLabel#reviewHeading { font-size: 22px; font-weight: 700; }"
            "QLabel#reviewGuide { font-size: 14px; }"
            "QLabel#reviewStatus { font-weight: 600; }"
            "QLabel#qualityBaseline { padding: 6px; font-weight: 600; "
            "  background: palette(alternate-base); border: 1px solid palette(mid); }"
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

        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(2)
        self._load_runner = LatestTaskRunner(self, thread_pool=self.thread_pool)
        self._load_runner.finished.connect(self._load_finished)
        self._playback_runner = LatestTaskRunner(self, thread_pool=self.thread_pool)
        self._playback_runner.finished.connect(self._playback_finished)
        self._decision_runner = LatestTaskRunner(self, thread_pool=self.thread_pool)
        self._decision_runner.finished.connect(self._decision_finished)
        self._observation_runner = LatestTaskRunner(self, thread_pool=self.thread_pool)
        self._observation_runner.finished.connect(self._observation_finished)
        self._operation_timer = QTimer(self)
        self._operation_timer.setInterval(250)
        self._operation_timer.timeout.connect(self._update_operation_status)

        focus_order = [
            self.decision_context.technical_toggle,
            self.retry_load,
            self.cohort_choice,
            self.technical_details,
            self.previous,
            self.replay,
            self.stop,
            self.next,
            self.table,
            *self.defect_checks.values(),
            self.mark_bad,
            self.need_another,
            self.leave_undecided,
            self.repair_marked,
            self.accept,
            self.reject,
        ]
        for current, following in zip(focus_order, focus_order[1:]):
            self.setTabOrder(current, following)

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
            QKeySequence("Ctrl+Shift+Return"),
            self,
            activated=lambda: self.apply_decision("split"),
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
        self._load_failed = False
        self.status.setText("Loading and checksum-validating all bundle sources...")
        self._update_actions()
        operation = self.sample_loader
        arguments = (self.bundle,)
        if self._resumable_load:
            if self.quality_gate_path is None:
                operation = load_resumable_cohort_review_session
                arguments = (self.bundle_path,)
            else:
                operation = _load_quality_gated_review_session
                arguments = (self.bundle_path, self.quality_gate_path)
        self._load_runner.start(operation, *arguments)

    def _load_finished(self, result, error):
        if not self._load_active:
            return
        self._load_active = False
        if error is not None:
            self._load_failed = True
            self.quality_gate_context = None
            self.quality_baseline.hide()
            self.samples = ()
            self.samples_by_cohort = {}
            self.status.setText(f"BLOCKED: {error}")
            self.retry_load.show()
            self._populate_cohorts()
            self._update_actions()
            return
        self.retry_load.hide()
        self._load_failed = False
        if self._resumable_load:
            if self.quality_gate_path is None:
                _resume, bundle, samples, assessments = result
            else:
                (
                    _resume,
                    bundle,
                    samples,
                    assessments,
                    self.quality_gate_context,
                ) = result
            for assessment in assessments:
                key = (assessment.workspace_id, assessment.cohort_id)
                self.heard[key].add(assessment.queue_id)
                if assessment.assessment == "bad":
                    self.bad[key].add(assessment.queue_id)
                    self.bad_reasons[key][assessment.queue_id] = set(
                        assessment.defect_reasons or ("unspecified",)
                    )
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
        self.bad_reasons = defaultdict(
            dict,
            {
                key: {
                    queue_id: set(reasons)
                    for queue_id, reasons in values.items()
                    if (*key, queue_id) in valid and queue_id in self.bad[key]
                }
                for key, values in self.bad_reasons.items()
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
            "Review complete. All required cohorts were saved."
            if remaining == 0
            else (
                f"{remaining} required cohorts remain. Hear "
                f"{self.bundle.document['sample_item_count']} samples to decide "
                f"{self.bundle.document['pending_item_count']} pending WAVs."
            )
        )
        self.summary.setToolTip(
            f"{self.bundle.document['workspace_count']} source workspaces; "
            f"{self.bundle.document['blocked_item_count']} inherited blocked items "
            "are excluded from this review."
        )
        if self.quality_gate_context is None or remaining == 0:
            self.quality_baseline.hide()
        else:
            context = self.quality_gate_context
            self.quality_baseline.setText(
                "VOICE BASELINE ALREADY ACCEPTED: "
                f"{context.resolved_voice_character} ({context.voice_speaker}). "
                "You are not choosing the narrator again. Hear the listed samples "
                "to validate these new story WAVs; Accept applies only to the "
                "current exact cohort."
            )
            self.quality_baseline.setToolTip(
                f"Gate {context.gate_id}; matched {remaining} remaining cohorts"
            )
            self.quality_baseline.show()
        self.status.setText(
            "COMPLETE: all required cohorts are saved"
            if remaining == 0
            else status or "READY: play the selected sample"
        )
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
            completed = self.bundle.document["cohort_count"] == 0
            self.sample_position.setText(
                "Review complete" if completed else "No sample selected"
            )
            self.sample_identity.clear()
            self.sample_text.setText(
                "All required cohorts are complete."
                if completed
                else "Select a cohort and sample to begin."
            )
            self.cohort_audit.clear()
            self.decision_context.set_context(
                {
                    "purpose": "Approve or reject checksum-bound generated WAVs",
                    "effect": "change only the exact current cohort",
                }
            )
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
            f"{_display_required_reason(sample.required_reason)} | "
            "Pace metrics are report-only; listening decides quality"
        )
        self.sample_text.setText(sample.item.text)
        cohort = self._current_cohort()
        if cohort is not None:
            identity = cohort["identity"]
            model = str(identity["model"])
            binding = identity.get("source_reference_binding")
            reference = (
                "Checksum-bound source-reference binding"
                if binding is not None
                else "Workspace voice manifest reference"
            )
            repair = identity.get("repair_strategy") or "direct render"
            self.decision_context.set_context(
                {
                    "purpose": "Approve or reject generated story WAVs",
                    "game_speaker": sample.item.speaker,
                    "synthesis_voice": identity["voice_character"],
                    "reference": reference,
                    "backend": identity["provider"],
                    "model": Path(model).name if "/" in model else model,
                    "generation_profile": identity["generation_profile"],
                    "controls": f"{repair} | Seed: {identity['seed']}",
                    "effect": (
                        f"apply only to {cohort['item_count']} checksum-bound WAV(s) "
                        "in this cohort"
                    ),
                },
                technical=(
                    f"Exact model: {model}\n"
                    f"Workspace: {cohort['workspace_id']}\n"
                    f"Cohort: {cohort['cohort_id']}\n"
                    f"Line: {sample.item.line_id}"
                ),
            )
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
                (
                    "Bad: "
                    + ", ".join(
                        _DEFECT_REASON_LABELS.get(reason, "Unspecified")
                        for reason in sorted(
                            self.bad_reasons[key].get(item.queue_id, ())
                        )
                    )
                )
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
        self._sync_defect_controls()
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
        self.status.setText(f"Preparing exact WAV: {sample.item.line_id}")
        self._update_actions()
        self._playback_runner.start(self.playback_preparer, sample)

    def _playback_finished(self, result, error):
        if not self._playback_prepare_active:
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
        target = next(
            (
                sample
                for sample in self.samples_by_cohort.get((workspace_id, cohort_id), ())
                if sample.item.queue_id == queue_id
                and sample.item.authority is not None
                and sample.item.authority.audio_sha256 == audio_sha256
            ),
            None,
        )
        if target is not None:
            self.heard[(workspace_id, cohort_id)].add(queue_id)
            self.status.setText(f"HEARD: {target.item.line_id}")
        self._playback_target = None
        selected_queue_id = selected.item.queue_id if selected is not None else None
        playback_serial = self._playback_serial
        # Qt Multimedia can still be inside its backend's EndOfMedia callback here.
        # Calling stop() or closing the QIODevice re-entrantly can deadlock the
        # Cocoa/FFmpeg backend, so return to the event loop before cleanup.
        QTimer.singleShot(
            0,
            lambda: self._finish_completed_playback(
                playback_serial,
                selected_queue_id,
            ),
        )

    def _finish_completed_playback(self, playback_serial, selected_queue_id):
        if playback_serial != self._playback_serial:
            return
        self._show_current_cohort()
        if selected_queue_id is not None:
            self._select_queue_id(selected_queue_id)
        self._checkpoint_observations()

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
            self.bad_reasons[key].pop(sample.item.queue_id, None)
        else:
            self.bad[key].add(sample.item.queue_id)
            self.bad_reasons[key][sample.item.queue_id] = {"other_or_unclear"}
        queue_id = sample.item.queue_id
        self._show_current_cohort()
        self._select_queue_id(queue_id)
        self._checkpoint_observations()

    def _sync_defect_controls(self):
        sample = self._selected_sample()
        key = self._current_key()
        reasons = (
            self.bad_reasons[key].get(sample.item.queue_id, set())
            if sample is not None and key is not None
            else set()
        )
        enabled = (
            sample is not None
            and key is not None
            and sample.item.queue_id in self.heard[key]
            and not self._load_active
            and not self._decision_active
            and not self._playback_prepare_active
        )
        self._updating_defect_controls = True
        try:
            for reason, control in self.defect_checks.items():
                control.setChecked(reason in reasons)
                control.setEnabled(enabled)
        finally:
            self._updating_defect_controls = False

    def _defect_reasons_changed(self, _checked=False):
        if self._updating_defect_controls:
            return
        sample = self._selected_sample()
        key = self._current_key()
        if sample is None or key is None or sample.item.queue_id not in self.heard[key]:
            self._sync_defect_controls()
            return
        reasons = {
            reason
            for reason, control in self.defect_checks.items()
            if control.isChecked()
        }
        if not reasons.issubset(COHORT_REVIEW_DEFECT_REASONS):
            self.status.setText("BLOCKED: unsupported speech defect reason")
            self._sync_defect_controls()
            return
        queue_id = sample.item.queue_id
        if reasons:
            self.bad[key].add(queue_id)
            self.bad_reasons[key][queue_id] = reasons
        else:
            self.bad[key].discard(queue_id)
            self.bad_reasons[key].pop(queue_id, None)
        self._show_current_cohort()
        self._select_queue_id(queue_id)
        self._checkpoint_observations()

    def _checkpoint_observations(self):
        if not self._checkpoint_observations_enabled:
            return
        snapshot = (
            self.bundle_path,
            self.original_bundle,
            self.bundle,
            {key: frozenset(value) for key, value in self.heard.items()},
            {key: frozenset(value) for key, value in self.bad.items()},
            {
                key: {
                    queue_id: frozenset(reasons) for queue_id, reasons in values.items()
                }
                for key, values in self.bad_reasons.items()
            },
        )
        if self._observation_active:
            self._pending_observation = snapshot
            return
        self._start_observation_checkpoint(snapshot)

    def _start_observation_checkpoint(self, snapshot):
        self._observation_active = True
        self.operation.setText(
            "Saving listening progress in background; replay and decisions remain available."
        )
        self._observation_runner.start(self.observation_writer, *snapshot)

    def _observation_finished(self, _result, error):
        if not self._observation_active:
            return
        self._observation_active = False
        if error is not None:
            self._close_after_observation = False
            self.status.setText(
                "LISTENING CHECKPOINT FAILED: keep this window open or replay "
                f"samples after reopening ({error})"
            )
        pending = self._pending_observation
        self._pending_observation = None
        if pending is not None:
            self._start_observation_checkpoint(pending)
            return
        if self._close_after_observation:
            self._close_after_observation = False
            QTimer.singleShot(0, self.close)
            return
        self._update_actions()

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
        if decision in {"accepted", "split", "expand"} and len(reviewed) != len(
            samples
        ):
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
        if decision == "split":
            source = next(
                value
                for value in self.bundle.document["sources"]
                if value["workspace_id"] == key[0]
            )
            plan_cohort = next(
                value
                for value in source["plan"]["cohorts"]
                if value["cohort_id"] == key[1]
            )
            target_ids = {value["queue_id"] for value in plan_cohort["items"]}
            unreviewed_count = len(target_ids - set(reviewed))
            if not bad or (len(bad) == len(samples) and unreviewed_count == 0):
                self.status.setText(
                    "BLOCKED: mixed review requires a marked-bad WAV and at least "
                    "one acceptable or unreviewed WAV"
                )
                return
        if not self.confirmer(decision, cohort, len(reviewed), len(bad)):
            return
        source = next(
            value
            for value in self.bundle.document["sources"]
            if value["workspace_id"] == key[0]
        )
        current_clean = source["plan"]["policy"]["clean_samples_per_bucket"]
        assessments = {
            queue_id: {
                "assessment": "bad" if queue_id in bad else "acceptable",
                "defect_reasons": sorted(self.bad_reasons[key].get(queue_id, ())),
            }
            for queue_id in reviewed
        }
        if decision == "split":
            unreviewed = max(0, cohort["item_count"] - len(reviewed))
            self._decision_scope_text = (
                f"rejecting {len(bad)} marked WAVs for repair; approving "
                f"{len(reviewed) - len(bad)} individually heard WAVs; leaving "
                f"{unreviewed} unreviewed WAVs pending"
            )
        elif decision == "expand":
            self._decision_scope_text = (
                f"requesting more evidence after {len(reviewed)} heard samples; "
                "changing 0 WAV decisions"
            )
        else:
            verb = "approving" if decision == "accepted" else "rejecting"
            self._decision_scope_text = (
                f"{verb} all {cohort['item_count']} cohort WAVs after "
                f"{len(reviewed)} heard samples"
            )
        self._decision_active = True
        self._decision_started_at = time.perf_counter()
        self._operation_timer.start()
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
        self._decision_runner.start(operation, *arguments)

    def _decision_finished(self, result, error):
        if not self._decision_active:
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
            if task_result.projection.next_bundle.document["cohort_count"] == 0:
                self._resumable_load = False
                self._load_failed = False
                self.retry_load.hide()
                self._apply_loaded_bundle(
                    task_result.projection.next_bundle,
                    (),
                )
                return
            if task_result.refresh_error is not None or task_result.bundle is None:
                self.bundle = task_result.projection.next_bundle
                self.samples = ()
                self.samples_by_cohort = {}
                self._resumable_load = True
                self._load_failed = True
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
            self._load_failed = False
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
        elif decision == "split":
            acceptable = reviewed - bad
            unreviewed = max(0, item_count - reviewed)
            title = f"Repair {bad} WAVs and accept {acceptable}?"
            prompt = (
                f"Reject exactly {bad} individually heard and marked WAVs for repair, "
                f"and approve exactly {acceptable} individually heard acceptable WAVs?\n"
                f"Leave exactly {unreviewed} unreviewed WAVs pending in a checksum-bound "
                "successor. The whole cohort is not rejected."
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
            "Clear selected defect reasons"
            if ready and sample.item.queue_id in bad
            else "Mark bad: other or unclear"
        )
        all_heard = bool(samples) and len(heard) == len(samples)
        self.accept.setEnabled(authority_ready and all_heard and not bad)
        self.reject.setEnabled(authority_ready and bool(heard))
        current_clean = 5
        source = None
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
        self.leave_undecided.setEnabled(
            not self._decision_active and not self._observation_active
        )
        target_ids = (
            {value["queue_id"] for value in source_plan_cohort["items"]}
            if source_plan_cohort is not None
            else set()
        )
        sample_ids = {value.item.queue_id for value in samples}
        split_unreviewed_count = len(target_ids - sample_ids)
        split_acceptable_count = len(samples) - len(bad)
        self.repair_marked.setEnabled(
            authority_ready
            and all_heard
            and bool(bad)
            and (split_acceptable_count > 0 or split_unreviewed_count > 0)
        )
        item_count = cohort["item_count"] if cohort is not None else 0
        self.repair_marked.setText(
            f"Repair {len(bad)} marked; accept {split_acceptable_count} heard"
            + (
                f"; leave {split_unreviewed_count} pending"
                if split_unreviewed_count
                else ""
            )
            if bad
            else "Send marked WAVs to repair and continue"
        )
        self.accept.setText(
            f"Accept all {item_count} WAVs" if item_count else "Accept cohort"
        )
        self.reject.setText(
            f"Reject all {item_count} WAVs" if item_count else "Reject cohort"
        )
        self.retry_load.setEnabled(
            self._load_failed and not self._load_active and not self._decision_active
        )
        self.retry_load.setVisible(
            self._load_failed and not self._load_active and not self._decision_active
        )
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
        elif self.bundle.document["cohort_count"] == 0:
            decision_text = "Review complete. All required cohorts are saved."
        elif not samples:
            decision_text = "No reviewable cohort is currently loaded."
        elif remaining:
            decision_text = (
                f"Play {remaining} remaining sample{'s' if remaining != 1 else ''}. "
                "A sample-level bad mark does not reject anything by itself."
            )
        elif bad:
            if split_acceptable_count or split_unreviewed_count:
                decision_text = (
                    f"All {len(samples)} required samples were heard: repair/reject "
                    f"exactly {len(bad)} marked WAVs, approve exactly "
                    f"{split_acceptable_count} individually heard acceptable WAVs, "
                    "and leave "
                    f"{split_unreviewed_count} unsampled WAVs pending; or deliberately "
                    f"reject all {item_count}."
                )
            else:
                unsampled = max(0, item_count - len(sample_ids))
                decision_text = (
                    f"{len(bad)} heard samples are marked bad, but {unsampled} target "
                    "WAVs were not individually sampled. Mixed review is blocked so no "
                    "sibling is approved implicitly; request more evidence or leave undecided."
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
            "Quickly mark this heard sample as other/unclear, or clear all selected reasons."
            if sample is not None and sample.item.queue_id in heard
            else "Listen to the selected sample completely before marking it bad."
        )
        self._sync_defect_controls()
        self.accept.setToolTip(
            "Accept is available after every required sample is heard and none is marked bad."
        )
        self.reject.setToolTip(
            "Reject applies to every WAV in this exact cohort; hear at least one sample first."
        )
        self.repair_marked.setToolTip(
            "Reject only marked WAVs, approve only individually heard acceptable WAVs, "
            "and keep every unsampled sibling pending in the successor."
        )
        self.need_another.setToolTip(
            "Hear every current sample before requesting more evidence."
            if not all_heard
            else "The five-sample evidence bound has been reached."
            if current_clean >= 5
            else "No unsampled technically clean evidence remains in this cohort."
            if not has_more_clean
            else "Request one more clean checksum-bound sample without changing WAV authority."
        )
        self.leave_undecided.setToolTip(
            "Close with heard and defect observations checkpointed; no WAV is approved or rejected."
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
                f"Saving in background ({elapsed:.1f}s), {self._decision_scope_text}: "
                "Accept, Reject, repair and Mark bad are disabled "
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
        if self._observation_active:
            self.progress.show()
            self.operation.setText(
                "Saving listening progress in background; replay, navigation and "
                "review decisions remain available. Closing waits for the latest "
                "coalesced checkpoint."
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
        if (
            self._load_active
            or self._playback_prepare_active
            or self._decision_active
            or self._observation_active
        ):
            if self._observation_active:
                self._close_after_observation = True
            self.status.setText(
                "Close deferred until the current authority task finishes"
            )
            event.ignore()
            return
        self.stop_playback()
        event.accept()


def launch_cohort_review_bundle(bundle_path, *, quality_gate=None):
    application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = CohortReviewBundleDialog(bundle_path, quality_gate=quality_gate)
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
        "--quality-gate",
        type=Path,
        help=(
            "require every remaining cohort to match this accepted reusable "
            "voice-quality gate"
        ),
    )
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
            if arguments.quality_gate is None:
                status = load_resumable_cohort_review_bundle(
                    arguments.bundle,
                    persist=False,
                )
            else:
                status, _bundle, _samples, _assessments, _context = (
                    _load_quality_gated_review_session(
                        arguments.bundle,
                        arguments.quality_gate,
                        False,
                    )
                )
        except Exception as error:
            print(str(error), file=sys.stderr)
            return 1
        print(
            json.dumps(status.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
        return 0
    return launch_cohort_review_bundle(
        arguments.bundle,
        quality_gate=arguments.quality_gate,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CohortReviewBundleDialog",
    "build_parser",
    "launch_cohort_review_bundle",
    "main",
]

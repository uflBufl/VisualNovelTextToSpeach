"""Qt review surface for blind missing-voice reuse evidence."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Literal, TypeAlias, TypedDict

from PySide6.QtCore import QObject, Qt, QThreadPool, QUrl
from PySide6.QtGui import QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.authoring.missing_voice_reuse_review import (
    AUTOMATIC_UNRESOLVED_ORIGIN,
    MissingVoiceReuseReviewError,
    load_missing_voice_reuse_review,
    missing_voice_reuse_review_progress,
    record_missing_voice_reuse_decision,
    record_missing_voice_reuse_heard,
)
from vntts.authoring.review_context_ui import (
    ReviewDecisionContext,
    review_form_layout,
    review_model_label,
    review_scroll_area,
)
from vntts.qt_audio import QtPcmPlayer as QMediaPlayer


class ReviewSample(TypedDict):
    queue_id: str
    line_id: str
    length_bucket: str
    text: str


class GeneratedReviewArm(TypedDict):
    queue_id: str
    status: Literal["generated"]
    attempt_count: int
    audio: str
    quality: dict[str, object] | None
    repair_strategy: str | None


class FailedReviewArm(TypedDict):
    queue_id: str
    status: Literal["failed"]
    attempt_count: int
    failure_kind: str


ReviewArm: TypeAlias = GeneratedReviewArm | FailedReviewArm


class ReviewCandidate(TypedDict):
    label: str
    samples: list[ReviewArm]


class ReviewCohort(TypedDict):
    cohort_id: str
    sample_count: int
    samples: list[ReviewSample]
    complete_candidate_labels: list[str]


class ReviewContextTechnical(TypedDict):
    plan_id: str
    workspace_ids: list[str]


class ReviewContext(TypedDict):
    purpose: str
    game_speaker: str
    synthesis_voice: str
    reference: str
    backend: str
    model: str
    generation_profile: str
    seed: str | int
    controls: str
    effect: str
    technical: ReviewContextTechnical


class ReviewBundle(TypedDict):
    bundle_id: str
    target_mode: Literal["missing", "failed"]
    character: str
    decision_context: ReviewContext | None
    candidates: list[ReviewCandidate]
    cohorts: list[ReviewCohort]


class ReviewDecision(TypedDict):
    cohort_id: str
    decision: str | None
    decision_origin: str | None


class HeardRecord(TypedDict):
    cohort_id: str
    queue_id: str
    label: str


class ReviewSession(TypedDict):
    decisions: list[ReviewDecision]
    heard: list[HeardRecord]


HeardKey: TypeAlias = tuple[str, str, str]
HeardRecorder: TypeAlias = Callable[[Path, str, str, str], object]
DecisionRecorder: TypeAlias = Callable[[Path, str, str], object]
DecisionConfirmer: TypeAlias = Callable[[str], bool]
ReviewLoader: TypeAlias = Callable[[Path], tuple[object, object]]
ReviewProgress: TypeAlias = Callable[[ReviewBundle, ReviewSession], tuple[int, int]]

_review_loader: ReviewLoader = load_missing_voice_reuse_review
_review_progress: ReviewProgress = missing_voice_reuse_review_progress
_default_heard_recorder: HeardRecorder = record_missing_voice_reuse_heard
_default_decision_recorder: DecisionRecorder = record_missing_voice_reuse_decision


def _review_sample(value: object) -> ReviewSample | None:
    if not isinstance(value, dict):
        return None
    queue_id = value.get("queue_id")
    line_id = value.get("line_id")
    length_bucket = value.get("length_bucket")
    text = value.get("text")
    if (
        not isinstance(queue_id, str)
        or not isinstance(line_id, str)
        or not isinstance(length_bucket, str)
        or not isinstance(text, str)
    ):
        return None
    return {
        "queue_id": queue_id,
        "line_id": line_id,
        "length_bucket": length_bucket,
        "text": text,
    }


def _review_arm(value: object) -> ReviewArm | None:
    if not isinstance(value, dict):
        return None
    queue_id = value.get("queue_id")
    status = value.get("status")
    attempts = value.get("attempt_count")
    if not isinstance(queue_id, str) or not isinstance(attempts, int):
        return None
    if status == "generated":
        audio = value.get("audio")
        raw_quality = value.get("quality")
        repair = value.get("repair_strategy")
        if not isinstance(audio, str):
            return None
        if not isinstance(repair, (str, type(None))):
            return None
        if raw_quality is None:
            quality: dict[str, object] | None = None
        elif isinstance(raw_quality, dict):
            quality = {
                key: item for key, item in raw_quality.items() if isinstance(key, str)
            }
        else:
            return None
        return {
            "queue_id": queue_id,
            "status": "generated",
            "attempt_count": attempts,
            "audio": audio,
            "quality": quality,
            "repair_strategy": repair,
        }
    if status == "failed":
        failure_kind = value.get("failure_kind")
        if not isinstance(failure_kind, str):
            return None
        return {
            "queue_id": queue_id,
            "status": "failed",
            "attempt_count": attempts,
            "failure_kind": failure_kind,
        }
    return None


def _review_candidate(value: object) -> ReviewCandidate | None:
    if not isinstance(value, dict):
        return None
    label = value.get("label")
    arms = value.get("samples")
    if not isinstance(label, str) or not isinstance(arms, list):
        return None
    samples = [_review_arm(arm) for arm in arms]
    if any(arm is None for arm in samples):
        return None
    return {"label": label, "samples": [arm for arm in samples if arm is not None]}


def _review_cohort(value: object) -> ReviewCohort | None:
    if not isinstance(value, dict):
        return None
    cohort_id = value.get("cohort_id")
    sample_count = value.get("sample_count")
    samples = value.get("samples")
    complete = value.get("complete_candidate_labels")
    if (
        not isinstance(cohort_id, str)
        or not isinstance(sample_count, int)
        or not isinstance(samples, list)
        or not isinstance(complete, list)
        or any(not isinstance(label, str) for label in complete)
    ):
        return None
    reviewed_samples = [_review_sample(sample) for sample in samples]
    if any(sample is None for sample in reviewed_samples):
        return None
    return {
        "cohort_id": cohort_id,
        "sample_count": sample_count,
        "samples": [sample for sample in reviewed_samples if sample is not None],
        "complete_candidate_labels": complete,
    }


def _review_context(value: object) -> ReviewContext | None:
    if not isinstance(value, dict):
        return None
    technical = value.get("technical")
    if not isinstance(technical, dict):
        return None
    plan_id = technical.get("plan_id")
    workspace_ids = technical.get("workspace_ids")
    text_fields = (
        "purpose",
        "game_speaker",
        "synthesis_voice",
        "reference",
        "backend",
        "model",
        "generation_profile",
        "controls",
        "effect",
    )
    seed = value.get("seed")
    if not isinstance(plan_id, str) or not isinstance(workspace_ids, list):
        return None
    if any(not isinstance(workspace_id, str) for workspace_id in workspace_ids):
        return None
    context_values: dict[str, str] = {}
    for field in text_fields:
        field_value = value.get(field)
        if not isinstance(field_value, str):
            return None
        context_values[field] = field_value
    if not isinstance(seed, (str, int)) or isinstance(seed, bool):
        return None
    return {
        "purpose": context_values["purpose"],
        "game_speaker": context_values["game_speaker"],
        "synthesis_voice": context_values["synthesis_voice"],
        "reference": context_values["reference"],
        "backend": context_values["backend"],
        "model": context_values["model"],
        "generation_profile": context_values["generation_profile"],
        "seed": seed,
        "controls": context_values["controls"],
        "effect": context_values["effect"],
        "technical": {"plan_id": plan_id, "workspace_ids": workspace_ids},
    }


def _review_decisions(value: list[object]) -> list[ReviewDecision]:
    decisions: list[ReviewDecision] = []
    for decision in value:
        if not isinstance(decision, dict):
            raise MissingVoiceReuseReviewError("Missing-voice review data is malformed")
        cohort_id = decision.get("cohort_id")
        selected = decision.get("decision")
        origin = decision.get("decision_origin")
        if not isinstance(cohort_id, str) or not isinstance(
            selected, (str, type(None))
        ):
            raise MissingVoiceReuseReviewError("Missing-voice review data is malformed")
        if not isinstance(origin, (str, type(None))):
            raise MissingVoiceReuseReviewError("Missing-voice review data is malformed")
        decisions.append(
            {"cohort_id": cohort_id, "decision": selected, "decision_origin": origin}
        )
    return decisions


def _review_heard_records(value: list[object]) -> list[HeardRecord]:
    heard: list[HeardRecord] = []
    for record in value:
        if not isinstance(record, dict):
            raise MissingVoiceReuseReviewError("Missing-voice review data is malformed")
        cohort_id = record.get("cohort_id")
        queue_id = record.get("queue_id")
        label = record.get("label")
        if (
            not isinstance(cohort_id, str)
            or not isinstance(queue_id, str)
            or not isinstance(label, str)
        ):
            raise MissingVoiceReuseReviewError("Missing-voice review data is malformed")
        heard.append({"cohort_id": cohort_id, "queue_id": queue_id, "label": label})
    return heard


def _review_data(value: object) -> tuple[ReviewBundle, ReviewSession]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise MissingVoiceReuseReviewError("Missing-voice review data is malformed")
    raw_bundle, raw_session = value
    if not isinstance(raw_bundle, dict) or not isinstance(raw_session, dict):
        raise MissingVoiceReuseReviewError("Missing-voice review data is malformed")
    candidates = raw_bundle.get("candidates")
    cohorts = raw_bundle.get("cohorts")
    decisions = raw_session.get("decisions")
    heard = raw_session.get("heard")
    if (
        not isinstance(candidates, list)
        or not isinstance(cohorts, list)
        or not isinstance(decisions, list)
        or not isinstance(heard, list)
    ):
        raise MissingVoiceReuseReviewError("Missing-voice review data is malformed")
    reviewed_candidates = [_review_candidate(candidate) for candidate in candidates]
    reviewed_cohorts = [_review_cohort(cohort) for cohort in cohorts]
    if any(candidate is None for candidate in reviewed_candidates) or any(
        cohort is None for cohort in reviewed_cohorts
    ):
        raise MissingVoiceReuseReviewError("Missing-voice review data is malformed")
    bundle_id = raw_bundle.get("bundle_id")
    character = raw_bundle.get("character")
    target_mode = raw_bundle.get("target_mode", "missing")
    if (
        not isinstance(bundle_id, str)
        or not isinstance(character, str)
        or target_mode not in {"missing", "failed"}
    ):
        raise MissingVoiceReuseReviewError("Missing-voice review data is malformed")
    return (
        {
            "bundle_id": bundle_id,
            "target_mode": target_mode,
            "character": character,
            "decision_context": _review_context(raw_bundle.get("decision_context")),
            "candidates": [
                candidate for candidate in reviewed_candidates if candidate is not None
            ],
            "cohorts": [cohort for cohort in reviewed_cohorts if cohort is not None],
        },
        {
            "decisions": _review_decisions(decisions),
            "heard": _review_heard_records(heard),
        },
    )


def _create_audio_player(parent: QObject) -> QMediaPlayer:
    return QMediaPlayer(parent)


class MissingVoiceReuseReviewDialog(QDialog):
    """Review exact cohort samples while keeping failed arms visible."""

    def __init__(
        self,
        session_path: str | Path,
        parent: QWidget | None = None,
        *,
        thread_pool: QThreadPool | None = None,
        heard_recorder: HeardRecorder = _default_heard_recorder,
        decision_recorder: DecisionRecorder = _default_decision_recorder,
        confirmer: DecisionConfirmer | None = None,
    ) -> None:
        super().__init__(parent)
        self.session_path = Path(session_path).expanduser().resolve()
        self.heard_recorder: HeardRecorder = heard_recorder
        self.decision_recorder: DecisionRecorder = decision_recorder
        self.confirmer: DecisionConfirmer = confirmer or self._confirm_decision
        self.bundle, self.session = _review_data(_review_loader(self.session_path))
        self.heard_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.heard_runner.finished.connect(self._heard_saved)
        self.decision_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.decision_runner.finished.connect(self._decision_saved)
        self._pending_heard: list[HeardKey] = []
        self._saving_heard: HeardKey | None = None
        self._playing_key: HeardKey | None = None
        self._cohort: ReviewCohort | None = None
        self._sample_index = 0
        self._close_pending = False

        self.failed_control_mode = self.bundle.get("target_mode") == "failed"
        self._decision_name = (
            "failed-line fallback" if self.failed_control_mode else "family voice"
        )
        self.setWindowTitle(
            "Blind failed-line fallback review"
            if self.failed_control_mode
            else "Blind missing-voice reuse review"
        )
        self.setMinimumSize(820, 520)
        self.resize(1050, 650)

        self.progress = QLabel()
        self.progress.setWordWrap(True)
        self.progress.setAccessibleName("Missing voice review progress")
        instructions = (
            "The original production route failed its technical gate and has no "
            "playable WAV. Hear every available opaque fallback sample. Choose the "
            "fallback only if it completed every required sample and sounds "
            "acceptable; otherwise keep the exact lines unresolved."
            if self.failed_control_mode
            else "Compare opaque voices only within this family. Failed renders stay "
            "visible and cannot be selected. Finish every available sample, then "
            "choose one complete voice or Neither."
        )
        self.instructions = QLabel(instructions)
        self.instructions.setWordWrap(True)
        self.instructions.setAccessibleName("Missing voice review instructions")
        self.decision_context = ReviewDecisionContext()
        self._show_decision_context()
        self.cohort_heading = QLabel()
        self.cohort_heading.setWordWrap(True)
        self.cohort_heading.setAccessibleName("Current missing voice family")

        self.previous = QPushButton("Previous sample")
        self.sample_selector = QComboBox()
        self.sample_selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.sample_selector.setMinimumContentsLength(16)
        self.sample_selector.currentTextChanged.connect(self.sample_selector.setToolTip)
        self.sample_selector.setAccessibleName("Exact review sample")
        self.sample_selector.setAccessibleDescription(
            "Choose one exact sample from the current review family"
        )
        self.sample_label = QLabel("Sample")
        self.sample_label.setBuddy(self.sample_selector)
        self.next = QPushButton("Next sample")
        self.previous.setAccessibleName("Previous exact review sample")
        self.previous.setAccessibleDescription(
            "Select the previous sample in the current family"
        )
        self.next.setAccessibleName("Next exact review sample")
        self.next.setAccessibleDescription(
            "Select the next sample in the current family"
        )
        self.previous.setShortcut(QKeySequence("Alt+Left"))
        self.next.setShortcut(QKeySequence("Alt+Right"))
        self.previous.clicked.connect(lambda: self._move_sample(-1))
        self.next.clicked.connect(lambda: self._move_sample(1))
        self.sample_selector.currentIndexChanged.connect(self._select_sample)
        navigation = review_form_layout()
        navigation.addRow(self.sample_label, self.sample_selector)
        navigation.addRow(self.previous, self.next)

        self.sample_text = QLabel()
        self.sample_text.setWordWrap(True)
        self.sample_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.sample_text.setMinimumHeight(80)
        self.sample_text.setAccessibleName("Exact review sample text")
        sample_layout = QVBoxLayout()
        sample_layout.addLayout(navigation)
        sample_layout.addWidget(self.sample_text)
        sample_box = QGroupBox("Current exact sample")
        sample_box.setLayout(sample_layout)

        self.play_grid = review_form_layout()
        candidate_panels = []
        self.play_buttons: dict[str, QPushButton] = {}
        self.arm_statuses: dict[str, QLabel] = {}
        for column, candidate in enumerate(self.bundle["candidates"]):
            label = candidate["label"]
            button = QPushButton(f"Play {label}")
            button.setMinimumWidth(180)
            button.setAccessibleName(f"Play opaque candidate {label}")
            button.setAccessibleDescription(
                "Play this checksum-bound candidate to completion before deciding"
            )
            if column < 9:
                button.setShortcut(QKeySequence(f"Ctrl+{column + 1}"))
            button.clicked.connect(
                lambda _checked=False, value=label: self._play(value)
            )
            status = QLabel()
            status.setWordWrap(True)
            status.setAlignment(Qt.AlignmentFlag.AlignTop)
            status.setAccessibleName(f"Opaque candidate {label} status")
            panel = QWidget()
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(0, 0, 0, 0)
            panel_layout.addWidget(button)
            panel_layout.addWidget(status)
            candidate_panels.append(panel)
            self.play_buttons[label] = button
            self.arm_statuses[label] = status
        for index in range(0, len(candidate_panels), 2):
            self.play_grid.addRow(*candidate_panels[index : index + 2])
        playback_box = QGroupBox("Opaque candidate evidence")
        playback_box.setLayout(self.play_grid)

        self.now_playing = QLabel("READY")
        self.now_playing.setAccessibleName("Current missing voice playback")
        self.now_playing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.now_playing.setMinimumHeight(42)
        self.now_playing.setStyleSheet(
            "QLabel { background-color: #3f3f46; color: white; "
            "font-weight: 700; border-radius: 5px; padding: 7px; }"
        )
        self.stop = QPushButton("Stop audio")
        self.stop.setAccessibleName("Stop missing voice review audio")
        self.stop.setAccessibleDescription("Stop the current candidate playback")
        self.stop.setShortcut(QKeySequence("Ctrl+Space"))
        self.stop.clicked.connect(self._stop)
        playback_controls = QHBoxLayout()
        playback_controls.addWidget(self.now_playing, 1)
        playback_controls.addWidget(self.stop)

        self.decision_reason = QLabel()
        self.decision_reason.setWordWrap(True)
        self.decision_reason.setAccessibleName("Missing voice decision availability")
        self.decision_buttons: dict[str, QPushButton] = {}
        decisions = review_form_layout()
        for candidate in self.bundle["candidates"]:
            label = candidate["label"]
            button = QPushButton(
                f"Use fallback {label} for these lines"
                if self.failed_control_mode
                else f"Choose {label} for this family"
            )
            button.clicked.connect(
                lambda _checked=False, value=label: self._save_decision(value)
            )
            button.setAccessibleName(f"Choose opaque candidate {label}")
            button.setAccessibleDescription(
                f"Save candidate {label} as the {self._decision_name} decision"
            )
            self.decision_buttons[label] = button
        decision_buttons = tuple(self.decision_buttons.values())
        for index in range(0, len(decision_buttons), 2):
            decisions.addRow(*decision_buttons[index : index + 2])
        self.neither = QPushButton(
            "Keep these lines unresolved"
            if self.failed_control_mode
            else "Neither voice is acceptable"
        )
        self.neither.setShortcut(QKeySequence("Alt+N"))
        self.neither.setAccessibleName(
            "Keep failed lines unresolved"
            if self.failed_control_mode
            else "Reject all missing voice candidates"
        )
        self.neither.clicked.connect(lambda: self._save_decision("neither"))
        self.neither.setAccessibleDescription(
            "Keep this review unresolved instead of selecting a candidate"
        )
        decisions.addRow(self.neither)
        decision_layout = QVBoxLayout()
        decision_layout.addWidget(self.decision_reason)
        decision_layout.addLayout(decisions)
        decision_box = QGroupBox(
            "Failed-line decision" if self.failed_control_mode else "Family decision"
        )
        decision_box.setLayout(decision_layout)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Missing voice review operation status")
        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.close)
        self.close_button = close_buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setAccessibleName("Close missing voice review")
        self.close_button.setAccessibleDescription(
            "Close this review without making another decision"
        )

        review_content = QWidget()
        review_layout = QVBoxLayout(review_content)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.addWidget(self.progress)
        review_layout.addWidget(self.instructions)
        review_layout.addWidget(self.decision_context)
        review_layout.addWidget(self.cohort_heading)
        review_layout.addWidget(sample_box)
        review_layout.addWidget(playback_box)
        review_layout.addLayout(playback_controls)
        review_layout.addWidget(decision_box)
        review_layout.addWidget(self.status)
        self.review_scroll = review_scroll_area(
            review_content,
            "Scrollable missing voice review",
        )
        layout = QVBoxLayout(self)
        layout.addWidget(self.review_scroll, 1)
        layout.addWidget(close_buttons)

        self.player = _create_audio_player(self)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(self._playback_error)
        self._load_next_cohort()
        self.setTabOrder(self.decision_context.technical_toggle, self.previous)
        self.setTabOrder(self.previous, self.sample_selector)
        self.setTabOrder(self.sample_selector, self.next)
        prior = self.next
        for button in self.play_buttons.values():
            self.setTabOrder(prior, button)
            prior = button
        self.setTabOrder(prior, self.stop)
        prior = self.stop
        for button in self.decision_buttons.values():
            self.setTabOrder(prior, button)
            prior = button
        self.setTabOrder(prior, self.neither)
        self.setTabOrder(self.neither, self.close_button)

    def _show_decision_context(self) -> None:
        context = self.bundle["decision_context"]
        if context is None:
            self.decision_context.set_context(
                {
                    "purpose": (
                        "Choose a replacement WAV for a failed line"
                        if self.failed_control_mode
                        else "Choose a reusable voice for an unvoiced family"
                    ),
                    "game_speaker": self.bundle["character"],
                    "synthesis_voice": "Unknown (legacy review bundle)",
                    "reference": "Unknown (legacy review bundle)",
                    "backend": "Unknown (legacy review bundle)",
                    "model": "Unknown (legacy review bundle)",
                    "generation_profile": "Unknown (legacy review bundle)",
                    "controls": "Unknown (legacy review bundle)",
                    "effect": (
                        "select a checksum-bound fallback WAV or keep the line unresolved"
                        if self.failed_control_mode
                        else "bind one candidate to this cohort or keep it unbound"
                    ),
                },
                technical=(
                    f"Bundle: {self.bundle['bundle_id']}\n"
                    "This bundle predates published synthesis context; mutable "
                    "workspace settings are not guessed."
                ),
            )
            return
        model = context["model"]
        model_label = review_model_label(model)
        controls = f"{context['controls']} | Seed: {context['seed']}"
        technical = context["technical"]
        self.decision_context.set_context(
            {**context, "model": model_label, "controls": controls},
            technical=(
                f"Exact model: {model}\n"
                f"Plan: {technical['plan_id']}\n"
                f"Evidence workspaces: {', '.join(technical['workspace_ids'])}\n"
                f"Bundle: {self.bundle['bundle_id']}"
            ),
        )

    def _load_next_cohort(self) -> None:
        self._stop()
        self.bundle, self.session = _review_data(_review_loader(self.session_path))
        completed, total = _review_progress(self.bundle, self.session)
        self.progress.setText(
            f"Completed {completed} of {total} families | Remaining {total - completed}"
        )
        decisions = {
            value["cohort_id"]: value["decision"] for value in self.session["decisions"]
        }
        self._cohort = next(
            (
                cohort
                for cohort in self.bundle["cohorts"]
                if decisions[cohort["cohort_id"]] is None
            ),
            None,
        )
        self._sample_index = 0
        self.sample_selector.blockSignals(True)
        self.sample_selector.clear()
        self.sample_selector.setToolTip("")
        if self._cohort is None:
            automatic_count = sum(
                value.get("decision_origin") == AUTOMATIC_UNRESOLVED_ORIGIN
                for value in self.session["decisions"]
            )
            self.cohort_heading.setText("Review complete")
            if automatic_count:
                self.sample_text.setText(
                    f"{automatic_count} cohort(s) had no complete selectable candidate "
                    "and were kept unresolved automatically. No listening or human "
                    "confirmation is required."
                )
                self.status.setText(
                    "Any surviving WAV is optional diagnostic evidence only. "
                    "The automatic unresolved outcome is ready for decision import."
                )
            else:
                self.sample_text.setText("All exact families have a recorded decision.")
                self.status.setText(
                    "The blind key remains private until decision import."
                )
            self.sample_selector.blockSignals(False)
            self._set_all_actions(False)
            return
        for sample in self._cohort["samples"]:
            self.sample_selector.addItem(
                f"{sample['length_bucket'].title()} | {sample['line_id']}"
            )
        self.sample_selector.blockSignals(False)
        self.sample_selector.setToolTip(self.sample_selector.currentText())
        self.cohort_heading.setText(
            f"{'Failed-line group' if self.failed_control_mode else 'Family'} "
            f"{completed + 1} of {total} | "
            f"{self._cohort['sample_count']} required sample(s)"
        )
        self.status.setText(
            f"Replay remains available while the {self._decision_name} decision "
            "is saved in the background."
        )
        self._refresh_sample()

    def _select_sample(self, index: int) -> None:
        if self._cohort is None or index < 0:
            return
        self._stop()
        self._sample_index = index
        self._refresh_sample()

    def _move_sample(self, delta: int) -> None:
        if self._cohort is None:
            return
        index = max(
            0,
            min(len(self._cohort["samples"]) - 1, self._sample_index + delta),
        )
        self.sample_selector.setCurrentIndex(index)

    def _refresh_sample(self) -> None:
        if self._cohort is None:
            return
        sample = self._cohort["samples"][self._sample_index]
        self.sample_text.setText(sample["text"])
        heard = self._heard_keys()
        for candidate in self.bundle["candidates"]:
            label = candidate["label"]
            arm = next(
                value
                for value in candidate["samples"]
                if value["queue_id"] == sample["queue_id"]
            )
            button = self.play_buttons[label]
            if arm["status"] == "generated":
                was_heard = (sample["queue_id"], label) in heard
                button.setText(f"{'Replay' if was_heard else 'Play'} {label}")
                button.setEnabled(True)
                quality = arm["quality"] or {}
                duration = quality.get("duration_seconds")
                duration_text = (
                    f"{float(duration):.2f}s"
                    if isinstance(duration, (int, float))
                    else "duration unknown"
                )
                repair = arm["repair_strategy"] or "direct render"
                self.arm_statuses[label].setText(
                    f"AVAILABLE | {duration_text} | {repair}"
                )
            else:
                button.setText(f"{label} unavailable")
                button.setEnabled(False)
                self.arm_statuses[label].setText(
                    f"FAILED | {arm['failure_kind']} | attempts: {arm['attempt_count']}"
                )
        self.previous.setEnabled(self._sample_index > 0)
        self.next.setEnabled(self._sample_index + 1 < len(self._cohort["samples"]))
        self.stop.setEnabled(self._playing_key is not None)
        self._update_decisions()

    def _play(self, label: str) -> None:
        if self._cohort is None:
            return
        sample = self._cohort["samples"][self._sample_index]
        arm = next(
            value
            for candidate in self.bundle["candidates"]
            if candidate["label"] == label
            for value in candidate["samples"]
            if value["queue_id"] == sample["queue_id"]
        )
        if arm["status"] != "generated":
            return
        self._playing_key = (self._cohort["cohort_id"], sample["queue_id"], label)
        self.now_playing.setText(f"LOADING {label}")
        self.player.setSource(
            QUrl.fromLocalFile(str(self.session_path.parent / arm["audio"]))
        )
        self.player.play()

    def _stop(self) -> None:
        if hasattr(self, "player"):
            self.player.stop()
            self.player.setSource(QUrl())
        self._playing_key = None
        if hasattr(self, "now_playing"):
            self.now_playing.setText("READY")
        if hasattr(self, "stop"):
            self.stop.setEnabled(False)

    def _playback_state_changed(self, state: object) -> None:
        if (
            state == QMediaPlayer.PlaybackState.PlayingState
            and self._playing_key is not None
        ):
            self.now_playing.setText(f"PLAYING {self._playing_key[2]}")
            self.stop.setEnabled(True)

    def _media_status_changed(self, status: object) -> None:
        if status != QMediaPlayer.MediaStatus.EndOfMedia or self._playing_key is None:
            return
        key = self._playing_key
        self._playing_key = None
        self.now_playing.setText(f"FINISHED {key[2]}")
        self.stop.setEnabled(False)
        if key not in self._all_heard_records() and key not in self._pending_heard:
            self._pending_heard.append(key)
            self._start_next_heard_save()
        self._refresh_sample()

    def _start_next_heard_save(self) -> None:
        if self.heard_runner.active or not self._pending_heard:
            return
        self._saving_heard = self._pending_heard.pop(0)
        self.status.setText(
            "Saving heard evidence in background. Playback and replay remain available."
        )
        self.heard_runner.start(
            self.heard_recorder, self.session_path, *self._saving_heard
        )
        self._update_decisions()

    def _heard_saved(self, _result: object, error: Exception | None) -> None:
        saved = self._saving_heard
        self._saving_heard = None
        if error is not None:
            self.status.setText(f"HEARD SAVE FAILED: {error}. Replay to retry.")
        else:
            try:
                self.bundle, self.session = _review_data(
                    _review_loader(self.session_path)
                )
                self.status.setText(
                    "Heard evidence saved. Playback and replay remain available."
                )
            except Exception as refresh_error:
                self.status.setText(f"HEARD SAVED, REFRESH FAILED: {refresh_error}")
        if saved is not None and error is not None:
            self._pending_heard = [
                value for value in self._pending_heard if value != saved
            ]
        self._start_next_heard_save()
        self._refresh_sample()
        if (
            self._close_pending
            and not self.heard_runner.active
            and not self._pending_heard
        ):
            self._close_pending = False
            self.close()

    def _heard_keys(self) -> set[tuple[str, str]]:
        if self._cohort is None:
            return set()
        cohort_id = self._cohort["cohort_id"]
        return (
            {
                (value["queue_id"], value["label"])
                for value in self.session["heard"]
                if value["cohort_id"] == cohort_id
            }
            | {
                (queue_id, label)
                for current_cohort, queue_id, label in self._pending_heard
                if current_cohort == cohort_id
            }
            | (
                {(self._saving_heard[1], self._saving_heard[2])}
                if self._saving_heard is not None and self._saving_heard[0] == cohort_id
                else set()
            )
        )

    def _all_heard_records(self) -> set[HeardKey]:
        return {
            (value["cohort_id"], value["queue_id"], value["label"])
            for value in self.session["heard"]
        }

    def _required_heard(self) -> set[tuple[str, str]]:
        if self._cohort is None:
            return set()
        queue_ids = {sample["queue_id"] for sample in self._cohort["samples"]}
        return {
            (sample["queue_id"], candidate["label"])
            for candidate in self.bundle["candidates"]
            for sample in candidate["samples"]
            if sample["queue_id"] in queue_ids and sample["status"] == "generated"
        }

    def _update_decisions(self) -> None:
        ready = (
            self._cohort is not None
            and not self.heard_runner.active
            and not self._pending_heard
            and self._heard_keys() == self._required_heard()
            and not self.decision_runner.active
        )
        complete = (
            set(self._cohort["complete_candidate_labels"])
            if self._cohort is not None
            else set()
        )
        for label, button in self.decision_buttons.items():
            button.setVisible(label in complete)
            button.setEnabled(ready and label in complete)
        self.neither.setEnabled(ready)
        if self._cohort is None:
            self.decision_reason.setText("Review complete.")
        elif self.decision_runner.active:
            self.decision_reason.setText(
                f"Saving the {self._decision_name} decision in background. "
                "Replay remains available."
            )
        elif not complete:
            self.decision_reason.setText(
                "No candidate completed every required sample. Hear available evidence, "
                "then choose Neither; incomplete candidates cannot win by omission."
            )
        elif ready:
            self.decision_reason.setText(
                f"Decision ready. Choose one complete {self._decision_name} "
                "candidate or Neither."
            )
        else:
            remaining = len(self._required_heard() - self._heard_keys())
            self.decision_reason.setText(
                f"Decision locked: finish {remaining} available sample(s)."
            )

    def _save_decision(self, decision: str) -> None:
        if self._cohort is None or self.decision_runner.active:
            return
        if not self.confirmer(decision):
            self.status.setText("Decision cancelled; review evidence is unchanged.")
            return
        self.status.setText(
            f"Saving {self._decision_name} decision in background. "
            "Replay remains available."
        )
        self.decision_runner.start(
            self.decision_recorder,
            self.session_path,
            self._cohort["cohort_id"],
            decision,
        )
        self._update_decisions()

    def _confirm_decision(self, decision: str) -> bool:
        label = "Neither" if decision == "neither" else f"candidate {decision}"
        answer = QMessageBox.question(
            self,
            "Save irreversible review decision?",
            f"Save {label} for this {self._decision_name}? "
            "This review window cannot revise it afterward.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return bool(answer == QMessageBox.StandardButton.Yes)

    def _decision_saved(self, _result: object, error: Exception | None) -> None:
        if error is not None:
            self.status.setText(
                f"SAVE FAILED: {error}. Replay and retry remain available."
            )
            self._update_decisions()
        else:
            try:
                self._load_next_cohort()
            except Exception as refresh_error:
                self.status.setText(f"SAVED, BUT REFRESH FAILED: {refresh_error}")
                self._set_all_actions(False)
        if self._close_pending:
            self._close_pending = False
            self.close()

    def _playback_error(self, _error: object, error_string: str) -> None:
        self._playing_key = None
        self.now_playing.setText("PLAYBACK FAILED")
        self.stop.setEnabled(False)
        self.status.setText(f"PLAYBACK FAILED: {error_string}. Replay is available.")

    def _set_all_actions(self, enabled: bool) -> None:
        for button in self.play_buttons.values():
            button.setEnabled(enabled)
        for button in self.decision_buttons.values():
            button.setEnabled(enabled)
        self.neither.setEnabled(enabled)
        self.previous.setEnabled(enabled)
        self.next.setEnabled(enabled)
        self.sample_selector.setEnabled(enabled)
        self.stop.setEnabled(enabled and self._playing_key is not None)

    def closeEvent(self, event: QCloseEvent) -> None:
        if (
            self.heard_runner.active
            or self._pending_heard
            or self.decision_runner.active
        ):
            self._close_pending = True
            self.status.setText(
                "Close requested; waiting for the current background save to finish."
            )
            event.ignore()
            return
        self._stop()
        super().closeEvent(event)


def launch_missing_voice_reuse_review(session_path: str | Path) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = MissingVoiceReuseReviewDialog(session_path)
    except MissingVoiceReuseReviewError as error:
        QMessageBox.critical(
            None,
            "Unable to open missing-voice review",
            f"Session: {Path(session_path).expanduser()}\n\n{error}",
        )
        return 2
    dialog.show()
    return app.exec()


__all__ = ["MissingVoiceReuseReviewDialog", "launch_missing_voice_reuse_review"]

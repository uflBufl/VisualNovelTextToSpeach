"""Qt operator interface for checksum-bound failed-reference audits."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Literal, Protocol, TypeAlias, TypedDict, TypeGuard

from PySide6.QtCore import QObject, QThreadPool, QUrl
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.failure_reference_audit import (
    FailureReferenceAudio,
    FailureReferenceAudit,
    load_failure_reference_audit,
    load_failure_reference_decisions,
    prepare_failure_reference_audio,
    record_failure_reference_decision,
)
from vntts.authoring.failure_reference_preview import (
    FailureReferencePreview,
    FailureReferencePreviewCancelled,
    FailureReferencePreviewService,
)
from vntts.authoring.review_context_ui import (
    ReviewDecisionContext,
    review_form_layout,
    review_model_label,
    review_scroll_area,
)
from vntts.qt_audio import QtPcmPlayer as QMediaPlayer


class AuditCandidate(TypedDict):
    candidate_id: str


class AuditCase(TypedDict):
    queue_id: str
    line_id: str
    speaker: str
    text: str


class AuditGroup(TypedDict):
    group_id: str
    synthesis_voice_character: str
    case_count: int
    candidates: list[AuditCandidate]
    cases: list[AuditCase]


class AuditDocument(TypedDict):
    audit_id: str
    workspace: str
    workspace_id: str
    groups: list[AuditGroup]


class AuditDecision(TypedDict):
    group_id: str
    decision: str


class AuditDecisions(TypedDict):
    decisions: list[AuditDecision]
    decision_set_id: str | None


class _AudioPlayer(Protocol):
    def stop(self) -> None: ...

    def play_bytes(self, payload: bytes, source: str) -> object | None: ...

    def setSource(self, source: QUrl) -> None: ...


AuditLoader: TypeAlias = Callable[[str | Path], FailureReferenceAudit]
DecisionLoader: TypeAlias = Callable[[Path], object]
AudioPreparer: TypeAlias = Callable[[Path, str, str], FailureReferenceAudio]
DecisionRecorder: TypeAlias = Callable[[Path, str, str], object]
PreviewServiceFactory: TypeAlias = Callable[[Path], "_PreviewService"]
AudioBytesPlayer: TypeAlias = Callable[
    [_AudioPlayer, QObject | None, bytes, str], object | None
]
AudioBufferReleaser: TypeAlias = Callable[[_AudioPlayer, object | None], None]


class _PreviewService(Protocol):
    def generate(
        self, group_id: str, candidate_id: str, text: str
    ) -> FailureReferencePreview: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


_audit_loader: AuditLoader = load_failure_reference_audit
_decision_loader: DecisionLoader = load_failure_reference_decisions
_default_audio_preparer: AudioPreparer = prepare_failure_reference_audio
_default_decision_recorder: DecisionRecorder = record_failure_reference_decision
_preview_service_factory: PreviewServiceFactory = FailureReferencePreviewService


def _play_audio_bytes(
    player: _AudioPlayer, _parent: QObject | None, payload: bytes, source: str
) -> object | None:
    return player.play_bytes(payload, source)


def _release_audio_buffer(player: _AudioPlayer, _buffer: object | None) -> None:
    player.setSource(QUrl())


_audio_bytes_player: AudioBytesPlayer = _play_audio_bytes
_audio_buffer_releaser: AudioBufferReleaser = _release_audio_buffer


def _is_audit_document(value: object) -> TypeGuard[AuditDocument]:
    return (
        isinstance(value, dict)
        and isinstance(value.get("audit_id"), str)
        and isinstance(value.get("workspace"), str)
        and isinstance(value.get("workspace_id"), str)
        and isinstance(value.get("groups"), list)
        and all(
            isinstance(group, dict)
            and isinstance(group.get("group_id"), str)
            and isinstance(group.get("synthesis_voice_character"), str)
            and isinstance(group.get("case_count"), int)
            and isinstance(group.get("candidates"), list)
            and isinstance(group.get("cases"), list)
            and all(
                isinstance(candidate, dict)
                and isinstance(candidate.get("candidate_id"), str)
                for candidate in group["candidates"]
            )
            and all(
                isinstance(case, dict)
                and all(
                    isinstance(case.get(field), str)
                    for field in ("queue_id", "line_id", "speaker", "text")
                )
                for case in group["cases"]
            )
            for group in value["groups"]
        )
    )


def _audit_decisions(value: object) -> AuditDecisions:
    if not isinstance(value, dict) or not isinstance(value.get("decisions"), list):
        raise RuntimeError("Reference audit decisions are malformed")
    decisions: list[AuditDecision] = []
    for decision in value["decisions"]:
        if (
            not isinstance(decision, dict)
            or not isinstance(decision.get("group_id"), str)
            or not isinstance(decision.get("decision"), str)
        ):
            raise RuntimeError("Reference audit decisions are malformed")
        decisions.append(
            {"group_id": decision["group_id"], "decision": decision["decision"]}
        )
    decision_set_id = value.get("decision_set_id")
    if not isinstance(decision_set_id, (str, type(None))):
        raise RuntimeError("Reference audit decisions are malformed")
    return {"decisions": decisions, "decision_set_id": decision_set_id}


def _load_public_document(
    audit: str | Path,
) -> tuple[FailureReferenceAudit, AuditDocument, AuditDecisions]:
    validated = _audit_loader(audit)
    path = validated.directory / "audit.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    actual = canonical_document_sha256(
        {name: value for name, value in document.items() if name != "audit_id"}
    )
    if actual != validated.audit_id:
        raise RuntimeError("Reference audit changed while opening the interface")
    if not _is_audit_document(document):
        raise RuntimeError("Reference audit document is malformed")
    return validated, document, _audit_decisions(_decision_loader(validated.directory))


class FailureReferenceAuditDialog(QDialog):
    """Review four exact control groups without revealing private source names."""

    def __init__(
        self,
        audit: str | Path,
        parent: QWidget | None = None,
        *,
        audio_preparer: AudioPreparer = _default_audio_preparer,
        decision_recorder: DecisionRecorder = _default_decision_recorder,
        preview_service_factory: PreviewServiceFactory = _preview_service_factory,
    ) -> None:
        super().__init__(parent)
        self.audit, self.document, decisions = _load_public_document(audit)
        self.audio_preparer: AudioPreparer = audio_preparer
        self.decision_recorder: DecisionRecorder = decision_recorder
        self.preview_service: _PreviewService = preview_service_factory(
            self.audit.directory
        )
        self.decisions = {value["group_id"]: value for value in decisions["decisions"]}
        self._playback_active = False
        self._save_active = False
        self._preview_active = False
        self._preview_result: FailureReferencePreview | None = None
        self._playback_buffer: object | None = None
        self._playback_target: tuple[str, str, str] | None = None
        self._playback_kind: Literal["reference", "generated"] | None = None
        self._heard_candidates: dict[str, set[str]] = {}

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
        self.decision_context = ReviewDecisionContext()
        workspace = json.loads(
            (Path(self.document["workspace"]) / "workspace.json").read_text(
                encoding="utf-8"
            )
        )
        run_config = (
            workspace.get("run_config") if isinstance(workspace, dict) else None
        )
        if not isinstance(run_config, dict):
            raise RuntimeError("Reference audit workspace configuration is malformed")
        self._run_config: dict[str, object] = {
            str(key): value for key, value in run_config.items() if isinstance(key, str)
        }
        self.progress = QProgressBar()
        self.progress.setRange(0, len(self.document["groups"]))
        self.progress.setAccessibleName("Reference groups decided")
        self.progress.setFormat("%v of %m groups decided")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Current failed-reference review summary")
        self.status = QLabel(
            "READY: compare source audio or generate a non-authoritative voice sample."
        )
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Failed-reference review status")
        self.group_choice = QComboBox()
        self.group_choice.setAccessibleName("Failed-reference control group")
        self.group_choice.setAccessibleDescription(
            "Choose one failed-reference group to review"
        )
        self.group_label = QLabel("Reference group")
        self.group_label.setBuddy(self.group_choice)
        self.candidate_choice = QComboBox()
        self.candidate_choice.setAccessibleName("Blinded reference candidate")
        self.candidate_choice.setAccessibleDescription(
            "Choose one blinded source recording from the current group"
        )
        self.candidate_label = QLabel("Candidate")
        self.candidate_label.setBuddy(self.candidate_choice)
        self.group_choice.currentIndexChanged.connect(self._show_group)
        self.candidate_choice.currentIndexChanged.connect(self._candidate_changed)
        self.preview_text_choice = QComboBox()
        self.preview_text_choice.setAccessibleName("Generated preview phrase")
        self.preview_text_choice.setAccessibleDescription(
            "Choose one affected line to synthesize with the selected reference"
        )
        self.preview_text_label = QLabel("Preview phrase")
        self.preview_text_label.setBuddy(self.preview_text_choice)
        self.preview_text_choice.currentIndexChanged.connect(self._preview_text_changed)
        for choice in (
            self.group_choice,
            self.candidate_choice,
            self.preview_text_choice,
        ):
            choice.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            choice.setMinimumContentsLength(16)
            choice.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Fixed,
            )

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
        self.technical_details = QCheckBox("Affected failed lines")
        self.technical_details.setAccessibleName("Show affected failed line details")
        self.technical_details.setAccessibleDescription(
            "Reveal the exact failed lines affected by this reference decision"
        )
        self.technical_details.toggled.connect(self.cases.setVisible)
        self.cases.setVisible(False)

        self.play = QPushButton("Play reference")
        self.stop = QPushButton("Stop")
        self.play.setAccessibleName("Play selected source candidate")
        self.play.setAccessibleDescription(
            "Play the selected checksum-bound source candidate through to the end"
        )
        self.stop.setAccessibleName("Stop source candidate playback")
        self.stop.setAccessibleDescription("Stop source or generated preview playback")
        self.generate_preview = QPushButton("Generate sample")
        self.replay_preview = QPushButton("Replay preview")
        self.cancel_preview = QPushButton("Cancel generation")
        self.generate_preview.setAccessibleName("Generate optional voice sample")
        self.replay_preview.setAccessibleName("Replay optional generated sample")
        self.cancel_preview.setAccessibleName("Cancel optional sample generation")
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
        self.neither = QPushButton("No suitable reference")
        self.choose.setAccessibleName("Use selected source reference")
        self.neither.setAccessibleName("Reject all source reference candidates")
        self.choose.setAccessibleDescription(
            "Select this source recording for later explicit voice binding"
        )
        self.neither.setAccessibleDescription(
            "Record that the available source recordings are unsuitable; this does "
            "not reject the character"
        )
        self.previous = QPushButton("Previous group")
        self.next = QPushButton("Next group")
        self.previous.setAccessibleName("Previous failed-reference group")
        self.previous.setAccessibleDescription("Select the previous reference group")
        self.next.setAccessibleName("Next failed-reference group")
        self.next.setAccessibleDescription("Select the next reference group")
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

        playback = review_form_layout()
        playback.addRow(self.candidate_label, self.candidate_choice)
        playback.addRow(self.play, self.stop)
        self.preview_toggle = QToolButton()
        self.preview_toggle.setText("Generated preview")
        self.preview_toggle.setCheckable(True)
        self.preview_toggle.setChecked(False)
        self.preview_toggle.setAccessibleName("Show optional generated preview")
        self.preview_toggle.setAccessibleDescription(
            "Reveal non-authoritative generated preview controls"
        )
        self.preview_panel = QWidget()
        preview = review_form_layout(self.preview_panel)
        preview.setContentsMargins(0, 0, 0, 0)
        preview.addRow(self.preview_text_label, self.preview_text_choice)
        preview.addRow(self.generate_preview)
        preview.addRow(self.replay_preview, self.cancel_preview)
        self.preview_panel.hide()
        self.preview_toggle.toggled.connect(self.preview_panel.setVisible)
        decisions_row = review_form_layout()
        decisions_row.addRow(self.choose, self.neither)
        decisions_row.addRow(self.previous, self.next)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setAccessibleName("Close failed-reference audit")
        self.close_button.setAccessibleDescription(
            "Close this audit without making another reference decision"
        )

        review_content = QWidget()
        review_layout = QVBoxLayout(review_content)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.addWidget(self.heading)
        review_layout.addWidget(self.explanation)
        review_layout.addWidget(self.decision_context)
        review_layout.addWidget(self.progress)
        review_layout.addWidget(self.summary)
        review_layout.addWidget(self.status)
        review_layout.addWidget(self.group_label)
        review_layout.addWidget(self.group_choice)
        review_layout.addWidget(self.candidate_heading)
        review_layout.addWidget(self.candidate_heard)
        review_layout.addLayout(playback)
        review_layout.addWidget(self.preview_toggle)
        review_layout.addWidget(self.preview_panel)
        review_layout.addWidget(self.action_reason)
        review_layout.addLayout(decisions_row)
        review_layout.addWidget(self.technical_details)
        review_layout.addWidget(self.cases)
        self.review_scroll = review_scroll_area(
            review_content,
            "Scrollable failed-reference audit",
        )
        layout = QVBoxLayout(self)
        layout.addWidget(self.review_scroll, 1)
        layout.addWidget(buttons)

        self.player = QMediaPlayer(self)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(self._media_error)

        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(2)
        self._playback_runner = LatestTaskRunner(self, thread_pool=self.thread_pool)
        self._playback_runner.finished.connect(self._playback_finished)
        self._save_runner = LatestTaskRunner(self, thread_pool=self.thread_pool)
        self._save_runner.finished.connect(self._save_finished)
        self._preview_runner = LatestTaskRunner(self, thread_pool=self.thread_pool)
        self._preview_runner.finished.connect(self._preview_finished)

        QShortcut(QKeySequence("Ctrl+Alt+R"), self, self.play_selected)
        QShortcut(QKeySequence("Ctrl+Alt+S"), self, self.stop_playback)
        QShortcut(QKeySequence("Ctrl+Alt+G"), self, self.generate_selected_preview)
        QShortcut(QKeySequence("Ctrl+Alt+P"), self, self.replay_generated_preview)
        QShortcut(QKeySequence("Ctrl+Alt+Left"), self, lambda: self._move_group(-1))
        QShortcut(QKeySequence("Ctrl+Alt+Right"), self, lambda: self._move_group(1))

        self.setTabOrder(self.decision_context.technical_toggle, self.group_choice)
        self.setTabOrder(self.group_choice, self.candidate_choice)
        self.setTabOrder(self.candidate_choice, self.play)
        self.setTabOrder(self.play, self.stop)
        self.setTabOrder(self.stop, self.preview_toggle)
        self.setTabOrder(self.preview_toggle, self.preview_text_choice)
        self.setTabOrder(self.preview_text_choice, self.generate_preview)
        self.setTabOrder(self.generate_preview, self.replay_preview)
        self.setTabOrder(self.replay_preview, self.cancel_preview)
        self.setTabOrder(self.cancel_preview, self.choose)
        self.setTabOrder(self.choose, self.neither)
        self.setTabOrder(self.neither, self.previous)
        self.setTabOrder(self.previous, self.next)
        self.setTabOrder(self.next, self.technical_details)
        self.setTabOrder(self.technical_details, self.close_button)

        for index, group in enumerate(self.document["groups"], start=1):
            voice = group["synthesis_voice_character"]
            self.group_choice.addItem(
                f"{index}/{len(self.document['groups'])}: {voice} "
                f"({group['case_count']} failed lines)",
                group["group_id"],
            )
        self._show_group()

    def _current_group(self) -> AuditGroup | None:
        index = self.group_choice.currentIndex()
        if index < 0:
            return None
        return self.document["groups"][index]

    def _show_group(self) -> None:
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
        self.technical_details.setText(f"Affected lines: {len(group['cases'])}")
        self.summary.setText(
            f"Reference group {self.group_choice.currentIndex() + 1}/"
            f"{self.group_choice.count()} | {completed}/{self.group_choice.count()} "
            f"decided | Voice target: {group['synthesis_voice_character']} | "
            f"Current decision: {decision_text}.\n"
            "This records reference evidence only; it does not approve generated speech."
        )
        speakers = sorted({str(case["speaker"]) for case in group["cases"]})
        speaker = ", ".join(speakers) if speakers else "Unknown"
        model = str(self._run_config.get("model") or "Unknown")
        self.decision_context.set_context(
            {
                "purpose": "Choose source audio suitable for voice cloning",
                "game_speaker": speaker,
                "synthesis_voice": group["synthesis_voice_character"],
                "reference": "Current candidate is blinded until decision import",
                "backend": self._run_config.get("backend") or "Unknown",
                "model": review_model_label(model),
                "generation_profile": (
                    self._run_config.get("generation_profile") or "Unknown"
                ),
                "controls": "Generated preview uses seed 0; source playback is original",
                "effect": (
                    "select reference evidence for later binding; it does not approve "
                    "a character or generated WAV"
                ),
            },
            technical=(
                f"Exact model: {model}\n"
                f"Workspace: {self.document['workspace_id']}\n"
                f"Audit: {self.document['audit_id']}\n"
                f"Reference group: {group['group_id']}"
            ),
        )
        self._update_candidate_card()
        self._update_actions()

    def _candidate_changed(self) -> None:
        self.stop_playback()
        self._update_candidate_card()
        self._update_actions()

    def _preview_text_changed(self) -> None:
        self.stop_playback()
        self._update_actions()

    def _candidate_position(self) -> tuple[int, int]:
        index = self.candidate_choice.currentIndex()
        return (index + 1, self.candidate_choice.count())

    def _current_heard_candidates(self) -> set[str]:
        group = self._current_group()
        if group is None:
            return set()
        return self._heard_candidates.setdefault(group["group_id"], set())

    def _all_current_candidates_heard(self) -> bool:
        group = self._current_group()
        if group is None:
            return False
        expected = {candidate["candidate_id"] for candidate in group["candidates"]}
        return expected.issubset(self._current_heard_candidates())

    def _update_candidate_card(self) -> None:
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
            self.neither.setText("Reject reference")
        else:
            self.choose.setText(f"Use Candidate {position}")
            self.neither.setText("No suitable reference")

    def play_selected(self) -> None:
        group = self._current_group()
        candidate_id = self.candidate_choice.currentData()
        if group is None or not isinstance(candidate_id, str):
            return
        self.stop_playback()
        self._playback_active = True
        self.status.setText("Loading and verifying copied reference audio...")
        self._playback_runner.start(
            self.audio_preparer,
            self.audit.directory,
            group["group_id"],
            candidate_id,
        )
        self._update_actions()

    def _playback_finished(self, audio: object, error: Exception | None) -> None:
        self._playback_active = False
        if error is not None:
            self.status.setText(f"BLOCKED: {error}")
            self._update_actions()
            return
        if not isinstance(audio, FailureReferenceAudio):
            self.status.setText("BLOCKED: prepared reference audio is malformed")
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
        playback = _audio_bytes_player(
            self.player, self, audio.payload, f"memory:{audio.path.name}"
        )
        if playback is None:
            self.status.setText("BLOCKED: unable to open immutable audio buffer")
            self._update_actions()
            return
        self._playback_buffer = playback
        self._playback_target = (audio.group_id, audio.candidate_id, audio.sha256)
        self._playback_kind = "reference"
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

    def stop_playback(self) -> None:
        self.player.stop() if hasattr(self, "player") else None
        if hasattr(self, "player"):
            _audio_buffer_releaser(self.player, self._playback_buffer)
        self._playback_buffer = None
        self._playback_target = None
        self._playback_kind = None
        if hasattr(self, "play"):
            self._update_actions()

    def generate_selected_preview(self) -> None:
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
        self._preview_active = True
        self.status.setText(
            "GENERATING: loading the workspace model if needed and rendering one "
            "deterministic sample in the background. No authoring state is written."
        )
        self._preview_runner.start(
            self.preview_service.generate,
            group["group_id"],
            candidate_id,
            text,
        )
        self._update_actions()

    def cancel_preview_generation(self) -> None:
        if not self._preview_active:
            return
        self.preview_service.cancel()
        self.status.setText(
            "CANCELLING: waiting for the preview worker to stop safely."
        )
        self._update_actions()

    def _preview_finished(self, preview: object, error: Exception | None) -> None:
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
        if not isinstance(preview, FailureReferencePreview):
            self.status.setText("BLOCKED: generated preview is malformed")
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

    def replay_generated_preview(self) -> None:
        preview = self._preview_result
        if preview is not None and self._preview_matches_selection():
            self._play_generated_preview(preview)

    def _play_generated_preview(self, preview: FailureReferencePreview) -> None:
        self.stop_playback()
        playback = _audio_bytes_player(
            self.player, self, preview.payload, "memory:generated-preview.wav"
        )
        if playback is None:
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
        self.status.setText(
            f"PLAYING GENERATED SAMPLE: {preview.backend}, "
            f"{preview.generation_profile}, seed {preview.seed}. This is optional "
            "evidence and does not select the reference."
        )
        self._update_actions()

    def _preview_matches_selection(self) -> bool:
        preview = self._preview_result
        group = self._current_group()
        return bool(
            preview is not None
            and group is not None
            and preview.group_id == group["group_id"]
            and preview.candidate_id == self.candidate_choice.currentData()
            and preview.text == self.preview_text_choice.currentData()
        )

    def choose_selected(self) -> None:
        candidate_id = self.candidate_choice.currentData()
        if isinstance(candidate_id, str):
            self.save_decision(candidate_id)

    def save_decision(self, decision: str) -> None:
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
        self._save_active = True
        self.status.setText(
            "Saving checksum-bound reference decision in the background; playback "
            "remains available."
        )
        self._save_runner.start(
            self.decision_recorder,
            self.audit.directory,
            group["group_id"],
            decision,
        )
        self._update_actions()

    def _save_finished(self, document: object, error: Exception | None) -> None:
        self._save_active = False
        if error is not None:
            self.status.setText(f"BLOCKED: decision was not saved: {error}")
            self._update_actions()
            return
        decisions = _audit_decisions(document)
        self.decisions = {value["group_id"]: value for value in decisions["decisions"]}
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

    def _move_group(self, offset: int) -> None:
        count = self.group_choice.count()
        if count:
            self.group_choice.setCurrentIndex(
                (self.group_choice.currentIndex() + offset) % count
            )

    def _media_status_changed(self, status: object) -> None:
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

    def _media_error(self, _error: object, error_string: str) -> None:
        if error_string:
            self.status.setText(f"BLOCKED: audio playback failed: {error_string}")
        self._playback_buffer = None
        self._playback_target = None
        self._playback_kind = None
        self._update_actions()

    def _update_actions(self) -> None:
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
            group = self._current_group()
            if group is None:
                return
            total = len(group["candidates"])
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

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._playback_active or self._save_active or self._preview_active:
            self.status.setText(
                "Close deferred until the current checksum-bound task finishes."
            )
            event.ignore()
            return
        self.stop_playback()
        self.preview_service.close()
        event.accept()


def launch_failure_reference_audit(audit_directory: str | Path) -> int:
    application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = FailureReferenceAuditDialog(audit_directory)
    except Exception as error:
        QMessageBox.critical(None, "Unable to open failed-reference audit", str(error))
        return 1
    dialog.show()
    return application.exec()


def failure_reference_audit_status(
    audit_directory: str | Path,
) -> dict[str, str | int | None]:
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


def main(argv: list[str] | None = None) -> int:
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

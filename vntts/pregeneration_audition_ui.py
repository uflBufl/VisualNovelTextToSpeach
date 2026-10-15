"""One-at-a-time voice verification for self-service pregeneration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Event
from traceback import format_exception
from typing import Protocol, TypeAlias, cast

from PySide6.QtCore import (
    QEvent,
    QObject,
    QSignalBlocker,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QPixmap, QTextOption
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.voice_manifest import normalize_character_name

from vntts.async_ui import LatestTaskRunner
from vntts.document_identity import sha256_file
from vntts.path_safety import read_regular_bytes
from vntts.pregeneration_audition import (
    COMPARISON_PREVIEW_TEXT,
    VoiceAuditionCancelled,
    VoiceAuditionError,
    VoiceAuditionIncomplete,
    VoiceAuditionPreview,
    VoiceAuditionPreviewService,
    VoiceAuditionRejected,
    preview_identity,
)
from vntts.pregeneration_voices import (
    UNLINKED_BANK_LABEL,
    UNLINKED_BANK_MEDIA,
    VoiceCandidate,
    VoiceGroup,
    VoicePlan,
    is_playable_main_voice_source,
)
from vntts.preview_content_advisor import (
    PreviewContentAdvisor,
    PreviewContentCheck,
    PreviewContentCheckCancelled,
)
from vntts.qt_audio import QtPcmPlayer as QMediaPlayer
from vntts.reference_recommendation import (
    cached_reference_checks,
    recommended_reference,
)
from vntts.speech_presentation import speech_runtime_label
from vntts.voices import default_voice_choice_id


class _PreviewAudio(Protocol):
    @property
    def path(self) -> Path: ...

    @property
    def audio_sha256(self) -> str: ...


CandidateEntry: TypeAlias = tuple[VoiceCandidate, str, bool]
PreviewKey: TypeAlias = tuple[str, str | None]
PendingDecision: TypeAlias = tuple[VoiceGroup, str]


@dataclass(frozen=True)
class _PreviewRequest:
    plan: VoicePlan
    group: VoiceGroup
    candidate: VoiceCandidate
    text: str | None
    cancellation: Event


class _VoiceDecisionRecorder(Protocol):
    def remember_many(self, selections: tuple[PendingDecision, ...]) -> None: ...


class _VoiceAuditionPreviewer(Protocol):
    @property
    def backend(self) -> object | None: ...

    def generate(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
        cancel_event: Event | None = None,
        progress: Callable[[str], object] | None = None,
    ) -> _PreviewAudio: ...

    def reference_audio(
        self, plan: VoicePlan, group: VoiceGroup, candidate_source_id: str
    ) -> Path | None: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


class _PrefetchVoiceAuditionPreviewer(_VoiceAuditionPreviewer, Protocol):
    def peek(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
    ) -> _PreviewAudio | None: ...

    def prefetch(
        self,
        plan: VoicePlan,
        requests: tuple[tuple[VoiceGroup, str, str | None], ...],
        *,
        automatic: bool = False,
    ) -> None: ...

    def preparation_state(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
    ) -> str: ...

    def new_session(self) -> _VoiceAuditionPreviewer: ...


class _PreviewPlayer(Protocol):
    def stop(self) -> None: ...

    def play_bytes(self, payload: bytes, source: str) -> object | None: ...


_preview_service_factory: Callable[[], _VoiceAuditionPreviewer] = (
    VoiceAuditionPreviewService
)
_speech_runtime_label: Callable[[object | None], str] = speech_runtime_label


class VoiceAuditionUIError(RuntimeError):
    """The player audition card received an invalid unresolved plan."""


def _read_verified_audio(path: Path, expected_sha256: str | None) -> bytes:
    payload = read_regular_bytes(path)
    if sha256(payload).hexdigest() != expected_sha256:
        raise VoiceAuditionUIError("Voice sample changed after validation")
    return payload


def _candidate_name(candidate: VoiceCandidate) -> str:
    return (
        "Game voice"
        if candidate.source_character.startswith("Player candidate ")
        else candidate.source_character
    )


class VoiceAuditionPanel(QGroupBox):
    completed = Signal()
    cancelled = Signal()
    saveFailed = Signal()

    @property
    def saved_inspection_choice(self) -> PendingDecision | None:
        if (
            self._inspection_mode
            and self._save_succeeded
            and len(self._pending_decisions) == 1
        ):
            return self._pending_decisions[0]
        return None

    def __init__(
        self,
        decisions: _VoiceDecisionRecorder,
        *,
        preview_service: _VoiceAuditionPreviewer | None = None,
        content_advisor: PreviewContentAdvisor | None = None,
        thread_pool: QThreadPool | None = None,
        player: _PreviewPlayer | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.decisions = decisions
        self._owns_preview_service = preview_service is None
        self.preview_service: _VoiceAuditionPreviewer = (
            preview_service or _preview_service_factory()
        )
        self.preview_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.preview_runner.finished.connect(self._preview_finished)
        self.decision_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.decision_runner.finished.connect(self._decision_finished)
        self.player: _PreviewPlayer | None = player
        self._plan: VoicePlan | None = None
        self._groups: tuple[VoiceGroup, ...] = ()
        self._group_index = 0
        self._candidate_offset = 0
        self._candidate_entries: tuple[CandidateEntry, ...] = ()
        self._initialize_preview_state()
        self._failed_candidate_source_ids: set[str] = set()
        self._accepted_choice: str | None = None
        self._sample_text: str | None = None
        self._pending_decisions: list[PendingDecision] = []
        self._save_succeeded = False
        self._cancel_requested = False
        self._terminal_emitted = False
        self._ignore_preview_result = False
        self._shutdown_requested = False
        self._inspection_mode = False
        self._show_portraits = False
        self._playing_source: str | None = None

        self.summary = QLabel()
        self.summary.setAccessibleName("Voice choice estimate")
        self.summary.setWordWrap(True)
        self.choose_all_button = QPushButton("Choose all automatically")
        self.choose_all_button.clicked.connect(self.choose_all_automatically)
        summary_row = QHBoxLayout()
        summary_row.addWidget(self.summary, 1)
        summary_row.addWidget(self.choose_all_button)

        self.character = QLabel()
        self.character.setTextFormat(Qt.TextFormat.PlainText)
        self.character.setAccessibleName("Character needing a voice choice")
        self.character.setWordWrap(True)
        character_font = self.character.font()
        character_font.setBold(True)
        self.character.setFont(character_font)
        self.portrait_image = QLabel()
        self.portrait_image.setAccessibleName("Exact game character portrait")
        self.portrait_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.portrait_image.setVisible(False)
        self.scope = QLabel()
        self.scope.setTextFormat(Qt.TextFormat.PlainText)
        self.scope.setAccessibleName("Voice choice scope")
        self.scope.setWordWrap(True)
        self.runtime = QLabel()
        self.runtime.setWordWrap(True)
        self.runtime.setTextFormat(Qt.TextFormat.PlainText)
        self.runtime.setAccessibleName("Character voice preview compute device")
        self._initialize_selectors()
        voice_reference_row = QHBoxLayout()
        voice_reference_row.addWidget(QLabel("Voice reference"))
        voice_reference_row.addWidget(self.voice_reference, 1)

        self.preview_box = self._build_preview()
        self.a_box = self._build_original_reference()
        self._initialize_advice(content_advisor, thread_pool)
        self.a_use = QPushButton("Use this voice")
        self.a_use.setAccessibleName("Use selected character voice")
        self.a_use.clicked.connect(self.use_a)
        self.auto_button = QPushButton("Choose for me")
        self.auto_button.setToolTip("Opt out of verifying this sample yourself")
        self.auto_button.clicked.connect(self.choose_for_me)
        self.retry_save_button = QPushButton("Retry saving choices")
        self.retry_save_button.clicked.connect(self._start_save)
        self.retry_save_button.setVisible(False)
        outcomes = QHBoxLayout()
        outcomes.addWidget(self.auto_button)
        outcomes.addStretch()
        outcomes.addWidget(self.retry_save_button)
        outcomes.addWidget(self.a_use)

        self.status = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.status.setAccessibleName("Voice preview status")
        self.status.setFrameShape(QFrame.Shape.NoFrame)
        self.status.viewport().setAutoFillBackground(False)
        self.status.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addLayout(summary_row)
        layout.addWidget(self.portrait_image)
        layout.addWidget(self.character)
        layout.addWidget(self.scope)
        layout.addLayout(voice_reference_row)
        layout.addWidget(self.a_box)
        layout.addWidget(self.preview_box)
        layout.addLayout(outcomes)
        layout.addWidget(self.status)
        layout.addWidget(self.reference_details_toggle)
        layout.addWidget(self.reference_details)
        layout.addWidget(self.advice_details)
        layout.addWidget(self.runtime)
        self.reference_details_toggle.toggled.connect(self._refresh_runtime)
        for widget in (self.status, self.recommendation, self.a_play, self.a_original):
            widget.installEventFilter(self)
        self._size_transient_controls()
        self.runtime_timer = QTimer(self)
        self.runtime_timer.setInterval(500)
        self.runtime_timer.timeout.connect(self._refresh_runtime)
        self.preview_runner.activeChanged.connect(self._refresh_runtime)
        self._refresh_runtime()
        self._connect_player()
        self.setVisible(False)

    def _build_original_reference(self) -> QGroupBox:
        box = QGroupBox()
        box.setAccessibleName("Voice sample")
        self.a_reason = QLabel()
        self.a_reason.setAccessibleName("Voice sample details")
        self.a_reason.setWordWrap(True)
        self.a_reason.setTextFormat(Qt.TextFormat.PlainText)
        self.a_reason.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.a_reason.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.a_original = QPushButton("Play original reference")
        self.a_original.setAccessibleName("Play original reference")
        self.a_original.clicked.connect(self._play_original)
        candidate_layout = QVBoxLayout(box)
        candidate_layout.addWidget(self.a_reason)
        candidate_layout.addWidget(
            self.a_original, alignment=Qt.AlignmentFlag.AlignRight
        )
        return box

    def _build_preview(self) -> QGroupBox:
        self.preview_phrase = QComboBox()
        self.preview_phrase.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.preview_phrase.setMinimumContentsLength(10)
        self.preview_text = QLabel()
        self.preview_text.setAccessibleName("Text for the generated voice preview")
        self.preview_text.setTextFormat(Qt.TextFormat.PlainText)
        self.preview_text.setWordWrap(True)
        self.preview_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.preview_text.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.preview_phrase.setAccessibleName("Generated preview phrase")
        self.preview_phrase.currentIndexChanged.connect(self._preview_phrase_selected)

        self.a_play = QPushButton("Play generated preview")
        self.a_play.setMinimumWidth(self.a_play.sizeHint().width())
        self.a_play.setText("Generate preview")
        self.a_play.setAccessibleName("Play generated voice preview")
        self.a_play.clicked.connect(self.play_a)
        preview_row = QHBoxLayout()
        preview_row.addWidget(QLabel("Phrase"))
        preview_row.addWidget(self.preview_phrase, 1)
        preview_row.addWidget(self.a_play)
        preview_box = QGroupBox("Generated preview")
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.addWidget(self.preview_text)
        preview_layout.addLayout(preview_row)

        return preview_box

    def _set_status(self, message: str) -> None:
        self.status.setPlainText(message)
        self.status.setToolTip(message)

    def _initialize_selectors(self) -> None:
        self.voice_reference = QComboBox(
            sizeAdjustPolicy=QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon,
            minimumContentsLength=18,
        )
        self.voice_reference.setItemDelegate(QStyledItemDelegate(self.voice_reference))
        self.voice_reference.view().setTextElideMode(Qt.TextElideMode.ElideRight)
        self.voice_reference.setAccessibleName("Voice reference")
        self.voice_reference.currentIndexChanged.connect(self._voice_reference_selected)
        self.voice_reference.highlighted.connect(self._voice_reference_highlighted)
        self._hover_index = 0
        self._focus_index = 0
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(180)
        self._hover_timer.timeout.connect(self._prepare_highlighted_reference)

    def _initialize_advice(
        self,
        content_advisor: PreviewContentAdvisor | None,
        thread_pool: QThreadPool | None,
    ) -> None:
        preview_root = getattr(self.preview_service, "root", None)
        if not isinstance(preview_root, (str, Path)):
            owner = getattr(self.preview_service, "owner", None)
            preview_root = getattr(getattr(owner, "service", None), "root", None)
        self.content_advisor = content_advisor or PreviewContentAdvisor(
            preview_root if isinstance(preview_root, (str, Path)) else None
        )
        self.advice_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.advice_runner.finished.connect(self._advice_finished)
        self._advice_cancel = Event()
        self._advice_attempted: set[tuple[str, str]] = set()
        self._advice_context: tuple[VoicePlan, str, str | None] | None = None
        self._advice_unavailable = False
        self._advice_error: str | None = None
        self._advice_sources: set[str] = set()
        self._content_checks: dict[str, PreviewContentCheck] = {}
        self.cache_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.cache_runner.finished.connect(self._cache_finished)
        self._cache_cancel = Event()
        self._create_advice_widgets()

    def _create_advice_widgets(self) -> None:
        self.reference_details_toggle = QCheckBox("Technical details")
        self.reference_details = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.reference_details.setAccessibleName("Technical voice reference details")
        self.reference_details.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.reference_details.hide()
        self.reference_details_toggle.toggled.connect(self.reference_details.setVisible)
        self.recommendation = QLabel()
        self.recommendation.setAccessibleName("Generated speech content check")
        self.recommendation.setWordWrap(True)
        self.recommendation.setTextFormat(Qt.TextFormat.PlainText)
        cast(QVBoxLayout, self.preview_box.layout()).addWidget(self.recommendation)
        self.advice_details = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.advice_details.setAccessibleName("Speech check details")
        self.advice_details.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.advice_details.hide()
        self.reference_details_toggle.toggled.connect(self.advice_details.setVisible)

    def _size_transient_controls(self) -> None:
        for button, label in (
            (self.a_play, "Play generated preview"),
            (self.a_original, "Play original reference"),
        ):
            current = button.text()
            button.setText(label)
            button.setMinimumWidth(button.sizeHint().width())
            button.setText(current)
        self.status.setFixedHeight(self.status.fontMetrics().lineSpacing() * 2 + 12)
        self.recommendation.setFixedHeight(
            self.recommendation.fontMetrics().lineSpacing() * 2
        )
        self.recommendation.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.recommendation.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.FontChange:
            self._size_transient_controls()
        return super().eventFilter(watched, event)

    def _initialize_preview_state(self) -> None:
        self._previews: dict[PreviewKey, _PreviewAudio] = {}
        self._preview_errors: dict[PreviewKey, str] = {}
        self._auditioned: dict[PreviewKey, str] = {}
        self._preview_request: _PreviewRequest | None = None
        self._queued_preview_request: _PreviewRequest | None = None

    def _refresh_runtime(self) -> None:
        background_preparing = getattr(self.preview_service, "preparing", False) is True
        if (
            self._plan is not None
            and self._candidate_entries
            and self._group_index < len(self._groups)
            and not self._shutdown_requested
            and not self._cancel_requested
            and not self.decision_runner.active
            and self._playing_source != "preview"
            and self._ready_preview(self._current_entry()[0]) is not None
        ):
            self._set_playing_source(self._playing_source)
            self.a_play.setEnabled(True)
            candidate = self._current_entry()[0]
            if self._preview_errors.pop(self._preview_key(candidate), None) is not None:
                self._show_reference_details(candidate)
        if self.preview_runner.active and self._shutdown_requested:
            message = "Stopping voice preview preparation..."
        elif self.preview_runner.active:
            message = "Preparing voice samples. " + _speech_runtime_label(
                self.preview_service.backend
            )
        elif background_preparing:
            message = "Preparing voice samples in the background. You can browse and play ready samples."
        else:
            message = "No preview generation. Saved previews play without TTS."
        self.runtime.setText(message)
        self.runtime.setVisible(
            self.reference_details_toggle.isChecked()
            and (self.preview_runner.active or self._shutdown_requested)
        )
        self.runtime.setToolTip(message)
        if (
            self._candidate_entries
            and self._group_index < len(self._groups)
            and not self._shutdown_requested
            and not self._terminal_emitted
        ):
            self._refresh_reference_advice()
            self._check_ready_previews()

    @property
    def active(self) -> bool:
        return bool(self.preview_runner.active or self.decision_runner.active)

    def start(self, plan: VoicePlan, *, group_id: str | None = None) -> None:
        if not isinstance(plan, VoicePlan):
            raise VoiceAuditionUIError("Voice audition plan is invalid")
        groups = tuple(
            group
            for group in plan.groups
            if (group_id is None and group.route == "needs-audition")
            or group.group_id == group_id
        )
        if not groups:
            raise VoiceAuditionUIError("Voice plan has no matching character")
        if any(
            not (
                group.candidate_inventory if group_id is not None else group.candidates
            )
            and group.narrator_candidate is None
            for group in groups
        ):
            raise VoiceAuditionUIError("This character has no voice to inspect")
        if self._shutdown_requested:
            if self._owns_preview_service:
                self.preview_service = _preview_service_factory()
            elif getattr(self.preview_service, "supports_prefetch", False) is True:
                self.preview_service = cast(
                    _PrefetchVoiceAuditionPreviewer, self.preview_service
                ).new_session()
        self._cancel_preview_request()
        self._cancel_advice()
        self.preview_runner.cancel()
        self._preview_request = None
        self._plan = plan
        self._inspection_mode = group_id is not None
        self._show_portraits = all(
            group.portrait_image and group.portrait_image_sha256
            for group in plan.groups
            if group.character != "Narrator"
        )
        self._groups = groups
        self._group_index = 0
        self._pending_decisions = []
        self._failed_candidate_source_ids.clear()
        self._preview_errors.clear()
        self._save_succeeded = False
        self._cancel_requested = False
        self._terminal_emitted = False
        self._ignore_preview_result = False
        self._shutdown_requested = False
        self.retry_save_button.setVisible(False)
        self.summary.setVisible(not self._inspection_mode)
        self.choose_all_button.setVisible(not self._inspection_mode)
        self.auto_button.setVisible(not self._inspection_mode)
        self.runtime_timer.start()
        self.setVisible(True)
        self._show_group()

    def current_group(self) -> VoiceGroup:
        return self._groups[self._group_index]

    def play_a(self) -> None:
        if self._playing_source == "preview":
            self._stop_playback()
            return
        if self.decision_runner.active or self._cancel_requested:
            return
        candidate, _choice, _narrator = self._current_entry()
        preview = self._ready_preview(candidate)
        if preview is not None:
            self._cancel_preview_request()
            self._set_decision_actions(True)
            self._play_preview(preview)
            return
        request = _PreviewRequest(
            self._require_plan(),
            self.current_group(),
            candidate,
            self._sample_text,
            Event(),
        )
        if self.preview_runner.active:
            self._cancel_preview_request()
            self._queued_preview_request = request
            self._set_status(
                "Stopping the previous preview; the selected sample will play next."
            )
            return
        self._launch_preview_request(request)

    def _launch_preview_request(self, request: _PreviewRequest) -> None:
        self._preview_errors.pop((request.candidate.source_id, request.text), None)
        self._show_reference_details(request.candidate)
        self._stop_player()
        self._accepted_choice = None
        self.a_use.setEnabled(False)
        self._set_status(
            "Preparing this generated preview... You can browse other voices."
        )
        self._preview_request = request
        arguments: dict[str, object] = {
            "text": request.text,
            "cancel_event": request.cancellation,
        }
        self.preview_runner.start(
            self.preview_service.generate,
            request.plan,
            request.group,
            request.candidate.source_id,
            **arguments,
        )

    def use_a(self) -> None:
        if self._accepted_choice is not None:
            self._record_choice(self._accepted_choice)

    def choose_for_me(self) -> None:
        if self.decision_runner.active:
            return
        if self._inspection_mode:
            self.cancel()
            return
        source_id = self._automatic_source_id(self.current_group())
        if source_id is None:
            self._set_status(
                "No working automatic voice remains. Retry this preview, or return "
                "to the voice plan and use Edit selected role in Voices."
            )
            return
        self._record_choice(source_id)

    def choose_all_automatically(self) -> None:
        if self.decision_runner.active or self._cancel_requested:
            return
        choices: list[PendingDecision] = []
        for group in self._groups[self._group_index :]:
            source_id = self._automatic_source_id(group)
            if source_id is None:
                self._set_status(
                    "No working automatic voice remains. Retry the failed preview, "
                    "or return to the voice plan and use Edit selected role in Voices."
                )
                return
            choices.append((group, source_id))
        self._pending_decisions.extend(choices)
        self._group_index = len(self._groups)
        self._ignore_preview_result = self.preview_runner.active
        if self.preview_runner.active:
            self._cancel_preview_request()
        self._stop_player()
        self._start_save(
            "VNTTS selected the recommended voice for every remaining character."
        )

    def _automatic_source_id(self, group: VoiceGroup) -> str | None:
        candidates = tuple(
            candidate
            for candidate in group.candidates
            if candidate.source_id not in self._failed_candidate_source_ids
        )
        if candidates:
            text = self._advice_text(group)
            checks = self._saved_checks(group, candidates, text)
            return recommended_reference(
                candidates,
                checks,
                saved_source_id=group.source_id
                if group.resolution.startswith("saved-")
                else None,
            )
        narrator = group.narrator_candidate
        if (
            narrator is not None
            and narrator.source_id not in self._failed_candidate_source_ids
        ):
            return default_voice_choice_id
        return None

    def cancel(self) -> None:
        if self._terminal_emitted:
            return
        if self.decision_runner.active:
            self._set_status("Finishing the voice save before returning...")
            return
        self._cancel_requested = True
        self._cache_cancel.set()
        self.cache_runner.cancel()
        self._cancel_advice()
        self._hover_timer.stop()
        self._stop_player()
        self._cancel_preview_request()
        if getattr(self.preview_service, "supports_prefetch", False) is True:
            self.preview_service.cancel()
        self._set_decision_actions(False)
        self.choose_all_button.setEnabled(False)
        self._set_status("Cancelling voice selection...")
        if not self.active:
            self._emit_cancelled()

    def shutdown(self) -> None:
        self._stop_player()
        self._shutdown_requested = True
        self._cache_cancel.set()
        self.cache_runner.cancel()
        self._hover_timer.stop()
        self._cancel_advice()
        if getattr(self.preview_service, "supports_prefetch", False) is True:
            self.preview_service.cancel()
        self.runtime_timer.stop()
        self._refresh_runtime()
        if self.preview_runner.active:
            self._cancel_preview_request()
            return
        if not self.active:
            self.preview_service.close()

    def _show_group(self) -> None:
        self._hover_timer.stop()
        self._cancel_advice()
        self._candidate_offset = 0
        self._cancel_preview_request()
        self._previews = {}
        self._auditioned.clear()
        self._accepted_choice = None
        self.reference_details_toggle.setChecked(False)
        group = self.current_group()
        candidates = group.candidate_inventory or group.candidates
        entries: list[CandidateEntry] = [
            (candidate, candidate.source_id, False) for candidate in candidates
        ]
        if group.narrator_candidate is not None:
            entries.append((group.narrator_candidate, default_voice_choice_id, True))
        entries.sort(
            key=lambda entry: (
                entry[0].source_id != group.source_id,
                self._inspection_mode
                and not is_playable_main_voice_source(
                    entry[0].source_bank, entry[0].source_line_ids
                ),
            )
        )
        self._candidate_entries = tuple(entries)
        self._advice_attempted.clear()
        self._content_checks.clear()
        self._advice_unavailable = False
        self._advice_error = None
        self._focus_index = 0
        with QSignalBlocker(self.voice_reference):
            self.voice_reference.clear()
            for index, (candidate, _choice, narrator) in enumerate(
                self._candidate_entries, 1
            ):
                duration = (
                    f"{candidate.reference_duration_seconds:.1f} s"
                    if candidate.reference_duration_seconds is not None
                    else "duration unavailable"
                )
                label = (
                    f"Narrator fallback - {duration}"
                    if narrator
                    else f"Reference {index} - {duration}"
                )
                self.voice_reference.addItem(label)
                origin = (
                    UNLINKED_BANK_LABEL
                    if candidate.candidate_origin == UNLINKED_BANK_MEDIA
                    else "Playable line"
                    if is_playable_main_voice_source(
                        candidate.source_bank, candidate.source_line_ids
                    )
                    else "Story reference"
                )
                self.voice_reference.setItemData(
                    self.voice_reference.count() - 1,
                    "\n".join(
                        (origin, *candidate.source_titles, *candidate.source_excerpts)
                    )
                    + (
                        ""
                        if candidate.source_excerpts
                        else "\nOriginal text unavailable."
                    ),
                    Qt.ItemDataRole.ToolTipRole,
                )
            self.voice_reference.setCurrentIndex(0)
        self.summary.setText(
            "Review the suggested voices, or keep every automatic choice."
            if self._inspection_mode
            else f"Voice sample {self._group_index + 1} of {len(self._groups)}."
        )
        self.character.setText(group.character)
        self._show_portrait(group)
        identity = normalize_character_name(group.character)
        count = len(
            {
                line_id
                for item in self._require_plan().groups
                if normalize_character_name(item.character) == identity
                for line_id in item.line_ids
            }
        )
        self.scope.setText(
            f"Used for {count} selected line{'s' if count != 1 else ''}. Saving changes "
            "this character's voice for future speech and preparation; "
            "existing prepared audio is unchanged."
        )
        self._show_preview_phrases(group)
        self._restore_cached_previews()
        self.voice_reference.setToolTip(
            self.voice_reference.currentData(Qt.ItemDataRole.ToolTipRole)
            or self.voice_reference.currentText()
        )
        self.preview_phrase.setToolTip(self.preview_phrase.currentData())
        self.voice_reference.setEnabled(len(self._candidate_entries) > 1)
        self.preview_phrase.setEnabled(self.preview_phrase.count() > 1)
        self.choose_all_button.setEnabled(True)
        self._show_current_candidate()

    def _show_preview_phrases(self, group: VoiceGroup) -> None:
        with QSignalBlocker(self.preview_phrase):
            self.preview_phrase.clear()
            if self._require_plan().synthesis_language in {None, "en", "English"}:
                self.preview_phrase.addItem(
                    "Comparison sample", COMPARISON_PREVIEW_TEXT
                )
            for label, text in (
                ("Story sample", group.sample_text),
                ("Alternate story sample", group.alternate_sample_text),
            ):
                if text is not None:
                    self.preview_phrase.addItem(label, text)
            for index in range(self.preview_phrase.count()):
                self.preview_phrase.setItemData(
                    index,
                    self.preview_phrase.itemData(index),
                    Qt.ItemDataRole.ToolTipRole,
                )
            self.preview_phrase.setCurrentIndex(0)
        self._sample_text = self.preview_phrase.currentData()

    def _voice_reference_selected(self, index: int) -> None:
        if self.decision_runner.active or not 0 <= index < len(self._candidate_entries):
            return
        self._cancel_preview_request()
        self._candidate_offset = index
        self._focus_index = index
        self._hover_timer.stop()
        self.voice_reference.setToolTip(
            self.voice_reference.currentData(Qt.ItemDataRole.ToolTipRole)
            or self.voice_reference.currentText()
        )
        self._show_current_candidate()

    def _preview_phrase_selected(self, index: int) -> None:
        if self.decision_runner.active:
            return
        text = self.preview_phrase.itemData(index)
        if not isinstance(text, str) or not text:
            return
        self._cancel_preview_request()
        self._cancel_advice()
        self._sample_text = text
        self._advice_attempted.clear()
        self._advice_unavailable = False
        self._advice_error = None
        self._content_checks.clear()
        self._restore_cached_previews()
        self.preview_phrase.setToolTip(text)
        self._show_current_candidate()

    def _current_entry(self) -> CandidateEntry:
        return self._candidate_entries[self._candidate_offset]

    def _show_current_candidate(self) -> None:
        self._stop_player()
        self._accepted_choice = None
        self.preview_text.setText(self._sample_text or "")
        candidate, _choice, narrator = self._current_entry()
        self.a_box.setTitle(
            "Original reference for narrator" if narrator else "Original game reference"
        )
        reference_count = len(candidate.reference_sha256s)
        reference_summary = (
            f"{reference_count} original reference"
            + ("s" if reference_count != 1 else "")
            + (
                f" · {candidate.reference_duration_seconds:.1f} s total"
                if candidate.reference_duration_seconds is not None
                else ""
            )
            if reference_count
            else "No original reference"
        )
        transcript = "\n".join(candidate.source_excerpts) or (
            UNLINKED_BANK_LABEL
            if candidate.candidate_origin == UNLINKED_BANK_MEDIA
            else "Text unavailable."
        )
        self.a_reason.setText(
            f"Voice: {_candidate_name(candidate)}\n{reference_summary}\n"
            f"Original spoken text: {transcript}"
        )
        self._show_reference_details(candidate)
        preview = self._ready_preview(candidate)
        self.a_play.setText(
            "Play generated preview" if preview is not None else "Generate preview"
        )
        self.a_play.setEnabled(True)
        self.a_original.setEnabled(bool(candidate.reference_sha256s))
        self._set_playing_source(None)
        self.a_original.setToolTip(
            "Listen to the first original reference; generation uses the full reference set."
            if reference_count > 1
            else "Listen to the original reference used for this voice."
            if candidate.reference_sha256s
            else "This voice has no recorded reference."
        )
        self.a_use.setText("Use narrator voice" if narrator else "Use this voice")
        self.a_use.setAccessibleDescription(
            "Save this voice for future speech and story preparation"
        )
        heard = (
            preview is not None
            and self._auditioned.get(self._preview_key(candidate))
            == preview.audio_sha256
        )
        self.a_use.setEnabled(preview is not None and heard)
        self.a_box.setVisible(True)
        self.voice_reference.setEnabled(len(self._candidate_entries) > 1)
        self.preview_phrase.setEnabled(self.preview_phrase.count() > 1)
        self.auto_button.setText("Choose for me")
        self.auto_button.setEnabled(True)
        self._prefetch_window()
        self._refresh_reference_advice()
        if preview is not None and heard:
            self._accepted_choice = self._current_entry()[1]
            self._set_status("Replay the preview or use this verified voice.")
        else:
            self._set_status(
                "Listen to the preview before choosing."
                if preview is not None
                else "Play the original or generate a preview."
                if candidate.reference_sha256s
                else "Generate a preview before using this voice."
            )

    def _show_reference_details(self, candidate: VoiceCandidate) -> None:
        details = [
            f"Voice source: {candidate.source_speaker}",
            candidate.recommendation,
        ]
        if candidate.candidate_origin == UNLINKED_BANK_MEDIA:
            details.append(UNLINKED_BANK_LABEL)
        error = self._preview_errors.get(self._preview_key(candidate))
        if error is not None:
            details.append(f"Preview error: {error}")
        details.extend(
            f"{index}. SHA-256 {checksum}"
            for index, checksum in enumerate(candidate.reference_sha256s, 1)
        )
        if candidate.source_line_ids:
            details.append("Source lines: " + ", ".join(candidate.source_line_ids))
        self.reference_details.setPlainText(
            "\n".join(details) if details else "No recorded reference files."
        )

    def _preview_finished(
        self, preview: _PreviewAudio | None, error: Exception | None
    ) -> None:
        if self._shutdown_requested:
            if not self.active:
                self.preview_service.close()
            return
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if self._ignore_preview_result:
            self._ignore_preview_result = False
            self._maybe_complete()
            return
        request = self._completed_preview_request(error)
        if request is None:
            return
        candidate = request.candidate
        if error is not None:
            self._preview_failed(request, error)
            return
        assert preview is not None
        self._failed_candidate_source_ids.discard(candidate.source_id)
        self._previews[self._preview_key(candidate)] = preview
        self._set_decision_actions(True)
        self._play_preview(preview)
        self._prefetch_window()
        self._refresh_reference_advice()
        self._check_ready_previews()

    def _preview_failed(self, request: _PreviewRequest, error: Exception) -> None:
        candidate = request.candidate
        from vntts.support import record_game_import

        record_game_import(
            "voice-preview",
            outcome="failed",
            command_kind="preview",
            exception_type=type(error).__name__,
            reason=str(error),
            traceback_tail="".join(format_exception(error))[-12000:],
        )
        if isinstance(error, (VoiceAuditionIncomplete, VoiceAuditionRejected)):
            self._failed_candidate_source_ids.add(candidate.source_id)
        self.a_play.setEnabled(True)
        self.a_original.setEnabled(bool(candidate.reference_sha256s))
        self.a_use.setEnabled(False)
        self.voice_reference.setEnabled(len(self._candidate_entries) > 1)
        self.preview_phrase.setEnabled(self.preview_phrase.count() > 1)
        self.auto_button.setEnabled(True)
        self._preview_errors[self._preview_key(candidate)] = str(error)
        self._set_status(
            "Preview failed. Listen to the original reference or retry."
            if candidate.reference_sha256s
            else "Preview failed. Retry or choose another voice."
        )
        self._refresh_reference_advice()
        self._show_reference_details(candidate)

    def _completed_preview_request(
        self, error: Exception | None
    ) -> _PreviewRequest | None:
        request = self._preview_request
        self._preview_request = None
        if request is None or request.plan is not self._plan:
            return None
        if request.cancellation.is_set() or isinstance(error, VoiceAuditionCancelled):
            self._finish_cancelled_preview()
            return None
        if (
            request.group != self.current_group()
            or request.text != self._sample_text
            or request.candidate.source_id != self._current_entry()[0].source_id
        ):
            return None
        return request

    def _finish_cancelled_preview(self) -> None:
        queued = self._queued_preview_request
        self._queued_preview_request = None
        if queued is not None and not queued.cancellation.is_set():
            self._launch_preview_request(queued)
            return
        if not self.decision_runner.active:
            self._set_decision_actions(True)
            if self._playing_source is None:
                self._set_status(
                    "Preview preparation stopped. Browse voices or retry this preview."
                )
        self._maybe_complete()

    def _play_preview(self, preview: _PreviewAudio) -> None:
        self._accepted_choice = None
        self.a_use.setEnabled(False)
        try:
            payload = _read_verified_audio(preview.path, preview.audio_sha256)
        except (OSError, VoiceAuditionUIError) as error:
            key = self._preview_key(self._current_entry()[0])
            self._previews.pop(key, None)
            self._auditioned.pop(key, None)
            self._set_playing_source(None)
            self._set_status(f"Unable to play generated preview: {error}")
            return
        player = self._ensure_player()
        player.stop()
        if player.play_bytes(payload, source=str(preview.path)) is None:
            key = self._preview_key(self._current_entry()[0])
            self._previews.pop(key, None)
            self._auditioned.pop(key, None)
            self._set_playing_source(None)
            return
        self._accepted_choice = self._current_entry()[1]
        self._auditioned[self._preview_key(self._current_entry()[0])] = (
            preview.audio_sha256
        )
        self.a_use.setEnabled(True)
        self._set_playing_source("preview")
        self._set_status("Playing the generated preview.")

    def _cancel_preview_request(self) -> None:
        if self._preview_request is not None:
            self._preview_request.cancellation.set()
        if self._queued_preview_request is not None:
            self._queued_preview_request.cancellation.set()
            self._queued_preview_request = None

    def _ready_preview(self, candidate: VoiceCandidate) -> _PreviewAudio | None:
        if getattr(self.preview_service, "supports_prefetch", False) is True:
            return cast(_PrefetchVoiceAuditionPreviewer, self.preview_service).peek(
                self._require_plan(),
                self.current_group(),
                candidate.source_id,
                text=self._sample_text,
            )
        return self._previews.get(self._preview_key(candidate))

    def _voice_reference_highlighted(self, index: int) -> None:
        if 0 <= index < len(self._candidate_entries):
            self._hover_index = index
            self._hover_timer.start()

    def _prepare_highlighted_reference(self) -> None:
        if (
            self._shutdown_requested
            or self._cancel_requested
            or self.decision_runner.active
        ):
            return
        self._focus_index = self._hover_index
        self._prefetch_window()
        self._refresh_reference_advice()

    def _focus_candidates(self) -> tuple[VoiceCandidate, ...]:
        count = len(self._candidate_entries)
        indices = (self._focus_index, self._focus_index + 1, self._focus_index - 1)
        if self._focus_index == 0:
            indices = (0, 1, 2)
        elif self._focus_index == count - 1:
            indices = (count - 1, count - 2, count - 3)
        return tuple(
            self._candidate_entries[index][0] for index in indices if 0 <= index < count
        )

    def _prefetch_window(self) -> None:
        if getattr(self.preview_service, "supports_prefetch", False) is not True:
            return
        group = self.current_group()
        requests = tuple(
            (group, candidate.source_id, self._sample_text)
            for candidate in self._focus_candidates()
        )
        cast(_PrefetchVoiceAuditionPreviewer, self.preview_service).prefetch(
            self._require_plan(), requests, automatic=True
        )

    def _preview_key(self, candidate: VoiceCandidate) -> PreviewKey:
        return candidate.source_id, self._sample_text

    def _advice_text(self, group: VoiceGroup) -> str:
        if group == self.current_group() and self._sample_text is not None:
            return self._sample_text
        if self._require_plan().synthesis_language in {None, "en", "English"}:
            return COMPARISON_PREVIEW_TEXT
        return group.sample_text

    def _saved_checks(
        self, group: VoiceGroup, candidates: tuple[VoiceCandidate, ...], text: str
    ) -> dict[str, PreviewContentCheck]:
        plan = self._require_plan()
        return cached_reference_checks(
            candidates,
            text,
            backend=plan.synthesis_backend,
            model=plan.synthesis_model,
            language=plan.synthesis_language,
            profile=plan.synthesis_profile,
            advisor=self.content_advisor,
        )

    def _availability(self, candidate: VoiceCandidate) -> str:
        if self._ready_preview(candidate) is not None:
            return "Ready"
        if (
            self._preview_request is not None
            and not self._preview_request.cancellation.is_set()
            and self._preview_request.candidate.source_id == candidate.source_id
        ):
            return "Preparing"
        if self._preview_key(candidate) in self._preview_errors:
            return "Preparation failed"
        state = getattr(self.preview_service, "preparation_state", None)
        if callable(state):
            value = state(
                self._require_plan(),
                self.current_group(),
                candidate.source_id,
                text=self._sample_text,
            )
            labels = {
                "preparing": "Preparing",
                "queued": "Queued",
                "failed": "Preparation failed",
            }
            if isinstance(value, str) and value in labels:
                return labels[value]
        return "Not prepared"

    def _unchecked_content_state(self, candidate: VoiceCandidate) -> str:
        if self._advice_unavailable:
            return "Check unavailable"
        if self.advice_runner.active and candidate.source_id in self._advice_sources:
            return "Checking content"
        return "Content not checked"

    def _refresh_reference_advice(self) -> None:
        candidates = tuple(entry[0] for entry in self._candidate_entries)
        checks = {}
        for candidate in candidates:
            check = self._content_checks.get(candidate.source_id)
            preview = self._ready_preview(candidate)
            if (
                check is not None
                and isinstance(preview, VoiceAuditionPreview)
                and check.preview_identity == preview.identity
                and check.audio_sha256 == preview.audio_sha256
                and check.text == preview.text
                and check.seed == preview.seed
            ):
                checks[candidate.source_id] = check
        messages = {
            "no_asr_mismatch": "No text mismatch detected; listen for audio quality.",
            "boundary_variation": "Word boundaries differ; check by ear.",
            "uncertain_recognition": "Recognition uncertain; check by ear.",
            "review_possible_content_mismatch": "Possible extra or missing words; check by ear.",
        }
        short_messages = {
            "no_asr_mismatch": "No text mismatch",
            "boundary_variation": "Boundary variation",
            "uncertain_recognition": "Recognition uncertain",
            "review_possible_content_mismatch": "Check words by ear",
        }
        details = []
        with QSignalBlocker(self.voice_reference):
            for index, (candidate, _choice, narrator) in enumerate(
                self._candidate_entries
            ):
                check = checks.get(candidate.source_id)
                duration = (
                    f"{candidate.reference_duration_seconds:.1f} s"
                    if candidate.reference_duration_seconds is not None
                    else "duration unavailable"
                )
                content = (
                    short_messages[check.advice.status]
                    if check
                    else self._unchecked_content_state(candidate)
                )
                label = "Narrator fallback" if narrator else f"Reference {index + 1}"
                self.voice_reference.setItemText(
                    index,
                    f"{label} - {duration} - {self._availability(candidate)} - {content}",
                )
                if (
                    check is not None
                    and candidate.source_id == self._current_entry()[0].source_id
                ):
                    details.append(f"Recognized speech: {check.advice.observed_text}")
        current = checks.get(self._current_entry()[0].source_id)
        message = (
            messages[current.advice.status]
            if current
            else "Speech content has not been checked."
        )
        if self._require_plan().synthesis_language not in {None, "en", "English"}:
            message = "Speech checks currently support English previews."
        elif (
            current is None
            and self.advice_runner.active
            and self._current_entry()[0].source_id in self._advice_sources
        ):
            message = "Checking prepared speech content..."
        elif current is None and self._advice_unavailable:
            message = "Speech check unavailable; listen before choosing."
        self.recommendation.setText("Content check: " + message)
        self.recommendation.setToolTip(self.recommendation.text())
        if self._advice_error is not None:
            details.append(f"Speech check error: {self._advice_error}")
        self.advice_details.setPlainText(
            "\n\n".join(details)
            + ("\n\n" if details else "")
            + "Checks describe generated words, not voice quality. Recognition can mishear names. "
            "Only prepared samples near the browsing focus are checked."
        )

    def _check_ready_previews(self) -> None:
        if (
            self.advice_runner.active
            or self.cache_runner.active
            or self._advice_unavailable
            or self._shutdown_requested
            or self._cancel_requested
            or self.decision_runner.active
            or self._require_plan().synthesis_language not in {None, "en", "English"}
        ):
            return
        previews = []
        text = self._advice_text(self.current_group())
        for candidate in self._focus_candidates():
            preview = self._ready_preview(candidate)
            if not isinstance(preview, VoiceAuditionPreview):
                continue
            expected = preview_identity(
                candidate,
                text,
                backend=self._require_plan().synthesis_backend,
                model=self._require_plan().synthesis_model,
                language=self._require_plan().synthesis_language,
                profile=self._require_plan().synthesis_profile,
            )
            key = (preview.identity, preview.audio_sha256)
            if (
                key not in self._advice_attempted
                and preview.identity == expected
                and preview.text == text
                and preview.candidate_source_id == candidate.source_id
            ):
                previews.append(preview)
                self._advice_attempted.add(key)
            if len(previews) == 3:
                break
        if not previews:
            return
        self._advice_cancel = Event()
        self._advice_sources = {preview.candidate_source_id for preview in previews}
        self._advice_context = (
            self._require_plan(),
            self.current_group().group_id,
            self._sample_text,
        )
        self.advice_runner.start(
            self.content_advisor.check,
            tuple(previews),
            install_model=True,
            cancel_event=self._advice_cancel,
        )
        self._refresh_reference_advice()

    def _advice_finished(
        self, checks: tuple[PreviewContentCheck, ...] | None, error: Exception | None
    ) -> None:
        if self._shutdown_requested or self._cancel_requested:
            return
        if self._group_index >= len(self._groups) or self._terminal_emitted:
            return
        context = self._advice_context
        if context != (
            self._require_plan(),
            self.current_group().group_id,
            self._sample_text,
        ):
            return
        if error is not None and not isinstance(error, PreviewContentCheckCancelled):
            self._advice_unavailable = True
            self._advice_error = str(error)
        else:
            for check in checks or ():
                self._content_checks[check.source_id] = check
        self._refresh_reference_advice()

    def _restore_cached_previews(self) -> None:
        self._cache_cancel.set()
        self.cache_runner.cancel()
        if getattr(self.preview_service, "supports_prefetch", False) is not True:
            return
        self._cache_cancel = Event()
        plan, group, text = (
            self._require_plan(),
            self.current_group(),
            self._sample_text,
        )
        candidates = tuple(entry[0] for entry in self._candidate_entries)
        service, advisor, cancellation = (
            self.preview_service,
            self.content_advisor,
            self._cache_cancel,
        )

        def read_cache() -> dict[str, PreviewContentCheck]:
            restore = getattr(service, "restore_cached", None)
            if getattr(service, "supports_prefetch", False) is True and callable(
                restore
            ):
                restore(
                    plan,
                    tuple(
                        (group, candidate.source_id, text) for candidate in candidates
                    ),
                    cancel_event=cancellation,
                )
            checks = {}
            for candidate in candidates:
                if cancellation.is_set():
                    return {}
                checks.update(
                    cached_reference_checks(
                        (candidate,),
                        text or group.sample_text,
                        backend=plan.synthesis_backend,
                        model=plan.synthesis_model,
                        language=plan.synthesis_language,
                        profile=plan.synthesis_profile,
                        advisor=advisor,
                    )
                )
            return checks

        self.cache_runner.start(read_cache)

    def _cache_finished(
        self, checks: dict[str, PreviewContentCheck] | None, error: Exception | None
    ) -> None:
        if self._shutdown_requested or self._cancel_requested or self._terminal_emitted:
            return
        if self._group_index >= len(self._groups):
            return
        if error is None:
            self._content_checks.update(checks or {})
            self._advice_attempted.update(
                (check.preview_identity, check.audio_sha256)
                for check in (checks or {}).values()
            )
        self._refresh_reference_advice()
        self._check_ready_previews()

    def _cancel_advice(self) -> None:
        self._advice_cancel.set()
        self.advice_runner.cancel()

    def _play_original(self) -> None:
        if self._playing_source == "original":
            self._stop_playback()
            return
        if (
            self._cancel_requested
            or self._shutdown_requested
            or self.decision_runner.active
        ):
            return
        candidate, _choice, _narrator = self._current_entry()
        from vntts.support import record_game_import

        reference_sha256 = (
            candidate.reference_sha256s[0] if candidate.reference_sha256s else None
        )
        record_game_import(
            "voice-original-playback",
            outcome="requested",
            reference_sha256=reference_sha256,
        )
        self._cancel_preview_request()
        self._stop_player()
        self._accepted_choice = None
        self.a_use.setEnabled(False)
        try:
            reference = self.preview_service.reference_audio(
                self._require_plan(), self.current_group(), candidate.source_id
            )
        except (OSError, ValueError, VoiceAuditionError) as error:
            self._set_status(f"Unable to play original reference: {error}")
            record_game_import(
                "voice-original-playback", outcome="failed", reason=str(error)
            )
            return
        if reference is None:
            self._set_status("This voice has no recorded reference.")
            record_game_import("voice-original-playback", outcome="unavailable")
            return
        try:
            payload = _read_verified_audio(reference, reference_sha256)
        except (OSError, VoiceAuditionUIError) as error:
            self._set_status(f"Unable to play original reference: {error}")
            record_game_import(
                "voice-original-playback", outcome="failed", reason=str(error)
            )
            return
        player = self._ensure_player()
        self._set_status(
            f"Starting the original reference for {_candidate_name(candidate)}..."
        )
        if player.play_bytes(payload, source=str(reference)) is None:
            return
        self._set_playing_source("original")
        self._accepted_choice = self._current_entry()[1]
        self.a_use.setEnabled(True)

    def _record_choice(self, source_id: str, status_message: str | None = None) -> None:
        if self._group_index >= len(self._groups) or self.decision_runner.active:
            return
        self._cancel_preview_request()
        self._stop_player()
        self._pending_decisions.append((self.current_group(), source_id))
        self._group_index += 1
        if self._group_index < len(self._groups):
            self._show_group()
            return
        self._start_save(status_message)

    def _start_save(self, status_message: str | None = None) -> None:
        self._cache_cancel.set()
        self.cache_runner.cancel()
        self._cancel_advice()
        if self.decision_runner.active or not self._pending_decisions:
            return
        if getattr(self.preview_service, "supports_prefetch", False) is True:
            self.preview_service.cancel()
        self.retry_save_button.setVisible(False)
        self._set_decision_actions(False)
        self.choose_all_button.setEnabled(False)
        self._set_status(
            f"{status_message} Saving in the background..."
            if status_message
            else "Saving voice choices in the background..."
        )
        self.decision_runner.start(
            self.decisions.remember_many,
            tuple(self._pending_decisions),
        )

    def _decision_finished(self, _result: object, error: Exception | None) -> None:
        if self._shutdown_requested:
            if not self.active:
                self.preview_service.close()
            return
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if error is not None:
            self.retry_save_button.setVisible(True)
            self.retry_save_button.setEnabled(True)
            self._set_status(f"Unable to save the voice choices: {error}")
            self.saveFailed.emit()
            return
        self._save_succeeded = True
        self._maybe_complete()

    def _maybe_complete(self) -> None:
        if self._cancel_requested:
            if not self.active:
                self._emit_cancelled()
            return
        if not self._save_succeeded or self.active:
            return
        self._terminal_emitted = True
        self._cancel_advice()
        self.runtime_timer.stop()
        self.setVisible(False)
        self.completed.emit()

    def _set_decision_actions(self, enabled: bool) -> None:
        enabled = bool(enabled) and not self._cancel_requested
        candidate = self._current_entry()[0] if self._candidate_entries else None
        self.a_play.setEnabled(enabled and self.a_box.isVisible())
        self.a_use.setEnabled(enabled and self._accepted_choice is not None)
        self.a_original.setEnabled(
            enabled
            and self.a_box.isVisible()
            and candidate is not None
            and bool(candidate.reference_sha256s)
        )
        self.voice_reference.setEnabled(enabled and len(self._candidate_entries) > 1)
        self.preview_phrase.setEnabled(enabled and self.preview_phrase.count() > 1)
        self.auto_button.setEnabled(enabled)

    def _show_portrait(self, group: VoiceGroup) -> None:
        self.portrait_image.clear()
        self.portrait_image.setVisible(False)
        if (
            not self._show_portraits
            or not group.portrait_image
            or not group.portrait_image_sha256
        ):
            return
        try:
            if sha256_file(group.portrait_image) != group.portrait_image_sha256:
                return
        except OSError:
            return
        pixmap = QPixmap(group.portrait_image)
        if pixmap.isNull():
            return
        self.portrait_image.setPixmap(
            pixmap.scaled(
                180,
                180,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.portrait_image.setVisible(True)

    def _emit_cancelled(self) -> None:
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        self._shutdown_requested = True
        self.runtime_timer.stop()
        self._refresh_runtime()
        self.setVisible(False)
        if (
            self._owns_preview_service
            or getattr(self.preview_service, "supports_prefetch", False) is True
        ):
            self.preview_service.close()
        self.cancelled.emit()

    def _ensure_player(self) -> _PreviewPlayer:
        if self.player is None:
            player = QMediaPlayer(self)
            player.errorOccurred.connect(
                lambda _code, message: self._set_status(
                    f"Unable to play voice sample: {message}"
                )
            )
            self.player = player
            self._connect_player()
        return self.player

    def _connect_player(self) -> None:
        signal = getattr(self.player, "playbackStateChanged", None)
        connect = getattr(signal, "connect", None)
        if callable(connect):
            connect(self._playback_state_changed)

    def _playback_state_changed(self, state: object) -> None:
        if getattr(state, "name", "") == "StoppedState":
            if self._playing_source is not None:
                self._set_status("Playback finished. Use this voice if suitable.")
            self._set_playing_source(None)

    def _stop_playback(self) -> None:
        self._cancel_preview_request()
        self._stop_player()
        self._set_status("Playback stopped.")

    def _set_playing_source(self, source: str | None) -> None:
        self._playing_source = source
        multiple = bool(
            self._candidate_entries
            and len(self._current_entry()[0].reference_sha256s) > 1
        )
        self.a_original.setText(
            "Stop original"
            if source == "original"
            else "Play first reference"
            if multiple
            else "Play original reference"
        )
        self.a_original.setAccessibleName(
            "Stop original reference"
            if source == "original"
            else "Play first original reference"
            if multiple
            else "Play original reference"
        )
        current = (
            self._current_entry()[0]
            if self._candidate_entries and self._group_index < len(self._groups)
            else None
        )
        preview_ready = bool(
            current is not None and self._ready_preview(current) is not None
        )
        self.a_play.setText(
            "Stop preview"
            if source == "preview"
            else "Play generated preview"
            if preview_ready
            else "Generate preview"
        )
        self.a_play.setAccessibleName(
            "Stop generated voice preview"
            if source == "preview"
            else "Play generated voice preview"
            if preview_ready
            else "Generate voice preview"
        )

    def _stop_player(self) -> None:
        if self.player is not None:
            self.player.stop()
        self._set_playing_source(None)

    def _require_plan(self) -> VoicePlan:
        assert self._plan is not None
        return self._plan


__all__ = ["VoiceAuditionPanel", "VoiceAuditionUIError"]

"""One-at-a-time voice verification for self-service pregeneration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from threading import Event
from traceback import format_exception
from typing import Protocol, TypeAlias, cast

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QSignalBlocker,
    QSortFilterProxyModel,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QFont,
    QIcon,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.voice_manifest import normalize_character_name

from vntts.action_icons import action_icon, set_action_icon
from vntts.async_ui import LatestTaskRunner
from vntts.authoring.pcm_playback import PcmPlaybackError
from vntts.document_identity import sha256_file
from vntts.path_safety import read_regular_bytes
from vntts.pregeneration_audition import (
    COMPARISON_PREVIEW_TEXT,
    VoiceAuditionCancelled,
    VoiceAuditionPreview,
    VoiceAuditionPreviewService,
    VoiceAuditionReference,
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
from vntts.reference_recommendation import cached_reference_checks
from vntts.voices import default_voice_choice_id, voice_source_label


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
    current_voice: bool = False


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
    ) -> VoiceAuditionReference | None: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


class _PrefetchVoiceAuditionPreviewer(_VoiceAuditionPreviewer, Protocol):
    def generate(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
        cancel_event: Event | None = None,
        progress: Callable[[str], object] | None = None,
        automatic: bool = False,
    ) -> _PreviewAudio: ...

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

    def preparation_error(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
    ) -> str | None: ...

    def new_session(self) -> _VoiceAuditionPreviewer: ...


class _PreviewPlayer(Protocol):
    def stop(self) -> None: ...

    def play_bytes(self, payload: bytes, source: str) -> object | None: ...


_preview_service_factory: Callable[[], _VoiceAuditionPreviewer] = (
    VoiceAuditionPreviewService
)


class VoiceAuditionUIError(RuntimeError):
    """The player audition card received an invalid unresolved plan."""


def _read_verified_audio(path: Path, expected_sha256: str | None) -> bytes:
    payload = read_regular_bytes(path)
    if sha256(payload).hexdigest() != expected_sha256:
        raise VoiceAuditionUIError("Voice sample changed after validation")
    return payload


def _candidate_name(candidate: VoiceCandidate) -> str:
    return (
        voice_source_label(candidate.source_character)
        or candidate.source_speaker.strip()
        or "Unknown source"
    )


class VoiceAuditionPanel(QGroupBox):
    _queued_preview_request: _PreviewRequest | None

    completed = Signal()
    cancelled = Signal()
    saveFailed = Signal()
    activeChanged = Signal(bool)
    selectionChanged = Signal()
    beforeSave = Signal(object)

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
        self.player: _PreviewPlayer | None = player
        self.playback_guard: Callable[[], None] | None = None
        self._plan: VoicePlan | None = None
        self._group: VoiceGroup | None = None
        self._candidate_offset = 0
        self._candidate_entries: tuple[CandidateEntry, ...] = ()
        self._initialize_preview_state(thread_pool)
        self._accepted_choice: str | None = None
        self._sample_text: str | None = None
        self._pending_decisions: list[PendingDecision] = []
        self._save_succeeded = False
        self._cancel_requested = False
        self._terminal_emitted = False
        self._shutdown_requested = False
        self._policy_choice: tuple[str, str] | None = None
        self._show_portraits = False
        self._playing_source: str | None = None
        self._current_voice: tuple[VoicePlan, VoiceGroup, VoiceCandidate] | None = None
        self._current_playback_guard: Callable[[], None] | None = None
        self._current_previews: dict[str | None, _PreviewAudio] = {}
        self._original_request: (
            tuple[VoicePlan, VoiceGroup, VoiceCandidate, bool] | None
        ) = None

        self.portrait_image = QLabel()
        self.portrait_image.setAccessibleName("Exact game character portrait")
        self.portrait_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.portrait_image.setVisible(False)
        self.candidate_voice_label = QLabel()
        self.candidate_voice_label.setTextFormat(Qt.TextFormat.PlainText)
        self.candidate_voice_label.setWordWrap(True)
        self.candidate_voice_label.setAccessibleName("New voice for comparison")
        self.candidate_voice_label.hide()
        self._initialize_selectors()

        self._build_preview()
        self.a_box = self._build_original_reference()
        self.current_voice_box = self._build_current_voice()
        self._initialize_advice(content_advisor, thread_pool)
        self.a_use = QPushButton("Save")
        set_action_icon(self.a_use, "check")
        self.a_use.setAccessibleName("Use selected character voice")
        self.a_use.setAutoDefault(False)
        self.a_use.clicked.connect(self.use_a)
        self.retry_save_button = QPushButton("Retry saving choices")
        set_action_icon(self.retry_save_button, "refresh-cw")
        self.retry_save_button.clicked.connect(self._start_save)
        self.retry_save_button.setVisible(False)
        self.outcome_actions = QHBoxLayout()
        self.outcome_actions.addStretch()
        self.outcome_actions.addWidget(self.retry_save_button)
        self.outcome_actions.addWidget(self.a_use)

        self.status = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.status.setAccessibleName("Voice preview status")
        self.status.setFrameShape(QFrame.Shape.NoFrame)
        self.status.viewport().setAutoFillBackground(False)
        self.status.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)

        self._build_layout()
        self.runtime_timer = QTimer(self)
        self.runtime_timer.setInterval(500)
        self.runtime_timer.timeout.connect(self._refresh_runtime)
        self.preview_runner.activeChanged.connect(self._refresh_runtime)
        self._refresh_runtime()
        self._connect_player()
        for runner in (self.preview_runner, self.original_runner, self.decision_runner):
            runner.activeChanged.connect(
                lambda _active: self.activeChanged.emit(self.active)
            )
        self.setVisible(False)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.portrait_image)
        layout.addWidget(self.phrase_box)
        layout.addWidget(self.reference_browser)
        actions = QHBoxLayout()
        actions.addWidget(self.candidate_voice_label, 1)
        actions.addWidget(self.a_original, alignment=Qt.AlignmentFlag.AlignTop)
        actions.addWidget(self.a_play, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(actions)
        self.a_original.setFont(self.current_original.font())
        self.a_play.setFont(self.current_play.font())
        layout.addWidget(self.a_box)
        layout.addWidget(self.current_voice_box)
        layout.addLayout(self.outcome_actions)
        layout.addWidget(self.recommendation)
        layout.addWidget(self.status)
        self.setTitle("New voice")
        self.a_box.setFlat(True)
        self.a_box.setContentsMargins(0, 0, 0, 0)
        for compact_layout in (layout, self.a_box.layout()):
            assert compact_layout is not None
            compact_layout.setContentsMargins(0, 0, 0, 0)
            compact_layout.setSpacing(4)
        for box in (self.phrase_box, self.current_voice_box, self):
            box.setFlat(False)
            if box is not self.phrase_box:
                box.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
                )
            # Keep the group title weight from propagating to body controls.
            for child in box.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
            ):
                child_font = child.font()
                child_font.setBold(child_font.bold())
                child.setFont(child_font)
            title_font = box.font()
            title_font.setBold(True)
            box.setFont(title_font)
            box_layout = box.layout()
            assert box_layout is not None
            box_layout.setContentsMargins(8, 6, 8, 6)
            box_layout.setSpacing(4)
        self.a_box.setContentsMargins(0, 0, 0, 0)
        for widget in (
            self.status,
            self.recommendation,
            self.a_play,
            self.a_original,
            self.preview_text,
        ):
            widget.installEventFilter(self)
        self._size_transient_controls()

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
        self.a_original = QPushButton("Original")
        set_action_icon(self.a_original, "play")
        self.a_original.setAccessibleName("Play original reference")
        self.a_original.clicked.connect(self._play_original)
        candidate_layout = QVBoxLayout(box)
        candidate_layout.addWidget(self.a_reason)
        self.reference_transcript = QLineEdit()
        self.reference_transcript.setAccessibleName(
            "Exact reference transcript for Qwen"
        )
        self.reference_transcript.setPlaceholderText(
            "Optional: verified words spoken in this reference"
        )
        self.reference_transcript.setToolTip(
            "Use only the exact spoken words. Leave blank for voice-only conditioning."
        )
        self.reference_transcript.editingFinished.connect(
            self._reference_transcript_edited
        )
        candidate_layout.addWidget(self.reference_transcript)
        return box

    def _build_preview(self) -> None:
        self._game_phrases: tuple[str, ...] = ()
        self.preview_text = QPlainTextEdit(tabChangesFocus=True)
        self.preview_text.setAccessibleName("Text for the generated voice preview")
        self.preview_text.setPlaceholderText("Type a phrase to hear this voice")
        self.preview_text.setWordWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
        )
        self.preview_text.textChanged.connect(self._preview_text_edited)
        self.game_phrase_button = QPushButton("Game phrase")
        set_action_icon(self.game_phrase_button, "refresh-cw")
        self.game_phrase_button.setAutoDefault(False)
        self.game_phrase_button.setAccessibleName("Insert next game phrase")
        self.game_phrase_button.setToolTip("Insert the next game phrase for preview")
        self.game_phrase_button.clicked.connect(self._insert_game_phrase)
        self.a_play = QPushButton("Preview")
        set_action_icon(self.a_play, "play")
        self.a_play.setMinimumWidth(self.a_play.sizeHint().width())
        self.a_play.setAccessibleName("Play voice preview")
        self.a_play.clicked.connect(self.play_a)
        self.phrase_box = QGroupBox("Preview phrase")
        self.phrase_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        phrase_layout = QVBoxLayout(self.phrase_box)
        phrase_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        phrase_row = QHBoxLayout()
        phrase_row.addWidget(self.preview_text, 1)
        phrase_row.addWidget(
            self.game_phrase_button, alignment=Qt.AlignmentFlag.AlignBottom
        )
        phrase_layout.addLayout(phrase_row)

    def _build_current_voice(self) -> QGroupBox:
        box = QGroupBox("Current voice")
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(box)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.current_voice_label = QLabel()
        self.current_voice_label.setTextFormat(Qt.TextFormat.PlainText)
        self.current_voice_label.setWordWrap(True)
        self.current_reason = QLabel()
        self.current_reason.setTextFormat(Qt.TextFormat.PlainText)
        self.current_reason.setWordWrap(True)
        self.current_reason.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.current_original = QPushButton("Original")
        set_action_icon(self.current_original, "play")
        self.current_original.setAccessibleName("Play current voice original reference")
        self.current_original.clicked.connect(
            lambda: self._play_original(current_voice=True)
        )
        self.current_play = QPushButton("Preview")
        set_action_icon(self.current_play, "play")
        self.current_play.setAccessibleName("Play current voice preview")
        self.current_play.clicked.connect(self.play_current)
        row = QHBoxLayout()
        row.addWidget(self.current_voice_label, 1)
        row.addWidget(self.current_original, alignment=Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.current_play, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(row)
        layout.addWidget(self.current_reason)
        self.current_status = QLabel()
        self.current_status.setTextFormat(Qt.TextFormat.PlainText)
        self.current_status.setWordWrap(True)
        self.current_status.hide()
        layout.addWidget(self.current_status)
        box.hide()
        return box

    def set_current_voice(
        self,
        plan: VoicePlan | None,
        label: str,
        *,
        playback_guard: Callable[[], None] | None = None,
        error: str | None = None,
    ) -> None:
        self.candidate_voice_label.setVisible(
            bool(self._candidate_entries or self._policy_choice)
        )
        self.setTitle("New voice")
        self._cancel_preview_request()
        self._stop_player()
        self._current_voice = None
        self._current_previews.clear()
        self._current_playback_guard = playback_guard
        self._set_playback_status("", current_voice=True)
        self.current_voice_label.setText(label)
        self.current_voice_label.setToolTip(label)
        self.current_reason.setText(error or "No current voice is available.")
        if plan is not None and not error:
            if len(plan.groups) != 1:
                raise VoiceAuditionUIError(
                    "Current voice plan must contain one character"
                )
            group = plan.groups[0]
            candidates = (*group.candidate_inventory, *group.candidates)
            if group.narrator_candidate is not None:
                candidates += (group.narrator_candidate,)
            candidate = next(
                (item for item in candidates if item.source_id == group.source_id), None
            )
            if candidate is None:
                raise VoiceAuditionUIError("Current voice plan has no resolved source")
            self._current_voice = plan, group, candidate
            self.current_voice_label.setText(_candidate_name(candidate))
            if self._sample_text is None:
                self._sample_text = group.sample_text.strip() or (
                    COMPARISON_PREVIEW_TEXT
                    if plan.synthesis_language in {None, "en", "English"}
                    else ""
                )
                with QSignalBlocker(self.preview_text):
                    self.preview_text.setPlainText(self._sample_text)
            self.current_reason.setText(
                candidate.reference_transcript
                or "\n".join(candidate.source_excerpts)
                or (
                    "Transcript unavailable. Use Original to listen to the recording."
                    if candidate.reference_sha256s
                    else "No original reference."
                )
            )
        self._refresh_saved_reference()
        if self._candidate_entries:
            self._refresh_reference_advice()
        self._refresh_current_actions()

    def _refresh_current_actions(self) -> None:
        enabled = bool(self._current_voice) and not (
            self._shutdown_requested
            or self._cancel_requested
            or self.decision_runner.active
        )
        self.current_play.setEnabled(enabled and bool(self._sample_text))
        self.current_original.setEnabled(
            enabled
            and bool(self._current_voice and self._current_voice[2].reference_sha256s)
        )

    def _set_playback_status(
        self, message: str, *, current_voice: bool = False
    ) -> None:
        if current_voice:
            self.current_status.setText(message)
            self.current_status.setVisible(bool(message))
        else:
            self._set_status(message)

    def play_current(self) -> None:
        if self._playing_source == "current-preview":
            self._stop_playback()
            return
        if self._current_voice is None or not self.current_play.isEnabled():
            return
        if not self._check_playback_available(current_voice=True):
            return
        self._stop_player()
        preview = self._current_ready_preview()
        self._cancel_preview_request()
        if preview is not None:
            self._play_preview(preview, current_voice=True)
            return
        plan, group, candidate = self._current_voice
        request = _PreviewRequest(
            plan, group, candidate, self._sample_text, Event(), True
        )
        if self.preview_runner.active:
            self._queued_preview_request = request
            self._set_playback_status(
                "Stopping the previous preview; the current voice will play next.",
                current_voice=True,
            )
        else:
            self._launch_preview_request(request)

    def _current_ready_preview(self) -> _PreviewAudio | None:
        if self._current_voice is None:
            return None
        if getattr(self.preview_service, "supports_prefetch", False) is True:
            plan, group, candidate = self._current_voice
            return cast(_PrefetchVoiceAuditionPreviewer, self.preview_service).peek(
                plan, group, candidate.source_id, text=self._sample_text
            )
        return self._current_previews.get(self._sample_text)

    def _set_status(self, message: str) -> None:
        self.status.setPlainText(message)
        self.status.setToolTip(message)
        self.status.setVisible(bool(message))

    def _initialize_selectors(self) -> None:
        self.reference_browser = QWidget(self)
        self.reference_browser.setAccessibleName("Voice references")
        layout = QVBoxLayout(self.reference_browser)
        layout.setContentsMargins(0, 0, 0, 0)
        self.reference_search = QLineEdit()
        self.reference_search.setPlaceholderText("Search references")
        self.reference_search.setAccessibleName("Search voice references")
        self._current_reference_icon = action_icon("check")
        layout.addWidget(self.reference_search)
        self.reference_model = QStandardItemModel(0, 3, self)
        self.reference_model.setHorizontalHeaderLabels(("Ref", "Text", "Length"))
        self.reference_proxy = QSortFilterProxyModel(self)
        self.reference_proxy.setSourceModel(self.reference_model)
        self.reference_proxy.setFilterKeyColumn(-1)
        self.reference_proxy.setFilterRole(Qt.ItemDataRole.ToolTipRole)
        self.reference_proxy.setFilterCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive
        )
        self.reference_proxy.setSortRole(Qt.ItemDataRole.UserRole + 1)
        self.reference_table = QTableView()
        self.reference_table.setAccessibleName("Voice references")
        self.reference_table.setModel(self.reference_proxy)
        self.reference_table.setMinimumHeight(200)
        self.reference_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.reference_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.reference_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.reference_table.setWordWrap(False)
        self.reference_table.setMouseTracking(True)
        self.reference_table.verticalHeader().hide()
        header = self.reference_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 60)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.reference_table.setSortingEnabled(True)
        self.reference_table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self.reference_table.selectionModel().currentChanged.connect(
            self._reference_row_selected
        )
        self.reference_table.entered.connect(self._reference_row_hovered)
        self.reference_search.textChanged.connect(self._filter_references)
        layout.addWidget(self.reference_table)
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
        self.recommendation = QLabel()
        self.recommendation.setAccessibleName("Generated speech content check")
        self.recommendation.setWordWrap(True)
        self.recommendation.setTextFormat(Qt.TextFormat.PlainText)

    def _size_transient_controls(self) -> None:
        self.preview_text.setFixedHeight(
            self.preview_text.fontMetrics().lineSpacing() * 2 + 12
        )
        for button in (self.a_play, self.a_original):
            button.setMinimumWidth(button.sizeHint().width())
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

    def _initialize_preview_state(self, thread_pool: QThreadPool | None) -> None:
        self.preview_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.preview_runner.finished.connect(self._preview_finished)
        self.original_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.original_runner.finished.connect(self._original_finished)
        self.decision_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.decision_runner.finished.connect(self._decision_finished)
        self._previews: dict[PreviewKey, _PreviewAudio] = {}
        self._preview_errors: dict[PreviewKey, str] = {}
        self._auditioned: dict[PreviewKey, str] = {}
        self._preview_request: _PreviewRequest | None = None
        self._queued_preview_request = None

    def _refresh_runtime(self) -> None:
        if (
            self._plan is not None
            and self._candidate_entries
            and not self._pending_decisions
            and not self._shutdown_requested
            and not self._cancel_requested
            and not self.decision_runner.active
            and self._playing_source != "preview"
            and bool(self._sample_text)
            and self._ready_preview(self._current_entry()[0]) is not None
        ):
            self._set_playing_source(self._playing_source)
            self.a_play.setEnabled(True)
            candidate = self._current_entry()[0]
            self._preview_errors.pop(self._preview_key(candidate), None)
        if (
            self._candidate_entries
            and not self._pending_decisions
            and not self._shutdown_requested
            and not self._terminal_emitted
        ):
            self._refresh_reference_advice()
            self._check_ready_previews()
            self._refresh_preparation_failure()

    @property
    def active(self) -> bool:
        return bool(
            self.preview_runner.active
            or self.original_runner.active
            or self.decision_runner.active
        )

    @property
    def current_selection(self) -> tuple[VoicePlan, VoiceGroup, str] | None:
        if self._plan is None or self._pending_decisions:
            return None
        if self._policy_choice is not None:
            return self._plan, self.current_group(), self._policy_choice[0]
        if not self._candidate_entries:
            return None
        return self._plan, self.current_group(), self._current_entry()[1]

    def start_policy(
        self, plan: VoicePlan, *, group_id: str, choice_id: str, label: str
    ) -> None:
        self.start(plan, group_id=group_id, policy_choice=(choice_id, label))

    def start(
        self,
        plan: VoicePlan,
        *,
        group_id: str,
        policy_choice: tuple[str, str] | None = None,
    ) -> None:
        if not isinstance(plan, VoicePlan):
            raise VoiceAuditionUIError("Voice audition plan is invalid")
        groups = tuple(group for group in plan.groups if group.group_id == group_id)
        if len(groups) != 1:
            raise VoiceAuditionUIError("Voice plan must have one matching character")
        if policy_choice is None and any(
            not (group.candidate_inventory or group.candidates)
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
        self._policy_choice = policy_choice
        self._show_portraits = all(
            group.portrait_image and group.portrait_image_sha256
            for group in plan.groups
            if group.character != "Narrator"
        )
        self._group = groups[0]
        self._pending_decisions = []
        self._preview_errors.clear()
        self._save_succeeded = False
        self._cancel_requested = False
        self._terminal_emitted = False
        self._shutdown_requested = False
        self.retry_save_button.setVisible(False)
        self.runtime_timer.start()
        self.setVisible(True)
        self._show_group()

    def current_group(self) -> VoiceGroup:
        if self._group is None:
            raise VoiceAuditionUIError("No character voice is selected")
        return self._group

    def play_a(self) -> None:
        if self._playing_source == "preview":
            self._stop_playback()
            return
        if (
            self.decision_runner.active
            or self._cancel_requested
            or not self._candidate_entries
            or not self._sample_text
        ):
            return
        if self.original_runner.active:
            self._stop_player()
        if not self._check_playback_available():
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
        if not request.current_voice:
            self._preview_errors.pop((request.candidate.source_id, request.text), None)
        self._stop_player()
        if not request.current_voice:
            self._accepted_choice = None
            self.a_use.setEnabled(False)
        self._set_playback_status(
            "Preparing preview; it will play automatically. You can browse other voices.",
            current_voice=request.current_voice,
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
        group = self.current_group()
        self.reference_browser.setEnabled(True)
        self._show_portrait(group)
        self.a_use.show()
        self.phrase_box.show()
        self.reference_browser.setVisible(self._policy_choice is None)
        self.a_original.setVisible(self._policy_choice is None)
        self.a_play.setVisible(self._policy_choice is None)
        if self._policy_choice is not None:
            self._candidate_entries = ()
            self.reference_model.removeRows(0, self.reference_model.rowCount())
            self.a_box.hide()
            self._accepted_choice, label = self._policy_choice
            self.candidate_voice_label.setText(label)
            self.candidate_voice_label.show()
            self.a_use.setText("Save")
            self.a_use.setEnabled(True)
            self._set_status("")
            self._refresh_saved_reference()
            self._refresh_current_actions()
            self.selectionChanged.emit()
            return
        candidates = group.candidate_inventory or group.candidates
        entries: list[CandidateEntry] = [
            (candidate, candidate.source_id, False) for candidate in candidates
        ]
        if group.narrator_candidate is not None:
            entries.append((group.narrator_candidate, default_voice_choice_id, True))
        entries.sort(
            key=lambda entry: (
                entry[0].source_id != group.source_id,
                not is_playable_main_voice_source(
                    entry[0].source_bank, entry[0].source_line_ids
                ),
            )
        )
        self._candidate_entries = tuple(entries)
        self.reference_browser.setVisible(bool(entries))
        self._advice_attempted.clear()
        self._content_checks.clear()
        self._advice_unavailable = False
        self._advice_error = None
        self._focus_index = 0
        self._populate_reference_choices()
        self._show_preview_text(group)
        self._restore_cached_previews()
        self.reference_table.setEnabled(bool(self._candidate_entries))
        self._show_current_candidate()

    @property
    def reference_source_names(self) -> tuple[str, ...]:
        """Owners of every listed reference, including the narrator fallback."""
        return tuple(
            dict.fromkeys(
                _candidate_name(candidate)
                for candidate, _choice, _narrator in self._candidate_entries
            )
        )

    def _reference_label(self, index: int, *, mixed_sources: bool) -> str:
        candidate, _choice, narrator = self._candidate_entries[index]
        if narrator:
            return f"Narrator fallback - {_candidate_name(candidate)}"
        return (
            f"{index + 1} - {_candidate_name(candidate)}"
            if mixed_sources
            else str(index + 1)
        )

    def _populate_reference_choices(self) -> None:
        mixed_sources = len(self.reference_source_names) > 1
        self.reference_table.horizontalHeader().resizeSection(
            0,
            220
            if mixed_sources or any(entry[2] for entry in self._candidate_entries)
            else 60,
        )
        with QSignalBlocker(self.reference_table.selectionModel()):
            self.reference_model.removeRows(0, self.reference_model.rowCount())
            for index, (candidate, _choice, _narrator) in enumerate(
                self._candidate_entries
            ):
                label = self._reference_label(index, mixed_sources=mixed_sources)
                text = candidate.reference_transcript or "\n".join(
                    candidate.source_excerpts
                )
                duration = (
                    f"{candidate.reference_duration_seconds:.1f} s"
                    if candidate.reference_duration_seconds is not None
                    else ""
                )
                self.reference_model.appendRow(
                    [
                        QStandardItem(label),
                        QStandardItem(text or "No transcript"),
                        QStandardItem(duration),
                    ]
                )
                self.reference_model.item(index, 0).setData(
                    index, Qt.ItemDataRole.UserRole + 1
                )
                self.reference_model.item(index, 2).setData(
                    candidate.reference_duration_seconds or 0,
                    Qt.ItemDataRole.UserRole + 1,
                )
            self.reference_search.clear()
            self.reference_table.setCurrentIndex(
                self.reference_proxy.mapFromSource(
                    self.reference_model.index(self._candidate_offset, 0)
                )
            )
        self._refresh_saved_reference()

    def _filter_references(self, text: str) -> None:
        self._hover_timer.stop()
        with QSignalBlocker(self.reference_table.selectionModel()):
            self.reference_proxy.setFilterFixedString(text)
            selected = self.reference_proxy.mapFromSource(
                self.reference_model.index(self._candidate_offset, 0)
            )
            self.reference_table.setCurrentIndex(selected)

    def _reference_row_selected(
        self, index: QModelIndex, _previous: QModelIndex
    ) -> None:
        if index.isValid():
            self._voice_reference_selected(
                self.reference_proxy.mapToSource(index).row()
            )

    def _reference_row_hovered(self, index: QModelIndex) -> None:
        if index.isValid():
            self._voice_reference_highlighted(
                self.reference_proxy.mapToSource(index).row()
            )

    def _matches_saved_voice(self, candidate: VoiceCandidate) -> bool:
        if self._current_voice is None:
            return False
        saved = self._current_voice[2]
        return (
            candidate.source_speaker == saved.source_speaker
            and normalize_character_name(candidate.source_character)
            == normalize_character_name(saved.source_character)
            and candidate.reference_sha256s == saved.reference_sha256s
            and candidate.reference_transcript == saved.reference_transcript
        )

    def _refresh_saved_reference(self) -> None:
        same_voice = bool(self._candidate_entries) and self._matches_saved_voice(
            self._current_entry()[0]
        )
        same_preview = (
            same_voice
            and self._plan is not None
            and self._current_voice is not None
            and all(
                getattr(self._plan, field) == getattr(self._current_voice[0], field)
                for field in (
                    "synthesis_backend",
                    "synthesis_model",
                    "synthesis_language",
                    "synthesis_profile",
                    "pocket_voice_cloning",
                )
            )
        )
        self.current_voice_box.setVisible(not same_preview)
        self.setTitle("Selected reference" if same_preview else "New voice")

    def _show_preview_text(self, group: VoiceGroup) -> None:
        phrases = [group.sample_text, group.alternate_sample_text]
        for candidate, _choice, _narrator in self._candidate_entries:
            phrases.extend(candidate.source_excerpts)
            phrases.append(candidate.reference_transcript)
        self._game_phrases = tuple(
            dict.fromkeys(
                text.strip()
                for text in phrases
                if text and text.strip() and text.strip() != COMPARISON_PREVIEW_TEXT
            )
        )
        self._sample_text = (
            self._sample_text
            if self._sample_text is not None
            else (
                self._game_phrases[0]
                if self._game_phrases
                else COMPARISON_PREVIEW_TEXT
                if self._require_plan().synthesis_language in {None, "en", "English"}
                else ""
            )
        )
        with QSignalBlocker(self.preview_text):
            self.preview_text.setPlainText(self._sample_text)
        self._refresh_current_actions()
        self.game_phrase_button.setToolTip(
            "Insert the next available game phrase"
            if self._game_phrases
            else "No game text is available for this voice"
        )

    def _insert_game_phrase(self) -> None:
        if self.decision_runner.active or not self._game_phrases:
            return
        current = self.preview_text.toPlainText().strip()
        index = (
            (self._game_phrases.index(current) + 1) % len(self._game_phrases)
            if current in self._game_phrases
            else 0
        )
        self.preview_text.setPlainText(self._game_phrases[index])

    def _voice_reference_selected(self, index: int) -> None:
        if self.decision_runner.active or not 0 <= index < len(self._candidate_entries):
            return
        self._cancel_preview_request()
        self._candidate_offset = index
        self._focus_index = index
        self._hover_timer.stop()
        self._show_current_candidate()

    def _preview_text_edited(self) -> None:
        if not self.decision_runner.active:
            text = self.preview_text.toPlainText().strip()
            if text != self._sample_text:
                self._set_sample_text(text)

    def _set_sample_text(self, text: str) -> None:
        self._cancel_preview_request()
        self._stop_player()
        self._cancel_advice()
        self._sample_text = text
        self._advice_attempted.clear()
        self._advice_unavailable = False
        self._advice_error = None
        self._content_checks.clear()
        if self._candidate_entries:
            self._restore_cached_previews()
            self._show_current_candidate()
        self._refresh_current_actions()

    def _reference_transcript_edited(self) -> None:
        if self.decision_runner.active or not self._candidate_entries:
            return
        candidate = self._current_entry()[0]
        transcript = self.reference_transcript.text().strip()
        if candidate.reference_transcript == transcript:
            return
        self._cancel_preview_request()
        self._cancel_advice()
        candidate = replace(candidate, reference_transcript=transcript)

        def updated(value: VoiceCandidate) -> VoiceCandidate:
            return candidate if value.source_id == candidate.source_id else value

        group = self.current_group()
        group = replace(
            group,
            candidates=tuple(updated(value) for value in group.candidates),
            candidate_inventory=tuple(
                updated(value) for value in group.candidate_inventory
            ),
            narrator_candidate=updated(group.narrator_candidate)
            if group.narrator_candidate
            else None,
        )
        self._group = group
        self._plan = replace(
            self._require_plan(),
            groups=tuple(
                group if value.group_id == group.group_id else value
                for value in self._require_plan().groups
            ),
        )
        self._candidate_entries = tuple(
            (updated(value), entry_choice, fallback)
            for value, entry_choice, fallback in self._candidate_entries
        )
        self._previews.clear()
        self._auditioned.clear()
        self._preview_errors.clear()
        self._content_checks.clear()
        self._advice_attempted.clear()
        self._restore_cached_previews()
        self._show_current_candidate()

    def _current_entry(self) -> CandidateEntry:
        return self._candidate_entries[self._candidate_offset]

    def _show_current_candidate(self) -> None:
        self._stop_player()
        self._accepted_choice = None
        candidate, _choice, _narrator = self._current_entry()
        self.candidate_voice_label.setText(_candidate_name(candidate))
        self.candidate_voice_label.show()
        with QSignalBlocker(self.reference_transcript):
            self.reference_transcript.setText(candidate.reference_transcript or "")
        self.reference_transcript.setVisible(
            self._require_plan().synthesis_backend == "qwen-tts"
        )
        reference_count = len(candidate.reference_sha256s)
        transcript = (
            candidate.reference_transcript
            or "\n".join(candidate.source_excerpts)
            or (
                "Transcript unavailable. Use Original to listen to the recording."
                if reference_count
                else "No original reference."
            )
        )
        self.a_reason.setText(transcript)
        preview = self._ready_preview(candidate)
        self.a_play.setEnabled(bool(self._sample_text))
        self.a_original.setEnabled(bool(candidate.reference_sha256s))
        self._set_playing_source(None)
        self.a_original.setToolTip(
            "Listen to the first original reference; generation uses the full reference set."
            if reference_count > 1
            else "Listen to the original reference used for this voice."
            if candidate.reference_sha256s
            else "This voice has no recorded reference."
        )
        self.a_use.setText("Save")
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
        self.reference_table.setEnabled(bool(self._candidate_entries))
        self.game_phrase_button.setEnabled(bool(self._game_phrases))
        self._prefetch_window()
        self._refresh_reference_advice()
        if preview is not None and heard:
            self._accepted_choice = self._current_entry()[1]
        preview_error = self._preview_errors.get(self._preview_key(candidate))
        self._set_status(
            f"Preview failed. {preview_error}\n"
            + (
                "Listen to the original reference or retry."
                if candidate.reference_sha256s
                else "Retry or choose another voice."
            )
            if preview_error is not None
            else ""
        )

        self._refresh_preparation_failure()
        self._refresh_saved_reference()
        self.selectionChanged.emit()

    def _preparation_error(self, candidate: VoiceCandidate) -> str | None:
        if self._availability(candidate) != "Preparation failed":
            return None
        getter = getattr(self.preview_service, "preparation_error", None)
        if callable(getter):
            error = getter(
                self._require_plan(),
                self.current_group(),
                candidate.source_id,
                text=self._sample_text,
            )
            if isinstance(error, str) and error.strip():
                return error
        return "The preview could not be prepared."

    def _refresh_preparation_failure(self) -> None:
        if self.active or self._cancel_requested or self._playing_source is not None:
            return
        candidate = self._current_entry()[0]
        current = self.status.toPlainText()
        if self._preview_key(candidate) in self._preview_errors or current.startswith(
            "Unable to"
        ):
            return
        error = self._preparation_error(candidate)
        message = (
            f"Background preview failed: {error} Play preview to retry."
            if error
            else ""
        )
        if error or current.startswith("Background preview failed:"):
            if current != message:
                self._set_status(message)

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
        request = self._completed_preview_request(error)
        if request is None:
            return
        candidate = request.candidate
        if request.current_voice:
            if error is not None:
                self._set_playback_status(
                    f"Unable to prepare current voice preview: {error}",
                    current_voice=True,
                )
            else:
                assert preview is not None
                self._current_previews[request.text] = preview
                self._play_preview(preview, current_voice=True)
            return
        if error is not None:
            self._preview_failed(request, error)
            return
        assert preview is not None
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
        self.a_play.setEnabled(True)
        self.a_original.setEnabled(bool(candidate.reference_sha256s))
        self.a_use.setEnabled(False)
        self.reference_table.setEnabled(bool(self._candidate_entries))
        self.game_phrase_button.setEnabled(bool(self._game_phrases))
        self._preview_errors[self._preview_key(candidate)] = str(error)
        self._set_status(
            f"Preview failed. {error}\n"
            + (
                "Listen to the original reference or retry."
                if candidate.reference_sha256s
                else "Retry or choose another voice."
            )
        )
        self._refresh_reference_advice()

    def _completed_preview_request(
        self, error: Exception | None
    ) -> _PreviewRequest | None:
        request = self._preview_request
        self._preview_request = None
        if request is None:
            return None
        if request.cancellation.is_set() or isinstance(error, VoiceAuditionCancelled):
            self._finish_cancelled_preview()
            return None
        if request.current_voice:
            if self._current_voice is None or (
                request.plan is not self._current_voice[0]
                or request.group != self._current_voice[1]
                or request.candidate != self._current_voice[2]
                or request.text != self._sample_text
            ):
                return None
            return request
        if request.plan is not self._plan or not self._candidate_entries:
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
            if self._playing_source is None and not self.original_runner.active:
                self._set_status(
                    "Preview preparation stopped. Browse voices or retry this preview."
                )
        self._maybe_complete()

    def _play_preview(
        self, preview: _PreviewAudio, *, current_voice: bool = False
    ) -> None:
        if not current_voice:
            self._accepted_choice = None
            self.a_use.setEnabled(False)
        try:
            guard = (
                self._current_playback_guard if current_voice else self.playback_guard
            )
            if guard is not None:
                guard()
            payload = _read_verified_audio(preview.path, preview.audio_sha256)
        except (OSError, ValueError, VoiceAuditionUIError) as error:
            if current_voice:
                self._current_previews.pop(self._sample_text, None)
            else:
                key = self._preview_key(self._current_entry()[0])
                self._previews.pop(key, None)
                self._auditioned.pop(key, None)
            self._set_playing_source(None)
            self._set_playback_status(
                f"Unable to play generated preview: {error}",
                current_voice=current_voice,
            )
            return
        player = self._ensure_player()
        player.stop()
        self._set_playing_source("current-preview" if current_voice else "preview")
        if player.play_bytes(payload, source=str(preview.path)) is None:
            if current_voice:
                self._current_previews.pop(self._sample_text, None)
            else:
                key = self._preview_key(self._current_entry()[0])
                self._previews.pop(key, None)
                self._auditioned.pop(key, None)
            self._set_playing_source(None)
            return
        if not current_voice:
            self._accepted_choice = self._current_entry()[1]
            self._auditioned[self._preview_key(self._current_entry()[0])] = (
                preview.audio_sha256
            )
            self.a_use.setEnabled(True)
        self._set_playback_status(
            "Starting the generated preview...", current_voice=current_voice
        )

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
        focus = self.reference_proxy.mapFromSource(
            self.reference_model.index(self._focus_index, 0)
        )
        if not focus.isValid():
            return ()
        count = self.reference_proxy.rowCount()
        row = focus.row()
        indices = (row, row + 1, row - 1)
        if row == 0:
            indices = (0, 1, 2)
        elif row == count - 1:
            indices = (count - 1, count - 2, count - 3)
        return tuple(
            self._candidate_entries[
                self.reference_proxy.mapToSource(
                    self.reference_proxy.index(index, 0)
                ).row()
            ][0]
            for index in indices
            if 0 <= index < count
        )

    def _prefetch_window(self) -> None:
        if (
            getattr(self.preview_service, "supports_prefetch", False) is not True
            or not self._sample_text
        ):
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

    def _availability(self, candidate: VoiceCandidate) -> str:
        if self._ready_preview(candidate) is not None:
            return "Ready"
        if (
            self._preview_request is not None
            and not self._preview_request.current_voice
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
        mixed_sources = len(self.reference_source_names) > 1
        with QSignalBlocker(self.reference_table.selectionModel()):
            for index, (candidate, _choice, _narrator) in enumerate(
                self._candidate_entries
            ):
                check = checks.get(candidate.source_id)
                content = (
                    short_messages[check.advice.status]
                    if check
                    else self._unchecked_content_state(candidate)
                )
                availability = self._availability(candidate)
                label = self._reference_label(index, mixed_sources=mixed_sources)
                is_current = self._matches_saved_voice(candidate)
                label = " - ".join(
                    part
                    for part in (
                        label,
                        availability if availability != "Not prepared" else "",
                        content
                        if content not in {"Content not checked", "No text mismatch"}
                        else "",
                    )
                    if part
                )
                origin = (
                    UNLINKED_BANK_LABEL
                    if candidate.candidate_origin == UNLINKED_BANK_MEDIA
                    else "Playable line"
                    if is_playable_main_voice_source(
                        candidate.source_bank, candidate.source_line_ids
                    )
                    else "Story reference"
                )
                text = candidate.reference_transcript or "\n".join(
                    candidate.source_excerpts
                )
                tooltip = "\n".join(
                    (
                        label,
                        f"Source: {_candidate_name(candidate)}",
                        origin,
                        *candidate.source_titles,
                        text or "No transcript",
                        self.reference_model.item(index, 2).text(),
                        availability,
                        content,
                    )
                )
                reference_item = self.reference_model.item(index, 0)
                reference_item.setText(label)
                reference_item.setIcon(
                    self._current_reference_icon if is_current else QIcon()
                )
                self.reference_model.item(index, 1).setText(text or "No transcript")
                self.reference_model.item(index, 1).setData(
                    text.casefold(), Qt.ItemDataRole.UserRole + 1
                )
                for column in range(self.reference_model.columnCount()):
                    item = self.reference_model.item(index, column)
                    item.setToolTip(
                        ("Current voice reference\n" if is_current else "") + tooltip
                    )
                    item.setData(
                        "Current voice reference" if is_current else "",
                        Qt.ItemDataRole.AccessibleDescriptionRole,
                    )
                    font = QFont(self.reference_table.font())
                    font.setBold(is_current)
                    item.setFont(font)
                    item.setData(candidate.source_id, Qt.ItemDataRole.UserRole)
        current = checks.get(self._current_entry()[0].source_id)
        message = (
            messages[current.advice.status]
            if current and current.advice.status != "no_asr_mismatch"
            else ""
        )
        if self._require_plan().synthesis_language not in {None, "en", "English"}:
            message = ""
        elif (
            current is None
            and self.advice_runner.active
            and self._current_entry()[0].source_id in self._advice_sources
        ):
            message = "Checking prepared speech content..."
        elif current is None and self._advice_unavailable:
            message = "Speech check unavailable; listen before choosing."
        self.recommendation.setText(message)
        self.recommendation.setVisible(bool(message))
        self.recommendation.setToolTip(message)

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
        if self._pending_decisions or self._terminal_emitted:
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
        if self._pending_decisions:
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

    def _play_original(self, *, current_voice: bool = False) -> None:
        source = "current-original" if current_voice else "original"
        if self._playing_source == source or (
            self.original_runner.active
            and self._original_request is not None
            and self._original_request[3] == current_voice
        ):
            self._stop_playback()
            return
        if (
            self._cancel_requested
            or self._shutdown_requested
            or self.decision_runner.active
        ):
            return
        if current_voice and self._current_voice is None:
            return
        if not current_voice and not self._candidate_entries:
            return
        if not self._check_playback_available(current_voice=current_voice):
            return
        if current_voice:
            assert self._current_voice is not None
            plan, group, candidate = self._current_voice
        else:
            plan, group, candidate = (
                self._require_plan(),
                self.current_group(),
                self._current_entry()[0],
            )
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
        if not current_voice:
            self._accepted_choice = None
            self.a_use.setEnabled(False)
        self._set_playback_status(
            "Preparing original reference...", current_voice=current_voice
        )
        self._original_request = plan, group, candidate, current_voice
        self.original_runner.start(
            self.preview_service.reference_audio,
            plan,
            group,
            candidate.source_id,
        )
        self._set_playing_source(self._playing_source)

    def _original_finished(
        self, reference: VoiceAuditionReference | None, error: Exception | None
    ) -> None:
        from vntts.support import record_game_import

        request = self._original_request
        self._original_request = None
        if request is None or self._cancel_requested or self._shutdown_requested:
            return
        plan, group, candidate, current_voice = request
        if current_voice:
            if self._current_voice != (plan, group, candidate):
                return
        elif (
            plan is not self._plan
            or not self._candidate_entries
            or candidate != self._current_entry()[0]
        ):
            return
        self._set_playing_source(None)
        guard = self._current_playback_guard if current_voice else self.playback_guard
        if error is None and guard is not None:
            try:
                guard()
            except (OSError, ValueError, VoiceAuditionUIError) as failure:
                error = failure
        if error is not None:
            self._set_playback_status(
                f"Unable to play original reference: {error}",
                current_voice=current_voice,
            )
            record_game_import(
                "voice-original-playback", outcome="failed", reason=str(error)
            )
            return
        if reference is None:
            self._set_playback_status(
                "This voice has no recorded reference.", current_voice=current_voice
            )
            record_game_import("voice-original-playback", outcome="unavailable")
            return
        self._play_original_reference(reference, candidate, current_voice=current_voice)

    def _play_original_reference(
        self,
        reference: VoiceAuditionReference,
        candidate: VoiceCandidate,
        *,
        current_voice: bool,
    ) -> None:
        player = self._ensure_player()
        self._set_playback_status(
            f"Starting the original reference for {_candidate_name(candidate)}...",
            current_voice=current_voice,
        )
        self._set_playing_source("current-original" if current_voice else "original")
        if player.play_bytes(reference.payload, source=str(reference.path)) is None:
            self._set_playing_source(None)
            return
        if not current_voice:
            self._accepted_choice = self._current_entry()[1]
            self.a_use.setEnabled(True)

    def _record_choice(self, source_id: str) -> None:
        if self._pending_decisions or self.decision_runner.active:
            return
        self._cancel_preview_request()
        self._stop_player()
        self._pending_decisions.append((self.current_group(), source_id))
        self._start_save()

    def _start_save(self) -> None:
        self._cache_cancel.set()
        self.cache_runner.cancel()
        self._cancel_advice()
        if self.decision_runner.active or not self._pending_decisions:
            return
        if getattr(self.preview_service, "supports_prefetch", False) is True:
            self.preview_service.cancel()
        self.retry_save_button.setVisible(False)
        self._set_decision_actions(False)
        self._set_status("Saving voice choice in the background...")
        self.beforeSave.emit(tuple(self._pending_decisions))
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
        self.a_play.setEnabled(
            enabled and self.a_box.isVisible() and bool(self._sample_text)
        )
        self.a_use.setEnabled(enabled and self._accepted_choice is not None)
        self.a_original.setEnabled(
            enabled
            and self.a_box.isVisible()
            and candidate is not None
            and bool(candidate.reference_sha256s)
        )
        self.reference_table.setEnabled(enabled and bool(self._candidate_entries))
        self.reference_browser.setEnabled(enabled)
        self.game_phrase_button.setEnabled(enabled and bool(self._game_phrases))
        self.preview_text.setEnabled(enabled)
        self.reference_transcript.setEnabled(enabled)
        self._refresh_current_actions()
        if not enabled:
            self.current_play.setEnabled(False)
            self.current_original.setEnabled(False)

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

    def _check_playback_available(self, *, current_voice: bool = False) -> bool:
        try:
            check_available = getattr(self._ensure_player(), "check_available", None)
            if callable(check_available):
                check_available()
        except PcmPlaybackError as error:
            if current_voice:
                self._set_playback_status(
                    f"Unable to play current voice sample: {error}", current_voice=True
                )
            else:
                self._playback_failed(str(error))
            return False
        return True

    def _playback_failed(self, message: str) -> None:
        current_voice = self._playing_source in {"current-preview", "current-original"}
        if not current_voice:
            self._accepted_choice = None
            self.a_use.setEnabled(False)
        self._set_playing_source(None)
        self._set_playback_status(
            f"Unable to play voice sample: {message}", current_voice=current_voice
        )

    def _ensure_player(self) -> _PreviewPlayer:
        if self.player is None:
            player = QMediaPlayer(self)
            self.player = player
            self._connect_player()
        return self.player

    def _connect_player(self) -> None:
        error_signal = getattr(self.player, "errorOccurred", None)
        connect_error = getattr(error_signal, "connect", None)
        if callable(connect_error):
            connect_error(lambda _code, message: self._playback_failed(message))
        signal = getattr(self.player, "playbackStateChanged", None)
        connect = getattr(signal, "connect", None)
        if callable(connect):
            connect(self._playback_state_changed)

    def _playback_state_changed(self, state: object) -> None:
        current_voice = self._playing_source in {"current-preview", "current-original"}
        if getattr(state, "name", "") == "PlayingState":
            self._set_playback_status(
                "Playing the generated preview."
                if self._playing_source in {"preview", "current-preview"}
                else "Playing the original reference.",
                current_voice=current_voice,
            )
        if getattr(state, "name", "") == "StoppedState":
            if self._playing_source is not None:
                self._set_playback_status(
                    "Current voice playback finished."
                    if self._playing_source.startswith("current-")
                    else "Playback finished. Use this voice if suitable.",
                    current_voice=current_voice,
                )
            self._set_playing_source(None)

    def _stop_playback(self) -> None:
        current_voice = self._playing_source in {
            "current-preview",
            "current-original",
        } or bool(self._original_request and self._original_request[3])
        self._cancel_preview_request()
        self._stop_player()
        self._set_playback_status("Playback stopped.", current_voice=current_voice)

    def _set_playing_source(self, source: str | None) -> None:
        self._playing_source = source
        preparing_original = (
            self.original_runner.active
            and self._original_request is not None
            and not self._original_request[3]
        )
        multiple = bool(
            self._candidate_entries
            and len(self._current_entry()[0].reference_sha256s) > 1
        )
        self.a_original.setText("Original")
        self.a_original.setAccessibleName(
            "Stop original reference"
            if source == "original" or preparing_original
            else "Play first original reference"
            if multiple
            else "Play original reference"
        )
        self.a_play.setText("Preview")
        self.a_play.setAccessibleName(
            "Stop voice preview" if source == "preview" else "Play voice preview"
        )
        preparing_current = (
            self.original_runner.active
            and self._original_request is not None
            and self._original_request[3]
        )
        self.current_original.setText("Original")
        self.current_original.setAccessibleName(
            "Stop current voice original reference"
            if source == "current-original" or preparing_current
            else "Play current voice original reference"
        )
        self.current_play.setText("Preview")
        self.current_play.setAccessibleName(
            "Stop current voice preview"
            if source == "current-preview"
            else "Play current voice preview"
        )
        for button, playing in (
            (
                self.a_original,
                source == "original" or preparing_original,
            ),
            (self.a_play, source == "preview"),
            (self.current_original, source == "current-original" or preparing_current),
            (self.current_play, source == "current-preview"),
        ):
            set_action_icon(button, "square" if playing else "play")
            button.setToolTip(button.accessibleName())

    def _stop_player(self) -> None:
        self.original_runner.cancel()
        self._original_request = None
        if self.player is not None:
            self.player.stop()
        self._set_playing_source(None)

    def _require_plan(self) -> VoicePlan:
        assert self._plan is not None
        return self._plan


__all__ = ["VoiceAuditionPanel", "VoiceAuditionUIError"]

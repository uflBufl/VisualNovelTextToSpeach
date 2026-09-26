"""Accessible Qt workbench for safe offline authoring workspaces."""

from __future__ import annotations

import codecs
import hashlib
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import TypeAlias, cast

from PySide6.QtCore import (
    QProcess,
    QProcessEnvironment,
    QSettings,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAccessible,
    QAccessibleAnnouncementEvent,
    QCloseEvent,
    QDesktopServices,
    QKeySequence,
    QShortcut,
    QTextCursor,
)
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
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav

from vntts.async_ui import LatestTaskRunner
from vntts.authoring.bulk_generation import ReviewAuthority, ReviewCommit
from vntts.authoring.cohort_bundle import (
    CohortReviewBundle,
    build_cohort_review_bundle,
)
from vntts.authoring.cohort_bundle_ui import CohortReviewBundleDialog
from vntts.authoring.generation_lease import process_started_at
from vntts.authoring.review_playback_evidence import ReviewPlaybackEvidence
from vntts.authoring.workbench import (
    AuthoringRuntimeStatus,
    AuthoringWorkbenchError,
    CollectionSelection,
    ImmutableHistoryTimestamp,
    ReviewItem,
    WorkspaceCollection,
    WorkspaceSummary,
    WorkspaceVoice,
    generation_command,
    inspect_workspace,
    list_review_items,
    load_workbench_projection_data,
    load_workspace_authority,
    prepare_review_audio,
    review_selected_item,
    review_technical_summary,
    workspace_voice_snapshot,
)
from vntts.qt_audio import QtPcmPlayer as QMediaPlayer
from vntts.qt_audio import play_audio_bytes, release_audio_buffer
from vntts.voices import CharacterVoice, CharacterVoiceRegistry

PROCESS_LOG_CHARACTER_LIMIT = 64 * 1024
PROCESS_LOG_TRUNCATION_MARKER = "... earlier process output truncated ...\n"

PollEntry: TypeAlias = (
    tuple[str, object] | tuple[str, str, int | None] | tuple[str, int, int, int, int]
)
PollSignature: TypeAlias = tuple[PollEntry, ...]
ReviewSaver: TypeAlias = Callable[
    [Path, str, str, ReviewAuthority], ReviewCommit | WorkspaceSummary
]
PlaybackPreparer: TypeAlias = Callable[[Path, ReviewItem], tuple[ReviewItem, bytes]]
CohortBundleBuilder: TypeAlias = Callable[[Sequence[Path]], CohortReviewBundle]
SpecialistReviewerFactory: TypeAlias = Callable[[CohortReviewBundle, QWidget], QDialog]


class AnnouncementLabel(QLabel):
    """Visible status text that emits a native screen-reader announcement."""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        assertive: bool = False,
    ) -> None:
        super().__init__(text, parent)
        self._announcement_politeness = (
            QAccessible.AnnouncementPoliteness.Assertive
            if assertive
            else QAccessible.AnnouncementPoliteness.Polite
        )

    def setText(self, text: str) -> None:
        message = str(text)
        changed = message != self.text()
        super().setText(message)
        if changed and self.isVisible():
            event = QAccessibleAnnouncementEvent(self, message)
            event.setPoliteness(self._announcement_politeness)
            QAccessible.updateAccessibility(event)


class DisclosureSection(QWidget):
    """Compact, keyboard-accessible inspector section with a real chevron."""

    toggled = Signal(bool)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.header = QToolButton(self)
        self.header.setText(str(title))
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.ArrowType.RightArrow)
        self.header.setAccessibleName(f"{title} disclosure")
        self.header.setAccessibleDescription(
            f"Expand or collapse the {str(title).lower()} section"
        )
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(12, 0, 0, 4)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.header)
        layout.addWidget(self.content)
        self.header.toggled.connect(self._set_expanded)
        self.setFocusProxy(self.header)
        self.setChecked(False)

    def isChecked(self) -> bool:
        return bool(self.header.isChecked())

    def setChecked(self, checked: bool) -> None:
        checked = bool(checked)
        if self.header.isChecked() == checked:
            self._set_expanded(checked, emit=False)
        else:
            self.header.setChecked(checked)

    def first_control(self) -> QWidget:
        for index in range(self.content_layout.count()):
            item = self.content_layout.itemAt(index)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                return widget
            child_layout = item.layout()
            if child_layout is not None:
                for child_index in range(child_layout.count()):
                    child_item = child_layout.itemAt(child_index)
                    if (
                        child_item is not None
                        and (child := child_item.widget()) is not None
                    ):
                        return child
        return self.header

    def _set_expanded(self, checked: bool, *, emit: bool = True) -> None:
        self.header.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self.content.setVisible(checked)
        if emit:
            self.toggled.emit(checked)


@dataclass(frozen=True)
class VoiceReference:
    character: str
    index: int
    count: int
    path: Path
    duration_seconds: float | None


class VoiceReferenceController:
    """Search and navigate the contained references of one workspace snapshot."""

    def __init__(self, manifest_path: str | Path) -> None:
        self.manifest_path = Path(manifest_path).expanduser().resolve()
        self.registry = CharacterVoiceRegistry.from_file(self.manifest_path)
        self._characters = tuple(
            sorted(
                self.registry.unique_voices(),
                key=lambda voice: voice.character.casefold(),
            )
        )
        self._indexes = {voice.character: 0 for voice in self._characters}

    @classmethod
    def from_workspace(
        cls, workspace_directory: str | Path, manifest_path: str | Path
    ) -> VoiceReferenceController:
        return cls.from_voices(
            manifest_path, workspace_voice_snapshot(workspace_directory)
        )

    @classmethod
    def from_voices(
        cls, manifest_path: str | Path, workspace_voices: Iterable[WorkspaceVoice]
    ) -> VoiceReferenceController:
        instance = cls.__new__(cls)
        instance.manifest_path = Path(manifest_path).expanduser().resolve()
        voices = tuple(
            CharacterVoice(
                character=value.character,
                speaker=value.speaker,
                aliases=value.aliases,
                references=value.references,
            )
            for value in workspace_voices
        )
        instance.registry = CharacterVoiceRegistry(voices)
        instance._characters = tuple(
            sorted(voices, key=lambda voice: voice.character.casefold())
        )
        instance._indexes = {voice.character: 0 for voice in voices}
        return instance

    def characters(self, search: str = "") -> tuple[str, ...]:
        needle = str(search).strip().casefold()
        return tuple(
            voice.character
            for voice in self._characters
            if not needle
            or needle in voice.character.casefold()
            or any(needle in alias.casefold() for alias in voice.aliases)
        )

    def references(self, character: str) -> tuple[Path, ...]:
        voice = self.registry.resolve(character)
        if voice is None:
            raise AuthoringWorkbenchError(f"Unknown voice character: {character!r}")
        return tuple(voice.references)

    def current(self, character: str) -> VoiceReference | None:
        references = self.references(character)
        if not references:
            return None
        index = min(self._indexes.get(character, 0), len(references) - 1)
        self._indexes[character] = index
        path = references[index]
        duration = None
        try:
            duration = probe_pcm16_mono_wav(path).duration_seconds
        except OSError, Pcm16MonoWavError:
            pass
        return VoiceReference(character, index, len(references), path, duration)

    def move(self, character: str, offset: int) -> VoiceReference | None:
        references = self.references(character)
        if not references:
            return None
        current = self._indexes.get(character, 0)
        self._indexes[character] = (current + int(offset)) % len(references)
        return cast(VoiceReference, self.current(character))

    def select(self, character: str, index: int) -> VoiceReference:
        references = self.references(character)
        index = int(index)
        if index < 0 or index >= len(references):
            raise AuthoringWorkbenchError(
                f"Reference index is unavailable for {character!r}: {index}"
            )
        self._indexes[character] = index
        return cast(VoiceReference, self.current(character))


@dataclass(frozen=True)
class _WorkbenchProjection:
    summary: WorkspaceSummary
    reviews: tuple[ReviewItem, ...]
    workspace: dict[str, object]
    collections: tuple[WorkspaceCollection, ...]
    collection_selection: CollectionSelection
    history: tuple[ImmutableHistoryTimestamp, ...]
    voice_controller: VoiceReferenceController | None
    poll_signature: PollSignature


def _poll_signature(paths: Iterable[Path]) -> PollSignature:
    values: list[PollEntry] = []
    for path in paths:
        try:
            status = path.lstat()
        except FileNotFoundError:
            values.append((str(path), None))
        except OSError as error:
            values.append((str(path), type(error).__name__, error.errno))
        else:
            values.append(
                (
                    str(path),
                    status.st_mode,
                    status.st_size,
                    status.st_mtime_ns,
                    status.st_ino,
                )
            )
    return tuple(values)


def _load_workbench_projection(
    workspace_directory: Path,
    selected_collection_ids: tuple[str, ...] | None,
    local_process_id: int | None,
    local_process_started_at: str | None,
    poll_paths: tuple[Path, ...],
) -> _WorkbenchProjection:
    before = _poll_signature(poll_paths)
    data = load_workbench_projection_data(
        workspace_directory,
        selected_collection_ids,
        local_process_id=local_process_id,
        local_process_started_at=local_process_started_at,
    )
    voice_controller = (
        None
        if data.summary.voice_manifest is None
        else VoiceReferenceController.from_voices(
            data.summary.voice_manifest, data.voices
        )
    )
    data.verify_voice_controls()
    after = _poll_signature(poll_paths)
    if before != after:
        raise AuthoringWorkbenchError(
            "Workspace authority changed while the workbench projection was loading"
        )
    return _WorkbenchProjection(
        summary=data.summary,
        reviews=data.reviews,
        workspace=data.workspace,
        collections=data.collections,
        collection_selection=data.collection_selection,
        history=data.history,
        voice_controller=voice_controller,
        poll_signature=after,
    )


def _prepare_review_playback(
    workspace_directory: Path, selected: ReviewItem
) -> tuple[ReviewItem, bytes]:
    del workspace_directory
    return selected, prepare_review_audio(selected)


def _save_review(
    reviewer: ReviewSaver | None,
    workspace: Path,
    queue_id: str,
    decision: str,
    authority: ReviewAuthority,
    selected: ReviewItem,
) -> ReviewCommit | WorkspaceSummary:
    if reviewer is None:
        return review_selected_item(selected, decision)
    return reviewer(workspace, queue_id, decision, authority)


class AuthoringWorkbenchDialog(QDialog):
    """Thin Qt shell over the validated authoring workspace boundary."""

    settings_group = "authoring/workbench"
    all_speakers_scope = "review-scope:all"
    narrator_scope = "review-scope:narrator"
    characters_scope = "review-scope:characters"

    def __init__(
        self,
        workspace_directory: str | Path,
        parent: QWidget | None = None,
        *,
        settings: QSettings | None = None,
        process: QProcess | None = None,
        stop_timeout_ms: int = 5_000,
        clock: Callable[[], datetime] | None = None,
        reviewer: ReviewSaver | None = None,
        review_thread_pool: QThreadPool | None = None,
        projection_loader: Callable[..., _WorkbenchProjection] | None = None,
        projection_thread_pool: QThreadPool | None = None,
        playback_preparer: PlaybackPreparer | None = None,
        synchronous_projection: bool = False,
        cohort_bundle_builder: CohortBundleBuilder | None = None,
        specialist_reviewer_factory: SpecialistReviewerFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._initialize_state(
            workspace_directory,
            settings,
            process,
            stop_timeout_ms,
            clock,
            playback_preparer,
        )
        self._initialize_task_runners(
            projection_loader,
            synchronous_projection,
            projection_thread_pool,
            reviewer,
            review_thread_pool,
            cohort_bundle_builder,
            specialist_reviewer_factory,
        )
        self._build_overview()
        self._build_voice_widgets()
        self._layout_voice_section()
        review_filters = self._build_review_table()
        review_actions, generation_actions = self._build_action_controls()
        self._build_technical_section()
        self._build_workbench_layout(review_filters, review_actions, generation_actions)
        self._connect_signals()
        self._start_ui()

    def _initialize_state(
        self,
        workspace_directory: str | Path,
        settings: QSettings | None,
        process: QProcess | None,
        stop_timeout_ms: int,
        clock: Callable[[], datetime] | None,
        playback_preparer: PlaybackPreparer | None,
    ) -> None:
        self.workspace_directory = Path(workspace_directory).expanduser().resolve()
        self.settings = settings or QSettings()
        self.process = process or QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.stop_timeout_ms = int(stop_timeout_ms)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.summary: WorkspaceSummary | None = None
        self.collection_selection: CollectionSelection | None = None
        self.voice_controller: VoiceReferenceController | None = None
        self.active_started_at: datetime | None = None
        self.close_after_stop = False
        self._finishing = False
        self._process_generation = 0
        self._stop_generation_token: int | None = None
        self.local_process_started_at: str | None = None
        self.process_outcome: str | None = None
        self.media_outcome: str | None = None
        self._current_reference_key: tuple[str, int, Path] | None = None
        self._selected_review_identity: tuple[object, ...] | None = None
        self._preview_active = False
        self._review_playback_buffer: object | None = None
        self._review_evidence = ReviewPlaybackEvidence()
        self._playback_prepare_active = False
        self._playback_preparer = playback_preparer or _prepare_review_playback
        self._playback_runner = LatestTaskRunner(self)
        self._playback_runner.finished.connect(self._playback_preparation_finished)
        self._selected_collection_ids: tuple[str, ...] | None = None
        self._recent_reference_choices: tuple[tuple[str, int], ...] | None = None
        self._collection_selection_version = 0
        self._loading_collections = self._selection_refresh_pending = False
        self._loading_recent_choices = False
        self._stop_requested = self._forced_kill = False
        self._log_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._poll_paths = self._default_poll_paths()
        self._poll_signature: PollSignature | None = None
        self._workspace: dict[str, object] | None = None
        self._all_reviews: tuple[ReviewItem, ...] = ()
        self._filtered_reviews: tuple[ReviewItem, ...] = ()
        self._selected_review_queue_id: str | None = None
        self._integrity_error: str | None = None
        self._history: tuple[ImmutableHistoryTimestamp, ...] = ()
        self._projection_active = self._projection_pending = False
        self._projection_selection_version = 0

    def _initialize_task_runners(
        self,
        projection_loader: Callable[..., _WorkbenchProjection] | None,
        synchronous_projection: bool,
        projection_thread_pool: QThreadPool | None,
        reviewer: ReviewSaver | None,
        review_thread_pool: QThreadPool | None,
        cohort_bundle_builder: CohortBundleBuilder | None,
        specialist_reviewer_factory: SpecialistReviewerFactory | None,
    ) -> None:
        self._projection_loader = projection_loader or _load_workbench_projection
        self._synchronous_projection = bool(synchronous_projection)
        self._projection_runner = LatestTaskRunner(
            self, thread_pool=projection_thread_pool
        )
        self._projection_runner.finished.connect(self._projection_finished)
        self._review_save_active = False
        self._review_save_queue_id: str | None = None
        self._review_save_decision: str | None = None
        self._review_advance_queue_id: str | None = None
        self._specialist_reviewer: QDialog | None = None
        self._reviewer = reviewer
        review_thread_pool = review_thread_pool or QThreadPool(self)
        review_thread_pool.setMaxThreadCount(1)
        self._review_runner = LatestTaskRunner(self, thread_pool=review_thread_pool)
        self._review_runner.finished.connect(self._review_save_finished)
        self._review_shortcuts: list[QShortcut] = []
        self._specialist_active = False
        self._specialist_runner = LatestTaskRunner(self)
        self._specialist_runner.finished.connect(self._specialist_task_finished)
        self._cohort_bundle_builder = (
            cohort_bundle_builder or build_cohort_review_bundle
        )
        self._specialist_reviewer_factory = (
            specialist_reviewer_factory or CohortReviewBundleDialog
        )

    def _build_overview(self) -> None:
        self.setWindowTitle("VNTTS authoring workbench")
        self.setMinimumSize(900, 640)
        self.resize(1_080, 720)
        self.title = QLabel()
        self.title.setAccessibleName("Selected authoring workspace")
        self.title.setWordWrap(True)
        self.narrator = QLabel()
        self.narrator.setAccessibleName("Configured narrator and synthesis model")
        self.narrator.setWordWrap(True)
        self.status = AnnouncementLabel(assertive=True)
        self.status.setAccessibleName("Authoring runtime status")
        self.status.setWordWrap(True)
        self.counts = QLabel()
        self.counts.setAccessibleName("Authoring outcome counts")
        self.counts.setWordWrap(True)
        self.outcome_details = DisclosureSection("Outcome details")
        self.outcome_details.setAccessibleName("Detailed authoring outcome counts")
        self.outcome_details_text = QLabel()
        self.outcome_details_text.setWordWrap(True)
        self.outcome_details_text.setAccessibleName(
            "Source-audio, fallback, skip and latest outcome details"
        )
        self.outcome_details.content_layout.addWidget(self.outcome_details_text)
        self.active = QLabel()
        self.active.setAccessibleName("Current generation attempt")
        self.active.setWordWrap(True)
        self.readiness_details = DisclosureSection("Readiness details")
        self.readiness_details.setAccessibleName("Authoring readiness details")
        self.readiness_text = QLabel()
        self.readiness_text.setWordWrap(True)
        self.readiness_text.setAccessibleName(
            "Selected collections, immutable history and input paths"
        )
        readiness_layout = self.readiness_details.content_layout
        readiness_layout.addWidget(self.readiness_text)

    def _build_voice_widgets(self) -> None:
        self.collection_tree = QTreeWidget()
        self.collection_tree.setHeaderLabels(["Story collection", "Kind", "Lines"])
        self.collection_tree.setAccessibleName("Story collections in this workspace")
        self.collection_tree.setAccessibleDescription(
            "Check declared collections to filter exact immutable queue IDs for generation and retry"
        )
        self.collection_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.voice_search = QLineEdit()
        self.voice_search.setPlaceholderText("Search configured voices")
        self.voice_search.setAccessibleName("Search voice references")
        self.voice_search.setAccessibleDescription(
            "Filter the configured voice characters available for reference preview"
        )
        self.voice_character = QComboBox()
        self.voice_character.setAccessibleName("Voice character")
        self.voice_character.setAccessibleDescription(
            "Choose a configured character voice; named characters never use narrator fallback"
        )
        self.recent_choice = QComboBox()
        self.recent_choice.setEditable(True)
        self.recent_choice.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.recent_choice.setAccessibleName("Recent narrator and reference previews")
        self.recent_choice.setAccessibleDescription(
            "Search recent contained reference choices; preview selection never changes workspace synthesis configuration"
        )
        cast(QLineEdit, self.recent_choice.lineEdit()).setPlaceholderText(
            "Search recent narrator/reference previews"
        )
        self.reference_label = QLabel("No voice reference selected")
        self.reference_label.setAccessibleName("Selected voice reference")
        self.reference_previous = QPushButton("Previous reference")
        self.reference_play = QPushButton("Play reference")
        self.reference_stop = QPushButton("Stop reference")
        self.reference_next = QPushButton("Next reference")
        self._accessible_button(
            self.reference_previous,
            "Previous voice reference",
            "Select the previous contained local reference for this character",
        )
        self._accessible_button(
            self.reference_play,
            "Play voice reference",
            "Play the selected local reference without invoking speech synthesis",
        )
        self._accessible_button(
            self.reference_stop,
            "Stop voice reference",
            "Stop reference playback",
        )
        self._accessible_button(
            self.reference_next,
            "Next voice reference",
            "Select the next contained local reference for this character",
        )

        self.player = QMediaPlayer(self)
        self.player.errorOccurred.connect(self._media_error)
        self.player.mediaStatusChanged.connect(self._media_status_changed)

    def _layout_voice_section(self) -> None:
        voice_header = QHBoxLayout()
        self.voice_search_label = QLabel("Find voice")
        self.voice_search_label.setBuddy(self.voice_search)
        self.voice_character_label = QLabel("Voice")
        self.voice_character_label.setBuddy(self.voice_character)
        voice_header.addWidget(self.voice_search_label)
        voice_header.addWidget(self.voice_search)
        voice_header.addWidget(self.voice_character_label)
        voice_header.addWidget(self.voice_character, 1)
        recent_header = QHBoxLayout()
        self.recent_choice_label = QLabel("Recent previews")
        self.recent_choice_label.setBuddy(self.recent_choice)
        recent_header.addWidget(self.recent_choice_label)
        recent_header.addWidget(self.recent_choice, 1)
        voice_controls = QHBoxLayout()
        for widget in (
            self.reference_previous,
            self.reference_play,
            self.reference_stop,
            self.reference_next,
        ):
            voice_controls.addWidget(widget)
        self.voice_box = DisclosureSection("Voice references")
        self.voice_box.setAccessibleName("Voice reference chooser")
        self.voice_content = QWidget()
        voice_layout = QVBoxLayout(self.voice_content)
        voice_layout.addLayout(recent_header)
        voice_layout.addLayout(voice_header)
        voice_layout.addWidget(self.reference_label)
        voice_layout.addLayout(voice_controls)
        self.voice_box.content_layout.addWidget(self.voice_content)

    def _build_review_table(self) -> QGridLayout:
        self.review_character = QComboBox()
        self.review_character.setAccessibleName("Filter review by source speaker")
        self.review_status = QComboBox()
        self.review_status.addItems(
            [
                "Awaiting review",
                "Technical attention",
                "All statuses",
                "Approved",
                "Rejected",
                "Failed",
                "Failed: audio limit",
                "Failed: silence",
            ]
        )
        self.review_status.setAccessibleName("Filter review by status")
        self.review_status.setAccessibleDescription(
            "Show review outcomes with one decision or failure status"
        )
        self.review_collection = QComboBox()
        self.review_collection.setAccessibleName("Filter review by collection")
        self.review_collection.setAccessibleDescription(
            "Show review outcomes from one source story collection"
        )
        self.review_search = QLineEdit()
        self.review_search.setPlaceholderText("Search line text")
        self.review_search.setAccessibleName("Filter review by line text")
        self.review_search.setAccessibleDescription(
            "Search dialogue text, line identity, or queue identity"
        )
        self.review_character.setAccessibleDescription(
            "Show every source speaker, Narrator only, characters only, or one "
            "named character without changing generation scope"
        )
        review_filters = QGridLayout()
        self.review_filter_labels: list[QLabel] = []
        for column, (text, widget) in enumerate(
            (
                ("Speaker", self.review_character),
                ("Status", self.review_status),
                ("Collection", self.review_collection),
                ("Search", self.review_search),
            )
        ):
            label = QLabel(text)
            label.setBuddy(widget)
            review_filters.addWidget(label, 0, column)
            review_filters.addWidget(widget, 1, column)
            self.review_filter_labels.append(label)
        self.review_scope = QLabel()
        self.review_scope.setAccessibleName("Independent review scope and counts")
        self.review_scope.setWordWrap(True)
        self.current_review = QLabel("Current review: none")
        self.current_review.setAccessibleName("Current review line speaker and status")
        self.current_review.setWordWrap(True)
        self.review_action_reason = AnnouncementLabel("Select an awaiting-review line")
        self.review_action_reason.setAccessibleName("Review action availability reason")
        self.review_action_reason.setWordWrap(True)
        self.specialist_review = QPushButton("Open specialist cohort reviewer")
        self.specialist_review_status = AnnouncementLabel(
            "Cohort decisions belong in the dedicated specialist reviewer."
        )
        self.specialist_review_status.setWordWrap(True)

        self.review_table = QTableWidget(0, 9)
        self.review_table.setHorizontalHeaderLabels(
            [
                "Line",
                "Source speaker",
                "Effective voice",
                "Status",
                "Attempts",
                "Collection",
                "Technical",
                "Text",
                "Queue ID",
            ]
        )
        self.review_table.setAccessibleName("Generated and failed line outcomes")
        self.review_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.review_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.review_table.currentCellChanged.connect(self._update_review_actions)
        self.review_table.setSortingEnabled(False)
        self.review_table.verticalHeader().setVisible(False)
        self.review_table.horizontalHeader().setSectionResizeMode(
            7, QHeaderView.ResizeMode.Stretch
        )
        self.review_table.setColumnHidden(6, True)
        self.review_table.setColumnHidden(8, True)
        return review_filters

    def _build_action_controls(self) -> tuple[QGridLayout, QHBoxLayout]:
        self.previous_pending = QPushButton("Previous pending")
        self.next_pending = QPushButton("Next pending")
        self.approve = QPushButton("Approve")
        self.reject_button = QPushButton("Reject")
        self.review_play = QPushButton("Replay")
        self.review_stop = QPushButton("Stop selected audio")
        self.reload_authority = QPushButton("Reload workspace")
        self.retry_failed = QPushButton("Retry failed")
        self.generate = QPushButton("Generate ready lines")
        self.stop_generation = QPushButton("Stop generation")
        self.open_output = QPushButton("Open output folder")
        self.reset_layout = QPushButton("Reset layout")
        for button, name, description in (
            (
                self.previous_pending,
                "Previous pending review item",
                "Select the previous awaiting-review item in the active review filter",
            ),
            (
                self.next_pending,
                "Next pending review item",
                "Select the next awaiting-review item in the active review filter",
            ),
            (
                self.approve,
                "Approve selected audio",
                "Make this generated line eligible for a later final game pack. "
                "Keyboard shortcut: Ctrl+Enter or Ctrl+Return",
            ),
            (
                self.reject_button,
                "Reject selected audio",
                "Keep but unpublish this generated line. Keyboard shortcut: "
                "Ctrl+Backspace",
            ),
            (
                self.review_play,
                "Play selected generated audio",
                "Play the exact validated generated WAV selected for review. "
                "Keyboard shortcut: Ctrl+R",
            ),
            (
                self.review_stop,
                "Stop selected generated audio",
                "Stop generated-audio or voice-reference preview playback",
            ),
            (
                self.reload_authority,
                "Reload workspace",
                "Revalidate workspace authority after a transient load or save failure",
            ),
            (
                self.retry_failed,
                "Retry failed lines",
                "Start a child process for exact failed queue IDs",
            ),
            (
                self.generate,
                "Generate ready lines",
                "Start a child process for ready pending lines",
            ),
            (
                self.stop_generation,
                "Stop generation",
                "Terminate the current child, then kill it after the timeout",
            ),
            (
                self.open_output,
                "Open output folder",
                "Open the contained mutable generated-audio directory",
            ),
            (
                self.reset_layout,
                "Reset authoring workbench layout",
                "Restore review-first splitter sizes and collapse secondary details",
            ),
            (
                self.specialist_review,
                "Open specialist cohort reviewer",
                "Build one checksum-bound bundle from this workspace and review it in the dedicated interface",
            ),
        ):
            self._accessible_button(button, name, description)
        review_actions = QGridLayout()
        self.review_actions_layout = review_actions
        review_buttons = (
            self.previous_pending,
            self.next_pending,
            self.review_play,
            self.review_stop,
            self.approve,
            self.reject_button,
        )
        for widget in review_buttons:
            widget.setMinimumWidth(widget.sizeHint().width())
        for column, widget in enumerate(review_buttons[:4]):
            review_actions.addWidget(widget, 0, column)
        review_actions.addWidget(self.approve, 1, 0)
        review_actions.addWidget(self.reject_button, 1, 1)
        generation_actions = QHBoxLayout()
        for widget in (
            self.retry_failed,
            self.generate,
            self.stop_generation,
            self.open_output,
        ):
            generation_actions.addWidget(widget)
        return review_actions, generation_actions

    def _build_technical_section(self) -> None:
        self.technical = DisclosureSection("Technical details")
        self.technical.setAccessibleName("Technical process details")
        self.show_technical_columns = QCheckBox("Show technical review columns")
        self.show_technical_columns.setAccessibleName(
            "Show technical and queue ID review columns"
        )
        self.show_technical_columns.setAccessibleDescription(
            "Reveal the Technical and Queue ID columns in the review table"
        )
        self.process_log = QPlainTextEdit()
        self.process_log.setReadOnly(True)
        self.process_log.setAccessibleName("Generation process log")
        self.copy_diagnostics = QPushButton("Copy diagnostics")
        self._accessible_button(
            self.copy_diagnostics,
            "Copy generation diagnostics",
            "Copy the workspace status and raw child-process log",
        )
        technical_layout = self.technical.content_layout
        technical_controls = QHBoxLayout()
        technical_controls.addWidget(self.reload_authority)
        technical_controls.addWidget(self.reset_layout)
        technical_layout.addWidget(self.show_technical_columns)
        technical_layout.addLayout(technical_controls)
        technical_layout.addWidget(self.process_log)
        technical_layout.addWidget(self.copy_diagnostics)

    def _build_workbench_layout(
        self,
        review_filters: QGridLayout,
        review_actions: QGridLayout,
        generation_actions: QHBoxLayout,
    ) -> None:
        review_panel = QGroupBox("Generated-audio review")
        review_panel.setAccessibleName("Independent generated-audio review scope")
        review_panel.setMinimumHeight(320)
        review_layout = QVBoxLayout(review_panel)
        review_layout.addLayout(review_filters)
        review_layout.addWidget(self.review_scope)
        review_layout.addWidget(self.current_review)
        review_layout.addWidget(self.review_table, 1)
        review_layout.addWidget(self.review_action_reason)
        review_layout.addLayout(review_actions)
        self.specialist_section = DisclosureSection("Specialist cohort review")
        self.specialist_section.setAccessibleName(
            "Open the dedicated specialist cohort reviewer"
        )
        self.specialist_section.content_layout.addWidget(self.specialist_review_status)
        self.specialist_section.content_layout.addWidget(self.specialist_review)
        self.specialist_section.setChecked(True)
        review_layout.addWidget(self.specialist_section)

        self.generation_section = DisclosureSection("Generation scope and controls")
        self.generation_section.setAccessibleName(
            "Collection-scoped generation controls"
        )
        generation_layout = self.generation_section.content_layout
        generation_layout.addWidget(self.narrator)
        generation_layout.addWidget(self.active)
        generation_layout.addWidget(self.collection_tree)
        generation_layout.addLayout(generation_actions)

        secondary = QWidget()
        secondary_layout = QVBoxLayout(secondary)
        secondary_layout.setContentsMargins(4, 4, 4, 4)
        secondary_layout.addWidget(self.outcome_details)
        secondary_layout.addWidget(self.generation_section)
        secondary_layout.addWidget(self.readiness_details)
        secondary_layout.addWidget(self.voice_box)
        secondary_layout.addWidget(self.technical)
        secondary_layout.addStretch(1)
        self.inspector_scroll = QScrollArea()
        self.inspector_scroll.setAccessibleName("Scrollable authoring inspector")
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_scroll.setWidget(secondary)
        self.inspector_scroll.setMinimumHeight(140)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(review_panel)
        self.splitter.addWidget(self.inspector_scroll)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.status)
        layout.addWidget(self.counts)
        layout.addWidget(self.splitter, 1)

    def _connect_signals(self) -> None:
        self.voice_search.textChanged.connect(self._populate_voice_choices)
        self.voice_character.currentTextChanged.connect(self._show_reference)
        self.voice_character.activated.connect(self._record_current_reference)
        self.recent_choice.activated.connect(self._choose_recent_reference)
        cast(QLineEdit, self.recent_choice.lineEdit()).returnPressed.connect(
            self._choose_typed_recent_reference
        )
        self.collection_tree.itemChanged.connect(self._collection_selection_changed)
        self.review_character.currentTextChanged.connect(self._apply_review_filters)
        self.review_status.currentTextChanged.connect(self._apply_review_filters)
        self.review_collection.currentTextChanged.connect(self._apply_review_filters)
        self.review_search.textChanged.connect(self._apply_review_filters)
        self.reference_previous.clicked.connect(lambda: self._move_reference(-1))
        self.reference_next.clicked.connect(lambda: self._move_reference(1))
        self.reference_play.clicked.connect(self.play_reference)
        self.reference_stop.clicked.connect(self.stop_preview)
        self.review_play.clicked.connect(self.play_selected_outcome)
        self.review_stop.clicked.connect(self.stop_preview)
        self.specialist_review.clicked.connect(self.open_specialist_reviewer)
        self.reload_authority.clicked.connect(self.refresh)
        self.approve.clicked.connect(lambda: self.review_selected("approved"))
        self.reject_button.clicked.connect(lambda: self.review_selected("rejected"))
        self.previous_pending.clicked.connect(lambda: self._move_pending(-1))
        self.next_pending.clicked.connect(lambda: self._move_pending(1))
        self.retry_failed.clicked.connect(self.start_failed_retry)
        self.generate.clicked.connect(self.start_generation)
        self.stop_generation.clicked.connect(self.stop_child)
        self.open_output.clicked.connect(self.open_output_folder)
        self.reset_layout.clicked.connect(self._reset_layout)
        self.copy_diagnostics.clicked.connect(self.copy_diagnostic_text)
        self.show_technical_columns.toggled.connect(self._show_technical_review_columns)
        self.technical.toggled.connect(self._technical_toggled)
        self.readiness_details.toggled.connect(self.readiness_text.setVisible)
        self.voice_box.toggled.connect(self.voice_content.setVisible)
        for section in (
            self.generation_section,
            self.outcome_details,
            self.readiness_details,
            self.voice_box,
            self.technical,
        ):
            section.toggled.connect(
                lambda checked, value=section: self._inspector_section_toggled(
                    value, checked
                )
            )
        self.process.readyReadStandardOutput.connect(self._append_process_output)
        self.process.started.connect(self._process_started)
        self.process.finished.connect(self._process_finished)
        self.process.errorOccurred.connect(self._process_error)

    def _start_ui(self) -> None:
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1_000)
        self.elapsed_timer.timeout.connect(self.update_elapsed)
        self.elapsed_timer.start()
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(1_000)
        self.status_timer.timeout.connect(self._poll_authoritative)
        self._restore_settings()
        self._restore_collection_selection()
        self._install_review_shortcuts()
        self.refresh()
        self.status_timer.start()
        self._set_focus_chain()
        self.review_table.setFocus()

    @staticmethod
    def _accessible_button(button: QPushButton, name: str, description: str) -> None:
        button.setAccessibleName(name)
        button.setAccessibleDescription(description)

    def _restore_collection_selection(self) -> None:
        stored = self.settings.value(self._workspace_settings_key("collections"))
        if stored is None:
            return
        if isinstance(stored, str):
            stored = [stored]
        self._selected_collection_ids = tuple(str(value) for value in stored)

    def _projection_arguments(
        self,
    ) -> tuple[Path, tuple[str, ...] | None, int | None, str | None, tuple[Path, ...]]:
        return (
            self.workspace_directory,
            self._selected_collection_ids,
            (
                int(self.process.processId())
                if self.process.state() != QProcess.ProcessState.NotRunning
                else None
            ),
            self.local_process_started_at,
            self._poll_paths,
        )

    def refresh(self) -> None:
        selected = self._selected_review_item()
        if selected is not None:
            self._selected_review_queue_id = selected.queue_id
        if self._projection_active:
            self._projection_pending = True
            return
        if self._review_save_active:
            self._projection_pending = True
            return
        self._projection_pending = False
        arguments = self._projection_arguments()
        if not self._synchronous_projection:
            self._projection_active = True
            self._projection_selection_version = self._collection_selection_version
            self.reload_authority.setEnabled(False)
            if self.summary is None:
                self.status.setText("LOADING: validating authoritative workspace")
            self._update_review_actions(preserve_queue_id=True)
            self._projection_runner.start(self._projection_loader, *arguments)
            return
        try:
            projection = self._projection_loader(*arguments)
        except Exception as error:
            self._fail_closed(error)
            return
        self._apply_projection(projection)

    def _projection_finished(self, projection: object, error: Exception | None) -> None:
        if not self._projection_active:
            return
        self._projection_active = False
        if error is not None:
            self._fail_closed(error)
        elif not isinstance(projection, _WorkbenchProjection):
            self._fail_closed("Authority worker returned no validated projection")
        elif self._projection_selection_version != self._collection_selection_version:
            self._projection_pending = False
            QTimer.singleShot(0, self.refresh)
            return
        else:
            self._apply_projection(projection)
        if self._projection_pending:
            self._projection_pending = False
            QTimer.singleShot(0, self.refresh)

    def _default_poll_paths(self) -> tuple[Path, ...]:
        output = self.workspace_directory / "generated-audio"
        return (
            self.workspace_directory / "workspace.json",
            self.workspace_directory / "queue.jsonl",
            self.workspace_directory / "inputs/story-index.jsonl",
            self.workspace_directory / "inputs/voice/manifest.json",
            output / "generation-state.json",
            output / "manifest.json",
            output / ".generation-lease.json",
            output / ".job-process.json",
        )

    def _workspace_poll_signature(self) -> PollSignature:
        return _poll_signature(self._poll_paths)

    def _poll_authoritative(self) -> None:
        if self._review_save_active or self._projection_active:
            return
        if self._poll_signature != self._workspace_poll_signature():
            self.refresh()

    def _fail_closed(self, error: object) -> None:
        self._projection_pending = False
        self._projection_runner.cancel()
        self._projection_active = False
        self._playback_runner.cancel()
        self._playback_prepare_active = False
        self._review_runner.cancel()
        self._review_save_active = False
        self._review_save_queue_id = None
        self._review_save_decision = None
        self._review_advance_queue_id = None
        self._discard_review_playback_copy()
        self._preview_active = False
        self.summary = None
        self.collection_selection = None
        self._workspace = None
        self._history = ()
        self._integrity_error = str(error)
        self._all_reviews = ()
        self._filtered_reviews = ()
        self.voice_controller = None
        self._current_reference_key = None
        self._selected_review_identity = None
        self.status.setText(f"BLOCKED: {error}")
        self.status.setToolTip(str(error))
        self.review_table.setRowCount(0)
        self.review_scope.setText("Review unavailable: integrity validation failed")
        self.current_review.setText("Current review: none")
        self.review_action_reason.setText(f"Review disabled: {error}")
        self.collection_tree.clear()
        self.voice_character.clear()
        self.recent_choice.clear()
        self.recent_choice.setEnabled(False)
        self.readiness_text.setText(f"Blocked: {error}")
        self.reference_label.setText("Voice references unavailable")
        for action in (
            self.generate,
            self.retry_failed,
            self.approve,
            self.reject_button,
            self.review_play,
            self.review_stop,
            self.open_output,
            self.reference_previous,
            self.reference_play,
            self.reference_stop,
            self.reference_next,
        ):
            action.setEnabled(False)
            action.setToolTip(str(error))
        self.reload_authority.setEnabled(not self._projection_active)
        self.reload_authority.setText("Retry workspace load")
        self.reload_authority.setToolTip("Retry authoritative workspace validation")
        self.technical.setChecked(True)
        self.stop_generation.setEnabled(
            self.process.state() != QProcess.ProcessState.NotRunning
        )

    def _apply_projection(self, projection: _WorkbenchProjection) -> None:
        self.summary = projection.summary
        reviews = projection.reviews
        self._workspace = projection.workspace
        self._history = projection.history
        self._poll_signature = projection.poll_signature
        self._selected_collection_ids = projection.collection_selection.collection_ids
        self.title.setText(self.summary.title)
        self._integrity_error = None
        workspace = projection.workspace
        run_config = cast(dict[str, object], workspace["run_config"])
        narrator_character = str(workspace["narrator_character"])
        self.narrator.setText(
            f"Narrator: {narrator_character} | Backend: {run_config['backend']} | "
            f"Model: {run_config['model']} | Profile: {run_config['generation_profile']}"
        )
        self._populate_collections(projection.collections)
        self.collection_selection = projection.collection_selection
        self.status.setText(self._status_text())
        self.status.setToolTip("; ".join(self.summary.blocked_reasons))
        self._show_counts()
        self._show_readiness_details(workspace, projection.history)
        self._show_active()
        self._all_reviews = tuple(reviews)
        self._populate_review_filter_choices()
        self._apply_review_filters()
        self._load_voice_controller(projection.voice_controller)
        self._populate_recent_choices(narrator_character)
        self.recent_choice.setEnabled(self.recent_choice.count() > 0)
        running = self.process.state() != QProcess.ProcessState.NotRunning
        owned_elsewhere = self.summary.runtime_status in {
            AuthoringRuntimeStatus.RUNNING_HERE,
            AuthoringRuntimeStatus.RUNNING_EXTERNAL,
            AuthoringRuntimeStatus.BLOCKED,
        }
        selection_readiness = self.collection_selection.readiness
        self.generate.setEnabled(
            not running
            and not owned_elsewhere
            and selection_readiness.ready > 0
            and not selection_readiness.blocked_reasons
        )
        self.generate.setToolTip(
            "" if self.generate.isEnabled() else self._disabled_generation_reason()
        )
        retry_enabled = (
            not running
            and not owned_elsewhere
            and selection_readiness.failed > 0
            and selection_readiness.ready > 0
            and not selection_readiness.blocked_reasons
        )
        self.retry_failed.setEnabled(retry_enabled)
        self.retry_failed.setToolTip(
            ""
            if retry_enabled
            else (
                "Another process owns generation"
                if self.summary.runtime_status
                in {
                    AuthoringRuntimeStatus.RUNNING_HERE,
                    AuthoringRuntimeStatus.RUNNING_EXTERNAL,
                }
                else "No ready failed lines are available to retry"
            )
        )
        self.stop_generation.setEnabled(running)
        self.open_output.setEnabled(True)
        self.reload_authority.setEnabled(True)
        self.reload_authority.setText("Reload workspace")
        self.reload_authority.setToolTip("Reload authoritative workspace state")
        self._update_review_actions(preserve_queue_id=True)

    def _show_counts(self) -> None:
        if self.summary is None or self.collection_selection is None:
            return
        self.counts.setText(
            "<b>Review</b>: "
            + " | ".join(
                (
                    f"Generated awaiting review: {self.summary.generated}",
                    f"Approved: {self.summary.approved}",
                    f"Rejected: {self.summary.rejected}",
                )
            )
            + "<br><b>Coverage</b>: "
            + " | ".join(
                (
                    f"Lines ready to generate: {self.summary.pending}",
                    f"Failed: {self.summary.failed}",
                    f"Missing references: {self.summary.missing_voice if self.summary.missing_voice is not None else 'unknown'}",
                    f"Live fallback: {self.summary.live_fallback}",
                    f"Omitted events: {self.summary.omitted}",
                )
            )
            + "<br><b>Selection</b>: "
            + " | ".join(
                (
                    f"Selected collections: {self.collection_selection.collection_count}",
                    f"Selected story lines: {self.collection_selection.story_records}",
                    f"Selected queue lines: {self.collection_selection.queue_items}",
                    f"Selected ready lines: {self.collection_selection.readiness.ready}",
                )
            )
        )
        self.outcome_details_text.setText(
            "<b>Source handling</b>: "
            + " | ".join(
                (
                    f"Recoverable source audio: {self.summary.recoverable_source_audio}",
                    f"Manual review: {self.summary.manual_review}",
                    f"Resolve source audio: {self.summary.resolve_audio}",
                )
            )
            + "<br><b>Skipped</b>: "
            + " | ".join(
                (
                    "Audio events / sound effects: "
                    f"{self.summary.skipped_sound_effects}",
                    f"Other actions: {self.summary.skipped_actions}",
                )
            )
            + "<br><b>Latest outcome</b>: "
            + " | ".join(
                (
                    f"Line: {self.summary.latest_line or 'none'}",
                    f"Status: {self.summary.latest_status or 'none'}",
                    f"Updated: {self.summary.latest_updated_at or 'unknown'}",
                )
            )
        )

    def _status_text(self) -> str:
        summary = self.summary
        assert summary is not None
        labels = {
            AuthoringRuntimeStatus.READY: "READY: generation can start",
            AuthoringRuntimeStatus.RUNNING_HERE: "RUNNING HERE: child generation is active",
            AuthoringRuntimeStatus.RUNNING_EXTERNAL: "RUNNING ELSEWHERE: another process owns generation",
            AuthoringRuntimeStatus.INTERRUPTED: "INTERRUPTED: inspect and resume the preserved attempt",
            AuthoringRuntimeStatus.NEEDS_REVIEW: "REVIEW REQUIRED: generated audio awaits decisions",
            AuthoringRuntimeStatus.NEEDS_ATTENTION: "NEEDS ATTENTION: failed or missing inputs remain",
            AuthoringRuntimeStatus.COMPLETE: "COMPLETE: all selected outcomes are terminal",
            AuthoringRuntimeStatus.BLOCKED: "BLOCKED: configuration or integrity must be repaired",
        }
        lines = [
            outcome for outcome in (self.process_outcome, self.media_outcome) if outcome
        ]
        if summary.runtime_status in {
            AuthoringRuntimeStatus.RUNNING_HERE,
            AuthoringRuntimeStatus.RUNNING_EXTERNAL,
            AuthoringRuntimeStatus.INTERRUPTED,
            AuthoringRuntimeStatus.BLOCKED,
        }:
            primary = labels[summary.runtime_status]
        else:
            primary = labels[summary.runtime_status]
        lines.append(primary)
        selection = self.collection_selection
        if selection is not None and not selection.collection_ids:
            lines.append(
                "NO COLLECTION SELECTED: generation is disabled; review remains independently available"
            )
        elif selection is not None and selection.queue_items == 0:
            lines.append(
                "NO QUEUED ITEMS IN SELECTION: review remains independently available"
            )
        elif selection is not None and selection.readiness.blocked_reasons:
            lines.append(
                "GENERATION SCOPE NEEDS ATTENTION: "
                + "; ".join(selection.readiness.blocked_reasons)
            )
        elif selection is not None:
            lines.append(
                f"GENERATION SCOPE READY: {selection.readiness.ready} selected line(s)"
            )
        return "\n".join(lines)

    def _disabled_generation_reason(self) -> str:
        if self.collection_selection is None:
            return "Collection selection is unavailable"
        readiness = self.collection_selection.readiness
        if not self.collection_selection.collection_ids:
            return "Select at least one story collection"
        if readiness.blocked_reasons:
            return "; ".join(readiness.blocked_reasons)
        if readiness.ready == 0:
            return "No ready pending or failed lines exist in selected collections"
        summary = self.summary
        assert summary is not None
        if summary.blocked_reasons:
            return "; ".join(summary.blocked_reasons)
        if summary.runtime_status is AuthoringRuntimeStatus.NEEDS_REVIEW:
            return "Review generated audio before starting more work"
        if summary.runtime_status in {
            AuthoringRuntimeStatus.RUNNING_HERE,
            AuthoringRuntimeStatus.RUNNING_EXTERNAL,
        }:
            return "Generation is already running"
        return "No ready pending lines are available"

    def _show_readiness_details(
        self,
        workspace: dict[str, object],
        history: Iterable[ImmutableHistoryTimestamp] | None = None,
    ) -> None:
        selection = self.collection_selection
        assert selection is not None
        story = workspace.get("story_index")
        voice = workspace.get("voice_manifest")
        history = self._history if history is None else tuple(history)
        readiness = selection.readiness
        lines = [
            "Collections: " + (", ".join(selection.collection_ids) or "none"),
            f"Exact selected queue IDs: {len(readiness.queue_ids)}",
            "Story snapshot: "
            + (
                f"{story['path']} ({story['sha256'][:12]}...)"
                if isinstance(story, dict)
                else "not configured"
            ),
            "Voice snapshot: "
            + (
                f"{voice['path']} ({voice['sha256'][:12]}...)"
                if isinstance(voice, dict)
                else "not configured"
            ),
            *(value.display for value in history),
        ]
        if not any(value.kind.startswith("Source ") for value in history):
            lines.append("Source job time: unavailable in this legacy import")
        if readiness.blocked_reasons:
            lines.append("Selection blockers: " + "; ".join(readiness.blocked_reasons))
        self.readiness_text.setText("\n".join(lines))

    def _show_active(self) -> None:
        summary = self.summary
        assert summary is not None
        attempt = summary.active
        if attempt is None:
            self.active_started_at = None
            self.active.setText("Current attempt: none")
            return
        self.active_started_at = self._parse_datetime(attempt.started_at)
        self.active.setText(
            f"Current attempt: {attempt.line_id or attempt.queue_id} | {attempt.speaker or 'unknown voice'} | "
            f"{attempt.phase or 'unknown phase'} | attempt {attempt.attempt or '?'} of {attempt.attempt_limit or '?'} | "
            f"latest error: {attempt.last_error or 'none'}"
        )
        self.update_elapsed()

    def update_elapsed(self) -> None:
        if self.active_started_at is None:
            return
        seconds = max(0, int((self.clock() - self.active_started_at).total_seconds()))
        base = self.active.text().split(" | elapsed ", 1)[0]
        self.active.setText(f"{base} | elapsed {seconds // 60}:{seconds % 60:02d}")

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _populate_collections(self, collections: Iterable[WorkspaceCollection]) -> None:
        self._loading_collections = True
        self.collection_tree.blockSignals(True)
        self.collection_tree.clear()
        try:
            declared = tuple(collection.collection_id for collection in collections)
            if self._selected_collection_ids is None:
                self._selected_collection_ids = declared
            selected = set(self._selected_collection_ids)
            for collection in collections:
                item = QTreeWidgetItem(
                    self.collection_tree,
                    [
                        collection.title,
                        collection.kind,
                        str(collection.record_count),
                    ],
                )
                item.setData(0, Qt.ItemDataRole.UserRole, collection.collection_id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if collection.collection_id in selected
                    else Qt.CheckState.Unchecked,
                )
        finally:
            self.collection_tree.blockSignals(False)
            self._loading_collections = False

    def _collection_selection_changed(
        self, _item: QTreeWidgetItem, _column: int
    ) -> None:
        if self._loading_collections:
            return
        selected = []
        for index in range(self.collection_tree.topLevelItemCount()):
            item = cast(QTreeWidgetItem, self.collection_tree.topLevelItem(index))
            if item.checkState(0) == Qt.CheckState.Checked:
                selected.append(str(item.data(0, Qt.ItemDataRole.UserRole)))
        self._selected_collection_ids = tuple(selected)
        self._collection_selection_version += 1
        self.settings.setValue(
            self._workspace_settings_key("collections"), list(selected)
        )
        self.settings.sync()
        if self._selection_refresh_pending:
            return
        self._selection_refresh_pending = True
        QTimer.singleShot(0, self._refresh_collection_selection)

    def _refresh_collection_selection(self) -> None:
        self._selection_refresh_pending = False
        self.refresh()

    def _workspace_settings_key(self, suffix: str) -> str:
        return (
            f"{self.settings_group}/workspaces/{self.workspace_directory.name}/{suffix}"
        )

    def _workspace_document(self) -> tuple[Path, dict[str, object]]:
        if self._workspace is not None:
            return self.workspace_directory, self._workspace
        directory, workspace, _workspace_sha256 = load_workspace_authority(
            self.workspace_directory
        )
        return directory, workspace

    def _load_voice_controller(
        self, controller: VoiceReferenceController | None
    ) -> None:
        if controller is None:
            self.voice_controller = None
            self.voice_character.clear()
            self._show_reference()
            return
        current = None
        if self.voice_controller is not None and self.voice_character.currentText():
            current = self.voice_controller.current(self.voice_character.currentText())
        if current is not None:
            try:
                controller.select(current.character, current.index)
            except AuthoringWorkbenchError:
                pass
        self.voice_controller = controller
        self._populate_voice_choices()

    def _populate_voice_choices(self, *_arguments: object) -> None:
        current = self.voice_character.currentText()
        self.voice_character.blockSignals(True)
        self.voice_character.clear()
        if self.voice_controller is not None:
            self.voice_character.addItems(
                self.voice_controller.characters(self.voice_search.text())
            )
        if current:
            index = self.voice_character.findText(current)
            if index >= 0:
                self.voice_character.setCurrentIndex(index)
        self.voice_character.blockSignals(False)
        self._show_reference()

    def _populate_recent_choices(self, narrator_character: str) -> None:
        self._loading_recent_choices = True
        try:
            values = []
            if self.voice_controller is not None:
                try:
                    if self.voice_controller.references(narrator_character):
                        values.append((narrator_character, 0))
                except AuthoringWorkbenchError:
                    pass
                stored = self.settings.value(
                    self._workspace_settings_key("recent-references"), []
                )
                if isinstance(stored, str):
                    stored = [stored]
                for encoded in cast(Iterable[object], stored):
                    try:
                        value = json.loads(str(encoded))
                    except TypeError, ValueError:
                        continue
                    if not isinstance(value, dict) or set(value) != {
                        "character",
                        "index",
                    }:
                        continue
                    character = value["character"]
                    index = value["index"]
                    if (
                        not isinstance(character, str)
                        or not character.strip()
                        or isinstance(index, bool)
                        or not isinstance(index, int)
                    ):
                        continue
                    try:
                        references = self.voice_controller.references(character)
                    except AuthoringWorkbenchError:
                        continue
                    if index < 0 or index >= len(references):
                        continue
                    choice = (character, index)
                    if choice not in values:
                        values.append(choice)
            values = values[:8]
            self.recent_choice.blockSignals(True)
            self.recent_choice.clear()
            for character, index in values:
                self.recent_choice.addItem(
                    f"{character} - reference {index + 1}",
                    (character, index),
                )
            self.recent_choice.blockSignals(False)
            self._store_recent_choices(values)
        finally:
            self._loading_recent_choices = False

    def _store_recent_choices(self, values: Iterable[tuple[str, int]]) -> None:
        values = tuple(values)
        if values == self._recent_reference_choices:
            return
        encoded = [
            json.dumps(
                {"character": character, "index": index},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for character, index in values
        ]
        self.settings.setValue(
            self._workspace_settings_key("recent-references"), encoded
        )
        self.settings.sync()
        self._recent_reference_choices = values

    def _record_current_reference(self, *_arguments: object) -> None:
        if self._loading_recent_choices or self.voice_controller is None:
            return
        reference = self.voice_controller.current(self.voice_character.currentText())
        if reference is None:
            return
        current = (reference.character, reference.index)
        values = [current]
        for index in range(self.recent_choice.count()):
            value = self.recent_choice.itemData(index)
            if isinstance(value, (tuple, list)) and len(value) == 2:
                choice = (str(value[0]), int(value[1]))
                if choice not in values:
                    values.append(choice)
        self._store_recent_choices(values[:8])
        _directory, workspace = self._workspace_document()
        self._populate_recent_choices(str(workspace["narrator_character"]))

    def _choose_recent_reference(self, index: int) -> None:
        if self._loading_recent_choices:
            return
        value = self.recent_choice.itemData(index)
        if isinstance(value, (tuple, list)) and len(value) == 2:
            self._apply_recent_reference(str(value[0]), int(value[1]))

    def _choose_typed_recent_reference(self) -> None:
        typed = self.recent_choice.currentText().strip().casefold()
        for index in range(self.recent_choice.count()):
            if self.recent_choice.itemText(index).casefold() == typed:
                self._choose_recent_reference(index)
                return
        self.media_outcome = "RECENT PREVIEW UNAVAILABLE: choose a validated entry"
        if self.summary is not None:
            self.status.setText(self._status_text())

    def _apply_recent_reference(self, character: str, index: int) -> None:
        if self.summary is None or self.summary.voice_manifest is None:
            return
        try:
            controller = VoiceReferenceController.from_workspace(
                self.workspace_directory, self.summary.voice_manifest
            )
            controller.select(character, index)
        except AuthoringWorkbenchError as error:
            self.media_outcome = f"RECENT PREVIEW UNAVAILABLE: {error}"
            self._record_current_reference()
            return
        self.voice_controller = controller
        self.voice_search.clear()
        choice = self.voice_character.findText(character)
        if choice >= 0:
            self.voice_character.setCurrentIndex(choice)
        self._show_reference()
        self._record_current_reference()

    def _show_reference(self, *_arguments: object) -> None:
        reference = (
            self.voice_controller.current(self.voice_character.currentText())
            if self.voice_controller is not None and self.voice_character.currentText()
            else None
        )
        reference_key = (
            None
            if reference is None
            else (reference.character, reference.index, reference.path)
        )
        if reference_key != self._current_reference_key:
            self._discard_review_playback_copy()
            self._preview_active = False
            self.media_outcome = None
            self._current_reference_key = reference_key
        enabled = reference is not None
        for widget in (
            self.reference_previous,
            self.reference_play,
            self.reference_stop,
            self.reference_next,
        ):
            widget.setEnabled(enabled)
        if reference is None:
            self.reference_label.setText("No voice reference selected")
            return
        duration = (
            "unknown duration"
            if reference.duration_seconds is None
            else f"{reference.duration_seconds:.2f} seconds"
        )
        self.reference_label.setText(
            f"{reference.character}: reference {reference.index + 1}/{reference.count}, {duration}"
        )
        self.reference_label.setToolTip(str(reference.path))

    def _move_reference(self, offset: int) -> None:
        if self.voice_controller is None or not self.voice_character.currentText():
            return
        self.voice_controller.move(self.voice_character.currentText(), offset)
        self._show_reference()
        self._record_current_reference()

    def play_reference(self) -> None:
        if self.voice_controller is None:
            return
        token = self.voice_controller.current(self.voice_character.currentText())
        if token is None:
            return
        try:
            current = inspect_workspace(self.workspace_directory)
            if current.voice_manifest is None:
                raise AuthoringWorkbenchError("Voice manifest is no longer available")
            trusted = VoiceReferenceController.from_workspace(
                self.workspace_directory, current.voice_manifest
            )
            reference = trusted.select(token.character, token.index)
        except AuthoringWorkbenchError as error:
            self.status.setText(f"BLOCKED: voice reference changed: {error}")
            return
        if current.voice_manifest != trusted.manifest_path:
            self.status.setText("BLOCKED: voice manifest selection changed")
            return
        self.voice_controller = trusted
        self.player.setSource(QUrl.fromLocalFile(str(reference.path)))
        self.player.play()
        self._preview_active = True
        self.media_outcome = f"PLAYING REFERENCE: {reference.character} {reference.index + 1}/{reference.count}"
        self.status.setText(self._status_text())

    def _media_error(self, _error: object, message: str = "") -> None:
        self._discard_review_playback_copy()
        self._preview_active = False
        self.media_outcome = "AUDIO PREVIEW ERROR: " + (
            message or self.player.errorString()
        )
        if self.summary is not None:
            self.status.setText(self._status_text())
        self._update_review_actions(preserve_queue_id=True)

    def _media_status_changed(self, status: object) -> None:
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        self._review_evidence.complete()
        self._discard_review_playback_copy()
        self._preview_active = False
        self.media_outcome = "AUDIO PREVIEW FINISHED"
        if self.summary is not None:
            self.status.setText(self._status_text())
        self._update_review_actions(preserve_queue_id=True)

    def stop_preview(self) -> None:
        self._discard_review_playback_copy()
        self._preview_active = False
        self.media_outcome = "AUDIO PREVIEW STOPPED"
        if self.summary is not None:
            self.status.setText(self._status_text())
        self._update_review_actions(preserve_queue_id=True)

    def _populate_reviews(self, reviews: Iterable[ReviewItem]) -> None:
        reviews = tuple(reviews)
        self.review_table.setRowCount(len(reviews))
        for row, review in enumerate(reviews):
            for column, value in enumerate(
                (
                    review.line_id,
                    review.speaker,
                    self._effective_review_voice(review),
                    review.review_status or review.status,
                    str(review.attempts),
                    review.collection_id or "Unassigned",
                    review_technical_summary(review),
                    review.text,
                    review.queue_id,
                )
            ):
                self.review_table.setItem(row, column, QTableWidgetItem(value))
            cast(QTableWidgetItem, self.review_table.item(row, 0)).setData(256, review)
        for column, width in enumerate((190, 120, 120, 110, 80, 120, 260)):
            self.review_table.setColumnWidth(column, width)

    def _populate_review_filter_choices(self) -> None:
        current_scope = getattr(
            self,
            "_stored_review_character_scope",
            self.review_character.currentData() or self.all_speakers_scope,
        )
        self.review_character.blockSignals(True)
        self.review_character.clear()
        self.review_character.addItem("All speakers", self.all_speakers_scope)
        self.review_character.addItem("Narrator only", self.narrator_scope)
        self.review_character.addItem("Characters only", self.characters_scope)
        for character in sorted(
            {
                item.voice_character
                for item in self._all_reviews
                if item.voice_character.casefold() != "narrator"
            },
            key=str.casefold,
        ):
            self.review_character.addItem(character, character)
        index = self.review_character.findData(current_scope)
        self.review_character.setCurrentIndex(index if index >= 0 else 0)
        self.review_character.blockSignals(False)
        if hasattr(self, "_stored_review_character_scope"):
            del self._stored_review_character_scope
        self._replace_combo_values(
            self.review_collection,
            "All collections",
            sorted(
                {
                    item.collection_id
                    for item in self._all_reviews
                    if item.collection_id is not None
                },
                key=str.casefold,
            ),
        )
        if hasattr(self, "_stored_review_collection"):
            index = self.review_collection.findText(self._stored_review_collection)
            self.review_collection.setCurrentIndex(index if index >= 0 else 0)
            del self._stored_review_collection

    @staticmethod
    def _replace_combo_values(
        combo: QComboBox, all_label: str, values: Sequence[str]
    ) -> None:
        current = combo.currentText() or all_label
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(all_label)
        combo.addItems(values)
        index = combo.findText(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _apply_review_filters(self, *_arguments: object) -> None:
        if not hasattr(self, "review_character"):
            return
        character_scope = self.review_character.currentData()
        status = self.review_status.currentText()
        collection = self.review_collection.currentText()
        needle = self.review_search.text().strip().casefold()

        def included(item: ReviewItem) -> bool:
            narrator = item.voice_character.casefold() == "narrator"
            if character_scope == self.narrator_scope and not narrator:
                return False
            if character_scope == self.characters_scope and narrator:
                return False
            if (
                character_scope
                not in {
                    None,
                    self.all_speakers_scope,
                    self.narrator_scope,
                    self.characters_scope,
                }
                and item.voice_character != character_scope
            ):
                return False
            if collection not in {"", "All collections"} and (
                item.collection_id != collection
            ):
                return False
            if needle and not any(
                needle in value.casefold()
                for value in (item.text, item.line_id, item.queue_id)
            ):
                return False
            if status == "Awaiting review":
                return item.status == "generated" and item.review_status in {
                    None,
                    "pending_review",
                }
            if status == "Technical attention":
                return (
                    item.status == "generated"
                    and item.review_status in {None, "pending_review"}
                    and bool(item.technical_flags)
                )
            if status == "Approved":
                return bool(
                    item.status == "approved" and item.review_status == "approved"
                )
            if status == "Rejected":
                return bool(
                    item.status == "generated" and item.review_status == "rejected"
                )
            if status == "Failed":
                return bool(item.status == "failed")
            if status == "Failed: audio limit":
                return bool(
                    item.status == "failed"
                    and item.failure_category == "audio limit / missed EOS"
                )
            if status == "Failed: silence":
                return bool(
                    item.status == "failed"
                    and item.failure_category == "speech silence"
                )
            return True

        self._filtered_reviews = tuple(
            item for item in self._all_reviews if included(item)
        )
        self.review_table.blockSignals(True)
        try:
            self._populate_reviews(self._filtered_reviews)
            target = self._selected_review_queue_id
            row = self._row_for_queue_id(target)
            if row < 0:
                row = self._first_pending_row()
            if row < 0 and self.review_table.rowCount() > 0:
                row = 0
            if row >= 0:
                self.review_table.setCurrentCell(row, 0)
        finally:
            self.review_table.blockSignals(False)
        if not self._all_reviews:
            scope = (
                "Review complete: this workspace has no generated, approved, "
                "rejected or failed outcomes to display."
            )
        elif not self._filtered_reviews:
            scope = (
                f"No outcomes match the active review filters; "
                f"{len(self._all_reviews)} outcomes exist in this workspace."
            )
        else:
            scope = (
                f"Independent review scope: showing {len(self._filtered_reviews)} of "
                f"{len(self._all_reviews)} outcomes. Generation collection selection "
                "does not filter this list."
            )
        self.review_scope.setText(scope)
        self._update_review_actions(preserve_queue_id=True)

    def open_specialist_reviewer(self) -> None:
        if self._specialist_active or self._specialist_reviewer is not None:
            return
        self._start_specialist_task(
            self._cohort_bundle_builder,
            (self.workspace_directory,),
        )

    def _start_specialist_task(
        self, function: CohortBundleBuilder, *arguments: Sequence[Path]
    ) -> None:
        self._specialist_active = True
        self.specialist_review_status.setText(
            "Building a checksum-bound bundle from the current workspace..."
        )
        self._update_specialist_action()
        self._specialist_runner.start(function, *arguments)

    def _specialist_task_finished(
        self, result: object, error: Exception | None
    ) -> None:
        if not self._specialist_active:
            return
        self._specialist_active = False
        if error is not None:
            self.specialist_review_status.setText(
                f"Specialist review is blocked: {error}. Select Open to retry."
            )
            self._update_specialist_action()
            return
        if not isinstance(result, CohortReviewBundle):
            self.specialist_review_status.setText(
                "Specialist review is blocked: bundle builder returned no exact bundle."
            )
            self._update_specialist_action()
            return
        try:
            dialog = self._specialist_reviewer_factory(result, self)
        except Exception as dialog_error:
            self.specialist_review_status.setText(
                f"Specialist review is blocked: {dialog_error}. Select Open to retry."
            )
            self._update_specialist_action()
            return
        self._specialist_reviewer = dialog
        dialog.setModal(True)
        dialog.finished.connect(
            lambda _result, current=dialog: self._specialist_review_finished(current)
        )
        self.specialist_review_status.setText(
            f"Opened specialist bundle {result.bundle_id[:12]}; "
            "the workbench will refresh after it closes."
        )
        dialog.open()
        self._update_specialist_action()

    def _specialist_review_finished(self, dialog: QDialog) -> None:
        if dialog is not self._specialist_reviewer:
            return
        self._specialist_reviewer = None
        self.specialist_review_status.setText(
            "Specialist reviewer closed. Refreshing workspace outcomes..."
        )
        self._update_specialist_action()
        self.refresh()

    def _effective_review_voice(self, item: ReviewItem) -> str:
        effective = item.voice_character
        if effective.casefold() == "narrator" and self._workspace is not None:
            return str(self._workspace["narrator_character"])
        return str(effective)

    def _update_specialist_action(self) -> None:
        self.specialist_review.setEnabled(
            not self._specialist_active
            and self._specialist_reviewer is None
            and not self._review_save_active
            and not self._projection_active
            and not self._playback_prepare_active
        )

    def _discard_review_playback_copy(self) -> None:
        self.player.stop()
        self._review_evidence.cancel()
        playback = self._review_playback_buffer
        self._review_playback_buffer = None
        release_audio_buffer(self.player, playback)

    def _row_for_queue_id(self, queue_id: str | None) -> int:
        if queue_id is None:
            return -1
        for row in range(self.review_table.rowCount()):
            item = self.review_table.item(row, 0)
            review = item.data(256) if item is not None else None
            if isinstance(review, ReviewItem) and review.queue_id == queue_id:
                return row
        return -1

    def _first_pending_row(self) -> int:
        for row in range(self.review_table.rowCount()):
            item = self.review_table.item(row, 0)
            review = item.data(256) if item is not None else None
            if (
                isinstance(review, ReviewItem)
                and review.status == "generated"
                and review.review_status in {None, "pending_review"}
            ):
                return row
        return -1

    def _move_pending(self, offset: int) -> None:
        pending = [
            row
            for row in range(self.review_table.rowCount())
            if (
                (
                    review := cast(
                        ReviewItem,
                        cast(QTableWidgetItem, self.review_table.item(row, 0)).data(
                            256
                        ),
                    )
                ).status
                == "generated"
                and review.review_status in {None, "pending_review"}
            )
        ]
        if not pending:
            self.review_action_reason.setText(
                "Navigation unavailable: no awaiting-review item matches the active filter"
            )
            return
        current = self.review_table.currentRow()
        try:
            position = pending.index(current)
        except ValueError:
            position = -1 if offset > 0 else 0
        row = pending[(position + int(offset)) % len(pending)]
        self.review_table.setCurrentCell(row, 0)
        item = self.review_table.item(row, 0)
        if item is not None:
            self.review_table.scrollToItem(item)

    def _next_pending_queue_id(self) -> str | None:
        pending = [
            item.queue_id
            for item in self._filtered_reviews
            if item.status == "generated"
            and item.review_status in {None, "pending_review"}
        ]
        if not pending:
            return None
        selected = self._selected_review_item()
        if selected is None or selected.queue_id not in pending:
            return str(pending[0])
        if len(pending) == 1:
            return None
        return str(pending[(pending.index(selected.queue_id) + 1) % len(pending)])

    def _selected_review_item(self) -> ReviewItem | None:
        row = self.review_table.currentRow()
        if row < 0:
            return None
        item = self.review_table.item(row, 0)
        value = item.data(256) if item is not None else None
        return value if isinstance(value, ReviewItem) else None

    def _update_review_actions(
        self, *_arguments: object, preserve_queue_id: bool = False
    ) -> None:
        selected = self._selected_review_item()
        if selected is not None and not preserve_queue_id:
            self._selected_review_queue_id = selected.queue_id
        selected_identity = (
            None
            if selected is None
            else (
                selected.queue_id,
                selected.status,
                selected.review_status,
                selected.audio,
                selected.authority,
            )
        )
        if selected_identity != self._selected_review_identity:
            if self._playback_prepare_active:
                self._playback_runner.cancel()
                self._playback_prepare_active = False
            self._discard_review_playback_copy()
            self._preview_active = False
            self.media_outcome = None
            self._selected_review_identity = selected_identity
        running = self.summary is not None and self.summary.runtime_status in {
            AuthoringRuntimeStatus.RUNNING_HERE,
            AuthoringRuntimeStatus.RUNNING_EXTERNAL,
            AuthoringRuntimeStatus.BLOCKED,
        }
        enabled = (
            selected is not None
            and selected.status
            in {
                "generated",
                "approved",
            }
            and not running
            and not self._review_save_active
            and not self._projection_active
            and not self._playback_prepare_active
            and selected.authority is not None
        )
        heard = self._review_evidence.allows(selected)
        self.approve.setEnabled(enabled and heard)
        self.reject_button.setEnabled(enabled and heard)
        self.review_play.setEnabled(
            enabled and selected is not None and selected.audio is not None
        )
        self.review_stop.setEnabled(self._preview_active)
        navigation_enabled = (
            self._first_pending_row() >= 0
            and not self._review_save_active
            and not self._projection_active
            and not self._playback_prepare_active
        )
        self.previous_pending.setEnabled(navigation_enabled)
        self.next_pending.setEnabled(navigation_enabled)
        if self._integrity_error is not None:
            reason = f"Review disabled: integrity error: {self._integrity_error}"
        elif self._review_save_active:
            reason = (
                "Saving review: revalidating the exact WAV, authoritative state, "
                "and generation lease"
            )
        elif self._projection_active:
            reason = "Review disabled: authoritative workspace refresh is active"
        elif self._playback_prepare_active:
            reason = "Preparing replay: validating and copying exact WAV bytes"
        elif selected is None:
            if not self._all_reviews:
                reason = "Review complete: no review outcomes exist in this workspace"
            elif not self._filtered_reviews:
                reason = "Review disabled: no outcomes match the active filters"
            else:
                reason = "Review disabled: select a generated or approved outcome"
        elif running:
            reason = "Review disabled: another generation process owns the state lease"
        elif selected.status not in {"generated", "approved"}:
            reason = f"Review disabled: {selected.status} has no reviewable WAV"
        elif selected.authority is None:
            reason = "Review disabled: exact state and WAV authority is unavailable"
        elif selected.audio is None:
            reason = "Playback disabled: no state-validated generated WAV is available"
        elif not heard:
            reason = "Review disabled: play this exact WAV through to the end first"
        else:
            reason = (
                "Ready: exact WAV and state will be revalidated when the action starts"
            )
        self.approve.setToolTip(reason)
        self.reject_button.setToolTip(reason)
        self.review_play.setToolTip("" if self.review_play.isEnabled() else reason)
        self.review_stop.setToolTip(
            "" if self._preview_active else "No audio preview is currently playing"
        )
        self.reload_authority.setEnabled(
            not self._review_save_active
            and not self._projection_active
            and not self._playback_prepare_active
        )
        self.review_action_reason.setText(reason)
        if selected is None:
            self.current_review.setText("Current review: none")
        else:
            self.current_review.setText(
                f"Current review: {selected.line_id} | source speaker {selected.speaker} | "
                f"effective voice {self._effective_review_voice(selected)} | "
                f"status {selected.review_status or selected.status} | "
                f"attempts {selected.attempts} | "
                f"{review_technical_summary(selected)}"
            )

    def play_selected_outcome(self) -> None:
        if self._playback_prepare_active:
            self.review_action_reason.setText(
                "Preparing replay: wait for exact WAV validation"
            )
            return
        selected = self._selected_review_item()
        if selected is None:
            self.status.setText("Select one generated outcome to play")
            return
        if selected.authority is None:
            self._fail_closed("Selected review row has no exact authority snapshot")
            return
        self._playback_prepare_active = True
        self._update_review_actions(preserve_queue_id=True)
        self._playback_runner.start(
            self._playback_preparer, self.workspace_directory, selected
        )

    def _playback_preparation_finished(
        self, result: object, error: Exception | None
    ) -> None:
        if not self._playback_prepare_active:
            return
        self._playback_prepare_active = False
        if error is not None:
            self._fail_closed(f"Generated audio replay blocked: {error}")
            return
        if not isinstance(result, tuple) or len(result) != 2:
            self._fail_closed("Replay worker returned no validated audio")
            return
        current, audio_bytes = result
        selected = self._selected_review_item()
        if (
            selected is None
            or selected.authority is None
            or current.queue_id != selected.queue_id
            or current.authority != selected.authority
        ):
            self._fail_closed(
                "Review selection changed while replay was being prepared"
            )
            return
        if hashlib.sha256(audio_bytes).hexdigest() != selected.authority.audio_sha256:
            self._fail_closed("Replay worker returned bytes with the wrong digest")
            return
        self._discard_review_playback_copy()
        playback = play_audio_bytes(self.player, self, audio_bytes, "vntts-review.wav")
        if playback is None:
            self._fail_closed(
                "Unable to open immutable generated-audio playback buffer"
            )
            return
        self._review_playback_buffer = playback
        self._review_evidence.begin(current)
        self._preview_active = True
        self.media_outcome = f"PLAYING GENERATED REVIEW AUDIO: {current.line_id}"
        self.status.setText(self._status_text())
        self._update_review_actions(preserve_queue_id=True)

    def review_selected(self, decision: str) -> None:
        if self._playback_prepare_active:
            self.review_action_reason.setText(
                "Preparing replay: wait before saving a review decision"
            )
            return
        if self._review_save_active:
            self.review_action_reason.setText(
                "Saving review: wait for the current authoritative decision"
            )
            return
        if decision not in {"approved", "rejected"}:
            self._fail_closed(f"Unsupported review decision: {decision!r}")
            return
        selected = self._selected_review_item()
        if selected is None:
            self.status.setText("Select one generated outcome to review")
            return
        if not self._review_evidence.allows(selected):
            self.review_action_reason.setText(
                "Review disabled: play this exact WAV through to the end first"
            )
            return
        self._discard_review_playback_copy()
        self._preview_active = False
        self.media_outcome = None
        self._review_save_active = True
        self._review_save_queue_id = selected.queue_id
        self._review_save_decision = decision
        self._review_advance_queue_id = self._next_pending_queue_id()
        self._update_review_actions(preserve_queue_id=True)
        self._review_runner.start(
            _save_review,
            self._reviewer,
            self.workspace_directory,
            selected.queue_id,
            decision,
            selected.authority,
            selected,
        )

    def _review_save_finished(self, result: object, error: Exception | None) -> None:
        if not self._review_save_active:
            return
        queue_id = self._review_save_queue_id
        decision = self._review_save_decision
        advance_queue_id = self._review_advance_queue_id
        self._review_save_active = False
        self._review_save_queue_id = None
        self._review_save_decision = None
        self._review_advance_queue_id = None
        if error is not None:
            self._fail_closed(f"Unable to save review: {error}")
            return
        if result is None or queue_id is None or decision is None:
            self._fail_closed("Review worker returned no authoritative result")
            return
        if isinstance(result, WorkspaceSummary):
            self.summary = result
            committed = None
        elif isinstance(result, ReviewCommit):
            committed = result
            if committed.queue_id != queue_id or committed.review_status != decision:
                self._fail_closed(
                    "Review worker returned a different queue identity or decision"
                )
                return
        else:
            self._fail_closed("Review worker returned an unsupported result")
            return
        updated = []
        found = False
        for item in self._all_reviews:
            authority = item.authority
            if committed is not None and authority is not None:
                authority = replace(
                    authority,
                    state_sha256=committed.authority.state_sha256,
                )
            if item.queue_id == queue_id:
                found = True
                authority = committed.authority if committed is not None else authority
                item = replace(
                    item,
                    status=(
                        committed.status
                        if committed is not None
                        else "approved"
                        if decision == "approved"
                        else "generated"
                    ),
                    review_status=decision,
                    authority=authority,
                )
            elif authority is not item.authority:
                item = replace(item, authority=authority)
            updated.append(item)
        if not found:
            self._fail_closed(
                "Reviewed queue identity disappeared before the durable save returned"
            )
            return
        self._all_reviews = tuple(updated)
        refresh_terminal_projection = False
        if committed is not None and self.summary is not None:
            generated = sum(
                item.status == "generated" and item.review_status == "pending_review"
                for item in self._all_reviews
            )
            approved = sum(
                item.status == "approved" and item.review_status == "approved"
                for item in self._all_reviews
            )
            rejected = sum(
                item.status == "generated" and item.review_status == "rejected"
                for item in self._all_reviews
            )
            selected_item = next(
                item for item in self._all_reviews if item.queue_id == queue_id
            )
            self.summary = replace(
                self.summary,
                generated=generated,
                approved=approved,
                rejected=rejected,
                latest_line=selected_item.line_id,
                latest_text=selected_item.text,
                latest_status=selected_item.status,
                latest_updated_at=committed.updated_at,
            )
            refresh_terminal_projection = generated == 0
        self._selected_review_queue_id = advance_queue_id
        self._apply_review_filters()
        self._show_counts()
        self._poll_signature = self._workspace_poll_signature()
        self.status.setText(self._status_text())
        self.review_action_reason.setText(f"Saved {decision} for {queue_id}")
        if refresh_terminal_projection:
            QTimer.singleShot(0, self.refresh)

    def start_generation(self) -> None:
        if (
            self.collection_selection is None
            or not self.collection_selection.readiness.queue_ids
        ):
            self.process_outcome = (
                "GENERATION CANCELLED: selected collections contain no ready queue IDs"
            )
            self.refresh()
            return
        self._start_child(self.collection_selection.readiness.queue_ids)

    def start_failed_retry(self) -> None:
        selected_queue_ids = (
            set(self.collection_selection.queue_ids)
            if self.collection_selection is not None
            else set()
        )
        try:
            failed = tuple(
                item.queue_id
                for item in list_review_items(self.workspace_directory)
                if item.status == "failed" and item.queue_id in selected_queue_ids
            )
        except AuthoringWorkbenchError as error:
            self.process_outcome = f"RETRY BLOCKED: {error}"
            self.refresh()
            return
        if not failed:
            self.process_outcome = (
                "RETRY CANCELLED: no failed queue IDs remain after refresh"
            )
            self.refresh()
            return
        self._start_child(failed)

    def _start_child(self, queue_ids: Sequence[str]) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.status.setText("Generation is already running in this window")
            return
        try:
            command = generation_command(
                self.workspace_directory,
                queue_ids=queue_ids,
            )
        except AuthoringWorkbenchError as error:
            self.status.setText(f"Unable to start generation: {error}")
            return
        self.process_log.clear()
        self._log_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.process_outcome = None
        self.process.setWorkingDirectory(str(self.workspace_directory))
        self.process.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
        self.process.setProgram(command[0])
        self.process.setArguments(list(command[1:]))
        self._process_generation += 1
        self._stop_generation_token = None
        self._stop_requested = False
        self._forced_kill = False
        self.process.start()

    def stop_child(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self.status.setText("STOPPING: asking generation process to terminate")
        token = self._process_generation
        self._stop_generation_token = token
        self._stop_requested = True
        self.process.terminate()
        QTimer.singleShot(self.stop_timeout_ms, lambda: self._kill_if_running(token))

    def _kill_if_running(self, token: int) -> None:
        if (
            token == self._process_generation
            and token == self._stop_generation_token
            and self.process.state() != QProcess.ProcessState.NotRunning
        ):
            self.status.setText(
                "STOPPING: generation did not exit; forcing termination"
            )
            self._forced_kill = True
            self.process.kill()

    def _process_started(self) -> None:
        self.local_process_started_at = process_started_at(self.process.processId())
        self.process_outcome = f"PROCESS STARTED: PID {self.process.processId()}"
        self.status.setText(self.process_outcome)
        self.generate.setEnabled(False)
        self.retry_failed.setEnabled(False)
        self.stop_generation.setEnabled(True)

    def _append_process_output(self, *, final: bool = False) -> None:
        data = bytes(self.process.readAllStandardOutput().data())
        text = self._log_decoder.decode(data, final=final)
        if text:
            self._append_process_log(text)

    def _append_process_log(self, text: str) -> None:
        self.process_log.moveCursor(QTextCursor.MoveOperation.End)
        self.process_log.insertPlainText(text)
        retained = self.process_log.toPlainText()
        if len(retained) <= PROCESS_LOG_CHARACTER_LIMIT:
            return
        tail_size = PROCESS_LOG_CHARACTER_LIMIT - len(PROCESS_LOG_TRUNCATION_MARKER)
        self.process_log.setPlainText(
            PROCESS_LOG_TRUNCATION_MARKER + retained[-tail_size:]
        )
        self.process_log.moveCursor(QTextCursor.MoveOperation.End)

    def _process_finished(self, exit_code: int, _exit_status: object) -> None:
        if self._finishing:
            return
        self._finishing = True
        stop_requested = self._stop_requested
        forced_kill = self._forced_kill
        self._stop_generation_token = None
        self._append_process_output(final=True)
        self.local_process_started_at = None
        if forced_kill:
            self.process_outcome = (
                "FORCIBLY STOPPED BY USER: authoritative partial state was reloaded"
            )
        elif stop_requested:
            self.process_outcome = (
                "STOPPED BY USER: authoritative partial state was reloaded"
            )
        elif exit_code == 0:
            self.process_outcome = "PROCESS EXITED 0: authoritative state was reloaded"
        else:
            self.process_outcome = (
                f"PROCESS EXITED {exit_code}: review diagnostics and preserved state"
            )
        self._stop_requested = False
        self._forced_kill = False
        self._finishing = False
        self.refresh()
        if self.close_after_stop:
            self.close_after_stop = False
            self.close()

    def _process_error(self, error: object) -> None:
        terminal = (
            self.process.state() == QProcess.ProcessState.NotRunning
            or error == QProcess.ProcessError.FailedToStart
        )
        if terminal:
            self._stop_generation_token = None
            self.local_process_started_at = None
        self.process_outcome = (
            f"PROCESS ERROR: {error}"
            if terminal
            else f"PROCESS I/O ERROR WHILE RUNNING: {error}"
        )
        self._append_process_log(self.process_outcome + "\n")
        self.refresh()

    def open_output_folder(self) -> None:
        try:
            current = inspect_workspace(self.workspace_directory)
        except AuthoringWorkbenchError as error:
            self.status.setText(f"BLOCKED: output folder changed: {error}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(current.output))):
            self.status.setText("Unable to open the contained output folder")

    def copy_diagnostic_text(self) -> None:
        summary = self.summary.to_dict() if self.summary is not None else {}
        QApplication.clipboard().setText(
            f"Workspace: {self.workspace_directory}\nStatus: {summary}\n\n{self.process_log.toPlainText()}"
        )

    def _technical_toggled(self, checked: bool) -> None:
        self.process_log.setVisible(checked)
        self.copy_diagnostics.setVisible(checked)

    def _show_technical_review_columns(self, checked: bool) -> None:
        self.review_table.setColumnHidden(6, not checked)
        self.review_table.setColumnHidden(8, not checked)

    def _inspector_section_toggled(
        self, section: DisclosureSection, checked: bool
    ) -> None:
        if not checked:
            return
        QTimer.singleShot(
            0,
            lambda: self.inspector_scroll.ensureWidgetVisible(
                section.first_control(), 0, 12
            ),
        )

    def _restore_settings(self) -> None:
        self.settings.beginGroup(self.settings_group)
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        sizes = self.settings.value("splitter")
        restored_sizes = (
            [int(value) for value in sizes] if isinstance(sizes, list) else []
        )
        if len(restored_sizes) == 2 and min(restored_sizes) >= 100:
            self.splitter.setSizes(restored_sizes)
        else:
            self.splitter.setSizes([560, 180])
        layout_version = cast(int, self.settings.value("layout-version", 0, type=int))
        generation_expanded = (
            cast(bool, self.settings.value("generation-expanded", False, type=bool))
            if layout_version >= 2
            else False
        )
        self.generation_section.setChecked(generation_expanded)
        expanded = cast(
            bool, self.settings.value("technical-expanded", False, type=bool)
        )
        self.technical.setChecked(expanded)
        self._technical_toggled(expanded)
        self.show_technical_columns.setChecked(
            cast(
                bool, self.settings.value("technical-review-columns", False, type=bool)
            )
        )
        readiness_expanded = cast(
            bool, self.settings.value("readiness-expanded", False, type=bool)
        )
        self.readiness_details.setChecked(readiness_expanded)
        self.readiness_text.setVisible(readiness_expanded)
        outcome_expanded = cast(
            bool, self.settings.value("outcome-details-expanded", False, type=bool)
        )
        self.outcome_details.setChecked(outcome_expanded)
        voice_expanded = cast(
            bool, self.settings.value("voice-expanded", False, type=bool)
        )
        self.voice_box.setChecked(voice_expanded)
        self.voice_content.setVisible(voice_expanded)
        self.review_status.setCurrentText(
            str(self.settings.value("review-status", "Awaiting review"))
        )
        self.review_search.setText(str(self.settings.value("review-search", "")))
        stored_character = str(
            self.settings.value("review-character", self.all_speakers_scope)
        )
        if self.settings.value("review-exclude-narrator", False, type=bool):
            stored_character = self.characters_scope
        elif stored_character in {"", "All characters", "All speakers"}:
            stored_character = self.all_speakers_scope
        elif stored_character in {"Narrator", "Narrator only"}:
            stored_character = self.narrator_scope
        self._stored_review_character_scope = stored_character
        self._stored_review_collection = str(
            self.settings.value("review-collection", "All collections")
        )
        self.settings.endGroup()

    def _save_settings(self) -> None:
        self.settings.beginGroup(self.settings_group)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.sizes())
        self.settings.setValue(
            "generation-expanded", self.generation_section.isChecked()
        )
        self.settings.setValue("layout-version", 2)
        self.settings.setValue("technical-expanded", self.technical.isChecked())
        self.settings.setValue(
            "technical-review-columns", self.show_technical_columns.isChecked()
        )
        self.settings.setValue("readiness-expanded", self.readiness_details.isChecked())
        self.settings.setValue(
            "outcome-details-expanded", self.outcome_details.isChecked()
        )
        self.settings.setValue("voice-expanded", self.voice_box.isChecked())
        self.settings.setValue("review-status", self.review_status.currentText())
        self.settings.setValue("review-search", self.review_search.text())
        self.settings.remove("review-exclude-narrator")
        self.settings.setValue(
            "review-character",
            self.review_character.currentData() or self.all_speakers_scope,
        )
        self.settings.setValue(
            "review-collection", self.review_collection.currentText()
        )
        self.settings.endGroup()
        self.settings.sync()

    def _reset_layout(self) -> None:
        self.splitter.setSizes([560, 180])
        self.generation_section.setChecked(False)
        self.outcome_details.setChecked(False)
        self.technical.setChecked(False)
        self.readiness_details.setChecked(False)
        self.voice_box.setChecked(False)
        self.show_technical_columns.setChecked(False)
        self.inspector_scroll.verticalScrollBar().setValue(0)
        self.settings.beginGroup(self.settings_group)
        self.settings.remove("splitter")
        self.settings.remove("generation-expanded")
        self.settings.setValue("layout-version", 2)
        self.settings.remove("technical-expanded")
        self.settings.remove("technical-review-columns")
        self.settings.remove("readiness-expanded")
        self.settings.remove("outcome-details-expanded")
        self.settings.remove("voice-expanded")
        self.settings.endGroup()
        self.settings.sync()

    def _install_review_shortcuts(self) -> None:
        bindings = (
            ("Ctrl+Shift+Left", lambda: self._move_pending(-1)),
            ("Ctrl+Shift+Right", lambda: self._move_pending(1)),
            (
                "Ctrl+R",
                lambda: self._trigger_if_enabled(
                    self.review_play, self.play_selected_outcome
                ),
            ),
            (
                "Ctrl+Return",
                lambda: self._trigger_if_enabled(
                    self.approve, lambda: self.review_selected("approved")
                ),
            ),
            (
                "Ctrl+Enter",
                lambda: self._trigger_if_enabled(
                    self.approve, lambda: self.review_selected("approved")
                ),
            ),
            (
                "Ctrl+Backspace",
                lambda: self._trigger_if_enabled(
                    self.reject_button, lambda: self.review_selected("rejected")
                ),
            ),
        )
        for sequence, callback in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
            self._review_shortcuts.append(shortcut)

    @staticmethod
    def _trigger_if_enabled(button: QPushButton, callback: Callable[[], None]) -> None:
        if button.isEnabled():
            callback()

    def _set_focus_chain(self) -> None:
        widgets = (
            self.review_character,
            self.review_status,
            self.review_collection,
            self.review_search,
            self.review_table,
            self.previous_pending,
            self.next_pending,
            self.review_play,
            self.review_stop,
            self.approve,
            self.reject_button,
            self.specialist_section.header,
            self.specialist_review,
            self.outcome_details.header,
            self.generation_section.header,
            self.collection_tree,
            self.retry_failed,
            self.generate,
            self.stop_generation,
            self.open_output,
            self.readiness_details.header,
            self.voice_box.header,
            self.recent_choice,
            self.voice_search,
            self.voice_character,
            self.reference_previous,
            self.reference_play,
            self.reference_stop,
            self.reference_next,
            self.technical.header,
            self.show_technical_columns,
            self.reload_authority,
            self.reset_layout,
            self.copy_diagnostics,
        )
        for first, second in pairwise(widgets):
            QWidget.setTabOrder(first, second)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._specialist_active:
            self.specialist_review_status.setText(
                "Close deferred: wait for the checksum-bound specialist bundle to finish"
            )
            event.ignore()
            return
        if self._review_save_active:
            self.review_action_reason.setText(
                "Close deferred: wait for the authoritative review save to finish"
            )
            event.ignore()
            return
        if self._projection_active:
            self.status.setText(
                "Close deferred: wait for authoritative workspace loading to finish"
            )
            event.ignore()
            return
        if self._playback_prepare_active:
            self.status.setText(
                "Close deferred: wait for exact replay preparation to finish"
            )
            event.ignore()
            return
        self._save_settings()
        self._discard_review_playback_copy()
        if self.process.state() == QProcess.ProcessState.NotRunning:
            event.accept()
            return
        choice = QMessageBox.question(
            self,
            "Generation is still running",
            "Stop generation and close the workbench?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self.close_after_stop = True
        self.stop_child()
        event.ignore()


def launch_authoring_workbench(workspace_directory: str | Path) -> int:
    application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = AuthoringWorkbenchDialog(workspace_directory)
    except Exception as error:
        QMessageBox.critical(None, "Unable to open authoring workbench", str(error))
        return 1
    dialog.show()
    return application.exec()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(value in {"-h", "--help"} for value in arguments):
        print("usage: vntts-authoring-workbench WORKSPACE")
        return 0
    if len(arguments) != 1:
        print("usage: vntts-authoring-workbench WORKSPACE", file=sys.stderr)
        return 2
    return launch_authoring_workbench(arguments[0])


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AuthoringWorkbenchDialog",
    "VoiceReference",
    "VoiceReferenceController",
    "launch_authoring_workbench",
    "main",
]

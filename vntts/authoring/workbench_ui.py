"""Accessible Qt workbench for safe offline authoring workspaces."""

from __future__ import annotations

import codecs
import hashlib
import json
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QProcess,
    QProcessEnvironment,
    QRunnable,
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
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
from vntts.authoring.bulk_generation import ReviewCommit
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
    ReviewItem,
    WorkspaceCollection,
    WorkspaceSummary,
    generation_command,
    immutable_history_timestamps,
    inspect_collection_selection,
    inspect_workspace,
    list_review_items,
    list_workspace_collections,
    load_workspace_authority,
    prepare_review_audio,
    review_selected_item,
    review_technical_summary,
    workspace_voice_snapshot,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class AnnouncementLabel(QLabel):
    """Visible status text that emits a native screen-reader announcement."""

    def __init__(self, *arguments, assertive=False, **keywords):
        super().__init__(*arguments, **keywords)
        self._announcement_politeness = (
            QAccessible.AnnouncementPoliteness.Assertive
            if assertive
            else QAccessible.AnnouncementPoliteness.Polite
        )

    def setText(self, text):
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

    def __init__(self, title, parent=None):
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

    def isChecked(self):
        return self.header.isChecked()

    def setChecked(self, checked):
        checked = bool(checked)
        if self.header.isChecked() == checked:
            self._set_expanded(checked, emit=False)
        else:
            self.header.setChecked(checked)

    def first_control(self):
        for index in range(self.content_layout.count()):
            item = self.content_layout.itemAt(index)
            widget = item.widget()
            if widget is not None:
                return widget
            child_layout = item.layout()
            if child_layout is not None:
                for child_index in range(child_layout.count()):
                    child = child_layout.itemAt(child_index).widget()
                    if child is not None:
                        return child
        return self.header

    def _set_expanded(self, checked, *, emit=True):
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

    def __init__(self, manifest_path):
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
    def from_workspace(cls, workspace_directory, manifest_path):
        instance = cls.__new__(cls)
        instance.manifest_path = Path(manifest_path).expanduser().resolve()
        voices = tuple(
            CharacterVoice(
                character=value.character,
                speaker=value.speaker,
                aliases=value.aliases,
                references=value.references,
            )
            for value in workspace_voice_snapshot(workspace_directory)
        )
        instance.registry = CharacterVoiceRegistry(voices)
        instance._characters = tuple(
            sorted(voices, key=lambda voice: voice.character.casefold())
        )
        instance._indexes = {voice.character: 0 for voice in voices}
        return instance

    def characters(self, search=""):
        needle = str(search).strip().casefold()
        return tuple(
            voice.character
            for voice in self._characters
            if not needle
            or needle in voice.character.casefold()
            or any(needle in alias.casefold() for alias in voice.aliases)
        )

    def references(self, character):
        voice = self.registry.resolve(character)
        if voice is None:
            raise AuthoringWorkbenchError(f"Unknown voice character: {character!r}")
        return voice.references

    def current(self, character):
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

    def move(self, character, offset):
        references = self.references(character)
        if not references:
            return None
        current = self._indexes.get(character, 0)
        self._indexes[character] = (current + int(offset)) % len(references)
        return self.current(character)

    def select(self, character, index):
        references = self.references(character)
        index = int(index)
        if index < 0 or index >= len(references):
            raise AuthoringWorkbenchError(
                f"Reference index is unavailable for {character!r}: {index}"
            )
        self._indexes[character] = index
        return self.current(character)


@dataclass(frozen=True)
class _WorkbenchProjection:
    summary: WorkspaceSummary
    reviews: tuple[ReviewItem, ...]
    workspace: dict
    collections: tuple[WorkspaceCollection, ...]
    collection_selection: object
    history: tuple
    voice_controller: VoiceReferenceController | None
    poll_signature: tuple


def _poll_signature(paths):
    values = []
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
    workspace_directory,
    selected_collection_ids,
    local_process_id,
    local_process_started_at,
    poll_paths,
):
    before = _poll_signature(poll_paths)
    summary = inspect_workspace(
        workspace_directory,
        local_process_id=local_process_id,
        local_process_started_at=local_process_started_at,
    )
    reviews = tuple(list_review_items(workspace_directory))
    _directory, workspace, _workspace_sha256 = load_workspace_authority(
        workspace_directory
    )
    collections = tuple(list_workspace_collections(workspace_directory))
    declared = tuple(value.collection_id for value in collections)
    if selected_collection_ids is None:
        selected = declared
    else:
        requested = set(selected_collection_ids)
        selected = tuple(value for value in declared if value in requested)
    collection_selection = inspect_collection_selection(
        workspace_directory,
        collection_ids=selected,
    )
    history = tuple(immutable_history_timestamps(workspace_directory))
    voice_controller = (
        None
        if summary.voice_manifest is None
        else VoiceReferenceController.from_workspace(
            workspace_directory, summary.voice_manifest
        )
    )
    after = _poll_signature(poll_paths)
    if before != after:
        raise AuthoringWorkbenchError(
            "Workspace authority changed while the workbench projection was loading"
        )
    return _WorkbenchProjection(
        summary=summary,
        reviews=reviews,
        workspace=workspace,
        collections=collections,
        collection_selection=collection_selection,
        history=history,
        voice_controller=voice_controller,
        poll_signature=after,
    )


class _ProjectionTaskSignals(QObject):
    finished = Signal(int, object, object)


class _ProjectionTask(QRunnable):
    def __init__(self, serial, loader, arguments, signals):
        super().__init__()
        self.serial = serial
        self.loader = loader
        self.arguments = arguments
        self.signals = signals

    def run(self):
        try:
            result = self.loader(*self.arguments)
        except Exception as error:
            self.signals.finished.emit(self.serial, None, error)
        else:
            self.signals.finished.emit(self.serial, result, None)


def _prepare_review_playback(workspace_directory, selected):
    del workspace_directory
    return selected, prepare_review_audio(selected)


class _PlaybackTaskSignals(QObject):
    finished = Signal(int, object, object)


class _PlaybackTask(QRunnable):
    def __init__(self, serial, preparer, workspace, selected, signals):
        super().__init__()
        self.serial = serial
        self.preparer = preparer
        self.workspace = workspace
        self.selected = selected
        self.signals = signals

    def run(self):
        try:
            result = self.preparer(self.workspace, self.selected)
        except Exception as error:
            self.signals.finished.emit(self.serial, None, error)
        else:
            self.signals.finished.emit(self.serial, result, None)


class _ReviewTaskSignals(QObject):
    finished = Signal(int, object, object)


class _ReviewTask(QRunnable):
    def __init__(
        self,
        serial,
        reviewer,
        workspace,
        queue_id,
        decision,
        authority,
        selected,
        signals,
    ):
        super().__init__()
        self.serial = serial
        self.reviewer = reviewer
        self.workspace = workspace
        self.queue_id = queue_id
        self.decision = decision
        self.authority = authority
        self.selected = selected
        self.signals = signals

    def run(self):
        try:
            if self.reviewer is None:
                result = review_selected_item(self.selected, self.decision)
            else:
                result = self.reviewer(
                    self.workspace,
                    self.queue_id,
                    self.decision,
                    self.authority,
                )
        except Exception as error:
            self.signals.finished.emit(self.serial, None, error)
        else:
            self.signals.finished.emit(self.serial, result, None)


class AuthoringWorkbenchDialog(QDialog):
    """Thin Qt shell over the validated authoring workspace boundary."""

    settings_group = "authoring/workbench"

    def __init__(
        self,
        workspace_directory,
        parent=None,
        *,
        settings=None,
        process=None,
        stop_timeout_ms=5_000,
        clock=None,
        reviewer=None,
        review_thread_pool=None,
        projection_loader=None,
        projection_thread_pool=None,
        playback_preparer=None,
        synchronous_projection=False,
        cohort_bundle_builder=None,
        specialist_reviewer_factory=None,
    ):
        super().__init__(parent)
        self.workspace_directory = Path(workspace_directory).expanduser().resolve()
        self.settings = settings or QSettings()
        self.process = process or QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.stop_timeout_ms = int(stop_timeout_ms)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.summary = self.collection_selection = None
        self.voice_controller = self.active_started_at = None
        self.close_after_stop = False
        self._finishing = False
        self._process_generation = 0
        self._stop_generation_token = self.local_process_started_at = None
        self.process_outcome = self.media_outcome = None
        self._current_reference_key = self._selected_review_identity = None
        self._preview_active = False
        self._review_playback_buffer = None
        self._review_evidence = ReviewPlaybackEvidence()
        self._playback_prepare_active = False
        self._playback_prepare_serial = 0
        self._playback_preparer = playback_preparer or _prepare_review_playback
        self._playback_signals = _PlaybackTaskSignals()
        self._playback_signals.finished.connect(self._playback_preparation_finished)
        self._playback_thread_pool = QThreadPool.globalInstance()
        self._selected_collection_ids = self._recent_reference_choices = None
        self._collection_selection_version = 0
        self._loading_collections = self._selection_refresh_pending = False
        self._loading_recent_choices = False
        self._stop_requested = self._forced_kill = False
        self._log_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._poll_paths = self._default_poll_paths()
        self._poll_signature = self._workspace = None
        self._all_reviews = self._filtered_reviews = ()
        self._selected_review_queue_id = self._integrity_error = None
        self._history = ()
        self._projection_active = self._projection_pending = False
        self._projection_serial = 0
        self._projection_selection_version = 0
        self._projection_loader = projection_loader or _load_workbench_projection
        self._synchronous_projection = bool(synchronous_projection)
        self._projection_signals = _ProjectionTaskSignals()
        self._projection_signals.finished.connect(self._projection_finished)
        self._projection_thread_pool = (
            projection_thread_pool or QThreadPool.globalInstance()
        )
        self._review_save_active = False
        self._review_save_serial = 0
        self._review_save_queue_id = self._review_save_decision = None
        self._review_advance_queue_id = self._specialist_reviewer = None
        self._reviewer = reviewer
        self._review_signals = _ReviewTaskSignals(self)
        self._review_signals.finished.connect(self._review_save_finished)
        self._review_thread_pool = review_thread_pool or QThreadPool(self)
        self._review_thread_pool.setMaxThreadCount(1)
        self._review_shortcuts = []
        self._specialist_active = False
        self._specialist_runner = LatestTaskRunner(self)
        self._specialist_runner.finished.connect(self._specialist_task_finished)
        self._cohort_bundle_builder = (
            cohort_bundle_builder or build_cohort_review_bundle
        )
        self._specialist_reviewer_factory = (
            specialist_reviewer_factory or CohortReviewBundleDialog
        )

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
        self.recent_choice.lineEdit().setPlaceholderText(
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

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.errorOccurred.connect(self._media_error)
        self.player.mediaStatusChanged.connect(self._media_status_changed)

        voice_header = QHBoxLayout()
        voice_header.addWidget(self.voice_search)
        voice_header.addWidget(self.voice_character, 1)
        recent_header = QHBoxLayout()
        recent_header.addWidget(QLabel("Recent previews"))
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

        self.review_character = QComboBox()
        self.review_character.setAccessibleName("Filter review by character")
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
        self.review_collection = QComboBox()
        self.review_collection.setAccessibleName("Filter review by collection")
        self.review_search = QLineEdit()
        self.review_search.setPlaceholderText("Search line text")
        self.review_search.setAccessibleName("Filter review by line text")
        self.narrator_only = QPushButton("Narrator only")
        self.exclude_narrator = QPushButton("Characters only")
        self.exclude_narrator.setCheckable(True)
        self._accessible_button(
            self.narrator_only,
            "Show only Narrator review items",
            "Set the review voice filter to source Narrator lines",
        )
        self._accessible_button(
            self.exclude_narrator,
            "Show character review items only",
            "Hide narrator outcomes without changing generation scope",
        )
        review_filters = QHBoxLayout()
        for widget in (
            self.review_character,
            self.review_status,
            self.review_collection,
            self.review_search,
            self.narrator_only,
            self.exclude_narrator,
        ):
            review_filters.addWidget(widget)
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
        self.previous_pending = QPushButton("Previous pending")
        self.next_pending = QPushButton("Next pending")
        self.approve = QPushButton("Approve (Ctrl+Enter)")
        self.reject = QPushButton("Reject (Ctrl+Backspace)")
        self.review_play = QPushButton("Replay (Ctrl+R)")
        self.review_stop = QPushButton("Stop selected audio")
        self.reload_authority = QPushButton("Refresh authority")
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
                "Make this generated line eligible for a later final game pack",
            ),
            (
                self.reject,
                "Reject selected audio",
                "Keep but unpublish this generated line",
            ),
            (
                self.review_play,
                "Play selected generated audio",
                "Play the exact validated generated WAV selected for review",
            ),
            (
                self.review_stop,
                "Stop selected generated audio",
                "Stop generated-audio or voice-reference preview playback",
            ),
            (
                self.reload_authority,
                "Retry authoritative workspace load",
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
            self.reject,
            self.reload_authority,
        )
        for widget in review_buttons:
            widget.setMinimumWidth(widget.sizeHint().width())
        for column, widget in enumerate(review_buttons[:4]):
            review_actions.addWidget(widget, 0, column)
        review_actions.addWidget(self.approve, 1, 0)
        review_actions.addWidget(self.reject, 1, 1)
        review_actions.addWidget(self.reload_authority, 1, 2, 1, 2)
        generation_actions = QHBoxLayout()
        for widget in (
            self.retry_failed,
            self.generate,
            self.stop_generation,
            self.open_output,
        ):
            generation_actions.addWidget(widget)

        self.technical = DisclosureSection("Technical details")
        self.technical.setAccessibleName("Technical process details")
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
        technical_layout.addWidget(self.process_log)
        technical_layout.addWidget(self.copy_diagnostics)

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
        secondary_layout.addWidget(self.reset_layout)
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

        self.voice_search.textChanged.connect(self._populate_voice_choices)
        self.voice_character.currentTextChanged.connect(self._show_reference)
        self.voice_character.activated.connect(self._record_current_reference)
        self.recent_choice.activated.connect(self._choose_recent_reference)
        self.recent_choice.lineEdit().returnPressed.connect(
            self._choose_typed_recent_reference
        )
        self.collection_tree.itemChanged.connect(self._collection_selection_changed)
        self.review_character.currentTextChanged.connect(self._apply_review_filters)
        self.review_status.currentTextChanged.connect(self._apply_review_filters)
        self.review_collection.currentTextChanged.connect(self._apply_review_filters)
        self.review_search.textChanged.connect(self._apply_review_filters)
        self.narrator_only.clicked.connect(self._show_narrator_reviews)
        self.exclude_narrator.toggled.connect(self._exclude_narrator_changed)
        self.reference_previous.clicked.connect(lambda: self._move_reference(-1))
        self.reference_next.clicked.connect(lambda: self._move_reference(1))
        self.reference_play.clicked.connect(self.play_reference)
        self.reference_stop.clicked.connect(self.stop_preview)
        self.review_play.clicked.connect(self.play_selected_outcome)
        self.review_stop.clicked.connect(self.stop_preview)
        self.specialist_review.clicked.connect(self.open_specialist_reviewer)
        self.reload_authority.clicked.connect(self.refresh)
        self.approve.clicked.connect(lambda: self.review_selected("approved"))
        self.reject.clicked.connect(lambda: self.review_selected("rejected"))
        self.previous_pending.clicked.connect(lambda: self._move_pending(-1))
        self.next_pending.clicked.connect(lambda: self._move_pending(1))
        self.retry_failed.clicked.connect(self.start_failed_retry)
        self.generate.clicked.connect(self.start_generation)
        self.stop_generation.clicked.connect(self.stop_child)
        self.open_output.clicked.connect(self.open_output_folder)
        self.reset_layout.clicked.connect(self._reset_layout)
        self.copy_diagnostics.clicked.connect(self.copy_diagnostic_text)
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
    def _accessible_button(button, name, description):
        button.setAccessibleName(name)
        button.setAccessibleDescription(description)

    def _restore_collection_selection(self):
        stored = self.settings.value(self._workspace_settings_key("collections"))
        if stored is None:
            return
        if isinstance(stored, str):
            stored = [stored]
        self._selected_collection_ids = tuple(str(value) for value in stored)

    def _projection_arguments(self):
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

    def refresh(self):
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
            self._projection_serial += 1
            serial = self._projection_serial
            self._projection_selection_version = self._collection_selection_version
            self.reload_authority.setEnabled(False)
            if self.summary is None:
                self.status.setText("LOADING: validating authoritative workspace")
            self._update_review_actions(preserve_queue_id=True)
            self._projection_thread_pool.start(
                _ProjectionTask(
                    serial,
                    self._projection_loader,
                    arguments,
                    self._projection_signals,
                )
            )
            return
        try:
            projection = self._projection_loader(*arguments)
        except Exception as error:
            self._fail_closed(error)
            return
        self._apply_projection(projection)

    def _projection_finished(self, serial, projection, error):
        if serial != self._projection_serial or not self._projection_active:
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

    def _default_poll_paths(self):
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

    def _workspace_poll_signature(self):
        return _poll_signature(self._poll_paths)

    def _poll_authoritative(self):
        if self._review_save_active or self._projection_active:
            return
        if self._poll_signature != self._workspace_poll_signature():
            self.refresh()

    def _fail_closed(self, error):
        self._projection_pending = False
        self._playback_prepare_serial += 1
        self._playback_prepare_active = False
        self._review_save_serial += 1
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
            self.reject,
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
        self.stop_generation.setEnabled(
            self.process.state() != QProcess.ProcessState.NotRunning
        )

    def _apply_projection(self, projection):
        self.summary = projection.summary
        reviews = projection.reviews
        self._workspace = projection.workspace
        self._history = projection.history
        self._poll_signature = projection.poll_signature
        self._selected_collection_ids = projection.collection_selection.collection_ids
        self.title.setText(self.summary.title)
        self._integrity_error = None
        workspace = projection.workspace
        run_config = workspace["run_config"]
        self.narrator.setText(
            f"Narrator: {workspace['narrator_character']} | Backend: {run_config['backend']} | "
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
        self._populate_recent_choices(workspace["narrator_character"])
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
        self.reload_authority.setText("Refresh authority")
        self.reload_authority.setToolTip("Reload authoritative workspace state")
        self._update_review_actions(preserve_queue_id=True)

    def _show_counts(self):
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

    def _status_text(self):
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
        if self.summary.runtime_status in {
            AuthoringRuntimeStatus.RUNNING_HERE,
            AuthoringRuntimeStatus.RUNNING_EXTERNAL,
            AuthoringRuntimeStatus.INTERRUPTED,
            AuthoringRuntimeStatus.BLOCKED,
        }:
            primary = labels[self.summary.runtime_status]
        else:
            primary = labels[self.summary.runtime_status]
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

    def _disabled_generation_reason(self):
        if self.collection_selection is None:
            return "Collection selection is unavailable"
        readiness = self.collection_selection.readiness
        if not self.collection_selection.collection_ids:
            return "Select at least one story collection"
        if readiness.blocked_reasons:
            return "; ".join(readiness.blocked_reasons)
        if readiness.ready == 0:
            return "No ready pending or failed lines exist in selected collections"
        if self.summary.blocked_reasons:
            return "; ".join(self.summary.blocked_reasons)
        if self.summary.runtime_status is AuthoringRuntimeStatus.NEEDS_REVIEW:
            return "Review generated audio before starting more work"
        if self.summary.runtime_status in {
            AuthoringRuntimeStatus.RUNNING_HERE,
            AuthoringRuntimeStatus.RUNNING_EXTERNAL,
        }:
            return "Generation is already running"
        return "No ready pending lines are available"

    def _show_readiness_details(self, workspace, history=None):
        story = workspace.get("story_index")
        voice = workspace.get("voice_manifest")
        history = self._history if history is None else tuple(history)
        readiness = self.collection_selection.readiness
        lines = [
            "Collections: "
            + (", ".join(self.collection_selection.collection_ids) or "none"),
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

    def _show_active(self):
        attempt = self.summary.active
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

    def update_elapsed(self):
        if self.active_started_at is None:
            return
        seconds = max(0, int((self.clock() - self.active_started_at).total_seconds()))
        base = self.active.text().split(" | elapsed ", 1)[0]
        self.active.setText(f"{base} | elapsed {seconds // 60}:{seconds % 60:02d}")

    @staticmethod
    def _parse_datetime(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _populate_collections(self, collections):
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

    def _collection_selection_changed(self, _item, _column):
        if self._loading_collections:
            return
        selected = []
        for index in range(self.collection_tree.topLevelItemCount()):
            item = self.collection_tree.topLevelItem(index)
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

    def _refresh_collection_selection(self):
        self._selection_refresh_pending = False
        self.refresh()

    def _workspace_settings_key(self, suffix):
        return (
            f"{self.settings_group}/workspaces/{self.workspace_directory.name}/{suffix}"
        )

    def _workspace_document(self):
        if self._workspace is not None:
            return self.workspace_directory, self._workspace
        directory, workspace, _workspace_sha256 = load_workspace_authority(
            self.workspace_directory
        )
        return directory, workspace

    def _load_voice_controller(self, controller):
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

    def _populate_voice_choices(self):
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

    def _populate_recent_choices(self, narrator_character):
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
                for encoded in stored:
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

    def _store_recent_choices(self, values):
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

    def _record_current_reference(self, *_arguments):
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
        self._populate_recent_choices(workspace["narrator_character"])

    def _choose_recent_reference(self, index):
        if self._loading_recent_choices:
            return
        value = self.recent_choice.itemData(index)
        if isinstance(value, (tuple, list)) and len(value) == 2:
            self._apply_recent_reference(str(value[0]), int(value[1]))

    def _choose_typed_recent_reference(self):
        typed = self.recent_choice.currentText().strip().casefold()
        for index in range(self.recent_choice.count()):
            if self.recent_choice.itemText(index).casefold() == typed:
                self._choose_recent_reference(index)
                return
        self.media_outcome = "RECENT PREVIEW UNAVAILABLE: choose a validated entry"
        if self.summary is not None:
            self.status.setText(self._status_text())

    def _apply_recent_reference(self, character, index):
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

    def _show_reference(self):
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

    def _move_reference(self, offset):
        if self.voice_controller is None or not self.voice_character.currentText():
            return
        self.voice_controller.move(self.voice_character.currentText(), offset)
        self._show_reference()
        self._record_current_reference()

    def play_reference(self):
        if self.voice_controller is None:
            return
        token = self.voice_controller.current(self.voice_character.currentText())
        if token is None:
            return
        try:
            current = inspect_workspace(self.workspace_directory)
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

    def _media_error(self, _error, message=""):
        self._discard_review_playback_copy()
        self._preview_active = False
        self.media_outcome = "AUDIO PREVIEW ERROR: " + (
            message or self.player.errorString()
        )
        if self.summary is not None:
            self.status.setText(self._status_text())
        self._update_review_actions(preserve_queue_id=True)

    def _media_status_changed(self, status):
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        self._review_evidence.complete()
        self._discard_review_playback_copy()
        self._preview_active = False
        self.media_outcome = "AUDIO PREVIEW FINISHED"
        if self.summary is not None:
            self.status.setText(self._status_text())
        self._update_review_actions(preserve_queue_id=True)

    def stop_preview(self):
        self._discard_review_playback_copy()
        self._preview_active = False
        self.media_outcome = "AUDIO PREVIEW STOPPED"
        if self.summary is not None:
            self.status.setText(self._status_text())
        self._update_review_actions(preserve_queue_id=True)

    def _populate_reviews(self, reviews):
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
            self.review_table.item(row, 0).setData(256, review)
        for column, width in enumerate((190, 120, 120, 110, 80, 120, 260)):
            self.review_table.setColumnWidth(column, width)

    def _populate_review_filter_choices(self):
        self._replace_combo_values(
            self.review_character,
            "All characters",
            sorted(
                {item.voice_character for item in self._all_reviews},
                key=str.casefold,
            ),
        )
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
        if hasattr(self, "_stored_review_character"):
            index = self.review_character.findText(self._stored_review_character)
            self.review_character.setCurrentIndex(index if index >= 0 else 0)
            del self._stored_review_character
        if hasattr(self, "_stored_review_collection"):
            index = self.review_collection.findText(self._stored_review_collection)
            self.review_collection.setCurrentIndex(index if index >= 0 else 0)
            del self._stored_review_collection

    @staticmethod
    def _replace_combo_values(combo, all_label, values):
        current = combo.currentText() or all_label
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(all_label)
        combo.addItems(values)
        index = combo.findText(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _apply_review_filters(self, *_arguments):
        if not hasattr(self, "review_character"):
            return
        character = self.review_character.currentText()
        status = self.review_status.currentText()
        collection = self.review_collection.currentText()
        needle = self.review_search.text().strip().casefold()
        exclude_narrator = self.exclude_narrator.isChecked()

        def included(item):
            if character not in {"", "All characters"} and (
                item.voice_character != character
            ):
                return False
            if exclude_narrator and item.voice_character.casefold() == "narrator":
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
                return item.status == "approved" and item.review_status == "approved"
            if status == "Rejected":
                return item.status == "generated" and item.review_status == "rejected"
            if status == "Failed":
                return item.status == "failed"
            if status == "Failed: audio limit":
                return (
                    item.status == "failed"
                    and item.failure_category == "audio limit / missed EOS"
                )
            if status == "Failed: silence":
                return (
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

    def open_specialist_reviewer(self):
        if self._specialist_active or self._specialist_reviewer is not None:
            return
        self._start_specialist_task(
            self._cohort_bundle_builder,
            (self.workspace_directory,),
        )

    def _start_specialist_task(self, function, *arguments):
        self._specialist_active = True
        self.specialist_review_status.setText(
            "Building a checksum-bound bundle from the current workspace..."
        )
        self._update_specialist_action()
        self._specialist_runner.start(function, *arguments)

    def _specialist_task_finished(self, result, error):
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

    def _specialist_review_finished(self, dialog):
        if dialog is not self._specialist_reviewer:
            return
        self._specialist_reviewer = None
        self.specialist_review_status.setText(
            "Specialist reviewer closed. Refreshing workspace outcomes..."
        )
        self._update_specialist_action()
        self.refresh()

    def _effective_review_voice(self, item):
        effective = item.voice_character
        if effective.casefold() == "narrator" and self._workspace is not None:
            return self._workspace["narrator_character"]
        return effective

    def _update_specialist_action(self):
        self.specialist_review.setEnabled(
            not self._specialist_active
            and self._specialist_reviewer is None
            and not self._review_save_active
            and not self._projection_active
            and not self._playback_prepare_active
        )

    def _discard_review_playback_copy(self):
        self.player.stop()
        self._review_evidence.cancel()
        playback = self._review_playback_buffer
        self._review_playback_buffer = None
        if playback is not None:
            self.player.setSource(QUrl())
            playback.close()
            playback.deleteLater()

    def _show_narrator_reviews(self):
        index = self.review_character.findText("Narrator")
        if index < 0:
            self.review_character.addItem("Narrator")
            index = self.review_character.findText("Narrator")
        self.exclude_narrator.setChecked(False)
        self.review_character.setCurrentIndex(index)

    def _exclude_narrator_changed(self, checked):
        if checked and self.review_character.currentText() == "Narrator":
            self.review_character.setCurrentText("All characters")
        self._apply_review_filters()

    def _row_for_queue_id(self, queue_id):
        if queue_id is None:
            return -1
        for row in range(self.review_table.rowCount()):
            item = self.review_table.item(row, 0)
            review = item.data(256) if item is not None else None
            if isinstance(review, ReviewItem) and review.queue_id == queue_id:
                return row
        return -1

    def _first_pending_row(self):
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

    def _move_pending(self, offset):
        pending = [
            row
            for row in range(self.review_table.rowCount())
            if (
                (review := self.review_table.item(row, 0).data(256)).status
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
        self.review_table.scrollToItem(self.review_table.item(row, 0))

    def _next_pending_queue_id(self):
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
            return pending[0]
        if len(pending) == 1:
            return None
        return pending[(pending.index(selected.queue_id) + 1) % len(pending)]

    def _selected_review_item(self):
        row = self.review_table.currentRow()
        if row < 0:
            return None
        item = self.review_table.item(row, 0)
        value = item.data(256) if item is not None else None
        return value if isinstance(value, ReviewItem) else None

    def _update_review_actions(self, *_arguments, preserve_queue_id=False):
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
                self._playback_prepare_serial += 1
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
        self.reject.setEnabled(enabled and heard)
        self.review_play.setEnabled(enabled and selected.audio is not None)
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
        self.reject.setToolTip(reason)
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

    def play_selected_outcome(self):
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
        self._playback_prepare_serial += 1
        serial = self._playback_prepare_serial
        self._update_review_actions(preserve_queue_id=True)
        self._playback_thread_pool.start(
            _PlaybackTask(
                serial,
                self._playback_preparer,
                self.workspace_directory,
                selected,
                self._playback_signals,
            )
        )

    def _playback_preparation_finished(self, serial, result, error):
        if serial != self._playback_prepare_serial or not self._playback_prepare_active:
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
        playback = QBuffer(self)
        playback.setData(QByteArray(audio_bytes))
        if not playback.open(QIODevice.OpenModeFlag.ReadOnly):
            playback.deleteLater()
            self._fail_closed(
                "Unable to open immutable generated-audio playback buffer"
            )
            return
        self._review_playback_buffer = playback
        self._review_evidence.begin(current)
        self.player.setSourceDevice(playback, QUrl("vntts-review.wav"))
        self.player.play()
        self._preview_active = True
        self.media_outcome = f"PLAYING GENERATED REVIEW AUDIO: {current.line_id}"
        self.status.setText(self._status_text())
        self._update_review_actions(preserve_queue_id=True)

    def review_selected(self, decision):
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
        self._review_save_serial += 1
        serial = self._review_save_serial
        self._review_save_queue_id = selected.queue_id
        self._review_save_decision = decision
        self._review_advance_queue_id = self._next_pending_queue_id()
        self._update_review_actions(preserve_queue_id=True)
        self._review_thread_pool.start(
            _ReviewTask(
                serial,
                self._reviewer,
                self.workspace_directory,
                selected.queue_id,
                decision,
                selected.authority,
                selected,
                self._review_signals,
            )
        )

    def _review_save_finished(self, serial, result, error):
        if serial != self._review_save_serial or not self._review_save_active:
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

    def start_generation(self):
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

    def start_failed_retry(self):
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

    def _start_child(self, queue_ids):
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

    def stop_child(self):
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self.status.setText("STOPPING: asking generation process to terminate")
        token = self._process_generation
        self._stop_generation_token = token
        self._stop_requested = True
        self.process.terminate()
        QTimer.singleShot(self.stop_timeout_ms, lambda: self._kill_if_running(token))

    def _kill_if_running(self, token):
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

    def _process_started(self):
        self.local_process_started_at = process_started_at(self.process.processId())
        self.process_outcome = f"PROCESS STARTED: PID {self.process.processId()}"
        self.status.setText(self.process_outcome)
        self.generate.setEnabled(False)
        self.retry_failed.setEnabled(False)
        self.stop_generation.setEnabled(True)

    def _append_process_output(self, *, final=False):
        data = bytes(self.process.readAllStandardOutput())
        text = self._log_decoder.decode(data, final=final)
        if text:
            self.process_log.moveCursor(QTextCursor.MoveOperation.End)
            self.process_log.insertPlainText(text)

    def _process_finished(self, exit_code, _exit_status):
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

    def _process_error(self, error):
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
        self.process_log.appendPlainText(self.process_outcome)
        self.refresh()

    def open_output_folder(self):
        try:
            current = inspect_workspace(self.workspace_directory)
        except AuthoringWorkbenchError as error:
            self.status.setText(f"BLOCKED: output folder changed: {error}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(current.output))):
            self.status.setText("Unable to open the contained output folder")

    def copy_diagnostic_text(self):
        summary = self.summary.to_dict() if self.summary is not None else {}
        QApplication.clipboard().setText(
            f"Workspace: {self.workspace_directory}\nStatus: {summary}\n\n{self.process_log.toPlainText()}"
        )

    def _technical_toggled(self, checked):
        self.process_log.setVisible(checked)
        self.copy_diagnostics.setVisible(checked)

    def _inspector_section_toggled(self, section, checked):
        if not checked:
            return
        QTimer.singleShot(
            0,
            lambda: self.inspector_scroll.ensureWidgetVisible(
                section.first_control(), 0, 12
            ),
        )

    def _restore_settings(self):
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
        layout_version = self.settings.value("layout-version", 0, type=int)
        generation_expanded = (
            self.settings.value("generation-expanded", False, type=bool)
            if layout_version >= 2
            else False
        )
        self.generation_section.setChecked(generation_expanded)
        expanded = self.settings.value("technical-expanded", False, type=bool)
        self.technical.setChecked(expanded)
        self._technical_toggled(expanded)
        readiness_expanded = self.settings.value("readiness-expanded", False, type=bool)
        self.readiness_details.setChecked(readiness_expanded)
        self.readiness_text.setVisible(readiness_expanded)
        outcome_expanded = self.settings.value(
            "outcome-details-expanded", False, type=bool
        )
        self.outcome_details.setChecked(outcome_expanded)
        voice_expanded = self.settings.value("voice-expanded", False, type=bool)
        self.voice_box.setChecked(voice_expanded)
        self.voice_content.setVisible(voice_expanded)
        self.review_status.setCurrentText(
            str(self.settings.value("review-status", "Awaiting review"))
        )
        self.review_search.setText(str(self.settings.value("review-search", "")))
        self.exclude_narrator.setChecked(
            self.settings.value("review-exclude-narrator", False, type=bool)
        )
        self._stored_review_character = str(
            self.settings.value("review-character", "All characters")
        )
        self._stored_review_collection = str(
            self.settings.value("review-collection", "All collections")
        )
        self.settings.endGroup()

    def _save_settings(self):
        self.settings.beginGroup(self.settings_group)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.sizes())
        self.settings.setValue(
            "generation-expanded", self.generation_section.isChecked()
        )
        self.settings.setValue("layout-version", 2)
        self.settings.setValue("technical-expanded", self.technical.isChecked())
        self.settings.setValue("readiness-expanded", self.readiness_details.isChecked())
        self.settings.setValue(
            "outcome-details-expanded", self.outcome_details.isChecked()
        )
        self.settings.setValue("voice-expanded", self.voice_box.isChecked())
        self.settings.setValue("review-status", self.review_status.currentText())
        self.settings.setValue("review-search", self.review_search.text())
        self.settings.setValue(
            "review-exclude-narrator", self.exclude_narrator.isChecked()
        )
        self.settings.setValue("review-character", self.review_character.currentText())
        self.settings.setValue(
            "review-collection", self.review_collection.currentText()
        )
        self.settings.endGroup()
        self.settings.sync()

    def _reset_layout(self):
        self.splitter.setSizes([560, 180])
        self.generation_section.setChecked(False)
        self.outcome_details.setChecked(False)
        self.technical.setChecked(False)
        self.readiness_details.setChecked(False)
        self.voice_box.setChecked(False)
        self.inspector_scroll.verticalScrollBar().setValue(0)
        self.settings.beginGroup(self.settings_group)
        self.settings.remove("splitter")
        self.settings.remove("generation-expanded")
        self.settings.setValue("layout-version", 2)
        self.settings.remove("technical-expanded")
        self.settings.remove("readiness-expanded")
        self.settings.remove("outcome-details-expanded")
        self.settings.remove("voice-expanded")
        self.settings.endGroup()
        self.settings.sync()

    def _install_review_shortcuts(self):
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
                    self.reject, lambda: self.review_selected("rejected")
                ),
            ),
        )
        for sequence, callback in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
            self._review_shortcuts.append(shortcut)

    @staticmethod
    def _trigger_if_enabled(button, callback):
        if button.isEnabled():
            callback()

    def _set_focus_chain(self):
        widgets = (
            self.review_character,
            self.review_status,
            self.review_collection,
            self.review_search,
            self.narrator_only,
            self.exclude_narrator,
            self.review_table,
            self.previous_pending,
            self.next_pending,
            self.review_play,
            self.review_stop,
            self.approve,
            self.reject,
            self.specialist_section.header,
            self.specialist_review,
            self.collection_tree,
            self.retry_failed,
            self.generate,
            self.stop_generation,
            self.open_output,
            self.reset_layout,
            self.outcome_details.header,
            self.generation_section.header,
            self.readiness_details.header,
            self.recent_choice,
            self.voice_search,
            self.voice_character,
            self.reference_previous,
            self.reference_play,
            self.reference_stop,
            self.reference_next,
            self.voice_box.header,
            self.technical.header,
            self.copy_diagnostics,
        )
        for first, second in zip(widgets, widgets[1:]):
            QWidget.setTabOrder(first, second)

    def closeEvent(self, event: QCloseEvent):
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


def launch_authoring_workbench(workspace_directory):
    application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = AuthoringWorkbenchDialog(workspace_directory)
    except Exception as error:
        QMessageBox.critical(None, "Unable to open authoring workbench", str(error))
        return 1
    dialog.show()
    return application.exec()


def main(argv=None):
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

"""Accessible Qt workbench for safe offline authoring workspaces."""

from __future__ import annotations

import codecs
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QTextCursor
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav

from vntts.authoring.bulk_generation import process_started_at
from vntts.authoring.workbench import (
    AuthoringRuntimeStatus,
    AuthoringWorkbenchError,
    ReviewItem,
    generation_command,
    immutable_history_timestamps,
    inspect_collection_selection,
    inspect_workspace,
    list_review_items,
    list_workspace_collections,
    review_workspace_item,
    workspace_voice_snapshot,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


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
        except (OSError, Pcm16MonoWavError):
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
    ):
        super().__init__(parent)
        self.workspace_directory = Path(workspace_directory).expanduser().resolve()
        self.settings = settings or QSettings()
        self.process = process or QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.stop_timeout_ms = int(stop_timeout_ms)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.summary = None
        self.collection_selection = None
        self.collection_selection = None
        self.voice_controller = None
        self.active_started_at = None
        self.close_after_stop = False
        self._finishing = False
        self._process_generation = 0
        self._stop_generation_token = None
        self.local_process_started_at = None
        self.process_outcome = None
        self.media_outcome = None
        self._current_reference_key = None
        self._selected_review_identity = None
        self._preview_active = False
        self._selected_collection_ids = None
        self._loading_collections = False
        self._selection_refresh_pending = False
        self._loading_recent_choices = False
        self._recent_reference_choices = None
        self._stop_requested = False
        self._forced_kill = False
        self._log_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._poll_paths = self._default_poll_paths()
        self._poll_signature = None

        self.setWindowTitle("VNTTS authoring workbench")
        self.resize(1_080, 720)
        self.title = QLabel()
        self.title.setAccessibleName("Selected authoring workspace")
        self.title.setWordWrap(True)
        self.narrator = QLabel()
        self.narrator.setAccessibleName("Configured narrator and synthesis model")
        self.narrator.setWordWrap(True)
        self.status = QLabel()
        self.status.setAccessibleName("Authoring runtime status")
        self.status.setWordWrap(True)
        self.counts = QLabel()
        self.counts.setAccessibleName("Authoring outcome counts")
        self.counts.setWordWrap(True)
        self.active = QLabel()
        self.active.setAccessibleName("Current generation attempt")
        self.active.setWordWrap(True)
        self.readiness_details = QGroupBox("Readiness details")
        self.readiness_details.setCheckable(True)
        self.readiness_details.setAccessibleName("Authoring readiness details")
        self.readiness_text = QLabel()
        self.readiness_text.setWordWrap(True)
        self.readiness_text.setAccessibleName(
            "Selected collections, immutable history and input paths"
        )
        readiness_layout = QVBoxLayout(self.readiness_details)
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
        voice_box = QGroupBox("Voice references")
        voice_box.setAccessibleName("Voice reference chooser")
        voice_layout = QVBoxLayout(voice_box)
        voice_layout.addLayout(recent_header)
        voice_layout.addLayout(voice_header)
        voice_layout.addWidget(self.reference_label)
        voice_layout.addLayout(voice_controls)

        self.review_table = QTableWidget(0, 6)
        self.review_table.setHorizontalHeaderLabels(
            ["Line", "Speaker", "Status", "Attempts", "Text", "Queue ID"]
        )
        self.review_table.setAccessibleName("Generated and failed line outcomes")
        self.review_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.review_table.currentCellChanged.connect(self._update_review_actions)
        self.approve = QPushButton("Approve selected")
        self.reject = QPushButton("Reject selected")
        self.review_play = QPushButton("Play selected audio")
        self.review_stop = QPushButton("Stop selected audio")
        self.retry_failed = QPushButton("Retry failed")
        self.generate = QPushButton("Generate ready lines")
        self.stop_generation = QPushButton("Stop generation")
        self.open_output = QPushButton("Open output folder")
        for button, name, description in (
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
        ):
            self._accessible_button(button, name, description)

        actions = QHBoxLayout()
        for widget in (
            self.review_play,
            self.review_stop,
            self.approve,
            self.reject,
            self.retry_failed,
            self.generate,
            self.stop_generation,
            self.open_output,
        ):
            actions.addWidget(widget)

        self.technical = QGroupBox("Technical details")
        self.technical.setCheckable(True)
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
        technical_layout = QVBoxLayout(self.technical)
        technical_layout.addWidget(self.process_log)
        technical_layout.addWidget(self.copy_diagnostics)

        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.addWidget(voice_box)
        details_layout.addWidget(self.review_table, 1)
        details_layout.addLayout(actions)
        details_layout.addWidget(self.technical)

        self.splitter = QSplitter()
        self.splitter.addWidget(self.collection_tree)
        self.splitter.addWidget(details)
        self.splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.narrator)
        layout.addWidget(self.status)
        layout.addWidget(self.counts)
        layout.addWidget(self.active)
        layout.addWidget(self.readiness_details)
        layout.addWidget(self.splitter, 1)

        self.voice_search.textChanged.connect(self._populate_voice_choices)
        self.voice_character.currentTextChanged.connect(self._show_reference)
        self.voice_character.activated.connect(self._record_current_reference)
        self.recent_choice.activated.connect(self._choose_recent_reference)
        self.recent_choice.lineEdit().returnPressed.connect(
            self._choose_typed_recent_reference
        )
        self.collection_tree.itemChanged.connect(self._collection_selection_changed)
        self.reference_previous.clicked.connect(lambda: self._move_reference(-1))
        self.reference_next.clicked.connect(lambda: self._move_reference(1))
        self.reference_play.clicked.connect(self.play_reference)
        self.reference_stop.clicked.connect(self.stop_preview)
        self.review_play.clicked.connect(self.play_selected_outcome)
        self.review_stop.clicked.connect(self.stop_preview)
        self.approve.clicked.connect(lambda: self.review_selected("approved"))
        self.reject.clicked.connect(lambda: self.review_selected("rejected"))
        self.retry_failed.clicked.connect(self.start_failed_retry)
        self.generate.clicked.connect(self.start_generation)
        self.stop_generation.clicked.connect(self.stop_child)
        self.open_output.clicked.connect(self.open_output_folder)
        self.copy_diagnostics.clicked.connect(self.copy_diagnostic_text)
        self.technical.toggled.connect(self._technical_toggled)
        self.readiness_details.toggled.connect(self.readiness_text.setVisible)
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
        self.refresh()
        self.status_timer.start()
        self._set_focus_chain()
        self.collection_tree.setFocus()

    @staticmethod
    def _accessible_button(button, name, description):
        button.setAccessibleName(name)
        button.setAccessibleDescription(description)

    def refresh(self):
        before = self._workspace_poll_signature()
        try:
            self._refresh_authoritative()
        except Exception as error:
            self._fail_closed(error)
        after = self._workspace_poll_signature()
        self._poll_signature = before if before != after else after

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
        values = []
        for path in self._poll_paths:
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

    def _poll_authoritative(self):
        if self._poll_signature != self._workspace_poll_signature():
            self.refresh()

    def _fail_closed(self, error):
        self.player.stop()
        self._preview_active = False
        self.summary = None
        self.voice_controller = None
        self._current_reference_key = None
        self._selected_review_identity = None
        self.status.setText(f"BLOCKED: {error}")
        self.status.setToolTip(str(error))
        self.review_table.setRowCount(0)
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
        self.stop_generation.setEnabled(
            self.process.state() != QProcess.ProcessState.NotRunning
        )

    def _refresh_authoritative(self):
        selected_queue_id = (
            self._selected_review_item().queue_id
            if self._selected_review_item() is not None
            else None
        )
        try:
            self.summary = inspect_workspace(
                self.workspace_directory,
                local_process_id=(
                    int(self.process.processId())
                    if self.process.state() != QProcess.ProcessState.NotRunning
                    else None
                ),
                local_process_started_at=self.local_process_started_at,
            )
            reviews = list_review_items(self.workspace_directory)
        except AuthoringWorkbenchError as error:
            self._fail_closed(error)
            return
        self.title.setText(self.summary.title)
        _directory, workspace = self._workspace_document()
        run_config = workspace["run_config"]
        self.narrator.setText(
            f"Narrator: {workspace['narrator_character']} | Backend: {run_config['backend']} | "
            f"Model: {run_config['model']} | Profile: {run_config['generation_profile']}"
        )
        self._populate_collections()
        self.collection_selection = inspect_collection_selection(
            self.workspace_directory,
            collection_ids=self._selected_collection_ids,
        )
        self.status.setText(self._status_text())
        self.status.setToolTip("; ".join(self.summary.blocked_reasons))
        self.counts.setText(
            " | ".join(
                (
                    f"Lines ready to generate: {self.summary.pending}",
                    f"Generated awaiting review: {self.summary.generated}",
                    f"Approved: {self.summary.approved}",
                    f"Rejected: {self.summary.rejected}",
                    f"Failed: {self.summary.failed}",
                    f"Missing references: {self.summary.missing_voice if self.summary.missing_voice is not None else 'unknown'}",
                    f"Recoverable source audio: {self.summary.recoverable_source_audio}",
                    f"Manual review: {self.summary.manual_review}",
                    f"Resolve source audio: {self.summary.resolve_audio}",
                    f"Skipped sound effects: {self.summary.skipped_sound_effects}",
                    f"Other skipped actions: {self.summary.skipped_actions}",
                    f"Selected collections: {self.collection_selection.collection_count}",
                    f"Selected story lines: {self.collection_selection.story_records}",
                    f"Selected queue lines: {self.collection_selection.queue_items}",
                    f"Selected ready lines: {self.collection_selection.readiness.ready}",
                )
            )
        )
        self._show_readiness_details(workspace)
        self._show_active()
        self.review_table.blockSignals(True)
        try:
            self._populate_reviews(reviews)
            if selected_queue_id is not None:
                for row in range(self.review_table.rowCount()):
                    item = self.review_table.item(row, 0)
                    review = item.data(256) if item is not None else None
                    if (
                        isinstance(review, ReviewItem)
                        and review.queue_id == selected_queue_id
                    ):
                        self.review_table.setCurrentCell(row, 0)
                        break
        finally:
            self.review_table.blockSignals(False)
        self._load_voice_controller()
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
        self._update_review_actions()

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
        selection = self.collection_selection
        if self.summary.runtime_status in {
            AuthoringRuntimeStatus.RUNNING_HERE,
            AuthoringRuntimeStatus.RUNNING_EXTERNAL,
            AuthoringRuntimeStatus.INTERRUPTED,
            AuthoringRuntimeStatus.BLOCKED,
        }:
            primary = labels[self.summary.runtime_status]
        elif selection is not None and not selection.collection_ids:
            primary = "NO COLLECTION SELECTED: choose at least one collection"
        elif selection is not None and selection.queue_items == 0:
            primary = "NO QUEUED ITEMS IN SELECTION: choose another collection"
        elif selection is not None and selection.readiness.blocked_reasons:
            primary = "SELECTION NEEDS ATTENTION: " + "; ".join(
                selection.readiness.blocked_reasons
            )
        elif selection is not None and selection.readiness.ready > 0:
            primary = f"READY: {selection.readiness.ready} selected line(s) can start"
        else:
            primary = labels[self.summary.runtime_status]
        lines.append(primary)
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

    def _show_readiness_details(self, workspace):
        story = workspace.get("story_index")
        voice = workspace.get("voice_manifest")
        history = immutable_history_timestamps(self.workspace_directory)
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

    def _populate_collections(self):
        self._loading_collections = True
        self.collection_tree.blockSignals(True)
        self.collection_tree.clear()
        try:
            collections = list_workspace_collections(self.workspace_directory)
            declared = tuple(collection.collection_id for collection in collections)
            if self._selected_collection_ids is None:
                stored = self.settings.value(
                    self._workspace_settings_key("collections")
                )
                if stored is None:
                    self._selected_collection_ids = declared
                else:
                    if isinstance(stored, str):
                        stored = [stored]
                    requested = {str(value) for value in stored}
                    self._selected_collection_ids = tuple(
                        value for value in declared if value in requested
                    )
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
        from vntts.authoring.workbench import _load_workspace

        return _load_workspace(self.workspace_directory)

    def _load_voice_controller(self):
        if self.summary.voice_manifest is None:
            self.voice_controller = None
            self.voice_character.clear()
            self._show_reference()
            return
        current = None
        if self.voice_controller is not None and self.voice_character.currentText():
            current = self.voice_controller.current(self.voice_character.currentText())
        controller = VoiceReferenceController.from_workspace(
            self.workspace_directory, self.summary.voice_manifest
        )
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
                    except (TypeError, ValueError):
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
            self.player.stop()
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
        self._preview_active = False
        self.media_outcome = "AUDIO PREVIEW ERROR: " + (
            message or self.player.errorString()
        )
        if self.summary is not None:
            self.status.setText(self._status_text())

    def stop_preview(self):
        self.player.stop()
        self._preview_active = False
        self.review_stop.setEnabled(False)
        self.media_outcome = "AUDIO PREVIEW STOPPED"
        if self.summary is not None:
            self.status.setText(self._status_text())

    def _populate_reviews(self, reviews):
        self.review_table.setRowCount(0)
        for review in reviews:
            row = self.review_table.rowCount()
            self.review_table.insertRow(row)
            for column, value in enumerate(
                (
                    review.line_id,
                    review.speaker,
                    review.review_status or review.status,
                    str(review.attempts),
                    review.text,
                    review.queue_id,
                )
            ):
                self.review_table.setItem(row, column, QTableWidgetItem(value))
            self.review_table.item(row, 0).setData(256, review)
        self.review_table.resizeColumnsToContents()

    def _selected_review_item(self):
        row = self.review_table.currentRow()
        if row < 0:
            return None
        item = self.review_table.item(row, 0)
        value = item.data(256) if item is not None else None
        return value if isinstance(value, ReviewItem) else None

    def _update_review_actions(self, *_arguments):
        selected = self._selected_review_item()
        selected_identity = (
            None
            if selected is None
            else (
                selected.queue_id,
                selected.status,
                selected.review_status,
                selected.audio,
            )
        )
        if selected_identity != self._selected_review_identity:
            self.player.stop()
            self._preview_active = False
            self.media_outcome = None
            self._selected_review_identity = selected_identity
        running = self.summary is not None and self.summary.runtime_status in {
            AuthoringRuntimeStatus.RUNNING_HERE,
            AuthoringRuntimeStatus.RUNNING_EXTERNAL,
        }
        enabled = (
            selected is not None
            and selected.status
            in {
                "generated",
                "approved",
            }
            and not running
        )
        self.approve.setEnabled(enabled)
        self.reject.setEnabled(enabled)
        self.review_play.setEnabled(enabled and selected.audio is not None)
        self.review_stop.setEnabled(self._preview_active)
        reason = (
            ""
            if enabled
            else "Select generated or approved audio while generation is idle"
        )
        self.approve.setToolTip(reason)
        self.reject.setToolTip(reason)
        self.review_play.setToolTip(
            "" if self.review_play.isEnabled() else reason or "No generated WAV exists"
        )
        self.review_stop.setToolTip(
            "" if self._preview_active else "No audio preview is currently playing"
        )

    def play_selected_outcome(self):
        selected = self._selected_review_item()
        if selected is None:
            self.status.setText("Select one generated outcome to play")
            return
        try:
            current = next(
                (
                    item
                    for item in list_review_items(self.workspace_directory)
                    if item.queue_id == selected.queue_id
                ),
                None,
            )
        except AuthoringWorkbenchError as error:
            self.media_outcome = f"GENERATED AUDIO BLOCKED: {error}"
            self.refresh()
            return
        if current is None or current.audio is None:
            self.media_outcome = "GENERATED AUDIO UNAVAILABLE: refresh the review list"
            self.status.setText(self._status_text())
            return
        self.player.setSource(QUrl.fromLocalFile(str(current.audio)))
        self.player.play()
        self._preview_active = True
        self.review_stop.setEnabled(True)
        self.media_outcome = f"PLAYING GENERATED REVIEW AUDIO: {current.line_id}"
        self.status.setText(self._status_text())

    def review_selected(self, decision):
        selected = self._selected_review_item()
        if selected is None:
            self.status.setText("Select one generated outcome to review")
            return
        self.player.stop()
        self._preview_active = False
        self.media_outcome = None
        try:
            review_workspace_item(self.workspace_directory, selected.queue_id, decision)
        except AuthoringWorkbenchError as error:
            self.status.setText(f"Unable to save review: {error}")
            return
        self.refresh()

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

    def _restore_settings(self):
        self.settings.beginGroup(self.settings_group)
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        sizes = self.settings.value("splitter")
        if isinstance(sizes, list):
            self.splitter.setSizes([int(value) for value in sizes])
        expanded = self.settings.value("technical-expanded", False, type=bool)
        self.technical.setChecked(expanded)
        self._technical_toggled(expanded)
        readiness_expanded = self.settings.value("readiness-expanded", False, type=bool)
        self.readiness_details.setChecked(readiness_expanded)
        self.readiness_text.setVisible(readiness_expanded)
        self.settings.endGroup()

    def _save_settings(self):
        self.settings.beginGroup(self.settings_group)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.sizes())
        self.settings.setValue("technical-expanded", self.technical.isChecked())
        self.settings.setValue("readiness-expanded", self.readiness_details.isChecked())
        self.settings.endGroup()
        self.settings.sync()

    def _set_focus_chain(self):
        widgets = (
            self.collection_tree,
            self.readiness_details,
            self.recent_choice,
            self.voice_search,
            self.voice_character,
            self.reference_previous,
            self.reference_play,
            self.reference_stop,
            self.reference_next,
            self.review_table,
            self.review_play,
            self.review_stop,
            self.approve,
            self.reject,
            self.retry_failed,
            self.generate,
            self.stop_generation,
            self.open_output,
            self.copy_diagnostics,
        )
        for first, second in zip(widgets, widgets[1:]):
            QWidget.setTabOrder(first, second)

    def closeEvent(self, event: QCloseEvent):
        self._save_settings()
        self.player.stop()
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

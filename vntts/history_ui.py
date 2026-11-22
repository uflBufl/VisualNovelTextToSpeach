from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime
from functools import partial
from threading import Event
from time import perf_counter
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QThreadPool, Signal
from PySide6.QtGui import QCloseEvent, QTextOption
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from vntts.action_icons import set_action_icon
from vntts.async_ui import LatestTaskRunner
from vntts.audio_output import PcmAdmission, pcm_admission_context
from vntts.history import DialogueHistory, DialogueHistoryEntry
from vntts.playback import ReplayAudio
from vntts.playback_observer import observe_playback_start, playback_preparation_context
from vntts.qt_dialogs import CloseGuardedDialog
from vntts.support import record_background_operation

_PREPARATION_LABELS = {
    "waiting-for-worker": "Waiting for the speech engine...",
    "loading-model": "Loading Pocket TTS...",
    "worker-ready": "Preparing speech for {character}...",
    "preparing-voice": "Preparing the voice for {character}...",
    "generating-first-chunk": "Generating speech for {character}...",
    "first-chunk-ready": "Starting audio for {character}...",
}


class DialogueHistoryDialog(CloseGuardedDialog):
    playback_started = Signal(int)

    def __init__(
        self,
        history: DialogueHistory,
        replay_handler: Callable[[str, str], object],
        parent: QWidget | None = None,
        *,
        stop_handler: Callable[[], object] | None = None,
        play_recording: Callable[[tuple[ReplayAudio, ...], Event], bool] | None = None,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self.history = history
        self.replay_handler = replay_handler
        self.play_recording = play_recording
        self._replaying_audio = False
        self.stop_handler = stop_handler
        self.replay_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.replay_runner.finished.connect(self._replay_finished)
        self.replay_runner.progressChanged.connect(self._show_preparation)
        self.stop_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.stop_runner.finished.connect(self._stop_finished)
        self._close_pending = False
        self._stop_unsupported = False
        self._spoken_character = ""
        self._playing_status = ""
        self._replay_error: Exception | None = None
        self._playing = False
        self._play_request = 0
        self._play_cancelled = Event()
        self.playback_started.connect(self._playback_started)
        self.visible_entries: list[DialogueHistoryEntry] = []
        self.setWindowTitle("History")
        self.resize(820, 480)

        self._create_entry_widgets()
        self._create_speech_controls()
        self._create_layout()
        self.refresh()

    def _create_entry_widgets(self) -> None:
        self.search = QLineEdit()
        self.search.setPlaceholderText("Speaker or dialogue text")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search history")
        self.search.textChanged.connect(self.refresh)
        self.search_label = QLabel("&Find a line")
        self.search_label.setBuddy(self.search)
        self.entries = QListWidget()
        self.entries.setAccessibleName("History entries")
        self.entries.setWordWrap(True)
        self.entries.setViewportMargins(6, 6, 6, 6)
        self.entries.setSpacing(6)
        self.entries.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.entries.currentRowChanged.connect(self.show_entry)
        self.empty_message = QLabel()
        self.empty_message.setWordWrap(True)
        self.empty_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_message.setMargin(12)
        self.entry_stack = QStackedWidget()
        self.entry_stack.addWidget(self.entries)
        self.entry_stack.addWidget(self.empty_message)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.document().setDocumentMargin(10)
        self.details.setAccessibleName("Selected dialogue details")
        self.details_label = QLabel("Selected &line")
        self.details_label.setBuddy(self.details)

    def _create_speech_controls(self) -> None:
        self.replay_button = QPushButton()
        self.current_voice_button = QPushButton()
        self.replay_button.clicked.connect(lambda: self._toggle_playback(recorded=True))
        self.current_voice_button.clicked.connect(
            lambda: self._toggle_playback(recorded=False)
        )
        self.export_button = QPushButton("Export...")
        set_action_icon(self.export_button, "download")
        self.export_button.setAccessibleName("Export history")
        self.export_button.setToolTip(
            "Export all retained lines, including those hidden by search."
        )
        self.status = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.status.setAccessibleName("Dialogue speech status")
        self.status.setFrameShape(QFrame.Shape.NoFrame)
        self.status.viewport().setAutoFillBackground(False)
        self.status.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.status.setFixedHeight(self.status.fontMetrics().lineSpacing() * 2 + 12)
        self.status.installEventFilter(self)
        self.export_button.clicked.connect(self.export_history)

    def _create_layout(self) -> None:
        actions = QHBoxLayout()
        actions.addWidget(self.replay_button)
        actions.addWidget(self.current_voice_button)
        actions.addStretch()

        entries_layout = QVBoxLayout()
        entries_layout.addWidget(QLabel("Played this session"))
        entries_layout.addWidget(self.entry_stack)
        details_layout = QVBoxLayout()
        details_layout.addWidget(self.details_label)
        details_layout.addWidget(self.details)
        details_layout.addLayout(actions)
        details_layout.addWidget(self.status)
        content = QHBoxLayout()
        content.addLayout(entries_layout, 1)
        content.addLayout(details_layout, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        set_action_icon(buttons.button(QDialogButtonBox.StandardButton.Close), "x")
        buttons.rejected.connect(self.close)
        footer = QHBoxLayout()
        footer.addWidget(self.export_button)
        footer.addStretch()
        footer.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(self.search_label)
        layout.addWidget(self.search)
        layout.addLayout(content)
        layout.addLayout(footer)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.status and event.type() == QEvent.Type.FontChange:
            self.status.setFixedHeight(self.status.fontMetrics().lineSpacing() * 2 + 12)
        return super().eventFilter(watched, event)

    def refresh(self) -> None:
        selected_id: str | None = None
        entry = self.current_entry()
        if entry is not None:
            selected_id = entry.id
        scroll_bar = self.entries.verticalScrollBar()
        previous_scroll = scroll_bar.value()
        latest = self.history.search(self.search.text())
        self.export_button.setEnabled(bool(self.history.snapshot()))
        self.entry_stack.setCurrentWidget(
            self.entries if latest else self.empty_message
        )
        if not latest:
            self.empty_message.setText(
                "No matching lines. Try another search."
                if self.search.text().strip()
                else "No dialogue has played in this session yet."
            )
        if latest == self.visible_entries:
            self._update_speech_controls(entry)
            return
        self.visible_entries = latest
        selected_index = next(
            (
                index
                for index, item in enumerate(self.visible_entries)
                if item.id == selected_id
            ),
            len(self.visible_entries) - 1,
        )
        with QSignalBlocker(self.entries):
            self.entries.clear()
            for entry in self.visible_entries:
                recorded_at = datetime.fromisoformat(entry.recorded_at).astimezone()
                preview = (
                    entry.text if len(entry.text) <= 100 else f"{entry.text[:97]}..."
                )
                self.entries.addItem(
                    f"{recorded_at:%H:%M:%S}  {entry.character}\n{preview}"
                )
            self.entries.setCurrentRow(selected_index)
        self.show_entry(selected_index)
        if not self.visible_entries:
            return
        if self.visible_entries[selected_index].id == selected_id:
            scroll_bar.setValue(min(previous_scroll, scroll_bar.maximum()))
        else:
            scroll_bar.setValue(scroll_bar.maximum())

    def current_entry(self) -> DialogueHistoryEntry | None:
        row = self.entries.currentRow()
        return (
            self.visible_entries[row] if 0 <= row < len(self.visible_entries) else None
        )

    def show_entry(self, row: int) -> None:
        entry = (
            self.visible_entries[row] if 0 <= row < len(self.visible_entries) else None
        )
        self._update_speech_controls(entry)
        if entry is None:
            self.details.clear()
            return
        recorded_at = datetime.fromisoformat(entry.recorded_at).astimezone()
        self.details.setPlainText(
            f"{entry.character} · {recorded_at:%b %d, %Y %H:%M}\n\n{entry.text}"
        )

    def _update_speech_controls(self, entry: DialogueHistoryEntry | None) -> None:
        if not (self.replay_runner.active or self.stop_runner.active):
            clips = (
                self.history.recording(entry.id)
                if entry and self.play_recording
                else ()
            )
            self.replay_button.setVisible(bool(clips))
            self.replay_button.setEnabled(bool(clips))
            caption = (
                "Original"
                if all(clip.complete for clip in clips)
                else "Original (excerpt)"
            )
            self.replay_button.setText(caption)
            self.replay_button.setAccessibleName(f"Play {caption.lower()}")
            self.replay_button.setToolTip(
                "Hear this line exactly as it sounded during reading."
            )
            self.current_voice_button.setEnabled(entry is not None)
            self.current_voice_button.setText("With current settings")
            self.current_voice_button.setAccessibleName("Speak with current settings")
            self.current_voice_button.setToolTip(
                "Speak the full line again using the current voice settings."
            )
            for button in (self.replay_button, self.current_voice_button):
                button.setMinimumWidth(0)
                button.setProperty("playbackState", "idle")
                set_action_icon(button, "play")
            return

        active, other = (
            (self.replay_button, self.current_voice_button)
            if self._replaying_audio
            else (self.current_voice_button, self.replay_button)
        )
        other.setEnabled(False)
        stopping = self.stop_runner.active or (
            self._replaying_audio and self._play_cancelled.is_set()
        )
        can_stop = (
            self._replaying_audio
            or self.stop_handler is not None
            and not self._stop_unsupported
        )
        state = "playing" if self._playing and not stopping else "preparing"
        active.setProperty("playbackState", state)
        if stopping:
            caption, icon = "Stopping...", "refresh-cw"
        elif can_stop:
            caption, icon = "Stop", "square"
        elif self._playing:
            caption, icon = "Playing...", "volume-2"
        else:
            caption, icon = "Preparing...", "refresh-cw"
        active.setText(caption)
        description = f"{caption.rstrip('.')} speech for {self._spoken_character}"
        active.setAccessibleName(description)
        active.setToolTip(description)
        set_action_icon(active, icon)
        active.setEnabled(can_stop and not stopping)

    def set_playing(self, playing: bool) -> None:
        if not self.replay_runner.active or self._play_cancelled.is_set():
            return
        self._playing = playing
        if playing:
            self.status.setPlainText(self._playing_status)
        self._update_speech_controls(self.current_entry())

    def _show_preparation(self, phase: str) -> None:
        if phase and not self._playing and not self._play_cancelled.is_set():
            self.status.setPlainText(
                _PREPARATION_LABELS[phase].format(character=self._spoken_character)
            )

    def _playback_started(self, request: int) -> None:
        if request == self._play_request:
            self.set_playing(True)

    def _toggle_playback(self, *, recorded: bool) -> None:
        requested_at = perf_counter()
        if self.replay_runner.active:
            self.stop_replay()
            return
        entry = self.current_entry()
        if entry is None or self.stop_runner.active:
            return
        clips = self.history.recording(entry.id) if recorded else ()
        if recorded and not clips:
            self._update_speech_controls(entry)
            return
        self._stop_unsupported = False
        self._spoken_character = entry.character
        self._replay_error = None
        self._playing = False
        self._play_request += 1
        self._play_cancelled = Event()
        self.status.setToolTip(entry.text)
        self._replaying_audio = recorded
        active = self.replay_button if recorded else self.current_voice_button
        active.setMinimumWidth(active.sizeHint().width())
        operation: Callable[[], object]
        if clips and self.play_recording is not None:
            operation = partial(self.play_recording, clips, self._play_cancelled)
            subject = "line" if all(clip.complete for clip in clips) else "excerpt"
            self._playing_status = f"Playing {entry.character}’s original {subject}..."
            self.status.setPlainText(
                f"Preparing {entry.character}’s original {subject}..."
            )
        else:
            operation = partial(self.replay_handler, entry.character, entry.text)
            self._playing_status = f"Speaking as {entry.character}..."
            self.status.setPlainText(f"Preparing speech for {entry.character}...")
        self.replay_runner.start_with_progress(
            self._run_replay,
            operation,
            self._play_request,
            self._play_cancelled,
            requested_at,
            recorded,
        )
        self._update_speech_controls(self.current_entry())

    def _run_replay(
        self,
        operation: Callable[[], object],
        request: int,
        cancelled: Event,
        requested_at: float,
        recorded: bool,
        *,
        progress: Callable[[str], None],
    ) -> object:
        def elapsed_ms() -> float:
            return round((perf_counter() - requested_at) * 1000, 3)

        timings = {"task_started_at_ms": elapsed_ms()}

        def preparing(phase: str) -> None:
            if not cancelled.is_set() and "first_audio_ms" not in timings:
                timings.setdefault(phase.replace("-", "_") + "_at_ms", elapsed_ms())
                if isValid(self):
                    progress(phase)

        def started() -> None:
            timings["first_audio_ms"] = elapsed_ms()
            if isValid(self):
                self.playback_started.emit(request)

        outcome = "complete"
        try:
            with (
                pcm_admission_context.set(PcmAdmission(lambda: not cancelled.is_set())),
                playback_preparation_context.set(None if recorded else preparing),
                observe_playback_start(started),
            ):
                result = operation()
                if isinstance(result, Future):
                    return result.result()
                return result
        except Exception:
            outcome = "failed"
            raise
        finally:
            record_background_operation(
                "history-original" if recorded else "history-current-voice",
                elapsed_ms(),
                "cancelled" if cancelled.is_set() else outcome,
                operation_id=uuid4().hex,
                **timings,
            )

    def _replay_finished(self, _result: object, error: Exception | None) -> None:
        self._replay_error = error
        self._playing = False
        self._update_speech_controls(self.current_entry())
        if self.stop_runner.active:
            return
        if self._replaying_audio and error is not None:
            self.status.setPlainText(
                f"Could not replay {self._spoken_character}: {error}"
            )
        elif self._replaying_audio and self._play_cancelled.is_set():
            self.status.setPlainText(f"Speech stopped for {self._spoken_character}.")
        elif error is not None:
            self.status.setPlainText(
                f"Could not speak as {self._spoken_character}: {error}. "
                "Check voice settings, then try again."
            )
        else:
            self.status.setPlainText(f"Speech finished for {self._spoken_character}.")
        if self._close_pending and not self.stop_runner.active:
            self._close_pending = False
            self.close()

    def stop_replay(self, *, close_after: bool = False) -> None:
        if close_after:
            self._close_pending = True
        if self.stop_runner.active:
            return
        if not self.replay_runner.active:
            if self._close_pending:
                self._close_pending = False
                self.close()
            return
        if self._replaying_audio:
            self._play_cancelled.set()
            self.status.setPlainText(f"Stopping speech for {self._spoken_character}...")
            self._update_speech_controls(self.current_entry())
            return
        if self.stop_handler is None:
            self.status.setPlainText("This speech engine cannot be stopped here.")
            return
        if self._stop_unsupported:
            self.status.setPlainText(
                "This speech engine cannot stop playback. Wait for it to finish."
            )
            return
        self.status.setPlainText(f"Stopping speech for {self._spoken_character}...")
        self._play_cancelled.set()
        self.stop_runner.start(self.stop_handler)
        self._update_speech_controls(self.current_entry())

    def _stop_finished(self, _result: object, error: Exception | None) -> None:
        if error is not None:
            self._close_pending = False
            self._update_speech_controls(self.current_entry())
            self.status.setPlainText(
                f"Unable to stop speech for {self._spoken_character}: {error}"
            )
            return
        if _result is False and self.replay_runner.active:
            self._stop_unsupported = True
            self._update_speech_controls(self.current_entry())
            self.status.setPlainText(
                "This speech engine cannot stop playback. Wait for it to finish."
            )
            return
        if _result is False:
            self._replay_finished(None, self._replay_error)
            return
        self.replay_runner.cancel()
        self._playing = False
        self._update_speech_controls(self.current_entry())
        self.status.setPlainText(f"Speech stopped for {self._spoken_character}.")
        if self._close_pending:
            self._close_pending = False
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.replay_runner.active or self.stop_runner.active:
            self.stop_replay(close_after=True)
            event.ignore()
            return
        super().closeEvent(event)

    def export_history(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export history",
            "dialogue-history.txt",
            "Text files (*.txt);;JSON files (*.json)",
        )
        if not path:
            return
        if not path.lower().endswith((".txt", ".json")):
            path += ".json" if "JSON" in selected_filter else ".txt"
        try:
            self.history.export(path)
        except OSError as error:
            QMessageBox.warning(self, "Unable to export history", str(error))

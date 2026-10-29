"""Read-only explanations and playback of unsuccessful preparation results."""

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vntts.action_icons import set_action_icon
from vntts.async_ui import LatestTaskRunner
from vntts.pregeneration_failures import (
    PreparationFailure,
    inspect_preparation_failures,
    load_preparation_failure_audio,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.qt_audio import QtPcmPlayer


class PreparationFailuresPanel(QWidget):
    def _create_failure_table(self, body: QVBoxLayout) -> None:
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("Character", "Phrase", "Reason"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.setMinimumHeight(160)
        self.table.setMaximumHeight(260)
        self.table.setAccessibleName("Unsuccessful preparation results")
        self.table.currentCellChanged.connect(self._selected)
        body.addWidget(self.table)

    def _create_playback_controls(self, body: QVBoxLayout) -> None:
        controls = QHBoxLayout()
        self.character = QLabel()
        self.character.setTextFormat(Qt.TextFormat.PlainText)
        controls.addWidget(self.character, 1)
        self.play = QPushButton("Listen")
        self.play.setToolTip(
            "Listen to this diagnostic recording. Active reading will stop first."
        )
        set_action_icon(self.play, "play")
        self.play.clicked.connect(self._play)
        controls.addWidget(self.play)
        self.stop_button = QPushButton("Stop")
        set_action_icon(self.stop_button, "square")
        self.stop_button.clicked.connect(self.stop)
        controls.addWidget(self.stop_button)
        body.addLayout(controls)

    def _create_technical_details(self, body: QVBoxLayout) -> None:
        self.details = QPushButton("Technical details")
        self.details.setCheckable(True)
        self.details.toggled.connect(self._show_details)
        body.addWidget(self.details, alignment=Qt.AlignmentFlag.AlignLeft)
        self.technical = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.technical.setAccessibleName("Preparation failure technical details")
        self.technical.setMaximumHeight(160)
        self.technical.hide()
        body.addWidget(self.technical)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        thread_pool: QThreadPool | None = None,
        player: QtPcmPlayer | None = None,
    ) -> None:
        super().__init__(parent)
        self.before_playback: Callable[[Callable[[], None]], None] | None = None
        self.generation_input: PregenerationInput | None = None
        self.failures: tuple[PreparationFailure, ...] = ()
        self.player = player
        self._play_request = 0
        self._playing = False
        self._refresh_pending = False
        self._refresh_on_show = False
        self.inspector = LatestTaskRunner(self, thread_pool=thread_pool)
        self.inspector.finished.connect(self._inspected)
        self.loader = LatestTaskRunner(self, thread_pool=thread_pool)
        self.loader.finished.connect(self._audio_loaded)
        self.toggle = QPushButton("Show preparation problems")
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self._expanded)
        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        self._create_failure_table(body)
        self._create_playback_controls(body)
        self.phrase = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.phrase.setAccessibleName("Full failed phrase")
        self.phrase.setFixedHeight(self.phrase.fontMetrics().lineSpacing() * 4 + 16)
        self.phrase.installEventFilter(self)
        body.addWidget(self.phrase)
        self.reason = QLabel()
        self.reason.setWordWrap(True)
        self.reason.setTextFormat(Qt.TextFormat.PlainText)
        self.reason.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.addWidget(self.reason)
        self.audio_status = QLabel()
        self.audio_status.setWordWrap(True)
        self.audio_status.setTextFormat(Qt.TextFormat.PlainText)
        body.addWidget(self.audio_status)
        self._create_technical_details(body)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body)
        self.body.hide()
        self.hide()
        if self.player is not None:
            self._connect_player()
        self._selected()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.phrase and event.type() == QEvent.Type.FontChange:
            self.phrase.setFixedHeight(self.phrase.fontMetrics().lineSpacing() * 4 + 16)
        return super().eventFilter(watched, event)

    def set_input(self, generation_input: PregenerationInput | None) -> None:
        if self.generation_input == generation_input:
            return
        self.stop()
        self.inspector.cancel()
        self._refresh_pending = False
        self._refresh_on_show = False
        self.generation_input = generation_input
        self.failures = ()
        self.table.setRowCount(0)
        self.toggle.setChecked(False)
        self.hide()

    def refresh(self) -> None:
        if self.generation_input is None:
            return
        if self.inspector.active:
            self._refresh_pending = True
            return
        self.inspector.start(inspect_preparation_failures, self.generation_input)

    def _inspected(self, result: object, error: Exception | None) -> None:
        refresh_pending = self._refresh_pending
        self._refresh_pending = False
        if error is not None:
            self.setVisible(True)
            self.toggle.setChecked(True)
            self.stop()
            self.failures = ()
            self.table.setRowCount(0)
            self.audio_status.setText(f"Unable to load preparation problems: {error}")
        elif isinstance(result, tuple) and all(
            isinstance(item, PreparationFailure) for item in result
        ):
            if result != self.failures:
                selected = self._current()
                self.failures = result
                self.table.blockSignals(True)
                self.table.setRowCount(len(result))
                for row, failure in enumerate(result):
                    for column, text in enumerate(
                        (failure.speaker, failure.text, failure.reason)
                    ):
                        cell = QTableWidgetItem(text)
                        cell.setToolTip(text)
                        self.table.setItem(row, column, cell)
                row = next(
                    (
                        i
                        for i, item in enumerate(result)
                        if selected and item.queue_id == selected.queue_id
                    ),
                    0,
                )
                if result:
                    self.table.selectRow(row)
                self.table.blockSignals(False)
                if self._current() != selected:
                    self._selected()
            self.setVisible(bool(result))
        if refresh_pending:
            self.refresh()

    def _current(self) -> PreparationFailure | None:
        row = self.table.currentRow()
        return self.failures[row] if 0 <= row < len(self.failures) else None

    def _selected(self, *_arguments: object) -> None:
        self.stop()
        item = self._current()
        self.character.setText(item.speaker if item else "")
        self.phrase.setPlainText(item.text if item else "")
        self.reason.setText(item.reason if item else "")
        self.technical.setPlainText(item.details if item else "")
        self.details.setVisible(bool(item and item.details))
        self.details.setChecked(False)
        self._show_details(False)
        self.audio_status.setText(
            {
                "available": "Diagnostic audio only; it is not a prepared recording.",
                "not_created": "No audio was created.",
                "unavailable": "The recording was not retained or is no longer available.",
            }[item.audio_status]
            if item
            else ""
        )

    def _show_details(self, expanded: bool) -> None:
        self.technical.setVisible(expanded and bool(self.technical.toPlainText()))

    def _expanded(self, expanded: bool) -> None:
        self.body.setVisible(expanded)
        self.toggle.setText(
            "Hide preparation problems" if expanded else "Show preparation problems"
        )
        if expanded:
            self.refresh()
        else:
            self.stop()

    def _play(self) -> None:
        item = self._current()
        generation_input = self.generation_input
        if item is None or generation_input is None or item.audio_status != "available":
            return
        self.stop()
        request = self._play_request

        def start() -> None:
            if (
                request != self._play_request
                or not self.isVisible()
                or not self.toggle.isChecked()
            ):
                return
            self.loader.start(load_preparation_failure_audio, generation_input, item)
            self.audio_status.setText("Preparing diagnostic audio...")
            self._update_buttons()

        if self.before_playback is None:
            start()
        else:
            self.before_playback(start)

    def _connect_player(self) -> None:
        assert self.player is not None
        self.player.errorOccurred.connect(
            lambda _code, message: self._playback_error(message)
        )
        self.player.playbackStateChanged.connect(self._playback_changed)

    def _audio_loaded(self, payload: object, error: Exception | None) -> None:
        if error is not None or not isinstance(payload, bytes):
            self._playback_error(str(error or "Audio is unavailable"))
            return
        if self.player is None:
            self.player = QtPcmPlayer(self)
            self._connect_player()
        self._playing = True
        self.audio_status.setText("Playing diagnostic audio.")
        self.player.play_bytes(payload, source="Failed preparation")
        self._update_buttons()

    def _playback_error(self, message: str) -> None:
        self.stop()
        self.audio_status.setText(f"Unable to play this recording: {message}")

    def _playback_changed(self, state: object) -> None:
        if state == QMediaPlayer.PlaybackState.StoppedState:
            self._playing = False
            self._update_buttons()
            self.audio_status.setText("Diagnostic playback stopped.")

    def _update_buttons(self) -> None:
        item = self._current()
        busy = self.loader.active or self._playing
        self.play.setEnabled(
            bool(item and item.audio_status == "available" and not busy)
        )
        self.stop_button.setEnabled(busy)

    def stop(self) -> None:
        self._play_request += 1
        self.loader.cancel()
        self._playing = False
        if self.player is not None:
            self.player.stop()
        self._update_buttons()

    def hideEvent(self, event: QHideEvent) -> None:
        self.stop()
        self._refresh_on_show |= self.inspector.cancel()
        self._refresh_pending = False
        super().hideEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._refresh_on_show:
            self._refresh_on_show = False
            self.refresh()

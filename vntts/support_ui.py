from datetime import datetime
from typing import Callable, Literal

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QHideEvent, QKeySequence, QShowEvent, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vntts import support
from vntts.action_icons import set_action_icon
from vntts.diagnostic_recording import MAX_DIAGNOSTIC_SECONDS
from vntts.reading_diagnostics import DiagnosticSessionEntry
from vntts.ui_text import shortcut_tooltip


class SupportCenterDialog(QDialog):
    diagnostics_requested = Signal()
    export_requested = Signal()
    settings_folder_requested = Signal()
    reading_start_requested = Signal(bool, int)
    reading_stop_requested = Signal()
    reading_export_requested = Signal(str)
    reading_delete_requested = Signal(str)
    reading_history_requested = Signal()
    reading_folder_requested = Signal()

    def __init__(
        self,
        event_log: support.RuntimeSupportLog,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.event_log = event_log
        self.reading_status_provider: Callable[[], dict[str, object] | None] | None = (
            None
        )
        self.setWindowTitle("Support and logs")
        self.setMinimumSize(620, 600)
        self.resize(940, 760)
        self._diagnostic_sessions: dict[str, DiagnosticSessionEntry] = {}
        self._reading_report: dict[str, object] | None = None
        self._pending_history_selection: str | None = None
        self._history_busy = False
        self._export_busy = False
        self._history_status = ""
        self._history_retry = QTimer(self)
        self._history_retry.setSingleShot(True)
        self._history_retry.timeout.connect(self.reading_history_requested.emit)

        self.events = QTextEdit()
        self.events.setReadOnly(True)
        self.events.setAccessibleName("Runtime events")
        self.events.document().setDocumentMargin(8)
        self.events.setPlaceholderText("No runtime events recorded yet.")
        self.events.installEventFilter(self)
        self._rendered_lines: list[str] = []
        self.pending_event_count = 0
        self.new_events_button = QPushButton("New events")
        set_action_icon(self.new_events_button, "arrow-down-to-line")
        self.new_events_button.setToolTip("Scroll to the newest log events")
        self.new_events_button.setAccessibleName("Show new runtime log events")
        self.new_events_button.clicked.connect(self.show_new_events)
        policy = self.new_events_button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.new_events_button.setSizePolicy(policy)
        self.new_events_button.hide()
        self.privacy_note = QLabel(
            "Export report includes technical logs. Session exports can also include "
            "game media and dialogue text when recording is selected. Paths and log "
            "messages may contain personal information; review the ZIP before sharing."
        )
        self.privacy_note.setToolTip(
            "Export report excludes screenshots, recognized dialogue, voice recordings "
            "and model files. Local usernames and known secret fields are redacted; "
            "game folder structure remains."
        )
        self.privacy_note.setAccessibleDescription(self.privacy_note.toolTip())
        self.privacy_note.setWordWrap(True)
        actions = QHBoxLayout()
        self.diagnostics_button = QPushButton("Diagnostics")
        set_action_icon(self.diagnostics_button, "scan")
        self.diagnostics_button.setToolTip("Open live diagnostics")
        self.export_button = QPushButton("Export report")
        set_action_icon(self.export_button, "download")
        self.export_button.setToolTip("Export support report")
        self.settings_button = QPushButton("Settings folder")
        set_action_icon(self.settings_button, "folder-open")
        self.settings_button.setToolTip("Open application settings folder")
        self.diagnostics_button.setAccessibleName("Open live diagnostics")
        self.diagnostics_button.setAccessibleDescription(
            "Open the latest capture, OCR, routing and latency diagnostics"
        )
        self.diagnostics_button.setShortcut(QKeySequence("Ctrl+D"))
        self.diagnostics_button.setToolTip(
            shortcut_tooltip(
                self.diagnostics_button.toolTip(), self.diagnostics_button.shortcut()
            )
        )
        self.export_button.setAccessibleName("Export support report")
        self.export_button.setShortcut(QKeySequence("Ctrl+E"))
        self.export_button.setToolTip(
            shortcut_tooltip(
                self.export_button.toolTip(), self.export_button.shortcut()
            )
        )
        self.settings_button.setAccessibleName("Open application settings folder")
        self.settings_button.setAccessibleDescription(
            "Open the local folder containing application settings"
        )
        self.settings_button.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.settings_button.setToolTip(
            shortcut_tooltip(
                self.settings_button.toolTip(), self.settings_button.shortcut()
            )
        )
        self.diagnostics_button.clicked.connect(self.request_diagnostics)
        self.export_button.clicked.connect(self.request_export)
        self.settings_button.clicked.connect(self.request_settings_folder)
        actions.addWidget(self.diagnostics_button)
        actions.addWidget(self.settings_button)
        actions.addStretch()
        self._export_status = ""
        self._launcher_status = ""
        self._reading_start_error = ""
        self.operation_status = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.operation_status.setAccessibleName("Support operation status")
        self.operation_status.setFrameShape(QFrame.Shape.NoFrame)
        self.operation_status.viewport().setAutoFillBackground(False)
        self.operation_status.setWordWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
        )
        self.operation_status.setFixedHeight(
            self.operation_status.fontMetrics().lineSpacing() * 3 + 12
        )
        self.operation_status.installEventFilter(self)
        self.reading_media = QCheckBox("Record game video and audio (macOS)")
        self.reading_media.setToolTip(
            "Opt-in: includes selected game video, game and speech audio, used source "
            "WAVs, recognized text and matched story dialogue in the session export. "
            "Dialogue text may be retained even if media recording fails. "
            "Never records the desktop or microphone."
        )
        self.reading_seconds = QSpinBox()
        self.reading_seconds.setRange(1, MAX_DIAGNOSTIC_SECONDS)
        self.reading_seconds.setValue(600)
        self.reading_seconds.setSuffix(" s")
        self.reading_seconds.setAccessibleName("Diagnostic session duration limit")
        self.reading_seconds.setToolTip(
            "Required duration, up to 3600 seconds (1 hour). Recording stops automatically at this limit or when disk space runs low."
        )
        self.reading_start = QPushButton("Start session")
        self.reading_stop = QPushButton("Stop")
        self.reading_export = QPushButton("Export selected")
        self.reading_delete = QPushButton("Delete selected")
        self.reading_folder = QPushButton("Sessions folder")
        self.reading_history = QTreeWidget()
        self.reading_history.setAccessibleName("Saved diagnostic sessions")
        self.reading_history.setRootIsDecorated(False)
        self.reading_history.setMinimumHeight(130)
        self.reading_history.installEventFilter(self)
        self.reading_history.setHeaderLabels(
            ["Recorded (local time)", "Game / window", "Size"]
        )
        self.reading_history.setColumnWidth(0, 215)
        self.reading_history.setColumnWidth(2, 100)
        self.reading_history.header().setStretchLastSection(False)
        self.reading_history.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.reading_history.itemSelectionChanged.connect(
            self._history_selection_changed
        )
        self.reading_storage = QLabel("Loading saved sessions...")
        self.reading_storage.setWordWrap(True)
        self.reading_history_detail = QPlainTextEdit(
            readOnly=True, tabChangesFocus=True
        )
        self.reading_history_detail.setAccessibleName(
            "Selected diagnostic session details"
        )
        self.reading_history_detail.setFrameShape(QFrame.Shape.NoFrame)
        self.reading_history_detail.viewport().setAutoFillBackground(False)
        self.reading_history_detail.setFixedHeight(
            self.reading_history_detail.fontMetrics().lineSpacing() * 3 + 12
        )
        self.reading_history_detail.installEventFilter(self)
        self.reading_status = QLabel("No current diagnostic session.")
        self.reading_status.setWordWrap(True)
        self.reading_status.setTextFormat(Qt.TextFormat.PlainText)
        self.reading_start.clicked.connect(
            lambda: self.reading_start_requested.emit(
                self.reading_media.isChecked(), self.reading_seconds.value()
            )
        )
        self.reading_stop.clicked.connect(self.request_reading_stop)
        self.reading_export.clicked.connect(self.request_session_export)
        self.reading_delete.clicked.connect(self.request_session_delete)
        self.reading_folder.clicked.connect(self.reading_folder_requested.emit)
        for button, icon, action in (
            (self.reading_start, "play", "Start diagnostic session"),
            (self.reading_stop, "square", "Stop diagnostic session"),
            (self.reading_export, "download", "Export selected diagnostic session"),
            (self.reading_delete, "trash-2", "Delete selected diagnostic session"),
            (self.reading_folder, "folder-open", "Open diagnostic sessions folder"),
        ):
            set_action_icon(button, icon)
            button.setAccessibleName(action)
            button.setToolTip(action)
            button.setAutoDefault(False)
        self.reading_stop.setEnabled(False)
        self.reading_export.setEnabled(False)
        self.reading_delete.setEnabled(False)
        self._create_layout(actions)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)

    def _create_layout(self, actions: QHBoxLayout) -> None:
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        set_action_icon(buttons.button(QDialogButtonBox.StandardButton.Close), "x")
        buttons.rejected.connect(self.close)
        footer = QHBoxLayout()
        footer.addWidget(self.export_button)
        footer.addStretch()
        footer.addWidget(buttons)
        heading = QHBoxLayout()
        heading.addWidget(QLabel("Runtime events"))
        heading.addStretch()
        heading.addWidget(self.new_events_button)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addLayout(heading)
        layout.addWidget(self.events, 1)
        layout.addLayout(actions)
        layout.addWidget(self.privacy_note)
        recording_options = QHBoxLayout()
        recording_options.addWidget(self.reading_media, 1)
        recording_options.addWidget(self.reading_seconds)
        layout.addLayout(recording_options)
        recording_actions = QHBoxLayout()
        for button in (
            self.reading_start,
            self.reading_stop,
            self.reading_folder,
        ):
            recording_actions.addWidget(button)
        layout.addLayout(recording_actions)
        layout.addWidget(self.reading_status)
        layout.addWidget(self.reading_storage)
        layout.addWidget(self.reading_history, 1)
        history_actions = QHBoxLayout()
        history_actions.addWidget(self.reading_export)
        history_actions.addWidget(self.reading_delete)
        history_actions.addStretch()
        layout.addLayout(history_actions)
        layout.addWidget(self.reading_history_detail)
        layout.addWidget(self.operation_status)
        self.body_scroll = QScrollArea()
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.body_scroll.setWidget(body)
        shell = QVBoxLayout(self)
        shell.setContentsMargins(16, 16, 16, 16)
        shell.setSpacing(10)
        shell.addWidget(self.body_scroll, 1)
        shell.addLayout(footer)
        self.setTabOrder(self.events, self.new_events_button)
        self.setTabOrder(self.new_events_button, self.diagnostics_button)
        self.setTabOrder(self.diagnostics_button, self.settings_button)
        self.setTabOrder(self.settings_button, self.reading_media)
        self.setTabOrder(self.reading_media, self.reading_seconds)
        self.setTabOrder(self.reading_seconds, self.reading_start)
        self.setTabOrder(self.reading_start, self.reading_stop)
        self.setTabOrder(self.reading_stop, self.reading_folder)
        self.setTabOrder(self.reading_folder, self.reading_history)
        self.setTabOrder(self.reading_history, self.reading_export)
        self.setTabOrder(self.reading_export, self.reading_delete)
        self.setTabOrder(self.reading_delete, self.reading_history_detail)
        self.setTabOrder(self.reading_history_detail, self.operation_status)
        self.setTabOrder(self.operation_status, self.export_button)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(watched, QAbstractScrollArea)
            and event.type() == QEvent.Type.FocusIn
        ):
            # Reveal the whole editor/list, not just its text cursor or selected row.
            self.body_scroll.ensureWidgetVisible(watched.viewport())
        if (
            isinstance(watched, QPlainTextEdit)
            and event.type() == QEvent.Type.FontChange
        ):
            watched.setFixedHeight(watched.fontMetrics().lineSpacing() * 3 + 12)
        return super().eventFilter(watched, event)

    def _show_status(self) -> None:
        self.operation_status.setPlainText(
            "\n".join(
                filter(
                    None,
                    (
                        self._export_status,
                        self._launcher_status,
                        self._reading_start_error,
                        self._history_status,
                    ),
                )
            )
        )

    def set_reading_start_error(self, message: str) -> None:
        self._reading_start_error = message
        self._show_status()

    def request_diagnostics(self) -> None:
        if not self.diagnostics_button.isEnabled():
            return
        self.diagnostics_button.setEnabled(False)
        self._launcher_status = "Opening live diagnostics..."
        self._show_status()
        self.diagnostics_requested.emit()

    def request_settings_folder(self) -> None:
        if not self.settings_button.isEnabled():
            return
        self.settings_button.setEnabled(False)
        self._launcher_status = "Opening the application settings folder..."
        self._show_status()
        self.settings_folder_requested.emit()

    def set_launcher_result(
        self,
        launcher: Literal["diagnostics", "settings-folder"],
        successful: bool,
        message: str,
    ) -> None:
        buttons = {
            "diagnostics": self.diagnostics_button,
            "settings-folder": self.settings_button,
        }
        button = buttons[launcher]
        button.setEnabled(True)
        self._launcher_status = (
            message if successful else f"{message}. Select {button.text()} to retry."
        )
        self._show_status()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.reading_history_requested.emit()
        self.refresh()
        self.timer.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def refresh_reading_session(self) -> None:
        report = (
            self.reading_status_provider() if self.reading_status_provider else None
        )
        previous = self._reading_report or {}
        current = report or {}
        self._reading_report = dict(report) if report else None
        if (previous.get("session_id"), previous.get("status")) != (
            current.get("session_id"),
            current.get("status"),
        ):
            self.reading_history_requested.emit()
        if previous != current:
            for index in range(self.reading_history.topLevelItemCount()):
                item = self.reading_history.topLevelItem(index)
                assert item is not None
                entry = self._diagnostic_sessions[
                    item.data(0, Qt.ItemDataRole.UserRole)
                ]
                self._set_history_row_label(item, entry)
        self._update_history_actions()
        if report is None:
            self.reading_start.setEnabled(True)
            self.reading_media.setEnabled(True)
            self.reading_seconds.setEnabled(True)
            self.reading_stop.setEnabled(False)
            self.reading_status.setText("No current diagnostic session.")
            return
        finished = report.get("status") in {"finished", "interrupted"}
        self.reading_start.setEnabled(finished)
        self.reading_media.setEnabled(finished)
        self.reading_seconds.setEnabled(finished)
        self.reading_stop.setEnabled(
            not finished and report.get("status") == "recording"
        )
        target = report.get("target")
        identity = (
            f"Window {target.get('window_id')}, PID {target.get('pid')}: {target.get('title')}. "
            if isinstance(target, dict)
            else ""
        )
        self.reading_status.setText(
            ("Last completed session" if finished else "Current session")
            + f" (started {report.get('started_at') or 'time unavailable'}). "
            + identity
            + f"Session: {report.get('status')}; media: {report.get('media_status')}; "
            f"analysis: {report.get('analysis_status')}. "
            + (
                f"Time remaining: {report['remaining_seconds']} s. "
                if report.get("status") == "recording" and "remaining_seconds" in report
                else ""
            )
            + f"{report.get('media_error') or report.get('error_type') or ''}"
        )

    def selected_diagnostic_session(self) -> DiagnosticSessionEntry | None:
        item = self.reading_history.currentItem()
        return (
            self._diagnostic_sessions.get(item.data(0, Qt.ItemDataRole.UserRole))
            if item is not None
            else None
        )

    def request_reading_stop(self) -> None:
        self.refresh_reading_session()
        session_id = (self._reading_report or {}).get("session_id")
        if isinstance(session_id, str):
            self._pending_history_selection = session_id
            with QSignalBlocker(self.reading_history):
                self.reading_history.selectionModel().clear()
            self._select_pending_history_session()
            self._update_history_actions()
        self.reading_stop_requested.emit()

    def _history_selection_changed(self) -> None:
        self._pending_history_selection = None
        self._update_history_actions()

    def _select_pending_history_session(self) -> bool:
        if self._pending_history_selection is None:
            return False
        for index in range(self.reading_history.topLevelItemCount()):
            item = self.reading_history.topLevelItem(index)
            assert item is not None
            if (
                item.data(0, Qt.ItemDataRole.UserRole)
                == self._pending_history_selection
            ):
                self._pending_history_selection = None
                self.reading_history.setCurrentItem(item)
                self.reading_history.scrollToItem(item)
                self.body_scroll.ensureWidgetVisible(self.reading_history.viewport())
                return True
        return False

    def set_diagnostic_sessions(self, entries: list[DiagnosticSessionEntry]) -> None:
        if any("Waiting for interrupted recorders" in entry.error for entry in entries):
            self._history_retry.start(15000)
        else:
            self._history_retry.stop()
        selected = self.selected_diagnostic_session()
        scroll = self.reading_history.verticalScrollBar().value()
        self._diagnostic_sessions = {entry.session_id: entry for entry in entries}
        with QSignalBlocker(self.reading_history):
            self.reading_history.clear()
            for entry in entries:
                report = entry.report or {}
                item = QTreeWidgetItem(
                    [
                        _diagnostic_date(report.get("started_at")),
                        "",
                        _diagnostic_size(entry.size_bytes),
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, entry.session_id)
                self._set_history_row_label(item, entry)
                self.reading_history.addTopLevelItem(item)
                if selected is not None and entry.session_id == selected.session_id:
                    self.reading_history.setCurrentItem(item)
            first = self.reading_history.topLevelItem(0)
            if (
                self._pending_history_selection is None
                and self.reading_history.currentItem() is None
                and first is not None
            ):
                self.reading_history.setCurrentItem(first)
        self.reading_history.doItemsLayout()
        if not self._select_pending_history_session():
            self.reading_history.verticalScrollBar().setValue(scroll)
        known_size = sum(
            entry.size_bytes for entry in entries if entry.size_bytes is not None
        )
        unknown = sum(entry.size_bytes is None for entry in entries)
        self.reading_storage.setText(
            f"Saved sessions: {len(entries)} · {_diagnostic_size(known_size)}"
            + (f" known; size unavailable for {unknown}" if unknown else " total")
        )
        self._update_history_actions()

    def _set_history_row_label(
        self, item: QTreeWidgetItem, entry: DiagnosticSessionEntry
    ) -> None:
        target = (entry.report or {}).get("target")
        title = (
            str(target.get("title") or "Untitled window")
            if isinstance(target, dict)
            else "Reading diagnostics"
        )
        issue, reason = _diagnostic_issue(entry, self._reading_report)
        item.setText(1, f"{title} — {issue}" if issue else title)
        for column in range(3):
            item.setToolTip(
                column,
                f"{item.text(0)}\n{item.text(1)}\nSession: {entry.session_id}\n{reason}",
            )

    def set_history_busy(self, busy: bool) -> None:
        self._history_busy = busy
        self._update_history_actions()

    def set_history_status(self, message: str) -> None:
        self._history_status = message
        self._show_status()

    def _update_history_actions(self) -> None:
        entry = self.selected_diagnostic_session()
        current = self._reading_report or {}
        current_busy = (
            entry is not None
            and entry.session_id == current.get("session_id")
            and current.get("status") not in {"finished", "interrupted"}
        )
        available = bool(entry and entry.saved and not current_busy)
        enabled = available and not self._history_busy and not self._export_busy
        self.export_button.setEnabled(not self._history_busy and not self._export_busy)
        self.reading_export.setEnabled(enabled)
        self.reading_delete.setEnabled(enabled)
        details = "Select a saved session to export or delete it."
        if entry is not None:
            issue, reason = _diagnostic_issue(entry, self._reading_report)
            details = "\n".join(
                filter(
                    None,
                    (
                        _diagnostic_files(entry.file_counts),
                        f"{issue}: {reason}" if reason else issue,
                    ),
                )
            )
        if self.reading_history_detail.toPlainText() != details:
            self.reading_history_detail.setPlainText(details)

    def request_session_export(self) -> None:
        entry = self.selected_diagnostic_session()
        if entry is not None and self.reading_export.isEnabled():
            self.reading_export_requested.emit(entry.session_id)

    def request_session_delete(self) -> None:
        entry = self.selected_diagnostic_session()
        item = self.reading_history.currentItem()
        if entry is None or item is None or not self.reading_delete.isEnabled():
            return
        answer = QMessageBox.question(
            self,
            "Delete diagnostic session?",
            f"Delete {item.text(0)} — {item.text(1)} ({item.text(2)})?\n"
            "The session's logs and recordings will be permanently deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.reading_delete_requested.emit(entry.session_id)

    def refresh(self) -> None:
        self.refresh_reading_session()
        entries = sorted(
            self.event_log.snapshot()
            + support.application_error_log.snapshot()
            + support.native_speech_log.snapshot()
            + support.game_import_log.snapshot(),
            key=lambda entry: str(entry.get("recorded_at", "")),
        )
        rendered = []
        for entry in entries:
            timestamp = str(entry.get("recorded_at", ""))
            message = str(entry.get("message", ""))
            if entry.get("level") == "game-import":
                details = "; ".join(
                    f"{key}={_support_value(entry[key])}"
                    for key in support.game_import_fields
                    if key != "stage" and key in entry
                )
                if details:
                    message = f"{message}; {details}"
            rendered.append(
                f"{timestamp} [{str(entry.get('level', '')).upper()}] {message}"
            )
        if rendered == self._rendered_lines:
            return
        scroll_bar = self.events.verticalScrollBar()
        saved_cursor = self.events.textCursor()
        was_at_end = (
            scroll_bar.value() >= scroll_bar.maximum() - 1
            and not saved_cursor.hasSelection()
        )
        saved_scroll = scroll_bar.value()
        saved_anchor = saved_cursor.anchor()
        saved_position = saved_cursor.position()
        old_lines = self._rendered_lines
        appended = (
            len(rendered) >= len(old_lines) and rendered[: len(old_lines)] == old_lines
        )
        new_count = len(rendered) - len(old_lines) if appended else len(rendered)

        if appended and old_lines:
            document_cursor = QTextCursor(self.events.document())
            document_cursor.movePosition(QTextCursor.MoveOperation.End)
            for line in rendered[len(old_lines) :]:
                document_cursor.insertBlock()
                document_cursor.insertText(line)
        else:
            self.events.setPlainText("\n".join(rendered))

        self._rendered_lines = list(rendered)
        if was_at_end or not old_lines:
            self.show_new_events()
            return

        if appended:
            restored_cursor = QTextCursor(self.events.document())
            restored_cursor.setPosition(saved_anchor)
            restored_cursor.setPosition(
                saved_position,
                QTextCursor.MoveMode.KeepAnchor,
            )
            self.events.setTextCursor(restored_cursor)
        scroll_bar.setValue(min(saved_scroll, scroll_bar.maximum()))
        self.pending_event_count += max(new_count, 0)
        self.new_events_button.setText(
            f"{self.pending_event_count} new event"
            f"{'s' if self.pending_event_count != 1 else ''}"
        )
        self.new_events_button.show()

    def show_new_events(self) -> None:
        cursor = self.events.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.events.setTextCursor(cursor)
        self.events.ensureCursorVisible()
        scroll_bar = self.events.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        self.pending_event_count = 0
        self.new_events_button.hide()

    def request_export(self) -> None:
        if not self.export_button.isEnabled():
            return
        self._export_busy = True
        self._update_history_actions()
        self._export_status = "Choosing a destination for the support report..."
        self._show_status()
        self.export_requested.emit()

    def set_export_started(self) -> None:
        self._export_busy = True
        self._update_history_actions()
        self.export_button.setEnabled(False)
        self._export_status = "Creating support report..."
        self._show_status()

    def set_export_result(self, successful: bool | None, message: str) -> None:
        self._export_busy = False
        self._update_history_actions()
        if successful is True:
            self._export_status = f"Support report saved to {message}"
        elif successful is False:
            self._export_status = (
                f"Support report export failed: {message}. "
                "Select Export report to retry."
            )
        else:
            self._export_status = message or "Support report export cancelled."
        self._show_status()


def _diagnostic_issue(
    entry: DiagnosticSessionEntry, current: dict[str, object] | None
) -> tuple[str, str]:
    report = entry.report or {}
    active = current is not None and current.get("session_id") == entry.session_id
    if active:
        report = current or report
    media_error = str(report.get("media_error") or "")
    if not media_error and report.get("media_status") in ("failed", "degraded"):
        media_error = f"Media recording: {report['media_status']}."
    session_error = str(report.get("error_type") or "")
    phases = {
        "recording": "Recording",
        "stopping": "Stopping",
        "finalizing": "Finishing",
    }
    phase = str(report.get("status", ""))
    if phase == "interrupted":
        return (
            "Interrupted",
            "Recording ended unexpectedly. Export includes the available evidence and attempts audio recovery; coverage may be incomplete.",
        )
    if active and phase in phases:
        reason = media_error or session_error
        return phases[phase] + (" (issue)" if reason else ""), reason
    if entry.saved is None:
        return "Unfinished" if phase in phases else "Unavailable", entry.error
    if media_error:
        return "Recording issue", media_error
    if session_error:
        return "Session issue", session_error
    if report.get("analysis_status") == "unavailable":
        return "Analysis issue", "Analysis is unavailable."
    return "", ""


def _diagnostic_files(counts: dict[str, int] | None) -> str:
    if counts is None:
        return "File inventory unavailable."
    if not counts:
        return "No files found."
    categories = {
        ".jsonl": "logs",
        ".mp4": "video",
        ".mov": "video",
        ".wav": "audio",
        ".m4a": "audio",
        ".caf": "audio",
        ".png": "images",
        ".jpg": "images",
    }
    totals: dict[str, int] = {}
    for suffix, count in counts.items():
        category = categories.get(suffix, "other files")
        totals[category] = totals.get(category, 0) + count
    return (
        "Files found: "
        + "; ".join(
            f"{name}: {totals[name]}"
            for name in ("logs", "video", "audio", "images", "other files")
            if name in totals
        )
        + "."
    )


def _diagnostic_date(value: object) -> str:
    try:
        return (
            datetime.fromisoformat(str(value))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S %Z")
        )
    except ValueError, OverflowError, OSError:
        return "Date unavailable"


def _diagnostic_size(value: int | None) -> str:
    if value is None:
        return "Unavailable"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{value} B"
        size /= 1000
    return "Unavailable"


def _support_value(value: object) -> str:
    text = str(value)
    return text if len(text) <= 500 else f"{text[:485]}... <truncated>"

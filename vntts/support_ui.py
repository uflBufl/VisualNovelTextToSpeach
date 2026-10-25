from datetime import datetime
from typing import Callable, Literal

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QHideEvent, QKeySequence, QShowEvent, QTextCursor, QTextOption
from PySide6.QtWidgets import (
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
    QSpinBox,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vntts import support
from vntts.action_icons import set_action_icon
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
        self._history_busy = False
        self._export_busy = False
        self._history_status = ""

        self.events = QTextEdit()
        self.events.setReadOnly(True)
        self.events.setAccessibleName("Runtime events")
        self.events.document().setDocumentMargin(8)
        self.events.setPlaceholderText("No runtime events recorded yet.")
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
            "Exports logs and diagnostics. Paths and log messages may contain "
            "personal information; review the ZIP before sharing."
        )
        self.privacy_note.setToolTip(
            "The report excludes screenshots, recognized dialogue, voice recordings "
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
        self.reading_media = QCheckBox(
            "Record selected game window and game/Rust audio (macOS)"
        )
        self.reading_media.setToolTip(
            "Opt-in: includes dialogue visible in the selected window, process audio and used source WAVs in the diagnostic export. Never records the desktop or microphone."
        )
        self.reading_seconds = QSpinBox()
        self.reading_seconds.setRange(1, 600)
        self.reading_seconds.setValue(600)
        self.reading_seconds.setSuffix(" s")
        self.reading_seconds.setAccessibleName("Diagnostic session duration limit")
        self.reading_start = QPushButton("Start session")
        self.reading_stop = QPushButton("Stop")
        self.reading_export = QPushButton("Export session")
        self.reading_delete = QPushButton("Delete session")
        self.reading_folder = QPushButton("Sessions folder")
        self.reading_history = QTreeWidget()
        self.reading_history.setAccessibleName("Saved diagnostic sessions")
        self.reading_history.setRootIsDecorated(False)
        self.reading_history.setMinimumHeight(130)
        self.reading_history.setHeaderLabels(
            ["Recorded (local time)", "Game / window", "Size"]
        )
        self.reading_history.setColumnWidth(0, 215)
        self.reading_history.setColumnWidth(2, 100)
        self.reading_history.header().setStretchLastSection(False)
        self.reading_history.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.reading_history.itemSelectionChanged.connect(self._update_history_actions)
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
        self.reading_status = QLabel(
            "No session. Logs exclude dialogue text. Media requires the checkbox above."
        )
        self.reading_status.setWordWrap(True)
        self.reading_status.setTextFormat(Qt.TextFormat.PlainText)
        self.reading_start.clicked.connect(
            lambda: self.reading_start_requested.emit(
                self.reading_media.isChecked(), self.reading_seconds.value()
            )
        )
        self.reading_stop.clicked.connect(self.reading_stop_requested.emit)
        self.reading_export.clicked.connect(self.request_session_export)
        self.reading_delete.clicked.connect(self.request_session_delete)
        self.reading_folder.clicked.connect(self.reading_folder_requested.emit)
        for button, icon, action in (
            (self.reading_start, "play", "Start diagnostic session"),
            (self.reading_stop, "square", "Stop diagnostic session"),
            (self.reading_export, "download", "Export diagnostic session"),
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
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
        layout.addLayout(footer)
        self.setTabOrder(self.events, self.new_events_button)
        self.setTabOrder(self.new_events_button, self.diagnostics_button)
        self.setTabOrder(self.diagnostics_button, self.settings_button)
        self.setTabOrder(self.settings_button, self.operation_status)
        self.setTabOrder(self.operation_status, self.export_button)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
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
        finished = report.get("status") == "finished"
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
            f"{report.get('media_error') or report.get('error_type') or ''}"
        )

    def selected_diagnostic_session(self) -> DiagnosticSessionEntry | None:
        item = self.reading_history.currentItem()
        return (
            self._diagnostic_sessions.get(item.data(0, Qt.ItemDataRole.UserRole))
            if item is not None
            else None
        )

    def set_diagnostic_sessions(self, entries: list[DiagnosticSessionEntry]) -> None:
        selected = self.selected_diagnostic_session()
        scroll = self.reading_history.verticalScrollBar().value()
        self._diagnostic_sessions = {entry.session_id: entry for entry in entries}
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
        if self.reading_history.currentItem() is None and first is not None:
            self.reading_history.setCurrentItem(first)
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
            and current.get("status") != "finished"
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

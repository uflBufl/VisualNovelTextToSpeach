from PySide6.QtCore import Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDesktopServices,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.onboarding import (
    DiagnosticResult,
    OnboardingDiagnostics,
    is_diagnostic_results,
)
from vntts.settings import AppSettings
from vntts.ui_text import copy_text_button, make_text_copyable


class ReadinessDialog(QDialog):
    reading_requested = Signal()
    settings_requested = Signal()
    permissions_requested = Signal()
    calibration_requested = Signal()
    voices_requested = Signal()
    refresh_requested = Signal()

    def __init__(
        self,
        settings: AppSettings,
        diagnostics: OnboardingDiagnostics,
        parent: QWidget | None = None,
        *,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.diagnostics = diagnostics
        self._results: tuple[DiagnosticResult, ...] = ()
        self._checks_running = False
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._checks_finished)
        self.setWindowTitle("Check readiness")
        self.resize(760, 470)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        summary_font = self.summary.font()
        summary_font.setBold(True)
        self.summary.setFont(summary_font)
        self.summary.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Status", "Component", "Details"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAccessibleName("Readiness results")
        self.table.setAccessibleDescription(
            "Select a check to see its exact remediation action"
        )
        table_palette = self.table.palette()
        dark = table_palette.color(QPalette.ColorRole.Base).lightness() < 128
        table_palette.setColor(
            QPalette.ColorRole.Highlight,
            QColor("#4a4f55" if dark else "#d5e0ea"),
        )
        table_palette.setColor(
            QPalette.ColorRole.HighlightedText,
            QColor("#ffffff" if dark else "#202020"),
        )
        self.table.setPalette(table_palette)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_remediation)
        self.selected_details = QPlainTextEdit()
        self.selected_details.setReadOnly(True)
        self.selected_details.setMaximumHeight(90)
        self.selected_details.setAccessibleName("Full details of the selected check")
        self.selected_details.hide()
        self.details_toggle = QCheckBox("Full check details")
        self.details_toggle.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.details_toggle.toggled.connect(self.selected_details.setVisible)
        self.copy_selected = copy_text_button(
            "Copy selected check", self._selected_text
        )
        self.copy_all = copy_text_button("Copy report", self._report_text)
        for secondary in (self.copy_selected, self.copy_all):
            secondary.setAutoDefault(False)
        self.copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self.table)
        self.copy_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.copy_shortcut.activated.connect(self.copy_selected.click)

        remediation = QHBoxLayout()
        self.remediation_reason = QLabel()
        self.remediation_reason.setWordWrap(True)
        self.remediation_reason.setAccessibleName("Selected readiness action status")
        remediation.addWidget(self.remediation_reason, 1)
        self.remediation_button = QPushButton("Fix selected issue")
        self.remediation_button.setAccessibleName("Fix selected readiness issue")
        self.remediation_button.clicked.connect(self._run_selected_remediation)
        self.remediation_button.hide()
        remediation.addWidget(self.remediation_button)
        controls = QHBoxLayout()
        controls.addWidget(self.copy_selected)
        controls.addWidget(self.copy_all)
        controls.addStretch()
        self.refresh_button = QPushButton("Run checks again")
        self.refresh_button.clicked.connect(self.refresh)
        controls.addWidget(self.refresh_button)
        self.cancel_button = QPushButton("Cancel checks")
        self.cancel_button.clicked.connect(self.cancel_checks)
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        controls.addWidget(self.cancel_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.reading_button = buttons.addButton(
            "Open Reading", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.reading_button.clicked.connect(self._open_reading)
        reading_font = self.reading_button.font()
        reading_font.setBold(True)
        self.reading_button.setFont(reading_font)
        self.reading_button.hide()
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.summary)
        layout.addWidget(self.progress)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.details_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.selected_details)
        layout.addLayout(remediation)
        layout.addLayout(controls)
        layout.addWidget(buttons)
        make_text_copyable(self)
        self.refresh()

    def _selected_text(self) -> str:
        result = self._selected_result()
        return (
            f"{result.status.upper()} | {result.name}\n{result.message}"
            if result
            else self.summary.text()
        )

    def _report_text(self) -> str:
        return (
            self.summary.text()
            + "\n\n"
            + "\n\n".join(
                f"{result.status.upper()} | {result.name}\n{result.message}"
                for result in self._results
            )
        )

    def update_settings(self, settings: AppSettings) -> None:
        self.settings = settings
        self.refresh()

    def refresh(self) -> None:
        self.runner.cancel()
        self._results = ()
        self.table.setRowCount(0)
        self.summary.setText("Running readiness checks...")
        self._set_checks_running(True)
        self._update_remediation()
        self.runner.start(self.diagnostics.run, self.settings)

    def cancel_checks(self) -> None:
        if not self.runner.cancel():
            return
        self.table.setRowCount(0)
        self._results = ()
        self._set_checks_running(False)
        self.summary.setText("Checks cancelled. No readiness result is active.")
        self._update_remediation()

    def _set_checks_running(self, running: bool) -> None:
        self._checks_running = running
        self.progress.setVisible(running)
        for widget in (self.copy_selected, self.copy_all, self.refresh_button):
            widget.setVisible(not running)
        self.refresh_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.cancel_button.setVisible(running)
        self.cancel_button.setDefault(running)
        self.reading_button.hide()

    def _checks_finished(self, results: object, error: Exception | None) -> None:
        self._set_checks_running(False)
        if error is not None or not is_diagnostic_results(results):
            self._results = ()
            self.table.setRowCount(0)
            reason = error if error is not None else "Readiness results are malformed"
            self.summary.setText(f"Checks failed: {reason}")
            self._update_remediation()
            return
        priority = {"error": 0, "warning": 1, "ok": 2}
        self._results = tuple(
            sorted(results, key=lambda result: priority[result.status])
        )
        self.table.setRowCount(len(results))
        dark = self.table.palette().color(QPalette.ColorRole.Base).lightness() < 128
        colors = {
            "ok": QColor("#75d894" if dark else "#287a3d"),
            "warning": QColor("#edc66f" if dark else "#9a6400"),
            "error": QColor("#ff929c" if dark else "#b3261e"),
        }
        for row, result in enumerate(self._results):
            message = result.message
            if message.startswith("Installed at "):
                message = "Installed and ready"
            elif message.startswith("Cached at "):
                message = "Downloaded and ready"
            elif message.startswith("Missing voice reference: "):
                message = (
                    "Voice recording missing: "
                    + message.replace("\\", "/").rsplit("/", 1)[-1]
                )
            values = (result.status.upper(), result.name, message)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(result.message if column == 2 else value)
                if column == 0:
                    item.setForeground(colors[result.status])
                self.table.setItem(row, column, item)
        errors = sum(result.status == "error" for result in results)
        warnings = sum(result.status == "warning" for result in results)
        if errors:
            self.summary.setText(
                f"Cannot start reading. Errors: {errors}."
                + (f" Warnings: {warnings}." if warnings else "")
            )
        elif warnings:
            self.summary.setText(
                f"Ready for live reading. Warnings: {warnings}; fallback available."
            )
        else:
            self.summary.setText("Ready for live reading.")
        self.reading_button.setVisible(errors == 0)
        if self._results:
            self.table.selectRow(0)
            self.table.scrollToTop()
        self._update_remediation()
        self.refresh_requested.emit()

    def _open_reading(self) -> None:
        self.reading_requested.emit()
        self.close()

    def _selected_result(self) -> DiagnosticResult | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._results):
            return None
        return self._results[row]

    def _update_remediation(self) -> None:
        self.selected_details.setPlainText(self._selected_text())
        result = self._selected_result()
        has_result = not self._checks_running and result is not None
        if not has_result:
            self.details_toggle.setChecked(False)
        self.details_toggle.setVisible(has_result)
        self.selected_details.setVisible(has_result and self.details_toggle.isChecked())
        self.copy_selected.setEnabled(not self._checks_running and result is not None)
        self.copy_all.setEnabled(not self._checks_running and bool(self._results))
        self.remediation_button.setEnabled(False)
        self.remediation_button.setDefault(False)
        self.refresh_button.setDefault(not self._checks_running)
        self.reading_button.setDefault(not self.reading_button.isHidden())
        self.remediation_button.hide()
        self.remediation_reason.hide()
        if self._checks_running:
            self.remediation_reason.setText(
                "Wait for the readiness checks to finish before opening a fix."
            )
            return
        if result is None:
            self.remediation_reason.setText(
                "Select a warning or error to see whether VNTTS can open its fix."
            )
            return
        if result.status == "ok":
            self.remediation_reason.setText(
                f"{result.name} is ready; no remediation is needed."
            )
            return
        self.remediation_reason.show()
        if result.remediation == "external-ocr":
            language_names = {
                "eng": "English",
                "jpn": "Japanese",
                "rus": "Russian",
                "deu": "German",
                "fra": "French",
                "spa": "Spanish",
                "kor": "Korean",
                "chi_sim": "Simplified Chinese",
                "chi_tra": "Traditional Chinese",
            }
            language_name = (
                ", ".join(
                    f"{language_names.get(code, 'selected OCR language')} ({code})"
                    for code in self.settings.ocr_language.split("+")
                    if code
                )
                or "your selected OCR language"
            )
            self.remediation_reason.setText(
                "Install Tesseract OCR (the text-recognition app) with language "
                f"data for {language_name}. Add the folder containing Tesseract to your "
                "system's app search path (PATH). "
                "Restart VNTTS, then run checks again."
            )
            self.remediation_button.setText("Tesseract install guide")
            self.remediation_button.setAccessibleName("Open Tesseract install guide")
            self.remediation_button.setEnabled(True)
            self.remediation_button.setDefault(result.status == "error")
            self.remediation_button.show()
            return
        if result.remediation == "external-audio":
            self.remediation_reason.setText(
                "Connect an audio output device and select it in system sound "
                "settings, then run checks again."
            )
            return
        actions = {
            "settings": "Open Settings",
            "permissions": "Open Permissions",
            "calibration": "Open Calibration",
            "voices": "Open Voices",
        }
        remediation = result.remediation
        label = actions.get(remediation) if remediation is not None else None
        if label is None:
            self.remediation_reason.setText(
                f"No automatic fix for {result.name}. Review the full details, "
                "then run checks again; copy the report if you need support."
            )
            return
        self.remediation_button.setText(label)
        self.remediation_button.setAccessibleName(label)
        self.remediation_button.setEnabled(True)
        self.remediation_button.setDefault(result.status == "error")
        self.remediation_button.show()
        if result.status == "warning":
            blocker = any(item.status == "error" for item in self._results)
            self.remediation_reason.setText(
                f"{result.name} has a fallback. "
                + (
                    "Resolve the errors before reading. "
                    if blocker
                    else "Reading can continue. "
                )
                + "Changing this fallback is optional."
            )
        else:
            self.remediation_reason.setText(
                f"{result.name}: fix this check, then run checks again."
            )

    def _run_selected_remediation(self) -> None:
        result = self._selected_result()
        if result is None or result.status == "ok":
            return
        if result.remediation == "external-ocr":
            if not QDesktopServices.openUrl(
                QUrl("https://tesseract-ocr.github.io/tessdoc/Installation.html")
            ):
                self.remediation_reason.setText(
                    "Could not open the installation guide. Visit "
                    "https://tesseract-ocr.github.io/tessdoc/Installation.html "
                    "in a browser, then run checks again."
                )
            return
        signals = {
            "settings": self.settings_requested,
            "permissions": self.permissions_requested,
            "calibration": self.calibration_requested,
            "voices": self.voices_requested,
        }
        remediation = result.remediation
        signal = signals.get(remediation) if remediation is not None else None
        if signal is not None:
            self.close()
            signal.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.runner.cancel()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self.runner.cancel()
        super().done(result)

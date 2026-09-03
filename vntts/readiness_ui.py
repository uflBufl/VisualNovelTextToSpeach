from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from vntts.async_ui import LatestTaskRunner


class ReadinessDialog(QDialog):
    settings_requested = Signal()
    permissions_requested = Signal()
    calibration_requested = Signal()
    voices_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, settings, diagnostics, parent=None, *, thread_pool=None):
        super().__init__(parent)
        self.settings = settings
        self.diagnostics = diagnostics
        self._results = ()
        self._checks_running = False
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._checks_finished)
        self.setWindowTitle("Check readiness")
        self.resize(820, 500)

        intro = QLabel(
            "Check the complete OCR-to-speech path before starting live reading. "
            "Warnings are usable fallbacks; errors need attention."
        )
        intro.setWordWrap(True)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Status", "Component", "Details"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAccessibleName("Readiness results")
        self.table.setAccessibleDescription(
            "Select a check to see its exact remediation action"
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_remediation)

        remediation = QHBoxLayout()
        self.remediation_reason = QLabel()
        self.remediation_reason.setWordWrap(True)
        self.remediation_reason.setAccessibleName("Selected readiness action status")
        remediation.addWidget(self.remediation_reason, 1)
        self.remediation_button = QPushButton("Fix selected issue")
        self.remediation_button.setAccessibleName("Fix selected readiness issue")
        self.remediation_button.clicked.connect(self._run_selected_remediation)
        remediation.addWidget(self.remediation_button)
        controls = QHBoxLayout()
        controls.addStretch()
        self.refresh_button = QPushButton("Run checks again")
        self.refresh_button.clicked.connect(self.refresh)
        controls.addWidget(self.refresh_button)
        self.cancel_button = QPushButton("Cancel checks")
        self.cancel_button.clicked.connect(self.cancel_checks)
        self.cancel_button.setEnabled(False)
        controls.addWidget(self.cancel_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.summary)
        layout.addWidget(self.progress)
        layout.addWidget(self.table, 1)
        layout.addLayout(remediation)
        layout.addLayout(controls)
        layout.addWidget(buttons)
        self.refresh()

    def update_settings(self, settings):
        self.settings = settings
        self.refresh()

    def refresh(self):
        self.runner.cancel()
        self._results = ()
        self._checks_running = True
        self.table.setRowCount(0)
        self.summary.setText("Running readiness checks...")
        self.progress.show()
        self.refresh_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._update_remediation()
        self.runner.start(self.diagnostics.run, self.settings)

    def cancel_checks(self):
        if not self.runner.cancel():
            return
        self.table.setRowCount(0)
        self._results = ()
        self._checks_running = False
        self.progress.hide()
        self.refresh_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.summary.setText("Checks cancelled. No readiness result is active.")
        self._update_remediation()

    def _checks_finished(self, results, error):
        self._checks_running = False
        self.progress.hide()
        self.refresh_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if error is not None:
            self._results = ()
            self.table.setRowCount(0)
            self.summary.setText(f"Checks failed: {error}")
            self._update_remediation()
            return
        self._results = tuple(results)
        self.table.setRowCount(len(results))
        colors = {
            "ok": QColor("#287a3d"),
            "warning": QColor("#9a6400"),
            "error": QColor("#b3261e"),
        }
        for row, result in enumerate(results):
            values = (result.status.upper(), result.name, result.message)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setForeground(colors[result.status])
                self.table.setItem(row, column, item)
        errors = sum(result.status == "error" for result in results)
        warnings = sum(result.status == "warning" for result in results)
        if errors:
            self.summary.setText(
                f"Not ready: {errors} error(s), {warnings} warning(s)."
            )
        elif warnings:
            self.summary.setText(f"Ready with {warnings} warning(s).")
        else:
            self.summary.setText("Ready to play. All checks passed.")
        preferred_row = next(
            (
                row
                for row, result in enumerate(self._results)
                if result.status == "error" and result.remediation is not None
            ),
            None,
        )
        if preferred_row is None:
            preferred_row = next(
                (
                    row
                    for row, result in enumerate(self._results)
                    if result.status == "warning" and result.remediation is not None
                ),
                0 if self._results else None,
            )
        if preferred_row is not None:
            self.table.selectRow(preferred_row)
        self._update_remediation()
        self.refresh_requested.emit()

    def _selected_result(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._results):
            return None
        return self._results[row]

    def _update_remediation(self):
        self.remediation_button.setEnabled(False)
        self.remediation_button.setText("Fix selected issue")
        if self._checks_running:
            self.remediation_reason.setText(
                "Wait for the readiness checks to finish before opening a fix."
            )
            return
        result = self._selected_result()
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
        actions = {
            "settings": "Open Settings",
            "permissions": "Open Permissions",
            "calibration": "Open Calibration",
            "voices": "Open Voice mappings",
        }
        label = actions.get(result.remediation)
        if label is None:
            self.remediation_reason.setText(
                f"No in-app fix is available for {result.name}. Follow the "
                "selected check's details, then run the checks again."
            )
            return
        self.remediation_button.setText(label)
        self.remediation_button.setEnabled(True)
        self.remediation_reason.setText(
            f"{result.name} is {result.status}. {label} to address this check."
        )

    def _run_selected_remediation(self):
        result = self._selected_result()
        if result is None or result.status == "ok":
            return
        signals = {
            "settings": self.settings_requested,
            "permissions": self.permissions_requested,
            "calibration": self.calibration_requested,
            "voices": self.voices_requested,
        }
        signal = signals.get(result.remediation)
        if signal is not None:
            signal.emit()

    def closeEvent(self, event: QCloseEvent):
        self.runner.cancel()
        super().closeEvent(event)

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
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
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._checks_finished)
        self.setWindowTitle("Ready to play")
        self.resize(820, 500)

        intro = QLabel(
            "Check the complete OCR-to-speech path before starting the game. "
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
        self.table.horizontalHeader().setStretchLastSection(True)

        actions = QHBoxLayout()
        for text, signal in (
            ("Settings", self.settings_requested),
            ("Permissions", self.permissions_requested),
            ("Calibrate", self.calibration_requested),
            ("Voice mappings", self.voices_requested),
        ):
            button = QPushButton(text)
            button.clicked.connect(signal.emit)
            actions.addWidget(button)
        actions.addStretch()
        self.refresh_button = QPushButton("Run checks again")
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        self.cancel_button = QPushButton("Cancel checks")
        self.cancel_button.clicked.connect(self.cancel_checks)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.cancel_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.summary)
        layout.addWidget(self.progress)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        layout.addWidget(buttons)
        self.refresh()

    def update_settings(self, settings):
        self.settings = settings
        self.refresh()

    def refresh(self):
        self.runner.cancel()
        self.table.setRowCount(0)
        self.summary.setText("Running readiness checks...")
        self.progress.show()
        self.refresh_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.runner.start(self.diagnostics.run, self.settings)

    def cancel_checks(self):
        if not self.runner.cancel():
            return
        self.table.setRowCount(0)
        self.progress.hide()
        self.refresh_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.summary.setText("Checks cancelled. No readiness result is active.")

    def _checks_finished(self, results, error):
        self.progress.hide()
        self.refresh_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if error is not None:
            self.table.setRowCount(0)
            self.summary.setText(f"Checks failed: {error}")
            return
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
        self.refresh_requested.emit()

    def closeEvent(self, event: QCloseEvent):
        self.runner.cancel()
        super().closeEvent(event)

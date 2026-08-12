from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class ReadinessDialog(QDialog):
    settings_requested = Signal()
    permissions_requested = Signal()
    calibration_requested = Signal()
    voices_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, settings, diagnostics, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.diagnostics = diagnostics
        self.setWindowTitle("Ready to play")
        self.resize(820, 500)

        intro = QLabel(
            "Check the complete OCR-to-speech path before starting the game. "
            "Warnings are usable fallbacks; errors need attention."
        )
        intro.setWordWrap(True)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
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
        refresh = QPushButton("Run checks again")
        refresh.clicked.connect(self.refresh)
        actions.addWidget(refresh)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        layout.addWidget(buttons)
        self.refresh()

    def update_settings(self, settings):
        self.settings = settings
        self.refresh()

    def refresh(self):
        results = self.diagnostics.run(self.settings)
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

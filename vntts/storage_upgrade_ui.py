"""Upgrade saved data before the desktop starts its storage consumers."""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
)

from vntts.async_ui import LatestTaskRunner
from vntts.storage_upgrade import storage_upgrade_required, upgrade_storage


class StorageUpgradeDialog(QDialog):
    def __init__(self, root: Path, *, thread_pool: QThreadPool | None = None) -> None:
        super().__init__()
        self.root = root
        self.backup = root.with_name(root.name + "-storage-backup")
        self.completed = False
        self.setWindowTitle("Update saved preparations")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        self.description = QLabel(
            "Saved preparations need an update before you can continue preparing "
            "stories. The update checks your existing data and verifies a separate "
            "backup before making changes. Existing recordings are retained."
            " Close other application instances and generation tools before updating."
        )
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        backup_label = QLabel(f"Backup folder: {self.backup}")
        backup_label.setWordWrap(True)
        backup_label.setTextFormat(Qt.TextFormat.PlainText)
        backup_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(backup_label)
        self.status = QLabel("Choose Update now to keep using your saved preparations.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setAccessibleName("Saved preparation update status")
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        buttons = QDialogButtonBox()
        self.update_button = buttons.addButton(
            "Update now", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.later_button = buttons.addButton(
            "Later", QDialogButtonBox.ButtonRole.RejectRole
        )
        self.update_button.setDefault(True)
        self.update_button.clicked.connect(self._update)
        self.later_button.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._finished)

    def _update(self) -> None:
        if self.completed:
            self.accept()
            return
        if self.runner.active:
            return
        self.update_button.setEnabled(False)
        self.later_button.setEnabled(False)
        self.progress.show()
        self.status.setText(
            "Checking saved data, verifying the backup and updating preparations. "
            "Please keep the application open."
        )
        self.runner.start(upgrade_storage, self.root, self.backup, apply=True)

    def _finished(self, result: object, error: Exception | None) -> None:
        self.progress.hide()
        self.update_button.setEnabled(True)
        self.later_button.setEnabled(True)
        if error is not None:
            self.status.setText(
                f"Unable to update saved preparations: {error}\n"
                f"Backup folder: {self.backup}\n"
                "You can retry, or choose Later and export a support report."
            )
            self.update_button.setText("Retry update")
            return
        self.completed = True
        self.description.setText(
            "Saved preparations are ready. Continue to the application to select "
            "your stories or resume an existing preparation."
        )
        self.status.setText(f"Saved preparations are updated. Backup: {self.backup}")
        self.update_button.setText("Continue")
        self.later_button.hide()

    def reject(self) -> None:
        if not self.runner.active:
            super().reject()


def offer_storage_upgrade(root: Path) -> None:
    try:
        required = storage_upgrade_required(root)
    except OSError, ValueError:
        logging.getLogger(__name__).exception(
            "Unable to check saved preparation storage for an upgrade"
        )
        return
    if required:
        dialog = StorageUpgradeDialog(root)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

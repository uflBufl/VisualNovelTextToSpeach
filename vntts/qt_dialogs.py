"""Qt dialog rejection that respects each owner's close guards."""

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QDialog


class CloseGuardedDialog(QDialog):
    """Route Escape and rejection through the dialog's closeEvent safeguards."""

    def reject(self) -> None:
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        super().reject()
        event.accept()

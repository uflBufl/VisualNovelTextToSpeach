"""One-time preparation of the original Pocket model."""

from threading import Event

from PySide6.QtCore import QEvent, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from vntts.async_ui import LatestTaskRunner
from vntts.pocket_model import pocket_model_ready, prepare_pocket_model


class _SetupSignals(QObject):
    progress = Signal(str)
    authorize = Signal(str, str)


def _emit(signals: _SetupSignals, signal: str, *arguments: str) -> None:
    # A cancelled worker can outlive its dialog or QApplication.
    if isValid(signals):
        try:
            getattr(signals, signal).emit(*arguments)
        except RuntimeError:
            if isValid(signals):
                raise


class _PocketSetupDialog(QDialog):
    ready = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Prepare Pocket voice cloning")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._owner = parent.window()
        self._owner.installEventFilter(self)
        self._url = ""
        self.status = QLabel("Preparing the original Pocket model...")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.code = QLabel()
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code.setAccessibleName("Hugging Face sign-in code")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.browser_button = QPushButton("Open browser")
        self.browser_button.clicked.connect(self._open_browser)
        self.browser_button.hide()
        self.continue_button = QPushButton("Continue after accepting terms")
        self.continue_button.clicked.connect(self._continue)
        self.continue_button.hide()
        self.retry_button = QPushButton("Retry")
        self.retry_button.clicked.connect(self._retry)
        self.retry_button.hide()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.browser_button)
        buttons.addWidget(self.continue_button)
        buttons.addWidget(self.retry_button)
        buttons.addStretch()
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.code)
        layout.addWidget(self.progress)
        layout.addLayout(buttons)
        self.runner = LatestTaskRunner(self)
        self.runner.finished.connect(self._finished)
        self._start()

    def _start(self) -> None:
        self.cancel_event = Event()
        self.continue_event = Event()
        self.destroyed.connect(self.cancel_event.set)
        self._url = ""
        self.status.setText("Preparing the original Pocket model...")
        self.code.clear()
        self.progress.show()
        self.browser_button.hide()
        self.continue_button.hide()
        self.retry_button.hide()
        self.cancel_button.setText("Cancel")
        signals = _SetupSignals()
        signals.progress.connect(self._progress)
        signals.authorize.connect(self._authorize)
        self._signals = signals
        self.runner.start(
            prepare_pocket_model,
            cancel_event=self.cancel_event,
            continue_event=self.continue_event,
            progress=lambda message: _emit(signals, "progress", message),
            authorize=lambda url, code: _emit(signals, "authorize", url, code),
        )

    def _retry(self) -> None:
        if self.cancel_event.is_set() or self.runner.active:
            return
        self.cancel_event.set()
        self._start()

    def _progress(self, message: str) -> None:
        if (
            self.sender() is self._signals
            and self.runner.active
            and not self.cancel_event.is_set()
        ):
            self.status.setText(message)

    def _authorize(self, url: str, code: str) -> None:
        if (
            self.sender() is not self._signals
            or not self.runner.active
            or self.cancel_event.is_set()
        ):
            return
        self._url = url
        self.code.setText(f"Sign-in code: {code}" if code else "")
        self.status.setText(
            "Sign in to Hugging Face in your browser using this code."
            if code
            else "Accept the original Pocket model terms in your browser, then continue."
        )
        self.continue_button.setVisible(not code)
        self.browser_button.show()
        self._open_browser()

    def _open_browser(self) -> None:
        if self._url and not QDesktopServices.openUrl(QUrl(self._url)):
            self.status.setText(
                f"Unable to open your browser. Open this address to continue: {self._url}"
            )

    def _continue(self) -> None:
        self.continue_button.hide()
        self.continue_event.set()
        self.status.setText("Checking access and preparing the original model...")

    def _finished(self, _result: object, error: Exception | None) -> None:
        if self.cancel_event.is_set():
            return
        self.progress.hide()
        self.browser_button.hide()
        self.continue_button.hide()
        self.code.clear()
        self.cancel_button.setText("Close")
        if error is not None:
            self.status.setText(f"Unable to prepare Pocket voice cloning: {error}")
            self.retry_button.show()
        elif pocket_model_ready():
            self.status.setText("Pocket voice cloning is ready on this computer.")
            self.ready.emit()
        else:
            self.status.setText(
                "Pocket setup did not finish. Retry to prepare voice cloning."
            )
            self.retry_button.show()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._owner and event.type() in (
            QEvent.Type.Close,
            QEvent.Type.Hide,
        ):
            self.close()
        return super().eventFilter(watched, event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.cancel_event.set()
        self.runner.cancel()
        super().closeEvent(event)

    def reject(self) -> None:
        self.cancel_event.set()
        self.runner.cancel()
        super().reject()


class PocketSetupControl(QWidget):
    readyChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ready = False
        self._dialog: _PocketSetupDialog | None = None
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.button = QPushButton("Prepare voice cloning...")
        self.button.setAccessibleDescription(
            "Sign in through your browser, accept Pocket model terms and download "
            "the original model once. Built-in voices need no account."
        )
        self.button.clicked.connect(self._open_setup)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.status, 1)
        layout.addWidget(self.button)
        self.refresh()

    @property
    def is_ready(self) -> bool:
        return self._ready

    def refresh(self) -> None:
        ready = pocket_model_ready()
        changed = self._ready != ready
        self._ready = ready
        self.status.setText(
            "Voice cloning is ready."
            if ready
            else "Built-in voices are ready. Prepare the original model to clone voices."
        )
        self.button.setEnabled(not ready)
        if changed:
            self.readyChanged.emit(ready)

    def showEvent(self, event: QShowEvent) -> None:
        self.refresh()
        super().showEvent(event)

    def _open_setup(self) -> None:
        if (
            self._dialog is not None
            and isValid(self._dialog)
            and not self._dialog.cancel_event.is_set()
        ):
            self._dialog.show()
            self._dialog.raise_()
            return
        self._dialog = _PocketSetupDialog(self)
        self._dialog.ready.connect(self.refresh)
        self._dialog.show()

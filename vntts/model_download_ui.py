"""Shared model preparation and progress in the native Qt interface."""

import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.huggingface_assets import (
    AssetProgress,
    SnapshotSize,
    download_snapshot,
    inspect_snapshot,
)
from vntts.model_activity import begin_model_activity, get_model_activity
from vntts.model_assets import ModelAssetManager
from vntts.qwen_backend import (
    QWEN_CUDA_MODEL,
    QWEN_CUDA_REVISION,
    QWEN_MODEL,
    QWEN_REVISION,
)

QWEN_BACKEND = "qwen-tts"


class ModelDownloadCancelled(RuntimeError):
    """Download was cancelled at a transfer boundary."""


@dataclass(frozen=True)
class ModelDownloadState:
    title: str
    detail: str = ""
    percent: int | None = None
    busy: bool = False
    action: str = ""


def _format_bytes(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GiB"
    if value >= 1024**2:
        return f"{value / 1024**2:.0f} MiB"
    if value >= 1024:
        return f"{value / 1024:.0f} KiB"
    return f"{value} B"


class QwenModelSetup(QObject):
    """One app-owned download for the pinned model used by all Qt screens."""

    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        source, revision = (
            (QWEN_CUDA_MODEL, QWEN_CUDA_REVISION)
            if sys.platform == "win32"
            else (QWEN_MODEL, QWEN_REVISION)
        )
        environment = ModelAssetManager().huggingface_environment()
        self.source = source
        self.revision = revision
        self.cache_dir = Path(
            environment.get("HF_HUB_CACHE")
            or environment.get("HUGGINGFACE_HUB_CACHE")
            or Path(environment["HF_HOME"]) / "hub"
        ).expanduser()
        self.runner = LatestTaskRunner(self)
        self.runner.finished.connect(self._finished)
        self._snapshot: SnapshotSize | None = None
        self._checked = False
        self._ready = False
        self._operation: str | None = None
        self._issue = ""
        self._closing = False
        self._cancel = Event()
        self._last_activity: AssetProgress | None = None
        self._dialog: QDialog | None = None
        self._temporary_dialog = False
        self._timer = QTimer(self, interval=250)
        self._timer.timeout.connect(self._refresh_activity)
        self._timer.start()

    def _refresh_activity(self) -> None:
        value = get_model_activity(QWEN_BACKEND)
        if value != self._last_activity:
            finished_loading = (
                value is None
                and self._last_activity is not None
                and self._last_activity.phase == "loading"
            )
            self._last_activity = value
            if (
                value is not None
                and value.phase in {"downloading", "loading"}
                and self._operation is None
                and self._dialog is None
            ):
                self.open_dialog(temporary=True)
            if value is None and self._temporary_dialog and self._dialog is not None:
                self._dialog.close()
            if finished_loading and not self._ready and self._operation is None:
                self._checked = False
                self.check()
            self.changed.emit()

    def check(self) -> None:
        if self._closing or self._checked or self.runner.active:
            return
        self._issue = ""
        self._operation = "check"
        self.runner.start(
            inspect_snapshot,
            self.source,
            revision=self.revision,
            cache_dir=self.cache_dir,
        )
        self.changed.emit()

    def is_ready_for_use(self, backend: str | None) -> bool:
        return backend != QWEN_BACKEND or self._ready

    def ready_for_use(self, backend: str | None) -> bool:
        if self.is_ready_for_use(backend):
            return True
        self.open_dialog()
        return False

    def open_dialog(self, *, temporary: bool = False) -> None:
        if self._closing:
            return
        if self._dialog is None:
            dialog = QDialog()
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.setWindowTitle("Models")
            dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
            dialog.setMinimumWidth(440)
            layout = QVBoxLayout(dialog)
            layout.addWidget(ModelDownloadPanel(self, dialog))
            self._dialog = dialog
            self._temporary_dialog = temporary
            dialog.finished.connect(self._dialog_closed)
        elif not temporary:
            self._temporary_dialog = False
        if not temporary:
            self.check()
        self._dialog.show()
        self._dialog.raise_()

    def _dialog_closed(self) -> None:
        self._dialog = None
        self._temporary_dialog = False

    def action(self) -> None:
        if self._operation == "download":
            self._cancel.set()
            self.changed.emit()
        elif not self._checked:
            self.check()
        elif not self._ready and not self.runner.active:
            if get_model_activity(QWEN_BACKEND) is not None:
                return
            self._operation = "download"
            self._issue = ""
            self._cancel = Event()
            self.runner.start(self._download, self._cancel)
            self.changed.emit()

    def shutdown(self) -> None:
        self._closing = True
        self._timer.stop()
        self._cancel.set()
        self.runner.cancel()
        if self._dialog is not None:
            self._dialog.close()

    def _download(self, cancelled: Event) -> str:
        owner = begin_model_activity(
            QWEN_BACKEND, AssetProgress("downloading", "Qwen3-TTS model")
        )

        def progress(value: AssetProgress) -> None:
            if cancelled.is_set():
                raise ModelDownloadCancelled("Download stopped")
            owner.report(value)

        try:
            return download_snapshot(
                self.source,
                revision=self.revision,
                cache_dir=self.cache_dir,
                asset="Qwen3-TTS model",
                progress=progress,
            )
        finally:
            owner.close()

    def _finished(self, result: object, error: Exception | None) -> None:
        operation = self._operation
        self._operation = None
        if operation == "check":
            self._checked = True
            if isinstance(result, SnapshotSize):
                self._snapshot = result
                self._ready = result.cached_files == result.total_files > 0
            else:
                self._issue = "Download size is unavailable."
        elif operation == "download":
            self._ready = error is None and isinstance(result, str)
            if not self._ready:
                self._checked = False
                self._snapshot = None
                self.check()
                self._issue = (
                    "Download stopped. Saved files can be reused."
                    if self._cancel.is_set()
                    else "Download failed. Check your connection and try again."
                )
        self.changed.emit()

    def state(self) -> ModelDownloadState:
        activity = get_model_activity(QWEN_BACKEND)
        if self._operation == "download" and self._cancel.is_set():
            return ModelDownloadState("Stopping Qwen3-TTS download", busy=True)
        if activity is not None:
            state = self._activity_state(activity)
            if self._operation == "download":
                return ModelDownloadState(
                    state.title, state.detail, state.percent, True, "Stop download"
                )
            return state
        if self._operation == "download":
            return ModelDownloadState("Preparing Qwen3-TTS download", busy=True)
        if self._operation == "check":
            return ModelDownloadState("Checking Qwen3-TTS model files", busy=True)
        size = self._snapshot
        if self._ready:
            detail = (
                f"{_format_bytes(size.total_bytes)} on disk."
                if size is not None and size.total_bytes is not None
                else ""
            )
            return ModelDownloadState("Qwen3-TTS model ready on disk", detail)
        if (
            size is not None
            and size.total_bytes is not None
            and size.cached_bytes is not None
        ):
            detail = (
                f"{_format_bytes(size.total_bytes)} total · "
                f"{_format_bytes(size.cached_bytes)} on disk · "
                f"{_format_bytes(size.total_bytes - size.cached_bytes)} to download"
            )
        else:
            detail = ""
        if self._issue:
            detail = f"{detail}\n{self._issue}".strip()
        if not self._checked:
            return ModelDownloadState(
                "Qwen3-TTS model files", detail, action="Check size"
            )
        action = (
            "Download model"
            if size and size.remaining_bytes is not None
            else "Download without size estimate"
        )
        return ModelDownloadState("Qwen3-TTS needs model files", detail, action=action)

    @staticmethod
    def _activity_state(activity: AssetProgress) -> ModelDownloadState:
        if activity.phase == "loading":
            return ModelDownloadState("Loading Qwen3-TTS into memory", busy=True)
        if activity.phase == "verifying":
            return ModelDownloadState("Checking downloaded Qwen3-TTS files", busy=True)
        if activity.phase != "downloading":
            return ModelDownloadState("Checking Qwen3-TTS model files", busy=True)
        received = ""
        if activity.completed_bytes is not None and activity.total_bytes:
            done, total = activity.completed_bytes, activity.total_bytes
            detail = f"{_format_bytes(done)} of {_format_bytes(total)} transferred"
        else:
            if activity.completed_bytes is not None:
                received = f"{_format_bytes(activity.completed_bytes)} transferred"
            if activity.completed_files is None or not activity.total_files:
                return ModelDownloadState("Downloading Qwen3-TTS", received, busy=True)
            done, total = activity.completed_files, activity.total_files
            detail = f"{done} of {total} files available"
            if received:
                detail = f"{detail} · {received}"
        return ModelDownloadState(
            "Downloading Qwen3-TTS", detail, min(100, round(done * 100 / total)), True
        )


class ModelDownloadPanel(QWidget):
    def __init__(self, setup: QwenModelSetup, parent: QDialog) -> None:
        super().__init__(parent)
        self.setup = setup
        self._previous: ModelDownloadState | None = None
        self.phase_label = QLabel(wordWrap=True)
        font = self.phase_label.font()
        font.setBold(True)
        self.phase_label.setFont(font)
        self.phase_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail = QLabel(wordWrap=True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Model download progress")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(parent.close)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        close.setAutoDefault(False)
        self.button = buttons.addButton("", QDialogButtonBox.ButtonRole.ActionRole)
        self.button.setAutoDefault(False)
        self.button.clicked.connect(setup.action)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.phase_label)
        layout.addWidget(self.detail)
        layout.addWidget(self.progress)
        layout.addWidget(buttons)
        self.setup.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        state = self.setup.state()
        if state == self._previous:
            return
        self._previous = state
        self.phase_label.setText(state.title)
        self.detail.setText(state.detail)
        self.detail.setVisible(bool(state.detail))
        self.button.setText(state.action)
        self.button.setVisible(bool(state.action))
        self.button.setDefault(bool(state.action) and not state.busy)
        self.progress.setRange(0, 0 if state.percent is None else 100)
        if state.percent is not None:
            self.progress.setValue(state.percent)
        self.progress.setTextVisible(False)
        self.progress.setVisible(state.busy)

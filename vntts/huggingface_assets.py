"""Report Hub transfers from its own progress events without inspecting caches."""

import logging
from collections.abc import Callable
from threading import Lock
from typing import Any


def download_snapshot(
    source: str,
    *,
    asset: str,
    report: Callable[[str], None] | None = None,
    **options: Any,
) -> str:
    from huggingface_hub import snapshot_download

    if report is None:
        return str(snapshot_download(source, **options))
    from huggingface_hub.utils.tqdm import tqdm

    report(f"Resolving {asset}...")
    reported = False
    lock = Lock()

    def downloading() -> None:
        nonlocal reported
        with lock:
            if not reported:
                report(f"Downloading {asset}...")
                reported = True

    class DownloadProgress(tqdm):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.byte_progress = kwargs.get("unit") == "B"
            super().__init__(*args, **kwargs)

        def refresh(self, *args: Any, **kwargs: Any) -> bool | None:
            # The Hub assigns a byte total only when a file needs fetching.
            if self.byte_progress and self.total:
                downloading()
            refreshed: bool | None = super().refresh(*args, **kwargs)
            return refreshed

    logger = logging.getLogger("huggingface_hub.file_download")
    previous_level = logger.level
    previous_effective_level = logger.getEffectiveLevel()

    class DownloadLogProgress(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            message = record.getMessage()
            # Hub 0.36 reports transfers here, but passes tqdm_class only file counts.
            if message.startswith("Downloading '") and "' to '" in message:
                downloading()
            # Preserve the existing verbosity while observing newly enabled INFO events.
            return record.levelno >= previous_effective_level

    progress_filter = DownloadLogProgress()
    logger.addFilter(progress_filter)
    logger.setLevel(min(previous_effective_level, logging.INFO))
    try:
        return str(snapshot_download(source, tqdm_class=DownloadProgress, **options))
    finally:
        logger.removeFilter(progress_filter)
        logger.setLevel(previous_level)

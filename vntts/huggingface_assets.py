"""Inspect pinned Hub snapshots and report the Hub's own transfer events."""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any

from durable_file import atomic_write_bytes

from vntts.path_safety import open_regular_binary


@dataclass(frozen=True)
class SnapshotSize:
    revision: str
    total_bytes: int | None
    cached_bytes: int | None
    remaining_bytes: int | None
    total_files: int
    cached_files: int


@dataclass(frozen=True)
class AssetProgress:
    phase: str
    asset: str
    completed_bytes: int | None = None
    total_bytes: int | None = None
    completed_files: int | None = None
    total_files: int | None = None
    reconstructed_bytes: int | None = None


@dataclass(frozen=True)
class _SnapshotFile:
    rfilename: str
    size: int | None


def _metadata(
    source: str,
    revision: str,
    metadata_dir: Path,
    timeout: float,
    token: str | bool | None,
) -> tuple[str, list[_SnapshotFile]]:
    from huggingface_hub import HfApi

    pinned = re.fullmatch(r"[0-9a-f]{40}", revision) is not None
    manifest = (
        metadata_dir / f"{sha256(f'{source}@{revision}'.encode()).hexdigest()}.json"
    )
    if pinned:
        try:
            with open_regular_binary(manifest) as stream:
                raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("Snapshot metadata is too large")
            data = json.loads(raw)
            if (
                data["schema_version"] != 1
                or data["source"] != source
                or data["revision"] != revision
                or not isinstance(data["files"], list)
            ):
                raise ValueError("Snapshot metadata does not match the pinned model")
            files = []
            for filename, size in data["files"]:
                if (
                    not isinstance(filename, str)
                    or not filename
                    or "\\" in filename
                    or PurePosixPath(filename).is_absolute()
                    or ".." in PurePosixPath(filename).parts
                    or (size is not None and (type(size) is not int or size < 0))
                ):
                    raise ValueError("Invalid snapshot metadata")
                files.append(_SnapshotFile(filename, size))
            return revision, files
        except OSError, ValueError, KeyError, TypeError:
            pass

    info = HfApi().repo_info(
        source, revision=revision, files_metadata=True, timeout=timeout, token=token
    )
    siblings = getattr(info, "siblings", None)
    if not info.sha or siblings is None:
        raise ValueError("Hub snapshot metadata is incomplete")
    if pinned and info.sha != revision:
        raise ValueError("Hub snapshot metadata does not match the pinned model")
    files = [_SnapshotFile(item.rfilename, item.size) for item in siblings]
    if pinned:
        try:
            metadata_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(
                manifest,
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": source,
                        "revision": revision,
                        "files": [(item.rfilename, item.size) for item in files],
                    }
                ).encode(),
            )
        except OSError:
            # A read-only cache must not prevent metadata inspection or downloads.
            pass
    return str(info.sha), files


def inspect_snapshot(
    source: str,
    *,
    revision: str,
    allow_patterns: list[str] | str | None = None,
    ignore_patterns: list[str] | str | None = None,
    cache_dir: str | Path | None = None,
    metadata_cache_dir: str | Path | None = None,
    timeout: float = 5.0,
    token: str | bool | None = None,
) -> SnapshotSize:
    """Read metadata without downloading weights or changing existing model files.

    Only complete cached files at the resolved commit count as available. Missing
    metadata remains unknown rather than presenting a misleading zero size.
    """
    from huggingface_hub import try_to_load_from_cache
    from huggingface_hub.constants import HF_HUB_CACHE
    from huggingface_hub.utils import filter_repo_objects

    resolved_revision, metadata = _metadata(
        source,
        revision,
        Path(metadata_cache_dir)
        if metadata_cache_dir is not None
        else Path(cache_dir or HF_HUB_CACHE) / ".vntts-metadata",
        timeout,
        token,
    )
    files = list(
        filter_repo_objects(
            metadata,
            allow_patterns=allow_patterns,
            ignore_patterns=ignore_patterns,
            key=lambda item: item.rfilename,
        )
    )
    total_bytes = cached_bytes = remaining_bytes = 0
    total_known = remaining_known = True
    cached_files = 0
    for item in files:
        size = item.size if item.size is not None and item.size >= 0 else None
        if size is not None:
            total_bytes += size
        else:
            total_known = False
        cached = try_to_load_from_cache(
            source, item.rfilename, revision=resolved_revision, cache_dir=cache_dir
        )
        cached_size = None
        if isinstance(cached, str):
            try:
                path = Path(cached)
                if path.is_file():
                    cached_size = path.stat().st_size
            except OSError:
                pass
        available = cached_size is not None and (size is None or cached_size == size)
        if available:
            cached_files += 1
            assert cached_size is not None
            cached_bytes += cached_size
        elif size is not None:
            remaining_bytes += size
        else:
            remaining_known = False
    return SnapshotSize(
        revision=resolved_revision,
        total_bytes=total_bytes if total_known else None,
        cached_bytes=cached_bytes,
        remaining_bytes=remaining_bytes if remaining_known else None,
        total_files=len(files),
        cached_files=cached_files,
    )


def download_snapshot(
    source: str,
    *,
    asset: str,
    report: Callable[[str], None] | None = None,
    progress: Callable[[AssetProgress], None] | None = None,
    **options: Any,
) -> str:
    from huggingface_hub import snapshot_download

    if report is None and progress is None:
        return str(snapshot_download(source, **options))
    from huggingface_hub.utils.tqdm import tqdm

    if report is not None:
        report(f"Resolving {asset}...")
    if progress is not None:
        progress(AssetProgress("resolving", asset))
    reported = False
    network_bytes: int | None = None
    reconstructed_bytes: int | None = None
    completed_files: int | None = None
    total_files: int | None = None
    lock = Lock()

    def downloading() -> None:
        nonlocal reported
        with lock:
            if not reported:
                if report is not None:
                    report(f"Downloading {asset}...")
                if progress is not None:
                    progress(
                        AssetProgress(
                            "downloading",
                            asset,
                            completed_bytes=network_bytes,
                            completed_files=completed_files,
                            total_files=total_files,
                            reconstructed_bytes=reconstructed_bytes,
                        )
                    )
                reported = True

    class DownloadProgress(tqdm):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.byte_progress = kwargs.get("unit") == "B"
            name = kwargs.get("name")
            self.network_progress = name == "huggingface_hub.snapshot_download.transfer"
            self.reconstruction = (
                self.byte_progress and name == "huggingface_hub.snapshot_download"
            )
            self.completed = kwargs.get("initial", 0)
            self.closed = False
            super().__init__(*args, **kwargs)

        def emit_progress(self) -> None:
            nonlocal network_bytes, reconstructed_bytes, completed_files, total_files
            # The Hub assigns a byte total only when a file needs fetching.
            if self.byte_progress and self.total:
                if not self.reconstruction:
                    downloading()
                if progress is not None:
                    with lock:
                        completed = int(
                            self.completed
                            if getattr(self, "disable", False)
                            else getattr(self, "n", self.completed)
                        )
                        if self.reconstruction:
                            reconstructed_bytes = completed
                        elif self.network_progress:
                            network_bytes = completed
                        progress(
                            AssetProgress(
                                "downloading" if reported else "resolving",
                                asset,
                                completed_bytes=network_bytes
                                if self.reconstruction
                                else completed,
                                # Xet network totals are estimates affected by deduplication.
                                total_bytes=None
                                if self.network_progress or self.reconstruction
                                else int(self.total),
                                completed_files=completed_files,
                                total_files=total_files,
                                reconstructed_bytes=reconstructed_bytes,
                            )
                        )
            elif not self.byte_progress and progress is not None:
                # Cached files also advance this bar; counts do not prove transfer.
                with lock:
                    completed_files = int(
                        self.completed
                        if getattr(self, "disable", False)
                        else getattr(self, "n", self.completed)
                    )
                    total_files = int(self.total) if self.total is not None else None
                    progress(
                        AssetProgress(
                            "downloading" if reported else "resolving",
                            asset,
                            # Only the named aggregate network bar is operation-wide.
                            completed_bytes=network_bytes,
                            completed_files=completed_files,
                            total_files=total_files,
                            reconstructed_bytes=reconstructed_bytes,
                        )
                    )

        def refresh(self, *args: Any, **kwargs: Any) -> bool | None:
            self.emit_progress()
            refreshed: bool | None = super().refresh(*args, **kwargs)
            return refreshed

        def update(self, n: float | None = 1) -> bool | None:
            self.completed += n or 0
            updated: bool | None = super().update(n)
            # Disabled terminal bars still need to update the desktop observer.
            if getattr(self, "disable", False):
                self.emit_progress()
            return updated

        def close(self) -> None:
            if self.closed or not hasattr(self, "total"):
                return
            self.closed = True
            self.emit_progress()
            super().close()

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

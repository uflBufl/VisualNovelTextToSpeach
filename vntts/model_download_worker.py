"""Run a Hugging Face model transfer with a cancellable process boundary."""

import multiprocessing
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from threading import Event
from time import monotonic

from vntts.cleanup import cleanup_on_exit
from vntts.huggingface_assets import AssetProgress, download_snapshot
from vntts.model_assets import ModelDownloadCancelled

_STALL_TIMEOUT = 60.0


def _download(sender: Connection, source: str, revision: str, cache_dir: Path) -> None:
    with sender:
        try:
            download_snapshot(
                source,
                revision=revision,
                cache_dir=cache_dir,
                token=False,
                asset="Qwen3-TTS model",
                progress=sender.send,
            )
        except Exception as error:
            sender.send(type(error).__name__)
        else:
            sender.send(None)


def _stop(process: BaseProcess) -> None:
    with cleanup_on_exit(process.close, description="Model download process close"):
        if process.pid is None:
            return
        process.join(0.2)
        if process.is_alive():
            process.kill()
            process.join()


def download_model_snapshot(
    source: str,
    *,
    revision: str,
    cache_dir: Path,
    cancelled: Event,
    progress: Callable[[AssetProgress], None],
) -> None:
    if cancelled.is_set():
        raise ModelDownloadCancelled("Download stopped")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    with receiver, sender:
        process = context.Process(
            target=_download,
            args=(sender, source, revision, cache_dir),
            name="model-download",
            daemon=True,
        )
        # Xet's native retries can outlive Python callbacks. Reap the writer
        # before handing the same Hugging Face cache to a retry.
        with cleanup_on_exit(
            lambda: _stop(process), description="Model download cleanup"
        ):
            process.start()
            sender.close()
            deadline = monotonic() + _STALL_TIMEOUT
            previous: tuple[int, ...] = (0, 0, 0)
            while not cancelled.is_set():
                if receiver.poll(0.1):
                    try:
                        event = receiver.recv()
                    except EOFError as error:
                        raise RuntimeError(
                            "Model download process exited early"
                        ) from error
                    if event is None:
                        return
                    if not isinstance(event, AssetProgress):
                        raise RuntimeError(f"Model download failed ({event})")
                    current = (
                        event.completed_files or 0,
                        event.completed_bytes or 0,
                        event.reconstructed_bytes or 0,
                    )
                    completed = tuple(map(max, previous, current))
                    if completed != previous:
                        previous = completed
                        deadline = monotonic() + _STALL_TIMEOUT
                    progress(event)
                if monotonic() >= deadline:
                    raise TimeoutError("Model download stopped making progress")
            raise ModelDownloadCancelled("Download stopped")

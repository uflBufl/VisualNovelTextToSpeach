"""One disposable ASR process per uncached preview-content batch."""

from __future__ import annotations

import multiprocessing
import os
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from vntts.cleanup import attempt_cleanup, cleanup_on_exit

if TYPE_CHECKING:
    from vntts.preview_content_advisor import (
        PreviewContentCheck,
        _Cancellation,
        _SavedPreview,
    )

_CHECK_TIMEOUT_SECONDS = 300


def _check_previews(
    sender: Connection,
    root: Path,
    previews: tuple[_SavedPreview, ...],
    install_model: bool,
    cancel_event: _Cancellation | None = None,
) -> None:
    try:
        # Limit only this optional worker, including native inference pools.
        for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
            os.environ[name] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        import torch

        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
        from vntts.preview_content_advisor import PreviewContentAdvisor

        # The parent owns checker.lock; the batch still owns each cache lock.
        checks = PreviewContentAdvisor(root)._check_batch(
            previews, install_model, cancel_event
        )
        sender.send(("ok", checks))
    except Exception as error:
        sender.send(("error", str(error)))
    finally:
        sender.close()


def _close_worker(process: BaseProcess) -> None:
    with cleanup_on_exit(process.close, description="Preview worker handle close"):
        if process.pid is None:
            return
        failure = RuntimeError("Preview content worker cleanup failed")
        stopped = attempt_cleanup(
            lambda: process.join(0.2),
            description="Preview worker join",
            primary_error=failure,
        )
        if process.is_alive() or not stopped:
            stopped = (
                attempt_cleanup(
                    process.terminate,
                    description="Preview worker terminate",
                    primary_error=failure,
                )
                and stopped
            )
            stopped = (
                attempt_cleanup(
                    lambda: process.join(2),
                    description="Preview worker termination join",
                    primary_error=failure,
                )
                and stopped
            )
        if process.is_alive() or not stopped:
            stopped = (
                attempt_cleanup(
                    process.kill,
                    description="Preview worker kill",
                    primary_error=failure,
                )
                and stopped
            )
            stopped = (
                attempt_cleanup(
                    lambda: process.join(2),
                    description="Preview worker kill join",
                    primary_error=failure,
                )
                and stopped
            )
        if process.is_alive():
            failure.add_note("Preview content worker did not stop")
            stopped = False
        if not stopped:
            raise failure


def _wait_for_checks(
    process: BaseProcess,
    receiver: Connection,
    cancel_event: _Cancellation | None,
) -> tuple[PreviewContentCheck, ...]:
    from vntts.preview_content_advisor import (
        PreviewContentCheck,
        PreviewContentCheckError,
        _raise_if_cancelled,
    )

    deadline = monotonic() + _CHECK_TIMEOUT_SECONDS
    while True:
        _raise_if_cancelled(cancel_event)
        if receiver.poll(0.1):
            message = receiver.recv()
            _raise_if_cancelled(cancel_event)
            if isinstance(message, tuple) and len(message) == 2:
                status, result = message
                if status == "error" and isinstance(result, str):
                    raise PreviewContentCheckError(result)
                if (
                    status == "ok"
                    and isinstance(result, tuple)
                    and all(isinstance(check, PreviewContentCheck) for check in result)
                ):
                    return result
            raise PreviewContentCheckError("Invalid preview content worker result")
        if not process.is_alive():
            raise PreviewContentCheckError(
                f"Preview content worker exited unexpectedly ({process.exitcode})"
            )
        if monotonic() >= deadline:
            raise PreviewContentCheckError("Preview content check timed out")


def check_in_process(
    root: Path,
    previews: tuple[_SavedPreview, ...],
    install_model: bool,
    cancel_event: _Cancellation | None,
) -> tuple[PreviewContentCheck, ...]:
    from vntts.preview_content_advisor import (
        PreviewContentCheckError,
        _raise_if_cancelled,
    )

    _raise_if_cancelled(cancel_event)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    try:
        with (
            cleanup_on_exit(
                receiver.close, description="Preview worker receiver close"
            ),
            cleanup_on_exit(sender.close, description="Preview worker sender close"),
        ):
            worker_cancel = context.Event()
            process = context.Process(
                target=_check_previews,
                args=(sender, root, previews, install_model, worker_cancel),
                daemon=True,
                name="preview-content-check",
            )
            with (
                cleanup_on_exit(
                    lambda: _close_worker(process), description="Preview worker cleanup"
                ),
                cleanup_on_exit(
                    worker_cancel.set, description="Preview worker cancellation"
                ),
            ):
                process.start()
                sender.close()
                return _wait_for_checks(process, receiver, cancel_event)
    except PreviewContentCheckError:
        raise
    except Exception as error:
        raise PreviewContentCheckError(
            f"Preview content worker failed: {error}"
        ) from error

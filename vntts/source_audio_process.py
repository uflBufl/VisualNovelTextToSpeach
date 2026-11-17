"""Own publisher scratch files until its worker and native decoder have stopped."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

import psutil

from vntts.cleanup import attempt_cleanup, cleanup_on_exit
from vntts.windows_recording_process import RecorderJob


class SourceAudioProcess:
    def __init__(
        self,
        arguments: Sequence[str],
        *,
        popen_factory: Callable[..., subprocess.Popen[str]],
        environment: dict[str, str] | None = None,
    ) -> None:
        self._directory = Path(
            tempfile.mkdtemp(prefix="vntts-source-audio-")
        ).absolute()
        self._process: subprocess.Popen[str] | None = None
        self._job: RecorderJob | None = None
        self._closed = False
        try:
            child_environment = dict(os.environ if environment is None else environment)
            child_environment.update(
                (name, str(self._directory)) for name in ("TMPDIR", "TEMP", "TMP")
            )
            options: dict = {"start_new_session": True}
            if sys.platform == "win32":
                self._job = RecorderJob()
                options = {
                    "creationflags": 0x4
                }  # CREATE_SUSPENDED: own before decoding.
            self._process = popen_factory(
                tuple(arguments),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_environment,
                **options,
            )
            if self._job is not None:
                self._job.assign(self._process)
                psutil.Process(self._process.pid).resume()
        except BaseException as error:
            attempt_cleanup(
                self.close,
                description="Source-audio process cleanup",
                primary_error=error,
            )
            raise

    @property
    def process(self) -> subprocess.Popen[str]:
        assert self._process is not None
        return self._process

    def close(self) -> None:
        if self._closed:
            return
        process = self._process
        if process is not None:
            # Publishers join their subprocess.run decoder before normal exit.
            if process.poll() is None:
                children: list[psutil.Process] = []
                with cleanup_on_exit(
                    lambda: self._drain(process, children),
                    description="Source-audio process drain",
                ):
                    try:
                        with suppress(psutil.NoSuchProcess):
                            identity = psutil.Process(process.pid)
                            identity.suspend()
                            children = identity.children(recursive=True)
                    finally:
                        if self._job is not None:
                            with cleanup_on_exit(
                                lambda: self._kill_windows_processes(process, children),
                                description="Source-audio worker termination",
                            ):
                                self._job.close()
                        else:
                            with suppress(ProcessLookupError):
                                os.killpg(process.pid, signal.SIGKILL)
            if self._job is not None:
                self._job.close()
        elif self._job is not None:
            self._job.close()
        # Failed drain leaves the directory owned by nobody rather than deleting live WAVs.
        shutil.rmtree(self._directory)
        self._closed = True

    def _drain(
        self, process: subprocess.Popen[str], children: list[psutil.Process]
    ) -> None:
        process.wait(timeout=5)
        self._wait_for_decoders(children)
        process.communicate(timeout=5)

    @staticmethod
    def _kill_windows_processes(
        process: subprocess.Popen[str], children: list[psutil.Process]
    ) -> None:
        # Assignment or job close can fail; still release the suspended worker.
        with cleanup_on_exit(
            lambda: process.kill() if process.poll() is None else None,
            description="Source-audio worker termination",
        ):
            for child in children:
                with suppress(psutil.NoSuchProcess):
                    child.kill()

    @staticmethod
    def _wait_for_decoders(children: list[psutil.Process]) -> None:
        deadline = time.monotonic() + 5
        while children:
            running = []
            for child in children:
                with suppress(psutil.NoSuchProcess):
                    # Zombies have already closed their files; only their parent can reap them.
                    if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                        running.append(child)
            children = running
            if not children:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Source-audio decoders did not stop")
            time.sleep(0.01)

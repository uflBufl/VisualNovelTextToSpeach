"""Shared process input for the isolated speech-worker protocol."""

from collections.abc import Mapping
from io import BytesIO

from vntts.speech_worker import _write_frame


class FakeProcess:
    pid: int

    def __init__(self, health: Mapping[str, object] | None) -> None:
        output = BytesIO()
        if health is not None:
            _write_frame(output, health)
        output.seek(0)
        self.stdin = BytesIO()
        self.stdout = output
        self.stderr = BytesIO()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

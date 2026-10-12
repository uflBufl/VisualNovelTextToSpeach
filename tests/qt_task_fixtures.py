"""Qt worker ownership and test-controlled execution."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event

from PySide6.QtCore import QRunnable, QThreadPool


@contextmanager
def release_and_wait_for_pool(
    pool: QThreadPool,
    release: Event,
    *,
    finish: Callable[[], None] | None = None,
) -> Iterator[None]:
    try:
        yield
    finally:
        release.set()
        try:
            if finish is not None:
                finish()
        finally:
            assert pool.waitForDone(3000), "Qt workers did not finish"


class ManualThreadPool:
    def __init__(self) -> None:
        self.tasks: list[QRunnable] = []

    def start(self, task: QRunnable) -> None:
        self.tasks.append(task)

    def run_next(self) -> None:
        self.tasks.pop(0).run()

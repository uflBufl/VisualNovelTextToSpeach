"""Deferred Qt tasks with explicit test-controlled execution."""

from PySide6.QtCore import QRunnable


class ManualThreadPool:
    def __init__(self) -> None:
        self.tasks: list[QRunnable] = []

    def start(self, task: QRunnable) -> None:
        self.tasks.append(task)

    def run_next(self) -> None:
        self.tasks.pop(0).run()

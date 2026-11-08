"""Carry one operation's actual audio onset across its playback workers."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

playback_started_context: ContextVar[Callable[[], None] | None] = ContextVar(
    "playback_started_context", default=None
)


@contextmanager
def observe_playback_start(callback: Callable[[], None]) -> Iterator[None]:
    started = False
    lock = Lock()

    def notify() -> None:
        nonlocal started
        with lock:
            if started:
                return
            started = True
        callback()

    token = playback_started_context.set(notify)
    try:
        yield
    finally:
        playback_started_context.reset(token)

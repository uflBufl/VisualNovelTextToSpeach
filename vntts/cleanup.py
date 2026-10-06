"""Cleanup precedence shared by resource owners and rollback operations."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager


def attempt_cleanup(
    cleanup: Callable[[], object],
    *,
    description: str,
    primary_error: BaseException | None = None,
) -> bool:
    """Run once; report success, preserving only an explicitly owned failure."""
    try:
        cleanup()
    except BaseException as cleanup_error:
        if primary_error is None:
            raise
        primary_error.add_note(f"{description} failed: {cleanup_error}")
        if cleanup_error is not primary_error:
            for note in getattr(cleanup_error, "__notes__", ()):
                primary_error.add_note(note)
        return False
    return True


@contextmanager
def cleanup_on_exit(
    cleanup: Callable[[], object], *, description: str
) -> Iterator[None]:
    """Own final cleanup and capture only this operation's propagated failure."""
    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        attempt_cleanup(cleanup, description=description, primary_error=primary_error)

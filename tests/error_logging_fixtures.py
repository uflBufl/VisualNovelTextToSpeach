"""Keep application logging tests independent of the surrounding process."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from vntts import support


@contextmanager
def isolated_application_error_log() -> Iterator[support.RuntimeSupportLog]:
    root = logging.getLogger()
    previous_log = support.application_error_log
    previous_handler = support._application_error_handler
    previous_attached = previous_handler in root.handlers
    previous_level = root.level
    if previous_handler is not None:
        # Reconfiguration closes its previous handler; keep the caller's alive.
        root.removeHandler(previous_handler)
    support._application_error_handler = None
    try:
        yield support.configure_application_error_log()
    finally:
        handler = support._application_error_handler
        try:
            if handler is not None:
                root.removeHandler(handler)
                handler.close()
        finally:
            support.application_error_log = previous_log
            support._application_error_handler = previous_handler
            if previous_attached and previous_handler is not None:
                root.addHandler(previous_handler)
            root.setLevel(previous_level)

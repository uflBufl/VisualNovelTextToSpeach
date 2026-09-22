"""Small, explicit helpers shared by VNTTS command entry points."""

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple


def cli_exit_code(successful: object) -> int:
    return 0 if successful else 1


def cli_message(message: object, *, exit_code: int = 0, error: bool = False) -> int:
    """Write one command-line message and return its process exit code."""
    print(message, file=sys.stderr if error else sys.stdout)
    return exit_code


def cli_messages(
    messages: Iterable[object], *, exit_code: int = 0, error: bool = False
) -> int:
    for message in messages:
        cli_message(message, error=error)
    return exit_code


def cli_error(error: object, *, exit_code: int = 1) -> int:
    return cli_message(error, exit_code=exit_code, error=True)


def cli_success(message: object) -> int:
    return cli_message(message)


class CLIReportResult(NamedTuple):
    successful: bool
    report_path: Path

    @property
    def exit_code(self) -> int:
        return cli_exit_code(self.successful)

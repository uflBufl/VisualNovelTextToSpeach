"""Small, explicit helpers shared by VNTTS command entry points."""

import sys
from pathlib import Path
from typing import NamedTuple


def cli_exit_code(successful):
    return 0 if successful else 1


def cli_message(message, *, exit_code=0, error=False):
    """Write one command-line message and return its process exit code."""
    print(message, file=sys.stderr if error else sys.stdout)
    return exit_code


def cli_messages(messages, *, exit_code=0, error=False):
    for message in messages:
        cli_message(message, error=error)
    return exit_code


def cli_error(error, *, exit_code=1):
    return cli_message(error, exit_code=exit_code, error=True)


def cli_success(message):
    return cli_message(message)


class CLIReportResult(NamedTuple):
    successful: bool
    report_path: Path

    @property
    def exit_code(self):
        return cli_exit_code(self.successful)

"""Bounded subprocess adapter for supported game-specific content importers."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from vntts.application_directories import get_local_data_directory
from vntts.pregeneration_setup import PregenerationSetupError, inspect_story_index


class GameContentImportError(PregenerationSetupError):
    """A supported game importer could not produce usable story content."""


class GameContentImportCancelled(GameContentImportError):
    """The user cancelled the exact importer process."""


@dataclass(frozen=True)
class ImporterAvailability:
    available: bool
    message: str


class Reverse1999GameImporter:
    provider_id = "reverse1999"
    display_name = "Reverse: 1999"

    def __init__(
        self,
        *,
        command=None,
        output_root=None,
        popen_factory=subprocess.Popen,
    ):
        self._configured_command = tuple(command) if command else None
        self.output_root = Path(
            output_root or get_local_data_directory() / "game-content" / "reverse1999"
        ).expanduser()
        self.popen_factory = popen_factory

    def availability(self):
        command = self.command()
        if command is None:
            return ImporterAvailability(
                False,
                "Reverse: 1999 import support is not installed in this build.",
            )
        return ImporterAvailability(True, "Installed game import is available.")

    def command(self):
        if self._configured_command:
            return self._configured_command
        executable = shutil.which("r1999-bootstrap")
        if executable:
            return (executable,)
        try:
            module_available = (
                importlib.util.find_spec("r1999extractor.bootstrap") is not None
            )
        except (ImportError, ModuleNotFoundError, ValueError):
            module_available = False
        if module_available:
            return (sys.executable, "-m", "r1999extractor.bootstrap")
        return None

    def import_installed(self, cancel_event=None):
        command = self.command()
        if command is None:
            raise GameContentImportError(self.availability().message)
        self.output_root.mkdir(parents=True, exist_ok=True)
        arguments = (
            *command,
            "--data-directory",
            str(self.output_root),
            "--game-version",
            "installed",
        )
        try:
            process = self.popen_factory(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as error:
            raise GameContentImportError(
                f"Unable to start the Reverse: 1999 importer: {error}"
            ) from error

        while process.poll() is None:
            if cancel_event is not None and cancel_event.wait(0.1):
                _terminate_process(process)
                raise GameContentImportCancelled("Game import was cancelled")
        stdout, stderr = process.communicate()
        if process.returncode:
            detail = _last_output_line(stderr) or _last_output_line(stdout)
            raise GameContentImportError(
                "Reverse: 1999 content could not be imported"
                + (f": {detail}" if detail else ".")
            )
        story_index = self.output_root / "reverse1999" / "story-index.jsonl"
        if not story_index.is_file():
            raise GameContentImportError(
                "The game importer finished without producing story content."
            )
        return inspect_story_index(story_index, provider_id=self.provider_id)


def _terminate_process(process):
    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def _last_output_line(value):
    if not isinstance(value, str):
        return None
    return next(
        (line.strip() for line in reversed(value.splitlines()) if line.strip()), None
    )


__all__ = [
    "GameContentImportCancelled",
    "GameContentImportError",
    "ImporterAvailability",
    "Reverse1999GameImporter",
]

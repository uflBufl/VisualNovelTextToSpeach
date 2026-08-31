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
            if getattr(sys, "frozen", False):
                return (
                    sys.executable,
                    "--game-content-import-worker",
                    self.provider_id,
                )
            return (sys.executable, "-m", "r1999extractor.bootstrap")
        return None

    def import_installed(self, cancel_event=None, installation_root=None):
        command = self.command()
        if command is None:
            raise GameContentImportError(self.availability().message)
        self.output_root.mkdir(parents=True, exist_ok=True)
        arguments = [
            *command,
            "--data-directory",
            str(self.output_root),
            "--game-version",
            "installed",
        ]
        if installation_root is not None:
            resource_root, config_directory, audio_directory = (
                resolve_reverse1999_installation(installation_root)
            )
            arguments.extend(("--resource-root", str(resource_root)))
            arguments.extend(("--config-directory", str(config_directory)))
            arguments.extend(("--game-audio-directory", str(audio_directory)))
        try:
            process = self.popen_factory(
                tuple(arguments),
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


def resolve_reverse1999_installation(path):
    """Resolve three required roots under one explicitly selected game folder."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise GameContentImportError(f"The selected game folder does not exist: {root}")
    resource_candidates = [root]
    resource_candidates.extend(
        candidate.parent for candidate in sorted(root.glob("**/bundles"))
    )
    resource_root = next(
        (
            candidate
            for candidate in resource_candidates
            if (candidate / "bundles").is_dir()
        ),
        None,
    )
    config_candidates = [root / "configs", *sorted(root.glob("**/configs"))]
    config_directory = next(
        (
            candidate
            for candidate in config_candidates
            if (candidate / "datacfg_1.dat").is_file()
            and (candidate / "language" / "json_language_en.json.dat").is_file()
        ),
        None,
    )
    audio_candidates = [root, *sorted(root.glob("**/en"))]
    audio_directory = next(
        (
            candidate
            for candidate in audio_candidates
            if candidate.is_dir() and any(candidate.glob("*.bnk"))
        ),
        None,
    )
    missing = []
    if resource_root is None:
        missing.append("story bundles")
    if config_directory is None:
        missing.append("game configuration")
    if audio_directory is None:
        missing.append("English voice banks")
    if missing:
        raise GameContentImportError(
            "The selected folder is not a complete Reverse: 1999 installation; "
            f"missing {', '.join(missing)}."
        )
    return resource_root, config_directory, audio_directory


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
    "resolve_reverse1999_installation",
]

"""Bounded subprocess adapter for supported game-specific content importers."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_manifest import normalize_character_name

from vntts.application_directories import get_local_data_directory
from vntts.game_audio_decoder import ensure_game_decoder
from vntts.pregeneration_setup import PregenerationSetupError, inspect_story_index
from vntts.subprocess_utils import last_output_line, terminate_process
from vntts.voices import is_narrator, synthesis_character_for_line


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
        self.allow_decoder_homebrew = False
        self._narrator_session = None

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
        try:
            module_available = (
                importlib.util.find_spec("r1999extractor.bootstrap") is not None
            )
        except ImportError, ModuleNotFoundError, ValueError:
            module_available = False
        if getattr(sys, "frozen", False):
            if not module_available:
                return None
            return (
                sys.executable,
                "--game-content-import-worker",
                self.provider_id,
            )
        executable = shutil.which("r1999-bootstrap")
        if executable:
            return (executable,)
        if module_available:
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
        roots = (
            resolve_reverse1999_installation(installation_root)
            if installation_root is not None
            else self._previous_installation()
        )
        if roots is not None:
            resource_root, config_directory, audio_directory = roots
            arguments.extend(("--resource-root", str(resource_root)))
            arguments.extend(("--config-directory", str(config_directory)))
            arguments.extend(("--game-audio-directory", str(audio_directory)))
        self._run(arguments, cancel_event)
        story_index = self.output_root / "reverse1999" / "story-index.jsonl"
        if not story_index.is_file():
            raise GameContentImportError(
                "The game importer finished without producing story content."
            )
        return inspect_story_index(story_index, provider_id=self.provider_id)

    def _previous_installation(self):
        """Reuse the imported source before asking platform discovery to find it again."""
        story_index = self.output_root / "reverse1999" / "story-index.jsonl"
        try:
            with story_index.open(encoding="utf-8") as stream:
                metadata = json.loads(stream.readline())
            source = metadata.get("source_bundle")
            if not isinstance(source, str) or not source:
                return None
            bundle = Path(source)
            if not bundle.is_file() or bundle.parent.name != "bundles":
                return None
            return resolve_reverse1999_installation(bundle.parent.parent)
        except OSError, ValueError, AttributeError, GameContentImportError:
            # Removed/moved sources must not prevent a fresh automatic import.
            return None

    def prepare_voice_candidates(self, job, cancel_event=None, *, progress=None):
        """Prepare only candidate references needed by the selected stories."""
        if job.provider_id != self.provider_id:
            return None
        roles = _candidate_roles(job)
        if not roles:
            return None
        return self.prepare_voice_roles(roles, cancel_event, progress=progress)

    def narrator_characters(self, cancel_event=None, installation_root=None):
        """List voiced characters without decoding the whole audio catalog."""
        story_index = self.output_root / "reverse1999" / "narrator-index.jsonl"
        bank_index = story_index.parent / "english-bank-index.json"
        narrator_banks = story_index.parent / "narrator-banks.json"
        if (
            installation_root is not None
            or not story_index.is_file()
            or not bank_index.is_file()
            or not narrator_banks.is_file()
            or self._bank_index_is_stale(bank_index)
        ):
            self.import_installed(cancel_event, installation_root)
        characters = {
            normalize_character_name(name): name
            for name in json.loads(narrator_banks.read_text(encoding="utf-8"))
            if not is_narrator(name)
        }
        for record in load_story_index_document(story_index).records:
            character = synthesis_character_for_line(
                record.speaker, record.voice_character
            )
            if record.source_audio_status == "available" and not is_narrator(character):
                characters.setdefault(normalize_character_name(character), character)
        return tuple(sorted(characters.values(), key=str.casefold))

    @staticmethod
    def _bank_index_is_stale(path):
        from r1999extractor.reverse1999_index import bank_index_staleness_reasons

        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return True
        return bool(bank_index_staleness_reasons(document))

    def narrator_references(self, character):
        from r1999extractor.narrator_references import NarratorReferenceSession

        root = self.output_root / "reverse1999"
        self._narrator_session = NarratorReferenceSession(
            root / "narrator-index.jsonl",
            root / "english-bank-index.json",
            character,
            root / "voice-candidates",
        )
        return self._narrator_session.references

    def prepare_voice_roles(
        self,
        roles,
        cancel_event=None,
        *,
        progress=None,
        narrator=False,
        narrator_line_id=None,
    ):
        """Reuse the extractor's checksum-bound, per-role reference cache."""
        if not roles:
            raise GameContentImportError("Choose a game character first")
        command = self.command()
        if command is None:
            raise GameContentImportError(self.availability().message)
        decoder = ensure_game_decoder(
            cancellation=cancel_event,
            progress=progress,
            allow_homebrew=self.allow_decoder_homebrew,
        )
        if narrator:
            if len(roles) != 1:
                raise GameContentImportError("Choose one narrator character at a time")
            if (
                self._narrator_session is None
                or self._narrator_session.role != roles[0]
            ):
                self.narrator_references(roles[0])
            if cancel_event is not None and cancel_event.is_set():
                raise GameContentImportCancelled("Narrator preparation cancelled")
            return self._narrator_session.prepare(
                line_id=narrator_line_id,
                decoder=decoder,
                runner=partial(self._decode_narrator, cancel_event=cancel_event),
            )
        environment = dict(os.environ)
        environment["PATH"] = os.pathsep.join(
            (str(decoder.parent), environment.get("PATH", ""))
        )
        if progress is not None:
            progress("Extracting game voice references. Please wait...")
        arguments = [
            *command,
            "--data-directory",
            str(self.output_root),
            "--prepare-voice-candidates-only",
        ]
        if narrator:
            arguments.append("--narrator")
        if narrator_line_id is not None:
            arguments.extend(("--narrator-line-id", narrator_line_id))
        for role in roles:
            arguments.extend(("--voice-candidate-role", role))
        stdout, _stderr = self._run(arguments, cancel_event, environment=environment)
        try:
            result = json.loads(last_output_line(stdout) or "")
            manifest = Path(result["voice_manifest"]).expanduser().resolve()
            root = (self.output_root / "reverse1999" / "voice-candidates").resolve()
            manifest.relative_to(root)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise GameContentImportError(
                "Reverse: 1999 voice preparation returned an invalid result"
            ) from error
        if manifest.is_symlink() or not manifest.is_file():
            raise GameContentImportError(
                "Reverse: 1999 voice preparation produced no usable manifest"
            )
        return manifest

    def _decode_narrator(self, arguments, *, capture_output, text, cancel_event):
        stdout, stderr = self._run(arguments, cancel_event)
        return subprocess.CompletedProcess(arguments, 0, stdout, stderr)

    def _run(self, arguments, cancel_event, *, environment=None):
        try:
            process = self.popen_factory(
                tuple(arguments),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
        except OSError as error:
            raise GameContentImportError(
                f"Unable to start the Reverse: 1999 importer: {error}"
            ) from error
        while True:
            if (
                cancel_event is not None
                and cancel_event.is_set()
                and process.poll() is None
            ):
                terminate_process(process)
                raise GameContentImportCancelled("Game import was cancelled")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            detail = last_output_line(stderr) or last_output_line(stdout)
            raise GameContentImportError(
                "Reverse: 1999 content could not be imported"
                + (f": {detail}" if detail else ".")
            )
        return stdout, stderr


def _candidate_roles(job):
    story_index = Path(job.story_index).expanduser().resolve()
    try:
        if sha256_file(story_index) != job.story_index_sha256:
            raise GameContentImportError(
                "Selected dialogue changed before character voices were prepared"
            )
        document = load_story_index_document(story_index)
    except GameContentImportError:
        raise
    except (OSError, StoryIndexError, ValueError) as error:
        raise GameContentImportError(
            f"Unable to inspect selected character voices: {error}"
        ) from error
    selected = set(job.selected_line_ids)
    available = set()
    requested = {}
    for record in document.records:
        character = synthesis_character_for_line(
            record.speaker,
            record.voice_character,
        )
        normalized = normalize_character_name(character)
        if record.source_audio_status == "available":
            available.add(normalized)
        elif (
            record.line_id in selected
            and record.speakable
            and not is_narrator(character)
        ):
            requested.setdefault(normalized, character)
    return tuple(
        sorted(
            (character for key, character in requested.items() if key in available),
            key=str.casefold,
        )
    )


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


__all__ = [
    "GameContentImportCancelled",
    "GameContentImportError",
    "ImporterAvailability",
    "Reverse1999GameImporter",
    "resolve_reverse1999_installation",
]

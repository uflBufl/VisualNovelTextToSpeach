"""Bounded subprocess adapter for supported game-specific content importers."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache, partial
from os import PathLike
from pathlib import Path
from typing import Protocol, TypeAlias

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    load_story_index_document,
)
from vntts_artifacts.voice_manifest import normalize_character_name

from vntts.application_directories import get_config_directory, get_local_data_directory
from vntts.chapter_voice_preload import (
    _has_authoritative_source_audio,
    _validated_source_audio_line_ids,
)
from vntts.game_audio_decoder import Cancellation, ProgressCallback, ensure_game_decoder
from vntts.path_safety import contained_regular_file
from vntts.pregeneration_setup import (
    GameContent,
    PregenerationJob,
    PregenerationSetupError,
    inspect_story_index,
    load_verified_story_index_document,
)
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


PathInput: TypeAlias = str | PathLike[str]
InstallationRoots: TypeAlias = tuple[Path, Path, Path]
InstallationParts: TypeAlias = tuple[Path | None, Path | None, Path | None]
PopenFactory: TypeAlias = Callable[..., subprocess.Popen[str]]


class NarratorReference(Protocol):
    collection_title: str | None
    line_id: str
    text: str


class NarratorDecoderRunner(Protocol):
    def __call__(
        self,
        arguments: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]: ...


class NarratorReferenceSession(Protocol):
    role: str
    references: tuple[NarratorReference, ...]

    def prepare(
        self,
        *,
        line_id: str | None = None,
        decoder: Path | None = None,
        runner: NarratorDecoderRunner | None = None,
    ) -> Path: ...


class GameImportRecorder(Protocol):
    def __call__(self, stage: str, **details: object) -> None: ...


class Reverse1999GameImporter:
    provider_id: str = "reverse1999"
    display_name: str = "Reverse: 1999"

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        output_root: PathInput | None = None,
        installation_file: PathInput | None = None,
        popen_factory: PopenFactory = subprocess.Popen,
    ) -> None:
        self._configured_command = tuple(command) if command else None
        self.output_root = Path(
            output_root or get_local_data_directory() / "game-content" / "reverse1999"
        ).expanduser()
        self.installation_file = Path(
            installation_file
            or get_config_directory() / "reverse1999-installation.json"
        ).expanduser()
        self.popen_factory = popen_factory
        self.allow_decoder_homebrew = False
        self._narrator_session: NarratorReferenceSession | None = None
        self._operation_id = uuid.uuid4().hex[:12]

    def _record(self, stage: str, **details: object) -> None:
        from vntts.support import record_game_import

        record_game_import(stage, operation_id=self._operation_id, **details)

    def availability(self) -> ImporterAvailability:
        command = self.command()
        if command is None:
            return ImporterAvailability(
                False,
                "Reverse: 1999 import support is not installed in this build.",
            )
        return ImporterAvailability(True, "Installed game import is available.")

    def command(self) -> tuple[str, ...] | None:
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

    def import_installed(
        self,
        cancel_event: Cancellation | None = None,
        installation_root: PathInput | None = None,
    ) -> GameContent:
        command = self.command()
        package_version: str | None
        package_revision: str | None
        try:
            package = importlib.metadata.distribution("reverse1999-extractor")
            direct_url = json.loads(package.read_text("direct_url.json") or "{}")
            package_version = package.version
            package_revision = direct_url.get("vcs_info", {}).get("commit_id")
        except (
            importlib.metadata.PackageNotFoundError,
            OSError,
            ValueError,
            AttributeError,
        ):
            package_version = package_revision = None
        self._record(
            "import-start",
            path=self.output_root,
            resource_root=installation_root,
            executable=command[0] if command else None,
            command_kind="configured" if self._configured_command else "automatic",
            package_version=package_version,
            package_revision=package_revision,
        )
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
            self._record(
                "import-roots",
                resource_root=resource_root,
                config_directory=config_directory,
                audio_directory=audio_directory,
            )
        else:
            self._record(
                "import-roots", reason="no usable saved source; auto-discovery required"
            )
        story_index = self.output_root / "reverse1999" / "story-index.jsonl"
        backup = story_index.with_name(f".story-index-{uuid.uuid4().hex}.backup")
        if story_index.is_file():
            try:
                # The extractor atomically replaces the index. A hard link keeps
                # the previous 243 MB catalog usable without copying its bytes.
                os.link(story_index, backup)
            except OSError as error:
                raise GameContentImportError(
                    f"Unable to preserve the previous story catalog: {error}"
                ) from error
        try:
            self._run(arguments, cancel_event)
            if not story_index.is_file():
                self._record(
                    "import-result", outcome="missing-story-index", index=story_index
                )
                raise GameContentImportError(
                    "The game importer finished without producing story content."
                )
            result = inspect_story_index(story_index, provider_id=self.provider_id)
        except Exception:
            if backup.is_file():
                os.replace(backup, story_index)
            raise
        else:
            try:
                backup.unlink(missing_ok=True)
            except OSError as error:
                self._record("story-backup-cleanup", reason=str(error))
        if roots is None:
            roots = self._previous_installation()
        if roots is not None:
            self._remember_installation(roots)
            self._remember_story_inputs(story_index, roots)
        self._record("import-result", outcome="complete", index=story_index)
        return result

    def installed_story_changed(self) -> bool:
        """Check known source metadata without scanning or parsing game archives."""
        story_index = self.output_root / "reverse1999" / "story-index.jsonl"
        if not story_index.is_file():
            return False
        state_path = story_index.parent / "source-inputs.json"
        try:
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            roots = self._previous_installation()
            if roots is None:
                return False
            try:
                paths = self._story_input_paths(story_index, roots)
                changed = any(
                    path.stat().st_mtime_ns > story_index.stat().st_mtime_ns
                    for path in paths
                )
            except OSError, ValueError, KeyError, TypeError:
                return False
        except OSError, ValueError:
            changed = True
        else:
            try:
                if (
                    saved["version"] != 1
                    or not isinstance(saved["inputs"], dict)
                    or len(saved["inputs"]) != 3
                ):
                    raise ValueError("Invalid saved import inputs")
                changed = saved["story_index"] != self._file_signature(story_index)
                for raw_path, signature in saved["inputs"].items():
                    if not isinstance(raw_path, str) or not raw_path:
                        raise ValueError("Invalid saved import input path")
                    changed |= signature != self._file_signature(Path(raw_path))
            except OSError, ValueError, KeyError, TypeError:
                changed = True
        self._record("story-update-check", changed=changed, index=story_index)
        return changed

    @staticmethod
    def _file_signature(path: Path) -> list[int]:
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]

    @staticmethod
    def _story_input_paths(
        story_index: Path, roots: InstallationRoots
    ) -> tuple[Path, Path, Path]:
        with story_index.open(encoding="utf-8") as stream:
            metadata = json.loads(stream.readline())
        source = metadata.get("source_bundle")
        if not isinstance(source, str) or not source:
            raise ValueError("Imported story source is missing")
        bundle = Path(source).resolve()
        if bundle.parent != (roots[0] / "bundles").resolve():
            raise ValueError("Imported story source is outside the selected game")
        configs = roots[1]
        return (
            bundle,
            configs / "datacfg_1.dat",
            configs / "language/json_language_en.json.dat",
        )

    def _remember_story_inputs(
        self, story_index: Path, roots: InstallationRoots
    ) -> None:
        try:
            inputs = {
                str(path): self._file_signature(path)
                for path in self._story_input_paths(story_index, roots)
            }
            atomic_write_json(
                story_index.parent / "source-inputs.json",
                {
                    "version": 1,
                    "story_index": self._file_signature(story_index),
                    "inputs": inputs,
                },
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._record("story-inputs-save", outcome="failed", reason=str(error))

    def _remember_installation(self, roots: InstallationRoots) -> None:
        resources, configs, audio = roots
        try:
            atomic_write_json(
                self.installation_file,
                {
                    "resource_root": str(resources),
                    "config_directory": str(configs),
                    "audio_directory": str(audio),
                },
            )
            self._record(
                "installation-save", path=self.installation_file, outcome="complete"
            )
        except OSError as error:
            # Import remains useful even if settings cannot be persisted.
            self._record(
                "installation-save",
                path=self.installation_file,
                outcome="failed",
                reason=str(error),
            )

    def _previous_installation(self) -> InstallationRoots | None:
        """Prefer durable selection; an existing index can recover older imports."""
        try:
            saved = json.loads(self.installation_file.read_text(encoding="utf-8"))
            values = [
                saved[key]
                for key in ("resource_root", "config_directory", "audio_directory")
            ]
            if not all(isinstance(value, str) and value for value in values):
                raise ValueError("Invalid saved installation paths")
            resources, configs, audio = (Path(value) for value in values)
            roots = resources, configs, audio
            if not (
                all(path.is_absolute() for path in roots)
                and (resources / "bundles").is_dir()
                and (configs / "datacfg_1.dat").is_file()
                and (configs / "language/json_language_en.json.dat").is_file()
                and any(audio.glob("*.bnk"))
            ):
                raise ValueError("Saved installation files are no longer available")
            self._record(
                "installation-load",
                path=self.installation_file,
                outcome="complete",
                resource_root=resources,
                config_directory=configs,
                audio_directory=audio,
            )
            return roots
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._record(
                "installation-load",
                path=self.installation_file,
                outcome="rejected",
                reason=str(error),
            )
        story_index = self.output_root / "reverse1999" / "story-index.jsonl"
        self._record("saved-source", index=story_index, exists=story_index.is_file())
        try:
            with story_index.open(encoding="utf-8") as stream:
                metadata = json.loads(stream.readline())
            source = metadata.get("source_bundle")
            if not isinstance(source, str) or not source:
                self._record("saved-source", reason="source_bundle missing or invalid")
                return None
            bundle = Path(source)
            exists = bundle.is_file()
            self._record("saved-source", source_bundle=bundle, exists=exists)
            if not exists or bundle.parent.name != "bundles":
                self._record(
                    "saved-source",
                    reason="bundle absent or not inside bundles directory",
                )
                return None
            return resolve_reverse1999_installation(bundle.parent.parent)
        except (OSError, ValueError, AttributeError, GameContentImportError) as error:
            # Removed/moved sources must not prevent a fresh automatic import.
            self._record(
                "saved-source",
                outcome="rejected",
                exception_type=type(error).__name__,
                reason=str(error),
            )
            return None

    def selected_installation_root(self) -> Path | None:
        """Return the resource root currently reused for automatic imports."""
        roots = self._previous_installation()
        return roots[0] if roots is not None else None

    def prepare_voice_candidates(
        self,
        job: PregenerationJob,
        cancel_event: Cancellation | None = None,
        *,
        progress: ProgressCallback | None = None,
    ) -> Path | None:
        """Prepare only candidate references needed by the selected stories."""
        if job.game != self.display_name:
            return None
        roles = _candidate_roles(
            job, self.output_root / "reverse1999" / "narrator-index.jsonl"
        )
        if not roles:
            return None
        return self.prepare_voice_roles(
            roles,
            cancel_event,
            progress=progress,
            target_story_index=job.story_index,
        )

    def narrator_characters(
        self,
        cancel_event: Cancellation | None = None,
        installation_root: PathInput | None = None,
    ) -> tuple[str, ...]:
        """List voiced characters without decoding the whole audio catalog."""
        story_index = self.output_root / "reverse1999" / "narrator-index.jsonl"
        bank_index = story_index.parent / "english-bank-index.json"
        narrator_banks = story_index.parent / "narrator-banks.json"
        self._record(
            "narrator-cache",
            index=story_index,
            cache_state={
                "narrator_index": story_index.is_file(),
                "bank_index": bank_index.is_file(),
                "narrator_banks": narrator_banks.is_file(),
                "explicit_selection": installation_root is not None,
            },
        )
        if (
            installation_root is not None
            or not story_index.is_file()
            or not bank_index.is_file()
            or not narrator_banks.is_file()
            or self._bank_index_is_stale(bank_index)
        ):
            self.import_installed(cancel_event, installation_root)
        result = self._cached_narrator_characters(
            story_index,
            narrator_banks,
            sha256_file(story_index),
            sha256_file(narrator_banks),
        )
        self._record(
            "narrator-result",
            characters=len(result),
            outcome="complete" if result else "empty",
        )
        return result

    @staticmethod
    @lru_cache(maxsize=4)
    def _cached_narrator_characters(
        story_index: Path,
        narrator_banks: Path,
        index_sha256: str,
        banks_sha256: str,
    ) -> tuple[str, ...]:
        cache = story_index.parent / "narrator-characters.json"
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            names = saved["characters"]
            if (
                saved["version"] == 1
                and saved["story_index_sha256"] == index_sha256
                and saved["narrator_banks_sha256"] == banks_sha256
                and isinstance(names, list)
                and all(isinstance(name, str) and name.strip() for name in names)
            ):
                return tuple(names)
        except OSError, ValueError, KeyError, TypeError:
            pass
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
        result = tuple(sorted(characters.values(), key=str.casefold))
        try:
            atomic_write_json(
                cache,
                {
                    "version": 1,
                    "story_index_sha256": index_sha256,
                    "narrator_banks_sha256": banks_sha256,
                    "characters": result,
                },
            )
        except OSError:
            pass
        return result

    @staticmethod
    def _bank_index_is_stale(path: Path) -> bool:
        from r1999extractor.reverse1999_index import bank_index_staleness_reasons

        from vntts.support import record_game_import

        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            record_game_import(
                "bank-index", index=path, reason=str(error), cache_state="unreadable"
            )
            return True
        reasons = bank_index_staleness_reasons(document)
        record_game_import(
            "bank-index",
            index=path,
            reason=reasons,
            cache_state="stale" if reasons else "current",
        )
        return bool(reasons)

    def narrator_references(self, character: str) -> tuple[NarratorReference, ...]:
        from r1999extractor.narrator_references import NarratorReferenceSession

        root = self.output_root / "reverse1999"
        self._narrator_session = NarratorReferenceSession(
            root / "narrator-index.jsonl",
            root / "english-bank-index.json",
            character,
            root / "voice-candidates",
        )
        self._record(
            "narrator-references",
            references=len(self._narrator_session.references),
            outcome="complete",
        )
        return self._narrator_session.references

    def prepare_voice_roles(
        self,
        roles: Sequence[str],
        cancel_event: Cancellation | None = None,
        *,
        progress: ProgressCallback | None = None,
        narrator: bool = False,
        narrator_line_id: str | None = None,
        target_story_index: PathInput | None = None,
    ) -> Path:
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
            return self._prepare_narrator_voice(
                roles, decoder, cancel_event, narrator_line_id
            )
        return self._prepare_role_voice_candidates(
            roles,
            command,
            decoder,
            cancel_event,
            progress,
            narrator_line_id,
            target_story_index,
        )

    def _prepare_narrator_voice(
        self,
        roles: Sequence[str],
        decoder: Path,
        cancel_event: Cancellation | None,
        narrator_line_id: str | None,
    ) -> Path:
        if len(roles) != 1:
            raise GameContentImportError("Choose one narrator character at a time")
        if self._narrator_session is None or self._narrator_session.role != roles[0]:
            self.narrator_references(roles[0])
        if cancel_event is not None and cancel_event.is_set():
            raise GameContentImportCancelled("Narrator preparation cancelled")
        session = self._narrator_session
        if session is None:
            raise GameContentImportError("Narrator references could not be prepared")
        return session.prepare(
            line_id=narrator_line_id,
            decoder=decoder,
            runner=partial(self._decode_narrator, cancel_event=cancel_event),
        )

    def _prepare_role_voice_candidates(
        self,
        roles: Sequence[str],
        command: Sequence[str],
        decoder: Path,
        cancel_event: Cancellation | None,
        progress: ProgressCallback | None,
        narrator_line_id: str | None,
        target_story_index: PathInput | None,
    ) -> Path:
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
        if target_story_index is not None:
            arguments.extend(("--target-story-index", str(target_story_index)))
        if narrator_line_id is not None:
            arguments.extend(("--narrator-line-id", narrator_line_id))
        for role in roles:
            arguments.extend(("--voice-candidate-role", role))
        stdout, _stderr = self._run(arguments, cancel_event, environment=environment)
        try:
            result = json.loads(last_output_line(stdout) or "")
            selected = Path(result["voice_manifest"]).expanduser().absolute()
            root = (self.output_root / "reverse1999" / "voice-candidates").absolute()
            relative = selected.relative_to(root)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise GameContentImportError(
                "Reverse: 1999 voice preparation returned an invalid result"
            ) from error
        try:
            return contained_regular_file(
                root,
                relative.as_posix(),
                "voice candidate manifest",
                error_type=GameContentImportError,
            )
        except GameContentImportError as error:
            raise GameContentImportError(
                "Reverse: 1999 voice preparation produced no usable manifest"
            ) from error

    def _decode_narrator(
        self,
        arguments: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        cancel_event: Cancellation | None,
    ) -> subprocess.CompletedProcess[str]:
        stdout, stderr = self._run(arguments, cancel_event)
        return subprocess.CompletedProcess(arguments, 0, stdout, stderr)

    def _run(
        self,
        arguments: Sequence[str],
        cancel_event: Cancellation | None,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        started = time.monotonic()
        self._record("process-start", executable=arguments[0])
        try:
            process = self.popen_factory(
                tuple(arguments),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
        except OSError as error:
            self._record(
                "process-start",
                outcome="failed",
                exception_type=type(error).__name__,
                reason=str(error),
            )
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
                self._record(
                    "process-exit",
                    outcome="cancelled",
                    cancelled=True,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
                raise GameContentImportCancelled("Game import was cancelled")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        self._record(
            "process-exit",
            exit_code=process.returncode,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            outcome="failed" if process.returncode else "complete",
        )
        if stdout:
            self._record(
                "process-stdout",
                stdout_tail=stdout[-6000:],
                reason="tail truncated" if len(stdout) > 6000 else None,
            )
        if stderr:
            self._record(
                "process-stderr",
                stderr_tail=stderr[-12000:],
                reason="tail truncated" if len(stderr) > 12000 else None,
            )
        if process.returncode:
            detail = last_output_line(stderr) or last_output_line(stdout)
            raise GameContentImportError(
                "Reverse: 1999 content could not be imported"
                + (f": {detail}" if detail else ".")
            )
        return stdout, stderr


def _candidate_roles(
    job: PregenerationJob, reference_index: Path | None = None
) -> tuple[str, ...]:
    story_index = Path(job.story_index).expanduser().resolve()
    try:
        document = load_verified_story_index_document(
            story_index, job.story_index_sha256
        )
        available = (
            _cached_playable_voice_roles(Path(reference_index))
            if reference_index is not None and Path(reference_index).exists()
            else _playable_voice_roles(document)
        )
    except (OSError, StoryIndexError, ValueError) as error:
        raise GameContentImportError(
            f"Unable to inspect selected character voices: {error}"
        ) from error
    selected = set(job.selected_line_ids)
    authoritative_source_lines = _validated_source_audio_line_ids(story_index, document)
    source_completion = document.metadata.get("source_audio_completion")
    requested: dict[str, str] = {}
    for record in document.records:
        character = synthesis_character_for_line(
            record.speaker,
            record.voice_character,
        )
        normalized = normalize_character_name(character)
        if (
            record.line_id in selected
            and not _has_authoritative_source_audio(
                record, source_completion, authoritative_source_lines
            )
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


def _playable_voice_roles(document: StoryIndexDocument) -> set[str]:
    from r1999extractor.story_voice_candidates import is_playable_main_voice_reference

    return {
        normalize_character_name(
            synthesis_character_for_line(record.speaker, record.voice_character)
        )
        for record in document.records
        if record.source_audio_status == "available"
        and (
            not record.line_id.startswith("playable-voice:")
            or is_playable_main_voice_reference(
                record.line_id, record.producer_fields.get("source_bank")
            )
        )
    }


def _cached_playable_voice_roles(index: Path) -> set[str]:
    checksum = sha256_file(index)
    cache = index.parent / "playable-voice-roles.json"
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            roles = saved.get("roles")
            if (
                saved.get("version") == 1
                and saved.get("index_sha256") == checksum
                and isinstance(roles, list)
                and all(
                    isinstance(role, str) and normalize_character_name(role) == role
                    for role in roles
                )
                and len(roles) == len(set(roles))
            ):
                return set(roles)
    except OSError, ValueError, TypeError:
        pass
    available = _playable_voice_roles(load_story_index_document(index))
    if sha256_file(index) != checksum:
        raise GameContentImportError("Voice reference index changed while being read")
    try:
        atomic_write_json(
            cache,
            {"version": 1, "index_sha256": checksum, "roles": sorted(available)},
        )
    except OSError:
        pass
    return available


def resolve_reverse1999_installation(path: PathInput) -> InstallationRoots:
    """Resolve one installation, including its split Windows resource folders."""
    from vntts.support import record_game_import

    started = time.perf_counter()
    root = Path(path).expanduser().resolve()
    record_game_import("folder-selection", path=root, exists=root.is_dir())
    if not root.is_dir():
        raise GameContentImportError(f"The selected game folder does not exist: {root}")
    search_roots = [root]
    if root.parent.name.casefold() == "streamingassets":
        sibling_name = {"persistentroot": "Windows", "windows": "PersistentRoot"}.get(
            root.name.casefold()
        )
        if sibling_name:
            sibling = (root.parent / sibling_name).resolve()
            if sibling.is_dir() and sibling.parent == root.parent:
                search_roots.append(sibling)
    record_game_import("folder-search", roots=search_roots)
    resource_root, config_directory, audio_directory = _find_installation_parts(
        root, search_roots, record_game_import
    )
    missing = []
    if resource_root is None:
        missing.append("story bundles")
    if config_directory is None:
        missing.append("game configuration")
    if audio_directory is None:
        missing.append("English voice banks")
    record_game_import(
        "folder-result",
        resource_root=resource_root,
        config_directory=config_directory,
        audio_directory=audio_directory,
        missing=missing,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    if missing:
        raise GameContentImportError(
            "The selected folder is not a complete Reverse: 1999 installation; "
            f"missing {', '.join(missing)}."
        )
    assert resource_root is not None
    assert config_directory is not None
    assert audio_directory is not None
    return resource_root, config_directory, audio_directory


def _find_installation_parts(
    root: Path,
    search_roots: Sequence[Path],
    record: GameImportRecorder,
) -> InstallationParts:
    resource_root = root if (root / "bundles").is_dir() else None
    config_directory = None
    audio_directory = root if any(root.glob("*.bnk")) else None
    checked_configs = set()

    for search_root in search_roots:
        for directory, names, files in os.walk(search_root):
            current = Path(directory)
            if resource_root is None and "bundles" in names:
                resource_root = current.resolve()
            if current.name == "configs":
                checked_configs.add(current)
                missing = [
                    name
                    for name in (
                        "datacfg_1.dat",
                        "language/json_language_en.json.dat",
                    )
                    if not (current / name).is_file()
                ]
                record("config-candidate", path=current, missing=missing)
                if not missing:
                    config_directory = current.resolve()
            if (
                audio_directory is None
                and current.name.casefold() == "en"
                and any(name.casefold().endswith(".bnk") for name in files)
            ):
                audio_directory = current.resolve()
            # Asset bundles are huge and cannot contain another installation part.
            names[:] = [name for name in names if name != "bundles"]
            if resource_root and config_directory and audio_directory:
                return resource_root, config_directory, audio_directory

    direct_config = root / "configs"
    if config_directory is None and direct_config not in checked_configs:
        missing = [
            name
            for name in ("datacfg_1.dat", "language/json_language_en.json.dat")
            if not (direct_config / name).is_file()
        ]
        record("config-candidate", path=direct_config, missing=missing)
    return resource_root, config_directory, audio_directory


__all__ = [
    "GameContentImportCancelled",
    "GameContentImportError",
    "ImporterAvailability",
    "Reverse1999GameImporter",
    "resolve_reverse1999_installation",
]

import json
import logging
import os
import shutil
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from tempfile import mkdtemp
from uuid import uuid4

from durable_file import atomic_write_json
from vntts_artifacts.text_utils import slugify
from vntts_artifacts.voice_manifest import validate_voice_manifest

from vntts.asset_integrity import ModelIntegrityError, asset_manifest_name
from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.cleanup import attempt_cleanup, cleanup_on_exit
from vntts.document_identity import file_sha256
from vntts.json_types import decode_json
from vntts.path_safety import copy_regular_file, open_regular_binary
from vntts.settings import get_local_data_directory
from vntts.voices import (
    CharacterVoiceRegistry,
    VoiceManifestError,
    normalize_character_name,
)

supported_audio_extensions = {".flac", ".m4a", ".mp3", ".ogg", ".wav"}
_VOICE_MANIFEST_READ_LIMIT = 16 * 1024 * 1024


class VoiceReplacementRequired(VoiceManifestError):
    def __init__(self, character: str, manifest_sha256: str) -> None:
        self.character = character
        self.manifest_sha256 = manifest_sha256
        super().__init__(
            f"A voice already exists for {character!r}. Confirm replacement."
        )


class VoicePackManager:
    def __init__(self, storage_root: str | os.PathLike[str] | None = None) -> None:
        self.storage_root = (
            Path(storage_root or get_local_data_directory() / "voice-packs")
            .expanduser()
            .resolve(strict=False)
        )

    @staticmethod
    def _check_pack_path(pack_path: Path) -> None:
        references_path = pack_path / "references"
        if any(
            path.is_symlink() or path.is_junction()
            for path in (pack_path, references_path, pack_path / "manifest.json")
        ):
            raise VoiceManifestError(
                "Managed voice pack directory must not be an alias"
            )

    def import_voice(
        self,
        character: str,
        reference_files: Sequence[str | os.PathLike[str]],
        *,
        aliases: tuple[str, ...] = (),
        pack: str = "custom",
        expected_manifest_sha256: str | None = None,
    ) -> Path:
        character = (character or "").strip()
        if not character:
            raise VoiceManifestError("Character name is required")
        if not normalize_character_name(character):
            raise VoiceManifestError(
                "Character name must contain at least one letter or number"
            )
        if any(
            alias.strip() and not normalize_character_name(alias) for alias in aliases
        ):
            raise VoiceManifestError(
                "Aliases must contain at least one letter or number"
            )
        references = self._validate_reference_files(reference_files)
        pack_path = self.storage_root / slugify(pack, fallback="asset")
        self._check_pack_path(pack_path)
        lock_path = pack_path.with_name(f".{pack_path.name}.import.lock")
        with exclusive_advisory_lock(lock_path, blocking=True):
            self._check_pack_path(pack_path)
            manifest_path: Path = pack_path / "manifest.json"
            if expected_manifest_sha256 is not None and (
                not manifest_path.exists()
                or file_sha256(manifest_path, error_type=VoiceManifestError)
                != expected_manifest_sha256
            ):
                raise VoiceManifestError("Voice library changed. Review it and retry.")
            manifest = (
                read_json(manifest_path, None)
                if manifest_path.exists()
                else {"version": 2, "voices": []}
            )
            if not isinstance(manifest, dict) or manifest.get("version") != 2:
                raise VoiceManifestError("Existing voice manifest is invalid")
            voices = manifest.get("voices")
            if not isinstance(voices, list) or not all(
                isinstance(item, dict) for item in voices
            ):
                raise VoiceManifestError("Existing voice manifest is invalid")
            existing_voice = next(
                (
                    item
                    for item in voices
                    if str(item.get("character", "")).casefold() == character.casefold()
                ),
                None,
            )
            if existing_voice is not None and expected_manifest_sha256 is None:
                raise VoiceReplacementRequired(
                    str(existing_voice["character"]),
                    file_sha256(manifest_path, error_type=VoiceManifestError),
                )
            references_path = pack_path / "references"
            copied = [
                references_path
                / (
                    f"{slugify(character, fallback='asset')}-{uuid4().hex[:10]}"
                    f"{source.suffix.casefold()}"
                )
                for source in references
            ]
            voice = {
                "character": character,
                "speaker": f"local-{slugify(character, fallback='asset')}-v2",
                "aliases": [alias.strip() for alias in aliases if alias.strip()],
                "references": [f"references/{path.name}" for path in copied],
            }
            voices = [
                item
                for item in voices
                if str(item.get("character", "")).casefold() != character.casefold()
            ]
            voices.append(voice)
            updated_manifest = {"version": 2, "voices": voices}
            validate_voice_manifest(updated_manifest, allow_legacy=False)
            voices.sort(key=lambda item: item["character"].casefold())
            references_path.mkdir(parents=True, exist_ok=True)
            try:
                for source, output in zip(references, copied, strict=True):
                    self._copy_reference(source, output)
            except BaseException as error:
                self._remove_failed_copies(copied, error)
                raise
            self._publish_pack(pack_path, manifest_path, updated_manifest, copied)
            return manifest_path

    def import_pack(
        self,
        source_manifest: str | os.PathLike[str],
        *,
        pack_name: str | None = None,
    ) -> Path:
        source_path = Path(source_manifest).expanduser().resolve()
        registry = CharacterVoiceRegistry.from_file(source_path)
        pack_name = pack_name or source_path.parent.name
        pack_path = self.storage_root / slugify(pack_name, fallback="asset")
        self._check_pack_path(pack_path)
        lock_path = pack_path.with_name(f".{pack_path.name}.import.lock")
        with exclusive_advisory_lock(lock_path, blocking=True):
            self._check_pack_path(pack_path)
            references_path = pack_path / "references"
            references_path.mkdir(parents=True, exist_ok=True)

            unique_voices = registry.unique_voices()
            entries = []
            copied: list[Path] = []
            try:
                for voice in unique_voices:
                    copied_references = []
                    for reference in voice.references:
                        self._validate_reference_file(reference)
                        output = references_path / (
                            f"{slugify(voice.character, fallback='asset')}-{uuid4().hex[:10]}"
                            f"{reference.suffix.casefold()}"
                        )
                        copied.append(output)
                        self._copy_reference(reference, output)
                        copied_references.append(f"references/{output.name}")
                    entry: dict[str, object] = {
                        "character": voice.character,
                        "speaker": voice.speaker,
                        "aliases": list(voice.aliases),
                        "references": copied_references,
                    }
                    if voice.source_character:
                        entry["vntts.source_character"] = voice.source_character
                    if voice.reference_transcript:
                        entry["vntts.reference_transcript"] = voice.reference_transcript
                    entries.append(entry)
            except BaseException as error:
                self._remove_failed_copies(copied, error)
                raise
            entries.sort(key=lambda item: str(item["character"]).casefold())
            output_manifest: Path = pack_path / "manifest.json"
            self._publish_pack(
                pack_path, output_manifest, {"version": 2, "voices": entries}, copied
            )
            return output_manifest

    @staticmethod
    def _copy_reference(source: Path, destination: Path) -> None:
        copy_regular_file(source, destination, preserve_metadata=True)

    @staticmethod
    def _remove_failed_copies(copied: Sequence[Path], error: BaseException) -> None:
        for output in copied:
            attempt_cleanup(
                partial(output.unlink, missing_ok=True),
                description="Voice reference rollback",
                primary_error=error,
            )

    @staticmethod
    def _restore_pack_files(
        backups: Sequence[tuple[Path, Path]], error: BaseException
    ) -> bool:
        restored = True
        for destination, backup in backups:

            def restore() -> None:
                if backup.exists():
                    os.replace(backup, destination)
                else:
                    destination.unlink(missing_ok=True)

            if not attempt_cleanup(
                restore,
                description=f"Voice pack rollback for {destination.name}",
                primary_error=error,
            ):
                restored = False
        return restored

    def _publish_pack(
        self,
        pack_path: Path,
        manifest_path: Path,
        document: dict[str, object],
        copied: Sequence[Path],
    ) -> None:
        checksum_path = pack_path / asset_manifest_name
        replacement_started = False
        backup_directory: Path | None = None

        def cleanup_backup() -> None:
            # Successful publication owns cleanup separately below.
            if backup_directory is not None and not replacement_started:
                shutil.rmtree(backup_directory)

        try:
            if checksum_path.is_symlink() or checksum_path.is_junction():
                raise ModelIntegrityError(
                    "Voice checksum manifest must not be an alias"
                )
            # ponytail: rollback handles exceptions; crash-atomic publication needs a versioned pack directory.
            backup_directory = Path(
                mkdtemp(dir=pack_path, prefix=".voice-pack-backup-")
            )
            with cleanup_on_exit(
                cleanup_backup, description="Voice pack backup cleanup"
            ):
                backups = []
                for destination in (manifest_path, checksum_path):
                    backup = backup_directory / destination.name
                    if destination.exists():
                        copy_regular_file(destination, backup)
                    backups.append((destination, backup))
                try:
                    replacement_started = True
                    atomic_write_json(manifest_path, document)
                    registry = CharacterVoiceRegistry.from_file(manifest_path)
                    self._write_voice_checksums(pack_path, manifest_path, registry)
                except BaseException as error:
                    rollback_incomplete = not self._restore_pack_files(backups, error)
                    if rollback_incomplete:
                        error.add_note(
                            f"Voice pack recovery backups retained at {backup_directory}"
                        )
                    else:
                        replacement_started = False
                    raise
        except BaseException as error:
            if not replacement_started:
                self._remove_failed_copies(copied, error)
            raise
        assert backup_directory is not None
        self._cleanup_published_pack(pack_path, backup_directory, registry)

    def _cleanup_published_pack(
        self,
        pack_path: Path,
        backup_directory: Path,
        registry: CharacterVoiceRegistry,
    ) -> None:
        for phase, path, cleanup in (
            (
                "backup cleanup",
                backup_directory,
                partial(shutil.rmtree, backup_directory),
            ),
            (
                "reference cleanup",
                pack_path / "references",
                partial(self._remove_unreferenced_files, pack_path, registry),
            ),
        ):
            try:
                cleanup()
            except OSError as error:
                logging.getLogger("vntts.assets").warning(
                    "Voice pack published at %s; %s failed for %s: %s",
                    pack_path,
                    phase,
                    path,
                    error,
                )

    @staticmethod
    def _remove_unreferenced_files(
        pack_path: Path, registry: CharacterVoiceRegistry
    ) -> None:
        references_path = pack_path / "references"
        referenced = {
            reference.resolve()
            for voice in registry.unique_voices()
            for reference in voice.references
        }
        for path in references_path.iterdir():
            if (
                not path.is_symlink()
                and path.is_file()
                and path.resolve() not in referenced
            ):
                path.unlink()

    def validate(self, manifest_path: str | os.PathLike[str]) -> Path:
        manifest_path = Path(manifest_path).expanduser().resolve()
        registry = CharacterVoiceRegistry.from_file(manifest_path)
        checksum_path = manifest_path.parent / asset_manifest_name
        if not checksum_path.is_file():
            raise ModelIntegrityError("Voice checksum manifest is missing")
        manifest = read_json(checksum_path, {})
        if not isinstance(manifest, dict):
            raise ModelIntegrityError("Voice checksum manifest is malformed")
        version = manifest.get("version")
        if type(version) is not int or version != 1:
            raise ModelIntegrityError("Unsupported voice checksum manifest version")
        if manifest.get("manifest_sha256") != file_sha256(
            manifest_path, error_type=ModelIntegrityError
        ):
            raise ModelIntegrityError("Voice manifest checksum failed")
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise ModelIntegrityError("Voice checksum file inventory is malformed")
        expected_files = {
            str(reference.relative_to(manifest_path.parent))
            for voice in registry.unique_voices()
            for reference in voice.references
        }
        if set(files) != expected_files:
            raise ModelIntegrityError("Voice checksum manifest has the wrong files")
        for filename, expected in files.items():
            if not isinstance(filename, str):
                raise ModelIntegrityError("Voice checksum filename is malformed")
            path = manifest_path.parent / filename
            if (
                not path.is_file()
                or file_sha256(path, error_type=ModelIntegrityError) != expected
            ):
                raise ModelIntegrityError(
                    f"Voice reference checksum failed: {filename}"
                )
        return manifest_path

    @staticmethod
    def _validate_reference_files(
        reference_files: Sequence[str | os.PathLike[str]],
    ) -> list[Path]:
        references = [Path(path).expanduser().resolve() for path in reference_files]
        if not references:
            raise VoiceManifestError("Select at least one voice reference")
        for reference in references:
            VoicePackManager._validate_reference_file(reference)
        return references

    @staticmethod
    def _validate_reference_file(reference: Path) -> None:
        if not reference.is_file():
            raise VoiceManifestError(f"Voice reference does not exist: {reference}")
        if reference.suffix.casefold() not in supported_audio_extensions:
            raise VoiceManifestError(
                f"Unsupported voice reference format: {reference.suffix}"
            )

    @staticmethod
    def _write_voice_checksums(
        pack_path: Path, manifest_path: Path, registry: CharacterVoiceRegistry
    ) -> None:
        voices = registry.unique_voices()
        files = {
            str(reference.relative_to(pack_path)): file_sha256(
                reference, error_type=ModelIntegrityError
            )
            for voice in voices
            for reference in voice.references
        }
        atomic_write_json(
            pack_path / asset_manifest_name,
            {
                "version": 1,
                "manifest_sha256": file_sha256(
                    manifest_path, error_type=ModelIntegrityError
                ),
                "files": files,
            },
        )


def read_json(path: str | os.PathLike[str], default: object) -> object:
    path = Path(path)
    if path.is_symlink() or path.is_junction():
        return default
    try:
        with open_regular_binary(path) as source:
            payload = source.read(_VOICE_MANIFEST_READ_LIMIT + 1)
        if len(payload) > _VOICE_MANIFEST_READ_LIMIT:
            return default
        return decode_json(payload)
    except OSError, UnicodeError, json.JSONDecodeError:
        return default

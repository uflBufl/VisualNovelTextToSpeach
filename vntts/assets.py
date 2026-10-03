import json
import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.text_utils import slugify
from vntts_artifacts.voice_manifest import validate_voice_manifest

from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.settings import get_local_data_directory
from vntts.voices import CharacterVoiceRegistry, VoiceManifestError

asset_manifest_name = "vntts-asset.json"
supported_audio_extensions = {".flac", ".m4a", ".mp3", ".ogg", ".wav"}
_ASSET_MANIFEST_READ_LIMIT = 64 * 1024
_VOICE_MANIFEST_READ_LIMIT = 16 * 1024 * 1024


class _ModelResponse(Protocol):
    @property
    def headers(self) -> Mapping[str, str]: ...

    def read(self, size: int) -> bytes: ...

    def getcode(self) -> int: ...


class _ModelOpener(Protocol):
    def __call__(
        self, request: Request, *, timeout: int
    ) -> AbstractContextManager[_ModelResponse]: ...


class AssetError(RuntimeError):
    pass


class UnsupportedModelError(AssetError):
    pass


class ModelDownloadCancelled(AssetError):
    pass


class ModelIntegrityError(AssetError):
    pass


def _model_filename(url: str) -> str:
    filename = Path(urlparse(url).path).name
    if not filename:
        raise ModelIntegrityError(f"Model URL has no filename: {url}")
    return filename


@dataclass(frozen=True)
class ModelAsset:
    name: str
    urls: tuple[str, ...]
    expected_hash: str | None = None

    @property
    def directory_name(self) -> str:
        return self.name.replace("/", "--")


def _model_filenames(asset: ModelAsset) -> dict[str, str]:
    filenames = {url: _model_filename(url) for url in asset.urls}
    if len(set(filenames.values())) != len(asset.urls):
        raise ModelIntegrityError("Model URLs resolve to the same filename")
    return filenames


class ModelAssetManager:
    def __init__(
        self,
        storage_root: str | os.PathLike[str] | None = None,
        *,
        opener: _ModelOpener | None = None,
        catalog_loader: Callable[[str], ModelAsset] | None = None,
    ) -> None:
        self.storage_root = (
            Path(storage_root or get_local_data_directory() / "models")
            .expanduser()
            .resolve(strict=False)
        )
        self.opener = opener or urlopen
        self.catalog_loader = catalog_loader or load_coqui_model_asset

    @property
    def coqui_cache_root(self) -> Path:
        return self.storage_root / "tts"

    def configure_environment(self) -> Path:
        os.environ["TTS_HOME"] = str(self.storage_root)
        return self.storage_root

    def configure_huggingface_environment(self) -> Path:
        cache_root = self.storage_root / "huggingface"
        os.environ["HF_HOME"] = str(cache_root)
        return cache_root

    def model_path(self, model_name: str) -> Path:
        return self.coqui_cache_root / model_name.replace("/", "--")

    def _check_model_path(self, model_path: Path) -> None:
        if any(
            path.is_symlink() or path.is_junction()
            for path in (self.coqui_cache_root, model_path)
        ):
            raise ModelIntegrityError("Managed model directory must not be an alias")

    @staticmethod
    def _check_model_file(path: Path, filename: str) -> None:
        if path.is_symlink() or path.is_junction():
            raise ModelIntegrityError(
                f"Managed model file must not be an alias: {filename}"
            )

    def is_ready(self, model_name: str) -> bool:
        try:
            self.validate(model_name)
        except AssetError:
            return False
        return True

    def validate(self, model_name: str, *, asset: ModelAsset | None = None) -> Path:
        asset = asset or self.catalog_loader(model_name)
        model_path = self.model_path(model_name)
        self._check_model_path(model_path)
        manifest_path = model_path / asset_manifest_name
        if not manifest_path.is_file():
            self._adopt_existing_model(model_path, asset)
        manifest = self._read_checksum_manifest(manifest_path)
        files = self._validate_checksum_manifest(manifest, model_name, asset)
        self._validate_model_files(model_path, files)
        self._validate_upstream_hash(model_path, asset)
        return model_path

    @staticmethod
    def _read_checksum_manifest(manifest_path: Path) -> object:
        try:
            if manifest_path.is_symlink() or manifest_path.is_junction():
                raise ValueError("checksum manifest is an alias")
            with manifest_path.open("rb") as source:
                payload = source.read(_ASSET_MANIFEST_READ_LIMIT + 1)
            if len(payload) > _ASSET_MANIFEST_READ_LIMIT:
                raise ValueError("checksum manifest is too large")
            manifest: object = json.loads(payload)
        except (OSError, ValueError) as error:
            raise ModelIntegrityError(
                f"Unable to read model checksum manifest: {error}"
            ) from error
        return manifest

    @staticmethod
    def _validate_checksum_manifest(
        manifest: object, model_name: str, asset: ModelAsset
    ) -> dict[str, object]:
        if not isinstance(manifest, dict):
            raise ModelIntegrityError("Model checksum manifest is malformed")
        if manifest.get("version") != 1:
            raise ModelIntegrityError("Unsupported model checksum manifest version")
        if manifest.get("model") != model_name:
            raise ModelIntegrityError("Model checksum manifest has the wrong model")

        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise ModelIntegrityError("Model checksum manifest has no files")
        expected_files = set(_model_filenames(asset).values())
        if set(files) != expected_files:
            raise ModelIntegrityError("Model checksum manifest has the wrong files")
        return files

    def _validate_model_files(self, model_path: Path, files: dict[str, object]) -> None:
        for filename, metadata in files.items():
            self._validate_model_file(model_path / filename, filename, metadata)

    def _validate_model_file(self, path: Path, filename: str, metadata: object) -> None:
        if not isinstance(metadata, dict):
            raise ModelIntegrityError(
                f"Model checksum metadata is malformed: {filename}"
            )
        self._check_model_file(path, filename)
        if not path.is_file():
            raise ModelIntegrityError(f"Model file is missing: {filename}")
        if path.stat().st_size != metadata.get("size"):
            raise ModelIntegrityError(f"Model file size changed: {filename}")
        if sha256_file(path) != metadata.get("sha256"):
            raise ModelIntegrityError(f"Model checksum failed: {filename}")

    def download(
        self,
        model_name: str,
        *,
        progress: Callable[[int | None, str], object] | None = None,
        cancel_event: Event | None = None,
        asset: ModelAsset | None = None,
    ) -> Path:
        progress = progress or (lambda _percent, _message: None)
        cancel_event = cancel_event or Event()
        asset = asset or self.catalog_loader(model_name)
        filenames = _model_filenames(asset)
        self.configure_environment()
        model_path = self.model_path(model_name)
        self._check_model_path(model_path)
        lock_path = model_path.with_name(f".{model_path.name}.download.lock")
        with exclusive_advisory_lock(lock_path, blocking=True):
            self._check_model_path(model_path)
            model_path.mkdir(parents=True, exist_ok=True)

            if self.is_ready_with_asset(model_name, asset):
                progress(100, "Model is already downloaded and verified")
                return model_path

            lengths = {url: self._content_length(url) for url in asset.urls}
            total_bytes = sum(
                length for length in lengths.values() if length is not None
            )
            downloaded_bytes = 0
            for url in asset.urls:
                filename = filenames[url]
                output = model_path / filename
                self._check_model_file(output, filename)
                expected_length = lengths[url]
                if output.is_file() and (
                    expected_length is None or output.stat().st_size == expected_length
                ):
                    downloaded_bytes += output.stat().st_size
                    continue
                downloaded_bytes = self._download_file(
                    url,
                    output,
                    expected_length,
                    downloaded_bytes,
                    total_bytes,
                    progress,
                    cancel_event,
                )

            self._check_cancelled(cancel_event)
            self._validate_upstream_hash(model_path, asset)
            self._write_checksum_manifest(model_path, asset)
            self.validate(model_name, asset=asset)
            progress(100, "Model download completed and checksums passed")
            return model_path

    def is_ready_with_asset(self, model_name: str, asset: ModelAsset) -> bool:
        try:
            self.validate(model_name, asset=asset)
        except AssetError:
            return False
        return True

    def _download_file(
        self,
        url: str,
        output: Path,
        expected_length: int | None,
        downloaded_before: int,
        total_bytes: int,
        progress: Callable[[int | None, str], object],
        cancel_event: Event,
    ) -> int:
        partial = output.with_suffix(f"{output.suffix}.part")
        self._check_model_file(partial, partial.name)
        existing_bytes = partial.stat().st_size if partial.is_file() else 0
        self._check_cancelled(cancel_event)
        if (
            expected_length is not None
            and partial.is_file()
            and existing_bytes == expected_length
        ):
            return downloaded_before + self._publish_download(
                partial, output, expected_length
            )
        existing_bytes = (
            existing_bytes
            if expected_length is None or existing_bytes < expected_length
            else 0
        )
        headers = {"User-Agent": "VisualNovelTextToSpeech/0.1"}
        if existing_bytes:
            headers["Range"] = f"bytes={existing_bytes}-"
        request = Request(url, headers=headers)
        self._check_cancelled(cancel_event)
        try:
            response_context = self.opener(request, timeout=60)
        except HTTPError as error:
            if error.code != 416:
                raise
            response_context = error
        with response_context as response:
            status = response.getcode()
            if status == 416:
                completed_length = self._resume_length(
                    response, existing_bytes, expected_length, output.name
                )
                self._check_cancelled(cancel_event)
                return downloaded_before + self._publish_download(
                    partial, output, completed_length
                )
            resumes = existing_bytes > 0 and status == 206
            file_length = (
                self._resume_length(
                    response, existing_bytes, expected_length, output.name
                )
                if resumes
                else expected_length
            )
            if file_length is None:
                file_length = self._response_length(response)
            mode = "ab" if resumes else "wb"
            if not resumes:
                existing_bytes = 0
            current_bytes = existing_bytes
            with partial.open(mode) as output_file:
                while True:
                    self._check_cancelled(cancel_event)
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output_file.write(chunk)
                    current_bytes += len(chunk)
                    completed = downloaded_before + current_bytes
                    percent = (
                        min(99, round(completed * 100 / total_bytes))
                        if total_bytes
                        else None
                    )
                    progress(percent, f"Downloading {output.name}")
        self._check_cancelled(cancel_event)
        return downloaded_before + self._publish_download(
            partial, output, file_length, resumes=resumes
        )

    @staticmethod
    def _resume_length(
        response: _ModelResponse,
        existing_bytes: int,
        expected_length: int | None,
        filename: str,
    ) -> int:
        header = response.headers.get("Content-Range", "")
        if response.getcode() == 416:
            match = re.fullmatch(r"bytes \*/(\d+)", header)
            total = int(match[1]) if match is not None else None
            valid = existing_bytes > 0 and total == existing_bytes
        else:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", header)
            total = int(match[3]) if match is not None else None
            valid = (
                match is not None
                and int(match[1]) == existing_bytes
                and int(match[2]) >= existing_bytes
                and int(match[2]) + 1 == total
            )
        if (
            total is None
            or not valid
            or (expected_length is not None and total != expected_length)
        ):
            raise ModelIntegrityError(
                f"Model download returned an invalid resume range: {filename}"
            )
        return total

    @staticmethod
    def _publish_download(
        partial: Path,
        output: Path,
        expected_length: int | None,
        *,
        resumes: bool = False,
    ) -> int:
        size = partial.stat().st_size
        if expected_length is not None and size != expected_length:
            reason = "an incomplete resume" if resumes else "the wrong size"
            raise ModelIntegrityError(
                f"Model download returned {reason}: {output.name}"
            )
        partial.replace(output)
        return size

    def _content_length(self, url: str) -> int | None:
        request = Request(
            url,
            method="HEAD",
            headers={"User-Agent": "VisualNovelTextToSpeech/0.1"},
        )
        try:
            with self.opener(request, timeout=30) as response:
                return self._response_length(response)
        except OSError, ModelIntegrityError:
            return None

    @staticmethod
    def _response_length(response: _ModelResponse) -> int | None:
        value = response.headers.get("Content-Length")
        if not value:
            return None
        try:
            length = int(value)
            if length < 0:
                raise ValueError("Content-Length must not be negative")
        except ValueError as error:
            raise ModelIntegrityError(
                f"Model download returned an invalid file size: {value!r}"
            ) from error
        return length

    @staticmethod
    def _check_cancelled(cancel_event: Event) -> None:
        if cancel_event.is_set():
            raise ModelDownloadCancelled("Model download cancelled")

    def _adopt_existing_model(self, model_path: Path, asset: ModelAsset) -> None:
        required_files = _model_filenames(asset).values()
        if not model_path.is_dir():
            raise ModelIntegrityError("Model is not downloaded")
        for filename in required_files:
            path = model_path / filename
            self._check_model_file(path, filename)
            if not path.is_file():
                raise ModelIntegrityError("Model is not downloaded")
        self._validate_upstream_hash(model_path, asset)
        self._write_checksum_manifest(model_path, asset)

    @staticmethod
    def _validate_upstream_hash(model_path: Path, asset: ModelAsset) -> None:
        if not asset.expected_hash:
            return
        hash_file = model_path / "hash.md5"
        if not hash_file.is_file():
            raise ModelIntegrityError("Model publisher checksum is missing")
        published_hash = hash_file.read_text(encoding="utf-8").strip()
        if published_hash != asset.expected_hash:
            raise ModelIntegrityError("Model publisher checksum does not match")

    @staticmethod
    def _write_checksum_manifest(model_path: Path, asset: ModelAsset) -> None:
        files = {}
        for filename in _model_filenames(asset).values():
            path = model_path / filename
            if not path.is_file():
                raise ModelIntegrityError(
                    f"Downloaded model file is missing: {filename}"
                )
            ModelAssetManager._check_model_file(path, filename)
            files[filename] = {
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        atomic_write_json(
            model_path / asset_manifest_name,
            {"version": 1, "model": asset.name, "files": files},
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
    ) -> Path:
        character = (character or "").strip()
        if not character:
            raise VoiceManifestError("Character name is required")
        references = self._validate_reference_files(reference_files)
        pack_path = self.storage_root / slugify(pack, fallback="asset")
        self._check_pack_path(pack_path)
        lock_path = pack_path.with_name(f".{pack_path.name}.import.lock")
        with exclusive_advisory_lock(lock_path, blocking=True):
            self._check_pack_path(pack_path)
            manifest_path: Path = pack_path / "manifest.json"
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
                    shutil.copy2(source, output)
            except Exception:
                for output in copied:
                    output.unlink(missing_ok=True)
                raise
            registry = self._publish_pack(
                pack_path, manifest_path, updated_manifest, copied
            )
            self._remove_unreferenced_files(pack_path, registry)
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
                        shutil.copy2(reference, output)
                        copied_references.append(f"references/{output.name}")
                    entries.append(
                        {
                            "character": voice.character,
                            "speaker": voice.speaker,
                            "aliases": list(voice.aliases),
                            "references": copied_references,
                        }
                    )
            except Exception:
                for output in copied:
                    output.unlink(missing_ok=True)
                raise
            entries.sort(key=lambda item: str(item["character"]).casefold())
            output_manifest: Path = pack_path / "manifest.json"
            imported_registry = self._publish_pack(
                pack_path, output_manifest, {"version": 2, "voices": entries}, copied
            )
            self._remove_unreferenced_files(pack_path, imported_registry)
            return output_manifest

    def _publish_pack(
        self,
        pack_path: Path,
        manifest_path: Path,
        document: dict[str, object],
        copied: Sequence[Path],
    ) -> CharacterVoiceRegistry:
        checksum_path = pack_path / asset_manifest_name
        replacement_started = False
        try:
            if checksum_path.is_symlink() or checksum_path.is_junction():
                raise ModelIntegrityError(
                    "Voice checksum manifest must not be an alias"
                )
            # ponytail: rollback handles exceptions; crash-atomic publication needs a versioned pack directory.
            with TemporaryDirectory(
                dir=pack_path, prefix=".voice-pack-backup-"
            ) as temp:
                backups = []
                for destination in (manifest_path, checksum_path):
                    backup = Path(temp) / destination.name
                    if destination.exists():
                        backup.write_bytes(destination.read_bytes())
                    backups.append((destination, backup))
                try:
                    replacement_started = True
                    atomic_write_json(manifest_path, document)
                    registry = CharacterVoiceRegistry.from_file(manifest_path)
                    self._write_voice_checksums(pack_path, manifest_path, registry)
                except Exception:
                    for destination, backup in backups:
                        if backup.exists():
                            os.replace(backup, destination)
                        else:
                            destination.unlink(missing_ok=True)
                    replacement_started = False
                    raise
        except Exception:
            if not replacement_started:
                for output in copied:
                    output.unlink(missing_ok=True)
            raise
        return registry

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
        if manifest.get("version") != 1:
            raise ModelIntegrityError("Unsupported voice checksum manifest version")
        if manifest.get("manifest_sha256") != sha256_file(manifest_path):
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
            if not path.is_file() or sha256_file(path) != expected:
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
            str(reference.relative_to(pack_path)): sha256_file(reference)
            for voice in voices
            for reference in voice.references
        }
        atomic_write_json(
            pack_path / asset_manifest_name,
            {
                "version": 1,
                "manifest_sha256": sha256_file(manifest_path),
                "files": files,
            },
        )


def load_coqui_model_asset(model_name: str) -> ModelAsset:
    from TTS.utils.manage import ModelManager

    try:
        model_type, language, dataset, model = model_name.split("/")
        item = ModelManager().models_dict[model_type][language][dataset][model]
    except (KeyError, ValueError) as error:
        raise UnsupportedModelError(f"Unknown Coqui model: {model_name}") from error
    urls = item.get("hf_url")
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        raise UnsupportedModelError(
            f"In-app download is not supported for {model_name}"
        )
    return ModelAsset(
        name=model_name,
        urls=tuple(urls),
        expected_hash=item.get("model_hash"),
    )


def read_json(path: str | os.PathLike[str], default: object) -> object:
    path = Path(path)
    if path.is_symlink() or path.is_junction():
        return default
    try:
        with path.open("rb") as source:
            payload = source.read(_VOICE_MANIFEST_READ_LIMIT + 1)
        if len(payload) > _VOICE_MANIFEST_READ_LIMIT:
            return default
        return json.loads(payload)
    except OSError, UnicodeError, json.JSONDecodeError:
        return default

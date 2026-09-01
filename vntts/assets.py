import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.text_utils import slugify

from vntts.settings import get_local_data_directory
from vntts.voices import CharacterVoiceRegistry, VoiceManifestError

asset_manifest_name = "vntts-asset.json"
supported_audio_extensions = {".flac", ".m4a", ".mp3", ".ogg", ".wav"}


class AssetError(RuntimeError):
    pass


class UnsupportedModelError(AssetError):
    pass


class ModelDownloadCancelled(AssetError):
    pass


class ModelIntegrityError(AssetError):
    pass


@dataclass(frozen=True)
class ModelAsset:
    name: str
    urls: tuple[str, ...]
    expected_hash: str | None = None

    @property
    def directory_name(self):
        return self.name.replace("/", "--")


class ModelAssetManager:
    def __init__(self, storage_root=None, *, opener=None, catalog_loader=None):
        self.storage_root = (
            Path(storage_root or get_local_data_directory() / "models")
            .expanduser()
            .resolve(strict=False)
        )
        self.opener = opener or urlopen
        self.catalog_loader = catalog_loader or load_coqui_model_asset

    @property
    def coqui_cache_root(self):
        return self.storage_root / "tts"

    def configure_environment(self):
        os.environ["TTS_HOME"] = str(self.storage_root)
        return self.storage_root

    def configure_huggingface_environment(self):
        cache_root = self.storage_root / "huggingface"
        os.environ["HF_HOME"] = str(cache_root)
        return cache_root

    def model_path(self, model_name):
        return self.coqui_cache_root / model_name.replace("/", "--")

    def is_ready(self, model_name):
        try:
            self.validate(model_name)
        except AssetError:
            return False
        return True

    def validate(self, model_name, *, asset=None):
        asset = asset or self.catalog_loader(model_name)
        model_path = self.model_path(model_name)
        manifest_path = model_path / asset_manifest_name
        if not manifest_path.is_file():
            self._adopt_existing_model(model_path, asset)

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModelIntegrityError(
                f"Unable to read model checksum manifest: {error}"
            ) from error
        if manifest.get("model") != model_name:
            raise ModelIntegrityError("Model checksum manifest has the wrong model")

        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise ModelIntegrityError("Model checksum manifest has no files")
        expected_files = {Path(urlparse(url).path).name for url in asset.urls}
        if set(files) != expected_files:
            raise ModelIntegrityError("Model checksum manifest has the wrong files")
        for filename, metadata in files.items():
            path = model_path / filename
            if not path.is_file():
                raise ModelIntegrityError(f"Model file is missing: {filename}")
            if path.stat().st_size != metadata.get("size"):
                raise ModelIntegrityError(f"Model file size changed: {filename}")
            if sha256_file(path) != metadata.get("sha256"):
                raise ModelIntegrityError(f"Model checksum failed: {filename}")

        self._validate_upstream_hash(model_path, asset)
        return model_path

    def download(
        self,
        model_name,
        *,
        progress=None,
        cancel_event=None,
        asset=None,
    ):
        progress = progress or (lambda _percent, _message: None)
        cancel_event = cancel_event or Event()
        asset = asset or self.catalog_loader(model_name)
        self.configure_environment()
        model_path = self.model_path(model_name)
        model_path.mkdir(parents=True, exist_ok=True)

        if self.is_ready_with_asset(model_name, asset):
            progress(100, "Model is already downloaded and verified")
            return model_path

        lengths = {url: self._content_length(url) for url in asset.urls}
        total_bytes = sum(length for length in lengths.values() if length is not None)
        downloaded_bytes = 0
        for url in asset.urls:
            filename = Path(urlparse(url).path).name
            if not filename:
                raise AssetError(f"Model URL has no filename: {url}")
            output = model_path / filename
            expected_length = lengths[url]
            if output.is_file() and (
                expected_length is None or output.stat().st_size == expected_length
            ):
                downloaded_bytes += output.stat().st_size
                continue
            downloaded_bytes = self._download_file(
                url,
                output,
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

    def is_ready_with_asset(self, model_name, asset):
        try:
            self.validate(model_name, asset=asset)
        except AssetError:
            return False
        return True

    def _download_file(
        self,
        url,
        output,
        downloaded_before,
        total_bytes,
        progress,
        cancel_event,
    ):
        partial = output.with_suffix(f"{output.suffix}.part")
        existing_bytes = partial.stat().st_size if partial.is_file() else 0
        headers = {"User-Agent": "VisualNovelTextToSpeech/0.1"}
        if existing_bytes:
            headers["Range"] = f"bytes={existing_bytes}-"
        request = Request(url, headers=headers)
        self._check_cancelled(cancel_event)
        with self.opener(request, timeout=60) as response:
            status = getattr(response, "status", None) or response.getcode()
            resumes = existing_bytes > 0 and status == 206
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
        partial.replace(output)
        return downloaded_before + output.stat().st_size

    def _content_length(self, url):
        request = Request(
            url,
            method="HEAD",
            headers={"User-Agent": "VisualNovelTextToSpeech/0.1"},
        )
        try:
            with self.opener(request, timeout=30) as response:
                value = response.headers.get("Content-Length")
                return int(value) if value else None
        except Exception:
            return None

    @staticmethod
    def _check_cancelled(cancel_event):
        if cancel_event.is_set():
            raise ModelDownloadCancelled("Model download cancelled")

    def _adopt_existing_model(self, model_path, asset):
        required_files = [Path(urlparse(url).path).name for url in asset.urls]
        if not model_path.is_dir() or not all(
            (model_path / filename).is_file() for filename in required_files
        ):
            raise ModelIntegrityError("Model is not downloaded")
        self._validate_upstream_hash(model_path, asset)
        self._write_checksum_manifest(model_path, asset)

    @staticmethod
    def _validate_upstream_hash(model_path, asset):
        if not asset.expected_hash:
            return
        hash_file = model_path / "hash.md5"
        if not hash_file.is_file():
            raise ModelIntegrityError("Model publisher checksum is missing")
        published_hash = hash_file.read_text(encoding="utf-8").strip()
        if published_hash != asset.expected_hash:
            raise ModelIntegrityError("Model publisher checksum does not match")

    @staticmethod
    def _write_checksum_manifest(model_path, asset):
        files = {}
        for url in asset.urls:
            filename = Path(urlparse(url).path).name
            path = model_path / filename
            if not path.is_file():
                raise ModelIntegrityError(
                    f"Downloaded model file is missing: {filename}"
                )
            files[filename] = {
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        atomic_write_json(
            model_path / asset_manifest_name,
            {"version": 1, "model": asset.name, "files": files},
        )


class VoicePackManager:
    def __init__(self, storage_root=None):
        self.storage_root = (
            Path(storage_root or get_local_data_directory() / "voice-packs")
            .expanduser()
            .resolve(strict=False)
        )

    def import_voice(self, character, reference_files, *, aliases=(), pack="custom"):
        character = (character or "").strip()
        if not character:
            raise VoiceManifestError("Character name is required")
        references = self._validate_reference_files(reference_files)
        pack_path = self.storage_root / slugify(pack, fallback="asset")
        references_path = pack_path / "references"
        references_path.mkdir(parents=True, exist_ok=True)
        manifest_path = pack_path / "manifest.json"
        manifest = read_json(manifest_path, {"version": 2, "voices": []})
        voices = manifest.get("voices")
        if not isinstance(voices, list):
            raise VoiceManifestError("Existing voice manifest is invalid")

        copied = []
        try:
            for source in references:
                filename = (
                    f"{slugify(character, fallback='asset')}-{uuid4().hex[:10]}"
                    f"{source.suffix.casefold()}"
                )
                output = references_path / filename
                shutil.copy2(source, output)
                copied.append(output)
        except Exception:
            for output in copied:
                output.unlink(missing_ok=True)
            raise

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
        voices.sort(key=lambda item: item["character"].casefold())
        atomic_write_json(
            manifest_path,
            {"version": 2, "voices": voices},
        )
        CharacterVoiceRegistry.from_file(manifest_path)
        self._write_voice_checksums(pack_path, manifest_path)
        return manifest_path

    def import_pack(self, source_manifest, *, pack_name=None):
        source_manifest = Path(source_manifest).expanduser().resolve()
        registry = CharacterVoiceRegistry.from_file(source_manifest)
        pack_name = pack_name or source_manifest.parent.name
        pack_path = self.storage_root / slugify(pack_name, fallback="asset")
        references_path = pack_path / "references"
        references_path.mkdir(parents=True, exist_ok=True)

        unique_voices = {
            id(voice): voice for voice in registry.voices.values()
        }.values()
        entries = []
        for voice in unique_voices:
            copied_references = []
            for reference in voice.references:
                if not reference.is_file():
                    raise VoiceManifestError(
                        f"Voice reference does not exist: {reference}"
                    )
                output = references_path / (
                    f"{slugify(voice.character, fallback='asset')}-{uuid4().hex[:10]}"
                    f"{reference.suffix.casefold()}"
                )
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
        entries.sort(key=lambda item: item["character"].casefold())
        output_manifest = pack_path / "manifest.json"
        atomic_write_json(output_manifest, {"version": 2, "voices": entries})
        CharacterVoiceRegistry.from_file(output_manifest)
        self._write_voice_checksums(pack_path, output_manifest)
        return output_manifest

    def validate(self, manifest_path):
        manifest_path = Path(manifest_path).expanduser().resolve()
        CharacterVoiceRegistry.from_file(manifest_path)
        checksum_path = manifest_path.parent / asset_manifest_name
        if not checksum_path.is_file():
            self._write_voice_checksums(manifest_path.parent, manifest_path)
        manifest = read_json(checksum_path, {})
        if manifest.get("manifest_sha256") != sha256_file(manifest_path):
            raise ModelIntegrityError("Voice manifest checksum failed")
        for filename, expected in manifest.get("files", {}).items():
            path = manifest_path.parent / filename
            if not path.is_file() or sha256_file(path) != expected:
                raise ModelIntegrityError(
                    f"Voice reference checksum failed: {filename}"
                )
        return manifest_path

    @staticmethod
    def _validate_reference_files(reference_files):
        references = [Path(path).expanduser().resolve() for path in reference_files]
        if not references:
            raise VoiceManifestError("Select at least one voice reference")
        for reference in references:
            if not reference.is_file():
                raise VoiceManifestError(f"Voice reference does not exist: {reference}")
            if reference.suffix.casefold() not in supported_audio_extensions:
                raise VoiceManifestError(
                    f"Unsupported voice reference format: {reference.suffix}"
                )
        return references

    @staticmethod
    def _write_voice_checksums(pack_path, manifest_path):
        registry = CharacterVoiceRegistry.from_file(manifest_path)
        voices = {id(voice): voice for voice in registry.voices.values()}.values()
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


def load_coqui_model_asset(model_name):
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


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return default

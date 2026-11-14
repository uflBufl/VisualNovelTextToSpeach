import os
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from ntpath import isreserved
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import Event
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from durable_file import atomic_write_json

from vntts.asset_integrity import AssetError, ModelIntegrityError, asset_manifest_name
from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.coqui_models import COQUI_MODEL_SOURCES
from vntts.document_identity import file_sha256
from vntts.json_types import decode_json
from vntts.path_safety import open_regular_binary, read_regular_text
from vntts.settings import get_local_data_directory

_ASSET_MANIFEST_READ_LIMIT = 64 * 1024


class _ModelResponse(Protocol):
    @property
    def headers(self) -> Mapping[str, str]: ...

    def read(self, size: int) -> bytes: ...

    def getcode(self) -> int: ...


class _ModelOpener(Protocol):
    def __call__(
        self, request: Request, *, timeout: int
    ) -> AbstractContextManager[_ModelResponse]: ...


class UnsupportedModelError(AssetError):
    pass


class ModelDownloadCancelled(AssetError):
    pass


def _is_flat_storage_name(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and PureWindowsPath(value).name == value
        and not isreserved(value)
        and not value.startswith(" ")
    )


def _model_directory_name(model_name: str) -> str:
    directory = model_name.replace("/", "--")
    if not _is_flat_storage_name(directory):
        raise ModelIntegrityError("Model name must resolve to one cache directory")
    if source := COQUI_MODEL_SOURCES.get(model_name):
        # Keep old verified checkpoints intact; Coqui 0.28 can rewrite them.
        directory += f"--{source.repo_id.replace('/', '--')}--{source.revision}"
    return directory


def _model_filename(url: str) -> str:
    filename = PurePosixPath(urlparse(url).path).name
    if not filename:
        raise ModelIntegrityError(f"Model URL has no filename: {url}")
    if not _is_flat_storage_name(filename):
        raise ModelIntegrityError(f"Model URL has an unsafe filename: {filename!r}")
    return filename


@dataclass(frozen=True)
class ModelAsset:
    name: str
    urls: tuple[str, ...]
    expected_hash: str | None = None

    @property
    def directory_name(self) -> str:
        return _model_directory_name(self.name)


def _model_filenames(asset: ModelAsset) -> dict[str, str]:
    filenames = {url: _model_filename(url) for url in asset.urls}
    names = {name.casefold() for name in filenames.values()}
    if len(names) != len(asset.urls):
        raise ModelIntegrityError("Model URLs resolve to the same filename")
    managed_names = {asset_manifest_name, *(f"{name}.part" for name in names)}
    if names & managed_names:
        raise ModelIntegrityError("Model URLs conflict with managed download filenames")
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

    def huggingface_environment(
        self, environment: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        result = dict(os.environ if environment is None else environment)
        result.setdefault("HF_HOME", str(self.storage_root / "huggingface"))
        return result

    def configure_huggingface_environment(self) -> Path:
        environment = self.huggingface_environment()
        os.environ["HF_HOME"] = environment["HF_HOME"]
        return Path(environment["HF_HOME"])

    def model_path(self, model_name: str) -> Path:
        return self.coqui_cache_root / _model_directory_name(model_name)

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

    def is_ready(self, model_name: str, *, asset: ModelAsset | None = None) -> bool:
        try:
            self.validate(model_name, asset=asset)
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
            with open_regular_binary(manifest_path) as source:
                payload = source.read(_ASSET_MANIFEST_READ_LIMIT + 1)
            if len(payload) > _ASSET_MANIFEST_READ_LIMIT:
                raise ValueError("checksum manifest is too large")
            manifest: object = decode_json(payload)
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
        version = manifest.get("version")
        if type(version) is not int or version != 1:
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
        size = metadata.get("size")
        if type(size) is not int or size < 0:
            raise ModelIntegrityError(f"Model checksum size is malformed: {filename}")
        self._check_model_file(path, filename)
        if not path.is_file():
            raise ModelIntegrityError(f"Model file is missing: {filename}")
        if path.stat().st_size != size:
            raise ModelIntegrityError(f"Model file size changed: {filename}")
        if file_sha256(path, error_type=ModelIntegrityError) != metadata.get("sha256"):
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
        self._check_cancelled(cancel_event)
        asset = asset or self.catalog_loader(model_name)
        self._check_cancelled(cancel_event)
        filenames = _model_filenames(asset)
        model_path = self.model_path(model_name)
        self.configure_environment()
        self._check_model_path(model_path)
        lock_path = model_path.with_name(f".{model_path.name}.download.lock")
        with exclusive_advisory_lock(
            lock_path,
            blocking=True,
            check_cancelled=lambda: self._check_cancelled(cancel_event),
        ):
            self._check_model_path(model_path)
            model_path.mkdir(parents=True, exist_ok=True)

            if self.is_ready_with_asset(model_name, asset):
                progress(100, "Model is already downloaded and verified")
                return model_path

            reuse_unverified_files = not (model_path / asset_manifest_name).exists()
            lengths: dict[str, int | None] = {}
            for url in asset.urls:
                self._check_cancelled(cancel_event)
                lengths[url] = self._content_length(url)
                self._check_cancelled(cancel_event)
            total_bytes = sum(
                length for length in lengths.values() if length is not None
            )
            downloaded_bytes = 0
            for url in asset.urls:
                self._check_cancelled(cancel_event)
                filename = filenames[url]
                output = model_path / filename
                self._check_model_file(output, filename)
                expected_length = lengths[url]
                if (
                    reuse_unverified_files
                    and output.is_file()
                    and (
                        expected_length is None
                        or output.stat().st_size == expected_length
                    )
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
        return self.is_ready(model_name, asset=asset)

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
                if status == 206
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
        published_hash = read_regular_text(hash_file, encoding="utf-8").strip()
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
                "sha256": file_sha256(path, error_type=ModelIntegrityError),
            }
        atomic_write_json(
            model_path / asset_manifest_name,
            {"version": 1, "model": asset.name, "files": files},
        )


def load_coqui_model_asset(model_name: str) -> ModelAsset:
    try:
        source = COQUI_MODEL_SOURCES[model_name]
    except KeyError as error:
        raise UnsupportedModelError(
            f"In-app download is not supported for {model_name}"
        ) from error
    return ModelAsset(
        name=model_name,
        urls=tuple(
            f"https://huggingface.co/{source.repo_id}/resolve/{source.revision}/{name}"
            for name in source.files
        ),
    )

"""Pinned, resumable Windows MOSS downloads for ordinary app startup."""

import hashlib
import os
import platform
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath
from time import monotonic
from urllib.request import Request, urlopen
from zipfile import ZipFile

from vntts.application_directories import get_local_data_directory
from vntts.authoring.advisory_lock import AdvisoryLockBusyError, exclusive_advisory_lock
from vntts.runtime_installation import _check_cancelled, _run
from vntts.services.tts_engine import TTSConfigurationError

RELEASE = "v0.3.0-vntts-timing-2"
ARCHIVE = (
    "https://github.com/AlexRedby/openmoss/releases/download/v0.3.0-vntts-timing-2/moss-native-timing-adaptive-windows-x64.zip",
    "e2abda985a00bf2883207ce70a6de436365ee9567327b287ae0a823685dd6076",
    17390235,
)
MODEL_REVISION = "9bfc4d52c9b2e8ee61c12384cbe606212bc743f7"
MODEL_NAME = "moss-tts-local-1.5-q8_0.gguf"
MODELS = (
    (
        MODEL_NAME,
        "71d19fe18e443749b72cdb1ab0575797676017393d37eaef6898eb791bba4069",
        4645051104,
    ),
    (
        "moss-tts-local-1.5-q8_0.extras.gguf",
        "687fec93e8e3a2bcc4c973444cbec41f88387bd1f297678dde093324e03090e6",
        4462667968,
    ),
)
DOWNLOAD_HEADROOM_BYTES = 128 * 1024 * 1024


class MossCppInstallRequired(TTSConfigurationError):
    def __init__(self, download_bytes, required_bytes, free_bytes):
        self.download_space = (download_bytes, required_bytes, free_bytes)
        super().__init__(
            "OpenMOSS files are not installed. Return to setup and choose "
            "Install OpenMOSS."
        )


def installation_root():
    return get_local_data_directory() / "models" / "moss-cpp"


def configured_paths(model_name=None, *, root=None):
    root = Path(root or installation_root())
    server = Path(
        os.environ.get("VNTTS_MOSS_CPP_EXECUTABLE")
        or root / RELEASE / "moss-tts-server.exe"
    ).expanduser()
    model = (
        model_name
        if str(model_name or "").lower().endswith(".gguf")
        else os.environ.get("VNTTS_MOSS_GGUF")
    )
    model = Path(model or root / MODEL_REVISION / MODEL_NAME).expanduser()
    return server, model, model.with_suffix(".extras.gguf")


def _remaining_download_bytes(output, size):
    if not output.is_symlink() and output.is_file() and output.stat().st_size == size:
        return 0
    partial = output.with_suffix(output.suffix + ".part")
    received = (
        partial.stat().st_size if not partial.is_symlink() and partial.is_file() else 0
    )
    return size - received if 0 <= received <= size else size


def managed_download_space(model_name=None, *, root=None):
    """Return remaining download, required free, and available bytes."""
    root = Path(root or installation_root())
    paths = configured_paths(model_name, root=root)
    targets = []
    archive = root / "runtime.zip"
    if not os.environ.get("VNTTS_MOSS_CPP_EXECUTABLE") and (
        archive.exists() or archive.is_symlink() or not paths[0].is_file()
    ):
        targets.append((archive, ARCHIVE[2]))
    if not (
        os.environ.get("VNTTS_MOSS_GGUF")
        or str(model_name or "").lower().endswith(".gguf")
    ):
        targets.extend((paths[1].parent / name, size) for name, _digest, size in MODELS)
    remaining = sum(_remaining_download_bytes(output, size) for output, size in targets)
    parent = root
    while not parent.exists():
        parent = parent.parent
    required = remaining + DOWNLOAD_HEADROOM_BYTES if remaining else 0
    return remaining, required, shutil.disk_usage(parent).free


def _hash(path, cancellation):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            _check_cancelled(cancellation)
            digest.update(chunk)
    return digest.hexdigest()


def _download(
    url,
    expected,
    size,
    output,
    progress,
    cancellation,
    *,
    allow_download=False,
):
    _check_cancelled(cancellation)
    if output.is_symlink():
        raise TTSConfigurationError(f"Unsafe MOSS download path: {output}")
    if output.is_file() and output.stat().st_size == size:
        progress(f"Verifying {output.name}...")
        if _hash(output, cancellation) == expected:
            return
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    if partial.is_symlink():
        raise TTSConfigurationError(f"Unsafe MOSS partial download: {partial}")
    received = partial.stat().st_size if partial.is_file() else 0
    if received > size:
        partial.unlink()
        received = 0
    if received == size:
        if _hash(partial, cancellation) == expected:
            partial.replace(output)
            return
        partial.unlink()
        received = 0
    required = size - received + DOWNLOAD_HEADROOM_BYTES
    free = shutil.disk_usage(output.parent).free
    if not allow_download:
        raise MossCppInstallRequired(size - received, required, free)
    if free < required:
        raise TTSConfigurationError(
            "Not enough free disk space for MOSS. Its model files require about 9.1 GB."
        )
    headers = {"User-Agent": "VNTTS"}
    if received:
        headers["Range"] = f"bytes={received}-"
    progress(
        f"Downloading {output.name}: {received / 1e9:.2f} / {size / 1e9:.2f} GB..."
    )
    with urlopen(Request(url, headers=headers), timeout=15) as response:
        resume = received > 0 and response.status == 206
        if (
            resume
            and response.headers.get("Content-Range")
            != f"bytes {received}-{size - 1}/{size}"
        ):
            raise TTSConfigurationError(
                "MOSS download returned an invalid resume range. Retry setup."
            )
        if not resume:
            received = 0
        last_progress = monotonic()
        with partial.open("ab" if resume else "wb") as target:
            while True:
                _check_cancelled(cancellation)
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > size:
                    raise TTSConfigurationError(
                        "MOSS download exceeded its pinned size"
                    )
                target.write(chunk)
                if monotonic() - last_progress >= 1:
                    progress(
                        f"Downloading {output.name}: {received / 1e9:.2f} / {size / 1e9:.2f} GB..."
                    )
                    last_progress = monotonic()
    _check_cancelled(cancellation)
    if received != size:
        raise TTSConfigurationError(
            "MOSS download was interrupted; restart setup to resume it."
        )
    progress(f"Verifying {output.name}...")
    if _hash(partial, cancellation) != expected:
        partial.unlink()
        raise TTSConfigurationError("MOSS download checksum failed; retry setup.")
    partial.replace(output)


def _extract_runtime(archive, destination, cancellation):
    with ZipFile(archive) as source:
        entries = source.infolist()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or ":" in entry.filename
                or stat.S_ISLNK(entry.external_attr >> 16)
            ):
                raise TTSConfigurationError("Unsafe MOSS runtime archive")
        if sum(entry.file_size for entry in entries) > 128 * 1024 * 1024:
            raise TTSConfigurationError("MOSS runtime archive is too large")
        destination.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            _check_cancelled(cancellation)
            if entry.is_dir():
                continue
            target = destination / entry.filename
            target.resolve().relative_to(destination.resolve())
            if target.is_symlink():
                raise TTSConfigurationError("Unsafe installed MOSS runtime file")
            data = source.read(entry)
            expected = hashlib.sha256(data).hexdigest()
            if target.is_file() and _hash(target, cancellation) == expected:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".tmp")
            if partial.is_symlink():
                raise TTSConfigurationError("Unsafe MOSS runtime staging file")
            partial.write_bytes(data)
            partial.replace(target)


def ensure_moss_cpp(
    model_name=None,
    *,
    cancellation=None,
    progress=None,
    root=None,
    allow_download=False,
):
    progress = progress or (lambda _message: None)
    root = Path(root or installation_root())
    paths = configured_paths(model_name, root=root)
    explicit_server = bool(os.environ.get("VNTTS_MOSS_CPP_EXECUTABLE"))
    explicit_model = bool(
        os.environ.get("VNTTS_MOSS_GGUF")
        or str(model_name or "").lower().endswith(".gguf")
    )
    if explicit_server and explicit_model:
        return paths  # Explicit installations are validated by the backend, never repaired here.
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise TTSConfigurationError(
            "Automatic MOSS C++ setup currently supports Windows x64. Configure a native server and GGUF model on this platform."
        )
    remaining, required, free = managed_download_space(model_name, root=root)
    if remaining and not allow_download:
        raise MossCppInstallRequired(remaining, required, free)
    if free < required:
        raise TTSConfigurationError(
            "Not enough free disk space for OpenMOSS: "
            f"{required / 1e9:.1f} GB required including working space, "
            f"{free / 1e9:.1f} GB available."
        )
    try:
        with exclusive_advisory_lock(root / "setup.lock"):
            _check_cancelled(cancellation)
            if not explicit_server and (
                (root / "runtime.zip").exists()
                or (root / "runtime.zip").is_symlink()
                or not paths[0].is_file()
            ):
                archive = root / "runtime.zip"
                _download(
                    *ARCHIVE,
                    archive,
                    progress,
                    cancellation,
                    allow_download=allow_download,
                )
                _extract_runtime(archive, paths[0].parent, cancellation)
            progress("Checking MOSS native runtime...")
            try:
                _run([str(paths[0]), "--help"], cancellation=cancellation, timeout=30)
            except TTSConfigurationError as error:
                repaired = False
                archive = root / "runtime.zip"
                if not explicit_server and not archive.is_file():
                    remaining = _remaining_download_bytes(archive, ARCHIVE[2])
                    required = remaining + DOWNLOAD_HEADROOM_BYTES
                    free = shutil.disk_usage(root).free
                    if not allow_download:
                        raise MossCppInstallRequired(
                            remaining, required, free
                        ) from error
                    _download(
                        *ARCHIVE,
                        archive,
                        progress,
                        cancellation,
                        allow_download=True,
                    )
                    _extract_runtime(archive, paths[0].parent, cancellation)
                    try:
                        _run(
                            [str(paths[0]), "--help"],
                            cancellation=cancellation,
                            timeout=30,
                        )
                    except TTSConfigurationError as repaired_error:
                        error = repaired_error
                    else:
                        repaired = True
                if not repaired:
                    raise TTSConfigurationError(
                        "MOSS native runtime check failed. "
                        f"Model downloads have not started. {error}"
                    ) from error
            if not explicit_model:
                for name, digest, size in MODELS:
                    _download(
                        f"https://huggingface.co/ilintar/moss-tts-local-gguf/resolve/{MODEL_REVISION}/{name}",
                        digest,
                        size,
                        paths[1].parent / name,
                        progress,
                        cancellation,
                        allow_download=allow_download,
                    )
            progress("MOSS C++ runtime and model files are ready.")
            return paths
    except AdvisoryLockBusyError as error:
        raise TTSConfigurationError(
            "MOSS setup is already running in another window. Wait for it to finish, then retry."
        ) from error
    except OSError as error:
        raise TTSConfigurationError(
            f"MOSS setup failed: {error}. Retry to resume downloaded files."
        ) from error

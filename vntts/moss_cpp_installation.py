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

RELEASE = "v0.3.0"
ARCHIVE = (
    "https://github.com/pwilkin/openmoss/releases/download/v0.3.0/moss-tts-vulkan-windows-x64.zip",
    "709c5c67e76c4180a38c6516cf908665fa2df6613900298a7d2c58eba5914733",
    15902158,
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


def _hash(path, cancellation):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            _check_cancelled(cancellation)
            digest.update(chunk)
    return digest.hexdigest()


def _download(url, expected, size, output, progress, cancellation):
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
    if shutil.disk_usage(output.parent).free < size - received + 128 * 1024 * 1024:
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


def ensure_moss_cpp(model_name=None, *, cancellation=None, progress=None, root=None):
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
    try:
        with exclusive_advisory_lock(root / "setup.lock"):
            _check_cancelled(cancellation)
            if not explicit_server:
                archive = root / "runtime.zip"
                _download(*ARCHIVE, archive, progress, cancellation)
                _extract_runtime(archive, paths[0].parent, cancellation)
            progress("Checking MOSS native runtime...")
            try:
                _run(
                    [str(paths[0]), "--version"], cancellation=cancellation, timeout=30
                )
            except TTSConfigurationError as error:
                raise TTSConfigurationError(
                    "MOSS native runtime could not start. If Windows reports missing "
                    "MSVCP140 or VCRUNTIME140 DLLs, install Microsoft's Visual C++ x64 "
                    "Redistributable (https://aka.ms/vs/17/release/vc_redist.x64.exe), "
                    f"then retry. Model downloads have not started. {error}"
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

"""Provision vgmstream for game imports, not for ordinary speech playback."""

import hashlib
import os
import platform
import shutil
import signal
import subprocess
import sys
import wave
from collections.abc import Callable, Sequence
from contextlib import suppress
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol, TypeAlias
from urllib.request import Request, urlopen
from zipfile import ZipFile

from durable_file import atomic_write_json, sha256_file

from vntts.application_directories import get_local_data_directory
from vntts.authoring.advisory_lock import AdvisoryLockBusyError, exclusive_advisory_lock
from vntts.cleanup import cleanup_on_exit, temporary_directory
from vntts.document_identity import file_sha256
from vntts.json_types import decode_json
from vntts.path_safety import open_regular_binary
from vntts.runtime_paths import get_bundle_root
from vntts.subprocess_utils import terminate_process

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

VERSION = "r2117"
ARCHIVES: dict[str, tuple[str, str]] = {
    "win32": (
        "vgmstream-win64.zip",
        "6c4a8a3813864fefed081bbd337dbc0ad93bf88e0b92f5db98d7ab258b22dc6c",
    ),
    "linux": (
        "vgmstream-linux.zip",
        "2f98c77f756079f63fbd119939067f1ed461d77e70993bc4cc372736d859c84a",
    ),
}
SOURCE_URL = f"https://codeload.github.com/vgmstream/vgmstream/zip/refs/tags/{VERSION}"
SOURCE_SHA256 = "1b9245a61a6d123f56ad853d9e0098c62447fa9ada58a6c116b195ff6ccb1219"
_VERIFICATION_RECORD_READ_LIMIT = 64 * 1024
PathInput: TypeAlias = str | PathLike[str]
ProgressCallback: TypeAlias = Callable[[str], object]


class Cancellation(Protocol):
    def is_set(self) -> bool: ...


class DecoderSetupError(RuntimeError):
    pass


class DecoderSetupCancelled(DecoderSetupError):
    pass


class DecoderSetupRequired(DecoderSetupError):
    """Homebrew modifies a shared installation and needs explicit UI consent."""


def _cancel(event: Cancellation | None) -> None:
    if event is not None and event.is_set():
        raise DecoderSetupCancelled("Game-audio preparation cancelled")


def find_game_decoder() -> Path | None:
    bundle = get_bundle_root()
    name = "vgmstream-cli.exe" if sys.platform == "win32" else "vgmstream-cli"
    if bundle is not None:
        path = bundle / "vgmstream" / name
        return (
            path
            if path.is_file() and path.resolve().is_relative_to(bundle.resolve())
            else None
        )
    return _find_host_tool(name)


def _find_host_tool(name: str) -> Path | None:
    installed = shutil.which(name)
    if installed:
        return Path(installed).absolute()
    if sys.platform == "darwin":
        for prefix in ("/opt/homebrew", "/usr/local"):
            path = Path(prefix) / "bin" / name
            if path.is_file():
                return path
    return None


def _run(
    command: Sequence[str],
    cancellation: Cancellation | None = None,
    *,
    timeout: float = 900,
) -> None:
    _cancel(cancellation)
    with temporary_directory(prefix="vntts-decoder-log-") as directory:
        output = (Path(directory) / "output").open("w+b")
        with cleanup_on_exit(
            output.close, description="Game-audio decoder log cleanup"
        ):
            process = _start_decoder_process(command, output)
            with cleanup_on_exit(
                lambda: _stop_decoder_process(process),
                description="Game-audio decoder process cleanup",
            ):
                _wait_for_decoder_process(process, cancellation, timeout)
                _raise_decoder_failure(process, output)


def _start_decoder_process(
    command: Sequence[str], output: BinaryIO
) -> subprocess.Popen[bytes]:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if not isinstance(creation_flags, int) or isinstance(creation_flags, bool):
        raise DecoderSetupError("Windows process flags are unavailable")
    return subprocess.Popen(
        command,
        stdout=output,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        creationflags=creation_flags if sys.platform == "win32" else 0,
    )


def _wait_for_decoder_process(
    process: subprocess.Popen[bytes],
    cancellation: Cancellation | None,
    timeout: float,
) -> None:
    from time import monotonic

    deadline = monotonic() + timeout
    while True:
        _cancel(cancellation)
        if monotonic() >= deadline:
            raise DecoderSetupError(
                "Game-audio decoder setup timed out. Retry when ready."
            )
        try:
            process.wait(timeout=0.1)
            return
        except subprocess.TimeoutExpired:
            continue


def _raise_decoder_failure(process: subprocess.Popen[bytes], output: BinaryIO) -> None:
    if process.returncode:
        output.seek(max(0, output.tell() - 2000))
        detail = output.read().decode("utf-8", errors="replace").strip()
        raise DecoderSetupError(f"Game-audio decoder failed: {detail}")


def _stop_decoder_process(process: subprocess.Popen[bytes]) -> None:
    returncode = process.poll()
    # A failed parent can leave installer children in its owned process group.
    if returncode is not None and (os.name == "nt" or returncode == 0):
        return
    if os.name == "nt":
        terminate_process(process)
        return
    # Homebrew can own compiler/download children; stop only the installation group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired, ProcessLookupError:
        pass
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        # ponytail: SIGKILL is the strongest local action; leave OS cleanup.
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def probe_game_decoder(
    path: PathInput, cancellation: Cancellation | None = None
) -> str:
    """Exercise real native loading and PCM decoding, not just file existence."""
    with temporary_directory(prefix="vntts-decoder-probe-") as directory:
        source, output = Path(directory) / "input.wav", Path(directory) / "output.wav"
        pcm = b"\x34\x12" * 240
        with wave.open(str(source), "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)
        _run(
            [str(path), "-i", "-o", str(output), str(source)], cancellation, timeout=15
        )
        try:
            with wave.open(str(output), "rb") as wav:
                if (
                    wav.getnchannels(),
                    wav.getsampwidth(),
                    wav.getframerate(),
                    wav.getnframes(),
                    wav.readframes(241),
                ) != (1, 2, 24000, 240, pcm):
                    raise DecoderSetupError(
                        "Game-audio decoder failed its audio integrity check"
                    )
        except (wave.Error, EOFError) as error:
            raise DecoderSetupError(
                "Game-audio decoder failed its audio integrity check"
            ) from error
    return str(path)


def _download(
    url: str,
    expected: str,
    output: Path,
    progress: ProgressCallback,
    cancellation: Cancellation | None,
) -> None:
    _cancel(cancellation)
    digest = hashlib.sha256()
    received = 0
    with (
        urlopen(Request(url, headers={"User-Agent": "VNTTS"}), timeout=15) as response,
        output.open("wb") as target,
    ):
        while True:
            _cancel(cancellation)
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > 64 * 1024 * 1024:
                raise DecoderSetupError("Decoder download exceeded the size limit")
            target.write(chunk)
            digest.update(chunk)
            progress(f"Downloading game-audio decoder: {received / 1048576:.1f} MB...")
    if digest.hexdigest() != expected:
        raise DecoderSetupError(
            "Game-audio decoder checksum failed. Nothing was installed; retry."
        )


def ensure_game_decoder(
    *,
    cancellation: Cancellation | None = None,
    progress: ProgressCallback | None = None,
    allow_homebrew: bool = False,
    storage_root: PathInput | None = None,
) -> Path:
    progress = progress or (lambda _message: None)
    _cancel(cancellation)
    try:
        path = find_game_decoder()
        if path is not None:
            progress("Checking game-audio decoder...")
            probe_game_decoder(path, cancellation)
            return path
        if get_bundle_root() is not None:
            raise DecoderSetupError(
                "This application bundle is missing its game-audio decoder. Rebuild or reinstall the complete package."
            )
        root = Path(storage_root or get_local_data_directory() / "tools" / "vgmstream")
        with exclusive_advisory_lock(root / "setup.lock"):
            if sys.platform == "darwin":
                return _install_homebrew_decoder(allow_homebrew, progress, cancellation)
            if sys.platform not in ARCHIVES or platform.machine().casefold() not in {
                "x86_64",
                "amd64",
            }:
                raise DecoderSetupError(
                    "Automatic game-audio decoder setup is unavailable on this platform. Install vgmstream-cli on PATH."
                )
            name, digest = ARCHIVES[sys.platform]
            destination = root / f"{VERSION}-{sys.platform}"
            if destination.is_symlink() or destination.is_junction():
                raise DecoderSetupError(
                    "Managed game-audio decoder directory must not be an alias"
                )
            executable = destination / (
                "vgmstream-cli.exe" if sys.platform == "win32" else "vgmstream-cli"
            )
            if _probe_cached_decoder(executable, cancellation):
                return executable
            return _install_managed_decoder(
                executable, name, digest, progress, cancellation
            )
    except AdvisoryLockBusyError as error:
        raise DecoderSetupError(
            "Another VNTTS window is preparing the game-audio decoder. Wait for it to finish, then retry."
        ) from error
    except (OSError, ValueError) as error:
        raise DecoderSetupError(
            f"Unable to prepare the game-audio decoder: {error}. Check your connection and retry."
        ) from error


def _install_homebrew_decoder(
    allow_homebrew: bool,
    progress: ProgressCallback,
    cancellation: Cancellation | None,
) -> Path:
    if not allow_homebrew:
        raise DecoderSetupRequired(
            "Game references need vgmstream. Install it and its audio libraries using Homebrew? This changes your Homebrew installation and may take several minutes."
        )
    brew = _find_host_tool("brew")
    if brew is None:
        raise DecoderSetupError(
            "Homebrew is not installed. Use the packaged VNTTS app (decoder included), or install Homebrew from brew.sh and retry. VNTTS will not install a system package manager silently."
        )
    progress(
        "Installing game-audio decoder and libraries with Homebrew. This may take several minutes..."
    )
    _run([str(brew), "install", "vgmstream"], cancellation)
    path = find_game_decoder()
    if path is None:
        raise DecoderSetupError(
            "Homebrew finished but vgmstream-cli was not found. Check the Homebrew installation and retry."
        )
    probe_game_decoder(path, cancellation)
    return path


def _probe_cached_decoder(executable: Path, cancellation: Cancellation | None) -> bool:
    manifest = executable.parent / "verified.json"
    if manifest.is_file() and not manifest.is_symlink() and not manifest.is_junction():
        try:
            with open_regular_binary(manifest) as source:
                payload = source.read(_VERIFICATION_RECORD_READ_LIMIT + 1)
            if len(payload) > _VERIFICATION_RECORD_READ_LIMIT:
                raise ValueError("verification record is too large")
            verified_files: object = decode_json(payload)
            if (
                not isinstance(verified_files, dict)
                or executable.name not in verified_files
            ):
                return False
            for name, checksum in verified_files.items():
                if (
                    not isinstance(name, str)
                    or Path(name).name != name
                    or not isinstance(checksum, str)
                ):
                    return False
                path = executable.parent / name
                if path.is_symlink() or not path.is_file():
                    return False
                if file_sha256(path, error_type=OSError) != checksum:
                    return False
            probe_game_decoder(executable, cancellation)
            return True
        except OSError, ValueError, TypeError, AttributeError:
            pass
    return False


def _install_managed_decoder(
    executable: Path,
    name: str,
    digest: str,
    progress: ProgressCallback,
    cancellation: Cancellation | None,
) -> Path:
    destination = executable.parent
    progress("Preparing game-audio decoder download...")
    with temporary_directory(prefix="download-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        archive = staging / "decoder.zip"
        _download(
            f"https://github.com/vgmstream/vgmstream/releases/download/{VERSION}/{name}",
            digest,
            archive,
            progress,
            cancellation,
        )
        files: dict[str, str] = {}
        with ZipFile(archive) as package:
            if sum(info.file_size for info in package.infolist()) > 64 * 1024 * 1024:
                raise DecoderSetupError("Decoder archive exceeded the size limit")
            for info in package.infolist():
                _cancel(cancellation)
                if (
                    Path(info.filename).name != info.filename
                    or "\\" in info.filename
                    or info.is_dir()
                ):
                    raise DecoderSetupError(
                        "Decoder archive contains an unexpected path"
                    )
                path = staging / info.filename
                path.write_bytes(package.read(info))
                files[info.filename] = sha256_file(path)
        candidate = staging / executable.name
        candidate.chmod(0o755)
        probe_game_decoder(candidate, cancellation)
        _cancel(cancellation)
        destination.mkdir(parents=True, exist_ok=True)
        for name in files:
            (staging / name).replace(destination / name)
        atomic_write_json(destination / "verified.json", files)
    return executable


def _stage_file(source: PathInput, target: PathInput) -> None:
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with temporary_directory(prefix="copy-", dir=target.parent) as directory:
        temporary = Path(directory) / target.name
        shutil.copyfile(source, temporary)
        temporary.chmod(0o755 if os.access(source, os.X_OK) else 0o644)
        temporary.replace(target)


def stage_game_decoder(destination: PathInput) -> Path:
    """Release builders collect native dependencies with PyInstaller afterwards."""
    destination = Path(destination)
    path = ensure_game_decoder(allow_homebrew=True, progress=print)
    destination.mkdir(parents=True, exist_ok=True)
    _stage_file(path, destination / path.name)
    if sys.platform == "win32":
        for library in path.parent.glob("*.dll"):
            _stage_file(library, destination / library.name)
    elif sys.platform == "darwin":
        from PyInstaller.depend.bindepend import binary_dependency_analysis

        # Include installed dependency notices as well as upstream codec notices.
        for _name, source, _kind in binary_dependency_analysis(
            [(path.name, str(path), "BINARY")]
        ):
            keg = next(
                (
                    p
                    for p in Path(source).resolve().parents
                    if p.parent.parent.name == "Cellar"
                ),
                None,
            )
            if keg is not None:
                for notice in keg.iterdir():
                    if notice.is_file() and notice.name.upper().startswith(
                        ("COPYING", "LICENSE", "NOTICE", "AUTHORS")
                    ):
                        target = destination / "licenses" / keg.parent.name
                        target.mkdir(parents=True, exist_ok=True)
                        _stage_file(notice, target / notice.name)
    with temporary_directory(prefix="vntts-decoder-licenses-") as temporary:
        archive = Path(temporary) / "source.zip"
        _download(SOURCE_URL, SOURCE_SHA256, archive, print, None)
        with ZipFile(archive) as source:
            for info in source.infolist():
                if not info.is_dir() and (
                    info.filename == f"vgmstream-{VERSION}/COPYING"
                    or "/ext_libs/licenses/" in info.filename
                ):
                    output = destination / "licenses" / Path(info.filename).name
                    output.parent.mkdir(exist_ok=True)
                    output.write_bytes(source.read(info))
    return destination


def confirm_decoder_setup(parent: QWidget | None, error: DecoderSetupRequired) -> bool:
    """Ask on the GUI thread only; the actual installation stays in the worker."""
    from PySide6.QtWidgets import QMessageBox

    return bool(
        QMessageBox.question(
            parent,
            "Set up game-audio decoder",
            str(error),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        == QMessageBox.StandardButton.Yes
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Stage the game-audio decoder for a desktop release"
    )
    parser.add_argument("destination", type=Path)
    stage_game_decoder(parser.parse_args().destination)

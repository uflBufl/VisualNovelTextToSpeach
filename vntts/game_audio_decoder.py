"""Provision vgmstream for game imports, not for ordinary speech playback."""

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen
from zipfile import ZipFile

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.application_directories import get_local_data_directory
from vntts.authoring.advisory_lock import AdvisoryLockBusyError, exclusive_advisory_lock
from vntts.runtime_paths import get_bundle_root
from vntts.subprocess_utils import terminate_process

VERSION = "r2117"
ARCHIVES = {
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


class DecoderSetupError(RuntimeError):
    pass


class DecoderSetupCancelled(DecoderSetupError):
    pass


class DecoderSetupRequired(DecoderSetupError):
    """Homebrew modifies a shared installation and needs explicit UI consent."""


def _cancel(event):
    if event is not None and event.is_set():
        raise DecoderSetupCancelled("Game-audio preparation cancelled")


def find_game_decoder():
    bundle = get_bundle_root()
    name = "vgmstream-cli.exe" if sys.platform == "win32" else "vgmstream-cli"
    if bundle is not None:
        path = bundle / "vgmstream" / name
        return path if path.is_file() else None
    installed = shutil.which(name)
    if installed:
        return Path(installed).absolute()
    if sys.platform == "darwin":
        for prefix in ("/opt/homebrew", "/usr/local"):
            path = Path(prefix) / "bin" / name
            if path.is_file():
                return path
    return None


def _run(command, cancellation=None, *, timeout=900):
    _cancel(cancellation)
    with TemporaryDirectory(prefix="vntts-decoder-log-") as directory:
        with (Path(directory) / "output").open("w+b") as output:
            process = subprocess.Popen(
                command,
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
                **(
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if sys.platform == "win32"
                    else {}
                ),
            )
            try:
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
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode:
                    output.seek(max(0, output.tell() - 2000))
                    detail = output.read().decode("utf-8", errors="replace").strip()
                    raise DecoderSetupError(f"Game-audio decoder failed: {detail}")
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        terminate_process(process)
                    else:
                        # Homebrew can own compiler/download children; stop only
                        # the process group created for this installation.
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        except ProcessLookupError:
                            pass
                        finally:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            process.wait()


def probe_game_decoder(path, cancellation=None):
    """Exercise real native loading and PCM decoding, not just file existence."""
    with TemporaryDirectory(prefix="vntts-decoder-probe-") as directory:
        source, output = Path(directory) / "input.wav", Path(directory) / "output.wav"
        pcm = b"\x34\x12" * 240
        with wave.open(str(source), "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)
        _run(
            [str(path), "-i", "-o", str(output), str(source)], cancellation, timeout=15
        )
        with wave.open(str(output), "rb") as wav:
            if (
                wav.getnchannels(),
                wav.getsampwidth(),
                wav.getframerate(),
                wav.readframes(241),
            ) != (1, 2, 24000, pcm):
                raise DecoderSetupError(
                    "Game-audio decoder failed its audio integrity check"
                )
    return str(path)


def _download(url, expected, output, progress, cancellation):
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
    *, cancellation=None, progress=None, allow_homebrew=False, storage_root=None
):
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
                if not allow_homebrew:
                    raise DecoderSetupRequired(
                        "Game references need vgmstream. Install it and its audio libraries using Homebrew? This changes your Homebrew installation and may take several minutes."
                    )
                brew = shutil.which("brew") or next(
                    (
                        str(p)
                        for p in (
                            Path("/opt/homebrew/bin/brew"),
                            Path("/usr/local/bin/brew"),
                        )
                        if p.is_file()
                    ),
                    None,
                )
                if brew is None:
                    raise DecoderSetupError(
                        "Homebrew is not installed. Use the packaged VNTTS app (decoder included), or install Homebrew from brew.sh and retry. VNTTS will not install a system package manager silently."
                    )
                progress(
                    "Installing game-audio decoder and libraries with Homebrew. This may take several minutes..."
                )
                _run([brew, "install", "vgmstream"], cancellation)
                path = find_game_decoder()
                if path is None:
                    raise DecoderSetupError(
                        "Homebrew finished but vgmstream-cli was not found. Check the Homebrew installation and retry."
                    )
                probe_game_decoder(path, cancellation)
                return path
            if sys.platform not in ARCHIVES or platform.machine().casefold() not in {
                "x86_64",
                "amd64",
            }:
                raise DecoderSetupError(
                    "Automatic game-audio decoder setup is unavailable on this platform. Install vgmstream-cli on PATH."
                )
            name, digest = ARCHIVES[sys.platform]
            destination = root / f"{VERSION}-{sys.platform}"
            executable = destination / (
                "vgmstream-cli.exe" if sys.platform == "win32" else "vgmstream-cli"
            )
            manifest = destination / "verified.json"
            if manifest.is_file():
                try:
                    files = json.loads(manifest.read_text(encoding="utf-8"))
                    if executable.name in files and all(
                        Path(n).name == n and sha256_file(destination / n) == h
                        for n, h in files.items()
                    ):
                        probe_game_decoder(executable, cancellation)
                        return executable
                except OSError, ValueError, TypeError, AttributeError:
                    pass
            progress("Preparing game-audio decoder download...")
            with TemporaryDirectory(prefix="download-", dir=root) as temporary:
                staging = Path(temporary)
                archive = staging / "decoder.zip"
                _download(
                    f"https://github.com/vgmstream/vgmstream/releases/download/{VERSION}/{name}",
                    digest,
                    archive,
                    progress,
                    cancellation,
                )
                files = {}
                with ZipFile(archive) as package:
                    if (
                        sum(info.file_size for info in package.infolist())
                        > 64 * 1024 * 1024
                    ):
                        raise DecoderSetupError(
                            "Decoder archive exceeded the size limit"
                        )
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
                destination.mkdir(parents=True, exist_ok=True)
                for name in files:
                    (staging / name).replace(destination / name)
                atomic_write_json(manifest, files)
            return executable
    except AdvisoryLockBusyError as error:
        raise DecoderSetupError(
            "Another VNTTS window is preparing the game-audio decoder. Wait for it to finish, then retry."
        ) from error
    except (OSError, ValueError) as error:
        raise DecoderSetupError(
            f"Unable to prepare the game-audio decoder: {error}. Check your connection and retry."
        ) from error


def _stage_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="copy-", dir=target.parent) as directory:
        temporary = Path(directory) / target.name
        shutil.copyfile(source, temporary)
        temporary.chmod(0o755 if os.access(source, os.X_OK) else 0o644)
        temporary.replace(target)


def stage_game_decoder(destination):
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
    with TemporaryDirectory(prefix="vntts-decoder-licenses-") as temporary:
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


def confirm_decoder_setup(parent, error):
    """Ask on the GUI thread only; the actual installation stays in the worker."""
    from PySide6.QtWidgets import QMessageBox

    return (
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

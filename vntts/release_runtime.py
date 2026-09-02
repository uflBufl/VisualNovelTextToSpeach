"""Build a self-contained, relocation-tested Pocket TTS release runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

BACKEND = "pocket-tts"
PYTHON_VERSION = "3.14"
PROBE_MODULES = (
    "durable_file",
    "numpy",
    "platformdirs",
    "pocket_tts",
    "safetensors",
    "scipy",
    "torch",
    "vntts",
    "vntts_artifacts",
)


def _runtime_interpreter(runtime_root: Path, platform_name: str) -> Path:
    return runtime_root / ("python.exe" if platform_name == "win32" else "bin/python")


def _runtime_site(runtime_root: Path, platform_name: str) -> Path:
    if platform_name == "win32":
        return runtime_root / "Lib/site-packages"
    candidates = sorted((runtime_root / "lib").glob("python*/site-packages"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one site-packages directory under {runtime_root}, "
            f"found {len(candidates)}"
        )
    return candidates[0]


def _find_managed_interpreter(
    managed_root: Path, platform_name: str, python_version: str
) -> Path:
    pattern = (
        "*/python.exe" if platform_name == "win32" else f"*/bin/python{python_version}"
    )
    contained = {
        candidate.resolve()
        for candidate in managed_root.glob(pattern)
        if candidate.is_file()
        and candidate.resolve().is_relative_to(managed_root.resolve())
    }
    if len(contained) != 1:
        raise RuntimeError(
            f"Expected one managed Python {python_version} interpreter under "
            f"{managed_root}, found {len(contained)}"
        )
    return contained.pop()


def _replace_posix_interpreter_link(
    runtime_root: Path, managed_interpreter: Path
) -> None:
    interpreter = _runtime_interpreter(runtime_root, "posix")
    if interpreter.exists() or interpreter.is_symlink():
        interpreter.unlink()
    relative_target = os.path.relpath(managed_interpreter, interpreter.parent)
    interpreter.symlink_to(relative_target)


def _prune_managed_runtime(managed_root: Path, managed_interpreter: Path) -> None:
    """Remove CPython development aliases that confuse frozen bundle layouts."""
    managed_root = managed_root.resolve()
    distribution_root = (
        managed_interpreter.parent
        if managed_interpreter.name.casefold() == "python.exe"
        else managed_interpreter.parents[1]
    )
    if not distribution_root.resolve().is_relative_to(managed_root):
        raise RuntimeError("Managed Python distribution escaped its staging root")
    for alias in managed_root.iterdir():
        if alias.is_symlink():
            alias.unlink()
    for relative in ("include", "share", "lib/pkgconfig"):
        candidate = distribution_root / relative
        if candidate.is_dir():
            shutil.rmtree(candidate)
        elif candidate.exists() or candidate.is_symlink():
            candidate.unlink()


def _promote_windows_runtime(
    managed_root: Path,
    managed_interpreter: Path,
    runtime_root: Path,
) -> Path:
    distribution_root = managed_interpreter.parent
    if not distribution_root.resolve().is_relative_to(managed_root.resolve()):
        raise RuntimeError("Managed Python distribution escaped its staging root")
    shutil.move(distribution_root, runtime_root)
    shutil.rmtree(managed_root)
    return _runtime_interpreter(runtime_root, "win32")


def _prune_runtime_entrypoints(
    managed_root: Path,
    managed_interpreter: Path,
    runtime_root: Path,
    platform_name: str,
) -> None:
    for root in (managed_root, runtime_root):
        if not root.is_dir():
            continue
        for candidate in root.iterdir():
            if candidate.name.startswith("."):
                if candidate.is_dir() and not candidate.is_symlink():
                    shutil.rmtree(candidate)
                else:
                    candidate.unlink()
    runtime_interpreter = _runtime_interpreter(runtime_root, platform_name)
    scripts = runtime_root / ("Scripts" if platform_name == "win32" else "bin")
    if scripts.is_dir():
        for candidate in scripts.iterdir():
            if platform_name == "win32" or candidate != runtime_interpreter:
                if candidate.is_dir() and not candidate.is_symlink():
                    shutil.rmtree(candidate)
                else:
                    candidate.unlink()
    if platform_name == "win32":
        if scripts.is_dir():
            scripts.rmdir()
        return
    for candidate in managed_interpreter.parent.iterdir():
        if candidate != managed_interpreter:
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
    for root in (managed_root, runtime_root):
        for candidate in root.rglob("*"):
            if candidate.is_file() and not candidate.is_symlink():
                candidate.chmod(candidate.stat().st_mode & ~0o111)
    managed_interpreter.chmod(managed_interpreter.stat().st_mode | 0o755)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_checked(run, command, *, environment=None, capture_output=False):
    return run(
        [str(value) for value in command],
        check=True,
        env=environment,
        capture_output=capture_output,
        text=capture_output,
    )


def _probe_script() -> str:
    modules = repr(PROBE_MODULES)
    return (
        "import importlib,json,sys;"
        f"names={modules};"
        "loaded={name:importlib.import_module(name) for name in names};"
        "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,"
        "'base_prefix':sys.base_prefix,'modules':{name:getattr(module,'__file__',"
        "'') for name,module in loaded.items()}},sort_keys=True))"
    )


def runtime_probe_script() -> str:
    """Return the isolated runtime provenance probe used by release checks."""
    return _probe_script()


def _probe_relocated_runtime(
    speech_runtimes: Path,
    *,
    platform_name: str,
    run,
) -> dict:
    with TemporaryDirectory(prefix="vntts-runtime-relocation-") as directory:
        relocated = Path(directory) / "speech-runtimes"
        shutil.copytree(speech_runtimes, relocated, symlinks=True)
        runtime_root = relocated / BACKEND
        interpreter = _runtime_interpreter(runtime_root, platform_name)
        completed = _run_checked(
            run,
            (interpreter, "-I", "-B", "-c", _probe_script()),
            capture_output=True,
        )
        report = json.loads(completed.stdout)
        allowed_root = relocated.resolve()
        origins = {
            "interpreter": Path(report["executable"]),
            "prefix": Path(report["prefix"]),
            "base_prefix": Path(report["base_prefix"]),
            **{
                f"module:{name}": Path(origin)
                for name, origin in report["modules"].items()
                if origin
            },
        }
        escaped = {
            name: str(path)
            for name, path in origins.items()
            if not path.resolve().is_relative_to(allowed_root)
        }
        if escaped:
            raise RuntimeError(
                "Relocated Pocket runtime escaped its staging root: "
                + json.dumps(escaped, sort_keys=True)
            )
        return report


def stage_pocket_runtime(
    project_root,
    destination,
    *,
    uv_executable="uv",
    python_version=PYTHON_VERSION,
    platform_name=sys.platform,
    run=subprocess.run,
) -> Path:
    project_root = Path(project_root).resolve()
    destination = Path(destination).resolve()
    backend_project = project_root / "backends" / BACKEND
    lockfile = backend_project / "uv.lock"
    if not lockfile.is_file():
        raise FileNotFoundError(f"Pocket runtime lockfile is missing: {lockfile}")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    managed_root = destination / "_python"
    runtime_root = destination / BACKEND
    _run_checked(
        run,
        (
            uv_executable,
            "python",
            "install",
            "--install-dir",
            managed_root,
            "--no-bin",
            "--no-registry",
            python_version,
        ),
    )
    managed_interpreter = _find_managed_interpreter(
        managed_root, platform_name, python_version
    )
    _prune_managed_runtime(managed_root, managed_interpreter)

    if platform_name == "win32":
        runtime_interpreter = _promote_windows_runtime(
            managed_root,
            managed_interpreter,
            runtime_root,
        )
        with TemporaryDirectory(prefix="vntts-pocket-lock-") as directory:
            requirements = Path(directory) / "requirements.txt"
            _run_checked(
                run,
                (
                    uv_executable,
                    "export",
                    "--quiet",
                    "--project",
                    backend_project,
                    "--frozen",
                    "--no-dev",
                    "--no-emit-project",
                    "--output-file",
                    requirements,
                ),
            )
            _run_checked(
                run,
                (
                    uv_executable,
                    "pip",
                    "sync",
                    "--python",
                    runtime_interpreter,
                    "--compile-bytecode",
                    requirements,
                ),
            )
    else:
        _run_checked(
            run,
            (
                uv_executable,
                "venv",
                "--relocatable",
                "--python",
                managed_interpreter,
                runtime_root,
            ),
        )
        _replace_posix_interpreter_link(runtime_root, managed_interpreter)
        runtime_interpreter = _runtime_interpreter(runtime_root, platform_name)
        sync_environment = dict(os.environ)
        sync_environment["VIRTUAL_ENV"] = str(runtime_root)
        _run_checked(
            run,
            (
                uv_executable,
                "sync",
                "--project",
                backend_project,
                "--active",
                "--frozen",
                "--no-install-project",
                "--compile-bytecode",
            ),
            environment=sync_environment,
        )
    _runtime_site(runtime_root, platform_name)
    _run_checked(
        run,
        (
            uv_executable,
            "pip",
            "install",
            "--python",
            runtime_interpreter,
            "--no-deps",
            "--reinstall",
            "--compile-bytecode",
            project_root,
        ),
    )
    _prune_runtime_entrypoints(
        managed_root,
        managed_interpreter,
        runtime_root,
        platform_name,
    )
    probe = _probe_relocated_runtime(
        destination,
        platform_name=platform_name,
        run=run,
    )
    manifest = {
        "backend": BACKEND,
        "python_request": python_version,
        "lock_sha256": _sha256(lockfile),
        "project_pyproject_sha256": _sha256(project_root / "pyproject.toml"),
        "probe": probe,
    }
    manifest_path = destination / "runtime-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage the locked Pocket TTS runtime for a release bundle."
    )
    parser.add_argument("destination")
    parser.add_argument("--project-root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--python-version", default=PYTHON_VERSION)
    arguments = parser.parse_args(argv)
    manifest = stage_pocket_runtime(
        arguments.project_root,
        arguments.destination,
        uv_executable=arguments.uv,
        python_version=arguments.python_version,
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

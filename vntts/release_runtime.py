"""Build self-contained, relocation-tested speech runtimes for release bundles."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from durable_file import sha256_file

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
QWEN_PROBE_MODULES = (
    "durable_file",
    "numpy",
    "platformdirs",
    "qwen_tts",
    "scipy",
    "torch",
    "torchaudio",
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


def _remove_runtime_entrypoint(path: Path) -> None:
    if path.is_junction():
        path.rmdir()
    elif path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _prune_runtime_scripts(runtime_root: Path, platform_name: str) -> None:
    scripts = runtime_root / ("Scripts" if platform_name == "win32" else "bin")
    if scripts.is_symlink() or scripts.is_junction():
        _remove_runtime_entrypoint(scripts)
        return
    if not scripts.is_dir():
        return
    if platform_name == "win32":
        _remove_runtime_entrypoint(scripts)
        return
    runtime_interpreter = _runtime_interpreter(runtime_root, platform_name)
    for candidate in scripts.iterdir():
        if candidate != runtime_interpreter:
            _remove_runtime_entrypoint(candidate)


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
                _remove_runtime_entrypoint(candidate)
    _prune_runtime_scripts(runtime_root, platform_name)
    if platform_name == "win32":
        return
    for candidate in managed_interpreter.parent.iterdir():
        if candidate != managed_interpreter:
            _remove_runtime_entrypoint(candidate)
    for root in (managed_root, runtime_root):
        for candidate in root.rglob("*"):
            if candidate.is_file() and not candidate.is_symlink():
                candidate.chmod(candidate.stat().st_mode & ~0o111)
    managed_interpreter.chmod(managed_interpreter.stat().st_mode | 0o755)


def _run_checked(
    run: Callable[..., object],
    command: Sequence[object],
    *,
    environment: Mapping[str, str] | None = None,
    capture_output: bool = False,
) -> object:
    return run(
        [str(value) for value in command],
        check=True,
        env=environment,
        capture_output=capture_output,
        text=capture_output,
    )


def _probe_script(modules: tuple[str, ...] = PROBE_MODULES) -> str:
    module_names = repr(modules)
    return (
        "import importlib,json,sys;"
        f"names={module_names};"
        "loaded={name:importlib.import_module(name) for name in names};"
        "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,"
        "'base_prefix':sys.base_prefix,'modules':{name:getattr(module,'__file__',"
        "'') for name,module in loaded.items()}},sort_keys=True))"
    )


def runtime_probe_script() -> str:
    """Return the isolated runtime provenance probe used by release checks."""
    return _probe_script()


def validate_runtime_provenance(
    report: Mapping[str, object],
    allowed_root: Path,
    *,
    probe_modules: tuple[str, ...] = PROBE_MODULES,
    description: str = "Runtime probe",
) -> None:
    modules = report.get("modules")
    if not isinstance(modules, Mapping):
        raise RuntimeError(f"{description} is missing module origins")
    origins = {
        "interpreter": report.get("executable"),
        "prefix": report.get("prefix"),
        "base_prefix": report.get("base_prefix"),
        **{f"module:{name}": modules.get(name) for name in probe_modules},
        **{f"module:{name}": origin for name, origin in modules.items()},
    }
    missing = sorted(
        name
        for name, origin in origins.items()
        if not isinstance(origin, str) or not origin
    )
    allowed_root = allowed_root.resolve()
    escaped = {
        name: origin
        for name, origin in origins.items()
        if isinstance(origin, str)
        and origin
        and not Path(origin).resolve().is_relative_to(allowed_root)
    }
    if missing or escaped:
        raise RuntimeError(
            f"{description} provenance failed: "
            + json.dumps({"missing": missing, "escaped": escaped}, sort_keys=True)
        )


def _probe_relocated_runtime(
    speech_runtimes: Path,
    *,
    platform_name: str,
    run: Callable[..., object],
    backend: str = BACKEND,
    probe_modules: tuple[str, ...] = PROBE_MODULES,
) -> dict[str, object]:
    with TemporaryDirectory(prefix="vntts-runtime-relocation-") as directory:
        relocated = Path(directory) / "speech-runtimes"
        shutil.copytree(speech_runtimes, relocated, symlinks=True)
        runtime_root = relocated / backend
        interpreter = _runtime_interpreter(runtime_root, platform_name)
        completed = _run_checked(
            run,
            (interpreter, "-I", "-B", "-c", _probe_script(probe_modules)),
            capture_output=True,
        )
        stdout = getattr(completed, "stdout", None)
        if not isinstance(stdout, str):
            raise RuntimeError("Runtime probe did not return JSON on stdout")
        parsed: object = json.loads(stdout)
        if not isinstance(parsed, dict):
            raise RuntimeError("Runtime probe returned a non-object JSON value")
        report: dict[str, object] = parsed
        validate_runtime_provenance(
            report,
            relocated,
            probe_modules=probe_modules,
            description=f"Relocated {backend} runtime",
        )
        return report


def _prepare_runtime_destination(
    destination: Path, backend_project: Path, *, append: bool
) -> Path:
    if destination.is_symlink() or destination.is_junction():
        raise RuntimeError("Speech runtime staging destination must not be an alias")
    destination = destination.resolve()
    lockfile = backend_project / "uv.lock"
    if not lockfile.is_file():
        raise FileNotFoundError(f"Speech runtime lockfile is missing: {lockfile}")
    if backend_project.is_relative_to(destination):
        raise RuntimeError(
            "Speech runtime staging destination contains the source project"
        )
    if append:
        for name in ("_python", backend_project.name):
            path = destination / name
            if path.is_symlink() or path.is_junction():
                raise RuntimeError(
                    f"Speech runtime staging directory must not be an alias: {path}"
                )
    if destination.exists() and not append:
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    runtime_root = destination / backend_project.name
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    return destination


def stage_speech_runtime(
    project_root: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    uv_executable: str | os.PathLike[str] = "uv",
    python_version: str = PYTHON_VERSION,
    platform_name: str = sys.platform,
    run: Callable[..., object] = subprocess.run,
    backend: str = BACKEND,
    append: bool = False,
) -> Path:
    if backend not in {BACKEND, "qwen-tts"}:
        raise ValueError(f"Unsupported release runtime {backend}")
    if backend == "qwen-tts" and platform_name != "win32":
        raise ValueError("The Qwen release runtime currently supports Windows only")
    project_root = Path(project_root).resolve()
    backend_project = project_root / "backends" / backend
    lockfile = backend_project / "uv.lock"
    destination = _prepare_runtime_destination(
        Path(destination), backend_project, append=append
    )
    managed_root = destination / "_python"
    runtime_root = destination / backend
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
        with TemporaryDirectory(prefix=f"vntts-{backend}-lock-") as directory:
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
                    "--break-system-packages",
                    "--compile-bytecode",
                    *(
                        ("--index", "https://download.pytorch.org/whl/cu126")
                        if backend == "qwen-tts"
                        else ()
                    ),
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
            *(("--break-system-packages",) if platform_name == "win32" else ()),
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
        backend=backend,
        probe_modules=(QWEN_PROBE_MODULES if backend == "qwen-tts" else PROBE_MODULES),
    )
    manifest = {
        "backend": backend,
        "python_request": python_version,
        "lock_sha256": sha256_file(lockfile),
        "project_pyproject_sha256": sha256_file(project_root / "pyproject.toml"),
        "probe": probe,
    }
    manifest_path = destination / (
        "runtime-manifest.json"
        if backend == BACKEND
        else f"{backend}-runtime-manifest.json"
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path


# Preserve the public name used before Qwen release runtimes were supported.
stage_pocket_runtime = stage_speech_runtime


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage a locked speech runtime for a release bundle."
    )
    parser.add_argument("destination")
    parser.add_argument("--project-root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--python-version", default=PYTHON_VERSION)
    parser.add_argument("--backend", choices=(BACKEND, "qwen-tts"), default=BACKEND)
    parser.add_argument("--append", action="store_true")
    arguments = parser.parse_args(argv)
    manifest = stage_speech_runtime(
        arguments.project_root,
        arguments.destination,
        uv_executable=arguments.uv,
        python_version=arguments.python_version,
        backend=arguments.backend,
        append=arguments.append,
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

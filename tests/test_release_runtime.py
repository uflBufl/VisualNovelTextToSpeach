import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from tests.symlink_support import symlink_or_skip
from vntts.release_runtime import (
    _find_managed_interpreter,
    _probe_relocated_runtime,
    _promote_windows_runtime,
    _prune_managed_runtime,
    _prune_runtime_entrypoints,
    _replace_posix_interpreter_link,
    _runtime_interpreter,
    _runtime_site,
    _sha256,
    stage_pocket_runtime,
)


class ReleaseRuntimeTest(unittest.TestCase):
    def test_stages_locked_runtime_and_requires_relocation_probe(self):
        with TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            backend = project / "backends" / "pocket-tts"
            backend.mkdir(parents=True)
            (backend / "uv.lock").write_text("locked", encoding="utf-8")
            (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
            destination = Path(directory) / "build" / "speech-runtimes"
            calls = []

            def runner(command, **options):
                calls.append((command, options))
                if command[1:3] == ["python", "install"]:
                    managed_root = Path(command[command.index("--install-dir") + 1])
                    managed_python = (
                        managed_root / "cpython-3.14-test" / "bin" / "python3.14"
                    )
                    managed_python.parent.mkdir(parents=True)
                    managed_python.write_bytes(b"python")
                    return SimpleNamespace(stdout="")
                if command[1] == "venv":
                    runtime = Path(command[-1])
                    interpreter = runtime / "bin/python"
                    interpreter.parent.mkdir(parents=True)
                    interpreter.write_bytes(b"launcher")
                    (runtime / "lib/python3.14/site-packages").mkdir(parents=True)
                    return SimpleNamespace(stdout="")
                if command[1] == "sync" or command[1:3] == ["pip", "install"]:
                    return SimpleNamespace(stdout="")
                interpreter = Path(command[0])
                speech_runtimes = interpreter.parents[2]
                runtime = speech_runtimes / "pocket-tts"
                site = runtime / "lib/python3.14/site-packages"
                report = {
                    "executable": str(interpreter),
                    "prefix": str(runtime),
                    "base_prefix": str(
                        speech_runtimes / "_python" / "cpython-3.14-test"
                    ),
                    "modules": {
                        name: str(site / name / "__init__.py")
                        for name in (
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
                    },
                }
                return SimpleNamespace(stdout=json.dumps(report))

            manifest_path = stage_pocket_runtime(
                project,
                destination,
                platform_name="darwin",
                run=runner,
            )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            interpreter = destination / "pocket-tts/bin/python"
            self.assertEqual(manifest["backend"], "pocket-tts")
            self.assertTrue(interpreter.is_symlink())
            self.assertFalse(os.readlink(interpreter).startswith("/"))
            flattened = [part for command, _options in calls for part in command]
            self.assertIn("--no-bin", flattened)
            self.assertIn("--no-registry", flattened)
            self.assertIn("--frozen", flattened)
            self.assertEqual(
                sum("vntts-runtime-relocation-" in part for part in flattened),
                1,
            )

    def test_windows_stage_installs_into_the_portable_base_distribution(self):
        with TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            backend = project / "backends" / "pocket-tts"
            backend.mkdir(parents=True)
            (backend / "uv.lock").write_text("locked", encoding="utf-8")
            (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
            destination = Path(directory) / "build" / "speech-runtimes"
            calls = []

            def runner(command, **options):
                calls.append((command, options))
                if command[1:3] == ["python", "install"]:
                    managed_root = Path(command[command.index("--install-dir") + 1])
                    distribution = managed_root / "cpython-3.14-test"
                    (distribution / "Lib/site-packages").mkdir(parents=True)
                    (distribution / "python.exe").write_bytes(b"python")
                    return SimpleNamespace(stdout="")
                if command[1] == "export":
                    requirements = Path(command[command.index("--output-file") + 1])
                    requirements.write_text("locked==1\n", encoding="utf-8")
                    return SimpleNamespace(stdout="")
                if command[1:3] in (["pip", "sync"], ["pip", "install"]):
                    runtime = destination / "pocket-tts"
                    scripts = runtime / "Scripts"
                    scripts.mkdir(exist_ok=True)
                    (scripts / "generated.exe").write_bytes(b"entrypoint")
                    return SimpleNamespace(stdout="")
                interpreter = Path(command[0])
                runtime = interpreter.parent
                site = runtime / "Lib/site-packages"
                report = {
                    "executable": str(interpreter),
                    "prefix": str(runtime),
                    "base_prefix": str(runtime),
                    "modules": {
                        name: str(site / name / "__init__.py")
                        for name in (
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
                    },
                }
                return SimpleNamespace(stdout=json.dumps(report))

            manifest_path = stage_pocket_runtime(
                project,
                destination,
                platform_name="win32",
                run=runner,
            )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            commands = [command for command, _options in calls]
            self.assertTrue((destination / "pocket-tts/python.exe").is_file())
            self.assertFalse((destination / "pocket-tts/Scripts").exists())
            self.assertFalse((destination / "_python").exists())
            self.assertEqual(
                manifest["probe"]["base_prefix"],
                manifest["probe"]["prefix"],
            )
            self.assertTrue(any(command[1] == "export" for command in commands))
            self.assertTrue(
                any(command[1:3] == ["pip", "sync"] for command in commands)
            )
            windows_installs = [
                command
                for command in commands
                if command[1:3] in (["pip", "sync"], ["pip", "install"])
            ]
            self.assertEqual(len(windows_installs), 2)
            self.assertTrue(
                all(
                    "--break-system-packages" in command for command in windows_installs
                )
            )
            self.assertFalse(any(command[1] == "venv" for command in commands))

    def test_runtime_paths_are_platform_specific(self):
        root = Path("runtime")

        self.assertEqual(_runtime_interpreter(root, "win32"), root / "python.exe")
        self.assertEqual(_runtime_interpreter(root, "darwin"), root / "bin/python")

    def test_promotes_managed_windows_distribution_without_a_venv(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            managed_root = root / "_python"
            distribution = managed_root / "cpython-3.14-test"
            interpreter = distribution / "python.exe"
            site = distribution / "Lib/site-packages"
            site.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            (site / "package.py").write_text("value = 1", encoding="utf-8")
            runtime = root / "pocket-tts"

            promoted = _promote_windows_runtime(
                managed_root,
                interpreter,
                runtime,
            )

            self.assertEqual(promoted, runtime / "python.exe")
            self.assertTrue(promoted.is_file())
            self.assertTrue((runtime / "Lib/site-packages/package.py").is_file())
            self.assertFalse(managed_root.exists())

    def test_managed_interpreter_must_be_unique_and_contained(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            interpreter = root / "cpython-3.14-test/bin/python3.14"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            alias = root / "cpython-3.14-alias/bin/python3.14"
            alias.parent.mkdir(parents=True)
            symlink_or_skip(alias, interpreter)

            self.assertEqual(
                _find_managed_interpreter(root, "darwin", "3.14"),
                interpreter.resolve(),
            )

    def test_replaces_posix_interpreter_with_relative_managed_python_link(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "speech-runtimes" / "pocket-tts"
            interpreter = runtime / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"old")
            managed = (
                root / "speech-runtimes" / "_python" / "cpython" / "bin/python3.14"
            )
            managed.parent.mkdir(parents=True)
            managed.write_bytes(b"python")

            _replace_posix_interpreter_link(runtime, managed)

            self.assertTrue(interpreter.is_symlink())
            self.assertFalse(os.readlink(interpreter).startswith("/"))
            self.assertEqual(interpreter.resolve(), managed.resolve())

    def test_prunes_only_managed_python_development_metadata(self):
        with TemporaryDirectory() as directory:
            managed_root = Path(directory) / "_python"
            distribution = managed_root / "cpython-3.14-test"
            interpreter = distribution / "bin/python3.14"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            standard_library = distribution / "lib/python3.14/os.py"
            standard_library.parent.mkdir(parents=True)
            standard_library.write_text("runtime", encoding="utf-8")
            for relative in (
                "include/Python.h",
                "share/man/python.1",
                "lib/pkgconfig/python.pc",
            ):
                path = distribution / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("development", encoding="utf-8")
            alias = managed_root / "cpython-3.14-alias"
            symlink_or_skip(alias, distribution)

            _prune_managed_runtime(managed_root, interpreter)

            self.assertTrue(interpreter.is_file())
            self.assertTrue(standard_library.is_file())
            self.assertFalse(alias.exists())
            self.assertFalse((distribution / "include").exists())
            self.assertFalse((distribution / "share").exists())
            self.assertFalse((distribution / "lib/pkgconfig").exists())

    def test_prunes_unused_entrypoints_and_executable_data_bits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            managed_root = root / "_python"
            managed_interpreter = managed_root / "cpython/bin/python3.14"
            managed_interpreter.parent.mkdir(parents=True)
            managed_interpreter.write_bytes(b"python")
            managed_interpreter.chmod(0o755)
            managed_lock = managed_root / ".lock"
            managed_lock.write_bytes(b"")
            managed_temp = managed_root / ".temp"
            managed_temp.mkdir()
            managed_script = managed_interpreter.parent / "pydoc3"
            managed_script.write_text("script", encoding="utf-8")
            managed_script.chmod(0o755)
            runtime = root / "pocket-tts"
            runtime_lock = runtime / ".lock"
            runtime_lock.parent.mkdir(parents=True)
            runtime_lock.write_bytes(b"")
            interpreter = runtime / "bin/python"
            interpreter.parent.mkdir(parents=True)
            symlink_or_skip(interpreter, managed_interpreter)
            entrypoint = runtime / "bin/vntts"
            entrypoint.write_text("script", encoding="utf-8")
            entrypoint.chmod(0o755)
            executable_data = runtime / "lib/python3.14/example.py"
            executable_data.parent.mkdir(parents=True)
            executable_data.write_text("data", encoding="utf-8")
            executable_data.chmod(0o755)

            _prune_runtime_entrypoints(
                managed_root,
                managed_interpreter,
                runtime,
                "darwin",
            )

            self.assertTrue(interpreter.is_symlink())
            self.assertFalse(entrypoint.exists())
            self.assertFalse(managed_script.exists())
            self.assertFalse(managed_lock.exists())
            self.assertFalse(managed_temp.exists())
            self.assertFalse(runtime_lock.exists())
            if os.name != "nt":
                self.assertTrue(managed_interpreter.stat().st_mode & 0o111)
                self.assertFalse(executable_data.stat().st_mode & 0o111)

    def test_windows_entrypoint_pruning_preserves_distribution_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            managed_root = root / "removed-managed-root"
            runtime = root / "pocket-tts"
            managed_interpreter = runtime / "python.exe"
            managed_interpreter.parent.mkdir(parents=True)
            managed_interpreter.write_bytes(b"python")
            runtime_library = managed_interpreter.parent / "python314.dll"
            runtime_library.write_bytes(b"library")
            entrypoint = runtime / "Scripts/vntts.exe"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_bytes(b"entrypoint")

            _prune_runtime_entrypoints(
                managed_root,
                managed_interpreter,
                runtime,
                "win32",
            )

            self.assertTrue(managed_interpreter.exists())
            self.assertFalse(entrypoint.exists())
            self.assertFalse(entrypoint.parent.exists())
            self.assertTrue(runtime_library.exists())

    def test_runtime_site_rejects_ambiguous_posix_layout(self):
        with TemporaryDirectory() as directory:
            runtime = Path(directory)
            for version in ("python3.14", "python3.15"):
                (runtime / "lib" / version / "site-packages").mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "Expected one site-packages"):
                _runtime_site(runtime, "darwin")

    def test_relocation_probe_rejects_dependency_outside_bundle(self):
        with TemporaryDirectory() as directory:
            speech_runtimes = Path(directory) / "speech-runtimes"
            runtime = speech_runtimes / "pocket-tts"
            interpreter = runtime / "bin/python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            report = {
                "executable": str(interpreter),
                "prefix": str(runtime),
                "base_prefix": str(speech_runtimes / "_python"),
                "modules": {"numpy": "/developer/.venv/numpy/__init__.py"},
            }
            runner = Mock()
            runner.return_value.stdout = json.dumps(report)

            with self.assertRaisesRegex(RuntimeError, "escaped its staging root"):
                _probe_relocated_runtime(
                    speech_runtimes,
                    platform_name="darwin",
                    run=runner,
                )
            self.assertIn("-B", runner.call_args.args[0])

    def test_sha256_is_stable(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "value"
            path.write_bytes(b"abc")

            self.assertEqual(
                _sha256(path),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )


if __name__ == "__main__":
    unittest.main()

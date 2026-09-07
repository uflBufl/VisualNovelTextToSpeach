import os
import subprocess
import sys
import tomllib
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.runtime_installation import (
    _nvidia_driver_status,
    _run,
    ensure_speech_runtime,
    runtime_installation_available,
)
from vntts.runtime_paths import find_managed_speech_runtime, managed_runtime_location
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError
from vntts.speech_worker import (
    _read_frame,
    _write_frame,
    probe_speech_runtime,
    resolve_speech_runtime_paths,
    worker_main,
)


class RuntimeInstallationTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "backends/pocket-tts"
        self.project.mkdir(parents=True)
        (self.project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        (self.project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        for patcher in (
            patch(
                "vntts.runtime_paths.__file__",
                str(self.root / "vntts/runtime_paths.py"),
            ),
            patch("vntts.runtime_paths.get_bundle_root", return_value=None),
            patch("vntts.runtime_installation.get_bundle_root", return_value=None),
            patch(
                "vntts.application_directories.get_local_data_directory",
                return_value=self.root / "data",
            ),
            patch("vntts.runtime_installation.shutil.which", return_value="uv"),
            patch(
                "vntts.runtime_installation._nvidia_driver_status",
                return_value="unknown",
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_environment(self, root):
        interpreter = root / ("python.exe" if sys.platform == "win32" else "bin/python")
        site = root / (
            "Lib/site-packages"
            if sys.platform == "win32"
            else "lib/python3.14/site-packages"
        )
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.touch()
        site.mkdir(parents=True, exist_ok=True)
        return root, interpreter, site

    def install(self, _command, *, environment, **_options):
        self.make_environment(Path(environment["UV_PROJECT_ENVIRONMENT"]))
        return b""

    def test_install_probe_remember_reuse_and_recipe_change(self):
        progress = []
        with (
            patch("vntts.runtime_installation._run", side_effect=self.install) as run,
            patch(
                "vntts.speech_worker.probe_speech_runtime", return_value={"modules": {}}
            ) as probe,
        ):
            paths = ensure_speech_runtime("pocket-tts", progress=progress.append)
            self.assertEqual(resolve_speech_runtime_paths("pocket-tts"), paths)
            self.assertEqual(ensure_speech_runtime("pocket-tts"), paths)
            run.assert_called_once()
            probe.assert_called_once()
            command = run.call_args.args[0]
            self.assertIn("--locked", command)
            self.assertIn("3.14", command)
            self.assertNotIn("VIRTUAL_ENV", run.call_args.kwargs["environment"])
            self.assertIn("Checking", progress[-2])
            self.assertTrue((paths[0].parent / "verified.json").is_file())
            (self.project / "uv.lock").write_text("version = 2\n", encoding="utf-8")
            self.assertIsNone(find_managed_speech_runtime("pocket-tts"))
            updated = ensure_speech_runtime("pocket-tts")
            self.assertNotEqual(paths[0], updated[0])
            self.assertTrue(paths[0].is_dir())

    def test_failed_probe_is_not_discovered_and_retry_can_finish(self):
        with (
            patch("vntts.runtime_installation._run", side_effect=self.install),
            patch(
                "vntts.speech_worker.probe_speech_runtime",
                side_effect=TTSConfigurationError("bad import"),
            ) as probe,
        ):
            with self.assertRaisesRegex(TTSConfigurationError, "bad import"):
                ensure_speech_runtime("pocket-tts")
            self.assertIsNone(find_managed_speech_runtime("pocket-tts"))
            probe.side_effect = None
            probe.return_value = {}
            self.assertEqual(
                ensure_speech_runtime("pocket-tts")[0],
                find_managed_speech_runtime("pocket-tts"),
            )

    def test_cancelled_install_does_not_publish_ready_marker(self):
        cancellation = Event()

        def cancelled(*args, **kwargs):
            self.install(*args, **kwargs)
            cancellation.set()

        with (
            patch("vntts.runtime_installation._run", side_effect=cancelled),
            patch("vntts.speech_worker.probe_speech_runtime", return_value={}),
            self.assertRaisesRegex(TTSSynthesisError, "cancelled"),
        ):
            ensure_speech_runtime("pocket-tts", cancellation=cancellation)
        self.assertIsNone(find_managed_speech_runtime("pocket-tts"))

    def test_recipe_changed_during_sync_is_not_published(self):
        def changed(*args, **kwargs):
            self.install(*args, **kwargs)
            (self.project / "uv.lock").write_text("version = 3\n", encoding="utf-8")

        location = managed_runtime_location("pocket-tts")
        with (
            patch("vntts.runtime_installation._run", side_effect=changed),
            patch("vntts.speech_worker.probe_speech_runtime", return_value={}),
            self.assertRaisesRegex(TTSConfigurationError, "recipe changed"),
        ):
            ensure_speech_runtime("pocket-tts")
        self.assertFalse((location / "verified.json").exists())

    def test_damaged_previously_verified_environment_is_not_overwritten(self):
        location = managed_runtime_location("pocket-tts")
        location.mkdir(parents=True)
        (location / "verified.json").write_text("{}", encoding="utf-8")
        with (
            patch("vntts.runtime_installation._run") as run,
            self.assertRaisesRegex(TTSConfigurationError, "Refusing to modify"),
        ):
            ensure_speech_runtime("pocket-tts")
        run.assert_not_called()

    def test_already_cancelled_preparation_never_launches_a_child(self):
        cancellation = Event()
        cancellation.set()
        with patch("vntts.runtime_installation.subprocess.Popen") as popen:
            with self.assertRaisesRegex(TTSSynthesisError, "cancelled"):
                ensure_speech_runtime("pocket-tts", cancellation=cancellation)
            popen.assert_not_called()

    def test_user_source_explicit_and_packaged_environments_are_not_modified(self):
        with patch("vntts.runtime_installation._run") as run:
            source = self.make_environment(self.project / ".venv")
            self.assertEqual(ensure_speech_runtime("pocket-tts"), source)
            explicit = self.make_environment(self.root / "explicit")
            self.assertEqual(
                ensure_speech_runtime("pocket-tts", runtime_directory=explicit[0]),
                explicit,
            )
            with self.assertRaises(TTSConfigurationError):
                ensure_speech_runtime(
                    "pocket-tts", runtime_directory=self.root / "missing"
                )
            with patch.dict(
                os.environ, {"VNTTS_POCKET_TTS_RUNTIME": str(self.root / "missing")}
            ):
                with self.assertRaises(TTSConfigurationError):
                    ensure_speech_runtime("pocket-tts")
            with (
                patch(
                    "vntts.speech_worker.get_bundle_root",
                    return_value=self.root / "bundle",
                ),
                patch(
                    "vntts.speech_worker.find_bundled_speech_runtime", return_value=None
                ),
                patch(
                    "vntts.runtime_installation.get_bundle_root",
                    return_value=self.root / "bundle",
                ),
                self.assertRaisesRegex(
                    TTSConfigurationError, "complete release package"
                ) as error,
            ):
                ensure_speech_runtime("pocket-tts")
            self.assertNotIn("uv sync", str(error.exception))
            run.assert_not_called()

    def test_qualification_never_promotes_cuda_or_unsupported_hosts(self):
        for host, machine, backend, expected in (
            ("win32", "AMD64", "pocket-tts", True),
            ("linux", "x86_64", "pocket-tts", True),
            ("darwin", "arm64", "pocket-tts", True),
            ("darwin", "x86_64", "pocket-tts", False),
            ("win32", "arm64", "pocket-tts", False),
            ("win32", "AMD64", "moss-tts-delay", False),
            ("win32", "AMD64", "moss-tts", False),
            ("darwin", "arm64", "moss-tts", True),
        ):
            with (
                self.subTest(host=host, machine=machine, backend=backend),
                patch("vntts.runtime_installation.sys.platform", host),
                patch(
                    "vntts.runtime_installation.platform.machine", return_value=machine
                ),
                patch(
                    "vntts.runtime_installation.source_runtime_project",
                    return_value=self.project,
                ),
            ):
                self.assertEqual(runtime_installation_available(backend), expected)

    def test_concurrent_installation_does_not_start_another_sync(self):
        location = managed_runtime_location("pocket-tts")
        with (
            exclusive_advisory_lock(location / "installation.lock"),
            patch("vntts.runtime_installation._run") as run,
            self.assertRaisesRegex(TTSConfigurationError, "Another window"),
        ):
            ensure_speech_runtime("pocket-tts")
        run.assert_not_called()

    def test_subprocess_cancellation_drains_and_terminates_child(self):
        cancellation = Event()
        process = Mock(returncode=None)
        process.poll.return_value = None

        def wait(*_args, **_kwargs):
            cancellation.set()
            raise subprocess.TimeoutExpired("uv", 0.1)

        process.communicate.side_effect = wait
        with (
            patch("vntts.runtime_installation.subprocess.Popen", return_value=process),
            patch("vntts.runtime_installation.terminate_process") as terminate,
            self.assertRaisesRegex(TTSSynthesisError, "cancelled"),
        ):
            _run(["uv"], cancellation=cancellation)
        terminate.assert_called_once_with(process)

    def test_nvidia_discovery_is_bounded_and_never_swallows_cancellation(self):
        with (
            patch("vntts.runtime_installation.sys.platform", "win32"),
            patch("vntts.runtime_installation._run", return_value=b"580.88\n") as run,
        ):
            self.assertEqual(_nvidia_driver_status(None), "detected")
            self.assertEqual(run.call_args.kwargs["timeout"], 5)
            run.side_effect = TTSConfigurationError("driver unavailable")
            self.assertEqual(_nvidia_driver_status(None), "unknown")
            run.side_effect = TTSSynthesisError("cancelled")
            with self.assertRaises(TTSSynthesisError):
                _nvidia_driver_status(None)
            with patch("vntts.runtime_installation.shutil.which", return_value=None):
                self.assertEqual(_nvidia_driver_status(None), "unknown")

    def test_worker_runtime_probe_never_constructs_model(self):
        source, output = BytesIO(), BytesIO()
        _write_frame(
            source,
            {
                "type": "runtime_probe",
                "backend": "pocket-tts",
                "runtime_site": str(self.root),
            },
        )
        source.seek(0)
        factory = Mock(side_effect=AssertionError("model must not load"))
        with patch(
            "vntts.speech_worker._module_health",
            return_value={"torch": {"version": "test"}},
        ):
            code = worker_main(
                input_stream=source,
                output_stream=output,
                backend_classes={"pocket-tts": factory},
            )
        self.assertEqual(code, 0)
        output.seek(0)
        self.assertEqual(_read_frame(output)[0]["type"], "runtime_health")
        factory.assert_not_called()

    def test_probe_rejects_wrong_environment(self):
        paths = self.make_environment(self.root / "environment")
        output = BytesIO()
        _write_frame(
            output,
            {
                "type": "runtime_health",
                "backend": "pocket-tts",
                "prefix": str(self.root),
            },
        )
        with (
            patch("vntts.runtime_installation._run", return_value=output.getvalue()),
            self.assertRaisesRegex(TTSConfigurationError, "verification failed"),
        ):
            probe_speech_runtime("pocket-tts", paths)

    def test_pocket_lock_is_cpu_only(self):
        project = Path(__file__).resolve().parents[1] / "backends/pocket-tts"
        lock = tomllib.loads((project / "uv.lock").read_text(encoding="utf-8"))
        torch = [package for package in lock["package"] if package["name"] == "torch"]
        self.assertTrue(torch)
        self.assertTrue(
            all(
                package["source"]["registry"] == "https://download.pytorch.org/whl/cpu"
                for package in torch
            )
        )
        self.assertFalse(
            any(
                package["name"].startswith(("nvidia-", "cuda-"))
                for package in lock["package"]
            )
        )

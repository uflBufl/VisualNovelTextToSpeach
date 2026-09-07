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
from vntts.runtime_ownership import claim_runtime, cleanup_managed_runtimes
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

    def prepared_runtime(self):
        with (
            patch("vntts.runtime_installation._run", side_effect=self.install),
            patch("vntts.speech_worker.probe_speech_runtime", return_value={}),
        ):
            return ensure_speech_runtime("pocket-tts")

    def next_recipe(self):
        with (self.project / "uv.lock").open("a", encoding="utf-8") as stream:
            stream.write("# another recipe\n")

    def test_active_runtime_and_unconfirmed_child_are_preserved_until_shutdown(self):
        old = self.prepared_runtime()
        use = claim_runtime("pocket-tts", old[0])
        self.assertIsNotNone(use)
        child = Mock(pid=999999, poll=Mock(return_value=None))
        use.begin_launch()
        use.launched(child)
        self.next_recipe()
        new = self.prepared_runtime()
        self.assertTrue(old[0].is_dir())
        use.close()
        self.assertTrue(use.path.exists())
        cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertTrue(old[0].is_dir())
        child.poll.return_value = 0
        use.close()
        messages = []
        cleanup_managed_runtimes("pocket-tts", new[0], progress=messages.append)
        self.assertFalse(old[0].exists())
        self.assertIn("Removed 1", messages[-1])
        self.assertTrue((old[0].parents[2] / "installation.lock").is_file())

    def test_orphan_child_and_uncertain_launch_block_cleanup(self):
        old = self.prepared_runtime()
        use = claim_runtime("pocket-tts", old[0])
        use.begin_launch()
        self.next_recipe()
        new = self.prepared_runtime()
        with patch(
            "vntts.runtime_ownership.inspect_process_status", return_value="dead"
        ):
            cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertTrue(old[0].exists())
        child = Mock(pid=987654, poll=Mock(return_value=0))
        use.launched(child)
        with patch(
            "vntts.runtime_ownership.inspect_process_status",
            side_effect=lambda pid: "live" if pid == child.pid else "dead",
        ):
            cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertTrue(old[0].exists())
        with patch(
            "vntts.runtime_ownership.inspect_process_status", return_value="dead"
        ):
            cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertFalse(old[0].exists())

    def test_failed_repair_keeps_previous_selection_and_removes_incomplete_copy(self):
        old = self.prepared_runtime()
        selected = old[0].parents[2] / "verified.json"
        original = selected.read_bytes()
        with (
            patch("vntts.runtime_installation._run", side_effect=self.install),
            patch(
                "vntts.speech_worker.probe_speech_runtime",
                side_effect=TTSConfigurationError("dependency import failed"),
            ),
            self.assertRaisesRegex(TTSConfigurationError, "dependency import failed"),
        ):
            ensure_speech_runtime("pocket-tts")
        self.assertEqual(selected.read_bytes(), original)
        self.assertEqual(list(old[0].parents[1].iterdir()), [old[0].parent])
        self.assertTrue(old[0].is_dir())

    def test_broken_dependencies_repair_beside_an_active_copy(self):
        old = self.prepared_runtime()
        use = claim_runtime("pocket-tts", old[0])
        self.addCleanup(use.close)

        def probe(_backend, paths, **_options):
            if paths == old:
                raise TTSConfigurationError("broken torch import")
            return {}

        with (
            patch("vntts.runtime_installation._run", side_effect=self.install),
            patch("vntts.speech_worker.probe_speech_runtime", side_effect=probe),
        ):
            new = ensure_speech_runtime("pocket-tts")
        self.assertNotEqual(new[0], old[0])
        self.assertEqual(find_managed_speech_runtime("pocket-tts"), new[0])
        self.assertTrue(old[0].is_dir())
        use.close()
        cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertFalse(old[0].exists())

    def test_cancel_or_publication_failure_preserves_previous_selection(self):
        from vntts_artifacts.atomic_io import atomic_write_json

        old = self.prepared_runtime()
        selected = old[0].parents[2] / "verified.json"
        original = selected.read_bytes()
        for failure in ("cancel", "publish"):
            with self.subTest(failure=failure):
                cancellation = Event()

                def probe(_backend, paths, **_options):
                    if paths == old:
                        raise TTSConfigurationError("broken import")
                    if failure == "cancel":
                        cancellation.set()
                    return {}

                def publish(path, document):
                    if path == selected:
                        raise PermissionError("cannot publish")
                    atomic_write_json(path, document)

                with (
                    patch("vntts.runtime_installation._run", side_effect=self.install),
                    patch(
                        "vntts.speech_worker.probe_speech_runtime", side_effect=probe
                    ),
                    patch(
                        "vntts.runtime_installation.atomic_write_json",
                        side_effect=publish,
                    ),
                    self.assertRaises((TTSSynthesisError, PermissionError)),
                ):
                    ensure_speech_runtime("pocket-tts", cancellation=cancellation)
                self.assertEqual(selected.read_bytes(), original)
                self.assertEqual(list(old[0].parents[1].iterdir()), [old[0].parent])

    def test_worker_claim_is_released_after_shutdown_or_startup_failure(self):
        from tests.test_speech_worker import FakeProcess
        from vntts.speech_worker import IsolatedSpeechBackend
        from vntts.voices import CharacterVoiceRegistry

        paths = self.prepared_runtime()
        for fail in (False, True):
            with self.subTest(fail=fail):
                process = FakeProcess(
                    {"type": "error", "message": "model unavailable"}
                    if fail
                    else {
                        "type": "health",
                        "backend": "pocket-tts",
                        "interpreter": str(paths[1]),
                        "prefix": str(paths[0]),
                        "runtime_site": str(paths[2]),
                        "sample_rate": 24000,
                        "modules": {},
                    }
                )
                process.pid = os.getpid()
                with patch(
                    "vntts.runtime_installation.ensure_speech_runtime",
                    return_value=paths,
                ):
                    if fail:
                        with self.assertRaises(TTSConfigurationError):
                            IsolatedSpeechBackend(
                                "pocket-tts",
                                CharacterVoiceRegistry(),
                                process_factory=lambda *_a, **_k: process,
                            )
                    else:
                        backend = IsolatedSpeechBackend(
                            "pocket-tts",
                            CharacterVoiceRegistry(),
                            process_factory=lambda *_a, **_k: process,
                        )
                        self.assertTrue(list((paths[0].parent / "users").iterdir()))
                        backend.shutdown()
                self.assertIsNotNone(process.poll())
                self.assertEqual(list((paths[0].parent / "users").iterdir()), [])

    def test_missing_interpreter_is_repaired(self):
        old = self.prepared_runtime()
        old[1].unlink()
        new = self.prepared_runtime()
        self.assertTrue(new[1].is_file())
        self.assertNotEqual(new[0], old[0])
        self.assertFalse(old[0].exists())

    def test_cleanup_failures_do_not_reject_a_good_runtime(self):
        old = self.prepared_runtime()
        self.next_recipe()
        messages = []
        with (
            patch("vntts.runtime_installation._run", side_effect=self.install),
            patch("vntts.speech_worker.probe_speech_runtime", return_value={}),
            patch(
                "vntts.runtime_ownership.shutil.rmtree",
                side_effect=PermissionError("in use"),
            ),
        ):
            new = ensure_speech_runtime("pocket-tts", progress=messages.append)
        self.assertTrue(new[0].exists())
        self.assertTrue(old[0].exists())
        self.assertTrue(any("cleanup was deferred" in message for message in messages))

    def test_cleanup_skips_unknown_ownership_and_junctions(self):
        old = self.prepared_runtime()
        use = claim_runtime("pocket-tts", old[0])
        self.next_recipe()
        new = self.prepared_runtime()
        use.close()
        with patch.object(Path, "is_junction", lambda path: path == old[0]):
            cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertTrue(old[0].is_dir())
        with patch.object(Path, "is_junction", lambda path: path == use.path.parent):
            with self.assertRaisesRegex(TTSConfigurationError, "usage directory"):
                claim_runtime("pocket-tts", old[0])
        use.path.write_text("{broken", encoding="utf-8")
        cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertTrue(old[0].is_dir())
        use.path.unlink()
        (old[0].parent / "owner.json").write_text("{}", encoding="utf-8")
        cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertTrue(old[0].is_dir())

    def test_cleanup_skips_recipe_under_installation(self):
        old = self.prepared_runtime()
        use = claim_runtime("pocket-tts", old[0])
        self.next_recipe()
        new = self.prepared_runtime()
        use.close()
        with exclusive_advisory_lock(old[0].parents[2] / "installation.lock"):
            cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertTrue(old[0].is_dir())
        cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertFalse(old[0].exists())

    def test_real_other_process_claim_protects_runtime(self):
        old = self.prepared_runtime()
        script = (
            "import sys; from pathlib import Path; "
            "from vntts import application_directories; "
            "application_directories.get_local_data_directory=lambda:Path(sys.argv[1]); "
            "from vntts.runtime_ownership import claim_runtime; "
            "use=claim_runtime('pocket-tts',Path(sys.argv[2])); "
            "assert use is not None; print('ready',flush=True); "
            "sys.stdin.readline(); use.close()"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root / "data"), str(old[0])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            from threading import Thread

            lines = []
            ready = Event()

            def read_ready():
                lines.append(child.stdout.readline())
                ready.set()

            Thread(target=read_ready, daemon=True).start()
            self.assertTrue(ready.wait(15), "runtime claimant did not start")
            self.assertEqual(lines, ["ready\n"])
            self.next_recipe()
            new = self.prepared_runtime()
            self.assertTrue(old[0].is_dir())
            _out, error = child.communicate("stop\n", timeout=10)
            self.assertEqual(child.returncode, 0, error)
            cleanup_managed_runtimes("pocket-tts", new[0])
            self.assertFalse(old[0].exists())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()

    def test_direct_import_keeps_runtime_claim_for_process_lifetime(self):
        from vntts.speech_backend_runtime import activate_backend_runtime

        old = self.prepared_runtime()
        with (
            patch("vntts.speech_backend_runtime._managed_runtime_uses", {}) as uses,
            patch("sys.path", list(sys.path)),
        ):
            activate_backend_runtime(
                old[0],
                environment_variable="TEST_RUNTIME",
                backend_directory="pocket-tts",
                missing_message="missing",
            )
            self.next_recipe()
            new = self.prepared_runtime()
            self.assertTrue(old[0].exists())
            self.assertEqual(len(uses), 1)
            for use in uses.values():
                use.close()
        cleanup_managed_runtimes("pocket-tts", new[0])
        self.assertFalse(old[0].exists())

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
            self.assertEqual(probe.call_count, 2)
            command = run.call_args.args[0]
            self.assertIn("--locked", command)
            self.assertIn("3.14", command)
            self.assertNotIn("VIRTUAL_ENV", run.call_args.kwargs["environment"])
            self.assertTrue(any("Checking" in message for message in progress))
            self.assertTrue((paths[0].parents[2] / "verified.json").is_file())
            (self.project / "uv.lock").write_text("version = 2\n", encoding="utf-8")
            self.assertIsNone(find_managed_speech_runtime("pocket-tts"))
            updated = ensure_speech_runtime("pocket-tts")
            self.assertNotEqual(paths[0], updated[0])
            self.assertFalse(paths[0].is_dir())

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

    def test_legacy_environment_is_preserved_while_replacement_is_published(self):
        location = managed_runtime_location("pocket-tts")
        location.mkdir(parents=True)
        legacy = self.make_environment(location / "environment")
        (location / "verified.json").write_text("{}", encoding="utf-8")
        with (
            patch("vntts.runtime_installation._run", side_effect=self.install),
            patch("vntts.speech_worker.probe_speech_runtime", return_value={}),
        ):
            replacement = ensure_speech_runtime("pocket-tts")
        self.assertNotEqual(replacement, legacy)
        self.assertTrue(legacy[0].is_dir())

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

    def test_subprocess_stderr_is_only_included_when_requested(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        process.communicate.return_value = (b"stdout\n", b"stderr\n")
        with patch("vntts.runtime_installation.subprocess.Popen", return_value=process):
            self.assertEqual(_run(["tool"], cancellation=None), b"stdout\n")
            self.assertEqual(
                _run(["tool"], cancellation=None, include_stderr=True),
                b"stdout\nstderr\n",
            )

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

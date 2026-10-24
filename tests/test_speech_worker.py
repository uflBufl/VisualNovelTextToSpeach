import json
import os
import queue
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from tests.speech_worker_fixtures import FakeProcess
from tests.symlink_support import symlink_or_skip
from vntts.model_assets import ModelAssetManager
from vntts.speech_backend import TTSConfigurationError, TTSSynthesisError
from vntts.speech_worker import (
    _REQUIRED_MODULES,
    IsolatedSpeechBackend,
    RetainedWorkerRuntime,
    _is_worker_backend,
    _is_worker_process,
    _module_health,
    _read_frame,
    _registry_from_document,
    _result_document,
    _result_document_value,
    _runtime_paths,
    _serialize_registry,
    _write_frame,
    create_moss_worker_backend,
    resolve_speech_runtime_paths,
    worker_main,
)
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class FakeWorkerBackend:
    sample_rate = 24_000
    device = "cpu"

    def __init__(self, registry, *, narrator_reference=None):
        self.registry = registry
        self.narrator_reference = narrator_reference

    def prime(self, voice):
        return voice == "Narrator"

    def set_live_mode_active(self, active):
        return bool(active)

    def render(self, request):
        def produce():
            pcm = np.array([[0.25], [-0.25]], dtype=np.float32)
            yield SynthesisChunk(pcm, self.sample_rate, 0, 12.5)
            return SynthesisResult(
                pcm=pcm,
                sample_rate=self.sample_rate,
                completion=SynthesisCompletion.COMPLETE,
                limits=SynthesisLimits(None, None),
                timing=SynthesisTiming(12.5, 20.0),
                diagnostics=SynthesisDiagnostics(
                    "fake",
                    "fresh-generation",
                    request.generation_profile,
                    request.seed,
                    1,
                    len(pcm),
                ),
            )

        return SynthesisChunkStream(produce())


class BackendFactoryTests(unittest.TestCase):
    def test_invalid_retained_options_preserve_worker_and_configuration(self):
        registry = CharacterVoiceRegistry()
        cancellation = Event()
        progress = MagicMock()
        worker = MagicMock(
            registry=registry,
            narrator_reference="alba",
            startup_cancellation=cancellation,
            startup_progress=progress,
        )
        worker.process.poll.return_value = None
        factory = MagicMock(return_value=worker)
        runtime = RetainedWorkerRuntime("pocket-tts", backend_factory=factory)
        runtime(
            registry,
            narrator_reference="alba",
            startup_cancellation=cancellation,
            startup_progress=progress,
        )
        identity = runtime._identity
        for options in (
            {"narrator_reference": "bella", "volume": False},
            {"narrator_reference": 42},
            {"narrator_reference": "bella", "startup_cancellation": 42},
            {"startup_cancellation": Event(), "startup_progress": 42},
        ):
            with self.subTest(options=options):
                with self.assertRaises(TTSConfigurationError):
                    runtime(CharacterVoiceRegistry(), **options)
                self.assertIs(runtime._instance, worker)
                self.assertEqual(runtime._identity, identity)
                self.assertIs(worker.registry, registry)
                self.assertEqual(worker.narrator_reference, "alba")
                self.assertIs(worker.startup_cancellation, cancellation)
                self.assertIs(worker.startup_progress, progress)
                worker.shutdown.assert_not_called()
                worker.set_volume.assert_not_called()
                factory.assert_called_once()

    def test_retained_reuse_preserves_omitted_narrator_and_raw_volume_setter(self):
        registry = CharacterVoiceRegistry()
        worker = MagicMock(narrator_reference="alba")
        worker.process.poll.return_value = None
        runtime = RetainedWorkerRuntime(
            "pocket-tts", backend_factory=MagicMock(return_value=worker)
        )
        runtime(registry)
        runtime(registry, volume=1)
        self.assertEqual(worker.narrator_reference, "alba")
        worker.set_volume.assert_called_once_with(1)
        self.assertIs(type(worker.set_volume.call_args.args[0]), int)

    def test_retained_worker_reuses_only_unchanged_live_process(self):
        registry = CharacterVoiceRegistry()
        first = MagicMock(narrator_reference="alba")
        first.process.poll.return_value = None
        second = MagicMock(narrator_reference="bella")
        second.process.poll.return_value = None
        factory = MagicMock(side_effect=(first, second))
        runtime = RetainedWorkerRuntime(
            "pocket-tts",
            backend_factory=factory,
        )

        runtime(registry, narrator_reference="alba", volume=0.5).shutdown()
        runtime(registry, narrator_reference="alba", volume=0.8).shutdown()

        factory.assert_called_once()
        first.set_volume.assert_called_once_with(0.8)

        runtime(registry, narrator_reference="bella", volume=0.8).shutdown()
        self.assertEqual(factory.call_count, 2)
        first.shutdown.assert_called_once_with()

        runtime.shutdown()
        second.shutdown.assert_called_once_with()

    @patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True)
    @patch("vntts.moss_cpp_backend.MossCppVoiceRouterBackend")
    def test_native_moss_drops_pocket_only_permission(self, native, _requested):
        registry = CharacterVoiceRegistry()

        create_moss_worker_backend(
            registry,
            model_name="model.gguf",
            allow_gated_model_access=True,
        )

        native.assert_called_once_with(registry, model_name="model.gguf")


class DeferredWorkerStartupTests(unittest.TestCase):
    def backend(self, **options):
        backend = IsolatedSpeechBackend(
            "pocket-tts", CharacterVoiceRegistry(), defer_startup=True, **options
        )
        self.addCleanup(backend.shutdown)
        return backend

    @staticmethod
    def ready(backend):
        backend.process = FakeProcess(None)
        backend.health = {"type": "health"}

    def test_configuration_and_prepared_request_do_not_install_or_launch(self):
        with patch("vntts.runtime_installation.ensure_speech_runtime") as install:
            process_factory = MagicMock()
            backend = self.backend(process_factory=process_factory)
            prepared = backend.prepare_playback("Bob", "Prepared line")
        self.assertIsNone(backend.process)
        self.assertIsNotNone(backend.capabilities)
        self.assertIsInstance(backend.registry, CharacterVoiceRegistry)
        self.assertEqual(prepared.payload.voice, "Bob")
        install.assert_not_called()
        process_factory.assert_not_called()

    def test_background_and_concurrent_demand_share_one_blocked_startup(self):
        backend = self.backend()
        entered, release = Event(), Event()

        def start(**_options):
            entered.set()
            self.assertTrue(release.wait(3))
            self.ready(backend)

        with (
            patch.object(backend, "_prepare_runtime"),
            patch.object(backend, "_start_worker", side_effect=start) as launch,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            future = backend.load_in_background()
            try:
                self.assertTrue(entered.wait(3))
                demands = [executor.submit(backend._ensure_worker) for _ in range(2)]
                self.assertIs(backend.load_in_background(), future)
                self.assertTrue(all(not demand.done() for demand in demands))
            finally:
                release.set()
            future.result(3)
            for demand in demands:
                self.assertIs(demand.result(3), backend.process)
            launch.assert_called_once()

    def test_timeout_wait_observes_same_failure_then_explicit_demand_retries(self):
        backend = self.backend()
        entered, release = Event(), Event()

        def fail(**_options):
            entered.set()
            self.assertTrue(release.wait(3))
            raise TTSConfigurationError("model is unavailable")

        with (
            patch.object(backend, "_prepare_runtime"),
            patch.object(backend, "_start_worker", side_effect=fail),
        ):
            future = backend.load_in_background()
            self.assertTrue(entered.wait(3))
            original_result = future.result
            first = True

            def wait_then_fail(timeout=None):
                nonlocal first
                if first:
                    first = False
                    release.set()
                    self.assertTrue(backend._startup.finished.wait(3))
                    raise FutureTimeout()
                return original_result(timeout)

            with (
                patch.object(future, "result", side_effect=wait_then_fail),
                patch.object(
                    backend, "_begin_startup", wraps=backend._begin_startup
                ) as begin,
            ):
                with self.assertRaisesRegex(TTSConfigurationError, "unavailable"):
                    backend._ensure_worker()
                begin.assert_called_once()
        with (
            patch.object(backend, "_prepare_runtime"),
            patch.object(
                backend, "_start_worker", side_effect=lambda **_: self.ready(backend)
            ) as retry,
        ):
            backend.load_in_background().result(3)
            retry.assert_called_once()

    def test_health_is_not_ready_until_initial_mode_is_flushed(self):
        backend = self.backend()
        entered, release = Event(), Event()
        backend.runtime_root = Path("/synthetic-runtime")

        def exchange(_process, command, **options):
            self.assertEqual((command, options["active"]), ("set-live-mode", True))
            entered.set()
            self.assertTrue(release.wait(3))
            return True

        with (
            patch.object(backend, "_prepare_runtime"),
            patch("vntts.runtime_ownership.claim_runtime", return_value=None),
            patch.object(
                backend, "_launch_worker", side_effect=lambda **_: self.ready(backend)
            ),
            patch.object(backend, "_exchange_value", side_effect=exchange),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            backend.set_live_mode_active(True)
            try:
                self.assertTrue(entered.wait(3))
                self.assertIsNotNone(backend.health)
                demand = executor.submit(backend._ensure_worker)
                self.assertFalse(backend._startup.future.done())
                self.assertFalse(demand.done())
            finally:
                release.set()
            self.assertIs(demand.result(3), backend.process)

    def test_inactive_mode_is_flushed_before_prime_and_render(self):
        for work in ("prime", "render"):
            with self.subTest(work=work):
                backend = self.backend()
                self.ready(backend)
                backend._live_mode_active = backend._worker_live_mode = True
                backend.set_live_mode_active(False)
                commands = []

                def exchange(_process, command, **options):
                    commands.append((command, options.get("active")))
                    return True

                def send(_process, document):
                    commands.append((document["type"], None))
                    raise RuntimeError("render dispatched")

                with (
                    patch.object(backend, "_exchange_value", side_effect=exchange),
                    patch.object(backend, "_send", side_effect=send),
                ):
                    if work == "prime":
                        backend.prime("Narrator")
                    else:
                        with self.assertRaisesRegex(RuntimeError, "render dispatched"):
                            backend.render(
                                SynthesisRequest("Narrator", "Line")
                            ).collect()
                self.assertEqual(commands, [("set-live-mode", False), (work, None)])

    def test_stop_cancels_old_warmup_even_after_ready_and_resume(self):
        entered, release = Event(), Event()

        def progress(message):
            if "is ready" in message:
                entered.set()
                self.assertTrue(release.wait(3))

        backend = self.backend(startup_progress=progress)
        with (
            patch.object(backend, "_prepare_runtime"),
            patch.object(
                backend, "_start_worker", side_effect=lambda **_: self.ready(backend)
            ),
            patch.object(backend, "prime") as prime,
        ):
            future = backend.load_in_background(warm_up=True)
            future.result(3)
            try:
                self.assertTrue(entered.wait(3))
                startup = backend._startup
                self.assertTrue(backend.stop())
                backend._stop_requested.clear()
                backend.set_live_mode_active(True)
            finally:
                release.set()
            self.assertTrue(startup.finished.wait(3))
            self.assertTrue(startup.cancelled.is_set())
            prime.assert_not_called()

    def test_shutdown_during_runtime_preparation_prevents_late_launch(self):
        backend = self.backend()
        entered, release = Event(), Event()

        def prepare(_cancellation):
            entered.set()
            self.assertTrue(release.wait(3))

        with (
            patch.object(backend, "_prepare_runtime", side_effect=prepare),
            patch.object(backend, "_start_worker") as launch,
        ):
            future = backend.load_in_background()
            try:
                self.assertTrue(entered.wait(3))
                backend.shutdown()
            finally:
                release.set()
            with self.assertRaisesRegex(TTSSynthesisError, "cancelled"):
                future.result(3)
            launch.assert_not_called()

    def test_process_arriving_after_shutdown_is_terminated(self):
        backend = self.backend()
        backend.runtime_root = Path("/synthetic-runtime")
        entered, release = Event(), Event()
        process = FakeProcess(None)

        def launch(**_options):
            entered.set()
            self.assertTrue(release.wait(3))
            backend.process = process
            backend.health = {"type": "health"}

        with (
            patch.object(backend, "_prepare_runtime"),
            patch("vntts.runtime_ownership.claim_runtime", return_value=None),
            patch.object(backend, "_launch_worker", side_effect=launch),
        ):
            future = backend.load_in_background()
            try:
                self.assertTrue(entered.wait(3))
                backend.shutdown()
            finally:
                release.set()
            with self.assertRaisesRegex(TTSSynthesisError, "cancelled"):
                future.result(3)
            self.assertIsNone(backend.process)
            self.assertEqual(process.returncode, -15)

    def test_retained_runtime_reuses_configured_and_loading_worker(self):
        backend = self.backend()
        factory = MagicMock(return_value=backend)
        runtime = RetainedWorkerRuntime("pocket-tts", backend_factory=factory)
        self.addCleanup(runtime.shutdown)
        registry = backend.registry
        runtime(registry, defer_startup=True)
        runtime(registry, defer_startup=True)
        entered, release = Event(), Event()

        def prepare(_cancellation):
            entered.set()
            self.assertTrue(release.wait(3))

        with (
            patch.object(backend, "_prepare_runtime", side_effect=prepare),
            patch.object(
                backend, "_start_worker", side_effect=lambda **_: self.ready(backend)
            ),
        ):
            future = backend.load_in_background()
            try:
                self.assertTrue(entered.wait(3))
                runtime(registry, defer_startup=True)
                factory.assert_called_once()
            finally:
                release.set()
            future.result(3)

    def test_startup_timeout_error_is_propagated_and_explicit_demand_retries(self):
        backend = self.backend()
        failure = TimeoutError("runtime installation timed out")
        with (
            patch.object(backend, "_prepare_runtime"),
            patch.object(backend, "_start_worker", side_effect=failure) as launch,
        ):
            with self.assertRaises(TimeoutError) as raised:
                backend._ensure_worker()
            self.assertIs(raised.exception, failure)
            launch.assert_called_once()
        with (
            patch.object(backend, "_prepare_runtime"),
            patch.object(
                backend, "_start_worker", side_effect=lambda **_: self.ready(backend)
            ) as retry,
        ):
            self.assertIs(backend._ensure_worker(), backend.process)
            retry.assert_called_once()

    def test_thread_start_failure_finishes_owner_and_allows_explicit_retry(self):
        backend = self.backend()
        failure = RuntimeError("cannot start worker thread")
        with patch("vntts.speech_worker.threading.Thread.start", side_effect=failure):
            future = backend.load_in_background()
        with self.assertRaises(RuntimeError) as raised:
            future.result(0)
        self.assertIs(raised.exception, failure)
        self.assertTrue(backend._startup.finished.is_set())
        self.assertFalse(backend.startup_pending)
        with (
            patch.object(backend, "_prepare_runtime"),
            patch.object(
                backend, "_start_worker", side_effect=lambda **_: self.ready(backend)
            ) as launch,
        ):
            backend.load_in_background().result(3)
            launch.assert_called_once()

    def test_queued_successor_thread_failure_finishes_owner_and_allows_retry(self):
        backend = self.backend()
        entered, release = Event(), Event()
        failure = RuntimeError("cannot start successor thread")

        def prepare(_cancellation):
            entered.set()
            self.assertTrue(release.wait(3))

        with (
            patch.object(backend, "_prepare_runtime", side_effect=prepare),
            patch.object(backend, "_start_worker") as launch,
        ):
            previous = backend.load_in_background()
            try:
                self.assertTrue(entered.wait(3))
                backend.cancel_startup()
                successor = backend.load_in_background()
                owner = backend._startup
                with patch(
                    "vntts.speech_worker.threading.Thread.start", side_effect=failure
                ):
                    release.set()
                    with self.assertRaises(RuntimeError) as raised:
                        successor.result(3)
                    self.assertIs(raised.exception, failure)
                    self.assertTrue(owner.finished.wait(3))
            finally:
                release.set()
            with self.assertRaisesRegex(TTSSynthesisError, "cancelled"):
                previous.result(3)
            self.assertFalse(backend.startup_pending)
            launch.assert_not_called()
        with (
            patch.object(backend, "_prepare_runtime"),
            patch.object(
                backend, "_start_worker", side_effect=lambda **_: self.ready(backend)
            ) as retry,
        ):
            backend.load_in_background().result(3)
            retry.assert_called_once()

    def test_new_intent_waits_for_cancelled_flight_before_retrying(self):
        backend = self.backend()
        entered, release = Event(), Event()
        attempts = []

        def prepare(cancellation):
            attempts.append(cancellation)
            if len(attempts) == 1:
                entered.set()
                self.assertTrue(release.wait(3))

        with (
            patch.object(backend, "_prepare_runtime", side_effect=prepare),
            patch.object(
                backend, "_start_worker", side_effect=lambda **_: self.ready(backend)
            ) as launch,
        ):
            previous = backend.load_in_background()
            try:
                self.assertTrue(entered.wait(3))
                backend.cancel_startup()
                successor = backend.load_in_background()
                self.assertIsNot(previous, successor)
                self.assertEqual(len(attempts), 1)
                self.assertFalse(successor.done())
            finally:
                release.set()
            with self.assertRaisesRegex(TTSSynthesisError, "cancelled"):
                previous.result(3)
            successor.result(3)
            self.assertEqual(len(attempts), 2)
            self.assertTrue(attempts[0]())
            self.assertFalse(attempts[1]())
            launch.assert_called_once()

    def test_retained_runtime_retires_cancelled_pending_owner(self):
        first, replacement = self.backend(), self.backend()
        factory = MagicMock(side_effect=(first, replacement))
        runtime = RetainedWorkerRuntime("pocket-tts", backend_factory=factory)
        self.addCleanup(runtime.shutdown)
        registry = first.registry
        runtime(registry, defer_startup=True)
        entered, release = Event(), Event()

        def prepare(_cancellation):
            entered.set()
            self.assertTrue(release.wait(3))

        with (
            patch.object(first, "_prepare_runtime", side_effect=prepare),
            patch.object(first, "_start_worker") as launch,
        ):
            future = first.load_in_background()
            try:
                self.assertTrue(entered.wait(3))
                first.cancel_startup()
                runtime(registry, defer_startup=True)
                self.assertIs(runtime._instance, replacement)
                self.assertTrue(first._closed)
            finally:
                release.set()
            with self.assertRaisesRegex(TTSSynthesisError, "cancelled"):
                future.result(3)
            launch.assert_not_called()

    def test_deferred_qwen_factory_does_not_install_runtime_in_caller(self):
        from vntts.speech_worker import create_qwen_worker_backend

        with (
            patch("vntts.runtime_installation.ensure_speech_runtime") as install,
            patch("vntts.speech_worker._isolated_backend_constructor") as constructor,
        ):
            create_qwen_worker_backend(CharacterVoiceRegistry(), defer_startup=True)
        install.assert_not_called()
        self.assertTrue(create_qwen_worker_backend.supports_deferred_startup)
        self.assertTrue(constructor.call_args.kwargs["defer_startup"])


class SpeechWorkerTest(unittest.TestCase):
    def test_installed_only_startup_never_installs_missing_runtime(self):
        with (
            patch(
                "vntts.speech_worker.resolve_speech_runtime_paths",
                side_effect=TTSConfigurationError("not installed"),
            ),
            patch("vntts.runtime_installation.ensure_speech_runtime") as install,
            self.assertRaisesRegex(TTSConfigurationError, "not installed"),
        ):
            IsolatedSpeechBackend(
                "moss-tts", CharacterVoiceRegistry(), allow_download=False
            )
        install.assert_not_called()

    def test_installed_only_worker_uses_offline_environment_and_existing_runtime(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = root, root / "bin/python", root / "lib/site-packages"
            captured = {}

            def process_factory(_command, **options):
                captured.update(options)
                return FakeProcess(
                    {
                        "type": "health",
                        "backend": "moss-tts",
                        "interpreter": str(paths[1]),
                        "prefix": str(root),
                        "runtime_site": str(paths[2]),
                        "sample_rate": 24000,
                        "modules": {},
                    }
                )

            with patch("vntts.speech_worker._runtime_paths", return_value=paths):
                backend = IsolatedSpeechBackend(
                    "moss-tts",
                    CharacterVoiceRegistry(),
                    process_factory=process_factory,
                    allow_download=False,
                )
            self.addCleanup(backend.shutdown)
            self.assertEqual(captured["env"]["HF_HUB_OFFLINE"], "1")
            self.assertEqual(captured["env"]["TRANSFORMERS_OFFLINE"], "1")
            self.assertNotIn("allow_download", backend.worker_options)

    def test_worker_import_in_isolated_process_does_not_load_gui_or_models(self):
        source = """
import importlib.abc
import sys

sys.path.insert(0, sys.argv[1])
forbidden = {
    "vntts.model_assets", "vntts.settings", "pynput", "PySide6",
    "torch", "mlx", "mlx_audio", "sounddevice",
}

class RejectHostDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in forbidden):
            raise ImportError("Forbidden worker import: " + fullname)
        return None

sys.meta_path.insert(0, RejectHostDependencies())
from vntts.speech_worker import probe_speech_runtime, worker_main
assert callable(probe_speech_runtime) and callable(worker_main)
print("worker-import-ready")
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                source,
                str(Path(__file__).resolve().parents[1]),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "worker-import-ready")

    def test_parent_worker_launch_preserves_explicit_huggingface_cache_overrides(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = root, root / "bin/python", root / "lib/site-packages"
            overrides = {
                "HF_HOME": str(root / "explicit-home"),
                "HF_HUB_CACHE": str(root / "explicit-hub"),
                "HUGGINGFACE_HUB_CACHE": str(root / "legacy-hub"),
                "TRANSFORMERS_CACHE": str(root / "transformers"),
            }
            captured, statuses = {}, []

            def process_factory(_command, **options):
                captured.update(options["env"])
                return FakeProcess(
                    {
                        "type": "health",
                        "backend": "moss-tts",
                        "interpreter": str(paths[1]),
                        "prefix": str(root),
                        "runtime_site": str(paths[2]),
                        "sample_rate": 24000,
                        "modules": {},
                    }
                )

            with (
                patch.dict(os.environ, overrides),
                patch("vntts.speech_worker._runtime_paths", return_value=paths),
            ):
                backend = IsolatedSpeechBackend(
                    "moss-tts",
                    CharacterVoiceRegistry(),
                    process_factory=process_factory,
                    startup_progress=statuses.append,
                )
                backend.shutdown()
                for name, value in overrides.items():
                    self.assertEqual(captured[name], value)
                    self.assertEqual(os.environ[name], value)
            self.assertTrue(
                any(overrides["HF_HUB_CACHE"] in status for status in statuses)
            )

    def test_offline_first_and_reading_first_use_the_same_worker_model_cache(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = root, root / "bin/python", root / "lib/site-packages"
            environments = []
            statuses = []

            def process_factory(_command, **options):
                environments.append(options["env"])
                return FakeProcess(
                    {
                        "type": "health",
                        "backend": "moss-tts",
                        "interpreter": str(paths[1]),
                        "prefix": str(root),
                        "runtime_site": str(paths[2]),
                        "sample_rate": 24000,
                        "modules": {},
                    }
                )

            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "vntts.model_assets.get_local_data_directory",
                    return_value=root / "appdata",
                ),
                patch("vntts.speech_worker._runtime_paths", return_value=paths),
            ):
                for reading_first in (False, True):
                    if reading_first:
                        ModelAssetManager().configure_huggingface_environment()
                    backend = IsolatedSpeechBackend(
                        "moss-tts",
                        CharacterVoiceRegistry(),
                        process_factory=process_factory,
                        startup_progress=statuses.append,
                    )
                    backend.shutdown()
                    if not reading_first:
                        self.assertNotIn("HF_HOME", os.environ)
            expected = str(root / "appdata/models/huggingface")
            self.assertEqual([e["HF_HOME"] for e in environments], [expected, expected])
            self.assertTrue(
                any(str(Path(expected) / "hub") in status for status in statuses)
            )

    def test_invalid_volume_never_installs_runtime_or_launches_process(self):
        for backend in (
            "pocket-tts",
            "chatterbox-nano",
            "moss-tts",
            "qwen-tts",
            "moss-tts-delay",
        ):
            for volume in (True, "0.5", None, -0.1, 1.1, float("nan"), float("inf")):
                with self.subTest(backend=backend, volume=volume):
                    factory = MagicMock()
                    with (
                        patch(
                            "vntts.runtime_installation.ensure_speech_runtime"
                        ) as prepare,
                        self.assertRaises(TTSConfigurationError),
                    ):
                        IsolatedSpeechBackend(
                            backend,
                            CharacterVoiceRegistry(),
                            volume=volume,
                            process_factory=factory,
                        )
                    prepare.assert_not_called()
                    factory.assert_not_called()

    def test_worker_integer_boundaries_reject_booleans(self):
        backend = FakeWorkerBackend(CharacterVoiceRegistry())
        self.assertTrue(_is_worker_backend(backend))
        document = _result_document(
            backend.render(SynthesisRequest("Narrator", "A line.")).collect()
        )
        document["limits"]["max_tokens"] = 256
        document["diagnostics"]["seed"] = 7
        self.assertEqual(_result_document_value(document), document)

        for value in (False, True):
            with self.subTest(payload_bytes=value):
                header = json.dumps({"payload_bytes": value}).encode()
                stream = BytesIO(len(header).to_bytes(4, "big") + header + b"x")
                with self.assertRaisesRegex(ValueError, "invalid payload size"):
                    _read_frame(stream)
            with self.subTest(sample_rate=value):
                backend.sample_rate = value
                self.assertFalse(_is_worker_backend(backend))
            for group, field in (("limits", "max_tokens"), ("diagnostics", "seed")):
                with self.subTest(field=field, value=value):
                    original = document[group][field]
                    document[group][field] = value
                    with self.assertRaisesRegex(
                        TTSConfigurationError, "render result is malformed"
                    ):
                        _result_document_value(document)
                    document[group][field] = original

    def test_result_decoder_rejects_missing_or_malformed_required_fields(self):
        backend = FakeWorkerBackend(CharacterVoiceRegistry())
        document = _result_document(
            backend.render(SynthesisRequest("Narrator", "A line.")).collect()
        )
        del document["completion"]
        with self.assertRaises(TTSConfigurationError):
            _result_document_value(document)
        document["completion"] = SynthesisCompletion.COMPLETE.value
        document["diagnostics"]["chunk_count"] = "one"
        with self.assertRaises(TTSConfigurationError):
            _result_document_value(document)

    def test_worker_frames_ignore_previous_process_and_report_current_exit(self):
        backend = object.__new__(IsolatedSpeechBackend)
        backend.name = "pocket-tts"
        backend._messages = queue.Queue()
        backend._stderr = ["native failure"]
        process = FakeProcess(None)
        previous = FakeProcess(None)
        document = {"type": "chunk", "request_id": "current"}
        payload = b"current PCM"
        backend._messages.put((previous, {"type": "eof"}, b""))
        backend._messages.put((process, document, payload))

        self.assertEqual(backend._next_frame(process, timeout=0), (document, payload))
        with self.assertRaises(queue.Empty):
            backend._next_frame(process, timeout=0)
        process.returncode = 1
        with self.assertRaisesRegex(
            TTSSynthesisError, "exited unexpectedly: native failure"
        ):
            backend._next_frame(process, timeout=0)

    def test_worker_process_guard_checks_stream_contracts(self):
        self.assertTrue(_is_worker_process(FakeProcess(None)))
        for name, incomplete in (
            ("stdin", SimpleNamespace(write=lambda data: None)),
            ("stdin", SimpleNamespace(flush=lambda: None)),
            ("stdout", SimpleNamespace(readline=lambda size=-1: b"")),
            ("stderr", SimpleNamespace(read=lambda size=-1: b"")),
        ):
            with self.subTest(stream=name, methods=vars(incomplete)):
                process = FakeProcess(None)
                setattr(process, name, incomplete)
                self.assertFalse(_is_worker_process(process))
        process = FakeProcess(None)
        process.stdout = SimpleNamespace(read=lambda size=-1: b"")
        process.stderr = SimpleNamespace(readline=lambda size=-1: b"")
        self.assertTrue(_is_worker_process(process))
        for name in ("stdin", "stdout", "stderr"):
            with self.subTest(optional_stream=name):
                process = FakeProcess(None)
                setattr(process, name, None)
                self.assertTrue(_is_worker_process(process))

    def test_rejected_worker_process_is_terminated(self):
        process = FakeProcess(None)
        process.stdin = object()
        with (
            TemporaryDirectory() as directory,
            patch(
                "vntts.runtime_installation.ensure_speech_runtime",
                return_value=tuple(
                    Path(directory) / name for name in ("runtime", "python", "site")
                ),
            ),
            self.assertRaisesRegex(
                TTSConfigurationError, "process factory is malformed"
            ),
        ):
            IsolatedSpeechBackend(
                "pocket-tts",
                CharacterVoiceRegistry(),
                process_factory=lambda *args, **kwargs: process,
            )
        self.assertEqual(process.returncode, -15)

    def test_registry_serializes_voice_and_assignment_with_same_fields(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            voice = CharacterVoice(
                "Narrator",
                "Narrator",
                aliases=("Guide",),
                references=(root / "reference.wav",),
                reference_root=root,
                reference_transcript="Original words.",
            )
            registry = CharacterVoiceRegistry((voice,))
            registry.assignments["hero"] = voice

            document = _serialize_registry(registry)
            self.assertEqual(document["voices"][0], document["assignments"]["hero"])
            restored = _registry_from_document(document)
            self.assertEqual(
                restored.assignments["hero"].reference_transcript, "Original words."
            )
            self.assertTrue(restored.assignments["hero"].reference_root.samefile(root))

    def test_forced_worker_shutdown_stays_bounded_when_wait_never_finishes(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = (
            subprocess.TimeoutExpired("worker", 2),
            subprocess.TimeoutExpired("worker", 2),
        )
        backend = object.__new__(IsolatedSpeechBackend)
        backend.process = process

        backend._terminate_process(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        self.assertIsNone(backend.process)

    def test_health_gate_covers_shared_and_binary_sensitive_dependencies(self):
        common = {
            "numpy",
            "scipy",
            "platformdirs",
            "vntts_artifacts",
            "durable_file",
        }
        for modules in _REQUIRED_MODULES.values():
            self.assertTrue(common.issubset(modules))
        self.assertTrue(
            {"pocket_tts", "torch", "safetensors"}.issubset(
                _REQUIRED_MODULES["pocket-tts"]
            )
        )
        self.assertTrue(
            {"torch", "torchaudio", "transformers", "tokenizers"}.issubset(
                _REQUIRED_MODULES["chatterbox-nano"]
            )
        )
        self.assertTrue(
            {"mlx.core", "mlx_audio", "transformers", "tokenizers"}.issubset(
                _REQUIRED_MODULES["moss-tts"]
            )
        )

    def test_worker_protocol_streams_typed_pcm_and_results(self):
        registry = CharacterVoiceRegistry()
        input_stream = BytesIO()
        _write_frame(
            input_stream,
            {
                "type": "initialize",
                "backend": "fake",
                "runtime_site": "/isolated/site-packages",
                "registry": _serialize_registry(registry),
                "options": {},
            },
        )
        common = {
            "request_id": "request-1",
            "registry": _serialize_registry(registry),
            "narrator_reference": None,
        }
        _write_frame(input_stream, {"type": "prime", "voice": "Narrator", **common})
        _write_frame(
            input_stream,
            {
                "type": "render",
                "voice": "Narrator",
                "text": "A line.",
                "seed": 7,
                "generation_profile": "stable",
                "cache_policy": "use",
                **common,
            },
        )
        _write_frame(
            input_stream,
            {"type": "set-live-mode", "active": True, **common},
        )
        _write_frame(input_stream, {"type": "shutdown"})
        input_stream.seek(0)
        output_stream = BytesIO()

        exit_code = worker_main(
            input_stream=input_stream,
            output_stream=output_stream,
            backend_classes={"fake": FakeWorkerBackend},
            required_modules={"fake": ()},
        )

        self.assertEqual(exit_code, 0)
        output_stream.seek(0)
        frames = []
        while (frame := _read_frame(output_stream)) is not None:
            frames.append(frame)
        self.assertEqual(
            [document["type"] for document, _payload in frames],
            ["health", "primed", "chunk", "result", "live-mode"],
        )
        self.assertEqual(frames[0][0]["device"], "cpu")
        self.assertTrue(frames[0][0]["platform"])
        self.assertTrue(frames[0][0]["machine"])
        chunk_document, chunk_payload = frames[2]
        self.assertEqual(chunk_document["shape"], [2, 1])
        np.testing.assert_allclose(
            np.frombuffer(chunk_payload, dtype=np.float32), [0.25, -0.25]
        )
        self.assertEqual(frames[3][0]["result"]["completion"], "complete")

    def test_worker_protocol_returns_underlying_identity_without_rendering(self):
        for supported in (False, True):
            with self.subTest(supported=supported):
                registry = CharacterVoiceRegistry()
                backend = FakeWorkerBackend(registry)
                backend.render = MagicMock(
                    side_effect=AssertionError("unexpected render")
                )
                if supported:
                    backend.synthesis_key = MagicMock(
                        return_value="exact-loaded-model-key"
                    )
                input_stream, output_stream = BytesIO(), BytesIO()
                _write_frame(
                    input_stream,
                    {
                        "type": "initialize",
                        "backend": "fake",
                        "runtime_site": "/isolated/site-packages",
                        "registry": _serialize_registry(registry),
                        "options": {},
                    },
                )
                _write_frame(
                    input_stream,
                    {
                        "type": "synthesis-key",
                        "request_id": "identity-1",
                        "voice": "Narrator",
                        "text": " A line. ",
                        "seed": 17,
                        "generation_profile": "stable",
                        "cache_policy": "bypass",
                        "registry": _serialize_registry(registry),
                        "narrator_reference": "/references/narrator.wav",
                    },
                )
                _write_frame(input_stream, {"type": "shutdown"})
                input_stream.seek(0)
                self.assertEqual(
                    worker_main(
                        input_stream=input_stream,
                        output_stream=output_stream,
                        backend_classes={"fake": lambda _registry: backend},
                        required_modules={"fake": ()},
                    ),
                    0,
                )
                output_stream.seek(0)
                self.assertEqual(_read_frame(output_stream)[0]["type"], "health")
                document, payload = _read_frame(output_stream)
                self.assertEqual(
                    document,
                    {
                        "type": "synthesis-key",
                        "request_id": "identity-1",
                        "value": "exact-loaded-model-key" if supported else None,
                    },
                )
                self.assertEqual(payload, b"")
                self.assertIsNone(_read_frame(output_stream))
                self.assertEqual(backend.narrator_reference, "/references/narrator.wav")
                backend.render.assert_not_called()
                if supported:
                    backend.synthesis_key.assert_called_once_with(
                        SynthesisRequest(
                            voice="Narrator",
                            text=" A line. ",
                            seed=17,
                            generation_profile="stable",
                            cache_policy=SynthesisCachePolicy.BYPASS,
                        )
                    )

    def test_module_provenance_rejects_dependency_outside_runtime(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime_site = root / "runtime" / "site-packages"
            runtime_site.mkdir(parents=True)
            outside = root / "root-environment" / "numpy" / "__init__.py"
            outside.parent.mkdir(parents=True)
            outside.touch()
            module = SimpleNamespace(__file__=str(outside), __version__="1")

            with (
                patch(
                    "vntts.speech_worker.importlib.import_module", return_value=module
                ),
                self.assertRaisesRegex(TTSConfigurationError, "outside"),
            ):
                _module_health(runtime_site, ("numpy",))

    def test_runtime_path_uses_the_selected_environment_python_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            interpreter = root / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            site_packages = (
                root / "Lib/site-packages"
                if sys.platform == "win32"
                else root / "lib/python9.9/site-packages"
            )
            site_packages.mkdir(parents=True)

            resolved_root, resolved_python, resolved_site = _runtime_paths(
                "pocket-tts", root
            )

            self.assertEqual(resolved_root, root.resolve())
            self.assertEqual(resolved_python, interpreter)
            self.assertEqual(resolved_site, site_packages.resolve())

    def test_windows_runtime_prefers_portable_base_interpreter(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            interpreter = root / "python.exe"
            interpreter.touch()
            site_packages = root / "Lib/site-packages"
            site_packages.mkdir(parents=True)

            with patch("vntts.speech_worker.sys.platform", "win32"):
                resolved_root, resolved_python, resolved_site = _runtime_paths(
                    "pocket-tts", root
                )

            self.assertEqual(resolved_root, root)
            self.assertEqual(resolved_python, interpreter)
            self.assertEqual(resolved_site, site_packages.resolve())

    def test_runtime_path_prefers_frozen_bundle_over_source_checkout(self):
        with TemporaryDirectory() as directory:
            bundle_root = Path(directory).resolve()
            root = bundle_root / "speech-runtimes" / "pocket-tts"
            interpreter = root / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            site_packages = (
                root / "Lib/site-packages"
                if sys.platform == "win32"
                else root / "lib/python9.9/site-packages"
            )
            site_packages.mkdir(parents=True)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "vntts.runtime_paths.find_bundled_speech_runtime",
                    return_value=root,
                ),
                patch("vntts.runtime_paths.get_bundle_root", return_value=bundle_root),
            ):
                resolved_root, resolved_python, resolved_site = _runtime_paths(
                    "pocket-tts"
                )

            self.assertEqual(resolved_root, root)
            self.assertEqual(resolved_python, interpreter)
            self.assertEqual(resolved_site, site_packages)

    def test_missing_frozen_runtime_does_not_recommend_developer_uv_command(self):
        with TemporaryDirectory() as directory:
            bundle_root = Path(directory).resolve()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "vntts.runtime_paths.find_bundled_speech_runtime",
                    return_value=None,
                ),
                patch("vntts.runtime_paths.get_bundle_root", return_value=bundle_root),
                self.assertRaisesRegex(
                    TTSConfigurationError, "complete release package"
                ) as raised,
            ):
                _runtime_paths("pocket-tts")

            self.assertNotIn("uv sync", str(raised.exception))

    def test_frozen_runtime_cannot_escape_the_application_bundle(self):
        with TemporaryDirectory() as bundle_directory, TemporaryDirectory() as outside:
            bundle_root = Path(bundle_directory).resolve()
            runtime = Path(outside).resolve()
            interpreter = runtime / "bin/python"
            site_packages = runtime / "lib/python3.14/site-packages"
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            site_packages.mkdir(parents=True)
            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "vntts.runtime_paths.find_bundled_speech_runtime",
                    return_value=runtime,
                ),
                patch("vntts.runtime_paths.get_bundle_root", return_value=bundle_root),
                self.assertRaisesRegex(TTSConfigurationError, "outside.*package"),
            ):
                _runtime_paths("pocket-tts")

    def test_bundled_runtime_accepts_only_contained_nested_paths(self):
        for escape, inside in (
            ("interpreter", False),
            ("site-packages", False),
            ("interpreter", True),
            ("site-packages", True),
        ):
            with (
                self.subTest(escape=escape, inside=inside),
                TemporaryDirectory() as directory,
            ):
                base = Path(directory).resolve()
                bundle = base / "bundle"
                root = bundle / "speech-runtimes/pocket-tts"
                interpreter = root / (
                    "python.exe" if sys.platform == "win32" else "bin/python"
                )
                site = root / (
                    "Lib/site-packages"
                    if sys.platform == "win32"
                    else "lib/python3.14/site-packages"
                )
                interpreter.parent.mkdir(parents=True)
                site.mkdir(parents=True)
                interpreter.touch()
                outside = (bundle if inside else base) / "shared"
                if escape == "interpreter":
                    outside.touch()
                    interpreter.unlink()
                    symlink_or_skip(interpreter, outside)
                else:
                    outside.mkdir()
                    site.rmdir()
                    symlink_or_skip(site, outside, target_is_directory=True)
                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch("vntts.runtime_paths.get_bundle_root", return_value=bundle),
                ):
                    if inside:
                        self.assertEqual(
                            resolve_speech_runtime_paths("pocket-tts"),
                            (root, interpreter, site.resolve()),
                        )
                    else:
                        with self.assertRaisesRegex(
                            TTSConfigurationError, "owning directory"
                        ):
                            resolve_speech_runtime_paths("pocket-tts")

    def test_explicit_runtime_keeps_external_interpreter_symlink(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "explicit"
            interpreter = root / (
                "python.exe" if sys.platform == "win32" else "bin/python"
            )
            site = root / (
                "Lib/site-packages"
                if sys.platform == "win32"
                else "lib/python3.14/site-packages"
            )
            interpreter.parent.mkdir(parents=True)
            site.mkdir(parents=True)
            outside = base / "python"
            outside.touch()
            symlink_or_skip(interpreter, outside)
            with patch(
                "vntts.runtime_paths.get_bundle_root", return_value=base / "bundle"
            ):
                self.assertEqual(
                    resolve_speech_runtime_paths("pocket-tts", root),
                    (root, interpreter, site),
                )

    def test_parent_launches_isolated_interpreter_without_host_support_paths(self):
        registry = CharacterVoiceRegistry()
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            interpreter = root / "bin/python"
            runtime_site = root / "lib/python3.14/site-packages"
            captured = {}

            def process_factory(command, **options):
                captured["command"] = command
                captured["options"] = options
                return FakeProcess(
                    {
                        "type": "health",
                        "backend": "pocket-tts",
                        "interpreter": str(interpreter.resolve()),
                        "prefix": str(root),
                        "runtime_site": str(runtime_site),
                        "sample_rate": 24_000,
                        "modules": {},
                    }
                )

            with (
                patch(
                    "vntts.speech_worker._runtime_paths",
                    return_value=(root, interpreter, runtime_site),
                ),
            ):
                backend = IsolatedSpeechBackend(
                    "pocket-tts", registry, process_factory=process_factory
                )

            command = captured["command"]
            self.assertEqual(command[0], str(interpreter))
            self.assertIn("-I", command)
            self.assertIn("-B", command)
            self.assertEqual(command[-1], str(Path(__file__).resolve().parents[1]))
            self.assertNotIn("site-packages", command)
            backend._terminate_process(backend.process)
            backend.health = {"device": "cuda", "accelerator": {"name": "Old GPU"}}

            def restart(command, **options):
                self.assertIsNone(backend.health)
                return process_factory(command, **options)

            backend.process_factory = restart
            backend._launch_worker()
            backend.shutdown()

    def test_frozen_parent_imports_worker_from_bundled_runtime(self):
        registry = CharacterVoiceRegistry()
        with TemporaryDirectory() as directory:
            bundle_root = Path(directory).resolve()
            runtime_root = bundle_root / "speech-runtimes/pocket-tts"
            interpreter = runtime_root / "bin/python"
            runtime_site = runtime_root / "lib/python3.14/site-packages"
            captured = {}

            def process_factory(command, **options):
                captured["command"] = command
                captured["options"] = options
                return FakeProcess(
                    {
                        "type": "health",
                        "backend": "pocket-tts",
                        "interpreter": str(interpreter.resolve()),
                        "prefix": str(runtime_root),
                        "runtime_site": str(runtime_site),
                        "sample_rate": 24_000,
                        "modules": {},
                    }
                )

            with (
                patch(
                    "vntts.speech_worker._runtime_paths",
                    return_value=(runtime_root, interpreter, runtime_site),
                ),
                patch("vntts.speech_worker.get_bundle_root", return_value=bundle_root),
                patch.dict(
                    os.environ,
                    {
                        "HF_TOKEN": "developer-token",
                        "HUGGING_FACE_HUB_TOKEN": "legacy-token",
                        "HF_HUB_CACHE": "/developer/huggingface-cache",
                    },
                ),
            ):
                backend = IsolatedSpeechBackend(
                    "pocket-tts", registry, process_factory=process_factory
                )

            self.assertEqual(captured["command"][-1], "")
            self.assertEqual(captured["options"]["cwd"], str(bundle_root))
            self.assertEqual(
                captured["options"]["env"]["HF_HUB_DISABLE_IMPLICIT_TOKEN"],
                "1",
            )
            self.assertNotIn("HF_TOKEN", captured["options"]["env"])
            self.assertNotIn("HUGGING_FACE_HUB_TOKEN", captured["options"]["env"])
            self.assertNotIn("HF_HUB_CACHE", captured["options"]["env"])
            backend.shutdown()

    def test_frozen_pocket_worker_uses_credentials_only_after_explicit_opt_in(self):
        registry = CharacterVoiceRegistry()
        with TemporaryDirectory() as directory:
            bundle_root = Path(directory).resolve()
            runtime_root = bundle_root / "speech-runtimes/pocket-tts"
            interpreter = runtime_root / "bin/python"
            runtime_site = runtime_root / "lib/python3.14/site-packages"
            captured = {}

            def process_factory(command, **options):
                captured["options"] = options
                return FakeProcess(
                    {
                        "type": "health",
                        "backend": "pocket-tts",
                        "interpreter": str(interpreter.resolve()),
                        "prefix": str(runtime_root),
                        "runtime_site": str(runtime_site),
                        "sample_rate": 24_000,
                        "modules": {},
                    }
                )

            with (
                patch(
                    "vntts.speech_worker._runtime_paths",
                    return_value=(runtime_root, interpreter, runtime_site),
                ),
                patch("vntts.speech_worker.get_bundle_root", return_value=bundle_root),
                patch.dict(
                    os.environ,
                    {
                        "HF_TOKEN": "explicit-token",
                        "HF_HUB_CACHE": "/developer/huggingface-cache",
                    },
                ),
            ):
                backend = IsolatedSpeechBackend(
                    "pocket-tts",
                    registry,
                    process_factory=process_factory,
                    allow_gated_model_access=True,
                )

            self.assertEqual(captured["options"]["env"]["HF_TOKEN"], "explicit-token")
            self.assertNotIn("HF_HUB_CACHE", captured["options"]["env"])
            self.assertNotIn(
                "HF_HUB_DISABLE_IMPLICIT_TOKEN", captured["options"]["env"]
            )
            backend.shutdown()

    def test_moss_worker_defaults_to_its_supported_stable_profile(self):
        backend = object.__new__(IsolatedSpeechBackend)

        with patch.object(IsolatedSpeechBackend, "_start_worker"):
            with patch(
                "vntts.speech_worker._runtime_paths",
                return_value=(Path("/runtime"), Path("/runtime/python"), Path("/site")),
            ):
                IsolatedSpeechBackend.__init__(
                    backend,
                    "moss-tts",
                    CharacterVoiceRegistry(),
                )

        self.assertEqual(backend.generation_profile, "stable")
        self.assertEqual(backend.model_name, "moss-tts")

    def test_stream_keeps_writing_after_an_underrun(self):
        output = MagicMock()
        stream = output.OutputStream.return_value.__enter__.return_value
        stream.write.side_effect = (True, False)
        with (
            patch.object(IsolatedSpeechBackend, "_start_worker"),
            patch(
                "vntts.speech_worker._runtime_paths",
                return_value=(Path("/runtime"), Path("/runtime/python"), Path("/site")),
            ),
        ):
            backend = IsolatedSpeechBackend(
                "moss-tts", CharacterVoiceRegistry(), audio_output=output
            )

        def render(request):
            result = FakeWorkerBackend(backend.registry).render(request).collect()

            def chunks():
                for index, frame in enumerate(result.pcm):
                    yield SynthesisChunk(frame[None, :], result.sample_rate, index, 0.0)
                return result

            return SynthesisChunkStream(chunks())

        with patch.object(backend, "render", side_effect=render):
            outcome = backend.play_prepared(
                backend.prepare_playback("Narrator", "Keep every chunk.")
            )

        self.assertTrue(outcome.successful)
        self.assertTrue(outcome.underflowed)
        self.assertEqual(stream.write.call_count, 2)
        np.testing.assert_array_equal(
            np.concatenate([call.args[0] for call in stream.write.call_args_list]),
            np.array([[0.25], [-0.25]], dtype=np.float32),
        )

    def test_stream_failure_preserves_first_audio_time(self):
        output = MagicMock()
        stream = output.OutputStream.return_value.__enter__.return_value
        stream.write.side_effect = (False, RuntimeError("device write failed"))
        with (
            patch.object(IsolatedSpeechBackend, "_start_worker"),
            patch(
                "vntts.speech_worker._runtime_paths",
                return_value=(Path("/runtime"), Path("/runtime/python"), Path("/site")),
            ),
        ):
            backend = IsolatedSpeechBackend(
                "moss-tts", CharacterVoiceRegistry(), audio_output=output
            )
        backend.clock = iter((1.0, 1.01, 1.02)).__next__

        def render(request):
            result = FakeWorkerBackend(backend.registry).render(request).collect()

            def chunks():
                for index, frame in enumerate(result.pcm):
                    yield SynthesisChunk(frame[None, :], result.sample_rate, index, 0.0)
                return result

            return SynthesisChunkStream(chunks())

        with patch.object(backend, "render", side_effect=render):
            outcome = backend.play_prepared(
                backend.prepare_playback("Narrator", "Keep the first write time.")
            )

        self.assertEqual(outcome.status.value, "failed")
        self.assertAlmostEqual(outcome.first_audio_ms, 10.0)
        self.assertIn("device write failed", outcome.error)
        self.assertEqual(stream.write.call_count, 2)

    def test_pocket_worker_ignores_an_inapplicable_saved_profile(self):
        backend = object.__new__(IsolatedSpeechBackend)
        backend.name = "pocket-tts"
        backend.generation_profile = "default"

        self.assertFalse(backend.set_generation_profile("stable"))
        self.assertEqual(backend.generation_profile, "default")

    def test_worker_exposes_the_exact_configured_model_identity(self):
        backend = object.__new__(IsolatedSpeechBackend)

        with patch.object(IsolatedSpeechBackend, "_start_worker"):
            with patch(
                "vntts.speech_worker._runtime_paths",
                return_value=(Path("/runtime"), Path("/runtime/python"), Path("/site")),
            ):
                IsolatedSpeechBackend.__init__(
                    backend,
                    "moss-tts",
                    CharacterVoiceRegistry(),
                    model_name="/models/moss-local",
                )

        self.assertEqual(backend.model_name, "/models/moss-local")
        self.assertEqual(backend.worker_options["model_name"], "/models/moss-local")

    def test_scalar_worker_request_waits_across_an_idle_poll(self):
        backend = object.__new__(IsolatedSpeechBackend)
        with patch.object(IsolatedSpeechBackend, "_start_worker"):
            with patch(
                "vntts.speech_worker._runtime_paths",
                return_value=(Path("/runtime"), Path("/runtime/python"), Path("/site")),
            ):
                IsolatedSpeechBackend.__init__(
                    backend,
                    "moss-tts",
                    CharacterVoiceRegistry(),
                )
        process = FakeProcess(None)
        response = ({"request_id": "request-1", "value": True}, b"")
        with (
            patch.object(backend, "_ensure_worker", return_value=process),
            patch.object(backend, "_send"),
            patch.object(
                backend,
                "_next_frame",
                side_effect=(queue.Empty(), response),
            ),
            patch("vntts.speech_worker.uuid.uuid4") as uuid4,
        ):
            uuid4.return_value.hex = "request-1"
            self.assertTrue(backend.prime("Narrator"))

    def test_parent_identity_uses_bounded_scalar_protocol_and_validates_results(self):
        for response_value in ("exact-loaded-model-key", None, 3, ""):
            with self.subTest(value=response_value):
                with (
                    patch.object(IsolatedSpeechBackend, "_start_worker"),
                    patch(
                        "vntts.speech_worker._runtime_paths",
                        return_value=(
                            Path("/runtime"),
                            Path("/runtime/python"),
                            Path("/site"),
                        ),
                    ),
                ):
                    backend = IsolatedSpeechBackend(
                        "moss-tts", CharacterVoiceRegistry()
                    )
                process = FakeProcess(None)
                request = SynthesisRequest(
                    "Narrator",
                    " A line. ",
                    seed=7,
                    generation_profile="stable",
                    cache_policy=SynthesisCachePolicy.REFRESH,
                )
                response = (
                    {
                        "type": "synthesis-key",
                        "request_id": "identity-1",
                        "value": response_value,
                    },
                    b"",
                )
                with (
                    patch.object(backend, "_ensure_worker", return_value=process),
                    patch.object(backend, "_send") as send,
                    patch.object(
                        backend, "_next_frame", side_effect=(queue.Empty(), response)
                    ),
                    patch("vntts.speech_worker.uuid.uuid4") as uuid4,
                ):
                    uuid4.return_value.hex = "identity-1"
                    if (
                        response_value is None
                        or isinstance(response_value, str)
                        and response_value
                    ):
                        self.assertEqual(backend.synthesis_key(request), response_value)
                    else:
                        with self.assertRaisesRegex(
                            TTSConfigurationError, "synthesis key is malformed"
                        ):
                            backend.synthesis_key(request)
                command = send.call_args.args[1]
                self.assertEqual(command["type"], "synthesis-key")
                self.assertEqual(command["voice"], request.voice)
                self.assertEqual(command["text"], request.text)
                self.assertEqual(command["seed"], request.seed)
                self.assertEqual(command["cache_policy"], "refresh")
                self.assertIsNone(process.returncode)

    def test_parent_identity_cancellation_and_timeout_terminate_worker(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                with (
                    patch.object(IsolatedSpeechBackend, "_start_worker"),
                    patch(
                        "vntts.speech_worker._runtime_paths",
                        return_value=(
                            Path("/runtime"),
                            Path("/runtime/python"),
                            Path("/site"),
                        ),
                    ),
                ):
                    backend = IsolatedSpeechBackend(
                        "moss-tts", CharacterVoiceRegistry(), request_timeout=0.001
                    )
                process = FakeProcess(None)
                cancellation = Event()

                def wait_for_response(*arguments, **options):
                    if cancelled:
                        cancellation.set()
                    raise queue.Empty

                with (
                    patch.object(backend, "_ensure_worker", return_value=process),
                    patch.object(backend, "_send"),
                    patch.object(backend, "_next_frame", side_effect=wait_for_response),
                    self.assertRaisesRegex(
                        TTSSynthesisError,
                        "cancelled" if cancelled else "did not answer 'synthesis-key'",
                    ),
                ):
                    backend.synthesis_key(
                        SynthesisRequest(
                            "Narrator", "A line.", cancellation=cancellation
                        )
                    )
                self.assertEqual(process.returncode, -15)

    def test_scalar_worker_request_times_out_and_terminates_worker(self):
        backend = object.__new__(IsolatedSpeechBackend)
        with patch.object(IsolatedSpeechBackend, "_start_worker"):
            with patch(
                "vntts.speech_worker._runtime_paths",
                return_value=(Path("/runtime"), Path("/runtime/python"), Path("/site")),
            ):
                IsolatedSpeechBackend.__init__(
                    backend,
                    "moss-tts",
                    CharacterVoiceRegistry(),
                    request_timeout=0.001,
                )
        process = FakeProcess(None)
        with (
            patch.object(backend, "_ensure_worker", return_value=process),
            patch.object(backend, "_send"),
            patch.object(backend, "_next_frame", side_effect=queue.Empty),
            self.assertRaisesRegex(TTSSynthesisError, "did not answer 'prime'"),
        ):
            backend.prime("Narrator")

        self.assertEqual(process.returncode, -15)

    def test_render_timeout_resets_after_each_chunk_and_terminates_idle_worker(self):
        with (
            patch.object(IsolatedSpeechBackend, "_start_worker"),
            patch(
                "vntts.speech_worker._runtime_paths",
                return_value=(Path("/runtime"), Path("/runtime/python"), Path("/site")),
            ),
        ):
            backend = IsolatedSpeechBackend(
                "moss-tts", CharacterVoiceRegistry(), request_timeout=1
            )
        process = FakeProcess(None)
        payload = np.zeros((1, 1), dtype=np.float32).tobytes()
        chunks = [
            (
                {
                    "type": "chunk",
                    "request_id": "request-1",
                    "shape": [1, 1],
                    "sample_rate": 24_000,
                    "index": index,
                    "elapsed_ms": 0.0,
                },
                payload,
            )
            for index in range(2)
        ]
        with (
            patch.object(backend, "_ensure_worker", return_value=process),
            patch.object(backend, "_send"),
            patch.object(backend, "_next_frame", side_effect=(*chunks, queue.Empty())),
            patch("vntts.speech_worker.uuid.uuid4") as uuid4,
            patch(
                "vntts.speech_worker.monotonic",
                side_effect=(0, 0.4, 0.5, 1.2, 1.3, 1.8, 2.4),
            ),
        ):
            uuid4.return_value.hex = "request-1"
            rendered = backend.render(SynthesisRequest("Narrator", "Hello"))
            self.assertEqual(next(rendered).index, 0)
            self.assertEqual(next(rendered).index, 1)
            with self.assertRaisesRegex(TTSSynthesisError, "did not answer 'render'"):
                next(rendered)

        self.assertEqual(process.returncode, -15)

    def test_startup_failures_have_one_cleanup_owner(self):
        for failure in (
            "cancelled",
            "timeout",
            "health",
            "interpreter",
            "prefix",
            "send",
        ):
            with self.subTest(failure=failure):
                with (
                    patch.object(IsolatedSpeechBackend, "_start_worker"),
                    patch(
                        "vntts.speech_worker._runtime_paths",
                        return_value=(
                            Path("/runtime"),
                            Path("/runtime/python"),
                            Path("/site"),
                        ),
                    ),
                ):
                    backend = IsolatedSpeechBackend(
                        "moss-tts", CharacterVoiceRegistry()
                    )
                process = FakeProcess(None)
                process.terminate = MagicMock()
                process.kill = MagicMock()
                process.wait = MagicMock(
                    side_effect=subprocess.TimeoutExpired("worker", 2)
                )
                backend.process_factory = lambda *_args, **_options: process
                use = MagicMock()
                backend._runtime_use = use
                message = {
                    "type": "health",
                    "interpreter": "/runtime/python",
                    "prefix": "/runtime",
                    "sample_rate": 24_000,
                }
                if failure == "health":
                    message = {"type": "error", "error": "model failed"}
                elif failure in {"interpreter", "prefix"}:
                    message[failure] = "/wrong"
                expected = (
                    TTSSynthesisError
                    if failure == "cancelled"
                    else TTSConfigurationError
                )
                send_error = ValueError("initialize failed")
                if failure == "send":
                    expected = ValueError
                with (
                    patch("vntts.speech_worker.threading.Thread.start"),
                    patch.object(
                        backend,
                        "_startup_cancelled",
                        return_value=failure == "cancelled",
                    ),
                    patch("vntts.speech_worker.monotonic", side_effect=(0, 9999)),
                    patch.object(backend, "_next_frame", return_value=(message, b"")),
                    patch.object(
                        backend,
                        "_send",
                        side_effect=send_error if failure == "send" else None,
                    ),
                    patch.object(
                        backend, "_terminate_process", wraps=backend._terminate_process
                    ) as terminate,
                    self.assertRaises(expected),
                ):
                    # Use a live deadline for the health-response cases.
                    if failure != "timeout":
                        with patch("vntts.speech_worker.monotonic", return_value=0):
                            backend._start_worker()
                    else:
                        backend._start_worker()
                terminate.assert_called_once_with(process)
                process.terminate.assert_called_once_with()
                process.kill.assert_called_once_with()
                self.assertEqual(process.wait.call_count, 2)
                use.close.assert_called_once_with()
                self.assertIsNone(backend.process)
                self.assertIsNone(backend._runtime_use)

    def test_cancelled_render_restarts_same_backend_with_fresh_request(self):
        cancellation = Event()
        with (
            patch.object(IsolatedSpeechBackend, "_start_worker"),
            patch(
                "vntts.speech_worker._runtime_paths",
                return_value=(Path("/runtime"), Path("/runtime/python"), Path("/site")),
            ),
        ):
            backend = IsolatedSpeechBackend(
                "moss-tts", CharacterVoiceRegistry(), startup_cancellation=cancellation
            )
        original = FakeProcess(None)
        backend.process = original

        def cancel_render(*_args, **_options):
            cancellation.set()
            raise queue.Empty

        with patch.object(backend, "_next_frame", side_effect=cancel_render):
            result = backend.render(
                SynthesisRequest("Narrator", "First.", cancellation=cancellation)
            ).collect()
        self.assertIs(result.completion, SynthesisCompletion.CANCELLED)
        self.assertEqual(original.returncode, -15)
        self.assertIsNone(backend.process)

        restarted = FakeProcess(None)
        backend.process_factory = MagicMock(return_value=restarted)
        backend._runtime_use = MagicMock()
        request = SynthesisRequest("Narrator", "Retry.")
        generated = FakeWorkerBackend(backend.registry).render(request)
        chunk = next(generated)
        expected = generated.collect()
        frames = (
            (
                {
                    "type": "health",
                    "interpreter": "/runtime/python",
                    "prefix": "/runtime",
                    "sample_rate": 24_000,
                },
                b"",
            ),
            (
                {
                    "type": "chunk",
                    "shape": list(chunk.pcm.shape),
                    "sample_rate": chunk.sample_rate,
                    "index": 0,
                    "elapsed_ms": 0.0,
                },
                chunk.pcm.tobytes(),
            ),
            ({"type": "result", "result": _result_document(expected)}, b""),
        )
        with (
            patch("vntts.speech_worker.threading.Thread.start"),
            patch.object(backend, "_next_frame", side_effect=frames),
        ):
            result = backend.render(request).collect()
        self.assertIs(result.completion, SynthesisCompletion.COMPLETE)
        np.testing.assert_array_equal(result.pcm, expected.pcm)
        self.assertIs(backend.process, restarted)
        self.assertIs(backend.startup_cancellation, cancellation)

        backend.clear_runtime_cache()
        rejected = FakeProcess(None)
        backend.process_factory.return_value = rejected
        with (
            patch("vntts.speech_worker.threading.Thread.start"),
            self.assertRaisesRegex(TTSSynthesisError, "startup was cancelled"),
        ):
            backend.prime("Narrator")
        self.assertEqual(rejected.returncode, -15)
        self.assertIsNone(backend.process)

    def test_current_render_cancellation_stops_worker_restart(self):
        with (
            patch.object(IsolatedSpeechBackend, "_start_worker"),
            patch(
                "vntts.speech_worker._runtime_paths",
                return_value=(Path("/runtime"), Path("/runtime/python"), Path("/site")),
            ),
        ):
            backend = IsolatedSpeechBackend("moss-tts", CharacterVoiceRegistry())
        cancellation = Event()
        process = FakeProcess(None)
        backend._runtime_use = MagicMock()

        def start_process(*_args, **_options):
            cancellation.set()
            return process

        backend.process_factory = start_process
        with (
            patch("vntts.speech_worker.threading.Thread.start"),
            patch.object(backend, "_next_frame") as next_frame,
            self.assertRaisesRegex(TTSSynthesisError, "startup was cancelled"),
        ):
            backend.render(
                SynthesisRequest("Narrator", "Test.", cancellation=cancellation)
            ).collect()
        next_frame.assert_not_called()
        self.assertEqual(process.returncode, -15)
        self.assertIsNone(backend.process)
        self.assertIsNone(backend._runtime_use)

    def test_startup_error_survives_process_and_claim_cleanup_failures(self):
        with (
            patch.object(IsolatedSpeechBackend, "_start_worker"),
            patch(
                "vntts.speech_worker._runtime_paths",
                return_value=(Path("/runtime"), Path("/runtime/python"), Path("/site")),
            ),
        ):
            backend = IsolatedSpeechBackend("moss-tts", CharacterVoiceRegistry())
        use = MagicMock()
        backend._runtime_use = use
        use.close.side_effect = OSError("claim cleanup failed")
        for error in (ValueError("launch failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                backend._runtime_use = use
                use.reset_mock()
                with (
                    patch.object(backend, "_launch_worker", side_effect=error),
                    patch.object(
                        backend,
                        "_terminate_process",
                        side_effect=OSError("terminate failed"),
                    ) as terminate,
                    self.assertRaises(type(error)) as raised,
                ):
                    backend._start_worker()
                self.assertIs(raised.exception, error)
                self.assertEqual(len(error.__notes__), 2)
                terminate.assert_called_once()
                use.close.assert_called_once_with()
                self.assertIsNone(backend._runtime_use)

    def test_factory_failures_preserve_cause_and_conservative_claim(self):
        for failure in ("spawn", "malformed-terminate", "malformed-poll"):
            with self.subTest(failure=failure):
                with (
                    patch.object(IsolatedSpeechBackend, "_start_worker"),
                    patch(
                        "vntts.speech_worker._runtime_paths",
                        return_value=(
                            Path("/runtime"),
                            Path("/runtime/python"),
                            Path("/site"),
                        ),
                    ),
                ):
                    backend = IsolatedSpeechBackend(
                        "moss-tts", CharacterVoiceRegistry()
                    )
                use = MagicMock()
                backend._runtime_use = use
                if failure == "spawn":
                    original = OSError("spawn failed")
                    backend.process_factory = MagicMock(side_effect=original)
                    use.launched.side_effect = OSError("launch reset failed")
                    expected = OSError
                else:
                    process = SimpleNamespace(
                        poll=MagicMock(return_value=None),
                        terminate=MagicMock(),
                        kill=MagicMock(),
                        wait=MagicMock(return_value=0),
                    )
                    backend.process_factory = MagicMock(return_value=process)
                    if failure == "malformed-terminate":
                        process.terminate.side_effect = OSError("termination failed")
                    else:
                        process.poll.side_effect = OSError("poll failed")
                    expected = TTSConfigurationError
                with self.assertRaises(expected) as raised:
                    backend._start_worker()
                self.assertEqual(len(raised.exception.__notes__), 1)
                if failure == "spawn":
                    self.assertIs(raised.exception, original)
                    use.launched.assert_called_once_with(None)
                else:
                    self.assertIn("factory is malformed", str(raised.exception))
                    use.launched.assert_not_called()
                use.close.assert_called_once_with()
                self.assertIsNone(backend.process)

    def test_worker_rejects_nonfinite_timeouts(self):
        for options in (
            {"startup_timeout": float("nan")},
            {"request_timeout": float("inf")},
        ):
            with self.subTest(options=options):
                with self.assertRaisesRegex(
                    TTSConfigurationError, "timeout must be positive and finite"
                ):
                    IsolatedSpeechBackend(
                        "moss-tts", CharacterVoiceRegistry(), **options
                    )

    def test_worker_rejects_unrepresentable_timeouts_before_process_acquisition(self):
        for name in ("startup_timeout", "request_timeout"):
            for value in (10**400, -(10**400)):
                process_factory = MagicMock()
                with self.subTest(field=name, negative=value < 0):
                    with (
                        patch(
                            "vntts.runtime_installation.ensure_speech_runtime"
                        ) as prepare,
                        self.assertRaisesRegex(
                            TTSConfigurationError, "timeout must be positive and finite"
                        ) as caught,
                    ):
                        IsolatedSpeechBackend(
                            "moss-tts",
                            CharacterVoiceRegistry(),
                            process_factory=process_factory,
                            **{name: value},
                        )
                    self.assertIsInstance(caught.exception.__cause__, OverflowError)
                    prepare.assert_not_called()
                    process_factory.assert_not_called()

    def test_worker_timeout_conversion_keeps_string_bool_and_large_finite_values(self):
        for value, expected in (("12.5", 12.5), (True, 1.0), (10**300, 1e300)):
            with (
                self.subTest(value=value),
                patch.object(IsolatedSpeechBackend, "_start_worker"),
                patch(
                    "vntts.runtime_installation.ensure_speech_runtime",
                    return_value=(
                        Path("/runtime"),
                        Path("/runtime/python"),
                        Path("/site"),
                    ),
                ),
            ):
                backend = IsolatedSpeechBackend(
                    "moss-tts",
                    CharacterVoiceRegistry(),
                    startup_timeout=value,
                    request_timeout=value,
                )
                self.assertEqual(
                    (backend.startup_timeout, backend.request_timeout),
                    (expected, expected),
                )

    def test_cancelled_startup_terminates_the_exact_worker(self):
        registry = CharacterVoiceRegistry()
        cancellation = Event()
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            interpreter = root / "bin/python"
            runtime_site = root / "lib/python3.14/site-packages"
            process = FakeProcess(None)

            def start_process(*_args, **_options):
                cancellation.set()
                return process

            with (
                patch(
                    "vntts.speech_worker._runtime_paths",
                    return_value=(root, interpreter, runtime_site),
                ),
                self.assertRaisesRegex(TTSSynthesisError, "cancelled"),
            ):
                IsolatedSpeechBackend(
                    "pocket-tts",
                    registry,
                    process_factory=start_process,
                    startup_cancellation=cancellation,
                )

            self.assertEqual(process.returncode, -15)


if __name__ == "__main__":
    unittest.main()

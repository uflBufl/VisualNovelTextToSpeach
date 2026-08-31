import os
import queue
import sys
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from vntts.speech_backend import TTSConfigurationError, TTSSynthesisError
from vntts.speech_worker import (
    _REQUIRED_MODULES,
    IsolatedSpeechBackend,
    _module_health,
    _read_frame,
    _runtime_paths,
    _serialize_registry,
    _write_frame,
    worker_main,
)
from vntts.synthesis import (
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import CharacterVoiceRegistry


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


class FakeProcess:
    def __init__(self, health):
        output = BytesIO()
        if health is not None:
            _write_frame(output, health)
        output.seek(0)
        self.stdin = BytesIO()
        self.stdout = output
        self.stderr = BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class SpeechWorkerTest(unittest.TestCase):
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
                    "vntts.speech_worker.find_bundled_speech_runtime",
                    return_value=root,
                ),
                patch("vntts.speech_worker.get_bundle_root", return_value=bundle_root),
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
                    "vntts.speech_worker.find_bundled_speech_runtime",
                    return_value=None,
                ),
                patch("vntts.speech_worker.get_bundle_root", return_value=bundle_root),
                self.assertRaisesRegex(
                    TTSConfigurationError, "complete release package"
                ) as raised,
            ):
                _runtime_paths("pocket-tts")

            self.assertNotIn("uv sync", str(raised.exception))

    def test_parent_launches_isolated_interpreter_without_host_support_paths(self):
        registry = CharacterVoiceRegistry()
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            interpreter = root / "bin/python"
            runtime_site = root / "lib/python3.11/site-packages"
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
            backend.shutdown()

    def test_frozen_parent_imports_worker_from_bundled_runtime(self):
        registry = CharacterVoiceRegistry()
        with TemporaryDirectory() as directory:
            bundle_root = Path(directory).resolve()
            runtime_root = bundle_root / "speech-runtimes/pocket-tts"
            interpreter = runtime_root / "bin/python"
            runtime_site = runtime_root / "lib/python3.11/site-packages"
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
            runtime_site = runtime_root / "lib/python3.11/site-packages"
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

    def test_cancelled_startup_terminates_the_exact_worker(self):
        registry = CharacterVoiceRegistry()
        cancellation = Event()
        cancellation.set()
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            interpreter = root / "bin/python"
            runtime_site = root / "lib/python3.11/site-packages"
            process = FakeProcess(None)

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
                    process_factory=lambda *_args, **_options: process,
                    startup_cancellation=cancellation,
                )

            self.assertEqual(process.returncode, -15)


if __name__ == "__main__":
    unittest.main()

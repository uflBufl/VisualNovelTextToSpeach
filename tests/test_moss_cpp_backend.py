"""Exercise the C++ adapter over HTTP and real child-process lifecycle, without weights."""

import base64
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

import numpy as np
import soundfile as sf

from vntts.moss_cpp_backend import MossCppVoiceRouterBackend, moss_cpp_paths
from vntts.onboarding import OnboardingDiagnostics
from vntts.pregeneration_voices import resolve_pregeneration_settings
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError
from vntts.settings import AppSettings
from vntts.speech_worker import create_moss_worker_backend
from vntts.synthesis import SynthesisCompletion, SynthesisRequest
from vntts.tts_benchmark import main as benchmark_main
from vntts.voices import CharacterVoiceRegistry

# Independent protocol peer: only the native executable is substituted. HTTP,
# WAV parsing, sampling, cache publication, cancellation and shutdown are real.
SERVER = r"""
import base64, hashlib, io, json, sys, time, wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import TCPServer
root = Path(__file__).parent
legacy = (root / 'legacy-runtime').exists()
if '--help' in sys.argv:
    if (root / 'slow-help').exists():
        (root / 'help-started').touch()
        time.sleep(30)
    print('Usage: --model PATH --n-gpu-layers N' + ('' if legacy else ' --voice-dir DIR'), file=sys.stderr)
    sys.exit(0)
if legacy and '--voice-dir' in sys.argv: sys.exit('unknown arg: --voice-dir')
port = int(sys.argv[sys.argv.index('--port') + 1])
voice_dir = None if '--voice-dir' not in sys.argv else Path(sys.argv[sys.argv.index('--voice-dir') + 1])
if voice_dir is not None: voice_dir.mkdir(parents=True, exist_ok=True)
codes_cache = {}
startup_log = root / 'startup.log'
if startup_log.exists(): print(startup_log.read_text(), flush=True)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(dict(
            architecture='moss_tts_local', sampling_rate=48000, n_channels=2,
            n_vq=12, codec_loaded=True, version='0.2.0' if legacy else '0.3.0',
            voice_registry=voice_dir is not None and not (root / 'disable-registry').exists(),
        )).encode())
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        (root / 'request.json').write_text(json.dumps(body))
        if 'voice' in body:
            assert 'reference_wav_b64' not in body and 'ref_text' not in body
            voice_id = body['voice']
            if voice_id not in codes_cache:
                wav = (voice_dir / (voice_id + '.wav')).read_bytes()
                assert hashlib.sha256(wav).hexdigest() == voice_id
                assert wav.startswith(b'RIFF')
                assert json.loads((voice_dir / (voice_id + '.json')).read_text()) == {}
                codes_cache[voice_id] = True
                (root / 'encoded.json').write_text(json.dumps(list(codes_cache)))
        if body['text'] == 'Wait.': time.sleep(30)
        if body['text'] == 'Fail.':
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'failed generation')
            return
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(b'\x00\x10\x00\xf0' * 4800)
        self.send_response(200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('X-MOSS-Audio-Frames', str(
            body['sampling']['max_audio_frames'] if body['text'] == 'Limit.' else 2))
        self.end_headers()
        data = audio.getvalue()
        self.wfile.write(data[:50] if body['text'] == 'Truncated.' else data)
class LoopbackServer(HTTPServer):
    def server_bind(self):
        # This protocol peer has no need for HTTPServer's reverse-DNS lookup.
        TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]
LoopbackServer(('127.0.0.1', port), Handler).serve_forever()
"""


class MossCppBackendTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.model = self.root / "local.gguf"
        self.model.write_bytes(b"GGUF test model")
        self.model.with_suffix(".extras.gguf").write_bytes(b"GGUF test codec")
        self.script = self.root / "server.py"
        self.script.write_text(SERVER)
        self.reference = self.root / "reference.wav"
        sf.write(self.reference, np.full(4800, 0.1), 48000)
        env = {
            "VNTTS_MOSS_CPP_EXECUTABLE": sys.executable,
            "VNTTS_MOSS_GGUF": str(self.model),
            "VNTTS_MOSS_GPU_LAYERS": "0",
            "VNTTS_MOSS_AUX_CPU": "1",
            "VNTTS_MOSS_CONTEXT": "4096",
        }
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, env).start()
        real_popen = subprocess.Popen
        self.children = []
        self.commands = []
        self.probes = []

        def launch(command, **options):
            child = real_popen(
                [sys.executable, str(self.script), *command[1:]], **options
            )
            if "--help" in command:
                self.probes.append(child)
                return child
            self.commands.append(command)
            self.children.append(child)
            return child

        patch("vntts.moss_cpp_backend.subprocess.Popen", side_effect=launch).start()

    def backend(self, **options):
        backend = create_moss_worker_backend(
            CharacterVoiceRegistry(),
            narrator_reference=self.reference,
            persistent_audio_cache_directory=self.root / "cache",
            prompt_cache_directory=self.root / "prompts",
            startup_timeout=10,
            **options,
        )
        self.addCleanup(backend.shutdown)
        return backend

    def test_reference_seed_profile_cache_and_owned_shutdown(self):
        backend = self.backend()
        self.assertIsInstance(backend, MossCppVoiceRouterBackend)
        self.assertFalse(backend.capabilities.streaming)
        request = SynthesisRequest(
            "Narrator", "Hello there.", seed=0, generation_profile="natural"
        )
        result = backend.render(request).collect()
        self.assertEqual(result.completion, SynthesisCompletion.COMPLETE)
        self.assertEqual(result.pcm.shape, (4800, 2))
        self.assertEqual(result.sample_rate, 48000)
        body = json.loads((self.root / "request.json").read_text())
        self.assertFalse(body["stream"])
        self.assertEqual(body["sampling"]["seed"], 0)
        self.assertEqual(body["sampling"]["audio_temperature"], 1.2)
        self.assertEqual(len(body["voice"]), 64)
        self.assertNotIn("reference_wav_b64", body)
        self.assertIn("--aux-cpu", self.commands[0])
        self.assertIn("127.0.0.1", self.commands[0])
        self.assertTrue(backend.model_name.startswith("openmoss-cpp:sha256:"))
        cached = backend.render(request).collect()
        self.assertEqual(cached.diagnostics.cache_source, "memory-cache")
        self.assertEqual(len(self.children), 1)
        backend.shutdown()
        self.assertIsNotNone(self.children[0].poll())

    def test_references_are_reused_by_content_and_cleaned_up_on_restart(self):
        backend = self.backend()
        directory = Path(backend.server_directory.name)
        for text in ("First line.", "Another line."):
            backend.render(SynthesisRequest("Narrator", text)).collect()
        encoded = json.loads((self.root / "encoded.json").read_text())
        self.assertEqual(len(encoded), 1)
        voice = json.loads((self.root / "request.json").read_text())["voice"]
        self.assertEqual(voice, encoded[0])
        self.assertEqual(len(list((directory / "voices").glob("*.wav"))), 1)
        # A content change at the same reference path must get new native codes.
        sf.write(self.reference, np.full(4800, 0.2), 48000)
        backend.render(SynthesisRequest("Narrator", "Changed voice.")).collect()
        self.assertEqual(len(json.loads((self.root / "encoded.json").read_text())), 2)
        backend._stop_server()
        self.assertFalse(directory.exists())
        backend.render(SynthesisRequest("Narrator", "After restart.")).collect()
        self.assertEqual(len(json.loads((self.root / "encoded.json").read_text())), 1)

    def test_inline_reference_compatibility_when_registry_not_reported(self):
        (self.root / "disable-registry").touch()
        backend = self.backend()
        backend.render(SynthesisRequest("Narrator", "Inline reference.")).collect()
        body = json.loads((self.root / "request.json").read_text())
        self.assertTrue(base64.b64decode(body["reference_wav_b64"]).startswith(b"RIFF"))
        self.assertNotIn("voice", body)

    def test_older_native_runtime_uses_inline_reference_without_unknown_flag(self):
        (self.root / "legacy-runtime").touch()
        backend = self.backend()
        for text in ("Legacy reference.", "After restarting."):
            backend.render(SynthesisRequest("Narrator", text)).collect()
            body = json.loads((self.root / "request.json").read_text())
            self.assertTrue(
                base64.b64decode(body["reference_wav_b64"]).startswith(b"RIFF")
            )
            self.assertNotIn("voice", body)
            self.assertNotIn("--voice-dir", self.commands[-1])
            backend._stop_server()
        self.assertEqual(len(self.probes), 1)
        self.assertEqual(self.probes[0].poll(), 0)

    def test_cancellation_during_capability_probe_leaves_no_child(self):
        (self.root / "slow-help").touch()
        cancellation = Event()
        errors = []

        def launch():
            try:
                self.backend(startup_cancellation=cancellation)
            except TTSSynthesisError as error:
                errors.append(error)

        task = Thread(target=launch)
        task.start()
        try:
            for _ in range(100):
                if (self.root / "help-started").exists():
                    break
                cancellation.wait(0.02)
            self.assertTrue((self.root / "help-started").exists())
        finally:
            cancellation.set()
            task.join(timeout=5)
        self.assertFalse(task.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.children, [])
        self.assertIsNotNone(self.probes[0].poll())

    def test_runtime_status_uses_actual_offload_and_aux_logs_and_clears_on_stop(self):
        cases = (
            (
                "-1",
                "load_tensors: offloaded 37/37 layers to GPU\n"
                "llama_model_load: using device Vulkan0 (NVIDIA GeForce RTX 2070 SUPER) (0000:01:00.0) - 8000 MiB free\n"
                "Model::load: aux backend = CPU\n",
                "GPU: NVIDIA GeForce RTX 2070 SUPER (Vulkan0), 37/37 GPU layers; audio model/codec: CPU",
            ),
            (
                "12",
                "load_tensors: offloaded 12/37 layers to GPU\n"
                "Model::load: pinning to GPU 0 (CUDA0, 8000/8192 MiB free)\n"
                "Model::load: aux backend = CUDA0\n",
                "GPU + CPU: CUDA0, 12/37 GPU layers; audio model/codec: CUDA0",
            ),
            ("0", "Model::load: aux backend = CPU\n", "CPU (explicitly selected)"),
            (
                "-1",
                "Model::load: no GPU device found; using CPU backend\n"
                "Model::load: aux backend = CPU\n",
                "CPU (no GPU detected)",
            ),
            (
                "-1",
                "load_tensors: offloaded 0/37 layers to GPU\n",
                "CPU (0/37 GPU layers; GPU requested)",
            ),
            (
                "-1",
                "Model::load: pinning to GPU 0 (Vulkan0, 8000/8192 MiB free)\n",
                "device unconfirmed (GPU offload requested); audio model/codec: device unconfirmed",
            ),
            ("-1", "", "device unconfirmed (GPU offload requested)"),
        )
        for layers, log, expected in cases:
            with self.subTest(layers=layers, log=log):
                os.environ["VNTTS_MOSS_GPU_LAYERS"] = layers
                (self.root / "startup.log").write_text(log)
                progress = []
                backend = self.backend(startup_progress=progress.append)
                self.assertIn(expected, backend.runtime_status)
                self.assertIn(backend.runtime_status, progress)
                directory = Path(backend.server_directory.name)
                backend._stop_server()
                self.assertIsNone(backend.runtime_status)
                self.assertFalse(directory.exists())
                (self.root / "startup.log").write_text("")
                backend._start_server(lambda: False)
                if layers != "0":
                    self.assertIn("device unconfirmed", backend.runtime_status)
                backend.server.terminate()
                backend.server.wait(timeout=2)
                self.assertIsNone(backend.runtime_status)
                backend.shutdown()

    def test_limit_is_not_cached_as_complete(self):
        backend = self.backend()
        request = SynthesisRequest("Narrator", "Limit.")
        result = backend.render(request).collect()
        self.assertEqual(result.completion, SynthesisCompletion.LIMITED)
        result = backend.render(request).collect()
        self.assertEqual(result.diagnostics.cache_source, "fresh-generation")

    def test_default_requests_gpu_offload_but_explicit_cpu_is_respected(self):
        for layers in (None, "0"):
            with self.subTest(layers=layers):
                os.environ.pop("VNTTS_MOSS_GPU_LAYERS", None)
                if layers is not None:
                    os.environ["VNTTS_MOSS_GPU_LAYERS"] = layers
                progress = []
                backend = self.backend(startup_progress=progress.append)
                command = self.commands[-1]
                self.assertEqual(
                    command[command.index("--n-gpu-layers") + 1], layers or "-1"
                )
                self.assertIn("--aux-cpu", command)
                self.assertTrue(
                    any(
                        ("CPU only" if layers == "0" else "automatic GPU offload")
                        in message
                        for message in progress
                    )
                )
                backend.shutdown()

    def test_server_failure_and_truncated_wav_cannot_populate_cache(self):
        backend = self.backend()
        for text in ("Fail.", "Truncated."):
            with self.subTest(text=text):
                request = SynthesisRequest("Narrator", text)
                with self.assertRaises(TTSSynthesisError):
                    backend.render(request).collect()
                self.assertIsNone(backend.server)
                self.assertEqual(list((self.root / "cache").glob("*.npy")), [])

    def test_cancellation_kills_owned_server_and_next_render_restarts(self):
        backend = self.backend()
        cancellation = Event()
        result = []
        task = Thread(
            target=lambda: result.append(
                backend.render(
                    SynthesisRequest("Narrator", "Wait.", cancellation=cancellation),
                ).collect()
            )
        )
        task.start()
        try:
            for _ in range(100):
                if (self.root / "request.json").exists():
                    break
                cancellation.wait(0.02)
            self.assertTrue((self.root / "request.json").exists())
            cancellation.set()
            task.join(5)
            self.assertFalse(task.is_alive())
            self.assertEqual(result[0].completion, SynthesisCompletion.CANCELLED)
            self.assertIsNotNone(self.children[0].poll())
            self.assertEqual(
                backend.render(SynthesisRequest("Narrator", "Again."))
                .collect()
                .completion,
                SynthesisCompletion.COMPLETE,
            )
            self.assertEqual(len(self.children), 2)
        finally:
            cancellation.set()
            backend.shutdown()
            task.join(5)

    def test_weights_and_runtime_settings_have_distinct_cache_identity(self):
        backend = self.backend()
        identity = backend.model_name
        backend.shutdown()
        self.model.with_suffix(".extras.gguf").write_bytes(b"GGUF different codec")
        changed = self.backend()
        self.assertNotEqual(changed.model_name, identity)

    def test_windows_without_cpp_configuration_never_loads_mlx(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.moss_cpp_backend.sys.platform", "win32"),
            patch("vntts.speech_worker.IsolatedSpeechBackend") as mlx,
            patch("vntts.moss_cpp_installation.ensure_moss_cpp") as setup,
            self.assertRaisesRegex(TTSConfigurationError, r"C\+\+/GGUF"),
        ):
            create_moss_worker_backend(CharacterVoiceRegistry())
        mlx.assert_not_called()
        setup.assert_called_once()

    def test_cpp_environment_replaces_saved_mlx_model_path(self):
        _exe, model, _sidecar = moss_cpp_paths("/old/models/moss-mlx-int8")
        self.assertEqual(model, self.model.resolve())

    def test_invalid_setup_and_windows_routing(self):
        settings = AppSettings(speech_backend="moss-tts", tts_model=str(self.model))
        self.assertEqual(
            resolve_pregeneration_settings(settings).speech_backend,
            "moss-tts",
        )
        with patch.dict(os.environ, {"VNTTS_MOSS_GPU_LAYERS": "oops"}):
            with self.assertRaises(TTSConfigurationError):
                self.backend()
        self.model.with_suffix(".extras.gguf").unlink()
        with self.assertRaisesRegex(TTSConfigurationError, "sidecar"):
            moss_cpp_paths()
        self.assertEqual(self.children, [])

    def test_wrong_server_model_and_cancelled_startup_leave_no_child(self):
        with patch.object(
            MossCppVoiceRouterBackend,
            "_http",
            return_value=(200, {}, b'{"architecture":"moss_tts_delay"}'),
        ):
            with self.assertRaisesRegex(TTSConfigurationError, "Local v1.5"):
                self.backend()
        self.assertIsNotNone(self.children[0].poll())
        cancellation = Event()
        cancellation.set()
        with self.assertRaisesRegex(TTSSynthesisError, "cancelled"):
            self.backend(startup_cancellation=cancellation)
        self.assertEqual(len(self.children), 1)

    def test_onboarding_checks_cpp_files_without_installing_mlx(self):
        settings = AppSettings(speech_backend="moss-tts", tts_model=str(self.model))
        diagnostics = OnboardingDiagnostics()
        with patch("vntts.runtime_installation.ensure_speech_runtime") as install:
            with patch.object(diagnostics, "run", return_value=()):
                diagnostics.prepare_and_run(
                    settings, cancellation=Event(), progress=lambda _: None
                )
            install.assert_not_called()
        result = diagnostics._check_model(settings)
        self.assertEqual(result.name, "MOSS C++ runtime")
        self.assertEqual(result.status, "warning")

    def test_windows_benchmark_arguments_render_and_shutdown(self):
        with patch(
            "vntts.tts_benchmark.find_default_voice_manifest", return_value=None
        ):
            result = benchmark_main(
                [
                    "--backend",
                    "moss-tts",
                    "--model",
                    str(self.model),
                    "--character",
                    "Narrator",
                    "--narrator-reference",
                    str(self.reference),
                    "--output",
                    str(self.root / "benchmark"),
                ]
            )
        self.assertEqual(result, 0)
        self.assertTrue(list((self.root / "benchmark").glob("*.wav")))
        self.assertTrue(all(child.poll() is not None for child in self.children))


if __name__ == "__main__":
    unittest.main()

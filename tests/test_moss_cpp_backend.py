"""Exercise the C++ adapter over HTTP and real child-process lifecycle, without weights."""

import base64
import json
import os
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

import numpy as np
import soundfile as sf

from vntts.moss_cpp_backend import (
    MossCppVoiceRouterBackend,
    _aux_cpu_workers,
    _diagnostic_file_size,
    _native_stage_timings,
    moss_cpp_paths,
)
from vntts.onboarding import OnboardingDiagnostics
from vntts.pregeneration_voices import resolve_pregeneration_settings
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError
from vntts.settings import AppSettings
from vntts.speech_worker import create_moss_worker_backend
from vntts.support import NativeSpeechLog, RuntimeSupportLog, SupportBundleBuilder
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
adaptive = (root / 'adaptive-runtime').exists()
if '--help' in sys.argv:
    if (root / 'slow-help').exists():
        (root / 'help-started').touch()
        time.sleep(30)
    print('Usage: --model PATH --n-gpu-layers N' + ('' if legacy else ' --voice-dir DIR') + (' --capabilities-json' if adaptive else ''), file=sys.stderr)
    sys.exit(0)
if '--capabilities-json' in sys.argv:
    print(json.dumps({} if (root / 'adaptive-invalid').exists() else {
        'schema': 'vntts.openmoss.capabilities', 'version': 1,
        'vulkan_optional': True,
        'vulkan_available': not (root / 'no-vulkan').exists(),
        'local_gpu': True, 'aux_cpu_threads': True,
        'aux_cpu_threads_default': 4, 'aux_cpu_threads_min': 1,
        'aux_cpu_threads_max': 16,
    }))
    sys.exit(0)
if legacy and '--voice-dir' in sys.argv: sys.exit('unknown arg: --voice-dir')
if adaptive and '--local-gpu' in sys.argv and (root / 'fail-local-gpu').exists():
    print('VNTTS_STARTUP_FAILURE_JSON={"category":"local_gpu"}', flush=True)
    sys.exit(23)
if adaptive and sys.argv[sys.argv.index('--n-gpu-layers') + 1] == '-1' and (root / 'fail-vulkan').exists():
    print('VNTTS_STARTUP_FAILURE_JSON={"category":"vulkan_allocation"}', flush=True)
    sys.exit(24)
if adaptive and (root / 'fail-unknown').exists():
    print('VNTTS_STARTUP_FAILURE_JSON={"category":"model_corrupt"}', flush=True)
    sys.exit(25)
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
        info = dict(
            architecture='moss_tts_local', sampling_rate=48000, n_channels=2,
            n_vq=12, codec_loaded=True, version='0.2.0' if legacy else '0.3.0',
            voice_registry=voice_dir is not None and not (root / 'disable-registry').exists(),
        )
        if adaptive and '--aux-cpu-threads' in sys.argv:
            layers = int(sys.argv[sys.argv.index('--n-gpu-layers') + 1])
            workers = int(sys.argv[sys.argv.index('--aux-cpu-threads') + 1])
            info['placement'] = dict(
                backbone='CPU' if layers == 0 else 'Vulkan GPU',
                local='Vulkan GPU' if '--local-gpu' in sys.argv else 'CPU',
                auxiliary='CPU', gpu_layers=0 if layers == 0 else 37,
                aux_cpu_threads=workers,
            )
        self.wfile.write(json.dumps(info).encode())
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        (root / 'request.json').write_text(json.dumps(body))
        with (root / 'requests.jsonl').open('a') as requests:
            requests.write(json.dumps(body) + '\n')
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
                print(f"[server] voice '{voice_id}' encoded: 10 frames in 0.40s (now cached)", flush=True)
            # Local v0.3.0 emits no cache-hit marker (the Delay pipeline does).
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
            sample = b'\x00\x10\x00\x10' if (root / 'audition').exists() else b'\x00\x10\x00\xf0'
            wav.writeframes(sample * 4800)
        self.send_response(200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('X-MOSS-Audio-Frames', str(
            body['sampling']['max_audio_frames'] if body['text'] == 'Limit.' or (
                (root / 'audition').exists() and body['sampling']['seed'] == 1) else 2))
        if not (root / 'missing-timings').exists():
            self.send_header('X-MOSS-Generate-Seconds', '1.25')
            self.send_header('X-MOSS-Decode-Seconds', '0.125')
            print('[generate] prefill done in 0.05s', flush=True)
        if (root / 'phase-timings').exists():
            self.send_header('X-MOSS-Backbone-Seconds', '0.5')
            self.send_header('X-MOSS-Frame-Decoder-Seconds', '0.6')
            self.send_header('X-MOSS-Input-Embedding-Seconds', '0.02')
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
    def test_disappearing_weight_file_is_unknown_for_diagnostics(self):
        with TemporaryDirectory() as directory:
            self.assertIsNone(_diagnostic_file_size(Path(directory) / "gone.gguf"))

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
        self.native_log = NativeSpeechLog(maximum_entries=20)
        patch("vntts.support.native_speech_log", self.native_log).start()
        self.resource_probe = patch(
            "vntts.moss_cpp_backend.NativeResourceSampler"
        ).start()
        self.resource_probe.return_value.finish.return_value = {
            "status": "sampled",
            "sample_count": 2,
            "native_rss_peak_bytes": 1024,
        }
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
            if "--port" in command:
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

    @staticmethod
    def _windows_sharing_violation():
        error = PermissionError(13, "server.log")
        error.winerror = 32
        return error

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
        self.assertEqual(body["sampling"]["seed"], 1)
        self.assertEqual(body["sampling"]["audio_temperature"], 1.2)
        self.assertEqual(len(body["voice"]), 64)
        self.assertNotIn("reference_wav_b64", body)
        self.assertIn("--aux-cpu", self.commands[0])
        self.assertIn("127.0.0.1", self.commands[0])
        self.assertTrue(backend.model_name.startswith("openmoss-cpp:sha256:"))
        cached = backend.render(request).collect()
        self.assertEqual(cached.diagnostics.cache_source, "memory-cache")
        self.assertEqual(len(self.children), 1)
        backend.render(SynthesisRequest("Narrator", "Hello there.")).collect()
        body = json.loads((self.root / "request.json").read_text())
        self.assertEqual(body["sampling"]["audio_temperature"], 1.7)
        backend.shutdown()
        self.assertIsNotNone(self.children[0].poll())

    def test_shutdown_stops_owned_server_when_stop_is_interrupted(self):
        backend = self.backend()
        with patch.object(backend, "stop", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                backend.shutdown()
        self.assertIsNotNone(self.children[0].poll())

    def test_interrupted_startup_stops_owned_server(self):
        with patch(
            "vntts.moss_cpp_backend.MossTTSVoiceRouterBackend.__init__",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.backend()
        self.assertEqual(len(self.children), 1)
        self.assertIsNotNone(self.children[0].poll())

    def test_shutdown_retries_transient_windows_server_log_lock(self):
        backend = self.backend()
        directory = backend.server_directory
        server = backend.server
        log = backend.server_log
        path = Path(directory.name)
        cleanup = directory.cleanup
        attempts = 0

        def lock_then_cleanup():
            nonlocal attempts
            self.assertIsNotNone(server.poll())
            self.assertTrue(log.closed)
            attempts += 1
            if attempts == 1:
                raise self._windows_sharing_violation()
            cleanup()

        with (
            patch.object(
                directory,
                "cleanup",
                side_effect=lock_then_cleanup,
            ) as mocked_cleanup,
            patch("vntts.moss_cpp_backend.sleep") as mocked_sleep,
        ):
            backend.shutdown()
        self.assertEqual(mocked_cleanup.call_count, 2)
        mocked_sleep.assert_called_once_with(0.05)
        self.assertFalse(path.exists())

    def test_shutdown_defers_persistent_windows_server_log_lock_then_retries(self):
        backend = self.backend()
        directory = backend.server_directory
        server = backend.server
        log = backend.server_log

        def locked_cleanup():
            self.assertIsNotNone(server.poll())
            self.assertTrue(log.closed)
            raise self._windows_sharing_violation()

        with (
            patch.object(directory, "cleanup", side_effect=locked_cleanup) as cleanup,
            patch("vntts.moss_cpp_backend.sleep") as mocked_sleep,
        ):
            backend.shutdown()
        self.assertEqual(cleanup.call_count, 3)
        mocked_sleep.assert_any_call(0.05)
        mocked_sleep.assert_any_call(0.1)
        self.assertEqual(backend._deferred_server_directories, [directory])
        backend.shutdown()
        self.assertEqual(backend._deferred_server_directories, [])
        self.assertFalse(Path(directory.name).exists())

    def test_locked_log_does_not_mask_original_synthesis_error(self):
        backend = self.backend()
        directory = backend.server_directory
        with (
            patch.object(
                directory, "cleanup", side_effect=self._windows_sharing_violation()
            ),
            patch("vntts.moss_cpp_backend.sleep"),
            self.assertRaisesRegex(TTSSynthesisError, "HTTP 500"),
        ):
            backend.render(SynthesisRequest("Narrator", "Fail.")).collect()
        self.assertEqual(backend._deferred_server_directories, [directory])
        backend.shutdown()
        self.assertFalse(Path(directory.name).exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows file sharing semantics")
    def test_shutdown_with_real_windows_log_reader_does_not_fail_audio(self):
        import ctypes
        from ctypes import wintypes

        backend = self.backend()
        directory = Path(backend.server_directory.name)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        # Reader permits writing, but holds deletion until its handle closes.
        handle = kernel32.CreateFileW(
            str(directory / "server.log"), 0x80000000, 0x3, None, 3, 0x80, None
        )
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        try:
            result = backend.render(
                SynthesisRequest("Narrator", "Hello there.")
            ).collect()
            backend.shutdown()
            self.assertEqual(result.completion, SynthesisCompletion.COMPLETE)
            self.assertTrue(directory.exists())
            self.assertIsNotNone(self.children[0].poll())
        finally:
            kernel32.CloseHandle(handle)
            backend.shutdown()
        self.assertFalse(directory.exists())

    def test_shutdown_reraises_nonsharing_directory_permission_error(self):
        backend = self.backend()
        directory = backend.server_directory
        server = backend.server
        log = backend.server_log
        error = PermissionError(13, "server.log")

        def denied_cleanup():
            self.assertIsNotNone(server.poll())
            self.assertTrue(log.closed)
            raise error

        with (
            patch.object(directory, "cleanup", side_effect=denied_cleanup) as cleanup,
            patch("vntts.moss_cpp_backend.sleep") as mocked_sleep,
            self.assertRaises(PermissionError),
        ):
            backend.shutdown()
        cleanup.assert_called_once_with()
        mocked_sleep.assert_not_called()
        directory.cleanup()

    def test_pause_probe_uses_real_adapter_and_preserves_native_responses(self):
        from scripts import moss_native_pause_probe as probe
        from tests.test_pregeneration_audition import clean_wav_bytes

        self.reference.write_bytes(clean_wav_bytes())
        output = self.root / "pause-probe"
        options = probe._parser().parse_args(
            ["--reference", str(self.reference), "--output", str(output)]
        )
        with patch("vntts.moss_cpp_installation._download") as download:
            self.assertEqual(probe.run(options, settings_loader=AppSettings), 0)
            download.assert_not_called()
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(len(report["attempts"]), 6)
        self.assertTrue(
            all(row["completion"] == "complete" for row in report["attempts"])
        )
        self.assertEqual(sum("--port" in command for command in self.commands), 1)
        self.assertTrue(all(child.poll() is not None for child in self.children))
        self.assertEqual(
            report["server_shutdown"],
            {
                "servers": [
                    {
                        "pid": self.children[0].pid,
                        "returncode": self.children[0].returncode,
                        "confirmed_exited": True,
                    }
                ],
                "confirmed_exited": True,
            },
        )
        body = json.loads((self.root / "request.json").read_text())
        self.assertEqual(body["sampling"]["audio_temperature"], 1.7)
        self.assertEqual(body["sampling"]["seed"], 1)
        requests = [
            json.loads(line)
            for line in (self.root / "requests.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [body["sampling"]["audio_temperature"] for body in requests],
            [0.8] * 3 + [1.7] * 3,
        )
        for body, attempt in zip(requests, report["attempts"], strict=True):
            self.assertEqual(attempt["native"]["operation"], "fresh-generation")
            self.assertEqual(attempt["native"]["seed"], 1)
            self.assertIn("resources", attempt["native"])
            for key, value in attempt["sampling"].items():
                self.assertEqual(body["sampling"][key], value)
        self.assertEqual(
            MossCppVoiceRouterBackend._generation_profiles["stable"][
                "audio_temperature"
            ],
            1.7,
        )
        from vntts.speech_backend import get_moss_tts_generation_profile

        self.assertEqual(
            get_moss_tts_generation_profile("stable")[1]["audio_temperature"], 0.8
        )
        with zipfile.ZipFile(output.with_suffix(".zip")) as archive:
            raw = [name for name in archive.namelist() if name.endswith("-raw.wav")]
            self.assertEqual(len(raw), 6)
            self.assertTrue(all(archive.read(name).startswith(b"RIFF") for name in raw))

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
                "llama_model_load: using device Vulkan0 (NVIDIA GeForce RTX 2070 SUPER)\n"
                "Model::load: aux backend = CPU\n"
                "Model::load: local decoder backend = Vulkan0\n",
                "GPU: NVIDIA GeForce RTX 2070 SUPER (Vulkan0), 37/37 GPU layers; "
                "audio frame model: Vulkan0; input embeddings/codec: CPU",
            ),
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
        request = SynthesisRequest("Narrator", "Limit.", seed=0)
        result = backend.render(request).collect()
        self.assertEqual(result.completion, SynthesisCompletion.LIMITED)
        first = self.native_log.snapshot()[-1]["message"]
        result = backend.render(request).collect()
        self.assertEqual(result.diagnostics.cache_source, "fresh-generation")
        second = self.native_log.snapshot()[-1]["message"]
        for field in ("request_key", "reference_key"):
            self.assertEqual(
                first.split(field + "=")[1].split(";")[0],
                second.split(field + "=")[1].split(";")[0],
            )
        for expected in (
            "outcome=limited",
            "seed=1",
            "frame_limit=38",
            "audio_frames=38",
            "max_audio_s=3.0",
            "reference_mode=registered",
        ):
            self.assertIn(expected, second)

    def test_native_request_keys_distinguish_inputs_and_are_instance_scoped(self):
        backend = self.backend()

        def fields():
            return dict(
                part.split("=", 1)
                for part in self.native_log.snapshot()[-1]["message"]
                .removeprefix("MOSS native: ")
                .split("; ")
            )

        with patch("vntts.moss_cpp_backend.secrets.randbits", return_value=123):
            backend.render(SynthesisRequest("Narrator", "First phrase.")).collect()
        first = fields()
        self.assertEqual(first["seed"], "123")
        backend.render(
            SynthesisRequest("Narrator", "Second phrase.", seed=123)
        ).collect()
        second = fields()
        self.assertNotEqual(first["request_key"], second["request_key"])
        self.assertEqual(first["reference_key"], second["reference_key"])
        self.assertRegex(first["request_key"], r"^[0-9a-f]{24}$")
        backend.render(
            SynthesisRequest("Narrator", "Second phrase.", seed=124)
        ).collect()
        changed_seed = fields()
        self.assertNotEqual(second["request_key"], changed_seed["request_key"])
        self.assertEqual(second["reference_key"], changed_seed["reference_key"])
        sf.write(self.reference, np.full(4800, 0.2), 48000)
        backend.render(
            SynthesisRequest("Narrator", "New reference.", seed=124)
        ).collect()
        self.assertNotEqual(changed_seed["reference_key"], fields()["reference_key"])
        self.assertNotEqual(
            backend._diagnostic_key("same"), self.backend()._diagnostic_key("same")
        )

    def test_random_seed_never_uses_native_random_sentinel(self):
        backend = self.backend()
        with patch("vntts.moss_cpp_backend.secrets.randbits", return_value=0):
            backend.render(SynthesisRequest("Narrator", "Random seed.")).collect()
        body = json.loads((self.root / "request.json").read_text())
        self.assertEqual(body["sampling"]["seed"], 1)

    def test_native_sampling_contract_invalidates_persistent_synthesis_cache(self):
        request = SynthesisRequest("Narrator", "Fixed preview.", seed=0)
        with patch(
            "vntts.moss_cpp_backend.NATIVE_GENERATION_CONTRACT", "nonzero-seed-v1"
        ):
            previous = self.backend()
            previous.render(request).collect()
            previous.shutdown()
        current = self.backend()
        result = current.render(request).collect()
        self.assertEqual(result.diagnostics.cache_source, "fresh-generation")
        current.audio_cache.clear()
        repeated = current.render(request).collect()
        self.assertEqual(repeated.diagnostics.cache_source, "persistent-cache")

    def test_resource_probe_failure_cannot_fail_synthesis(self):
        self.resource_probe.return_value.finish.side_effect = RuntimeError("private")
        result = self.backend().render(SynthesisRequest("Narrator", "Hello.")).collect()
        self.assertEqual(result.completion, SynthesisCompletion.COMPLETE)
        event = self.native_log.report()["events"][-1]["native"]
        self.assertEqual(event["resources"], {"status": "probe-failed"})
        self.assertNotIn("private", str(self.native_log.report()))

    def test_preview_retry_export_correlates_native_attempts_and_quality(self):
        from tests.test_pregeneration_audition import ambiguous_fixture
        from vntts.pregeneration_audition import (
            VoiceAuditionIncomplete,
            VoiceAuditionPreviewService,
        )

        (self.root / "audition").touch()
        plan, group, _manifest = ambiguous_fixture(self.root)

        def factory(_name, registry, _cache, **_options):
            backend = self.backend()
            backend.registry = registry
            return backend

        service = VoiceAuditionPreviewService(
            self.root / "previews", backend_factory=factory
        )
        self.addCleanup(service.close)
        source = group.candidates[0].source_id
        with self.assertRaises(VoiceAuditionIncomplete):
            service.generate(plan, group, source)
        accepted = service.generate(plan, group, source)
        replay = service.generate(plan, group, source)
        self.assertEqual(accepted.seed, 2)
        self.assertTrue(replay.reused)
        path = SupportBundleBuilder(
            AppSettings(), RuntimeSupportLog(), dependency_probe=lambda: {}
        ).build(self.root / "retry-support.zip")
        with zipfile.ZipFile(path) as archive:
            report = json.loads(archive.read("native-speech.json"))
        native = [entry["native"] for entry in report["events"]]
        fresh = [row for row in native if row["operation"] == "fresh-generation"]
        final = [row for row in native if row["operation"] == "preview-outcome"]
        self.assertEqual([row["seed"] for row in fresh], [1, 2])
        self.assertEqual(
            [row["outcome"] for row in final], ["limited", "success", "success"]
        )
        self.assertEqual(len({row["logical_key"] for row in final}), 1)
        self.assertEqual(
            [row["attempt_id"] for row in fresh],
            [row["attempt_id"] for row in final[:2]],
        )
        self.assertEqual(report["active_requests"], [])
        self.assertTrue(any(row["operation"] == "preview-quality" for row in native))
        self.assertNotIn(group.sample_text, json.dumps(report))
        self.assertNotIn(str(self.root), json.dumps(report))

    def test_native_timings_survive_shutdown_and_export_without_private_inputs(self):
        (self.root / "phase-timings").touch()
        backend = self.backend()
        first = SynthesisRequest("Narrator", "Private phrase not for export.", seed=7)
        backend.render(first).collect()
        message = self.native_log.snapshot()[-1]["message"]
        request_key = message.split("request_key=")[1].split(";")[0]
        for expected in (
            "outcome=complete",
            "reference=encoded",
            "reference_encoding_s=0.4",
            "prefill_s=0.05",
            "gen_s=1.25",
            "decode_s=0.125",
            "gen_backbone_s=0.5",
            "gen_frame_decoder_s=0.6",
            "gen_input_embedding_s=0.02",
        ):
            self.assertIn(expected, message)
        event = self.native_log.report()["events"][-1]["native"]
        for field in (
            "reference_prepare_s",
            "http_round_trip_s",
            "response_pcm_decode_s",
        ):
            self.assertIsInstance(event[field], float)
            self.assertGreaterEqual(event[field], 0)
            self.assertLessEqual(event[field], event["request_s"] + 0.001)
        (self.root / "phase-timings").unlink()
        backend.render(
            SynthesisRequest("Narrator", "Another private phrase.")
        ).collect()
        event = self.native_log.report()["events"][-1]["native"]
        for field in ("gen_backbone_s", "gen_frame_decoder_s", "gen_input_embedding_s"):
            self.assertIsNone(event[field])
        self.assertIn(
            "reference=unavailable", self.native_log.snapshot()[-1]["message"]
        )
        backend.render(first).collect()
        self.assertIn("operation=cached-wav", self.native_log.snapshot()[-1]["message"])
        self.assertIn("gen_s=unavailable", self.native_log.snapshot()[-1]["message"])
        self.assertNotIn(
            "http_round_trip_s", self.native_log.report()["events"][-1]["native"]
        )
        backend.audio_cache.clear()
        backend.render(first).collect()
        self.assertIn(
            "cache=persistent-cache", self.native_log.snapshot()[-1]["message"]
        )
        directory = Path(backend.server_directory.name)
        backend.shutdown()
        self.assertFalse(directory.exists())
        output = SupportBundleBuilder(
            AppSettings(),
            RuntimeSupportLog(),
            dependency_probe=lambda: {},
        ).build(self.root / "support.zip")
        with zipfile.ZipFile(output) as archive:
            report = archive.read("native-speech.json").decode()
        self.assertIn("reference=unavailable", report)
        self.assertIn("operation=server-start", report)
        self.assertIn("request_key=" + request_key, report)
        self.assertIn("seed=7", report)
        self.assertIn('"native_rss_peak_bytes": 1024', report)
        self.assertIn('"reference_sample_rate": 48000', report)
        self.assertNotIn(backend._diagnostic_salt.hex(), report)
        self.assertIn('"gen_backbone_s": 0.5', report)
        self.assertIn('"gen_frame_decoder_s": 0.6', report)
        self.assertIn('"gen_input_embedding_s": 0.02', report)
        for field in (
            "reference_prepare_s",
            "http_round_trip_s",
            "response_pcm_decode_s",
        ):
            self.assertIn(f'"{field}":', report)
        self.assertNotIn(first.text, report)
        self.assertNotIn(str(self.reference), report)
        self.assertNotIn(str(directory), report)
        for index in range(25):
            self.native_log.add("test", str(index))
        self.assertEqual(len(self.native_log.snapshot()), 20)

    def test_missing_failed_and_cancelled_timings_do_not_reuse_previous_request(self):
        backend = self.backend()
        backend.render(SynthesisRequest("Narrator", "First.")).collect()
        (self.root / "missing-timings").touch()
        backend.render(SynthesisRequest("Narrator", "Without timings.")).collect()
        message = self.native_log.snapshot()[-1]["message"]
        self.assertIn("gen_s=unavailable", message)
        self.assertIn("prefill_s=unavailable", message)
        with self.assertRaises(TTSSynthesisError):
            backend.render(SynthesisRequest("Narrator", "Fail.")).collect()
        message = self.native_log.snapshot()[-1]["message"]
        self.assertIn("outcome=failed", message)
        self.assertIn("gen_s=unavailable", message)
        self.assertIn("audio_frames=unavailable", message)
        event = self.native_log.report()["events"][-1]["native"]
        self.assertIsInstance(event["reference_prepare_s"], float)
        self.assertIsInstance(event["http_round_trip_s"], float)
        self.assertIsNone(event["response_pcm_decode_s"])
        self.assertIsNone(backend.server_directory)

    def test_native_timing_parser_bounds_log_and_rejects_invalid_measurements(self):
        log = self.root / "native.log"
        log.write_text(
            "private text/path must never be exported\n"
            "[generate] encoded reference: 2 frames (0.16s) in 0.30s\n"
            "[generate] generated 2 steps in 1.50s\n"
            "[generate] codec decode produced 9600 samples (0.10s audio) in 0.20s\n"
        )
        report = _native_stage_timings(log, 0, {}, 600)
        self.assertEqual(report["reference_encoding_s"], 0.3)
        self.assertEqual(report["gen_s"], 1.5)
        self.assertEqual(report["decode_s"], 0.2)
        self.assertNotIn("private", str(report))
        self.assertIsNone(
            _native_stage_timings(log, log.stat().st_size, {}, 600)["gen_s"]
        )
        for invalid in ("nan", "inf", "-1", "bad", "1e308"):
            self.assertIsNone(
                _native_stage_timings(
                    log, 0, {"x-moss-generate-seconds": invalid}, 600
                )["gen_s"]
            )
        log.write_text(
            "x" * (65 * 1024) + "\nreference [S1]: 2 frames (cached codes)\n"
        )
        self.assertEqual(
            _native_stage_timings(log, 0, {}, 600)["reference"], "unavailable"
        )

    def test_optional_native_phases_reject_invalid_values_and_preserve_zero(self):
        for field, header in (
            ("gen_backbone_s", "x-moss-backbone-seconds"),
            ("gen_frame_decoder_s", "x-moss-frame-decoder-seconds"),
            ("gen_input_embedding_s", "x-moss-input-embedding-seconds"),
        ):
            with self.subTest(field=field):
                self.assertIsNone(_native_stage_timings(None, 0, {}, 600)[field])
                self.assertEqual(
                    _native_stage_timings(None, 0, {header: "0"}, 600)[field], 0.0
                )
                for invalid in ("nan", "inf", "-1", "bad", "601", "1e308"):
                    self.assertIsNone(
                        _native_stage_timings(None, 0, {header: invalid}, 600)[field]
                    )

    def test_native_request_time_excludes_consumer_playback_delay(self):
        backend = self.backend()
        with patch("vntts.moss_cpp_backend.monotonic", return_value=10.0) as clock:
            stream = backend.render(SynthesisRequest("Narrator", "Timing test."))
            next(stream)
            clock.return_value = 100.0
            stream.collect()
        self.assertIn("request_s=0.0", self.native_log.snapshot()[-1]["message"])
        event = self.native_log.report()["events"][-1]["native"]
        for field in (
            "reference_prepare_s",
            "http_round_trip_s",
            "response_pcm_decode_s",
        ):
            self.assertEqual(event[field], 0.0)

    def test_stopped_server_before_request_is_reported_without_dereferencing_it(self):
        backend = self.backend()
        with patch.object(
            backend,
            "_start_server",
            side_effect=lambda _cancelled: backend._stop_server(),
        ):
            with self.assertRaisesRegex(TTSSynthesisError, "stopped before generation"):
                backend.render(SynthesisRequest("Narrator", "Stopped.")).collect()
        self.assertIn("outcome=failed", self.native_log.snapshot()[-1]["message"])
        event = self.native_log.report()["events"][-1]["native"]
        for field in (
            "reference_prepare_s",
            "http_round_trip_s",
            "response_pcm_decode_s",
        ):
            self.assertIsNone(event[field])

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

    def test_aux_cpu_worker_ladder_is_conservative(self):
        self.assertEqual(
            [_aux_cpu_workers(value) for value in (0, 1, 3, 4, 7, 8, 15, 16)],
            [1, 1, 1, 2, 2, 4, 4, 8],
        )

    def _managed_backend(self):
        return MossCppVoiceRouterBackend(
            CharacterVoiceRegistry(),
            narrator_reference=self.reference,
            persistent_audio_cache_directory=self.root / "cache",
            prompt_cache_directory=self.root / "prompts",
            startup_timeout=10,
        )

    def test_managed_runtime_adapts_workers_and_confirms_structured_placement(self):
        (self.root / "adaptive-runtime").touch()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.moss_cpp_installation.ensure_moss_cpp"),
            patch(
                "vntts.moss_cpp_backend.moss_cpp_paths",
                return_value=(
                    Path(sys.executable),
                    self.model,
                    self.model.with_suffix(".extras.gguf"),
                ),
            ),
            patch("vntts.moss_cpp_backend.os.cpu_count", return_value=8),
        ):
            backend = self._managed_backend()
        self.addCleanup(backend.shutdown)
        command = self.commands[-1]
        self.assertIn("--local-gpu", command)
        self.assertEqual(command[command.index("--aux-cpu-threads") + 1], "4")
        self.assertIn("Vulkan GPU", backend.runtime_status)
        self.assertIn("auxiliary CPU workers: 4", backend.runtime_status)
        self.assertIn("local_gpu=1:aux_cpu_threads=4", backend.model_name)
        backend._stop_server()
        with patch("vntts.moss_cpp_backend.os.cpu_count", return_value=16):
            backend._start_server(lambda: False)
        self.assertEqual(
            self.commands[-1][self.commands[-1].index("--aux-cpu-threads") + 1], "8"
        )
        self.assertIn("aux_cpu_threads=8", backend.model_name)

    def test_managed_runtime_skips_local_gpu_when_vulkan_is_unavailable(self):
        (self.root / "adaptive-runtime").touch()
        (self.root / "no-vulkan").touch()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.moss_cpp_installation.ensure_moss_cpp"),
            patch(
                "vntts.moss_cpp_backend.moss_cpp_paths",
                return_value=(
                    Path(sys.executable),
                    self.model,
                    self.model.with_suffix(".extras.gguf"),
                ),
            ),
        ):
            backend = self._managed_backend()
        self.addCleanup(backend.shutdown)
        self.assertNotIn("--local-gpu", self.commands[-1])
        self.assertFalse(self.native_log.report()["latest_runtime"]["vulkan_available"])

    def test_managed_local_gpu_failure_restarts_once_without_local_gpu(self):
        (self.root / "adaptive-runtime").touch()
        (self.root / "fail-local-gpu").touch()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.moss_cpp_installation.ensure_moss_cpp"),
            patch(
                "vntts.moss_cpp_backend.moss_cpp_paths",
                return_value=(
                    Path(sys.executable),
                    self.model,
                    self.model.with_suffix(".extras.gguf"),
                ),
            ),
            patch("vntts.moss_cpp_backend.os.cpu_count", return_value=4),
        ):
            backend = self._managed_backend()
        self.addCleanup(backend.shutdown)
        self.assertEqual(len(self.children), 2)
        self.assertIn("--local-gpu", self.commands[0])
        self.assertNotIn("--local-gpu", self.commands[1])
        self.assertEqual(
            self.commands[1][self.commands[1].index("--n-gpu-layers") + 1], "-1"
        )
        self.assertIn("fallback: local gpu", backend.runtime_status)
        self.assertIn("local_gpu=0:aux_cpu_threads=2", backend.model_name)
        runtime = self.native_log.report()["latest_runtime"]
        self.assertEqual(runtime["fallback_reason"], "local_gpu")
        self.assertEqual(runtime["aux_cpu_threads"], 2)
        (self.root / "fail-local-gpu").unlink()
        backend._stop_server()
        backend._start_server(lambda: False)
        self.assertIn("--local-gpu", self.commands[-1])
        self.assertNotIn("fallback:", backend.runtime_status)

    def test_managed_vulkan_failure_restarts_once_on_cpu(self):
        (self.root / "adaptive-runtime").touch()
        (self.root / "fail-vulkan").touch()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.moss_cpp_installation.ensure_moss_cpp"),
            patch(
                "vntts.moss_cpp_backend.moss_cpp_paths",
                return_value=(
                    Path(sys.executable),
                    self.model,
                    self.model.with_suffix(".extras.gguf"),
                ),
            ),
        ):
            backend = self._managed_backend()
        self.addCleanup(backend.shutdown)
        self.assertEqual(len(self.children), 2)
        self.assertEqual(
            self.commands[1][self.commands[1].index("--n-gpu-layers") + 1], "0"
        )
        self.assertNotIn("--local-gpu", self.commands[1])
        self.assertIn("fallback: vulkan allocation", backend.runtime_status)

    def test_advertised_invalid_managed_capabilities_are_rejected(self):
        (self.root / "adaptive-runtime").touch()
        (self.root / "adaptive-invalid").touch()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.moss_cpp_installation.ensure_moss_cpp"),
            patch(
                "vntts.moss_cpp_backend.moss_cpp_paths",
                return_value=(
                    Path(sys.executable),
                    self.model,
                    self.model.with_suffix(".extras.gguf"),
                ),
            ),
            self.assertRaisesRegex(TTSConfigurationError, "capabilities"),
        ):
            self._managed_backend()
        self.assertEqual(self.children, [])

    def test_managed_runtime_does_not_retry_unrelated_startup_failure(self):
        (self.root / "adaptive-runtime").touch()
        (self.root / "fail-unknown").touch()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.moss_cpp_installation.ensure_moss_cpp"),
            patch(
                "vntts.moss_cpp_backend.moss_cpp_paths",
                return_value=(
                    Path(sys.executable),
                    self.model,
                    self.model.with_suffix(".extras.gguf"),
                ),
            ),
            self.assertRaisesRegex(
                TTSConfigurationError, "fallback was not applicable"
            ),
        ):
            self._managed_backend()
        self.assertEqual(len(self.children), 1)

    def test_explicit_runtime_does_not_negotiate_managed_controls(self):
        (self.root / "adaptive-runtime").touch()
        with (
            patch("vntts.moss_cpp_installation.ensure_moss_cpp"),
            patch(
                "vntts.moss_cpp_backend.moss_cpp_paths",
                return_value=(
                    Path(sys.executable),
                    self.model,
                    self.model.with_suffix(".extras.gguf"),
                ),
            ),
        ):
            backend = self._managed_backend()
        self.addCleanup(backend.shutdown)
        self.assertNotIn("--local-gpu", self.commands[-1])
        self.assertNotIn("--aux-cpu-threads", self.commands[-1])

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
            self.assertIn(
                "outcome=cancelled", self.native_log.snapshot()[-1]["message"]
            )
            self.assertIn(
                "gen_s=unavailable", self.native_log.snapshot()[-1]["message"]
            )
            event = self.native_log.report()["events"][-1]["native"]
            self.assertIsInstance(event["reference_prepare_s"], float)
            self.assertIsNone(event["http_round_trip_s"])
            self.assertIsNone(event["response_pcm_decode_s"])
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

    def test_missing_native_files_identify_paths_instead_of_claiming_mlx(self):
        missing = self.root / "missing-server.exe"
        with patch.dict(os.environ, {"VNTTS_MOSS_CPP_EXECUTABLE": str(missing)}):
            with self.assertRaises(TTSConfigurationError) as caught:
                moss_cpp_paths("/old/models/moss-mlx-int8")
        message = str(caught.exception)
        self.assertIn(f"Native server executable is missing: {missing}", message)
        self.assertNotIn("Model GGUF is missing", message)
        self.assertNotIn("MLX", message)

        self.model.unlink()
        self.model.with_suffix(".extras.gguf").unlink()
        with self.assertRaises(TTSConfigurationError) as caught:
            moss_cpp_paths()
        message = str(caught.exception)
        self.assertIn(f"Model GGUF is missing: {self.model}", message)
        self.assertIn(
            f"Audio sidecar is missing: {self.model.with_suffix('.extras.gguf')}",
            message,
        )
        self.assertIn("--model PATH", message)

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

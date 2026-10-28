import hashlib
import os
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from vntts.audio_output import (
    prepared_output_stream,
    resolve_audio_output,
    write_pcm_chunks,
)
from vntts.rust_audio import (
    AudioRuntimeUnavailable,
    RustAudioWorker,
    check_audio_runtime,
    portaudio_library,
    worker_executable,
)
from vntts.rust_output import RustAudioModule
from vntts.system_audio import SystemAudioOutput

# A real pipe peer exercises framing, child death and Stop independently of PortAudio.
PEER = r"""
import hashlib, json, sys, time
mode, expected, marker = sys.argv[1:]
def emit(**value):
    print(json.dumps(value), flush=True)
emit(event="ready", protocol=2)
if mode == "blocked-upload":
    time.sleep(30)
    sys.exit()
clip = None
for line in sys.stdin.buffer:
    command = json.loads(line)
    op = command["op"]
    clip_id = command.get("id")
    if op == "load":
        size = command["frames"] * command["channels"] * 4
        pcm = sys.stdin.buffer.read(size)
        if len(pcm) != size or hashlib.sha256(pcm).hexdigest() != expected:
            emit(event="error", id=clip_id, error="PCM order changed")
            continue
        clip = command
        emit(event="loaded", id=clip_id)
    elif op == "start":
        if mode == "exit":
            sys.exit(3)
        emit(event="started", id=clip_id)
        emit(event="first_callback", id=clip_id, frames_written=0 if mode == "zero-first" else 1)
        if mode in ("normal", "bad-count", "stale-id", "zero-first", "bad-outcome", "bad-underflow") or (mode == "pause-first" and clip_id > 1):
            emit(event="finished", id=clip_id + (mode == "stale-id"), outcome="invalid" if mode == "bad-outcome" else "complete",
                 frames_played=clip["frames"] - (mode == "bad-count"),
                 underflow_count="0" if mode == "bad-underflow" else 0, callback_count=1,
                 sample_rate=clip["sample_rate"], channels=clip["channels"])
    elif op == "stop":
        if mode == "ignore-stop":
            continue
        if marker:
            open(marker, "w").write("stopped")
        emit(event="finished", id=clip_id, outcome="interrupted", frames_played=1, underflow_count=0, callback_count=1,
             sample_rate=clip["sample_rate"], channels=clip["channels"])
"""


class AudioRuntimeAvailabilityTest(unittest.TestCase):
    def test_exception_keeps_technical_message_and_supports_summary_override(self):
        error = AudioRuntimeUnavailable("technical detail", summary="Short summary")
        self.assertEqual(str(error), "technical detail")
        self.assertEqual(error.summary, "Short summary")
        self.assertEqual(
            AudioRuntimeUnavailable("technical detail").summary,
            "Audio playback is unavailable.",
        )

    def test_missing_default_worker_has_install_summary_and_full_guidance(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.rust_audio.portaudio_library", return_value="library"),
            patch("vntts.rust_audio.Path.is_file", return_value=False),
        ):
            with self.assertRaises(AudioRuntimeUnavailable) as raised:
                check_audio_runtime()
        self.assertEqual(
            raised.exception.summary, "The audio component is not installed."
        )
        self.assertIn("cargo build --release", str(raised.exception))

    def test_missing_worker_is_actionable_and_can_be_installed_then_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "worker"
            with (
                patch.dict(os.environ, {"VNTTS_AUDIO_WORKER": str(worker)}),
                patch("vntts.rust_audio.portaudio_library", return_value="library"),
                patch("vntts.rust_audio.subprocess.Popen") as launch,
            ):
                with self.assertRaises(AudioRuntimeUnavailable) as raised:
                    check_audio_runtime()
                self.assertIn("Correct VNTTS_AUDIO_WORKER", str(raised.exception))
                self.assertEqual(
                    raised.exception.summary,
                    "The configured audio worker was not found.",
                )
                worker.write_text("fixture")
                worker.chmod(0o700)
                check_audio_runtime()
                launch.assert_not_called()

    def test_startup_timeout_has_message_and_terminates_child(self):
        processes = []
        launch = subprocess.Popen

        def tracked_launch(*args, **kwargs):
            process = launch(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch("vntts.rust_audio.subprocess.Popen", side_effect=tracked_launch),
            patch.object(RustAudioWorker, "receive", side_effect=queue.Empty),
            self.assertRaisesRegex(RuntimeError, "startup timed out.*cargo build"),
        ):
            RustAudioWorker([sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    @unittest.skipIf(sys.platform == "win32", "POSIX executable permissions")
    def test_non_executable_worker_reports_permission_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "worker"
            worker.write_text("fixture")
            worker.chmod(0o600)
            with patch.dict(os.environ, {"VNTTS_AUDIO_WORKER": str(worker)}):
                with self.assertRaises(AudioRuntimeUnavailable) as raised:
                    check_audio_runtime()
                self.assertIn("not executable", str(raised.exception))
                self.assertEqual(
                    raised.exception.summary,
                    "The audio worker cannot be executed; check its permissions.",
                )

    def test_explicit_missing_portaudio_reports_override_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {"VNTTS_PORTAUDIO_LIBRARY": str(Path(directory) / "missing")},
            ):
                with self.assertRaises(AudioRuntimeUnavailable) as raised:
                    portaudio_library()
                self.assertIn("VNTTS_PORTAUDIO_LIBRARY", str(raised.exception))
                self.assertEqual(
                    raised.exception.summary,
                    "The configured PortAudio library was not found.",
                )

    def test_missing_portaudio_has_install_summary_and_full_guidance(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("vntts.rust_audio.ctypes.util.find_library", return_value=None),
            patch("vntts.rust_audio.importlib.util.find_spec", return_value=None),
        ):
            with self.assertRaises(AudioRuntimeUnavailable) as raised:
                portaudio_library()
        self.assertEqual(raised.exception.summary, "PortAudio is not installed.")
        self.assertIn("libportaudio2", str(raised.exception))


class RustAudioProtocolTest(unittest.TestCase):
    def setUp(self):
        self.pcm = np.arange(80, dtype=np.float32).reshape(40, 2) / 100
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.close()
            self.assertIsNotNone(worker.process.poll())

    def worker(self, mode="normal", marker="", pcm=None):
        data = self.pcm if pcm is None else pcm
        command = [
            sys.executable,
            "-u",
            "-c",
            textwrap.dedent(PEER),
            mode,
            hashlib.sha256(data.astype("<f4").tobytes()).hexdigest(),
            marker,
        ]
        worker = RustAudioWorker(command)
        self.workers.append(worker)
        return worker

    def play(self, worker, cancelled=lambda: False, stop_requested=None):
        first = Mock()
        with patch("vntts.rust_audio.record_audio_lifecycle") as record:
            result = worker.play_buffer(
                self.pcm, 24000, 2, "low", cancelled, first, stop_requested
            )
        return result, first, record

    def test_whole_buffer_preserves_sample_order_and_reuses_one_process(self):
        worker = self.worker()
        for clip_id in (1, 2):
            result, first, record = self.play(worker)
            self.assertEqual(result, (True, False))
            first.assert_called_once_with()
            self.assertEqual(worker.next_id, clip_id)
            self.assertEqual(record.call_args.kwargs["worker_pid"], worker.process.pid)
        self.assertFalse(worker.closed.is_set())

    def test_zero_frame_callback_does_not_claim_audio_onset(self):
        worker = self.worker("zero-first")
        result, first, _record = self.play(worker)
        self.assertEqual(result, (True, False))
        first.assert_not_called()

    def test_invalid_pcm_and_cancel_before_start_never_submit_a_clip(self):
        worker = self.worker()
        self.assertEqual(self.play(worker, cancelled=lambda: True)[0], (False, False))
        bad = self.pcm.copy()
        bad[0, 0] = np.nan
        with self.assertRaises(ValueError):
            worker.play_buffer(bad, 24000, 2, "low", lambda: False, None, None)
        self.assertEqual(worker.next_id, 0)

    def test_child_exit_stale_id_and_truncated_completion_fail_closed(self):
        for mode in ("exit", "stale-id", "bad-count", "bad-outcome", "bad-underflow"):
            with self.subTest(mode=mode):
                worker = self.worker(mode)
                with self.assertRaises(RuntimeError):
                    self.play(worker)
                self.assertTrue(worker.closed.is_set())
                self.assertIsNotNone(worker.process.poll())

    def test_stop_reaches_worker_even_while_owner_guard_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "stopped"
            worker = self.worker("wait", str(marker))
            stop = threading.Event()
            in_guard = threading.Event()
            release = threading.Event()
            calls = 0
            result = []

            def guard():
                nonlocal calls
                calls += 1
                if calls > 2:
                    in_guard.set()
                    release.wait(2)
                return stop.is_set()

            def play():
                result.append(self.play(worker, guard, stop)[0])

            owner = threading.Thread(target=play)
            owner.start()
            self.assertTrue(in_guard.wait(2))
            try:
                stop.set()
                deadline = time.monotonic() + 1
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "Stop depended on the blocked guard")
            finally:
                release.set()
                owner.join(3)
            self.assertFalse(owner.is_alive())
            self.assertEqual(result, [(False, False)])

    def test_reading_pause_stops_prepared_rust_pcm_and_resume_replays_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from queue import Queue

        from vntts.controller import AppController
        from vntts.generated_audio import (
            GeneratedAudioFallbackBackend,
            GeneratedAudioRoute,
            PreparedGeneratedAudio,
        )
        from vntts.live import LiveDialogReader, SpeechChunk
        from vntts.settings import AppSettings

        pcm = np.arange(40, dtype=np.float32).reshape(-1, 1) / 100
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "stopped"
            worker = self.worker("pause-first", str(marker), pcm=pcm)
            module = RustAudioModule()
            module.idle.append(worker)
            module.query_devices = Mock(return_value={"default_samplerate": 24000})
            live = Mock()
            live.stop.return_value = False
            backend = GeneratedAudioFallbackBackend(
                live, None, Mock(), audio_output=module
            )
            controller = AppController(AppSettings(), tts_factory=Mock())
            controller.tts = live
            controller.speech_backend = backend
            route = GeneratedAudioRoute(
                PreparedGeneratedAudio("game:1", "hash", pcm.reshape(-1), 24000),
                Mock(effective_source="generated"),
            )
            started = threading.Event()
            finished = Queue()
            errors = []

            def record(operation, **_details):
                if operation == "first-pcm":
                    started.set()

            def play(chunk, prepared):
                finished.put(controller._play_live_chunk(chunk, prepared))

            with (
                ThreadPoolExecutor(max_workers=1) as speech,
                ThreadPoolExecutor(max_workers=1) as playback,
                patch(
                    "vntts.audio_lifecycle.record_audio_lifecycle", side_effect=record
                ),
            ):
                reader = LiveDialogReader(
                    capture_executor=Mock(),
                    ocr_executor=Mock(),
                    speech_executor=speech,
                    playback_executor=playback,
                    capture_frame=Mock(),
                    recognize_frame=Mock(),
                    prepare_chunk=lambda _chunk: route,
                    play_prepared=play,
                    interrupt_speech=controller._interrupt_speech,
                    report_error=errors.append,
                )
                controller.live_reader = reader
                reader.active_generation = 1
                try:
                    reader._schedule([SpeechChunk(1, "Ada", "Hello.", ordinal=0)])
                    self.assertTrue(started.wait(2))
                    self.assertTrue(reader.toggle_pause())
                    deadline = time.monotonic() + 1
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(
                        marker.exists(), "Pause did not reach the prepared PCM owner"
                    )
                    self.assertEqual(worker.next_id, 1)
                    self.assertTrue(reader.runtime_control_snapshot()["queued"])
                    self.assertFalse(reader.toggle_pause())
                    self.assertFalse(finished.get(timeout=2))
                    self.assertTrue(finished.get(timeout=2))
                    self.assertEqual(worker.next_id, 2)
                    self.assertIn(1, reader.sealed_generations)
                    self.assertIsNone(reader.auto_advance_blocked_generation)
                    self.assertFalse(reader.pause_interrupted_chunk_ids)
                    self.assertEqual(errors, [])
                finally:
                    reader.emergency_stop()
                    reader.release_waiters()
                    module._terminate()

    def test_stop_watchdog_kills_unresponsive_worker_while_guard_remains_blocked(self):
        worker = self.worker("ignore-stop")
        stop = threading.Event()
        in_guard = threading.Event()
        release = threading.Event()
        calls = 0
        failures = []

        def guard():
            nonlocal calls
            calls += 1
            if calls > 2:
                in_guard.set()
                release.wait(6)
            return stop.is_set()

        def play():
            try:
                self.play(worker, guard, stop)
            except RuntimeError as error:
                failures.append(error)

        owner = threading.Thread(target=play)
        owner.start()
        self.assertTrue(in_guard.wait(2))
        try:
            stop.set()
            deadline = time.monotonic() + 4.5
            while worker.process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNotNone(worker.process.poll())
        finally:
            release.set()
            owner.join(3)
        self.assertFalse(owner.is_alive())
        self.assertTrue(failures)

    def test_stop_interrupts_blocked_payload_upload(self):
        pcm = np.zeros((300000, 2), dtype=np.float32)
        worker = self.worker("blocked-upload", pcm=pcm)
        stop = threading.Event()
        timer = threading.Timer(0.05, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError, "upload interrupted"):
                worker.play_buffer(pcm, 24000, 2, "low", lambda: False, None, stop)
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(worker.process.poll())

    def test_device_change_aborts_and_is_reported_as_failure(self):
        worker = self.worker("wait")
        timer = threading.Timer(0.05, worker.device_changed.set)
        timer.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "device changed"):
                self.play(worker)
        finally:
            timer.join()
        self.assertTrue(worker.closed.is_set())


class RustPreparedSelectionTest(unittest.TestCase):
    def test_rust_selection_reaches_full_buffer_owner_without_python_stream(self):
        native_module = RustAudioModule()
        native_module.check_output_settings = Mock()
        native_module.query_devices = Mock(return_value={"default_samplerate": 24000})
        output = resolve_audio_output(SystemAudioOutput(native_module))
        # Actual default outputs are logged; an explicitly injected owner stays injected.
        from vntts.audio_output import _LoggedAudioOutput

        output = _LoggedAudioOutput(output)
        worker = Mock()
        worker.play_buffer.return_value = (True, False)
        stop = threading.Event()
        pcm = np.zeros(100, dtype=np.float32)
        with (
            patch.object(native_module, "acquire", return_value=worker),
            patch.object(native_module, "release"),
            patch("vntts.audio_output.record_audio_lifecycle"),
        ):
            with prepared_output_stream(
                output, samplerate=24000, channels=1, dtype="float32", latency="low"
            ) as stream:
                result = write_pcm_chunks(
                    stream, pcm, 24000, lambda: False, stop_requested=stop
                )
        self.assertEqual(result, (True, False))
        self.assertIs(worker.play_buffer.call_args.args[-1], stop)
        np.testing.assert_array_equal(
            worker.play_buffer.call_args.args[0], pcm[:, None]
        )

    def test_missing_requested_binary_does_not_fall_back(self):
        with patch.dict(
            os.environ, {"VNTTS_AUDIO_WORKER": "/missing/vntts-audio-worker"}
        ):
            with self.assertRaisesRegex(RuntimeError, "Correct VNTTS_AUDIO_WORKER"):
                worker_executable()

    def test_injected_streams_keep_their_output_factory(self):
        native = Mock()
        output = SystemAudioOutput(native)
        output.OutputStream(
            samplerate=24000, channels=1, dtype="float32", latency="low"
        )
        native.OutputStream.assert_called_once()

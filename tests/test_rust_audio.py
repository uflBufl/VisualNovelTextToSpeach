import hashlib
import os
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
from vntts.rust_audio import RustAudioWorker, worker_executable
from vntts.system_audio import SystemAudioOutput

# A real pipe peer exercises framing, child death and Stop independently of PortAudio.
PEER = r"""
import hashlib, json, sys, time
mode, expected, marker = sys.argv[1:]
def emit(**value):
    print(json.dumps(value), flush=True)
emit(event="ready", protocol=1)
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
        if mode in ("normal", "bad-count", "stale-id", "zero-first", "bad-outcome", "bad-underflow"):
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
        native_module = Mock()
        native_module.query_devices.return_value = {"default_samplerate": 24000}
        output = resolve_audio_output(SystemAudioOutput(native_module))
        # Actual default outputs are logged; an explicitly injected owner stays injected.
        from vntts.audio_output import _LoggedAudioOutput

        output = _LoggedAudioOutput(output)
        worker = Mock()
        worker.play_buffer.return_value = (True, False)
        stop = threading.Event()
        pcm = np.zeros(100, dtype=np.float32)
        with (
            patch.dict(os.environ, {"VNTTS_PREPARED_AUDIO_BACKEND": "rust"}),
            patch("vntts.rust_audio.get_rust_audio_worker", return_value=worker),
            patch("vntts.audio_output.record_audio_lifecycle"),
        ):
            with prepared_output_stream(
                output, samplerate=24000, channels=1, dtype="float32", latency="low"
            ) as stream:
                result = write_pcm_chunks(
                    stream, pcm, 24000, lambda: False, stop_requested=stop
                )
        self.assertEqual(result, (True, False))
        native_module.OutputStream.assert_not_called()
        self.assertIs(worker.play_buffer.call_args.args[-1], stop)
        np.testing.assert_array_equal(
            worker.play_buffer.call_args.args[0], pcm[:, None]
        )

    def test_missing_requested_binary_does_not_fall_back(self):
        with patch.dict(
            os.environ, {"VNTTS_AUDIO_WORKER": "/missing/vntts-audio-worker"}
        ):
            with self.assertRaisesRegex(RuntimeError, "cargo build"):
                worker_executable()

    def test_live_streams_keep_existing_output_factory(self):
        native = Mock()
        output = SystemAudioOutput(native)
        with patch.dict(os.environ, {"VNTTS_PREPARED_AUDIO_BACKEND": "rust"}):
            output.OutputStream(
                samplerate=24000, channels=1, dtype="float32", latency="low"
            )
        native.OutputStream.assert_called_once()

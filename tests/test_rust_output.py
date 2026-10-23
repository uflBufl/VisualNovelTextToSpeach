import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from vntts.rust_audio import RustAudioWorker
from vntts.rust_output import RustAudioFormatError, RustAudioModule

# A real pipe peer checks transport and cancellation without Cargo or an audio device.
PEER = r"""
import hashlib, json, pathlib, sys, time
mode, expected, trace_name, drain_name = sys.argv[1:]
def emit(**event):
    print(json.dumps(event), flush=True)
def trace(command):
    with open(trace_name, "a") as output:
        output.write(json.dumps(command) + "\n")
def payload(size):
    data = bytearray()
    while len(data) < size:
        part = sys.stdin.buffer.read(size - len(data))
        if not part:
            raise EOFError("incomplete payload")
        data.extend(part)
    return data
emit(event="ready", protocol=2)
clip = None
count = 0
digest = hashlib.sha256()
for line in sys.stdin.buffer:
    command = json.loads(line)
    op = command["op"]
    clip_id = command.get("id")
    trace(command)
    if op == "query_device":
        emit(event="device", name="Fake output", default_samplerate=48000,
             max_output_channels=2, hostapi=0, host_api="Fake API")
    elif op == "check_format":
        supported = command["sample_rate"] == 48000
        emit(event="format", supported=supported, code=0 if supported else -9997,
             error=None if supported else "unsupported sample rate")
    elif op in ("open_stream", "load"):
        clip, count, digest = command, 0, hashlib.sha256()
        if op == "load":
            digest.update(payload(command["frames"] * command["channels"] * 4))
            count = command["frames"]
        if mode == "open-fail" and op == "open_stream":
            emit(event="error", id=clip_id, error="open acknowledgement rejected")
            continue
        emit(event="loaded" if op == "load" else "opened", id=clip_id)
        if mode == "blocked-write":
            time.sleep(30)
            break
    elif op == "append":
        frames = command["frames"]
        if frames > clip["capacity_frames"]:
            emit(event="error", id=clip_id, error="unbounded append")
            continue
        digest.update(payload(frames * clip["channels"] * 4))
        if count == 0:
            emit(event="started", id=clip_id)
            emit(event="first_callback", id=clip_id, frames_written=1)
        count += frames
        emit(event="appended", id=clip_id, frames=frames,
             underflow_count=0, producer_starvation_count=0)
    elif op in ("start", "end"):
        if digest.hexdigest() != expected:
            emit(event="error", id=clip_id, error="PCM bytes or order changed")
            continue
        if op == "start":
            emit(event="started", id=clip_id)
            emit(event="first_callback", id=clip_id, frames_written=1)
        else:
            emit(event="ended", id=clip_id)
            if mode == "drain":
                while not pathlib.Path(drain_name).exists():
                    time.sleep(.005)
        emit(event="finished", id=clip_id, outcome="complete", frames_played=count - (mode == "bad-count"),
             total_frames=count, underflow_count=3, producer_starvation_count=2,
             callback_count=4, sample_rate=clip["sample_rate"], channels=clip["channels"])
    elif op == "stop":
        emit(event="finished", id=clip_id, outcome="interrupted", frames_played=count,
             underflow_count=0, producer_starvation_count=0, callback_count=0,
             sample_rate=clip["sample_rate"], channels=clip["channels"])
"""


class RustOutputProtocolTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.trace = Path(self.directory.name) / "commands.jsonl"
        self.drain = Path(self.directory.name) / "drained"
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.close()
            self.assertIsNotNone(worker.process.poll())

    def module(self, pcm=None, mode="stream"):
        expected = hashlib.sha256(
            b"" if pcm is None else np.asarray(pcm, dtype="<f4").tobytes()
        ).hexdigest()

        def create():
            worker = RustAudioWorker(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    PEER,
                    mode,
                    expected,
                    str(self.trace),
                    str(self.drain),
                ]
            )
            self.workers.append(worker)
            return worker

        factory = patch("vntts.rust_output.RustAudioWorker", side_effect=create)
        factory.start()
        self.addCleanup(factory.stop)
        module = RustAudioModule()
        self.addCleanup(module._terminate)
        return module

    @staticmethod
    def stream(module, channels=2):
        return module.OutputStream(
            samplerate=24000, channels=channels, dtype="float32", latency="low"
        )

    def commands(self):
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def test_chunks_are_bounded_ordered_and_final_drain_counters_are_retained(self):
        pcm = np.arange(100001 * 2, dtype=np.float32).reshape(-1, 2) / 1000
        module = self.module(pcm)
        for expected_id in (1, 2):
            with self.stream(module) as stream:
                self.assertFalse(stream.write(pcm))
                self.assertTrue(stream.first_callback)
                self.assertFalse(stream.finished.is_set())
                self.assertEqual(stream.clip_id, expected_id)
            self.assertTrue(stream.finished.is_set())
            self.assertEqual(stream.underflow_count, 3)
            self.assertEqual(stream.producer_starvation_count, 2)
            self.assertEqual(stream.summary["frames_played"], len(pcm))
        commands = self.commands()
        self.assertEqual(
            [c["frames"] for c in commands if c["op"] == "append"],
            [48000, 48000, 4001] * 2,
        )
        self.assertEqual(
            [c["op"] for c in commands],
            ["open_stream", "append", "append", "append", "end"] * 2,
        )
        self.assertEqual(len(self.workers), 1)

    def test_end_ack_does_not_finish_until_native_drain(self):
        pcm = np.ones((10, 2), dtype=np.float32)
        module = self.module(pcm, "drain")
        stream = self.stream(module)
        stream.start()
        stream.write(pcm)
        done = threading.Event()
        failures = []

        def stop():
            try:
                stream.stop()
            except BaseException as error:
                failures.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=stop)
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while not any(c["op"] == "end" for c in self.commands()):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.005)
            self.assertFalse(
                done.wait(0.05), "EOS acknowledgement was treated as drain"
            )
            self.assertFalse(stream.finished.is_set())
        finally:
            self.drain.touch()
            thread.join(3)
            stream.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(stream.finished.is_set())
        self.assertEqual(stream.summary["outcome"], "complete")

    def test_abort_interrupts_an_upload_blocked_in_the_pipe(self):
        pcm = np.zeros((48000, 2), dtype=np.float32)
        module = self.module(pcm, "blocked-write")
        stream = self.stream(module)
        stream.start()
        worker = stream._worker()
        append_started = threading.Event()
        send = worker.send

        def observe(message, payload=b"", **options):
            if message["op"] == "append":
                append_started.set()
            send(message, payload, **options)

        failures = []

        def write():
            try:
                stream.write(pcm)
            except RuntimeError as error:
                failures.append(error)

        with patch.object(worker, "send", side_effect=observe):
            thread = threading.Thread(target=write)
            thread.start()
            try:
                self.assertTrue(append_started.wait(2))
                started = time.monotonic()
                stream.abort()
                thread.join(3)
                self.assertLess(time.monotonic() - started, 3)
                self.assertFalse(thread.is_alive())
                self.assertTrue(failures)
                self.assertIsNotNone(worker.process.poll())
            finally:
                worker.close()
                thread.join(3)
                stream.close()

    def test_prepared_buffer_uses_bulk_load_without_stream_ingress(self):
        pcm = np.linspace(-0.5, 0.5, 80, dtype=np.float32).reshape(40, 2)
        module = self.module(pcm)
        first = Mock()
        with self.stream(module) as stream:
            result = stream.play_buffer(pcm, lambda: False, first, None)
        self.assertEqual(result, (True, True))
        first.assert_called_once_with()
        self.assertEqual([c["op"] for c in self.commands()], ["load", "start"])
        self.assertFalse(self.workers[0].closed.is_set())

    def test_device_query_and_format_error_preserve_portaudio_code(self):
        module = self.module()
        self.assertEqual(module.query_devices(kind="output")["name"], "Fake output")
        self.assertEqual(module.query_hostapis(0), {"name": "Fake API"})
        module.check_output_settings(samplerate=48000, channels=2, dtype="float32")
        with self.assertRaises(RustAudioFormatError) as caught:
            module.check_output_settings(samplerate=24000, channels=2, dtype="float32")
        self.assertEqual(caught.exception.args[-1], -9997)
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(
            [c["op"] for c in self.commands()],
            ["query_device", "query_device", "check_format", "check_format"],
        )

    def test_truncated_native_completion_does_not_claim_stream_drain(self):
        pcm = np.ones((10, 2), dtype=np.float32)
        module = self.module(pcm, "bad-count")
        stream = self.stream(module)
        stream.start()
        try:
            stream.write(pcm)
            with self.assertRaises(RuntimeError):
                stream.stop()
        finally:
            stream.close()

    def test_scheduled_cancel_does_not_send_after_worker_ownership_changes(self):
        module = self.module()
        worker = module.acquire()
        worker.active_id = 1
        threads = []

        def create_thread(*args, **kwargs):
            thread = threading.Thread(*args, **kwargs)
            threads.append(thread)
            return thread

        with patch("vntts.rust_audio.Thread", side_effect=create_thread):
            with worker.send_lock:
                worker.cancel_active()
                # Completion and a new lease can overtake the scheduled Stop.
                worker.active_id = 2
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
        self.assertEqual(self.commands(), [])
        self.assertFalse(worker.closed.is_set())

    def test_stop_cancels_convenience_play_before_its_stream_exists(self):
        pcm = np.ones((10, 2), dtype=np.float32)
        module = self.module(pcm)
        converting = threading.Event()
        release = threading.Event()
        asarray = np.asarray

        def delayed_conversion(*args, **kwargs):
            converting.set()
            if not release.wait(3):
                raise RuntimeError("Test conversion was not released")
            return asarray(*args, **kwargs)

        with patch("vntts.rust_output.np.asarray", side_effect=delayed_conversion):
            module.play(pcm, 24000, latency="low")
            try:
                self.assertTrue(converting.wait(2))
                module.stop()
            finally:
                release.set()
                module.wait()
        self.assertEqual(self.commands(), [], "Pending playback restarted after Stop")

    def test_failed_open_ack_closes_unacknowledged_worker_instead_of_reusing_it(self):
        pcm = np.ones((10, 2), dtype=np.float32)
        module = self.module(pcm, "open-fail")
        with self.assertRaisesRegex(RuntimeError, "open acknowledgement rejected"):
            with self.stream(module) as stream:
                stream.write(pcm)
        self.assertTrue(self.workers[0].closed.is_set())
        self.assertIsNotNone(self.workers[0].process.poll())
        self.assertEqual(module.idle, [])
        self.assertFalse(any(command["op"] == "append" for command in self.commands()))

    def test_invalid_pcm_is_rejected_before_worker_acquisition(self):
        module = self.module()
        with self.stream(module) as stream:
            for pcm in (np.zeros((2, 3)), np.array([[np.nan, 0.0]])):
                with self.subTest(pcm=pcm), self.assertRaises(ValueError):
                    stream.write(pcm)
            self.assertFalse(stream.write(np.empty((0, 2))))
        self.assertEqual(self.workers, [])

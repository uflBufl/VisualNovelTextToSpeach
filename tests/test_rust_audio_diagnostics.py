"""Protocol evidence for native output gaps, independent of output hardware."""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.rust_audio import RustAudioWorker, active_audio_workers
from vntts.rust_output import RustOutputStream

PEER = r"""
import json, sys
mode = sys.argv[1]
def emit(**value):
    print(json.dumps(value), flush=True)
def finished(outcome):
    metric = dict(observation_count=3, unavailable_count=0,
                  discontinuity_count=1, max_gap_seconds=.03,
                  max_offset_seconds=.02, max_offset_position_frames=4,
                  max_offset_output_frame=4)
    if mode == 'invalid-clock':
        metric['max_offset_seconds'] = float('nan')
    if mode == 'invalid-count':
        metric['discontinuity_count'] = True
    emit(event='finished', id=clip['id'], outcome=outcome,
         frames_played=clip.get('frames', 4) if outcome == 'complete' else 1,
         total_frames=clip.get('frames', 4), underflow_count=0,
         callback_count=3, sample_rate=clip['sample_rate'], channels=clip['channels'],
         callback_timing=metric, dac_timing=metric,
         timing_sample_rate=48000.0, timing_output_frames=12,
         worker_pid=-1, worker_path='/spoofed/worker')
emit(event='ready', protocol=2)
clip = None
for line in sys.stdin.buffer:
    command = json.loads(line)
    op = command['op']
    if op in ('load', 'open_stream'):
        clip = command
        if op == 'load':
            sys.stdin.buffer.read(command['frames'] * command['channels'] * 4)
        emit(event='loaded' if op == 'load' else 'opened', id=clip['id'])
    elif op == 'append':
        sys.stdin.buffer.read(command['frames'] * clip['channels'] * 4)
        emit(event='appended', id=clip['id'])
    elif op == 'start':
        emit(event='started', id=clip['id'])
        emit(event='first_callback', id=clip['id'], frames_written=1)
        if mode != 'stop':
            finished('complete')
    elif op == 'stop':
        finished('interrupted')
    elif op == 'end':
        finished('complete')
"""


class NativeAudioDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.close()

    def worker(self, mode="complete"):
        worker = RustAudioWorker([sys.executable, "-u", "-c", PEER, mode])
        self.workers.append(worker)
        return worker

    def play(self, worker, first_write=None, stop=None):
        return worker.play_buffer(
            np.zeros((8, 2), dtype=np.float32),
            24000,
            2,
            "low",
            lambda: False,
            first_write,
            stop,
        )

    def test_cold_owner_is_published_before_pcm_and_every_clip_checks_session(self):
        worker = self.worker()
        observations = []

        def arm(identity, interrupted):
            snapshot = next(
                item
                for item in active_audio_workers()
                if item["worker_id"] == worker.diagnostic_worker_id
            )
            self.assertEqual(identity["worker_pid"], worker.process.pid)
            self.assertEqual(identity["worker_path"], worker.executable_path)
            self.assertEqual(snapshot["clip_id"], worker.next_id)
            self.assertFalse(interrupted())
            self.assertFalse(worker._diagnostic_clip)
            observations.append(snapshot)
            # A control command must not be held behind the diagnostic wait.
            acquired = threading.Event()

            def control():
                with worker.send_lock, worker.state_lock:
                    acquired.set()

            thread = threading.Thread(target=control)
            thread.start()
            self.assertTrue(acquired.wait(1))
            thread.join(1)
            return True

        with patch(
            "vntts.reading_diagnostics.arm_audio_worker_recording", side_effect=arm
        ):
            self.assertEqual(self.play(worker), (True, False))
            self.assertEqual(self.play(worker), (True, False))
        self.assertEqual(len(observations), 2)

    def test_stop_while_tap_is_arming_never_uploads_or_plays_pcm(self):
        worker = self.worker()
        stop, entered = threading.Event(), threading.Event()
        results = []

        def arm(identity, interrupted):
            entered.set()
            deadline = time.monotonic() + 2
            while not interrupted():
                if time.monotonic() > deadline:
                    self.fail("Stop did not interrupt recording arming")
                stop.wait(0.01)
            return False

        with (
            patch(
                "vntts.reading_diagnostics.arm_audio_worker_recording", side_effect=arm
            ),
            patch.object(worker, "_send_bounded", wraps=worker._send_bounded) as upload,
        ):
            playback = threading.Thread(
                target=lambda: results.append(self.play(worker, stop=stop))
            )
            playback.start()
            self.assertTrue(entered.wait(1))
            worker.cancel_active()
            stop.set()
            playback.join(2)
            self.assertFalse(playback.is_alive())
            upload.assert_not_called()
        self.assertEqual(results, [(False, False)])
        self.assertFalse(worker.closed.is_set())
        self.assertIsNone(worker.active_id)
        self.assertEqual(self.play(worker), (True, False))

    def test_stream_owner_is_published_before_open_command(self):
        worker = self.worker()
        observed = []

        def arm(identity, interrupted):
            observed.extend(active_audio_workers())
            self.assertFalse(worker._diagnostic_clip)
            return True

        with patch(
            "vntts.reading_diagnostics.arm_audio_worker_recording", side_effect=arm
        ) as arm_recording:
            module = Mock()
            module.acquire.return_value = worker
            stream = RustOutputStream(
                module, samplerate=24000, channels=1, dtype="float32", latency="low"
            )
            stream.start()
            stream._open()
        arm_recording.assert_called_once()
        self.assertEqual(observed[0]["worker_id"], worker.diagnostic_worker_id)

    def test_finished_evidence_preserves_source_context_and_actual_process_identity(
        self,
    ):
        worker = self.worker()
        first = Mock()
        context = audio_lifecycle_context.set({"playback_id": "line-54"})
        try:
            with patch("vntts.rust_audio.record_audio_lifecycle") as record:
                self.assertEqual(self.play(worker, first), (True, False))
        finally:
            audio_lifecycle_context.reset(context)
        first.assert_called_once_with()
        evidence = next(
            call.kwargs
            for call in record.call_args_list
            if call.args == ("rust-worker-finished",)
        )
        self.assertEqual(evidence["source_samples"], 16)
        self.assertEqual(evidence["sample_rate"], 24000)
        self.assertEqual(evidence["timing_sample_rate"], 48000)
        self.assertEqual(evidence["playback_id"], "line-54")
        self.assertEqual(evidence["worker_pid"], worker.process.pid)
        self.assertEqual(evidence["worker_path"], str(Path(sys.executable).resolve()))
        self.assertEqual(evidence["dac_timing"]["discontinuity_count"], 1)
        self.assertEqual(evidence["underflow_count"], 0)

    def test_stop_keeps_native_timing_and_public_worker_snapshot_is_live_and_copied(
        self,
    ):
        worker = self.worker("stop")
        stop = threading.Event()
        snapshots = []

        def first():
            snapshots.append(
                next(
                    item
                    for item in active_audio_workers()
                    if item["worker_pid"] == worker.process.pid
                )
            )
            stop.set()

        with patch("vntts.rust_audio.record_audio_lifecycle") as record:
            self.assertEqual(self.play(worker, first, stop), (False, False))
        self.assertEqual(snapshots[0]["clip_id"], 1)
        self.assertEqual(snapshots[0]["worker_path"], worker.executable_path)
        idle = next(
            item
            for item in active_audio_workers()
            if item["worker_pid"] == worker.process.pid
        )
        self.assertIsNone(idle["clip_id"])
        self.assertEqual(idle["worker_id"], snapshots[0]["worker_id"])
        evidence = next(
            call.kwargs
            for call in record.call_args_list
            if call.args == ("rust-worker-finished",)
        )
        self.assertEqual(evidence["outcome"], "interrupted")
        self.assertEqual(evidence["source_samples"], 16)
        self.assertEqual(evidence["timing_output_frames"], 12)
        worker.active_id = 2
        self.assertEqual(snapshots[0]["clip_id"], 1)
        worker.close()
        self.assertNotIn(
            worker.process.pid, [item["worker_pid"] for item in active_audio_workers()]
        )

    def test_query_only_workers_are_not_process_capture_targets(self):
        worker = self.worker()
        self.assertNotIn(
            worker.process.pid, [item["worker_pid"] for item in active_audio_workers()]
        )
        self.play(worker)
        self.assertIn(
            worker.process.pid, [item["worker_pid"] for item in active_audio_workers()]
        )
        other = self.worker()
        self.assertNotEqual(worker.diagnostic_worker_id, other.diagnostic_worker_id)

    def test_streaming_worker_completion_records_submitted_source_without_clip_adapter(
        self,
    ):
        worker = self.worker()
        worker.active_id = 1
        worker.send({"op": "open_stream", "id": 1, "sample_rate": 24000, "channels": 1})
        worker.receive(2)
        worker.send({"op": "append", "id": 1, "frames": 4}, bytes(4 * 4))
        worker.receive(2)
        with patch("vntts.rust_audio.record_audio_lifecycle") as record:
            worker.send({"op": "end", "id": 1})
            event = worker.receive(2)
        self.assertEqual(event["event"], "finished")
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["source_samples"], 4)
        self.assertEqual(record.call_args.kwargs["worker_pid"], worker.process.pid)

    def test_invalid_timing_cannot_enter_lifecycle_evidence_and_playback_fails_closed(
        self,
    ):
        for mode in ("invalid-clock", "invalid-count"):
            with self.subTest(mode=mode):
                worker = self.worker(mode)
                with patch("vntts.rust_audio.record_audio_lifecycle") as record:
                    with self.assertRaisesRegex(
                        RuntimeError, "invalid callback_timing"
                    ):
                        self.play(worker)
                record.assert_not_called()
                self.assertTrue(worker.closed.is_set())

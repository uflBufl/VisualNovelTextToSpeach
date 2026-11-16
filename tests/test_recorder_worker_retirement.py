"""Owned worker retirement must wait for its native recording segment."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from vntts.diagnostic_recording import NativeDiagnosticRecording


class RecorderWorkerRetirementTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.recording = NativeDiagnosticRecording(
            self.root,
            {"pid": 50, "window_id": 60, "executable": "/game"},
            600,
            lambda: (),
            self.root / "tools",
            deadline=700.0,
        )
        self.clock = patch("vntts.diagnostic_recording.monotonic", return_value=100.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.identity = {
            "worker_pid": 123,
            "worker_path": "/worker",
            "worker_id": "first",
        }

    @staticmethod
    def key(identity):
        return tuple(
            identity[name] for name in ("worker_pid", "worker_path", "worker_id")
        )

    def capture(self, identity=None, name="rust-123"):
        identity = self.identity if identity is None else identity
        output = self.root / name
        output.mkdir()
        (self.root / f"{name}-launcher.log").write_bytes(b"")
        process = Mock(pid=456, returncode=None)
        process.poll.side_effect = lambda: process.returncode
        process.wait.side_effect = lambda **kwargs: process.returncode
        self.recording.processes.append((process, output))
        self.recording._launched_at[name] = 99.0
        self.recording._identities[name] = identity
        self.recording._worker_lifetimes.append({**identity, "capture": name})
        self.recording._worker_arms[self.key(identity)] = "armed"
        self.final_metadata(output)
        return process, output

    def final_metadata(self, output, *, samples=True, **fields):
        (output / "session.json").write_text(
            json.dumps(
                {
                    "status": "captured_unqualified",
                    "errors": [],
                    "stop_requested": True,
                    "capture_stopped_monotonic_s": 100.0,
                    **fields,
                }
            )
        )
        (output / "audio.jsonl").write_text(
            json.dumps({"kind": "audio", "frame_count": 480}) + "\n" if samples else ""
        )

    def start_retirement(self, identity=None):
        identity = self.identity if identity is None else identity
        waiting = threading.Event()
        done = threading.Event()
        result = []
        original_wait = self.recording._arming.wait

        def wait(timeout):
            waiting.set()
            return original_wait(timeout)

        def finish():
            result.append(self.recording.finish_worker(identity))
            done.set()

        waiter = patch.object(self.recording._arming, "wait", side_effect=wait)
        waiter.start()
        self.addCleanup(waiter.stop)
        thread = threading.Thread(target=finish, daemon=True)
        thread.start()

        def cleanup():
            self.recording._finished.set()
            with self.recording._arming:
                self.recording._arming.notify_all()
            thread.join(2)

        self.addCleanup(cleanup)
        self.assertTrue(waiting.wait(2), "Retirement did not start waiting")
        return done, result

    def test_source_waits_for_helper_exit_before_retiring(self):
        process, output = self.capture()
        done, result = self.start_retirement()
        with patch.object(self.recording, "_request_capture_stop") as stop:
            self.recording._finish_worker_captures()
            stop.assert_called_once_with(process, output)
            self.assertFalse(
                done.is_set(), "Metadata alone must not acknowledge retirement"
            )
            self.assertEqual(
                self.recording._worker_arms[self.key(self.identity)], "armed"
            )
            process.returncode = 0
            self.recording._finish_worker_captures()
        self.assertTrue(done.wait(2))
        self.assertEqual(result, [(True, "finalized")])
        self.recording._retire_worker_captures(
            {self.key(self.identity): output.name}, {self.key(self.identity)}
        )
        self.recording._verify_final_captures(700.0)
        self.assertFalse(self.recording.report()["acquisition_gaps"])
        lifetime = self.recording._worker_lifetimes[0]
        self.assertEqual(lifetime["retirement_requested_monotonic"], 100.0)
        self.assertEqual(lifetime["capture_finalized_monotonic"], 100.0)

    def test_unannounced_worker_disappearance_remains_partial(self):
        _, output = self.capture()
        self.recording._retire_worker_captures(
            {self.key(self.identity): output.name}, {self.key(self.identity)}
        )
        self.assertEqual(
            self.recording.finish_worker(self.identity), (False, "capture-failed")
        )
        failure = self.recording._failures[output.name]
        self.assertIn("process capture is partial", failure["error_message"])
        self.assertEqual(self.recording.report()["qualification"], "failed")

    def test_invalid_final_evidence_never_acknowledges_retirement(self):
        for failure in ("exit", "native", "cutoff", "samples", "missing"):
            with self.subTest(failure=failure):
                identity = {**self.identity, "worker_id": failure}
                process, output = self.capture(identity, f"rust-{failure}")
                process.returncode = 1 if failure == "exit" else 0
                fields = {}
                if failure == "native":
                    fields.update(status="failed", errors=["Finalize CAF failed"])
                if failure == "cutoff":
                    fields["capture_stopped_monotonic_s"] = 99.99
                self.final_metadata(output, samples=failure != "samples", **fields)
                if failure == "missing":
                    (output / "session.json").unlink()
                self.recording._retirement_requests[self.key(identity)] = 100.0
                self.recording._finish_worker_captures()
                self.assertEqual(
                    self.recording.finish_worker(identity), (False, "capture-failed")
                )
                self.assertIn(output.name, self.recording._failures)

    def test_unresponsive_supervisor_has_bounded_wait_and_visible_gap(self):
        self.capture()
        with patch(
            "vntts.diagnostic_recording.monotonic",
            side_effect=[100.0, 100.0, 104.0, 104.0],
        ):
            self.assertEqual(
                self.recording.finish_worker(self.identity), (False, "timeout")
            )
        gap = self.recording.report()["acquisition_gaps"][0]
        self.assertEqual(gap["capture"], "rust-retirement")
        self.assertEqual(gap["worker_id"], "first")
        self.assertEqual(self.recording.error_type, "RustRecordingRetirementError")

    def test_replacement_with_reused_pid_gets_its_own_unretired_segment(self):
        process, output = self.capture()
        process.returncode = 0
        self.recording._retirement_requests[self.key(self.identity)] = 100.0
        self.recording._finish_worker_captures()
        replacement = {**self.identity, "worker_id": "replacement"}
        seen = {self.key(self.identity): output.name}
        with patch.object(self.recording, "_launch") as launch:
            self.recording._start_worker_capture(
                replacement, Path("/tap"), 700.0, seen, set()
            )
        replacement_output = self.root / "rust-123-2"
        self.assertEqual(launch.call_args.args[1], replacement_output)
        self.assertEqual(
            self.recording._identities[replacement_output.name], replacement
        )
        self.assertNotIn(self.key(replacement), self.recording._worker_arms)
        self.assertNotIn(self.key(replacement), self.recording._retirement_requests)
        self.assertEqual(
            self.recording.finish_worker(self.identity), (True, "finalized")
        )

    def test_global_stop_acknowledges_worker_before_waiting_for_other_captures(self):
        process, _ = self.capture()
        independent = Mock(pid=789, returncode=None)
        independent.poll.side_effect = lambda: independent.returncode
        independent.wait.side_effect = lambda **kwargs: independent.returncode
        self.recording.processes.append((independent, self.root / "game-audio"))
        done, result = self.start_retirement()
        self.recording.cancelled.set()

        def stop(helper):
            if helper is process:
                process.returncode = 0

        def wait_for_worker_acknowledgement(*, timeout):
            self.assertTrue(
                done.wait(2), "Global Stop waited on the source before acknowledging it"
            )
            self.assertEqual(result, [(True, "finalized")])
            independent.returncode = 0
            return 0

        with (
            patch("vntts.diagnostic_recording.sys.platform", "win32"),
            patch("vntts.diagnostic_recording.request_stop", side_effect=stop),
            patch.object(
                independent, "wait", side_effect=wait_for_worker_acknowledgement
            ),
        ):
            self.recording._stop_processes()
        self.assertEqual(result, [(True, "finalized")])
        self.assertFalse(self.recording._failures)

    def test_finished_session_needs_no_new_retirement_request(self):
        self.recording._finished.set()
        self.assertEqual(
            self.recording.finish_worker(self.identity), (True, "recorder-stopped")
        )
        self.assertFalse(self.recording._retirement_requests)

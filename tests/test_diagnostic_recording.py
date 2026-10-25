import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psutil

from vntts.diagnostic_recording import (
    MAX_DIAGNOSTIC_MEDIA_BYTES,
    MAX_DIAGNOSTIC_SECONDS,
    NativeDiagnosticRecording,
    _run_checked,
)


class NativeDiagnosticRecordingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def recording(self, workers=lambda: ()):
        return NativeDiagnosticRecording(
            self.root,
            {"pid": 50, "window_id": 60, "executable": "/game"},
            1,
            workers,
            self.root / "tools",
        )

    def fake_launch(self, recording, commands, *, samples=True, failed=False):
        def launch(command, output):
            commands.append(command)
            output.mkdir()
            (self.root / (output.name + "-launcher.log")).write_bytes(b"")
            video = output.name == "window"
            capture_failed = failed(output.name) if callable(failed) else failed
            (output / "session.json").write_text(
                json.dumps(
                    {
                        "status": "failed"
                        if capture_failed
                        else "succeeded"
                        if video
                        else "captured_unqualified",
                        "errors": [],
                    }
                )
            )
            (output / ("game.jsonl" if video else "audio.jsonl")).write_text(
                json.dumps(
                    {
                        "kind": "video" if video else "audio",
                        "written": True,
                        "frame_count": 480,
                    }
                )
                + "\n"
                if samples
                else ""
            )
            process = Mock(returncode=1 if capture_failed else 0)
            process.poll.return_value = process.returncode
            process.wait.return_value = process.returncode
            recording.processes.append((process, output))
            recording._launched_at[output.name] = time.monotonic()

        return launch

    def run_fake(self, recording, **kwargs):
        commands = []
        with (
            patch(
                "vntts.diagnostic_recording.prepare_recorders",
                return_value=(Path("/video"), Path("/tap")),
            ),
            patch(
                "vntts.diagnostic_recording.process_executable", return_value="/game"
            ),
            patch(
                "vntts.diagnostic_recording.shutil.disk_usage",
                return_value=Mock(free=10 * 1024**3),
            ),
            patch.object(
                recording,
                "_launch",
                side_effect=self.fake_launch(recording, commands, **kwargs),
            ),
        ):
            recording.run()
        return commands

    @unittest.skipUnless(hasattr(os, "killpg"), "Owned process groups require POSIX")
    def test_report_retains_helper_process_identity_while_native_capture_is_active(
        self,
    ):
        from vntts.diagnostic_recording import _stop_process

        recording = self.recording()
        recording._launch(
            [sys.executable, "-c", "import time; time.sleep(60)"], self.root / "window"
        )
        process = recording.processes[0][0]
        try:
            capture = recording.report()["captures"][0]
            self.assertEqual(capture["helper_pid"], process.pid)
            self.assertEqual(
                capture["helper_created"], psutil.Process(process.pid).create_time()
            )
            self.assertIsNone(capture["returncode"])
        finally:
            _stop_process(process, 2)

    def test_duration_is_mandatory_bounded_integer_up_to_one_hour(self):
        for invalid in (0, 3601, True, 1.5, "3600", None):
            with self.subTest(seconds=invalid), self.assertRaises(ValueError):
                NativeDiagnosticRecording(self.root, {}, invalid, lambda: (), self.root)
        self.assertEqual(MAX_DIAGNOSTIC_SECONDS, 3600)
        for seconds in (1, 600, 601, 3600):
            self.assertEqual(
                NativeDiagnosticRecording(
                    self.root, {}, seconds, lambda: (), self.root
                ).seconds,
                seconds,
            )

    def test_hour_keeps_continuous_initial_and_late_worker_capture_to_owned_deadline(
        self,
    ):
        clock = [100.0]
        recording = self.recording()
        recording.seconds = 3600
        calls = 0

        def workers():
            nonlocal calls
            calls += 1
            if calls == 1:
                clock[0] = 1300.0
                return ()
            recording.cancelled.set()
            return ({"worker_pid": 123, "worker_path": "/worker", "worker_id": "late"},)

        recording.workers = workers
        with patch(
            "vntts.diagnostic_recording.monotonic", side_effect=lambda: clock[0]
        ):
            commands = self.run_fake(recording)
        self.assertEqual(len(commands), 3)
        self.assertEqual(float(commands[0][4]), 3600)
        self.assertEqual(float(commands[1][4]), 3600)
        self.assertEqual(int(commands[2][4]), 2400)
        self.assertEqual(recording.deadline, 3700)
        self.assertEqual(recording.report()["captures"][2]["directory"], "rust-123")

    def test_duration_scales_media_budget_without_deleting_evidence(self):
        path = self.root / "large.caf"
        for seconds, limit in (
            (600, 1536 * 1024**2),
            (601, 3 * 1024**3),
            (3600, 9 * 1024**3),
        ):
            with self.subTest(seconds=seconds):
                recording = self.recording()
                recording.seconds = seconds
                with path.open("wb") as output:
                    output.truncate(limit)
                recording._check_media_budget()
                with path.open("ab") as output:
                    output.write(b"x")
                with self.assertRaisesRegex(RuntimeError, "evidence limit"):
                    recording._check_media_budget()
                self.assertTrue(path.is_file())
        self.assertEqual(MAX_DIAGNOSTIC_MEDIA_BYTES, 9 * 1024**3)

    def test_worker_arm_wait_is_identity_specific_cancellable_and_failure_is_cached(
        self,
    ):
        recording = self.recording()
        identity = {"worker_pid": 123, "worker_path": "/worker", "worker_id": "first"}
        key = tuple(identity.values())
        recording._worker_arms[key] = "armed"
        self.assertEqual(
            recording.wait_for_worker(identity, lambda: False), (True, "armed")
        )
        replacement = {**identity, "worker_id": "replacement"}
        self.assertEqual(
            recording.wait_for_worker(replacement, lambda: True), (False, "cancelled")
        )
        self.assertFalse(recording.report()["acquisition_gaps"])
        recording.deadline = time.monotonic() - 1
        self.assertEqual(
            recording.wait_for_worker(replacement, lambda: False), (False, "timeout")
        )
        recording.deadline = time.monotonic() + 600
        started = time.monotonic()
        self.assertEqual(
            recording.wait_for_worker(replacement, lambda: False), (False, "timeout")
        )
        self.assertLess(time.monotonic() - started, 0.1)
        report = recording.report()
        self.assertEqual(report["qualification"], "failed")
        self.assertEqual(report["acquisition_gaps"][0]["worker_id"], "replacement")

    def test_worker_arm_failure_and_recorder_exit_release_waiters(self):
        identity = {"worker_pid": 123, "worker_path": "/worker", "worker_id": "first"}
        for cause, reason in (
            ("capture", "capture-failed"),
            ("exit", "recorder-stopped"),
            ("stop", "cancelled"),
        ):
            with self.subTest(cause=cause):
                recording = self.recording()
                if cause == "capture":
                    recording._identities["rust-123"] = identity
                    recording._fail_capture("rust-123", RuntimeError("tap denied"))
                elif cause == "exit":
                    recording._worker_arms[tuple(identity.values())] = "armed"
                    recording._finished.set()
                else:
                    recording.cancelled.set()
                self.assertEqual(
                    recording.wait_for_worker(identity, lambda: False), (False, reason)
                )

    def test_native_started_tap_arms_cold_worker_without_waiting_for_source_samples(
        self,
    ):
        identity = {"worker_pid": 123, "worker_path": "/worker", "worker_id": "first"}
        replacement = {**identity, "worker_pid": 456, "worker_id": "replacement"}
        recording = self.recording(lambda: (identity, replacement))
        commands, results = [], []
        original_launch = self.fake_launch(recording, commands)

        def launch(command, output):
            original_launch(command, output)
            if not output.name.startswith("rust-"):
                return
            process = recording.processes[-1][0]
            process.poll.return_value = None
            worker = identity if output.name == "rust-123" else replacement
            (output / "session.json").write_text(
                json.dumps(
                    {
                        "status": "in_progress",
                        "target": {
                            "pid": worker["worker_pid"],
                            "executable": worker["worker_path"],
                        },
                        "capture_started_monotonic_s": time.monotonic(),
                    }
                )
            )
            (output / "audio.jsonl").write_text("")

        def wait():
            results.append(recording.wait_for_worker(identity, lambda: False))
            results.append(recording.wait_for_worker(replacement, lambda: False))
            recording.cancelled.set()

        waiter = threading.Thread(target=wait)
        waiter.start()
        try:
            with (
                patch(
                    "vntts.diagnostic_recording.prepare_recorders",
                    return_value=(Path("/video"), Path("/tap")),
                ),
                patch(
                    "vntts.diagnostic_recording.process_executable",
                    return_value="/game",
                ),
                patch(
                    "vntts.diagnostic_recording.shutil.disk_usage",
                    return_value=Mock(free=10 * 1024**3),
                ),
                patch.object(recording, "_launch", side_effect=launch),
                patch.object(recording, "_stop_processes"),
            ):
                recording.run()
        finally:
            recording.cancelled.set()
            waiter.join(2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(results, [(True, "armed"), (True, "armed")])
        self.assertIsNone(recording.ready_monotonic)
        self.assertEqual(len(commands), 4)

    def test_explicit_worker_contract_and_native_cli_never_select_parent_or_desktop(
        self,
    ):
        recording = self.recording(
            lambda: (
                {
                    "worker_pid": 123,
                    "worker_path": "/native/audio-worker",
                    "clip_id": 1,
                },
            )
        )
        commands = self.run_fake(recording)
        self.assertEqual(commands[0][1:4], ["--video-only", "50", "60"])
        self.assertEqual(commands[1][1:4], ["--capture", "50", "/game"])
        self.assertEqual(commands[2][1:4], ["--capture", "123", "/native/audio-worker"])
        self.assertEqual(commands[2][-1], "--allow-audio-capture")
        self.assertEqual(recording.status, "stopped")
        self.assertIsNotNone(recording.ready_monotonic)
        self.assertEqual(recording.report()["qualification"], "captured-unqualified")

    def test_shared_deadline_finalizes_capture_without_partial_cancellation_label(self):
        clock = [100.0]
        recording = self.recording()
        recording.deadline = 101.0

        def workers():
            clock[0] = 101.0
            recording.cancelled.set()
            metadata = self.root / "game-audio" / "session.json"
            metadata.write_text(
                json.dumps(
                    {
                        "status": "captured_unqualified",
                        "errors": [],
                        "stop_requested": True,
                        "capture_stopped_monotonic_s": 101.01,
                        "captured_seconds": 0.8,
                        "duration_completed": False,
                    }
                )
            )
            return ()

        recording.workers = workers
        with patch(
            "vntts.diagnostic_recording.monotonic", side_effect=lambda: clock[0]
        ):
            self.run_fake(recording)
        report = recording.report()
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["stop_reason"], "deadline")
        self.assertEqual(report["qualification"], "captured-unqualified")
        self.assertEqual(report["stop_requested_monotonic"], 101.0)
        self.assertEqual(report["deadline_monotonic"], 101.0)
        self.assertFalse(report["captures"][1]["session"]["duration_completed"])

    def test_stop_just_before_deadline_remains_partial_after_delayed_observation(self):
        clock = [100.0]
        recording = self.recording()
        recording.deadline = 101.0

        def workers():
            clock[0] = 100.95
            recording.cancelled.set()
            clock[0] = 101.05
            recording.cancelled.set()
            return ()

        recording.workers = workers
        with patch(
            "vntts.diagnostic_recording.monotonic", side_effect=lambda: clock[0]
        ):
            self.run_fake(recording)
        report = recording.report()
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(report["stop_reason"], "requested_stop")
        self.assertEqual(report["qualification"], "partial")
        self.assertEqual(report["stop_requested_monotonic"], 100.95)

    def test_external_early_native_stop_remains_an_acquisition_gap(self):
        clock = [100.0]
        recording = self.recording()
        recording.deadline = 101.0

        def workers():
            metadata = self.root / "game-audio" / "session.json"
            metadata.write_text(
                json.dumps(
                    {
                        "status": "captured_unqualified",
                        "errors": [],
                        "stop_requested": True,
                        "capture_stopped_monotonic_s": 100.5,
                        "duration_completed": False,
                    }
                )
            )
            clock[0] = 101.0
            recording.cancelled.set()
            return ()

        recording.workers = workers
        with patch(
            "vntts.diagnostic_recording.monotonic", side_effect=lambda: clock[0]
        ):
            self.run_fake(recording)
        report = recording.report()
        self.assertEqual(report["qualification"], "failed")
        self.assertEqual(
            report["captures"][1]["session"]["status"], "captured_unqualified"
        )
        self.assertIn("before owned cutoff", report["error_message"])
        self.assertEqual(report["acquisition_gaps"][0]["capture"], "game-audio")

    def test_controlled_stop_still_rejects_native_failure_during_finalization(self):
        clock = [100.0]
        recording = self.recording()
        recording.deadline = 101.0

        def workers():
            clock[0] = 101.0
            recording.cancelled.set()
            return ()

        def finalize():
            metadata = self.root / "game-audio" / "session.json"
            metadata.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "errors": ["No nonzero PCM; silence/consent is unqualified"],
                        "stop_requested": True,
                        "duration_completed": False,
                    }
                )
            )

        recording.workers = workers
        with (
            patch("vntts.diagnostic_recording.monotonic", side_effect=lambda: clock[0]),
            patch.object(recording, "_stop_processes", side_effect=finalize) as stop,
        ):
            self.run_fake(recording)
        stop.assert_called_once_with()
        report = recording.report()
        self.assertEqual(report["qualification"], "failed")
        self.assertEqual(report["acquisition_gaps"][0]["capture"], "game-audio")
        self.assertEqual(report["captures"][1]["session"]["status"], "failed")
        self.assertEqual(
            report["captures"][1]["session"]["errors"],
            ["No nonzero PCM; silence/consent is unqualified"],
        )

    def test_launch_without_samples_and_failed_native_capture_never_claim_readiness(
        self,
    ):
        for case in ("no-samples", "failed"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as folder:
                    self.root = Path(folder)
                    recording = self.recording()
                    self.run_fake(
                        recording, samples=case != "no-samples", failed=case == "failed"
                    )
                    self.assertEqual(recording.status, "failed")
                    self.assertIsNone(recording.ready_monotonic)
                    self.assertEqual(recording.report()["qualification"], "failed")

    def test_failed_worker_keeps_independent_captures_and_new_worker_recording(self):
        snapshots = iter(
            (
                ({"worker_pid": 123, "worker_path": "/worker", "worker_id": "first"},),
                (),
            )
        )
        replacement = {
            "worker_pid": 456,
            "worker_path": "/worker",
            "worker_id": "second",
        }
        recording = self.recording(lambda: next(snapshots, (replacement,)))
        commands = self.run_fake(recording, failed=lambda name: name == "rust-123")
        self.assertEqual(len(commands), 4)
        report = recording.report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["qualification"], "failed")
        self.assertEqual(report["captures"][2]["session"]["status"], "failed")
        self.assertEqual(
            report["captures"][3]["session"]["status"], "captured_unqualified"
        )
        self.assertEqual(report["captures"][0]["session"]["status"], "succeeded")
        self.assertIsNotNone(
            report["worker_lifetimes"][0]["retired_observed_monotonic"]
        )
        self.assertEqual(report["acquisition_gaps"][0]["worker_id"], "first")

    def test_pid_reuse_starts_separate_segment_and_idle_worker_is_not_relaunched(self):
        first = {
            "worker_pid": 123,
            "worker_path": "/worker",
            "worker_id": "first",
            "clip_id": 1,
        }
        idle = {**first, "clip_id": None}
        replacement = {**first, "worker_id": "second"}
        snapshots = iter(((first,), (idle,), ()))
        recording = self.recording(lambda: next(snapshots, (replacement,)))
        commands = self.run_fake(recording)
        self.assertEqual(len(commands), 4)
        self.assertEqual(
            [capture["directory"] for capture in recording.report()["captures"]],
            ["window", "game-audio", "rust-123", "rust-123-2"],
        )
        self.assertTrue(recording.report()["captures"][2]["acquisition_failure"])
        self.assertFalse(recording.report()["captures"][3]["acquisition_failure"])

    @unittest.skipUnless(hasattr(os, "killpg"), "Owned process groups require POSIX")
    def test_preparation_is_cancellable_and_reaps_its_owned_process(self):
        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(InterruptedError):
                _run_checked(
                    [sys.executable, "-c", "import time; time.sleep(60)"], cancelled, 90
                )
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 3)

    @unittest.skipUnless(hasattr(os, "killpg"), "Owned process groups require POSIX")
    def test_preparation_failure_preserves_the_native_permission_reason(self):
        with self.assertRaisesRegex(
            RuntimeError, "Screen Recording permission missing"
        ):
            _run_checked(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('Screen Recording permission missing', file=sys.stderr); sys.exit(1)",
                ],
                threading.Event(),
                3,
            )

    def test_failed_capture_finalizes_without_blocking_other_recorders(self):
        recording = self.recording()
        process = Mock(pid=101)
        process.poll.return_value = None
        output = self.root / "rust-123"
        with (
            patch("vntts.diagnostic_recording.monotonic", side_effect=[10, 11, 12]),
            patch("vntts.diagnostic_recording.os.killpg") as kill,
        ):
            recording._stop_failed_capture(process, output)
            recording._stop_failed_capture(process, output)
            recording._stop_failed_capture(process, output)
        self.assertEqual(
            kill.call_args_list,
            [
                unittest.mock.call(101, signal.SIGTERM),
                unittest.mock.call(101, signal.SIGKILL),
            ],
        )
        process.wait.assert_not_called()

    @unittest.skipUnless(hasattr(os, "killpg"), "Owned process groups require POSIX")
    def test_original_deadline_bounds_slow_preparation(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            _run_checked(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                threading.Event(),
                90,
                deadline=started + 0.1,
            )
        self.assertLess(time.monotonic() - started, 3)

    def test_expired_preparation_deadline_never_launches_a_child(self):
        with patch("vntts.diagnostic_recording.subprocess.Popen") as launch:
            with self.assertRaises(TimeoutError):
                _run_checked(
                    ["compiler"], threading.Event(), 90, deadline=time.monotonic() - 1
                )
        launch.assert_not_called()

    def test_expired_session_after_preparation_never_starts_capture(self):
        recording = self.recording()
        recording.deadline = 101
        clock = [100]

        def prepare(*args, **kwargs):
            self.assertEqual(kwargs["deadline"], 101)
            clock[0] = 102
            return Path("/video"), Path("/tap")

        with (
            patch("vntts.diagnostic_recording.prepare_recorders", side_effect=prepare),
            patch("vntts.diagnostic_recording.monotonic", side_effect=lambda: clock[0]),
            patch(
                "vntts.diagnostic_recording.shutil.disk_usage",
                return_value=Mock(free=10 * 1024**3),
            ),
            patch(
                "vntts.diagnostic_recording.process_executable", return_value="/game"
            ),
            patch.object(recording, "_launch") as launch,
        ):
            recording.run()
        launch.assert_not_called()
        self.assertEqual(recording.error_type, "TimeoutError")
        self.assertEqual(recording.report()["qualification"], "failed")

    def test_exited_preparation_parent_still_cleans_its_owned_child_group(self):
        from vntts.diagnostic_recording import _stop_process

        parent = Mock(pid=101)
        parent.poll.return_value = 0
        with patch("vntts.diagnostic_recording.os.killpg") as kill:
            _stop_process(parent, 2)
        self.assertEqual(
            kill.call_args_list,
            [
                unittest.mock.call(101, signal.SIGTERM),
                unittest.mock.call(101, signal.SIGKILL),
            ],
        )
        parent.wait.assert_called()

    def test_media_budget_stops_without_deleting_evidence(self):
        path = self.root / "large.caf"
        with path.open("wb") as output:
            output.truncate(1536 * 1024**2 + 1)
        with self.assertRaisesRegex(RuntimeError, "1.5 GiB"):
            self.recording()._check_media_budget()
        self.assertTrue(path.is_file())

    @unittest.skipUnless(hasattr(os, "killpg"), "Owned process groups require POSIX")
    def test_cleanup_signals_all_owned_groups_before_waiting_and_kills_stalls(self):
        recording = self.recording()
        events, processes = [], []
        for pid in (101, 102, 103):
            process = Mock(pid=pid, returncode=None)
            process.poll.side_effect = lambda process=process: process.returncode

            def wait(timeout, process=process):
                events.append(("wait", process.pid))
                if process.returncode is None:
                    raise subprocess.TimeoutExpired("recorder", timeout)
                return process.returncode

            process.wait.side_effect = wait
            processes.append(process)
            recording.processes.append((process, self.root / str(pid)))

        def kill(pid, sig):
            events.append((sig, pid))
            if sig == signal.SIGKILL:
                next(
                    process for process in processes if process.pid == pid
                ).returncode = -9

        with patch("vntts.diagnostic_recording.os.killpg", side_effect=kill):
            recording._stop_processes()
        self.assertEqual(events[:3], [(signal.SIGTERM, pid) for pid in (101, 102, 103)])
        self.assertTrue(all(process.returncode == -9 for process in processes))

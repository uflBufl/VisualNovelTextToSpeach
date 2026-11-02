import ctypes
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, call, patch

import psutil

from vntts.diagnostic_recording import (
    NativeDiagnosticRecording,
    _run_checked,
    prepare_recorders,
    process_executable,
)
from vntts.windows_recording_process import RecorderJob, request_stop


class WindowsRecorderOwnershipTest(unittest.TestCase):
    def kernel(self):
        kernel = Mock()
        kernel.CreateJobObjectW.return_value = 17
        kernel.SetInformationJobObject.return_value = 1
        kernel.AssignProcessToJobObject.return_value = 1
        kernel.CloseHandle.return_value = 1
        return kernel

    def test_job_owns_exact_helper_handle_and_closes_once(self):
        kernel = self.kernel()
        job = RecorderJob(kernel)
        process = Mock(pid=30, _handle=91)
        job.assign(process)
        job.close()
        job.close()
        limits = kernel.SetInformationJobObject.call_args.args
        self.assertEqual(limits[:2], (17, 9))
        self.assertEqual(limits[2]._obj.basic.flags, 0x2000)
        self.assertEqual(limits[3], ctypes.sizeof(limits[2]._obj))
        self.assertEqual(limits[3], 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112)
        kernel.AssignProcessToJobObject.assert_called_once_with(17, 91)
        kernel.CloseHandle.assert_called_once_with(17)

    def test_job_setup_failure_closes_its_handle(self):
        kernel = self.kernel()
        kernel.SetInformationJobObject.return_value = 0
        with self.assertRaisesRegex(OSError, "configure"):
            RecorderJob(kernel)
        kernel.CloseHandle.assert_called_once_with(17)

    def test_assignment_failure_is_explicit(self):
        kernel = self.kernel()
        kernel.AssignProcessToJobObject.return_value = 0
        job = RecorderJob(kernel)
        try:
            with self.assertRaisesRegex(OSError, "own recorder"):
                job.assign(Mock(_handle=91))
        finally:
            job.close()

    def test_stop_flushes_and_closes_only_helper_stdin(self):
        stream = Mock()
        process = Mock(stdin=stream)
        process.poll.return_value = None
        request_stop(process)
        self.assertEqual(
            stream.mock_calls, [call.write(b"stop\n"), call.flush(), call.close()]
        )
        process.kill.assert_not_called()
        stream.write.side_effect = BrokenPipeError()
        request_stop(process)
        self.assertEqual(stream.close.call_count, 2)


class WindowsDiagnosticRecordingTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def recording(self):
        return NativeDiagnosticRecording(
            self.root,
            {"pid": 50, "window_id": 2**40, "executable": r"C:\Game\game.exe"},
            1,
            lambda: (),
            self.root / "tools",
        )

    def test_resolves_selected_windows_executable_without_libproc(self):
        with (
            patch("vntts.diagnostic_recording.sys.platform", "win32"),
            patch("vntts.diagnostic_recording.psutil.Process") as process,
        ):
            process.return_value.exe.return_value = r"C:\Game\game.exe"
            self.assertEqual(process_executable(50), r"C:\Game\game.exe")
            process.return_value.exe.side_effect = psutil.NoSuchProcess(50)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                process_executable(50)

    def test_prepares_installed_worker_without_compiling_or_mac_fallback(self):
        cancelled = Event()
        worker = self.root / "vntts-capture-worker.exe"
        with (
            patch("vntts.diagnostic_recording.sys.platform", "win32"),
            patch("vntts.native_capture.worker_executable", return_value=worker),
            patch("vntts.diagnostic_recording._run_checked") as checked,
        ):
            self.assertEqual(
                prepare_recorders(self.root, cancelled, deadline=30), (worker, worker)
            )
        self.assertEqual(
            checked.call_args_list,
            [
                call([str(worker), flag], cancelled, 10, deadline=30)
                for flag in ("--preflight", "--selfcheck")
            ],
        )

    def test_launch_failure_releases_job_and_stops_new_helper(self):
        recording = self.recording()
        process, job = Mock(pid=70), Mock()
        job.assign.side_effect = OSError("ownership failed")
        with (
            patch("vntts.diagnostic_recording.sys.platform", "win32"),
            patch("vntts.diagnostic_recording.RecorderJob", return_value=job),
            patch(
                "vntts.diagnostic_recording.subprocess.Popen", return_value=process
            ) as spawn,
            patch("vntts.diagnostic_recording._stop_process") as stopped,
        ):
            with self.assertRaisesRegex(OSError, "ownership failed"):
                recording._launch(["helper.exe"], self.root / "window")
        job.assign.assert_called_once_with(process)
        stopped.assert_called_once_with(process, 2)
        job.close.assert_called_once()
        self.assertEqual(recording.processes, [])
        self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertNotIn("start_new_session", spawn.call_args.kwargs)

    def test_windows_preparation_failure_releases_owned_process(self):
        process, job = Mock(), Mock()
        process.poll.return_value = 1
        process.returncode = 1
        with (
            patch("vntts.diagnostic_recording.sys.platform", "win32"),
            patch("vntts.diagnostic_recording.RecorderJob", return_value=job),
            patch("vntts.diagnostic_recording.subprocess.Popen", return_value=process),
            patch("vntts.diagnostic_recording._stop_process") as stopped,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Native recorder preparation failed"
            ):
                _run_checked(["helper.exe", "--preflight"], Event(), 2)
        job.assign.assert_called_once_with(process)
        stopped.assert_called_once_with(process, 2)
        job.close.assert_called_once()

    def test_pointer_sized_hwnd_and_windows_path_case_are_preserved(self):
        recording = self.recording()
        recording.target["pid"] = 2**31 + 4
        commands = []

        def launch(command, output):
            commands.append(command)
            output.mkdir()
            (self.root / (output.name + "-launcher.log")).write_bytes(b"")
            video = output.name == "window"
            (output / "session.json").write_text(
                json.dumps(
                    {
                        "status": "succeeded" if video else "captured_unqualified",
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
            )
            process = Mock(returncode=0, stdin=io.BytesIO())
            process.poll.return_value = 0
            recording.processes.append((process, output))
            recording._launched_at[output.name] = 0

        def workers():
            recording.cancelled.set()
            return ()

        recording.workers = workers
        with (
            patch("vntts.diagnostic_recording.sys.platform", "win32"),
            patch(
                "vntts.diagnostic_recording.prepare_recorders",
                return_value=(Path("helper.exe"), Path("helper.exe")),
            ),
            patch(
                "vntts.diagnostic_recording.process_executable",
                return_value=r"c:\GAME\game.exe",
            ),
            patch(
                "vntts.diagnostic_recording.shutil.disk_usage",
                return_value=Mock(free=10 * 1024**3),
            ),
            patch.object(recording, "_launch", side_effect=launch),
        ):
            recording.run()
        self.assertEqual(recording.status, "stopped", recording.error_message)
        self.assertEqual(commands[0][2], str(2**31 + 4))
        self.assertEqual(commands[0][3], str(2**40))
        self.assertEqual(commands[1][-1], "--allow-audio-capture")

    def test_stop_requests_all_helpers_before_wait_then_closes_all_jobs(self):
        recording = self.recording()
        events = []
        processes = []
        for pid in (71, 72):
            process = Mock(pid=pid, returncode=None)
            process.poll.side_effect = lambda process=process: process.returncode

            def wait(timeout, process=process):
                if process.returncode is None:
                    raise subprocess.TimeoutExpired("helper", timeout)
                events.append(("wait", process.pid))
                return process.returncode

            process.wait.side_effect = wait
            process.kill.side_effect = lambda process=process: setattr(
                process, "returncode", 1
            )
            job = Mock()
            job.close.side_effect = lambda pid=pid: events.append(("close-job", pid))
            recording.processes.append((process, self.root / str(pid)))
            recording._jobs[pid] = job
            processes.append(process)
        with (
            patch("vntts.diagnostic_recording.sys.platform", "win32"),
            patch(
                "vntts.diagnostic_recording.request_stop",
                side_effect=lambda p: events.append(("stop", p.pid)),
            ),
        ):
            recording._stop_processes()
        self.assertEqual(events[:2], [("stop", 71), ("stop", 72)])
        self.assertEqual({pid for kind, pid in events if kind == "close-job"}, {71, 72})
        self.assertEqual(recording._jobs, {})
        for process in processes:
            process.kill.assert_called_once()

    def test_failed_windows_capture_requests_stop_before_owned_kill(self):
        recording = self.recording()
        process, job = Mock(pid=71), Mock()
        process.poll.return_value = None
        recording._jobs[71] = job
        with (
            patch("vntts.diagnostic_recording.sys.platform", "win32"),
            patch("vntts.diagnostic_recording.monotonic", side_effect=[10, 11, 12]),
            patch("vntts.diagnostic_recording.request_stop") as stop,
        ):
            for _ in range(3):
                recording._stop_failed_capture(process, self.root / "window")
        stop.assert_called_once_with(process)
        job.close.assert_called_once()
        process.kill.assert_called_once()

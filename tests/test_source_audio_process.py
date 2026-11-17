import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import psutil

from vntts import source_audio_process
from vntts.cleanup import cleanup_on_exit
from vntts.source_audio_process import SourceAudioProcess


class SourceAudioProcessTest(unittest.TestCase):
    def test_completed_publishers_remove_only_their_scratch_and_preserve_environment(
        self,
    ):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                environment = dict(os.environ, SOURCE_AUDIO_TEST="preserved")
                original = environment.copy()
                owner = SourceAudioProcess(
                    [
                        sys.executable,
                        "-c",
                        "import json,os,sys,tempfile; "
                        "print(json.dumps([tempfile.gettempdir(), "
                        "[os.environ[n] for n in ('TMPDIR','TEMP','TMP')], "
                        "os.environ['SOURCE_AUDIO_TEST']])); "
                        f"sys.exit({returncode})",
                    ],
                    popen_factory=subprocess.Popen,
                    environment=environment,
                )
                try:
                    stdout, _stderr = owner.process.communicate(timeout=5)
                    directory, temporary_paths, marker = json.loads(stdout)
                    self.assertTrue(Path(directory).is_absolute())
                    self.assertEqual(temporary_paths, [directory] * 3)
                    self.assertEqual(marker, "preserved")
                    self.assertEqual(environment, original)
                    self.assertTrue(Path(directory).is_dir())
                    owner.close()
                    owner.close()
                    self.assertFalse(Path(directory).exists())
                    self.assertEqual(owner.process.returncode, returncode)
                finally:
                    owner.close()

    def test_failure_drains_the_decoder_before_removing_wem_and_wav(self):
        script = """
import json, subprocess, sys, tempfile
from pathlib import Path
directory = Path(tempfile.mkdtemp(prefix='decoder-'))
(directory / 'input.wem').write_bytes(b'wem')
(directory / 'output.wav').write_bytes(b'wav')
decoder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(json.dumps([str(directory), decoder.pid]), flush=True)
decoder.wait()
"""
        for error in (RuntimeError("cancelled"), ValueError("callback failed")):
            with self.subTest(error=type(error).__name__):
                owner = SourceAudioProcess(
                    [sys.executable, "-c", script], popen_factory=subprocess.Popen
                )
                directory, decoder_pid = json.loads(owner.process.stdout.readline())
                decoder = psutil.Process(decoder_pid)
                scratch = Path(directory).parent
                remove = shutil.rmtree

                def remove_after_drain(path):
                    self.assertEqual(path, scratch)
                    self.assertIsNotNone(owner.process.poll())
                    self.assertFalse(
                        decoder.is_running()
                        and decoder.status() != psutil.STATUS_ZOMBIE
                    )
                    self.assertTrue((Path(directory) / "input.wem").is_file())
                    self.assertTrue((Path(directory) / "output.wav").is_file())
                    remove(path)

                try:
                    with (
                        patch.object(
                            source_audio_process.shutil, "rmtree", remove_after_drain
                        ),
                        self.assertRaises(type(error)) as raised,
                    ):
                        with cleanup_on_exit(
                            owner.close, description="Publisher cleanup"
                        ):
                            raise error
                    self.assertIs(raised.exception, error)
                    self.assertFalse(scratch.exists())
                finally:
                    owner.close()

    def test_failed_drain_keeps_scratch_and_the_original_error(self):
        owner = SourceAudioProcess(
            [
                sys.executable,
                "-c",
                "import time; print('ready', flush=True); time.sleep(60)",
            ],
            popen_factory=subprocess.Popen,
        )
        self.assertEqual(owner.process.stdout.readline().strip(), "ready")
        error = ValueError("cancelled")
        try:
            with (
                patch.object(
                    owner, "_wait_for_decoders", side_effect=OSError("drain failed")
                ),
                self.assertLogs("vntts.cleanup", level="ERROR"),
                self.assertRaises(ValueError) as raised,
            ):
                with cleanup_on_exit(owner.close, description="Publisher cleanup"):
                    raise error
            self.assertIs(raised.exception, error)
            self.assertTrue(owner._directory.is_dir())
            self.assertIn("drain failed", error.__notes__[0])
        finally:
            owner.process.communicate(timeout=5)
            owner.close()

    def test_start_failure_removes_scratch_and_preserves_the_failure(self):
        error = OSError("cannot start")
        directories = []

        def start(_arguments, **options):
            directories.append(Path(options["env"]["TMPDIR"]))
            raise error

        with self.assertRaises(OSError) as raised:
            SourceAudioProcess(["publisher"], popen_factory=start)
        self.assertIs(raised.exception, error)
        self.assertFalse(directories[0].exists())

    def test_worker_exiting_while_cleanup_starts_still_releases_scratch(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        with (
            patch.object(source_audio_process.sys, "platform", "darwin"),
            patch.object(
                source_audio_process.psutil,
                "Process",
                side_effect=psutil.NoSuchProcess(123),
            ),
            patch.object(
                source_audio_process.os,
                "killpg",
                side_effect=ProcessLookupError,
                create=True,
            ),
        ):
            owner = SourceAudioProcess(
                ["publisher"], popen_factory=Mock(return_value=process)
            )
            owner.close()
        self.assertFalse(owner._directory.exists())
        process.wait.assert_called_once_with(timeout=5)

    def test_windows_worker_is_assigned_before_resume_and_drained_before_removal(self):
        operations = Mock()
        process = Mock(pid=123)
        process.poll.side_effect = (None, None)
        identity = Mock()
        identity.children.return_value = []
        job = Mock()
        operations.attach_mock(job, "job")
        operations.attach_mock(identity, "identity")
        operations.attach_mock(process, "process")
        factory = Mock(return_value=process)
        with (
            patch.object(source_audio_process.sys, "platform", "win32"),
            patch.object(source_audio_process, "RecorderJob", return_value=job),
            patch.object(source_audio_process.psutil, "Process", return_value=identity),
        ):
            owner = SourceAudioProcess(["publisher"], popen_factory=factory)
            self.assertEqual(factory.call_args.kwargs["creationflags"], 0x4)
            self.assertNotIn("start_new_session", factory.call_args.kwargs)
            self.assertEqual(
                operations.mock_calls[:2],
                [call.job.assign(process), call.identity.resume()],
            )
            owner.close()
        self.assertFalse(owner._directory.exists())
        calls = operations.mock_calls
        self.assertLess(
            calls.index(call.identity.suspend()), calls.index(call.job.close())
        )
        self.assertLess(
            calls.index(call.job.close()), calls.index(call.process.wait(timeout=5))
        )

    def test_windows_assignment_and_job_cleanup_failure_still_drains_the_worker(self):
        error = OSError("assignment failed")
        process = Mock(pid=123)
        process.poll.return_value = None
        identity = Mock()
        identity.children.return_value = []
        job = Mock()
        job.assign.side_effect = error
        job.close.side_effect = OSError("job cleanup failed")
        factory = Mock(return_value=process)
        with (
            patch.object(source_audio_process.sys, "platform", "win32"),
            patch.object(source_audio_process, "RecorderJob", return_value=job),
            patch.object(source_audio_process.psutil, "Process", return_value=identity),
            self.assertLogs("vntts.cleanup", level="ERROR"),
            self.assertRaises(OSError) as raised,
        ):
            SourceAudioProcess(["publisher"], popen_factory=factory)
        directory = Path(factory.call_args.kwargs["env"]["TMPDIR"])
        try:
            self.assertIs(raised.exception, error)
            identity.resume.assert_not_called()
            process.kill.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=5)
            process.communicate.assert_called_once_with(timeout=5)
            self.assertIn("job cleanup failed", error.__notes__[0])
            self.assertTrue(directory.is_dir())
        finally:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()

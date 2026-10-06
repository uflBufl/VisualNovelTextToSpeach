import subprocess
import sys
import unittest
from threading import Event
from unittest.mock import Mock, patch

from vntts.runtime_preparation import run_runtime_command
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError


class RuntimePreparationTest(unittest.TestCase):
    def test_subprocess_cancellation_drains_and_terminates_child(self):
        cancellation = Event()
        process = Mock(returncode=None)
        process.poll.return_value = None

        def wait(*_args, **_kwargs):
            cancellation.set()
            raise subprocess.TimeoutExpired("uv", 0.1)

        process.communicate.side_effect = wait
        with (
            patch("vntts.runtime_preparation.subprocess.Popen", return_value=process),
            patch("vntts.runtime_preparation.terminate_process") as terminate,
            self.assertRaisesRegex(TTSSynthesisError, "cancelled"),
        ):
            run_runtime_command(["uv"], cancellation=cancellation)
        terminate.assert_called_once_with(process)

    def test_subprocess_stderr_is_only_included_when_requested(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        process.communicate.return_value = (b"stdout\n", b"stderr\n")
        with patch("vntts.runtime_preparation.subprocess.Popen", return_value=process):
            self.assertEqual(
                run_runtime_command(["tool"], cancellation=None), b"stdout\n"
            )
            self.assertEqual(
                run_runtime_command(["tool"], cancellation=None, include_stderr=True),
                b"stdout\nstderr\n",
            )

    def test_real_command_drains_both_pipes_and_supplies_stdin(self):
        payload = b"input" * 40000
        output = run_runtime_command(
            [
                sys.executable,
                "-c",
                "import sys; data=sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(data); sys.stderr.buffer.write(b'e' * 200000)",
            ],
            cancellation=None,
            input_bytes=payload,
            include_stderr=True,
            timeout=10,
        )
        self.assertEqual(output, payload + b"e" * 200000)

    def test_timeout_preserves_child_claim_and_stops_process(self):
        process = Mock(returncode=None)
        process.poll.return_value = None
        use = Mock()
        with (
            patch("vntts.runtime_preparation.subprocess.Popen", return_value=process),
            patch("vntts.runtime_preparation.monotonic", side_effect=(0, 2)),
            patch("vntts.runtime_preparation.terminate_process") as terminate,
            self.assertRaisesRegex(TTSConfigurationError, "timed out"),
        ):
            run_runtime_command(["tool"], cancellation=None, timeout=1, runtime_use=use)
        use.begin_launch.assert_called_once_with()
        use.launched.assert_called_once_with(process)
        terminate.assert_called_once_with(process)
        process.communicate.assert_not_called()

    def test_failed_launch_clears_launch_marker_without_claiming_child(self):
        use = Mock()
        with (
            patch(
                "vntts.runtime_preparation.subprocess.Popen",
                side_effect=OSError("missing"),
            ),
            self.assertRaisesRegex(OSError, "missing"),
        ):
            run_runtime_command(["tool"], cancellation=None, runtime_use=use)
        use.begin_launch.assert_called_once_with()
        use.launched.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main()

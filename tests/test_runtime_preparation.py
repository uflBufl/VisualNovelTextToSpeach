import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from vntts.runtime_ownership import RuntimeUse, read_record
from vntts.runtime_preparation import RuntimePreparationCancelled, run_runtime_command
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
            patch("vntts.subprocess_utils.terminate_process") as terminate,
            self.assertRaisesRegex(TTSSynthesisError, "cancelled") as caught,
        ):
            run_runtime_command(["uv"], cancellation=cancellation)
        terminate.assert_called_once_with(process)
        self.assertIsInstance(caught.exception, RuntimePreparationCancelled)
        self.assertEqual(
            str(caught.exception),
            "Speech runtime preparation cancelled. Retry when ready.",
        )

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
            patch("vntts.subprocess_utils.terminate_process") as terminate,
            self.assertRaisesRegex(TTSConfigurationError, "timed out"),
        ):
            run_runtime_command(["tool"], cancellation=None, timeout=1, runtime_use=use)
        use.begin_launch.assert_called_once_with()
        use.launched.assert_called_once_with(process)
        terminate.assert_called_once_with(process)
        process.communicate.assert_not_called()

    def test_post_launch_registration_failure_keeps_primary_error_and_stops_child(self):
        process = Mock(returncode=None)
        process.poll.return_value = None
        primary = OSError("child claim unavailable")
        use = Mock()
        use.launched.side_effect = primary
        cleanup_error = ValueError("termination failed")
        cleanup_error.add_note("termination context")
        with (
            patch("vntts.runtime_preparation.subprocess.Popen", return_value=process),
            patch("vntts.runtime_preparation.monotonic") as clock,
            patch(
                "vntts.subprocess_utils.terminate_process", side_effect=cleanup_error
            ) as terminate,
            self.assertRaises(OSError) as raised,
        ):
            run_runtime_command(["tool"], cancellation=None, runtime_use=use)
        self.assertIs(raised.exception, primary)
        use.launched.assert_called_once_with(process)
        clock.assert_not_called()
        terminate.assert_called_once_with(process)
        self.assertEqual(primary.__notes__[-1], "termination context")

    def test_post_launch_clock_failure_registers_and_reaps_real_child_before_claim_closure(
        self,
    ):
        native_popen = subprocess.Popen
        children = []
        primary = OSError("clock unavailable")
        with TemporaryDirectory() as directory:
            use = RuntimeUse(Path(directory))

            def spawn(*args, **kwargs):
                child = native_popen(*args, **kwargs)
                children.append(child)
                return child

            def clock():
                self.assertEqual(use.processes, children)
                self.assertEqual(read_record(use.path)["children"], [children[0].pid])
                self.assertFalse(read_record(use.path)["launching"])
                raise primary

            try:
                with (
                    patch(
                        "vntts.runtime_preparation.subprocess.Popen", side_effect=spawn
                    ),
                    patch("vntts.runtime_preparation.monotonic", side_effect=clock),
                    self.assertRaises(OSError) as raised,
                ):
                    run_runtime_command(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        cancellation=None,
                        runtime_use=use,
                    )
                self.assertIs(raised.exception, primary)
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                self.assertTrue(children[0].stdout.closed)
                self.assertTrue(children[0].stderr.closed)
                use.close()
                self.assertFalse(use.path.exists())
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)
                use.close()

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

    def test_failed_launch_preserves_spawn_error_when_reset_fails(self):
        use = Mock()
        use.launched.side_effect = OSError("launch marker unavailable")
        with (
            patch(
                "vntts.runtime_preparation.subprocess.Popen",
                side_effect=OSError("missing executable"),
            ),
            self.assertRaisesRegex(OSError, "missing executable") as raised,
        ):
            run_runtime_command(["tool"], cancellation=None, runtime_use=use)

        self.assertTrue(
            any(
                "Speech runtime launch reset failed" in note
                for note in raised.exception.__notes__
            )
        )


if __name__ == "__main__":
    unittest.main()

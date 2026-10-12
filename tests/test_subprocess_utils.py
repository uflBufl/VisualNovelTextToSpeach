import subprocess
import sys
import unittest
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, call, patch

from vntts import (
    game_content_importer,
    pregeneration_generation,
    prepared_sequence,
    runtime_preparation,
)
from vntts.services.tts_engine import TTSSynthesisError
from vntts.subprocess_utils import terminate_process, terminate_running_process


class TerminateProcessTest(unittest.TestCase):
    def test_cleanup_remains_bounded_when_termination_stalls(self):
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired("worker", 1)
        process.wait.side_effect = subprocess.TimeoutExpired("worker", 1)

        terminate_process(process, timeout=1)

        process.terminate.assert_called_once_with()
        process.communicate.assert_called_once_with(timeout=1)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1)

    def test_cancellation_stops_a_resistant_child_once_across_captured_runners(self):
        with TemporaryDirectory() as directory:
            cases = (
                (
                    prepared_sequence,
                    prepared_sequence.PreparedSequenceCancelled,
                    lambda factory, event: prepared_sequence._run(
                        ["publisher"], event, factory
                    ),
                ),
                (
                    game_content_importer,
                    game_content_importer.GameContentImportCancelled,
                    lambda factory, event: (
                        game_content_importer.Reverse1999GameImporter(
                            output_root=directory,
                            installation_file=Path(directory) / "installation.json",
                            popen_factory=factory,
                        )._run(["publisher"], event)
                    ),
                ),
                (
                    pregeneration_generation,
                    pregeneration_generation.OfflineGenerationCancelled,
                    lambda factory, event: (
                        pregeneration_generation.OfflineGenerationWorker(
                            popen_factory=factory
                        )._run_subprocess(["publisher"], event)
                    ),
                ),
            )
            for module, error_type, run in cases:
                with self.subTest(runner=module.__name__):
                    event = Event()
                    process = Mock()
                    process.poll.return_value = None
                    operations = []

                    def communicate(timeout=None):
                        event.set()
                        raise subprocess.TimeoutExpired("publisher", timeout)

                    process.communicate.side_effect = communicate
                    with (
                        patch(
                            "vntts.subprocess_utils.terminate_process",
                            side_effect=lambda _process: operations.append("stop"),
                        ) as stop,
                        patch(
                            "vntts.support.record_game_import",
                            side_effect=lambda stage, **_details: operations.append(
                                stage
                            ),
                        ) as record,
                        self.assertRaises(error_type),
                    ):
                        run(Mock(return_value=process), event)

                    stop.assert_called_once_with(process)
                    if module is game_content_importer:
                        self.assertEqual(
                            operations, ["process-start", "stop", "process-exit"]
                        )
                        self.assertEqual(
                            record.call_args.kwargs["outcome"], "cancelled"
                        )
                        self.assertTrue(record.call_args.kwargs["cancelled"])

    def test_running_predicate_terminates_only_an_unfinished_process(self):
        for returncode in (None, 0, 1):
            with self.subTest(returncode=returncode):
                process = Mock()
                process.poll.return_value = returncode
                with patch("vntts.subprocess_utils.terminate_process") as stop:
                    terminate_running_process(process)
                process.poll.assert_called_once_with()
                self.assertEqual(
                    stop.call_args_list, [call(process)] if returncode is None else []
                )

    def test_captured_runner_cleanup_preserves_primary_errors_and_releases_ownership(
        self,
    ):
        with TemporaryDirectory() as directory:
            factory = Mock()
            importer = game_content_importer.Reverse1999GameImporter(
                output_root=directory,
                installation_file=Path(directory) / "installation.json",
                popen_factory=factory,
            )
            worker = pregeneration_generation.OfflineGenerationWorker(
                popen_factory=factory
            )
            runners = (
                (
                    prepared_sequence,
                    prepared_sequence.PreparedSequenceCancelled,
                    lambda event: prepared_sequence._run(["publisher"], event, factory),
                ),
                (
                    game_content_importer,
                    game_content_importer.GameContentImportCancelled,
                    lambda event: importer._run(["publisher"], event),
                ),
                (
                    pregeneration_generation,
                    pregeneration_generation.OfflineGenerationCancelled,
                    lambda event: worker._run_subprocess(["publisher"], event),
                ),
                (
                    runtime_preparation,
                    TTSSynthesisError,
                    lambda event: runtime_preparation.run_runtime_command(
                        ["publisher"], cancellation=event
                    ),
                ),
            )
            for (module, cancelled_error, run), outcome in product(
                runners, ("raw", "cancelled", "success")
            ):
                with self.subTest(runner=module.__name__, outcome=outcome):
                    event = Event()
                    process = Mock(returncode=0)
                    process.poll.return_value = None
                    factory.return_value = process
                    primary = OSError("captured operation failed")
                    primary.add_note("operation context")
                    cleanup_error = ValueError("termination failed")
                    cleanup_error.add_note("termination context")
                    operations, observed_primary = [], []

                    def communicate(*_args, **_kwargs):
                        event.set()
                        raise subprocess.TimeoutExpired("publisher", 0.1)

                    def stop(_process):
                        operations.append("stop")
                        observed_primary.append(sys.exception())
                        raise cleanup_error

                    process.communicate.return_value = ("", "")
                    process.communicate.side_effect = {
                        "raw": primary,
                        "cancelled": communicate,
                        "success": None,
                    }[outcome]
                    expected_error = {
                        "raw": OSError,
                        "cancelled": cancelled_error,
                        "success": ValueError,
                    }[outcome]
                    with (
                        patch("vntts.runtime_preparation.subprocess.Popen", factory),
                        patch(
                            "vntts.subprocess_utils.terminate_process", side_effect=stop
                        ) as terminate,
                        patch(
                            "vntts.support.record_game_import",
                            side_effect=lambda stage, **_details: operations.append(
                                stage
                            ),
                        ) as record,
                        self.assertRaises(expected_error) as raised,
                    ):
                        run(event)
                    expected = {
                        "raw": primary,
                        "cancelled": observed_primary[0],
                        "success": cleanup_error,
                    }[outcome]
                    self.assertIs(raised.exception, expected)
                    terminate.assert_called_once_with(process)
                    self.assertIsNone(worker._process)
                    if outcome != "success":
                        self.assertIn(
                            "process cleanup failed: termination failed",
                            raised.exception.__notes__[-2],
                        )
                        self.assertEqual(
                            raised.exception.__notes__[-1], "termination context"
                        )
                    if outcome == "raw":
                        self.assertEqual(
                            raised.exception.__notes__[0], "operation context"
                        )
                    if module is game_content_importer:
                        expected_operations = {
                            "raw": ["process-start", "stop"],
                            "success": ["process-start", "stop"],
                            "cancelled": ["process-start", "stop", "process-exit"],
                        }[outcome]
                        self.assertEqual(operations, expected_operations)
                        if outcome == "cancelled":
                            self.assertEqual(
                                record.call_args.kwargs["outcome"], "cancelled"
                            )
                            self.assertTrue(record.call_args.kwargs["cancelled"])

    def test_broken_output_pipes_fall_back_to_wait_for_reaping(self):
        process = Mock()
        process.communicate.side_effect = OSError("broken pipe")

        terminate_process(process, timeout=1)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1)


if __name__ == "__main__":
    unittest.main()

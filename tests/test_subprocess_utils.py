import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from vntts import game_content_importer, pregeneration_generation, prepared_sequence
from vntts.subprocess_utils import terminate_process


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
                        patch.object(
                            module,
                            "terminate_process",
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

    def test_broken_output_pipes_fall_back_to_wait_for_reaping(self):
        process = Mock()
        process.communicate.side_effect = OSError("broken pipe")

        terminate_process(process, timeout=1)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1)


if __name__ == "__main__":
    unittest.main()

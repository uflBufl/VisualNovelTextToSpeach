import subprocess
import unittest
from unittest.mock import Mock

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

    def test_broken_output_pipes_fall_back_to_wait_for_reaping(self):
        process = Mock()
        process.communicate.side_effect = OSError("broken pipe")

        terminate_process(process, timeout=1)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1)


if __name__ == "__main__":
    unittest.main()

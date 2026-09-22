import subprocess
import unittest
from unittest.mock import Mock, call

from vntts.subprocess_utils import terminate_process


class TerminateProcessTest(unittest.TestCase):
    def test_cleanup_remains_bounded_when_kill_does_not_close_pipes(self):
        process = Mock()
        process.communicate.side_effect = (
            subprocess.TimeoutExpired("worker", 1),
            subprocess.TimeoutExpired("worker", 1),
        )

        terminate_process(process, timeout=1)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(
            process.communicate.call_args_list,
            [call(timeout=1), call(timeout=1)],
        )


if __name__ == "__main__":
    unittest.main()

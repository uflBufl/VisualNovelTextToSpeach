"""Qualify task completion after actual QApplication native teardown."""

import os
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import Mock, patch

from vntts.async_ui import _Task


class TaskSignalLifetimeTest(unittest.TestCase):
    def test_live_signal_error_is_not_hidden(self):
        signals = Mock()
        failure = RuntimeError("unrelated delivery failure")
        signals.finished.emit.side_effect = failure
        task = _Task(1, lambda: "done", signals)
        with patch("vntts.async_ui.isValid", return_value=True):
            with self.assertRaises(RuntimeError) as caught:
                task.run()
        self.assertIs(caught.exception, failure)

    def test_native_owner_retirement_during_emit_discards_delivery(self):
        signals = Mock()
        signals.finished.emit.side_effect = RuntimeError(
            "Signal source has been deleted"
        )
        task = _Task(1, lambda: "done", signals)
        with patch("vntts.async_ui.isValid", side_effect=[True, False]):
            task.run()
        signals.finished.emit.assert_called_once_with(1, "done", None)

    def test_application_teardown_retires_pending_task_delivery(self):
        script = textwrap.dedent("""
            from PySide6.QtCore import QCoreApplication, QObject
            from shiboken6 import delete, isValid
            from tests.qt_task_fixtures import ManualThreadPool
            from vntts.async_ui import LatestTaskRunner
            app = QCoreApplication([])
            pool = ManualThreadPool()
            runner = LatestTaskRunner(thread_pool=pool)
            received = []
            calls = []
            runner.finished.connect(lambda result, error: received.append(result))
            runner.start(lambda: calls.append("finished"))
            task = pool.tasks.pop()
            delete(app)
            assert not isValid(task.signals)
            task.run()
            assert calls == ["finished"]
            assert received == []
            print("retired completion")
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retired completion", result.stdout)
        self.assertNotIn("Signal source has been deleted", result.stderr)

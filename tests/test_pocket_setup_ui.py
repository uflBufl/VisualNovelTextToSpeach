import os
import unittest
from threading import Event, Thread
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QThread  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from shiboken6 import isValid  # noqa: E402

from tests.error_logging_fixtures import isolated_application_error_log  # noqa: E402
from tests.pocket_model_fixtures import pocket_model_readiness  # noqa: E402
from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.pocket_setup_ui import PocketSetupControl  # noqa: E402


class PocketSetupControlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.enterContext(isolated_application_error_log())
        self.readiness = self.enterContext(pocket_model_readiness(False))
        self.pool = ManualThreadPool()
        self.enterContext(
            patch("vntts.async_ui.QThreadPool.globalInstance", return_value=self.pool)
        )
        self.browser = self.enterContext(
            patch("vntts.pocket_setup_ui.QDesktopServices.openUrl")
        )
        self.control = PocketSetupControl()
        self.addCleanup(self._delete_control)

    def test_browser_auth_and_terms_continue_run_without_blocking_the_ui(self):
        sign_in = Event()
        terms = Event()
        finished = Event()
        worker_thread = []
        browser_threads = []
        changes = []
        self.control.readyChanged.connect(changes.append)
        self.browser.side_effect = lambda _url: browser_threads.append(
            QThread.currentThread()
        )

        def prepare(*, cancel_event, continue_event, progress, authorize):
            worker_thread.append(QThread.currentThread())
            progress("Waiting for browser sign-in")
            authorize("https://huggingface.co/device", "ABCD-EFGH")
            sign_in.set()
            self.assertTrue(terms.wait(3))
            authorize("https://huggingface.co/kyutai/pocket-tts", "")
            self.assertTrue(continue_event.wait(3))
            self.assertFalse(cancel_event.is_set())
            progress("Downloading original model")
            self.readiness.return_value = True
            finished.set()

        with patch("vntts.pocket_setup_ui.prepare_pocket_model", side_effect=prepare):
            self.control.button.click()
            dialog = self.control._dialog
            self.assertTrue(dialog.isModal())
            thread = Thread(target=self.pool.run_next)
            thread.start()
            try:
                self.assertTrue(sign_in.wait(3))
                self.application.processEvents()
                self.assertIn("ABCD-EFGH", dialog.code.text())
                self.assertTrue(dialog.continue_button.isHidden())
                terms.set()
                self._wait_until(lambda: not dialog.continue_button.isHidden())
                dialog.continue_button.click()
                self.assertTrue(finished.wait(3))
            finally:
                terms.set()
                dialog.continue_event.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())
            self._wait_until(lambda: not dialog.runner.active)
            self.assertTrue(self.control.is_ready)
            self.assertFalse(self.control.button.isEnabled())
            self.assertEqual(changes, [True])
            self.assertTrue(browser_threads)
            self.assertTrue(
                all(value == self.application.thread() for value in browser_threads)
            )
            self.assertNotEqual(worker_thread[0], self.application.thread())
            dialog.close()

    def test_failed_browser_launch_shows_a_copyable_address(self):
        self.browser.return_value = False
        self.control.button.click()
        dialog = self.control._dialog
        dialog._authorize("https://huggingface.co/oauth/device", "ABCD-EFGH")
        self.assertIn("Unable to open", dialog.status.text())
        self.assertIn("https://huggingface.co/oauth/device", dialog.status.text())
        self.assertIn("ABCD-EFGH", dialog.code.text())
        dialog.close()
        self.pool.run_next()

    def test_closing_owner_cancels_worker_and_ignores_late_auth(self):
        callbacks = {}

        def prepare(**arguments):
            callbacks.update(arguments)

        with patch("vntts.pocket_setup_ui.prepare_pocket_model", side_effect=prepare):
            self.control.button.click()
            self.pool.run_next()
            self.control.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(callbacks["cancel_event"].is_set())
            callbacks["authorize"]("https://huggingface.co/device", "late-code")
            callbacks["progress"]("late update")
            self.application.processEvents()
            self.browser.assert_not_called()

    def test_closing_owner_cancels_pending_setup(self):
        with patch("vntts.pocket_setup_ui.prepare_pocket_model"):
            self.control.show()
            self.control.button.click()
            dialog = self.control._dialog
            self.control.close()
            self.assertTrue(dialog.cancel_event.is_set())
            self.assertFalse(dialog.runner.active)
            self.pool.run_next()
            self.application.processEvents()
            self.browser.assert_not_called()

    def test_failure_is_visible_and_setup_can_be_retried(self):
        with patch(
            "vntts.pocket_setup_ui.prepare_pocket_model",
            side_effect=ValueError("Account does not have model access"),
        ):
            self.control.button.click()
            dialog = self.control._dialog
            self.pool.run_next()
            self.application.processEvents()
            self.assertIn("does not have model access", dialog.status.text())
            self.assertFalse(self.control.is_ready)
            dialog.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.control.button.click()
            self.assertIsNot(self.control._dialog, dialog)
            self.control._dialog.close()

    def _delete_control(self):
        if isValid(self.control):
            self.control.deleteLater()

    def _wait_until(self, predicate):
        for _ in range(300):
            if predicate():
                return
            QTest.qWait(10)
        self.fail("Pocket setup did not update")

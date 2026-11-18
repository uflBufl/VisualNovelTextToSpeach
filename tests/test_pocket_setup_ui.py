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

    def test_browser_retry_and_terms_continue_without_blocking_the_ui(self):
        terms = Event()
        browser_calls = []
        sign_in_url = "https://huggingface.co/device"
        terms_url = "https://huggingface.co/kyutai/pocket-tts"
        changes = []
        self.control.readyChanged.connect(changes.append)

        def open_browser(url):
            browser_calls.append((url.toString(), QThread.currentThread()))
            return len(browser_calls) > 1

        self.browser.side_effect = open_browser

        def prepare(*, cancel_event, continue_event, progress, authorize):
            progress("Waiting for browser sign-in")
            authorize(sign_in_url, "ABCD-EFGH")
            self.assertTrue(terms.wait(3))
            authorize(terms_url, "")
            self.assertTrue(continue_event.wait(3))
            self.assertFalse(cancel_event.is_set())
            progress("Downloading original model")
            self.readiness.return_value = True

        with patch("vntts.pocket_setup_ui.prepare_pocket_model", side_effect=prepare):
            self.control.button.click()
            dialog = self.control._dialog
            self.assertTrue(dialog.isModal())
            thread = Thread(target=self.pool.run_next)
            thread.start()
            try:
                self._wait_until(lambda: "Unable to open" in dialog.status.text())
                self.assertIn(sign_in_url, dialog.status.text())
                self.assertFalse(dialog.browser_button.isHidden())
                dialog.browser_button.click()
                self.assertIn("ABCD-EFGH", dialog.code.text())
                self.assertTrue(dialog.continue_button.isHidden())
                terms.set()
                self._wait_until(lambda: not dialog.continue_button.isHidden())
                dialog.continue_button.click()
                self._wait_until(lambda: not dialog.runner.active)
            finally:
                terms.set()
                dialog.continue_event.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())
            self.assertTrue(self.control.is_ready)
            self.assertFalse(self.control.button.isEnabled())
            self.assertEqual(changes, [True])
            self.assertEqual(
                browser_calls,
                [
                    (url, self.application.thread())
                    for url in (sign_in_url, sign_in_url, terms_url)
                ],
            )
            dialog.close()

    def test_deleting_owner_cancels_inflight_setup_and_ignores_late_auth(self):
        started = Event()
        finished = Event()

        def prepare(*, cancel_event, continue_event, progress, authorize):
            started.set()
            if cancel_event.wait(3):
                authorize("https://huggingface.co/device", "late-code")
                progress("late update")
                finished.set()

        with patch("vntts.pocket_setup_ui.prepare_pocket_model", side_effect=prepare):
            self.control.button.click()
            cancel_event = self.control._dialog.cancel_event
            thread = Thread(target=self.pool.run_next)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                self.control.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertTrue(cancel_event.is_set())
                self.assertTrue(finished.wait(3))
            finally:
                cancel_event.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
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

    def test_failure_can_be_retried_in_same_dialog_with_fresh_events(self):
        attempts = []
        changes = []
        self.control.readyChanged.connect(changes.append)

        def prepare(**arguments):
            attempts.append(arguments)
            if len(attempts) == 1:
                arguments["continue_event"].set()
                raise ValueError("Account does not have model access")
            self.assertFalse(arguments["cancel_event"].is_set())
            self.assertFalse(arguments["continue_event"].is_set())
            self.readiness.return_value = True

        with patch(
            "vntts.pocket_setup_ui.prepare_pocket_model",
            side_effect=prepare,
        ):
            self.control.button.click()
            dialog = self.control._dialog
            self.pool.run_next()
            self.application.processEvents()
            self.assertIn("does not have model access", dialog.status.text())
            self.assertFalse(self.control.is_ready)
            self.assertFalse(dialog.retry_button.isHidden())
            self.assertEqual(dialog.cancel_button.text(), "Close")
            self.control.button.click()
            self.assertIs(self.control._dialog, dialog)
            dialog.retry_button.click()
            self.assertTrue(attempts[0]["cancel_event"].is_set())
            self.assertIsNot(dialog.cancel_event, attempts[0]["cancel_event"])
            self.assertIsNot(dialog.continue_event, attempts[0]["continue_event"])
            self.assertTrue(dialog.retry_button.isHidden())
            self.assertFalse(dialog.progress.isHidden())
            self.assertEqual(dialog.cancel_button.text(), "Cancel")
            self.pool.run_next()
            self.application.processEvents()
            self.assertTrue(self.control.is_ready)
            self.assertFalse(self.control.button.isEnabled())
            self.assertTrue(dialog.retry_button.isHidden())
            self.assertEqual(changes, [True])
            dialog.close()

    def test_retry_ignores_queued_progress_and_auth_from_failed_attempt(self):
        attempts = []

        def prepare(**arguments):
            attempts.append(arguments)
            raise ValueError("The sign-in code expired")

        with patch("vntts.pocket_setup_ui.prepare_pocket_model", side_effect=prepare):
            self.control.button.click()
            dialog = self.control._dialog
            self.pool.run_next()
            self.application.processEvents()
            callbacks = attempts[0]
            failure_message = dialog.status.text()
            callbacks["progress"]("late progress after failure")
            callbacks["authorize"]("https://huggingface.co/device", "OLD-CODE")
            self.assertEqual(dialog.status.text(), failure_message)
            self.browser.assert_not_called()

            def publish_stale_updates():
                callbacks["progress"]("stale progress")
                callbacks["authorize"]("https://huggingface.co/device", "OLD-CODE")

            thread = Thread(target=publish_stale_updates)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            dialog.retry_button.click()
            self.application.processEvents()
            self.assertEqual(
                dialog.status.text(), "Preparing the original Pocket model..."
            )
            self.assertEqual(dialog.code.text(), "")
            self.assertTrue(dialog.browser_button.isHidden())
            self.browser.assert_not_called()
            dialog.close()
            self.pool.run_next()

    def test_incomplete_setup_offers_retry_without_reopening(self):
        with patch("vntts.pocket_setup_ui.prepare_pocket_model"):
            self.control.button.click()
            dialog = self.control._dialog
            self.pool.run_next()
            self.application.processEvents()
            self.assertIn("did not finish", dialog.status.text())
            self.assertFalse(dialog.retry_button.isHidden())
            dialog.retry_button.click()
            self.assertTrue(dialog.runner.active)
            dialog.close()
            self.pool.run_next()

    def test_closing_or_hiding_owner_cancels_retry_and_ignores_late_updates(self):
        for action in ("close", "hide_owner", "reject"):
            with self.subTest(action=action):
                attempts = []
                changes = []

                def prepare(**arguments):
                    attempts.append(arguments)
                    if len(attempts) == 1:
                        raise ValueError("The sign-in code expired")
                    arguments["progress"]("late progress")
                    arguments["authorize"]("https://huggingface.co/device", "LATE-CODE")
                    self.readiness.return_value = True

                self.control.show()
                self.control.readyChanged.connect(changes.append)
                with patch(
                    "vntts.pocket_setup_ui.prepare_pocket_model", side_effect=prepare
                ):
                    self.control.button.click()
                    dialog = self.control._dialog
                    self.pool.run_next()
                    self.application.processEvents()
                    dialog.retry_button.click()
                    cancel_event = dialog.cancel_event
                    if action == "hide_owner":
                        self.control.hide()
                    elif action == "reject":
                        dialog.reject()
                    else:
                        dialog.close()
                    self.assertTrue(cancel_event.is_set())
                    self.assertFalse(dialog.runner.active)
                    self.pool.run_next()
                    self.application.processEvents()
                    self.browser.assert_not_called()
                    self.assertEqual(changes, [])
                self.control.readyChanged.disconnect(changes.append)
                self.readiness.return_value = False
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _delete_control(self):
        if isValid(self.control):
            self.control.deleteLater()

    def _wait_until(self, predicate):
        for _ in range(300):
            if predicate():
                return
            QTest.qWait(10)
        self.fail("Pocket setup did not update")

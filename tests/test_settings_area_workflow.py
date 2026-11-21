import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QEventLoop, QObject, QRect, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.pocket_model_fixtures import pocket_model_readiness  # noqa: E402
from vntts.app import SettingsDialog, TrayApplication  # noqa: E402
from vntts.configuration_apply import ConfigurationApplyMixin  # noqa: E402
from vntts.ocr import DialogRegion  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.voice_library import VoiceLibrary  # noqa: E402
from vntts.window_capture import WindowGeometry  # noqa: E402


class WorkflowOwner(QObject):
    _edit_settings = ConfigurationApplyMixin._edit_settings
    _pick_settings_area = TrayApplication._pick_settings_area
    _open_calibration_overlay = TrayApplication._open_calibration_overlay
    _calibration_overlay_closed = TrayApplication._calibration_overlay_closed
    restore_control_window = TrayApplication.restore_control_window

    def __init__(self):
        super().__init__()
        self._calibration_capture_pending = False
        self.calibration_overlay = None
        self._controller_busy = False
        self._shutting_down = False
        self._calibration_visibility = None
        self._settings_area_dialog = None
        self.diagnostics_dialog = None
        self.controller = Mock()
        self.settings = AppSettings()
        self.set_status = Mock()
        self.show_error = Mock()

    def _begin_calibration(self, story_area, capture_mode):
        self._calibrating_story_area = story_area
        self._open_calibration_overlay(
            WindowGeometry(100, 100, 640, 480), Image.new("RGB", (640, 480), "white")
        )


class SettingsAreaWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_picker_returns_to_same_settings_with_draft_and_focus(self):
        with TemporaryDirectory() as directory, pocket_model_readiness(False):
            dialog = SettingsDialog(
                AppSettings(screenshot_directory=directory),
                voice_library=VoiceLibrary(Path(directory) / "voices"),
            )
            owner = WorkflowOwner()
            loop = QEventLoop()
            observations = []
            dialogue = DialogRegion(0.1, 0.7, 0.8, 0.2)
            story = DialogRegion(0, 0, 1, 1)

            def observe_return():
                observations.append(
                    (
                        dialog.isVisible(),
                        dialog.section_navigation.currentIndex(),
                        dialog.screenshot_directory.text(),
                        dialog.focusWidget(),
                    )
                )

            def after_cancel():
                observe_return()
                dialog.dialogue_area_button.click()
                QTimer.singleShot(0, confirm_dialogue)

            def confirm_dialogue():
                overlay = owner.calibration_overlay
                overlay._review_rectangle(QRect(64, 336, 512, 96))
                overlay._review.accept()
                QTimer.singleShot(0, after_dialogue)

            def after_dialogue():
                observe_return()
                dialog.story_area_button.click()
                QTimer.singleShot(0, confirm_story)

            def confirm_story():
                overlay = owner.calibration_overlay
                overlay._review_rectangle(QRect(0, 0, 640, 480))
                overlay._review.accept()
                QTimer.singleShot(0, finish)

            def finish():
                observe_return()
                dialog.reject()
                loop.quit()

            def start():
                dialog.screenshot_directory.setText("unsaved")
                dialog.dialogue_area_button.setFocus()
                dialog.dialogue_area_button.click()
                QTimer.singleShot(0, cancel)

            def cancel():
                QTest.keyClick(owner.calibration_overlay, Qt.Key.Key_Escape)
                QTimer.singleShot(0, after_cancel)

            QTimer.singleShot(0, lambda: owner._edit_settings(dialog))
            QTimer.singleShot(0, start)

            def abort():
                owner._shutting_down = True
                if owner.calibration_overlay is not None:
                    owner.calibration_overlay.close()
                dialog.reject()
                loop.quit()

            watchdog = QTimer()
            watchdog.setSingleShot(True)
            watchdog.timeout.connect(abort)
            watchdog.start(5000)
            loop.exec()
            watchdog.stop()
            self.assertEqual(dialog.edited_capture_areas(), (dialogue, story))
            self.assertEqual(len(observations), 3)
            for (visible, section, text, focus), button in zip(
                observations,
                (
                    dialog.dialogue_area_button,
                    dialog.dialogue_area_button,
                    dialog.story_area_button,
                ),
                strict=True,
            ):
                self.assertTrue(visible)
                self.assertEqual(section, 1)
                self.assertEqual(text, "unsaved")
                self.assertIs(focus, button)
            owner.show_error.assert_not_called()
            dialog.deleteLater()

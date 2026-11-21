import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from tests.pocket_model_fixtures import pocket_model_readiness  # noqa: E402
from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.app import SettingsDialog, TrayApplication  # noqa: E402
from vntts.configuration_apply import ConfigurationApplyMixin  # noqa: E402
from vntts.ocr import DialogRegion, save_dialog_region  # noqa: E402
from vntts.profiles import GameProfileStore  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.voice_library import VoiceLibrary  # noqa: E402


class SettingsCaptureAreasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.root = Path(directory)
        self.enterContext(pocket_model_readiness(False))
        self.enterContext(
            patch.dict(
                os.environ,
                {"VNTTS_DIALOG_REGION_FILE": str(self.root / "region.json")},
            )
        )
        self.library = VoiceLibrary(self.root / "voices")

    def dialog(self, **kwargs):
        dialog = SettingsDialog(
            AppSettings(screenshot_directory=str(self.root)),
            voice_library=self.library,
            **kwargs,
        )
        self.addCleanup(self.delete_dialog, dialog)
        return dialog

    @staticmethod
    def delete_dialog(dialog):
        dialog.reject()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)

    def test_capture_areas_are_adjacent_and_window_selector_tracks_source(self):
        dialog = self.dialog()
        dialog.show()
        self.application.processEvents()
        form = dialog.capture_form
        labels = [
            item.widget().text()
            for row in range(form.rowCount())
            if (item := form.itemAt(row, form.ItemRole.LabelRole)) is not None
        ]
        self.assertEqual(labels[labels.index("Dialogue area") + 1], "Story area")
        self.assertFalse(dialog.game_window.isVisibleTo(dialog))
        self.assertFalse(dialog.refresh_windows_button.isVisibleTo(dialog))
        dialog.capture_mode.setCurrentIndex(dialog.capture_mode.findData("window"))
        self.assertTrue(dialog.game_window.isVisibleTo(dialog))
        self.assertTrue(dialog.refresh_windows_button.isVisibleTo(dialog))
        dialog.capture_mode.setCurrentIndex(dialog.capture_mode.findData("screen"))
        self.assertFalse(dialog.game_window.isVisibleTo(dialog))

    def test_area_summaries_describe_saved_selection_and_changed_source(self):
        dialog = self.dialog(
            dialogue_region=DialogRegion(0, 0.7, 1, 0.3),
            story_region=DialogRegion(0, 0, 1, 1),
        )
        self.assertIn("top 70%", dialog.dialogue_area_summary.text())
        self.assertIn("height 100%", dialog.story_area_summary.text())
        self.assertEqual(dialog.dialogue_area_button.text(), "Adjust dialogue area...")
        dialog.capture_mode.setCurrentIndex(dialog.capture_mode.findData("window"))
        self.assertEqual(dialog.dialogue_area_button.text(), "Set dialogue area...")
        self.assertIn("Not set", dialog.dialogue_area_summary.text())
        self.assertIn("Not set", dialog.story_area_summary.text())
        dialog.capture_mode.setCurrentIndex(dialog.capture_mode.findData("screen"))
        self.assertIn("top 70%", dialog.dialogue_area_summary.text())

    def test_both_area_buttons_validate_and_failed_request_does_not_linger(self):
        for name in ("dialogue", "story"):
            with self.subTest(area=name):
                dialog = self.dialog()
                dialog.screenshot_directory.clear()
                getattr(dialog, f"{name}_area_button").click()
                self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertFalse(dialog.dialogue_area_requested)
                self.assertFalse(dialog.story_area_requested)
                dialog.screenshot_directory.setText(str(self.root))
                dialog.validate_and_accept()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertFalse(dialog.dialogue_area_requested)
                self.assertFalse(dialog.story_area_requested)

    def test_dialogue_area_saves_and_requests_calibration(self):
        dialog = self.dialog()
        dialog.dialogue_area_button.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertTrue(dialog.dialogue_area_requested)
        self.assertFalse(dialog.story_area_requested)

    def test_both_area_requests_wait_for_pack_validation_and_cancel_safely(self):
        pack = self.root / "pack.json"
        pack.touch()
        for name in ("dialogue", "story"):
            for cancel in (False, True):
                with self.subTest(area=name, cancel=cancel):
                    dialog = self.dialog()
                    dialog.game_pack.setText(str(pack))
                    pool = ManualThreadPool()
                    dialog.game_pack_runner.thread_pool = pool
                    with patch(
                        "vntts.app.apply_game_pack",
                        side_effect=lambda settings, *_: settings,
                    ):
                        getattr(dialog, f"{name}_area_button").click()
                        self.assertFalse(dialog.dialogue_area_button.isEnabled())
                        self.assertFalse(dialog.story_area_button.isEnabled())
                        if cancel:
                            dialog.reject()
                        pool.run_next()
                        self.application.processEvents()
                    self.assertEqual(
                        dialog.result(),
                        QDialog.DialogCode.Rejected
                        if cancel
                        else QDialog.DialogCode.Accepted,
                    )
                    self.assertEqual(
                        getattr(dialog, f"{name}_area_requested"), not cancel
                    )
                    if cancel:
                        self.assertFalse(dialog.dialogue_area_requested)
                        self.assertFalse(dialog.story_area_requested)

    def test_save_applies_settings_before_dispatching_requested_area(self):
        for name in ("dialogue", "story"):
            with self.subTest(area=name):
                dialog = self.dialog()
                getattr(dialog, f"{name}_area_button").click()
                owner = Mock()
                owner._controller_busy = owner._shutting_down = False
                owner.live_stop_runner.active = False
                owner._needs_modal_stop.return_value = False
                owner._controller_ready = False
                owner.settings = dialog.settings()
                owner._create_settings_dialog.return_value = dialog
                owner._runtime_settings.return_value = owner.settings
                owner._save_settings_with_login_rollback.return_value = (
                    self.root / "settings.json"
                )
                with patch.object(
                    dialog, "exec", return_value=QDialog.DialogCode.Accepted
                ):
                    ConfigurationApplyMixin.open_settings(owner)
                callback = owner._start_configuration_apply.call_args.kwargs[
                    "after_apply"
                ]
                self.assertIs(
                    callback,
                    owner.calibrate
                    if name == "dialogue"
                    else owner.calibrate_story_area,
                )
                owner.calibrate.assert_not_called()
                owner.calibrate_story_area.assert_not_called()
                callback()
                callback.assert_called_once_with()

    def test_pack_validation_failure_clears_area_request(self):
        pack = self.root / "pack.json"
        pack.touch()
        for name in ("dialogue", "story"):
            with self.subTest(area=name):
                dialog = self.dialog()
                dialog.game_pack.setText(str(pack))
                pool = ManualThreadPool()
                dialog.game_pack_runner.thread_pool = pool
                with patch(
                    "vntts.app.apply_game_pack", side_effect=ValueError("invalid pack")
                ):
                    getattr(dialog, f"{name}_area_button").click()
                    pool.run_next()
                    self.application.processEvents()
                self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertFalse(dialog.dialogue_area_requested)
                self.assertFalse(dialog.story_area_requested)
                self.assertTrue(dialog.dialogue_area_button.isEnabled())
                self.assertTrue(dialog.story_area_button.isEnabled())

    def test_settings_summary_does_not_invent_a_saved_default_region(self):
        owner = Mock()
        owner.settings = AppSettings()
        owner._capture_setup_pending = False
        owner.profile_store = GameProfileStore(self.root / "profiles.json")
        owner.controller._story_region.return_value = None
        with patch("vntts.app.SettingsDialog") as factory:
            TrayApplication._create_settings_dialog(owner)
        self.assertIsNone(factory.call_args.kwargs["dialogue_region"])
        region = DialogRegion(0.1, 0.7, 0.8, 0.2)
        save_dialog_region(region, self.root / "region.json")
        owner.settings = owner.settings.updated(onboarding_completed=True)
        with patch("vntts.app.SettingsDialog") as factory:
            TrayApplication._create_settings_dialog(owner)
        self.assertEqual(factory.call_args.kwargs["dialogue_region"], region)
        owner.settings = owner.settings.updated(
            capture_mode="window",
            game_window_title="Other game",
            onboarding_completed=False,
        )
        with patch("vntts.app.SettingsDialog") as factory:
            TrayApplication._create_settings_dialog(owner)
        self.assertIsNone(factory.call_args.kwargs["dialogue_region"])

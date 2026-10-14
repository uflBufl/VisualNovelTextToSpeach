import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from vntts.controller import AppController
from vntts.ocr import (
    DialogRegion,
    OCRRegions,
    get_dialog_region,
    load_dialog_region,
    save_dialog_region,
)
from vntts.profiles import GameProfileStore
from vntts.profiles_ui import GameProfilesDialog
from vntts.settings import AppSettings


class ProfileCaptureRegionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_active_profile_and_calibration_drive_runtime_capture(self):
        with TemporaryDirectory() as directory:
            store = GameProfileStore(Path(directory) / "profiles.json")
            first_region = DialogRegion(0.1, 0.5, 0.8, 0.3)
            second_region = DialogRegion(0.2, 0.4, 0.7, 0.4)
            first = store.create("First", AppSettings(), region=first_region)
            second = store.create("Second", AppSettings(), region=second_region)
            self.assertEqual(
                second.updated_from_settings(AppSettings()).dialog_region,
                second_region,
            )
            controller = AppController(first.apply(AppSettings()), profile_store=store)
            with patch("vntts.controller.capture_live_frame") as capture:
                controller._capture_live_frame()
                self.assertEqual(capture.call_args.kwargs["region"], first_region)

                controller.settings = second.apply(AppSettings())
                controller._capture_live_frame()
                self.assertEqual(capture.call_args.kwargs["region"], second_region)

                calibrated = DialogRegion(0.3, 0.3, 0.6, 0.5)
                store.update_region(second.id, calibrated)
                controller._capture_live_frame()
                self.assertEqual(capture.call_args.kwargs["region"], calibrated)

    def test_environment_override_precedes_active_profile(self):
        profile_region = DialogRegion(0.1, 0.5, 0.8, 0.3)
        override = DialogRegion(0.2, 0.4, 0.7, 0.4)
        with patch.dict(os.environ, {"VNTTS_DIALOG_REGION": "0.2,0.4,0.7,0.4"}):
            self.assertEqual(get_dialog_region(profile_region), override)

    def test_configured_layout_is_captured_with_its_profile_and_manual_diagnostics(
        self,
    ):
        with TemporaryDirectory() as directory:
            store = GameProfileStore(Path(directory) / "profiles.json")
            regions = OCRRegions(
                DialogRegion(0.1, 0.1, 0.3, 0.2), DialogRegion(0.1, 0.4, 0.8, 0.4)
            )
            first = store.create(
                "First",
                AppSettings(),
                region=DialogRegion(0, 0.6, 1, 0.4),
                ocr_regions=regions,
            )
            second = store.create(
                "Second", AppSettings(), region=DialogRegion(0, 0.5, 1, 0.5)
            )
            controller = AppController(first.apply(AppSettings()), profile_store=store)
            with patch("vntts.controller.capture_live_frame") as capture:
                controller._capture_live_frame()
                self.assertEqual(capture.call_args.kwargs["ocr_regions"], regions)
                controller.settings = second.apply(AppSettings())
                controller._capture_live_frame()
                self.assertIsNone(capture.call_args.kwargs["ocr_regions"])
            controller.settings = first.apply(AppSettings())
            with patch.dict(os.environ, {"VNTTS_DIALOG_REGION": "0,0.5,1,0.5"}):
                self.assertIsNone(controller._capture_ocr_regions())
            with patch(
                "vntts.controller_components.analyze_dialog_snapshot"
            ) as analyze:
                from vntts.diagnostics import DiagnosticSnapshot

                analyze.side_effect = lambda *args, **kwargs: kwargs[
                    "diagnostic_handler"
                ](DiagnosticSnapshot(None))
                controller.diagnostics.inspect_current_dialog(
                    notify=False, publish=False
                )
                self.assertEqual(analyze.call_args.kwargs["ocr_regions"], regions)

    def test_global_layout_is_used_without_a_profile(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dialog-region.json"
            regions = OCRRegions(
                DialogRegion(0.1, 0.1, 0.3, 0.2), DialogRegion(0.1, 0.4, 0.8, 0.4)
            )
            save_dialog_region(DialogRegion(0, 0.6, 1, 0.4), path, ocr_regions=regions)
            controller = AppController(
                AppSettings(),
                profile_store=GameProfileStore(Path(directory) / "profiles.json"),
            )
            with patch.dict(os.environ, {"VNTTS_DIALOG_REGION_FILE": str(path)}):
                self.assertEqual(controller._capture_ocr_regions(), regions)

    def test_profile_selection_preserves_global_region_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            region_file = root / "dialog-region.json"
            global_region = DialogRegion(0.1, 0.5, 0.8, 0.3)
            profile_region = DialogRegion(0.2, 0.4, 0.7, 0.4)
            save_dialog_region(global_region, region_file)
            store = GameProfileStore(root / "profiles.json")
            profile = store.create(
                "Game",
                AppSettings(game_window_title="Game"),
                region=profile_region,
            )
            with patch.dict(
                os.environ,
                {"VNTTS_DIALOG_REGION_FILE": str(region_file)},
            ):
                dialog = GameProfilesDialog(AppSettings(), store)
                dialog.refresh_profiles(profile.id)
                dialog.use_profile()

            selected = dialog.settings()
            self.assertEqual(load_dialog_region(region_file), global_region)

        self.assertEqual(selected.active_profile_id, profile.id)
        self.assertEqual(store.get(profile.id).dialog_region, profile_region)


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.controller import AppController
from vntts.ocr import DialogRegion, get_dialog_region
from vntts.profiles import GameProfileStore
from vntts.settings import AppSettings


class ProfileCaptureRegionTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

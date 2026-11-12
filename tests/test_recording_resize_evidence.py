import copy
import unittest

import numpy as np

from scripts.mac_live_recording.verify_resize import (
    inspect_frame,
    validate_resize_events,
)


class ResizeEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.metadata = {
            "status": "succeeded",
            "errors": [],
            "window_id": 123,
            "pid": 456,
            "bounds": {"width": 640, "height": 422},
            "mechanism": "ScreenCaptureKit desktopIndependentWindow",
            "requested_seconds": 33,
            "video_size": [640, 422],
            "video_resize_policy": "aspect-fit-black",
        }
        self.records = [
            {
                "kind": "video",
                "written": True,
                "pts_s": 100 + second,
                "width": 640,
                "height": 422,
            }
            for second in range(33)
        ] + [{"kind": "finished", "video_drops": 0}]
        self.fixture = [
            {
                "second": second,
                "window_id": 123,
                "width": width,
                "height": height,
                "fullscreen": fullscreen,
            }
            for second, width, height, fullscreen in (
                (1, 640, 422, False),
                (4, 320, 222, False),
                (8, 960, 622, False),
                (12, 350, 672, False),
                (16, 900, 322, False),
                (22, 1200, 800, True),
                (29, 640, 422, False),
            )
        ]

    @staticmethod
    def image(*, portrait=False):
        rgb = np.zeros((100, 100, 3), dtype=np.uint8)
        left, right, top, bottom = (30, 70, 5, 95) if portrait else (5, 95, 30, 70)
        rgb[top:bottom, left:right] = [255, 255, 0]
        rgb[top + 3 : bottom - 3, left + 3 : right - 3] = [0, 255, 0]
        return rgb

    def test_accepts_complete_fixed_extent_and_real_fixture_transitions(self):
        self.assertEqual(
            validate_resize_events(self.metadata, self.records, self.fixture),
            (640, 422),
        )
        self.assertEqual(inspect_frame(self.image(), bars="wide")["magenta_pixels"], 0)
        inspect_frame(self.image(portrait=True), bars="portrait")
        converted = self.image()
        converted[(converted == [0, 255, 0]).all(axis=2)] = [117, 252, 75]
        inspect_frame(converted, bars="wide")

    def test_rejects_magenta_missing_border_and_missing_black_bars(self):
        leaked = self.image()
        leaked[0, 0] = [255, 0, 255]
        with self.assertRaisesRegex(ValueError, "magenta"):
            inspect_frame(leaked)
        cropped = self.image()
        cropped[30:33, 5:95] = [0, 255, 0]
        with self.assertRaisesRegex(ValueError, "border edge"):
            inspect_frame(cropped)
        missing_bars = self.image()
        missing_bars[0] = [100, 100, 100]
        with self.assertRaisesRegex(ValueError, "black top/bottom"):
            inspect_frame(missing_bars, bars="wide")

    def test_rejects_invalid_metadata_identity_drops_and_encoded_extent(self):
        for change in (
            {"status": "failed"},
            {"errors": ["identity failed"]},
            {"target_guard_failure": {"reason": "wrong_pid"}},
            {"video_drops": 1},
            {"video_resize_policy": "crop"},
            {"video_size": [0, 422]},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_resize_events(
                    self.metadata | change, self.records, self.fixture
                )
        records = copy.deepcopy(self.records)
        records[5]["width"] = 320
        with self.assertRaisesRegex(ValueError, "extent"):
            validate_resize_events(self.metadata, records, self.fixture)
        fixture = copy.deepcopy(self.fixture)
        fixture[1]["window_id"] = 789
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_resize_events(self.metadata, self.records, fixture)

    def test_rejects_stall_nonmonotonic_timestamps_and_missing_fullscreen(self):
        for records in (
            self.records[:4] + self.records[9:],
            self.records[:4] + self.records[3:],
        ):
            with self.assertRaises(ValueError):
                validate_resize_events(self.metadata, records, self.fixture)
        records = copy.deepcopy(self.records)
        records[5]["pts_s"] = float("nan")
        with self.assertRaises(ValueError):
            validate_resize_events(self.metadata, records, self.fixture)
        with self.assertRaisesRegex(ValueError, "fullscreen"):
            validate_resize_events(
                self.metadata,
                self.records,
                [r for r in self.fixture if not r["fullscreen"]],
            )


if __name__ == "__main__":
    unittest.main()

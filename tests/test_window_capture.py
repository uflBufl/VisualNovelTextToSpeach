import unittest
from unittest.mock import Mock

from vntts.ocr import DialogRegion
from vntts.window_capture import (
    WindowCaptureTarget,
    WindowGeometry,
    WindowInfo,
    WindowMinimizedError,
    WindowNotFoundError,
)


class WindowCaptureTargetTest(unittest.TestCase):
    def test_capture_region_follows_window_movement_and_resize(self):
        window = WindowInfo(100, "Reverse: 1999", 42)
        backend = Mock()
        backend.list_windows.return_value = [window]
        backend.get_window.return_value = window
        backend.get_client_geometry.side_effect = [
            WindowGeometry(100, 200, 1600, 900),
            WindowGeometry(300, 100, 1920, 1080),
        ]
        target = WindowCaptureTarget("Reverse: 1999", backend)
        region = DialogRegion(0.1, 0.6, 0.8, 0.3)

        first = target.capture_box(region)
        second = target.capture_box(region)

        self.assertEqual(
            first,
            {"left": 260, "top": 740, "width": 1280, "height": 270},
        )
        self.assertEqual(
            second,
            {"left": 492, "top": 748, "width": 1536, "height": 324},
        )

    def test_destroyed_window_handle_is_resolved_after_game_restart(self):
        old_window = WindowInfo(100, "Reverse: 1999", 42)
        new_window = WindowInfo(200, "Reverse: 1999", 84)
        backend = Mock()
        backend.list_windows.side_effect = [[old_window], [new_window]]
        backend.get_window.side_effect = [None]
        backend.get_client_geometry.return_value = WindowGeometry(0, 0, 1920, 1080)
        target = WindowCaptureTarget("Reverse: 1999", backend)

        self.assertEqual(target.get_geometry().width, 1920)
        self.assertEqual(target.get_geometry().width, 1920)

        self.assertEqual(backend.get_client_geometry.call_args_list[0].args, (100,))
        self.assertEqual(backend.get_client_geometry.call_args_list[1].args, (200,))

    def test_minimized_window_reports_recoverable_error(self):
        backend = Mock()
        backend.list_windows.return_value = [
            WindowInfo(100, "Reverse: 1999", 42, minimized=True)
        ]
        target = WindowCaptureTarget("Reverse: 1999", backend)

        with self.assertRaisesRegex(WindowMinimizedError, "is minimized"):
            target.get_geometry()

        backend.get_client_geometry.assert_not_called()

    def test_missing_window_reports_selected_title(self):
        backend = Mock()
        backend.list_windows.return_value = []
        target = WindowCaptureTarget("Reverse: 1999", backend)

        with self.assertRaisesRegex(WindowNotFoundError, "Reverse: 1999"):
            target.get_geometry()

    def test_empty_selection_requests_configuration(self):
        target = WindowCaptureTarget("", Mock())

        with self.assertRaisesRegex(WindowNotFoundError, "Select a game window"):
            target.get_geometry()


if __name__ == "__main__":
    unittest.main()

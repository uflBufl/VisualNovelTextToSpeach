import unittest
from functools import partial
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from vntts.app import TrayApplication
from vntts.settings import AppSettings
from vntts.window_capture import DisplayCaptureTarget


class ScreenLiveScopeTest(unittest.TestCase):
    def setUp(self):
        controller = SimpleNamespace(
            settings=AppSettings(capture_mode="screen", game_window_title=None),
            capture_target=DisplayCaptureTarget(),
            observation_serial=0,
            live_scope_identification_failure=None,
            _is_game_focused=Mock(side_effect=AssertionError("Window focus")),
            identify_live_scope=Mock(),
        )
        self.tray = SimpleNamespace(
            controller=controller,
            _live_scope_generation=0,
            _live_scope_cancellation=Event(),
            _live_scope_target=controller.capture_target,
            _live_scope_settings=controller.settings,
            _live_scope_observation_serial=controller.observation_serial,
            _live_scope_window=None,
            _lifecycle_is_current=Mock(return_value=True),
            _cancel_live_scope=Mock(),
            _apply_controller_action_state=Mock(),
            _wait_for_live_scope_focus=Mock(),
            _report_live_scope_failure=Mock(),
            _start_live_with_available_scope=Mock(),
            live_scope_focus_timer=Mock(),
            live_scope_runner=Mock(),
            set_status=Mock(),
        )
        self.tray._live_scope_request_refusal = partial(
            TrayApplication._live_scope_request_refusal, self.tray
        )

    def test_completed_screen_scope_starts_without_window_focus(self):
        TrayApplication._live_scope_finished(self.tray, True, None)
        self.tray._start_live_with_available_scope.assert_called_once_with(
            allow_scope_bootstrap=False
        )
        self.tray.controller._is_game_focused.assert_not_called()
        self.tray._wait_for_live_scope_focus.assert_not_called()

    def test_screen_scope_retry_never_waits_for_window_focus(self):
        TrayApplication._retry_live_scope_when_focused(self.tray)
        self.tray.live_scope_runner.start.assert_called_once_with(
            self.tray.controller.identify_live_scope,
            cancellation=self.tray._live_scope_cancellation,
        )
        self.tray.controller._is_game_focused.assert_not_called()
        self.tray.live_scope_focus_timer.start.assert_not_called()

    def test_cancelled_screen_scope_cannot_start(self):
        self.tray._live_scope_cancellation.set()
        TrayApplication._live_scope_finished(self.tray, True, None)
        self.tray._start_live_with_available_scope.assert_not_called()
        self.tray._report_live_scope_failure.assert_called_once_with("cancelled")

    def test_replaced_display_source_cannot_start(self):
        self.tray.controller.capture_target = DisplayCaptureTarget()
        TrayApplication._live_scope_finished(self.tray, True, None)
        self.tray._start_live_with_available_scope.assert_not_called()
        self.tray._report_live_scope_failure.assert_called_once_with("target-changed")

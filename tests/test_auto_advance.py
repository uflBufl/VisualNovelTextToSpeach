import unittest
from unittest.mock import Mock, call

from pynput import keyboard

from vntts.auto_advance import DialogueAdvancer, send_windows_key
from vntts.auto_advance_policy import (
    auto_advance_control_state,
    guard_auto_advance_settings,
)
from vntts.settings import AppSettings


class FakeKeyboardController:
    def __init__(self):
        self.events = []

    def press(self, key):
        self.events.append(("press", key))

    def release(self, key):
        self.events.append(("release", key))


class DialogueAdvancerTest(unittest.TestCase):
    def test_cross_platform_fallback_sends_one_press_and_release(self):
        controller = FakeKeyboardController()
        advancer = DialogueAdvancer(
            "enter",
            controller_factory=lambda: controller,
            platform="linux",
        )

        self.assertTrue(advancer.advance())

        self.assertEqual(
            controller.events,
            [("press", keyboard.Key.enter), ("release", keyboard.Key.enter)],
        )

    def test_windows_uses_native_send_input(self):
        sender = Mock(return_value=True)
        controller_factory = Mock()
        advancer = DialogueAdvancer(
            "right",
            controller_factory=controller_factory,
            platform="win32",
            windows_sender=sender,
        )

        self.assertTrue(advancer.advance())

        sender.assert_called_once_with(0x27)
        controller_factory.assert_not_called()

    def test_interrupted_key_press_still_releases_and_keeps_original_error(self):
        for platform in ("linux", "darwin"):
            for error_type in (RuntimeError, KeyboardInterrupt):
                for release_fails in (False, True):
                    with self.subTest(
                        platform=platform, error=error_type, release=release_fails
                    ):
                        error = error_type("press failed")
                        release_error = (
                            ValueError("release failed") if release_fails else None
                        )
                        if release_fails and error_type is KeyboardInterrupt:
                            release_error = SystemExit("release interrupted")
                        controller = Mock()
                        controller.press.side_effect = error
                        controller.release.side_effect = release_error
                        quartz = Mock(kCGHIDEventTap=0)
                        quartz.CGEventCreateKeyboardEvent.side_effect = ("down", "up")
                        quartz.CGEventPost.side_effect = (error, release_error)
                        advancer = DialogueAdvancer(
                            "enter",
                            platform=platform,
                            controller_factory=lambda: controller,
                            quartz_module=quartz,
                        )

                        with self.assertRaises(error_type) as raised:
                            advancer.advance()

                        self.assertIs(raised.exception, error)
                        if platform == "linux":
                            controller.release.assert_called_once_with(
                                keyboard.Key.enter
                            )
                        else:
                            self.assertEqual(
                                quartz.CGEventPost.call_args_list,
                                [call(0, "down"), call(0, "up")],
                            )
                        if release_fails:
                            self.assertIn("release failed", error.__notes__[0])

    def test_successful_key_press_reports_release_failure(self):
        controller = Mock()
        controller.release.side_effect = RuntimeError("release failed")
        advancer = DialogueAdvancer(
            "enter", platform="linux", controller_factory=lambda: controller
        )

        with self.assertRaisesRegex(RuntimeError, "release failed"):
            advancer.advance()

        controller.press.assert_called_once_with(keyboard.Key.enter)
        controller.release.assert_called_once_with(keyboard.Key.enter)

    def test_windows_partial_send_releases_key_before_reporting_failure(self):
        user32 = Mock()
        user32.SendInput.side_effect = (1, 1)

        with self.assertRaisesRegex(OSError, "could not post"):
            send_windows_key(0x20, user32=user32)

        self.assertEqual(user32.SendInput.call_count, 2)
        self.assertEqual(user32.SendInput.call_args_list[1].args[0], 1)

    def test_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            DialogueAdvancer("escape")

    def test_macos_posts_native_quartz_events_without_pynput(self):
        quartz = Mock()
        quartz.kCGHIDEventTap = 0
        quartz.CGEventCreateKeyboardEvent.side_effect = ["down", "up"]
        controller_factory = Mock()
        advancer = DialogueAdvancer(
            "space",
            controller_factory=controller_factory,
            platform="darwin",
            quartz_module=quartz,
        )

        self.assertTrue(advancer.advance())

        self.assertEqual(
            quartz.CGEventCreateKeyboardEvent.call_args_list,
            [
                call(None, 49, True),
                call(None, 49, False),
            ],
        )
        self.assertEqual(
            quartz.CGEventPost.call_args_list,
            [call(0, "down"), call(0, "up")],
        )
        controller_factory.assert_not_called()

    def test_macos_does_not_press_key_until_release_event_exists(self):
        quartz = Mock()
        quartz.kCGHIDEventTap = 0
        quartz.CGEventCreateKeyboardEvent.side_effect = ["down", None]
        advancer = DialogueAdvancer(
            "space",
            platform="darwin",
            quartz_module=quartz,
        )

        with self.assertRaisesRegex(RuntimeError, "could not create"):
            advancer.advance()

        quartz.CGEventPost.assert_not_called()

    def test_macos_refuses_native_input_without_accessibility_permission(self):
        advancer = DialogueAdvancer(
            "space",
            platform="darwin",
            quartz_module=None,
            accessibility_probe=lambda: False,
        )

        with self.assertRaisesRegex(PermissionError, "Accessibility"):
            advancer.advance()


class AutoAdvancePolicyTest(unittest.TestCase):
    def test_publication_matches_effective_controls_without_mutating_preference(self):
        for capture in ("window", "screen"):
            for sequence in ("off", "shadow", "audio-auto", "audio-manual"):
                for enabled in (False, True):
                    with self.subTest(
                        capture=capture, sequence=sequence, enabled=enabled
                    ):
                        settings = AppSettings(
                            capture_mode=capture,
                            live_sequence_mode=sequence,
                            auto_advance_enabled=enabled,
                        )
                        expected = (
                            enabled
                            and capture == "window"
                            and sequence != "audio-manual"
                        )

                        guarded = guard_auto_advance_settings(settings)

                        self.assertEqual(
                            guarded, settings.updated(auto_advance_enabled=expected)
                        )
                        self.assertEqual(
                            auto_advance_control_state(capture, sequence, enabled)[1],
                            expected,
                        )
                        if expected == enabled:
                            self.assertIs(guarded, settings)
                        self.assertEqual(settings.auto_advance_enabled, enabled)


if __name__ == "__main__":
    unittest.main()

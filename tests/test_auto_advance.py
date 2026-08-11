import unittest
from unittest.mock import Mock, call

from pynput import keyboard

from vntts.auto_advance import DialogueAdvancer


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

    def test_macos_refuses_native_input_without_accessibility_permission(self):
        advancer = DialogueAdvancer(
            "space",
            platform="darwin",
            quartz_module=None,
            accessibility_probe=lambda: False,
        )

        with self.assertRaisesRegex(PermissionError, "Accessibility"):
            advancer.advance()


if __name__ == "__main__":
    unittest.main()

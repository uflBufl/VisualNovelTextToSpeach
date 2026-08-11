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
    def test_sends_one_press_and_release_for_configured_key(self):
        controller = FakeKeyboardController()
        advancer = DialogueAdvancer(
            "enter",
            controller_factory=lambda: controller,
            platform="win32",
        )

        self.assertTrue(advancer.advance())

        self.assertEqual(
            controller.events,
            [("press", keyboard.Key.enter), ("release", keyboard.Key.enter)],
        )

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


if __name__ == "__main__":
    unittest.main()

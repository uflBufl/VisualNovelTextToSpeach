import unittest
from unittest.mock import Mock, call, patch

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
        reports = []
        advancer = DialogueAdvancer(
            "right",
            controller_factory=controller_factory,
            platform="win32",
            windows_sender=sender,
            diagnostic_handler=reports.append,
        )

        self.assertTrue(advancer.advance())

        sender.assert_called_once_with(0x27)
        controller_factory.assert_not_called()
        self.assertEqual(reports[0]["mechanism"], "custom")
        self.assertEqual(reports[0]["key_code"], 0x27)
        self.assertTrue(reports[0]["success"])
        self.assertNotIn("press_started_monotonic", reports[0])
        self.assertNotIn("batch_started_monotonic", reports[0])

    def test_diagnostics_are_emitted_once_after_release_in_timestamp_order(self):
        for platform, mechanism in (("linux", "pynput"), ("darwin", "quartz")):
            with self.subTest(platform=platform):
                reports = []
                controller = Mock()
                controller.press.side_effect = lambda key: self.assertEqual(reports, [])
                controller.release.side_effect = lambda key: self.assertEqual(
                    reports, []
                )
                quartz = Mock(kCGHIDEventTap=0)
                quartz.CGEventCreateKeyboardEvent.side_effect = ("down", "up")
                quartz.CGEventPost.side_effect = lambda *args: self.assertEqual(
                    reports, []
                )
                advancer = DialogueAdvancer(
                    "enter",
                    platform=platform,
                    controller_factory=lambda: controller,
                    quartz_module=quartz,
                    diagnostic_handler=reports.append,
                )

                with patch("vntts.auto_advance.time.monotonic", side_effect=range(6)):
                    self.assertTrue(advancer.advance())

                self.assertEqual(len(reports), 1)
                report = reports[0]
                self.assertEqual(report["mechanism"], mechanism)
                self.assertEqual(report["input_key"], "enter")
                self.assertTrue(report["success"])
                self.assertEqual(
                    [
                        report[field]
                        for field in (
                            "started_monotonic",
                            "press_started_monotonic",
                            "press_completed_monotonic",
                            "release_started_monotonic",
                            "release_completed_monotonic",
                            "completed_monotonic",
                        )
                    ],
                    list(range(6)),
                )
                if platform == "darwin":
                    self.assertEqual(report["key_code"], 36)
                else:
                    self.assertNotIn("key_code", report)

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
                        reports = []
                        advancer = DialogueAdvancer(
                            "enter",
                            platform=platform,
                            controller_factory=lambda: controller,
                            quartz_module=quartz,
                            diagnostic_handler=reports.append,
                        )

                        with self.assertRaises(error_type) as raised:
                            advancer.advance()

                        self.assertIs(raised.exception, error)
                        self.assertEqual(len(reports), 1)
                        report = reports[0]
                        self.assertFalse(report["success"])
                        self.assertEqual(report["error_type"], error_type.__name__)
                        self.assertEqual(
                            report["press_error_type"], error_type.__name__
                        )
                        self.assertIn("release_completed_monotonic", report)
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
                            self.assertEqual(
                                report["release_error_type"],
                                type(release_error).__name__,
                            )

    def test_successful_key_press_reports_release_failure(self):
        controller = Mock()
        controller.release.side_effect = RuntimeError("release failed")
        reports = []
        advancer = DialogueAdvancer(
            "enter",
            platform="linux",
            controller_factory=lambda: controller,
            diagnostic_handler=reports.append,
        )

        with self.assertRaisesRegex(RuntimeError, "release failed"):
            advancer.advance()

        controller.press.assert_called_once_with(keyboard.Key.enter)
        controller.release.assert_called_once_with(keyboard.Key.enter)
        self.assertEqual(reports[0]["release_error_type"], "RuntimeError")
        self.assertNotIn("press_error_type", reports[0])

    def test_windows_partial_send_releases_key_before_reporting_failure(self):
        user32 = Mock()
        user32.SendInput.side_effect = (1, 1)

        with self.assertRaisesRegex(OSError, "could not post"):
            send_windows_key(0x20, user32=user32)

        self.assertEqual(user32.SendInput.call_count, 2)
        self.assertEqual(user32.SendInput.call_args_list[1].args[0], 1)

    def test_windows_batch_diagnostics_and_partial_send_cleanup(self):
        for sent in (0, 1, 2):
            with self.subTest(sent=sent):
                reports = []
                user32 = Mock()

                def send(count, inputs, size):
                    self.assertEqual(reports, [])
                    return sent if count == 2 else 1

                user32.SendInput.side_effect = send
                advancer = DialogueAdvancer(
                    "space", platform="win32", diagnostic_handler=reports.append
                )
                with (
                    patch("vntts.auto_advance._windows_input", return_value=user32),
                    patch(
                        "vntts.auto_advance.ctypes.GetLastError",
                        return_value=5,
                        create=True,
                    ) as get_last_error,
                    patch("vntts.auto_advance.time.monotonic", side_effect=range(6)),
                ):
                    if sent == 2:
                        self.assertTrue(advancer.advance())
                    else:
                        with self.assertRaisesRegex(OSError, "could not post"):
                            advancer.advance()

                self.assertEqual(len(reports), 1)
                report = reports[0]
                self.assertEqual(report["mechanism"], "sendinput")
                self.assertEqual(report["input_events_requested"], 2)
                self.assertEqual(report["input_events_sent"], sent)
                self.assertEqual(report["batch_started_monotonic"], 1)
                self.assertEqual(report["batch_completed_monotonic"], 2)
                self.assertNotIn("press_started_monotonic", report)
                self.assertEqual(user32.SendInput.call_args_list[0].args[0], 2)
                if sent == 2:
                    self.assertTrue(report["success"])
                    self.assertEqual(user32.SendInput.call_count, 1)
                    self.assertNotIn("release_started_monotonic", report)
                    self.assertNotIn("native_error_code", report)
                    get_last_error.assert_not_called()
                else:
                    self.assertFalse(report["success"])
                    self.assertEqual(report["error_type"], "OSError")
                    self.assertEqual(report["cleanup_events_sent"], 1)
                    self.assertEqual(report["release_started_monotonic"], 3)
                    self.assertEqual(report["release_completed_monotonic"], 4)
                    self.assertEqual(report["completed_monotonic"], 5)
                    self.assertEqual(report["native_error_code"], 5)
                    self.assertEqual(user32.SendInput.call_args_list[1].args[0], 1)

    def test_windows_batch_error_is_reported_without_fabricated_event_counts(self):
        error = OSError("batch failed")
        user32 = Mock()
        user32.SendInput.side_effect = error
        reports = []
        advancer = DialogueAdvancer(platform="win32", diagnostic_handler=reports.append)

        with patch("vntts.auto_advance._windows_input", return_value=user32):
            with self.assertRaises(OSError) as raised:
                advancer.advance()

        self.assertIs(raised.exception, error)
        self.assertEqual(reports[0]["batch_error_type"], "OSError")
        self.assertIn("batch_completed_monotonic", reports[0])
        self.assertNotIn("input_events_sent", reports[0])
        user32.SendInput.assert_called_once()

    def test_setup_failure_is_reported_without_press_markers(self):
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                error = RuntimeError("setup failed")
                reports = []
                probe = Mock(return_value={})
                advancer = DialogueAdvancer(
                    platform=platform,
                    controller_factory=Mock(side_effect=error),
                    diagnostic_handler=reports.append,
                    diagnostic_probe=probe,
                )

                with patch("vntts.auto_advance._windows_input", side_effect=error):
                    with self.assertRaises(RuntimeError) as raised:
                        advancer.advance()

                self.assertIs(raised.exception, error)
                self.assertEqual(reports[0]["error_type"], "RuntimeError")
                self.assertNotIn("press_started_monotonic", reports[0])
                self.assertNotIn("batch_started_monotonic", reports[0])
                probe.assert_not_called()

    def test_broken_diagnostic_handler_never_changes_input_result_or_error(self):
        for handler_error in (RuntimeError, KeyboardInterrupt):
            for input_error in (None, KeyboardInterrupt("press interrupted")):
                with self.subTest(handler_error=handler_error, input_error=input_error):
                    controller = Mock()
                    controller.press.side_effect = input_error
                    handler = Mock(side_effect=handler_error("sink failed"))
                    advancer = DialogueAdvancer(
                        platform="linux",
                        controller_factory=lambda: controller,
                        diagnostic_handler=handler,
                    )

                    if input_error is None:
                        self.assertTrue(advancer.advance())
                    else:
                        with self.assertRaises(KeyboardInterrupt) as raised:
                            advancer.advance()
                        self.assertIs(raised.exception, input_error)

                    controller.release.assert_called_once()
                    handler.assert_called_once()

    def test_focus_probe_runs_after_setup_and_before_native_input(self):
        for platform in ("linux", "darwin", "win32"):
            with self.subTest(platform=platform):
                events = []
                reports = []
                controller = Mock()
                controller.press.side_effect = lambda key: events.append("press")
                controller.release.side_effect = lambda key: events.append("release")
                quartz = Mock(kCGHIDEventTap=0)

                def create_event(source, key, pressed):
                    events.append("setup")
                    return "press" if pressed else "release"

                quartz.CGEventCreateKeyboardEvent.side_effect = create_event
                quartz.CGEventPost.side_effect = lambda tap, event: events.append(event)
                user32 = Mock()

                def send_batch(*args):
                    events.extend(("press", "release"))
                    return 2

                user32.SendInput.side_effect = send_batch

                def setup(backend):
                    events.append("setup")
                    return backend

                def probe():
                    events.append("probe")
                    return {"before_foreground_pid": 123}

                def report(details):
                    events.append("handler")
                    reports.append(details)

                advancer = DialogueAdvancer(
                    platform=platform,
                    controller_factory=lambda: setup(controller),
                    quartz_module=quartz,
                    diagnostic_handler=report,
                    diagnostic_probe=probe,
                )
                with patch(
                    "vntts.auto_advance._windows_input",
                    side_effect=lambda: setup(user32),
                ):
                    self.assertTrue(advancer.advance())

                self.assertEqual(
                    events,
                    ["setup"] * (2 if platform == "darwin" else 1)
                    + ["probe", "press", "release", "handler"],
                )
                self.assertEqual(reports[0]["before_foreground_pid"], 123)

    def test_failed_focus_probe_does_not_block_key_pair(self):
        for error_type in (RuntimeError, KeyboardInterrupt):
            with self.subTest(error_type=error_type):
                controller = FakeKeyboardController()
                reports = []
                probe = Mock(side_effect=error_type("probe failed"))
                advancer = DialogueAdvancer(
                    platform="linux",
                    controller_factory=lambda: controller,
                    diagnostic_handler=reports.append,
                    diagnostic_probe=probe,
                )

                self.assertTrue(advancer.advance())

                probe.assert_called_once()
                self.assertEqual(len(controller.events), 2)
                self.assertEqual(
                    reports[0]["before_focus_error_type"], error_type.__name__
                )
                self.assertTrue(reports[0]["success"])

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
        reports = []
        advancer = DialogueAdvancer(
            "space",
            platform="darwin",
            quartz_module=quartz,
            diagnostic_handler=reports.append,
        )

        with self.assertRaisesRegex(RuntimeError, "could not create"):
            advancer.advance()

        quartz.CGEventPost.assert_not_called()
        self.assertEqual(reports[0]["error_type"], "RuntimeError")
        self.assertNotIn("press_started_monotonic", reports[0])

    def test_macos_refuses_native_input_without_accessibility_permission(self):
        reports = []
        advancer = DialogueAdvancer(
            "space",
            platform="darwin",
            quartz_module=None,
            accessibility_probe=lambda: False,
            diagnostic_handler=reports.append,
        )

        with self.assertRaisesRegex(PermissionError, "Accessibility"):
            advancer.advance()
        self.assertEqual(reports[0]["error_type"], "PermissionError")
        self.assertNotIn("press_started_monotonic", reports[0])


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

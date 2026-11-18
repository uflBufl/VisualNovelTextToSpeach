import ctypes
import unittest
from unittest.mock import Mock, call, patch

from pynput import keyboard

from vntts.auto_advance import DialogueAdvancer, send_windows_key
from vntts.auto_advance_policy import (
    auto_advance_control_state,
    guard_auto_advance_settings,
)
from vntts.settings import AppSettings


class DialogueAdvancerTest(unittest.TestCase):
    def setUp(self):
        self.sleep = self.enterContext(patch("vntts.auto_advance.time.sleep"))

    def test_windows_custom_sender_reports_its_result_for_the_configured_key(self):
        controller_factory = Mock()
        for sent in (True, False):
            with self.subTest(sent=sent):
                sender = Mock(return_value=sent)
                reports = []
                advancer = DialogueAdvancer(
                    "right",
                    controller_factory=controller_factory,
                    platform="win32",
                    windows_sender=sender,
                    diagnostic_handler=reports.append,
                )

                self.assertIs(advancer.advance(), sent)

                sender.assert_called_once_with(0x27)
                self.assertEqual(reports[0]["mechanism"], "custom")
                self.assertEqual(reports[0]["key_code"], 0x27)
                self.assertIs(reports[0]["success"], sent)
                self.assertNotIn("press_started_monotonic", reports[0])
                self.assertNotIn("batch_started_monotonic", reports[0])
        controller_factory.assert_not_called()
        self.sleep.assert_not_called()

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

    def test_failed_key_press_attempts_release_and_reports_the_original_error(self):
        for platform, error, release_error in (
            (
                "linux",
                KeyboardInterrupt("press interrupted"),
                SystemExit("release interrupted"),
            ),
            ("darwin", RuntimeError("press failed"), None),
        ):
            with self.subTest(platform=platform):
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

                with self.assertRaises(type(error)) as raised:
                    advancer.advance()

                self.assertIs(raised.exception, error)
                [report] = reports
                self.assertFalse(report["success"])
                self.assertEqual(report["error_type"], type(error).__name__)
                self.assertEqual(report["press_error_type"], type(error).__name__)
                self.assertIn("release_completed_monotonic", report)
                self.sleep.assert_not_called()
                if platform == "linux":
                    self.assertEqual(
                        controller.method_calls,
                        [
                            call.press(keyboard.Key.enter),
                            call.release(keyboard.Key.enter),
                        ],
                    )
                    self.assertIn("release interrupted", error.__notes__[0])
                    self.assertEqual(report["release_error_type"], "SystemExit")
                else:
                    self.assertEqual(
                        quartz.CGEventPost.call_args_list,
                        [call(0, "down"), call(0, "up")],
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

    def test_windows_native_calls_report_failures_and_preserve_primary_error(self):
        for down_sent, up_sent in ((1, 1), (0, 1), (1, 0), (0, 0), (2, 1)):
            with self.subTest(down=down_sent, up=up_sent):
                reports = []
                user32 = Mock()
                flags = []

                def send(count, inputs, size):
                    self.assertEqual(reports, [])
                    self.assertEqual(count, 1)
                    event = inputs._obj
                    self.assertEqual(size, ctypes.sizeof(event))
                    self.assertEqual(event.type, 1)
                    self.assertEqual(event.keyboard.virtual_key, 0x20)
                    flags.append(event.keyboard.flags)
                    return down_sent if len(flags) == 1 else up_sent

                user32.SendInput.side_effect = send
                advancer = DialogueAdvancer(
                    "space", platform="win32", diagnostic_handler=reports.append
                )
                with (
                    patch("vntts.auto_advance._windows_input", return_value=user32),
                    patch(
                        "vntts.auto_advance.ctypes.GetLastError",
                        side_effect=(5, 6),
                        create=True,
                    ) as get_last_error,
                    patch("vntts.auto_advance.time.monotonic", side_effect=range(6)),
                ):
                    if down_sent == up_sent == 1:
                        self.assertTrue(advancer.advance())
                    else:
                        with self.assertRaisesRegex(OSError, "could not post"):
                            advancer.advance()

                self.assertEqual(len(reports), 1)
                report = reports[0]
                self.assertEqual(report["mechanism"], "sendinput")
                self.assertEqual(report["input_events_requested"], 2)
                self.assertEqual(report["press_started_monotonic"], 1)
                self.assertEqual(report["press_completed_monotonic"], 2)
                self.assertEqual(report["release_started_monotonic"], 3)
                self.assertEqual(report["release_completed_monotonic"], 4)
                self.assertEqual(report["completed_monotonic"], 5)
                self.assertNotIn("batch_started_monotonic", report)
                self.assertEqual(flags, [0, 0x0002])
                if down_sent == 1:
                    self.sleep.assert_called_with(0.05)
                else:
                    self.sleep.assert_not_called()
                self.sleep.reset_mock()
                if down_sent == up_sent == 1:
                    self.assertTrue(report["success"])
                    self.assertEqual(report["input_events_sent"], 2)
                    self.assertNotIn("native_error_code", report)
                    get_last_error.assert_not_called()
                else:
                    self.assertFalse(report["success"])
                    self.assertEqual(report["error_type"], "OSError")
                    self.assertNotIn("input_events_sent", report)
                    self.assertEqual(report["native_error_code"], 5)
                    phase = "press" if down_sent != 1 else "release"
                    self.assertEqual(report[f"{phase}_error_type"], "OSError")

    def test_windows_native_press_error_still_attempts_release(self):
        for error_type in (OSError, KeyboardInterrupt):
            with self.subTest(error=error_type):
                error = error_type("press failed")
                user32 = Mock()
                user32.SendInput.side_effect = [error, 1]
                reports = []
                advancer = DialogueAdvancer(
                    platform="win32", diagnostic_handler=reports.append
                )

                with patch("vntts.auto_advance._windows_input", return_value=user32):
                    with self.assertRaises(error_type) as raised:
                        advancer.advance()

                self.assertIs(raised.exception, error)
                self.assertEqual(reports[0]["press_error_type"], error_type.__name__)
                self.assertIn("release_completed_monotonic", reports[0])
                self.assertNotIn("input_events_sent", reports[0])
                self.assertEqual(user32.SendInput.call_count, 2)
                self.assertEqual(
                    user32.SendInput.call_args_list[1].args[1]._obj.keyboard.flags,
                    0x0002,
                )
                self.sleep.assert_not_called()

    def test_windows_holds_one_native_key_pair_for_supported_keys(self):
        for key, code in (
            ("space", 0x20),
            ("enter", 0x0D),
            ("right", 0x27),
            ("down", 0x28),
        ):
            with self.subTest(key=key):
                events = []
                user32 = Mock()

                def send(count, inputs, size):
                    self.assertEqual(count, 1)
                    events.append(
                        (inputs._obj.keyboard.virtual_key, inputs._obj.keyboard.flags)
                    )
                    return 1

                user32.SendInput.side_effect = send
                self.sleep.side_effect = lambda seconds: events.append(
                    ("hold", seconds)
                )
                with patch("vntts.auto_advance._windows_input", return_value=user32):
                    self.assertTrue(DialogueAdvancer(key, platform="win32").advance())

                self.assertEqual(events, [(code, 0), ("hold", 0.05), (code, 0x0002)])

    def test_windows_interrupted_hold_releases_the_same_key(self):
        error = KeyboardInterrupt("hold interrupted")
        user32 = Mock()
        user32.SendInput.return_value = 1
        self.sleep.side_effect = error

        with self.assertRaises(KeyboardInterrupt) as raised:
            send_windows_key(0x27, user32=user32)

        self.assertIs(raised.exception, error)
        events = [args[1]._obj.keyboard for args, _ in user32.SendInput.call_args_list]
        self.assertEqual(
            [(event.virtual_key, event.flags) for event in events],
            [(0x27, 0), (0x27, 0x0002)],
        )

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
        for handler_error, input_error in (
            (RuntimeError("sink failed"), None),
            (
                KeyboardInterrupt("sink interrupted"),
                KeyboardInterrupt("press interrupted"),
            ),
        ):
            with self.subTest(handler_error=handler_error):
                controller = Mock()
                controller.press.side_effect = input_error
                handler = Mock(side_effect=handler_error)
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

                def send_event(count, inputs, size):
                    events.append("release" if inputs._obj.keyboard.flags else "press")
                    return 1

                user32.SendInput.side_effect = send_event

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

    def test_interrupted_focus_probe_does_not_block_the_configured_key_pair(self):
        controller = Mock()
        reports = []
        advancer = DialogueAdvancer(
            "right",
            platform="linux",
            controller_factory=lambda: controller,
            diagnostic_handler=reports.append,
            diagnostic_probe=Mock(side_effect=KeyboardInterrupt()),
        )

        self.assertTrue(advancer.advance())

        self.assertEqual(
            controller.method_calls,
            [call.press(keyboard.Key.right), call.release(keyboard.Key.right)],
        )
        self.sleep.assert_not_called()
        self.assertEqual(reports[0]["before_focus_error_type"], "KeyboardInterrupt")
        self.assertTrue(reports[0]["success"])

    def test_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            DialogueAdvancer("escape")

    def test_macos_holds_one_native_key_pair_without_pynput(self):
        for key, code in (("space", 49), ("enter", 36), ("right", 124), ("down", 125)):
            with self.subTest(key=key):
                events = []
                quartz = Mock(kCGHIDEventTap=0)
                quartz.CGEventCreateKeyboardEvent.side_effect = ["down", "up"]
                quartz.CGEventPost.side_effect = lambda tap, event: events.append(event)
                self.sleep.side_effect = lambda duration: events.append(
                    ("hold", duration)
                )
                controller_factory = Mock()
                advancer = DialogueAdvancer(
                    key,
                    controller_factory=controller_factory,
                    platform="darwin",
                    quartz_module=quartz,
                )

                self.assertTrue(advancer.advance())

                self.assertEqual(events, ["down", ("hold", 0.05), "up"])
                self.assertEqual(
                    quartz.CGEventCreateKeyboardEvent.call_args_list,
                    [call(None, code, True), call(None, code, False)],
                )
                self.assertEqual(
                    quartz.CGEventPost.call_args_list,
                    [call(0, "down"), call(0, "up")],
                )
                controller_factory.assert_not_called()

    def test_macos_interrupted_hold_preserves_the_error_if_release_also_fails(self):
        error = SystemExit("hold interrupted")
        quartz = Mock(kCGHIDEventTap=0)
        quartz.CGEventCreateKeyboardEvent.side_effect = ("down", "up")
        quartz.CGEventPost.side_effect = (None, ValueError("release failed"))
        self.sleep.side_effect = error
        reports = []
        advancer = DialogueAdvancer(
            platform="darwin", quartz_module=quartz, diagnostic_handler=reports.append
        )

        with self.assertRaises(SystemExit) as raised:
            advancer.advance()

        self.assertIs(raised.exception, error)
        self.assertIn("release failed", error.__notes__[0])
        self.assertEqual(
            quartz.CGEventPost.call_args_list, [call(0, "down"), call(0, "up")]
        )
        [report] = reports
        self.assertFalse(report["success"])
        self.assertEqual(report["error_type"], "SystemExit")
        self.assertNotIn("press_error_type", report)
        self.assertEqual(report["release_error_type"], "ValueError")
        self.assertIn("release_completed_monotonic", report)

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
        self.sleep.assert_not_called()
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

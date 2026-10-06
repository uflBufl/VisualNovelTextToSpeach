import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pynput import keyboard  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.hotkey_ui import (  # noqa: E402
    HotkeyRecorder,
    hotkey_from_qt_sequence,
    hotkey_recording_errors,
    qt_sequence_from_hotkey,
)
from vntts.hotkeys import (  # noqa: E402
    HotkeyValidationError,
    default_hotkey,
    validate_hotkey_assignments,
)


class HotkeyValidationTest(unittest.TestCase):
    def test_new_macos_shortcuts_use_command(self):
        self.assertEqual(default_hotkey("h", platform="darwin"), "<cmd>+<shift>+h")
        self.assertEqual(default_hotkey("h", platform="win32"), "<ctrl>+<shift>+h")

    def test_semantically_duplicate_shortcuts_are_rejected(self):
        with self.assertRaisesRegex(HotkeyValidationError, "duplicates"):
            validate_hotkey_assignments(
                {
                    "Read once": "<ctrl>+<shift>+h",
                    "Live reading": "<shift>+<ctrl>+h",
                },
                platform="win32",
            )

    def test_assignment_errors_identify_the_failing_shortcut(self):
        for hotkey in ("<unknown>", "h", "<alt>+<f4>", "<ctrl>+h"):
            with self.subTest(hotkey=hotkey):
                with self.assertRaises(HotkeyValidationError) as raised:
                    validate_hotkey_assignments(
                        {"Read once": "<ctrl>+h", "Live reading": hotkey},
                        platform="win32",
                    )
                self.assertEqual(raised.exception.label, "Live reading")

    def test_modifier_only_shortcut_is_rejected(self):
        with self.assertRaisesRegex(HotkeyValidationError, "regular key"):
            validate_hotkey_assignments(
                {"Read once": "<ctrl>+<shift>"},
                platform="win32",
            )

    def test_modifier_virtual_key_codes_cannot_be_registered_as_regular_keys(self):
        for modifier in (keyboard.Key.ctrl_r, keyboard.Key.shift_r, keyboard.Key.alt_r):
            code = f"<{modifier.value.vk}>"
            for hotkey in (f"<ctrl>+{code}", f"<ctrl>+{code}+h"):
                with self.subTest(hotkey=hotkey):
                    with self.assertRaisesRegex(
                        HotkeyValidationError, "canonical modifier"
                    ):
                        validate_hotkey_assignments({"Read once": hotkey})

    def test_side_modifier_aliases_follow_the_listener_canonicalization(self):
        canonical = {keyboard.Key.ctrl, keyboard.Key.shift, keyboard.Key.alt}
        for name in ("ctrl_l", "ctrl_r", "shift_l", "shift_r", "alt_l", "alt_r"):
            hotkey = f"<{name}>+h"
            parsed = keyboard.HotKey.parse(hotkey)
            with self.subTest(hotkey=hotkey):
                if parsed[0] in canonical:
                    validate_hotkey_assignments({"Read once": hotkey})
                else:
                    with self.assertRaisesRegex(
                        HotkeyValidationError, "canonical modifier"
                    ):
                        validate_hotkey_assignments({"Read once": hotkey})

    def test_unmodified_shortcut_is_rejected(self):
        with self.assertRaisesRegex(HotkeyValidationError, "modifiers"):
            validate_hotkey_assignments(
                {"Read once": "h"},
                platform="win32",
            )

    def test_operating_system_shortcut_is_rejected(self):
        with self.assertRaisesRegex(HotkeyValidationError, "reserved"):
            validate_hotkey_assignments(
                {"Read once": "<cmd>+<space>"},
                platform="darwin",
            )


class HotkeyRecorderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_assignment_validation_targets_the_second_recorder(self):
        read = HotkeyRecorder("<ctrl>+h", platform="win32")
        live = HotkeyRecorder("<ctrl>+l", platform="win32")
        reserved = "<cmd>+<space>" if sys.platform == "darwin" else "<alt>+<f4>"
        for hotkey in ("h", "<ctrl>+h", reserved):
            with self.subTest(hotkey=hotkey):
                live.set_hotkey(hotkey)
                errors = hotkey_recording_errors(
                    {"Read once": read, "Live reading": live}
                )
                self.assertEqual(len(errors), 1)
                self.assertIs(errors[0][0], live)
                self.assertIn("Live reading", errors[0][1])

    def test_macos_recorder_round_trips_command_shortcut(self):
        recorder = HotkeyRecorder("<cmd>+<shift>+h", platform="darwin")

        self.assertEqual(recorder.hotkey(), "<cmd>+<shift>+h")
        self.assertEqual(
            recorder.keySequence().toString(QKeySequence.SequenceFormat.PortableText),
            "Ctrl+Shift+H",
        )

    def test_macos_recorder_maps_physical_control_and_function_key(self):
        recorder = HotkeyRecorder("<cmd>+h", platform="darwin")

        recorder.setKeySequence(QKeySequence("Meta+Alt+F2"))

        self.assertEqual(recorder.hotkey(), "<ctrl>+<alt>+<f2>")

    def test_recorder_round_trips_named_qt_keys_and_plus(self):
        for hotkey in (
            "<ctrl>+<caps_lock>",
            "<ctrl>+<media_play_pause>",
            "<ctrl>+<media_volume_up>",
            "<ctrl>++",
        ):
            recorder = HotkeyRecorder(hotkey, platform="win32")
            self.assertEqual(recorder.hotkey(), hotkey)

    def test_recorder_rejects_multi_stroke_sequences(self):
        with self.assertRaisesRegex(HotkeyValidationError, "press a shortcut"):
            hotkey_from_qt_sequence(QKeySequence("Ctrl+H, Ctrl+J"), platform="win32")

    def test_converter_rejects_unknown_named_and_function_keys(self):
        for hotkey in (
            "<ctrl>+<media_play>",
            "<ctrl>+<media_eject>",
            "<ctrl>+<f36>",
            "<ctrl>+<f¹>",
            "<ctrl>+<f" + "9" * 5000 + ">",
            "<ctrl>+ß",
        ):
            with self.subTest(hotkey=hotkey):
                with self.assertRaises(HotkeyValidationError):
                    qt_sequence_from_hotkey(hotkey, platform="win32")
                recorder = HotkeyRecorder(hotkey, platform="win32")
                self.assertTrue(recorder.keySequence().isEmpty())

    def test_recorder_captures_complete_shortcut_from_key_event(self):
        recorder = HotkeyRecorder("<cmd>+h", platform="darwin")
        recorder.clear()
        recorder.show()
        recorder.setFocus()

        QTest.keyClick(
            recorder,
            Qt.Key.Key_H,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        self.application.processEvents()

        self.assertEqual(recorder.hotkey(), "<cmd>+<shift>+h")
        recorder.close()


if __name__ == "__main__":
    unittest.main()

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.hotkey_ui import HotkeyRecorder  # noqa: E402
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

    def test_modifier_only_shortcut_is_rejected(self):
        with self.assertRaisesRegex(HotkeyValidationError, "regular key"):
            validate_hotkey_assignments(
                {"Read once": "<ctrl>+<shift>"},
                platform="win32",
            )

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

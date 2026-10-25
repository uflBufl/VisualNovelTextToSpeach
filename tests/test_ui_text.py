import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vntts.ui_text import (  # noqa: E402
    copy_text,
    copy_text_button,
    make_text_copyable,
    shortcut_tooltip,
)


class UiTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_copyable_wrapped_text_keeps_width_dependent_layout_height(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        label = QLabel("Reading details should wrap to fit the available width. " * 8)
        label.setWordWrap(True)
        policy = label.sizePolicy()
        policy.setVerticalPolicy(QSizePolicy.Policy.Maximum)
        policy.setHorizontalStretch(2)
        policy.setVerticalStretch(3)
        label.setSizePolicy(policy)
        layout.addWidget(label)

        make_text_copyable(root)

        self.assertEqual(
            label.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Ignored
        )
        self.assertEqual(
            label.sizePolicy().verticalPolicy(), QSizePolicy.Policy.Maximum
        )
        self.assertEqual(label.sizePolicy().horizontalStretch(), 2)
        self.assertEqual(label.sizePolicy().verticalStretch(), 3)
        self.assertTrue(label.hasHeightForWidth())
        self.assertTrue(layout.hasHeightForWidth())
        self.assertGreater(
            layout.totalHeightForWidth(160), layout.totalHeightForWidth(640)
        )
        root.deleteLater()

    def test_copy_confirms_exact_current_text_without_changing_button(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        current = ["<literal> full diagnostics\n" + "details " * 100]
        button = copy_text_button("Copy status", lambda: current[0], root)
        button.setAccessibleName("Copy current status")
        button.setToolTip("Copy the complete status")
        layout.addWidget(button)
        root.show()
        self.application.processEvents()
        self.addCleanup(root.deleteLater)
        self.addCleanup(root.close)
        initial_size = button.sizeHint()

        with (
            patch("vntts.ui_text.QToolTip.showText") as popup,
            patch("vntts.ui_text.QAccessible.updateAccessibility") as announcement,
        ):
            for value in (current[0], "New status", ""):
                current[0] = value
                button.click()
                self.assertEqual(self.application.clipboard().text(), value)
                self.assertEqual(popup.call_args.args[1], "Copied to clipboard")
                self.assertEqual(
                    announcement.call_args.args[0].message(), "Copied to clipboard"
                )
                self.assertEqual(button.text(), "Copy status")
                self.assertEqual(button.toolTip(), "Copy the complete status")
                self.assertEqual(button.accessibleName(), "Copy current status")
                self.assertEqual(button.sizeHint(), initial_size)

    def test_failed_clipboard_write_does_not_announce_success(self):
        source = QWidget()
        self.addCleanup(source.deleteLater)
        clipboard = Mock()
        clipboard.setText.side_effect = RuntimeError("Clipboard unavailable")
        with (
            patch("vntts.ui_text.QApplication.clipboard", return_value=clipboard),
            patch("vntts.ui_text.QToolTip.showText") as popup,
            patch("vntts.ui_text.QAccessible.updateAccessibility") as announcement,
        ):
            with self.assertRaisesRegex(RuntimeError, "Clipboard unavailable"):
                copy_text("full details", source)
        popup.assert_not_called()
        announcement.assert_not_called()

    def test_shortcut_hints_preserve_reason_and_use_actual_native_keys(self):
        reason = "Listen to the exact recording before approving"
        self.assertEqual(shortcut_tooltip(reason, QKeySequence()), reason)
        for values in (("Ctrl+R",), ("Ctrl+Return", "Ctrl+Enter")):
            keys = tuple(QKeySequence(value) for value in values)
            tip = shortcut_tooltip(reason, *keys)
            self.assertTrue(tip.startswith(reason + "\n"))
            for key in keys:
                self.assertIn(key.toString(QKeySequence.SequenceFormat.NativeText), tip)

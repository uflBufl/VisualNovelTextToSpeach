import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vntts.ui_text import make_text_copyable  # noqa: E402


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

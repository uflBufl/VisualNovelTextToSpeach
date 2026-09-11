"""Small shared text and clipboard helpers for player windows."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
)


def make_text_copyable(root):
    captions = {
        item.widget()
        for form in root.findChildren(QFormLayout)
        for row in range(form.rowCount())
        if (item := form.itemAt(row, QFormLayout.ItemRole.LabelRole)) is not None
    }
    for label in root.findChildren(QLabel):
        label.setTextInteractionFlags(
            label.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        if label.wordWrap() and label not in captions:
            label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )


def copy_text_button(label, text_provider, parent=None):
    button = QPushButton(label, parent)
    button.clicked.connect(lambda: QApplication.clipboard().setText(text_provider()))
    button.setToolTip(
        "Copy the full text, including details that do not fit on screen."
    )
    return button

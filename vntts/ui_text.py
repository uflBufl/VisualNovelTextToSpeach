"""Small shared text and clipboard helpers for player windows."""

from collections.abc import Callable, Iterable
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)


def make_text_copyable(root: QWidget) -> None:
    captions = {
        item.widget()
        for form in root.findChildren(QFormLayout)
        for row in range(form.rowCount())
        if (item := form.itemAt(row, QFormLayout.ItemRole.LabelRole)) is not None
    }
    for caption in captions:
        if isinstance(caption, QLabel):
            font = caption.font()
            font.setBold(True)
            caption.setFont(font)
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


def set_labeled_text(
    label: QLabel, rows: Iterable[tuple[object, object]]
) -> None:
    """Render semantic rows without treating names, paths or dialogue as markup."""
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(True)
    label.setText(
        "<br>".join(
            f"<b>{escape(str(caption))}:</b> {escape(str(value))}"
            for caption, value in rows
        )
    )


def plain_label_text(label: QLabel) -> str:
    if label.textFormat() != Qt.TextFormat.RichText:
        return label.text()
    document = QTextDocument()
    document.setHtml(label.text())
    return document.toPlainText()


def copy_text_button(
    label: str,
    text_provider: Callable[[], str],
    parent: QWidget | None = None,
) -> QPushButton:
    button = QPushButton(label, parent)
    button.clicked.connect(lambda: QApplication.clipboard().setText(text_provider()))
    button.setToolTip(
        "Copy the full text, including details that do not fit on screen."
    )
    return button

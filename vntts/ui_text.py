"""Small shared text and clipboard helpers for player windows."""

from collections.abc import Callable, Iterable
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QAccessible,
    QAccessibleAnnouncementEvent,
    QKeySequence,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QToolTip,
    QWidget,
)

from vntts.action_icons import set_action_icon


def add_composite_form_row(
    form: QFormLayout, label_text: str, field: QWidget, field_layout: QLayout
) -> QLabel:
    label = QLabel(label_text)
    label.setBuddy(field)
    form.addRow(label, field_layout)
    return label


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
            policy = label.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            label.setSizePolicy(policy)


def set_labeled_text(label: QLabel, rows: Iterable[tuple[object, object]]) -> None:
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
    set_action_icon(button, "copy")
    button.clicked.connect(lambda: copy_text(text_provider(), button))
    button.setToolTip(
        "Copy the full text, including details that do not fit on screen."
    )
    return button


def copy_text(text: str, source: QWidget) -> None:
    QApplication.clipboard().setText(text)
    if source.isVisible():
        message = "Copied to clipboard"
        QToolTip.showText(
            source.mapToGlobal(source.rect().bottomLeft()),
            message,
            source,
            source.rect(),
            1500,
        )
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(source, message))


def shortcut_tooltip(text: str, *shortcuts: QKeySequence) -> str:
    keys = [
        shortcut.toString(QKeySequence.SequenceFormat.NativeText)
        for shortcut in shortcuts
        if not shortcut.isEmpty()
    ]
    if not keys:
        return text
    label = "Shortcut" if len(keys) == 1 else "Shortcuts"
    return f"{text}\n{label}: {', '.join(keys)}"

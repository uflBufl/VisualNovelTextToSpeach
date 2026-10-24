"""Bundled Lucide icons using ordinary Qt icons and the button palette."""

from importlib.resources import files

from PySide6.QtCore import QEvent, QObject, Qt, Slot
from PySide6.QtGui import QAction, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QApplication


def action_icon(name: str, palette: QPalette | None = None) -> QIcon:
    svg = files("vntts").joinpath("icons", "lucide", f"{name}.svg").read_bytes()
    palette = palette or QApplication.palette()
    icon = QIcon()
    # Native pixmap icons avoid PySide's unsafe Python QIconEngine.clone lifetime.
    for mode, group, role in (
        (QIcon.Mode.Normal, QPalette.ColorGroup.Active, QPalette.ColorRole.ButtonText),
        (
            QIcon.Mode.Disabled,
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.ButtonText,
        ),
        (
            QIcon.Mode.Selected,
            QPalette.ColorGroup.Active,
            QPalette.ColorRole.HighlightedText,
        ),
    ):
        color = palette.color(group, role)
        renderer = QSvgRenderer(
            svg.replace(b"currentColor", color.name().encode("ascii"))
        )
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setOpacity(color.alphaF())
            renderer.render(painter)
        finally:
            painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


class _IconPaletteFilter(QObject):
    @Slot(QPalette)
    def update_action_palette(self, palette: QPalette) -> None:
        action = self.parent()
        if isinstance(action, QAction):
            name = action.property("vnttsActionIcon")
            if isinstance(name, str):
                action.setIcon(action_icon(name, palette))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.PaletteChange and isinstance(
            watched, QAbstractButton
        ):
            name = watched.property("vnttsActionIcon")
            if isinstance(name, str):
                watched.setIcon(action_icon(name, watched.palette()))
        return False


def set_action_icon(button: QAbstractButton | QAction, name: str) -> None:
    if button.property("vnttsActionIcon") is None:
        observer = _IconPaletteFilter(button)
        if isinstance(button, QAction):
            application = QApplication.instance()
            assert isinstance(application, QApplication)
            application.paletteChanged.connect(observer.update_action_palette)
        else:
            button.installEventFilter(observer)
    button.setProperty("vnttsActionIcon", name)
    if isinstance(button, QAction):
        button.setIconVisibleInMenu(True)
        button.setIcon(action_icon(name))
    else:
        button.setIcon(action_icon(name, button.palette()))

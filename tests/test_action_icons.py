import os
import unittest
from importlib.resources import files

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSize  # noqa: E402
from PySide6.QtGui import QAction, QColor, QIcon, QPalette  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from vntts.action_icons import action_icon, set_action_icon  # noqa: E402
from vntts.dashboard_ui import (  # noqa: E402
    CompactController,
    ControlDashboard,
    RuntimeControlState,
)
from vntts.settings import AppSettings  # noqa: E402


class ActionIconsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_bundled_icons_are_valid_and_render_visible_pixels(self):
        assets = files("vntts").joinpath("icons", "lucide")
        self.assertIn("ISC License", assets.joinpath("LICENSE").read_text())
        icons = [path for path in assets.iterdir() if path.name.endswith(".svg")]
        self.assertTrue(icons)
        for path in icons:
            with self.subTest(icon=path.name):
                self.assertTrue(QSvgRenderer(path.read_bytes()).isValid())
                image = action_icon(path.name.removesuffix(".svg")).pixmap(24).toImage()
                self.assertTrue(
                    any(
                        image.pixelColor(x, y).alpha()
                        for x in range(24)
                        for y in range(24)
                    )
                )

    def test_existing_icon_follows_light_dark_and_disabled_palette(self):
        original = self.application.palette()
        button = QPushButton("Play")
        set_action_icon(button, "play")
        self.addCleanup(button.deleteLater)
        try:
            for foreground, disabled in (
                ("#172839", "#697887"),
                ("#eef3fa", "#a1a4a8"),
            ):
                palette = QPalette(original)
                palette.setColor(QPalette.ColorRole.ButtonText, QColor(foreground))
                palette.setColor(
                    QPalette.ColorGroup.Disabled,
                    QPalette.ColorRole.ButtonText,
                    QColor(disabled),
                )
                self.application.setPalette(palette)
                self.application.processEvents()
                for mode, expected in (
                    (QIcon.Mode.Normal, foreground),
                    (QIcon.Mode.Disabled, disabled),
                ):
                    with self.subTest(foreground=foreground, mode=mode):
                        image = button.icon().pixmap(QSize(48, 48), mode).toImage()
                        colors = {
                            image.pixelColor(x, y).name()
                            for x in range(48)
                            for y in range(48)
                            if image.pixelColor(x, y).alpha() == 255
                        }
                        self.assertEqual(colors, {expected})
        finally:
            self.application.setPalette(original)

    def test_menu_icons_follow_palette_and_latest_action_until_deleted(self):
        original = self.application.palette()
        action = QAction("Read")
        set_action_icon(action, "play")
        try:
            self.assertTrue(action.isIconVisibleInMenu())
            for icon, foreground in (("play", "#172839"), ("square", "#eef3fa")):
                set_action_icon(action, icon)
                palette = QPalette(original)
                palette.setColor(QPalette.ColorRole.ButtonText, QColor(foreground))
                self.application.setPalette(palette)
                self.application.processEvents()
                for mode in (
                    QIcon.Mode.Normal,
                    QIcon.Mode.Disabled,
                    QIcon.Mode.Selected,
                ):
                    self.assertEqual(
                        action.icon().pixmap(QSize(24, 24), mode).toImage(),
                        action_icon(icon).pixmap(QSize(24, 24), mode).toImage(),
                    )
        finally:
            action.deleteLater()
            QCoreApplication.sendPostedEvents(action, QEvent.Type.DeferredDelete)
            self.application.setPalette(original)

    def test_icons_can_be_copied_and_modified_independently(self):
        original = action_icon("play")
        before = original.pixmap(24).toImage()
        copied = QIcon(original)
        copied.addPixmap(action_icon("square").pixmap(24))
        self.assertEqual(original.pixmap(24).toImage(), before)
        self.assertNotEqual(copied.pixmap(24).toImage(), before)

    def test_native_palette_alpha_is_preserved_for_disabled_icons(self):
        original = self.application.palette()
        palette = QPalette(original)
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255, 216))
        palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.ButtonText,
            QColor(255, 255, 255, 63),
        )
        try:
            self.application.setPalette(palette)
            icon = action_icon("square")
            for mode, expected_alpha in (
                (QIcon.Mode.Normal, 216),
                (QIcon.Mode.Disabled, 63),
            ):
                image = icon.pixmap(QSize(48, 48), mode).toImage()
                self.assertAlmostEqual(
                    max(
                        image.pixelColor(x, y).alpha()
                        for x in range(48)
                        for y in range(48)
                    ),
                    expected_alpha,
                    delta=1,
                )
        finally:
            self.application.setPalette(original)

    def test_full_and_compact_reading_icons_follow_the_available_action(self):
        dashboard = ControlDashboard(AppSettings())
        compact = CompactController()
        try:
            for state, live_icon, pause_icon in (
                (RuntimeControlState(ready=True), "play", "pause"),
                (RuntimeControlState(ready=True, live=True), "square", "pause"),
                (RuntimeControlState(ready=True, paused=True), "square", "play"),
                (RuntimeControlState(transition="starting"), "x", "pause"),
                (RuntimeControlState(transition="stopping"), "square", "pause"),
            ):
                for window in (dashboard, compact):
                    with self.subTest(state=state, window=type(window).__name__):
                        window.set_runtime_controls(state)
                        for button, expected in (
                            (window.live_button, live_icon),
                            (window.pause_button, pause_icon),
                            (window.read_button, "volume-2"),
                        ):
                            self.assertEqual(
                                button.icon().pixmap(24).toImage(),
                                action_icon(expected).pixmap(24).toImage(),
                            )
        finally:
            dashboard.deleteLater()
            compact.deleteLater()

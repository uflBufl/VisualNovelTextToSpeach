import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from vntts.playback_button import set_playback_button_state  # noqa: E402


class PlaybackButtonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_states_keep_caption_size_shortcut_and_accessible_feedback(self):
        button = QPushButton("Original")
        self.addCleanup(button.deleteLater)
        button.setShortcut(QKeySequence("Ctrl+P"))
        set_playback_button_state(button, "idle", label="original reference")
        size = button.sizeHint()
        for state, icon, action in (
            ("idle", "play", "Play"),
            ("preparing", "refresh-cw", "Preparing"),
            ("playing", "square", "Stop"),
            ("idle", "play", "Play"),
        ):
            with self.subTest(state=state):
                set_playback_button_state(button, state, label="original reference")
                self.assertEqual(button.property("playbackState"), state)
                self.assertEqual(button.property("vnttsActionIcon"), icon)
                self.assertEqual(button.text(), "Original")
                self.assertEqual(button.sizeHint(), size)
                self.assertEqual(
                    button.accessibleName(), f"{action} original reference"
                )
                self.assertIn("Shortcut:", button.toolTip())
                with patch("vntts.playback_button.set_action_icon") as render:
                    set_playback_button_state(button, state, label="original reference")
                    render.assert_not_called()

    def test_custom_shortcut_tooltip_survives_state_changes(self):
        button = QPushButton("Recording")
        self.addCleanup(button.deleteLater)
        button.setProperty("playbackShortcut", QKeySequence("Space"))
        set_playback_button_state(button, "preparing")
        self.assertIn("Space", button.toolTip())

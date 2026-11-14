"""Consistent playback feedback without changing a button's caption or size."""

from typing import Literal

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QAbstractButton

from vntts.action_icons import set_action_icon
from vntts.ui_text import shortcut_tooltip

type PlaybackState = Literal["idle", "preparing", "playing"]


def set_playback_button_state(
    button: QAbstractButton, state: PlaybackState, *, label: str = "audio"
) -> None:
    icon, action = {
        "idle": ("play", "Play"),
        "preparing": ("refresh-cw", "Preparing"),
        "playing": ("square", "Stop"),
    }[state]
    button.setProperty("playbackState", state)
    if button.property("vnttsActionIcon") != icon:
        set_action_icon(button, icon)
    description = f"{action} {label}"
    button.setAccessibleName(description)
    shortcut = button.property("playbackShortcut")
    button.setToolTip(
        shortcut_tooltip(
            description,
            shortcut if isinstance(shortcut, QKeySequence) else button.shortcut(),
        )
    )

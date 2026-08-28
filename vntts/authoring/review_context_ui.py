"""Shared, explicit context card for authoring decision interfaces."""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGroupBox, QLabel, QToolButton, QVBoxLayout


class ReviewDecisionContext(QGroupBox):
    """Present the same operator-facing decision facts across review dialogs."""

    FIELD_ORDER = (
        ("purpose", "You are deciding"),
        ("game_speaker", "Speaker in game"),
        ("synthesis_voice", "Voice used"),
        ("reference", "Reference audio"),
        ("backend", "Backend"),
        ("model", "Model"),
        ("generation_profile", "Generation profile"),
        ("controls", "Generation controls"),
        ("effect", "Your decision will"),
    )

    def __init__(self, parent=None):
        super().__init__("Decision context", parent)
        self.setAccessibleName("Authoring decision context")
        self.values = {}
        for key, label in self.FIELD_ORDER:
            value = QLabel("Unknown")
            value.setAccessibleName(label)
            value.hide()
            self.values[key] = value

        self.purpose = self._summary_label("Decision purpose and effect")
        self.identity = self._summary_label("Speaker voice and reference")
        self.synthesis = self._summary_label("Synthesis configuration")
        self.effect = self._summary_label("Decision consequence")

        self.technical_toggle = QToolButton()
        self.technical_toggle.setText("Technical authority details")
        self.technical_toggle.setCheckable(True)
        self.technical_toggle.setChecked(False)
        self.technical_toggle.setAccessibleName("Show technical authority details")
        self.technical = QLabel()
        self.technical.setWordWrap(True)
        self.technical.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.technical.setAccessibleName("Technical authoring authority details")
        self.technical.hide()
        self.technical_toggle.toggled.connect(self.technical.setVisible)

        layout = QVBoxLayout(self)
        layout.setSpacing(3)
        layout.addWidget(self.purpose)
        layout.addWidget(self.identity)
        layout.addWidget(self.synthesis)
        layout.addWidget(self.effect)
        layout.addWidget(self.technical_toggle)
        layout.addWidget(self.technical)

    @staticmethod
    def _summary_label(accessible_name):
        label = QLabel()
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setAccessibleName(accessible_name)
        return label

    def set_context(self, values: Mapping[str, object], *, technical=""):
        """Update every canonical field; absent values stay explicit."""
        for key, _label in self.FIELD_ORDER:
            value = values.get(key, "Unknown")
            text = str(value).strip() if value is not None else ""
            self.values[key].setText(text or "Unknown")
        value = {key: widget.text() for key, widget in self.values.items()}
        self.purpose.setText(f"You are deciding: {value['purpose']}")
        self.identity.setText(
            f"Speaker in game: {value['game_speaker']} | "
            f"Voice used: {value['synthesis_voice']} | "
            f"Reference: {value['reference']}"
        )
        self.synthesis.setText(
            f"Synthesis: {value['backend']} | {value['model']} | "
            f"{value['generation_profile']} | {value['controls']}"
        )
        self.effect.setText(f"Your decision will: {value['effect']}")
        details = str(technical or "").strip()
        self.technical.setText(details or "No additional technical details.")
        self.technical_toggle.setVisible(bool(details))
        if not details:
            self.technical_toggle.setChecked(False)


__all__ = ["ReviewDecisionContext"]

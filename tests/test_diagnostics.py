import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.diagnostics import (  # noqa: E402
    DiagnosticSnapshot,
    diagnostic_error_guidance,
    macos_permission_warnings,
    resolve_voice_label,
)
from vntts.diagnostics_ui import DiagnosticsDialog  # noqa: E402
from vntts.main import AppController, analyze_dialog_snapshot  # noqa: E402
from vntts.ocr import OCRResult  # noqa: E402
from vntts.voices import CharacterVoice  # noqa: E402


class DiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_snapshot_preserves_capture_ocr_and_voice_details(self):
        image = Image.new("RGB", (320, 100), "black")
        result = OCRResult(
            "Marcus",
            "Timekeeper.",
            92.5,
            "dark-background",
            2,
            ("Mareus -> Marcus",),
        )
        snapshots = []
        clock = iter((1.0, 1.025, 2.0, 2.075)).__next__

        with (
            patch("vntts.dialog_capture.capture_dialog", return_value=(image, None)),
            patch(
                "vntts.dialog_capture.recognize_screenshot_result", return_value=result
            ),
        ):
            _, _, actual = analyze_dialog_snapshot(
                "captures",
                diagnostic_handler=snapshots.append,
                voice_resolver=lambda character: f"Voice for {character}",
                clock=clock,
            )

        self.assertIs(actual, result)
        self.assertEqual(snapshots[0].image, image)
        self.assertEqual(snapshots[0].character, "Marcus")
        self.assertEqual(snapshots[0].text, "Timekeeper.")
        self.assertEqual(snapshots[0].preprocessing_profile, "dark-background")
        self.assertEqual(snapshots[0].voice, "Voice for Marcus")
        self.assertAlmostEqual(snapshots[0].capture_ms, 25.0)
        self.assertAlmostEqual(snapshots[0].ocr_ms, 75.0)
        self.assertEqual(snapshots[0].corrections, ("Mareus -> Marcus",))

    def test_dialog_renders_snapshot_and_latencies(self):
        dialog = DiagnosticsDialog()
        self.assertLessEqual(dialog.width(), 700)
        self.assertLessEqual(dialog.height(), 540)
        snapshot = DiagnosticSnapshot(
            Image.new("RGB", (320, 100), "black"),
            character="Marcus",
            text="The captured line.",
            confidence=91.2,
            preprocessing_profile="balanced",
            voice="Marcus (reverse1999-marcus)",
            capture_ms=12.3,
            ocr_ms=45.6,
            synthesis_ms=789.0,
            playback_ms=321.0,
            capture_interval_ms=600.0,
            game_focused=False,
            corrections=("Mareus -> Marcus", "tiniekeeper -> timekeeper"),
        )

        dialog.set_snapshot(snapshot)

        self.assertEqual(dialog.speaker.text(), "Marcus")
        self.assertEqual(dialog.text.toPlainText(), "The captured line.")
        self.assertEqual(dialog.confidence.text(), "91.2%")
        self.assertEqual(dialog.preprocessing.text(), "balanced")
        self.assertEqual(dialog.voice.text(), "Marcus (reverse1999-marcus)")
        self.assertEqual(dialog.capture_latency.text(), "12.3 ms")
        self.assertEqual(dialog.capture_interval.text(), "600.0 ms")
        self.assertEqual(dialog.game_focus.text(), "No")
        self.assertEqual(
            dialog.corrections.text(),
            "Mareus -> Marcus\ntiniekeeper -> timekeeper",
        )
        self.assertFalse(dialog.preview.pixmap().isNull())
        dialog.close()
        dialog.deleteLater()

    def test_dialog_can_be_concealed_during_capture_and_restored(self):
        dialog = DiagnosticsDialog()
        dialog.show()
        self.application.processEvents()

        self.assertTrue(dialog.conceal_for_capture())
        self.assertFalse(dialog.isVisible())
        self.assertTrue(dialog.concealed_for_capture)

        dialog.restore_after_capture()
        self.application.processEvents()

        self.assertTrue(dialog.isVisible())
        self.assertFalse(dialog.concealed_for_capture)
        dialog.close()
        dialog.deleteLater()

    def test_opening_dialog_does_not_request_a_capture(self):
        dialog = DiagnosticsDialog()
        refresh_requested = Mock()
        dialog.refresh_requested.connect(refresh_requested)

        dialog.show()
        self.application.processEvents()

        refresh_requested.assert_not_called()
        dialog.request_refresh()
        refresh_requested.assert_called_once_with()
        dialog.close()
        dialog.deleteLater()

    def test_macos_permission_warnings_explain_both_permissions(self):
        warnings = macos_permission_warnings(
            platform="darwin",
            screen_capture_trusted=lambda: False,
            accessibility_trusted=lambda: False,
        )

        self.assertEqual(len(warnings), 2)
        self.assertIn("Screen & System Audio Recording", warnings[0])
        self.assertIn("Accessibility", warnings[1])
        self.assertIn("auto advance", warnings[1])
        self.assertNotIn("global hotkeys", warnings[1])

    def test_adaptive_capture_state_updates_live_diagnostics(self):
        snapshots = []
        controller = AppController(
            diagnostic_handler=snapshots.append,
            model_asset_manager_factory=Mock,
        )
        controller.last_diagnostic = DiagnosticSnapshot(None, text="Stable")

        controller._capture_state_changed(False, 1.6)

        self.assertFalse(snapshots[-1].game_focused)
        self.assertEqual(snapshots[-1].capture_interval_ms, 1600.0)

    def test_unavailable_window_has_actionable_guidance(self):
        guidance = diagnostic_error_guidance(
            RuntimeError("Selected window is unavailable"),
            platform="darwin",
        )

        self.assertIn("Start or restore the game", guidance)
        self.assertIn("borderless", guidance)

    def test_voice_label_reports_character_and_narrator_routes(self):
        voice = CharacterVoice("Marcus", "reverse1999-marcus")
        router = Mock()
        router.registry.resolve.side_effect = lambda character: (
            voice if character == "Marcus" else None
        )
        router.narrator_speaker = "Claribel Dervla"

        self.assertEqual(
            resolve_voice_label(router, "Marcus"),
            "Marcus (reverse1999-marcus)",
        )
        self.assertEqual(
            resolve_voice_label(router, "Narrator"),
            "Claribel Dervla",
        )


if __name__ == "__main__":
    unittest.main()

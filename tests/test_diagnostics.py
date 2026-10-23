import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from vntts.diagnostics import (  # noqa: E402
    DiagnosticSnapshot,
    diagnostic_error_guidance,
    diagnostic_remediation,
    macos_permission_warnings,
    resolve_voice_label,
)
from vntts.diagnostics_ui import DiagnosticsDialog  # noqa: E402
from vntts.dialog_capture import CapturedDialogFrame, recognize_live_frame  # noqa: E402
from vntts.main import AppController, analyze_dialog_snapshot  # noqa: E402
from vntts.ocr import OCRResult  # noqa: E402
from vntts.voices import CharacterVoice  # noqa: E402


class DiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_snapshot_preserves_capture_ocr_and_voice_details(self):
        image = Image.new("RGB", (320, 100), "black")
        processed_image = Image.new("L", (600, 120), "white")
        result = OCRResult(
            "Marcus",
            "Timekeeper.",
            92.5,
            "dark-background",
            2,
            ("Mareus -> Marcus",),
            processed_image=processed_image,
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
        self.assertIs(snapshots[0].processed_image, processed_image)
        self.assertEqual(snapshots[0].character, "Marcus")
        self.assertEqual(snapshots[0].text, "Timekeeper.")
        self.assertEqual(snapshots[0].preprocessing_profile, "dark-background")
        self.assertEqual(snapshots[0].voice, "Voice for Marcus")
        self.assertAlmostEqual(snapshots[0].capture_ms, 25.0)
        self.assertAlmostEqual(snapshots[0].ocr_ms, 75.0)
        self.assertEqual(snapshots[0].corrections, ("Mareus -> Marcus",))

    def test_live_snapshot_preserves_actual_ocr_input(self):
        image = Image.new("RGB", (320, 100), "black")
        processed_image = Image.new("L", (600, 120), "white")
        result = OCRResult(
            "Marcus",
            "Hello?",
            95.0,
            "balanced",
            1,
            processed_image=processed_image,
        )
        snapshots = []
        with patch(
            "vntts.dialog_capture.recognize_screenshot_result", return_value=result
        ):
            recognized = recognize_live_frame(
                CapturedDialogFrame(image, 12.0),
                diagnostic_handler=snapshots.append,
            )

        self.assertEqual(recognized, ("Marcus", "Hello?"))
        self.assertIs(snapshots[0].processed_image, processed_image)

    def test_story_speaker_provenance_is_plain_text_and_clears_for_the_next_snapshot(
        self,
    ):
        dialog = DiagnosticsDialog()
        note = "Speaker from prepared story; raw OCR: <a ae Ce>"
        dialog.set_snapshot(DiagnosticSnapshot(None, speaker_note=note))
        self.assertEqual(dialog.speaker_note.text(), note)
        self.assertEqual(dialog.speaker_note.textFormat(), Qt.TextFormat.PlainText)
        self.assertFalse(dialog.speaker_note.isHidden())
        dialog.set_snapshot(DiagnosticSnapshot(None))
        self.assertTrue(dialog.speaker_note.isHidden())
        self.assertEqual(dialog.speaker_note.text(), "")
        dialog.close()

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
            processed_image=Image.new("L", (600, 120), "white"),
        )

        dialog.set_snapshot(snapshot)

        self.assertEqual(dialog.speaker.text(), "Marcus")
        self.assertEqual(dialog.text.text(), "The captured line.")
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
        self.assertFalse(dialog.processed_preview.pixmap().isNull())
        self.assertTrue(dialog.enlarge_processed_button.isEnabled())
        self.assertEqual(dialog.processed_pixmap.size().width(), 600)
        self.assertEqual(
            dialog.processed_pixmap.toImage().pixelColor(0, 0).name(), "#ffffff"
        )
        dialog.close()
        dialog.deleteLater()

    def test_dialog_renders_independent_speaker_and_dialogue_ocr(self):
        dialog = DiagnosticsDialog()
        speaker_image = Image.new("L", (500, 80), "#aaaaaa")
        dialogue_image = Image.new("L", (600, 120), "#ffffff")
        snapshot = DiagnosticSnapshot(
            Image.new("RGB", (320, 100), "black"),
            character="Marcus",
            text="The captured line.",
            confidence=91.2,
            speaker_image=speaker_image,
            speaker_confidence=84.5,
            dialogue_confidence=96.7,
            processed_image=dialogue_image,
        )

        dialog.set_snapshot(snapshot)

        self.assertEqual(dialog.speaker.text(), "Marcus")
        self.assertEqual(dialog.speaker_confidence.text(), "84.5%")
        self.assertEqual(dialog.dialogue_confidence.text(), "96.7%")
        self.assertFalse(dialog.speaker_processed_preview.pixmap().isNull())
        self.assertTrue(dialog.enlarge_speaker_button.isEnabled())
        speaker_zoom = dialog._capture_dialog(speaker=True)
        self.assertIsNotNone(speaker_zoom)
        self.assertIn("speaker OCR", speaker_zoom.windowTitle())
        speaker_zoom.close()
        dialog.close()
        dialog.deleteLater()

    def test_legacy_snapshot_and_unavailable_snapshot_clear_both_ocr_images(self):
        dialog = DiagnosticsDialog()
        dialog.set_snapshot(
            DiagnosticSnapshot(
                Image.new("RGB", (32, 10), "black"),
                text="Old",
                processed_image=Image.new("L", (64, 20), "white"),
                speaker_image=Image.new("L", (48, 12), "gray"),
            )
        )
        self.assertEqual(dialog.confidence.text(), "0.0%")
        self.assertTrue(dialog.enlarge_speaker_button.isEnabled())

        dialog.set_snapshot(DiagnosticSnapshot(None, text="Unavailable"))

        self.assertIsNone(dialog.processed_pixmap)
        self.assertIsNone(dialog.speaker_processed_pixmap)
        self.assertFalse(dialog.enlarge_processed_button.isEnabled())
        self.assertFalse(dialog.enlarge_speaker_button.isEnabled())
        self.assertIn("No processed OCR image", dialog.processed_preview.text())
        self.assertIn(
            "No processed speaker OCR image", dialog.speaker_processed_preview.text()
        )
        dialog.close()
        dialog.deleteLater()

    def test_dialog_can_be_concealed_during_capture_and_restored(self):
        dialog = DiagnosticsDialog()
        dialog.show()
        self.application.processEvents()

        self.assertTrue(dialog.conceal_for_capture())
        self.assertFalse(dialog.isVisible())
        self.assertTrue(dialog.concealed_for_capture)

        attributes = []
        original_show = dialog.show

        def show():
            attributes.append(
                dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            )
            original_show()

        with (
            patch.object(dialog, "show", side_effect=show),
            patch.object(dialog, "activateWindow") as activate,
            patch.object(dialog, "raise_") as raise_window,
        ):
            dialog.restore_after_capture()
            activate.assert_not_called()
            raise_window.assert_not_called()
        self.application.processEvents()

        self.assertTrue(dialog.isVisible())
        self.assertFalse(dialog.concealed_for_capture)
        self.assertEqual(attributes, [True])
        self.assertFalse(
            dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        )
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

    def test_refresh_timeout_restores_retry_and_ignores_stale_timer(self):
        dialog = DiagnosticsDialog(refresh_timeout_ms=60_000)
        refresh_requested = Mock()
        dialog.refresh_requested.connect(refresh_requested)
        dialog.show()
        self.application.processEvents()

        dialog.request_refresh()
        first_generation = dialog.refresh_generation
        dialog.conceal_for_capture()

        self.assertFalse(dialog.refresh_button.isEnabled())
        self.assertIn("Capturing", dialog.refresh_status.text())

        dialog._refresh_timed_out(first_generation)

        self.assertTrue(dialog.refresh_button.isEnabled())
        self.assertIn("timed out", dialog.refresh_status.text())
        self.assertTrue(dialog.isVisible())
        dialog.request_refresh()
        dialog.set_warning("Selected window is unavailable")
        dialog._refresh_timed_out(first_generation)

        self.assertTrue(dialog.refresh_button.isEnabled())
        self.assertIn("Refresh failed", dialog.refresh_status.text())
        self.assertEqual(refresh_requested.call_count, 2)
        dialog.close()
        dialog.deleteLater()

    def test_fresh_snapshot_clears_stale_image_and_warning(self):
        dialog = DiagnosticsDialog()
        dialog.set_snapshot(
            DiagnosticSnapshot(
                Image.new("RGB", (32, 10), "black"),
                text="Old",
                processed_image=Image.new("L", (64, 20), "white"),
            )
        )
        dialog.set_warning("Old warning")

        dialog.set_snapshot(DiagnosticSnapshot(None, text="Fresh"))

        self.assertIsNone(dialog.source_pixmap)
        self.assertTrue(dialog.preview.pixmap().isNull())
        self.assertIn("No capture image", dialog.preview.text())
        self.assertIsNone(dialog.processed_pixmap)
        self.assertTrue(dialog.processed_preview.pixmap().isNull())
        self.assertIn("No processed OCR image", dialog.processed_preview.text())
        self.assertFalse(dialog.enlarge_processed_button.isEnabled())
        self.assertIsNone(dialog._capture_dialog(processed=True))
        self.assertFalse(dialog.warning.isVisible())
        self.assertFalse(dialog.warning_action.isVisible())
        self.assertEqual(dialog.warning.toPlainText(), "")
        dialog.close()
        dialog.deleteLater()

    def test_warning_marks_previous_capture_and_enlargement_scrolls_both_axes(self):
        dialog = DiagnosticsDialog()
        dialog.set_snapshot(
            DiagnosticSnapshot(
                Image.new("RGB", (1200, 900), "black"),
                processed_image=Image.new("L", (1600, 1000), "white"),
                text="Previously captured text",
                captured_at=datetime(2026, 9, 22, 12, 34, tzinfo=timezone.utc),
            )
        )
        dialog.set_warning("Game window unavailable")

        self.assertIn("previous capture from", dialog.refresh_status.text())
        self.assertTrue(dialog.enlarge_button.isEnabled())
        zoom = dialog._capture_dialog()
        zoom.show()
        self.application.processEvents()
        scroll = zoom.findChild(QScrollArea)
        self.assertGreater(scroll.horizontalScrollBar().maximum(), 0)
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        zoom.close()
        processed_zoom = dialog._capture_dialog(processed=True)
        processed_zoom.show()
        self.application.processEvents()
        processed_scroll = processed_zoom.findChild(QScrollArea)
        self.assertGreater(processed_scroll.horizontalScrollBar().maximum(), 0)
        self.assertGreater(processed_scroll.verticalScrollBar().maximum(), 0)
        self.assertIn("Processed OCR", processed_zoom.windowTitle())
        processed_zoom.close()
        dialog.close()

    def test_closing_dialog_cancels_pending_refresh_timeout(self):
        dialog = DiagnosticsDialog(refresh_timeout_ms=1)
        dialog.request_refresh()
        generation = dialog.refresh_generation

        dialog.close()
        dialog.deleteLater()
        self.application.processEvents()

        self.assertFalse(dialog.refresh_timer.isActive())
        self.assertFalse(dialog.refresh_in_flight)
        self.assertGreater(dialog.refresh_generation, generation)

    def test_cancel_refresh_does_not_reopen_or_activate_concealed_controls(self):
        dialog = DiagnosticsDialog(refresh_timeout_ms=60_000)
        dialog.show()
        dialog.request_refresh()
        generation = dialog.refresh_generation
        dialog.conceal_for_capture()
        with (
            patch.object(dialog, "activateWindow") as activate,
            patch.object(dialog, "raise_") as raise_window,
        ):
            dialog.cancel_refresh()
            dialog._refresh_timed_out(generation)
            dialog.set_snapshot(DiagnosticSnapshot(None, text="Late capture"))
            activate.assert_not_called()
            raise_window.assert_not_called()
        self.assertFalse(dialog.isVisible())
        self.assertFalse(dialog.refresh_timer.isActive())
        self.assertFalse(dialog.refresh_in_flight)
        self.assertFalse(dialog.concealed_for_capture)
        self.assertTrue(dialog.refresh_button.isEnabled())
        dialog.close()
        dialog.deleteLater()

    def test_late_snapshot_does_not_reopen_closed_concealed_dialog(self):
        dialog = DiagnosticsDialog()
        dialog.show()
        self.application.processEvents()
        dialog.conceal_for_capture()
        dialog.close()

        dialog.set_snapshot(DiagnosticSnapshot(None, text="Late capture"))

        self.assertFalse(dialog.isVisible())
        self.assertFalse(dialog.concealed_for_capture)
        dialog.deleteLater()

    def test_every_dialog_exit_cancels_refresh_and_emits_closed_once(self):
        for action in ("close", "reject", "accept", "escape"):
            with self.subTest(action=action):
                dialog = DiagnosticsDialog(refresh_timeout_ms=60_000)
                closed = Mock()
                dialog.closed.connect(closed)
                dialog.show()
                dialog.request_refresh()
                generation = dialog.refresh_generation
                if action == "escape":
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                else:
                    dialog.conceal_for_capture()
                    getattr(dialog, action)()
                self.assertFalse(dialog.refresh_timer.isActive())
                self.assertFalse(dialog.refresh_in_flight)
                self.assertGreater(dialog.refresh_generation, generation)
                closed.assert_called_once_with()
                dialog.set_snapshot(DiagnosticSnapshot(None, text="Late"))
                self.assertFalse(dialog.isVisible())
                dialog.deleteLater()

    def test_warning_clear_and_retry_do_not_leave_stale_recovery_instructions(self):
        dialog = DiagnosticsDialog()
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.set_warning(
            "Permission missing", remediation=("settings", "Open Settings")
        )
        self.assertNotIn("Refresh failed", dialog.refresh_status.text())
        dialog.set_permission_warnings([])
        self.assertTrue(dialog.warning.isHidden())
        self.assertTrue(dialog.copy_error_button.isHidden())
        self.assertNotIn("warning", dialog.refresh_status.text())
        dialog.set_snapshot(DiagnosticSnapshot(None, text="Previously recognized"))
        dialog.set_warning("Window missing", remediation=("settings", "Open Settings"))
        dialog.request_refresh()
        self.assertTrue(dialog.refresh_in_flight)
        self.assertTrue(dialog.warning.isHidden())
        self.assertTrue(dialog.warning_action.isHidden())
        self.assertIn("Capturing", dialog.refresh_status.text())
        self.assertIn("Previous capture", dialog.refresh_status.text())
        self.assertEqual(dialog.text.text(), "Previously recognized")
        dialog.set_warning("Capture failed")
        self.assertFalse(dialog.refresh_in_flight)
        dialog.set_permission_warnings([])
        self.assertNotIn("warning", dialog.refresh_status.text())
        self.assertIn("Showing capture from", dialog.refresh_status.text())

    def test_long_warning_remains_readable_and_copyable_with_larger_text(self):
        dialog = DiagnosticsDialog()
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        font = dialog.font()
        font.setPointSize(18)
        dialog.setFont(font)
        dialog.resize(620, 460)
        warning = "<Missing window> Restore the selected game, then try again. " * 30
        dialog.set_warning(warning, remediation=("settings", "Open Settings"))
        dialog.show()
        self.application.processEvents()
        self.assertEqual(dialog.warning.toPlainText(), warning)
        self.assertTrue(dialog.warning.isReadOnly())
        self.assertGreater(dialog.warning.verticalScrollBar().maximum(), 0)
        dialog.warning.setFocus()
        QTest.keyClick(dialog.warning, Qt.Key.Key_PageDown)
        self.assertGreater(dialog.warning.verticalScrollBar().value(), 0)
        QTest.mouseClick(dialog.copy_error_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.application.clipboard().text(), warning)
        self.assertTrue(
            dialog.rect().contains(dialog.refresh_button.geometry().center())
        )
        self.assertTrue(
            dialog.rect().contains(dialog.warning_action.geometry().center())
        )

    def test_result_is_visible_before_optional_ocr_images_and_details_scroll(self):
        dialog = DiagnosticsDialog()
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.set_snapshot(
            DiagnosticSnapshot(
                Image.new("RGB", (320, 90), "black"),
                character="Marcus",
                text="The captured line.",
                speaker_image=Image.new("L", (1000, 160), "white"),
                processed_image=Image.new("L", (1000, 200), "gray"),
            )
        )
        dialog.show()
        self.application.processEvents()
        self.assertFalse(dialog.technical_panel.isVisible())
        viewport = dialog.body_scroll.viewport()
        self.assertTrue(
            viewport.rect().contains(
                dialog.text.mapTo(viewport, dialog.text.rect().center())
            )
        )
        dialog.technical_toggle.setFocus()
        QTest.keyClick(dialog.technical_toggle, Qt.Key.Key_Space)
        self.application.processEvents()
        self.assertTrue(dialog.processed_preview.isVisible())
        for preview in (dialog.speaker_processed_preview, dialog.processed_preview):
            self.assertLessEqual(preview.pixmap().width(), preview.width())
            self.assertLessEqual(preview.pixmap().height(), preview.height())
        self.assertTrue(dialog.enlarge_speaker_button.isEnabled())
        self.assertGreater(dialog.body_scroll.verticalScrollBar().maximum(), 0)
        dialog.body_scroll.ensureWidgetVisible(dialog.game_focus)
        self.assertGreater(dialog.body_scroll.verticalScrollBar().value(), 0)
        self.assertTrue(
            viewport.rect().contains(
                dialog.game_focus.mapTo(viewport, dialog.game_focus.rect().center())
            )
        )
        dialog.text.setFocus()
        QTest.keySequence(dialog.text, QKeySequence.StandardKey.SelectAll)
        QTest.keySequence(dialog.text, QKeySequence.StandardKey.Copy)
        self.assertEqual(self.application.clipboard().text(), "The captured line.")

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

    def test_window_capture_errors_offer_settings_recovery(self):
        missing = "Game window 'Reverse: 1999' is not available"
        unselected = "Select a game window in Settings"

        self.assertIn(
            "Start or restore the game",
            diagnostic_error_guidance(RuntimeError(missing), platform="win32"),
        )
        self.assertEqual(
            diagnostic_error_guidance(RuntimeError(unselected), platform="win32"),
            unselected,
        )
        for message in (missing, unselected):
            with self.subTest(message=message):
                self.assertEqual(
                    diagnostic_remediation(message, platform="win32"),
                    ("settings", "Open Settings"),
                )

    def test_warning_exposes_only_its_contextual_typed_remediation(self):
        dialog = DiagnosticsDialog()
        remediations = []
        dialog.remediation_requested.connect(remediations.append)

        dialog.set_warning(
            "The selected game window is unavailable.",
            remediation=("settings", "Open Settings"),
        )

        self.assertFalse(dialog.warning_action.isHidden())
        self.assertEqual(dialog.warning_action.text(), "Open Settings")
        dialog.warning_action.click()
        self.assertEqual(remediations, ["settings"])

        dialog.set_warning("Unclassified warning")
        self.assertTrue(dialog.warning_action.isHidden())
        dialog.close()
        dialog.deleteLater()

    def test_diagnostic_remediation_is_typed_by_failure_context(self):
        self.assertEqual(
            diagnostic_remediation(
                "Screen Recording permission is missing",
                platform="darwin",
            ),
            ("macos-permissions", "Open macOS permissions"),
        )
        self.assertEqual(
            diagnostic_remediation(
                "The selected game window is unavailable",
                platform="win32",
            ),
            ("settings", "Open Settings"),
        )
        self.assertIsNone(
            diagnostic_remediation("OCR confidence is low", platform="win32")
        )

    def test_voice_label_reports_character_and_narrator_routes(self):
        voice = CharacterVoice("Marcus", "reverse1999-marcus")
        router = Mock()
        router.registry.resolve.side_effect = lambda character: (
            voice if character == "Marcus" else None
        )
        router.narrator_speaker = "Claribel Dervla"
        router.narrator_voice = None
        router.narrator_reference = None

        self.assertEqual(
            resolve_voice_label(router, "Marcus"),
            "Marcus; voice ID: reverse1999-marcus",
        )
        self.assertEqual(
            resolve_voice_label(router, "Narrator"),
            "Claribel Dervla",
        )
        self.assertEqual(
            resolve_voice_label(router, "???"),
            "Claribel Dervla",
        )
        router.registry.resolve.side_effect = lambda _character: voice
        self.assertEqual(
            resolve_voice_label(router, "Narrator"),
            "Marcus; voice ID: reverse1999-marcus",
        )
        projected = CharacterVoice(
            "Narrator", "game-voice-id", source_character="Centurion"
        )
        router.registry.resolve.side_effect = lambda _character: projected
        self.assertEqual(
            resolve_voice_label(router, "Narrator"),
            "Centurion; voice ID: game-voice-id",
        )


if __name__ == "__main__":
    unittest.main()

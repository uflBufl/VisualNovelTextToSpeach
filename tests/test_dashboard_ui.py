import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from vntts.dashboard_ui import CompactController, ControlDashboard  # noqa: E402
from vntts.diagnostics import DiagnosticSnapshot  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402


class ControlDashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_live_state_and_diagnostics_are_visible(self):
        dashboard = ControlDashboard(AppSettings())
        snapshot = DiagnosticSnapshot(
            None,
            character="Selone",
            confidence=95,
            voice="Selone (reverse-1999-selone)",
            capture_ms=20,
            ocr_ms=110,
            last_first_audio_ms=240,
            speech_queue_depth=1,
            audio_source="MOSS fresh generation (voice Selone)",
        )

        dashboard.set_ready(True)
        dashboard.set_live(True)
        dashboard.set_diagnostic(snapshot)

        self.assertEqual(dashboard.mode.text(), "Live reading")
        self.assertEqual(dashboard.speaker.text(), "Selone")
        self.assertEqual(
            dashboard.audio_source.text(),
            "MOSS fresh generation (voice Selone)",
        )
        self.assertIn("first audio 240 ms", dashboard.latency.text())
        self.assertIn("queue 1", dashboard.latency.text())
        dashboard.deleteLater()

    def test_configuration_shows_policy_and_missing_generated_audio(self):
        dashboard = ControlDashboard(
            AppSettings(
                audio_source_policy="live-tts-only",
                generated_audio_manifest="missing/generated.json",
            )
        )

        self.assertIn("Audio policy: Live TTS only", dashboard.configuration.text())
        self.assertIn(
            "Generated audio: missing; open Settings",
            dashboard.configuration.text(),
        )
        dashboard.deleteLater()

    def test_close_quits_by_default_instead_of_hiding_silently(self):
        dashboard = ControlDashboard(AppSettings(keep_running_on_close=False))
        quit_requests = []
        dashboard.quit_requested.connect(lambda: quit_requests.append(True))
        dashboard.show()

        dashboard.close()

        self.assertEqual(quit_requests, [True])
        self.assertFalse(dashboard.isVisible())
        dashboard.deleteLater()

    def test_background_mode_is_explicit(self):
        dashboard = ControlDashboard(AppSettings(keep_running_on_close=True))
        background_events = []
        dashboard.hidden_to_background.connect(lambda: background_events.append(True))
        dashboard.show()

        dashboard.close()

        self.assertEqual(background_events, [True])
        self.assertFalse(dashboard.isVisible())
        dashboard.keep_running_on_close = False
        dashboard._quitting = True
        dashboard.close()
        dashboard.deleteLater()

    def test_compact_controller_exposes_play_controls_and_state(self):
        controller = CompactController(platform="darwin")
        requests = []
        controller.live_requested.connect(lambda: requests.append("live"))
        controller.set_ready(True)
        controller.set_dialogue("Selone", "I have returned.")
        controller.set_live(True)
        controller.set_paused(True)

        controller.live_button.click()

        self.assertEqual(requests, ["live"])
        self.assertEqual(controller.speaker.text(), "Selone")
        self.assertEqual(controller.mode.text(), "Paused")
        self.assertEqual(controller.live_button.text(), "Stop live")
        self.assertTrue(controller.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(
            controller.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        )
        controller.close()
        controller.deleteLater()

    def test_compact_status_and_warning_remain_visible_during_live_mode(self):
        controller = CompactController(platform="win32")
        controller.set_live(True)

        controller.set_status(
            "Auto advance paused because source-audio completion is unavailable"
        )

        self.assertEqual(controller.mode.text(), "Live")
        self.assertIn("source-audio completion", controller.status.text())

        controller.set_warning("Voice needed: Hotelier")

        self.assertEqual(controller.mode.text(), "Live")
        self.assertEqual(controller.status.text(), "Voice needed: Hotelier")
        self.assertIn("#a21818", controller.status.styleSheet())

        controller.set_paused(True)

        self.assertEqual(controller.mode.text(), "Paused")
        self.assertEqual(controller.status.text(), "Voice needed: Hotelier")
        controller.deleteLater()

    def test_other_platforms_do_not_enable_macos_compact_window_behavior(self):
        controller = CompactController(platform="win32")

        self.assertFalse(
            controller.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        )
        controller.deleteLater()

    def test_compact_text_gets_width_before_fixed_size_buttons(self):
        controller = CompactController(platform="win32")
        controller.resize(650, controller.sizeHint().height())
        controller.show()
        controller.set_status(
            "Auto advance key sent; waiting for the next dialogue generation"
        )
        controller.set_dialogue("Adar Llwch Gwin Fledgling", "A line")
        self.application.processEvents()

        buttons = (
            controller.read_button,
            controller.live_button,
            controller.pause_button,
            controller.skip_button,
            controller.stop_button,
            controller.full_button,
        )
        self.assertTrue(controller.status.wordWrap())
        self.assertTrue(controller.speaker.wordWrap())
        self.assertGreater(
            controller.status.width(),
            max(button.width() for button in buttons),
        )
        self.assertGreater(
            controller.speaker.width(),
            max(button.width() for button in buttons),
        )
        self.assertGreaterEqual(
            controller.status.height(),
            controller.status.heightForWidth(controller.status.width()),
        )
        self.assertGreaterEqual(
            controller.speaker.height(),
            controller.speaker.heightForWidth(controller.speaker.width()),
        )
        for button in buttons:
            self.assertEqual(
                button.sizePolicy().horizontalPolicy(),
                QSizePolicy.Policy.Fixed,
            )

        controller.close()
        controller.deleteLater()

    def test_short_window_scrolls_instead_of_clipping_controls(self):
        dashboard = ControlDashboard(
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999 with a long window title",
            )
        )
        dashboard.setMinimumHeight(200)
        dashboard.resize(720, 200)
        dashboard.show()
        self.application.processEvents()

        self.assertGreater(dashboard.content_scroll.verticalScrollBar().maximum(), 0)
        self.assertTrue(dashboard.configuration.text().endswith("OCR: eng"))
        self.assertGreaterEqual(
            dashboard.configuration.minimumHeight(),
            dashboard.configuration.fontMetrics().lineSpacing() * 3,
        )

        dashboard._quitting = True
        dashboard.close()
        dashboard.deleteLater()


if __name__ == "__main__":
    unittest.main()

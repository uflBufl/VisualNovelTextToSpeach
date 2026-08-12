import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.dashboard_ui import ControlDashboard  # noqa: E402
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
        )

        dashboard.set_ready(True)
        dashboard.set_live(True)
        dashboard.set_diagnostic(snapshot)

        self.assertEqual(dashboard.mode.text(), "Live reading")
        self.assertEqual(dashboard.speaker.text(), "Selone")
        self.assertIn("first audio 240 ms", dashboard.latency.text())
        self.assertIn("queue 1", dashboard.latency.text())
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


if __name__ == "__main__":
    unittest.main()

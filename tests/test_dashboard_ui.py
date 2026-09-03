import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QGroupBox, QSizePolicy  # noqa: E402

from vntts.controller import LiveSequenceStatus  # noqa: E402
from vntts.dashboard_ui import (  # noqa: E402
    CompactController,
    ControlDashboard,
    RuntimeControlState,
)
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
        self.assertTrue(dashboard.live_button.isDefault())
        self.assertIn("Live reading is active", dashboard.action_reason.text())
        dashboard.deleteLater()

    def test_disabled_controls_explain_recovery_and_keep_setup_available(self):
        dashboard = ControlDashboard(AppSettings())

        dashboard.set_status("Speech model failed to load")

        self.assertFalse(dashboard.live_button.isEnabled())
        self.assertIn("Speech model failed to load", dashboard.action_reason.text())
        self.assertIn("Check readiness", dashboard.action_reason.text())
        self.assertEqual(
            dashboard.live_button.toolTip(), dashboard.action_reason.text()
        )
        self.assertFalse(dashboard.narrator_voice_button.isEnabled())
        self.assertTrue(
            all(
                button.isEnabled()
                for button in dashboard.setup_buttons
                if button is not dashboard.narrator_voice_button
            )
        )
        self.assertEqual(
            {group.title() for group in dashboard.findChildren(QGroupBox)},
            {
                "Reading",
                "Offline audio",
                "Playback",
                "Sequence-first story cursor",
                "Setup and support",
            },
        )
        dashboard.deleteLater()

    def test_offline_audio_is_a_visible_player_action(self):
        dashboard = ControlDashboard(AppSettings())
        requests = []
        dashboard.pregeneration_requested.connect(
            lambda: requests.append("pregeneration")
        )
        dashboard.show()
        self.application.processEvents()

        self.assertTrue(dashboard.prepare_audio_button.isVisibleTo(dashboard))
        self.assertIn(
            "guided defaults",
            dashboard.prepare_audio_button.accessibleDescription(),
        )

        dashboard.prepare_audio_button.click()

        self.assertEqual(requests, ["pregeneration"])
        dashboard.close()
        dashboard.deleteLater()

    def test_primary_reading_surface_hides_technical_details_by_default(self):
        dashboard = ControlDashboard(AppSettings())
        dashboard.show()
        self.application.processEvents()

        self.assertTrue(dashboard.dialogue.isVisibleTo(dashboard))
        self.assertTrue(dashboard.speaker.isVisibleTo(dashboard))
        self.assertEqual(dashboard.speaker.accessibleName(), "Current dialogue speaker")
        self.assertTrue(dashboard.live_button.isVisibleTo(dashboard))
        self.assertFalse(dashboard.details_content.isVisibleTo(dashboard))
        self.assertEqual(dashboard.details_toggle.text(), "Show technical details")

        dashboard.details_toggle.click()

        self.assertTrue(dashboard.details_content.isVisibleTo(dashboard))
        self.assertTrue(dashboard.configuration.isVisibleTo(dashboard))
        self.assertEqual(dashboard.details_toggle.text(), "Hide technical details")
        dashboard.close()
        dashboard.deleteLater()

    def test_setup_surface_keeps_one_primary_entry_until_expanded(self):
        dashboard = ControlDashboard(AppSettings())
        requests = []
        dashboard.readiness_requested.connect(lambda: requests.append("readiness"))
        dashboard.show()
        self.application.processEvents()

        self.assertTrue(dashboard.setup_primary_button.isVisibleTo(dashboard))
        self.assertEqual(dashboard.setup_primary_button.text(), "Check readiness")
        self.assertTrue(dashboard.setup_more_button.isVisibleTo(dashboard))
        self.assertFalse(dashboard.setup_secondary_content.isVisibleTo(dashboard))

        dashboard.setup_primary_button.click()
        dashboard.setup_more_button.click()

        self.assertEqual(requests, ["readiness"])
        self.assertTrue(dashboard.setup_secondary_content.isVisibleTo(dashboard))
        self.assertEqual(dashboard.setup_more_button.text(), "Fewer setup options")
        dashboard.close()
        dashboard.deleteLater()

    def test_sequence_diagnostics_are_available_inside_technical_details(self):
        dashboard = ControlDashboard(
            AppSettings(
                live_sequence_mode="audio-manual",
                live_sequence_plan="live-sequence.json",
                story_index="story-index.jsonl",
            )
        )
        dashboard.show()
        self.application.processEvents()

        self.assertFalse(dashboard.sequence_group.isVisibleTo(dashboard))

        dashboard.details_toggle.click()

        self.assertTrue(dashboard.sequence_group.isVisibleTo(dashboard))
        dashboard.close()
        dashboard.deleteLater()

    def test_primary_live_action_is_keyboard_operable(self):
        dashboard = ControlDashboard(AppSettings())
        requests = []
        dashboard.live_requested.connect(lambda: requests.append("live"))
        dashboard.set_ready(True)
        dashboard.live_button.setFocus()

        QTest.keyClick(dashboard.live_button, Qt.Key.Key_Return)

        self.assertEqual(requests, ["live"])
        self.assertIn("Primary action", dashboard.live_button.accessibleDescription())
        self.assertTrue(dashboard.action_reason.accessibleName())
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

    def test_story_resync_is_visible_for_sequence_audio_modes(self):
        dashboard = ControlDashboard(AppSettings())

        self.assertTrue(dashboard.sequence_group.isHidden())

        dashboard.set_configuration(
            AppSettings(
                live_sequence_mode="audio-manual",
                live_sequence_plan="live-sequence.json",
                story_index="story-index.jsonl",
            )
        )
        dashboard.set_ready(True)

        self.assertFalse(dashboard.sequence_group.isHidden())
        self.assertTrue(dashboard.sequence_resync_button.isEnabled())
        self.assertIn(
            "anchor or recover",
            dashboard.sequence_resync_button.accessibleDescription(),
        )

        dashboard.set_configuration(
            AppSettings(
                live_sequence_mode="audio-auto",
                live_sequence_plan="live-sequence.json",
                story_index="story-index.jsonl",
            )
        )

        self.assertFalse(dashboard.sequence_group.isHidden())
        self.assertIn(
            "Sequence: Canonical audio; guarded automatic advance",
            dashboard.configuration.text(),
        )
        dashboard.deleteLater()

    def test_sequence_card_keeps_block_reason_and_recovery_visible(self):
        dashboard = ControlDashboard(AppSettings(live_sequence_mode="audio-manual"))
        dashboard.set_ready(True)

        dashboard.set_sequence_status(
            LiveSequenceStatus(
                "audio-manual",
                "desynchronized",
                chapter="314601",
                sequence=41,
                event_id="event-41",
                line_id="reverse1999:314601:41",
                speaker="Rhiannon",
                text="Canonical text.",
                reason="unexpected-transition",
                next_event_count=2,
                recovery_required=True,
                guidance="Set the visible story position to resume.",
                expected_audio_route="Generated audio (manifest declaration)",
                actual_audio_route="generated-audio",
                ocr_activity="Full OCR idle in locked routing; 1 frame recognized",
                expected_candidate_count=2,
            )
        )

        self.assertIn("desynchronized", dashboard.sequence_state.text())
        self.assertIn("314601", dashboard.sequence_position.text())
        self.assertIn("event-41", dashboard.sequence_identity.text())
        self.assertIn("Rhiannon: Canonical text.", dashboard.sequence_canonical.text())
        self.assertEqual(
            dashboard.sequence_guidance.text(),
            "Set the visible story position to resume.",
        )
        self.assertIn("Generated audio", dashboard.sequence_expected_audio.text())
        self.assertEqual(dashboard.sequence_actual_audio.text(), "generated-audio")
        self.assertIn("OCR idle", dashboard.sequence_ocr.text())
        self.assertTrue(dashboard.sequence_expected_button.isEnabled())
        self.assertIn("2 expected", dashboard.sequence_expected_button.text())
        self.assertIn("font-weight", dashboard.sequence_resync_button.styleSheet())
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
        controller.repeat_requested.connect(lambda: requests.append("repeat"))
        controller.set_ready(True)
        controller.set_dialogue("Selone", "I have returned.")
        controller.set_live(True)
        controller.set_paused(True)

        controller.live_button.click()
        controller.set_runtime_controls(
            RuntimeControlState(ready=True, replayable=True)
        )
        controller.repeat_button.click()

        self.assertEqual(requests, ["live", "repeat"])
        self.assertEqual(controller.speaker.text(), "Selone")
        self.assertEqual(controller.mode.text(), "Paused")
        self.assertEqual(controller.live_button.text(), "Stop live")
        self.assertEqual(controller.stop_button.text(), "Emergency stop")
        self.assertEqual(controller.full_button.text(), "Full controls")
        self.assertTrue(controller.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(
            controller.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        )
        controller.close()
        controller.deleteLater()

    def test_compact_expected_sequence_action_tracks_mode_candidates_and_readiness(
        self,
    ):
        controller = CompactController(platform="darwin")
        requests = []
        controller.sequence_expected_requested.connect(
            lambda: requests.append("expected")
        )

        controller.set_sequence_status(
            LiveSequenceStatus("off", "off", expected_candidate_count=1)
        )
        self.assertTrue(controller.sequence_expected_button.isHidden())

        controller.set_sequence_status(
            LiveSequenceStatus(
                "audio-manual",
                "locked",
                expected_candidate_count=1,
            )
        )
        self.assertFalse(controller.sequence_expected_button.isHidden())
        self.assertFalse(controller.sequence_expected_button.isEnabled())

        controller.set_ready(True)
        controller.sequence_expected_button.click()

        self.assertTrue(controller.sequence_expected_button.isEnabled())
        self.assertEqual(requests, ["expected"])
        controller.set_ready(False)
        self.assertFalse(controller.sequence_expected_button.isEnabled())

    def test_compact_recovery_uses_current_dashboard_action_name(self):
        controller = CompactController(platform="darwin")

        controller.set_status("Speech model failed to load")

        self.assertIn("Check readiness", controller.action_reason.text())
        self.assertIn("Full controls", controller.action_reason.text())
        controller.close()
        controller.deleteLater()
        controller.deleteLater()

    def test_runtime_capability_state_matches_dashboard_and_compact_controls(self):
        dashboard = ControlDashboard(AppSettings())
        compact = CompactController(platform="win32")
        cases = (
            (
                "idle",
                RuntimeControlState(ready=True),
                (True, True, False, False, False, False),
            ),
            (
                "live",
                RuntimeControlState(ready=True, live=True),
                (True, True, True, False, False, True),
            ),
            (
                "speaking",
                RuntimeControlState(ready=True, speaking=True),
                (True, True, True, True, False, True),
            ),
            (
                "paused",
                RuntimeControlState(ready=True, paused=True),
                (True, True, True, False, False, True),
            ),
            (
                "queued",
                RuntimeControlState(ready=True, queued=True),
                (True, True, True, False, False, True),
            ),
            (
                "replayable",
                RuntimeControlState(ready=True, replayable=True),
                (True, True, False, False, True, False),
            ),
        )
        for name, state, expected in cases:
            with self.subTest(state=name):
                dashboard.set_runtime_controls(state)
                compact.set_runtime_controls(state)
                dashboard_state = tuple(
                    button.isEnabled()
                    for button in (
                        dashboard.read_button,
                        dashboard.live_button,
                        dashboard.pause_button,
                        dashboard.skip_button,
                        dashboard.repeat_button,
                        dashboard.stop_button,
                    )
                )
                compact_state = tuple(
                    button.isEnabled()
                    for button in (
                        compact.read_button,
                        compact.live_button,
                        compact.pause_button,
                        compact.skip_button,
                        compact.repeat_button,
                        compact.stop_button,
                    )
                )
                self.assertEqual(dashboard_state, expected)
                self.assertEqual(compact_state, expected)
                self.assertEqual(
                    dashboard.repeat_button.toolTip(),
                    compact.repeat_button.toolTip(),
                )
        dashboard.deleteLater()
        compact.deleteLater()

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

    def test_compact_disabled_controls_show_recovery_beside_controls(self):
        controller = CompactController(platform="win32")

        controller.set_status("Speech model failed to load")

        self.assertFalse(controller.live_button.isEnabled())
        self.assertTrue(controller.full_button.isEnabled())
        self.assertIn("Speech model failed to load", controller.action_reason.text())
        self.assertIn("Full controls", controller.action_reason.text())
        self.assertEqual(
            controller.live_button.toolTip(), controller.action_reason.text()
        )
        self.assertTrue(controller.live_button.isDefault())
        self.assertTrue(controller.action_reason.accessibleName())
        controller.deleteLater()

    def test_dashboard_and_compact_controls_fit_scaled_fonts(self):
        base_font = QApplication.font()
        base_size = base_font.pointSizeF()
        if base_size <= 0:
            base_size = 12.0
        for scale in (1.0, 1.5, 2.0):
            with self.subTest(scale=scale):
                font = QFont(base_font)
                font.setPointSizeF(base_size * scale)
                dashboard = ControlDashboard(AppSettings())
                dashboard.setFont(font)
                dashboard.resize(620, 340)
                dashboard.show()
                compact = CompactController(platform="win32")
                compact.setFont(font)
                compact.show()
                compact._fit_content()
                self.application.processEvents()

                self.assertTrue(dashboard.action_reason.isVisibleTo(dashboard))
                self.assertGreaterEqual(
                    dashboard.content_scroll.verticalScrollBar().maximum(), 0
                )
                self.assertTrue(compact.action_reason.isVisibleTo(compact))
                self.assertLessEqual(
                    compact.full_button.geometry().right(),
                    compact.contentsRect().right(),
                )

                dashboard._quitting = True
                dashboard.close()
                dashboard.deleteLater()
                compact.close()
                compact.deleteLater()

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
            controller.repeat_button,
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

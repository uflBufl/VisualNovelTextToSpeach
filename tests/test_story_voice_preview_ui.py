import os
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
from PySide6.QtCore import QEvent, QObject, QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.native_clip_fixtures import FakeClipController  # noqa: E402
from tests.pregeneration_fixtures import (  # noqa: E402
    ambiguous_fixture,
    clean_wav_bytes,
)
from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from tests.story_fixtures import write_story_index  # noqa: E402
from vntts.authoring.pcm_playback import PersistentPcmPlayer  # noqa: E402
from vntts.pregeneration_audition import (  # noqa: E402
    COMPARISON_PREVIEW_TEXT,
    VoiceAuditionError,
)
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import VoiceCandidate, VoiceDecisionStore  # noqa: E402
from vntts.qt_audio import QtPcmPlayer  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.story_voice_preview_ui import StoryVoicePreview  # noqa: E402
from vntts.voice_library import VoiceLibrary  # noqa: E402


class StoryVoicePreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        plan, group, _manifest = ambiguous_fixture(self.root / "voices")
        narrator = VoiceCandidate("preset:marius", "Marius", "marius", ())
        self.group = replace(group, route="voice", narrator_candidate=narrator)
        self.other = replace(
            self.group,
            group_id="other-group",
            character="Other role",
            routing_role="Other role",
            route="needs-audition",
        )
        self.plan = replace(
            plan,
            synthesis_backend="pocket-tts",
            groups=(self.group, self.other),
        )
        payload = clean_wav_bytes()
        path = self.root / "preview.wav"
        path.write_bytes(payload)
        self.preview = SimpleNamespace(
            path=path, audio_sha256=sha256(payload).hexdigest()
        )
        self.service = Mock(supports_prefetch=False)
        self.service.generate.return_value = self.preview
        self.player = Mock()
        self.pool = ManualThreadPool()
        library = VoiceLibrary(self.root / "library")
        library.select("Narrator", route="voice", source_id="preset:marius")
        self.decisions = VoiceDecisionStore(
            self.root / "decisions.json", voice_library=library
        )
        content = inspect_story_index(write_story_index(self.root / "content"))
        self.dialog = OfflineAudioPreparationDialog(
            AppSettings(),
            discovery=lambda: ContentDiscovery((content,)),
            job_store=PregenerationJobStore(self.root / "jobs"),
            voice_decisions=self.decisions,
            audition_service=self.service,
            preview_player=self.player,
            game_narrator_chooser=Mock(),
            thread_pool=self.pool,
            voice_library=library,
        )
        self.addCleanup(self.dialog.deleteLater)
        self.addCleanup(self.dialog.summary_voice_preview.close)
        self.dialog._voice_plan = self.plan
        self.dialog._show_voice_confirmation(self.plan)
        self.pool.tasks.clear()

    def select(self, group):
        for row in range(self.dialog.voice_routes.count()):
            item = self.dialog.voice_routes.item(row)
            if item.data(int(Qt.ItemDataRole.UserRole) + 1) == group.group_id:
                self.dialog.voice_routes.setCurrentItem(item)
                return
        self.fail("Voice role is missing from the summary")

    def run_tasks(self):
        while self.pool.tasks:
            self.pool.run_next()
            self.application.processEvents()

    def test_unavailable_runtime_fails_before_generation_and_retry_can_start(self):
        preview = self.dialog.summary_voice_preview
        preview.player = None
        self.select(self.group)
        message = "Rust audio worker is missing. Run cargo build --release --locked"
        with patch(
            "vntts.rust_audio.check_audio_runtime", side_effect=RuntimeError(message)
        ):
            self.dialog.play_selected_voice.click()
        self.assertFalse(preview.active)
        self.assertEqual(self.pool.tasks, [])
        self.service.generate.assert_not_called()
        self.assertIn(message, self.dialog.voice_confirmation_status.text())
        self.assertEqual(self.dialog.play_selected_voice.text(), "Selected voice")
        self.assertEqual(
            self.dialog.play_selected_voice.accessibleName(), "Play selected voice"
        )
        self.assertEqual(
            self.dialog.play_selected_voice.toolTip(),
            "Play the selected voice preview.",
        )

        controller = FakeClipController()
        pcm = PersistentPcmPlayer(controller)
        preview.player._player_factory = lambda: pcm
        self.dialog.play_selected_voice.click()
        self.run_tasks()
        self.assertTrue(preview.active)
        self.assertEqual(self.dialog.play_selected_voice.text(), "Selected voice")
        self.assertEqual(
            self.dialog.play_selected_voice.accessibleName(),
            "Stop selected voice preview",
        )
        self.assertEqual(
            self.dialog.play_selected_voice.toolTip(),
            "Stop the selected voice preview.",
        )
        self.assertIn("Starting", self.dialog.voice_confirmation_status.text())
        controller.pump(1)
        preview.player._poll()
        self.assertIn("Playing", self.dialog.voice_confirmation_status.text())

    def test_worker_crash_during_start_clears_summary_busy_state(self):
        preview = self.dialog.summary_voice_preview
        preview.player = QtPcmPlayer(
            preview,
            player_factory=Mock(
                side_effect=RuntimeError("Rust audio worker protocol mismatch")
            ),
        )
        preview._connect_player()
        self.select(self.group)
        self.dialog.play_selected_voice.click()
        self.run_tasks()
        self.assertFalse(preview.active)
        self.assertIn("protocol mismatch", self.dialog.voice_confirmation_status.text())
        self.assertNotIn("Preparing", self.dialog.voice_confirmation_status.text())
        self.assertEqual(self.dialog.play_selected_voice.text(), "Selected voice")
        self.assertEqual(
            self.dialog.play_selected_voice.accessibleName(), "Play selected voice"
        )

    def test_selected_voice_plays_without_leaving_summary_or_saving_choice(self):
        self.select(self.group)
        bindings = self.dialog.voice_library.bindings()

        self.dialog.play_selected_voice.click()
        self.run_tasks()

        self.assertFalse(self.dialog.voice_confirmation.isHidden())
        self.assertFalse(hasattr(self.dialog, "voice_panel"))
        self.service.generate.assert_called_once()
        self.assertEqual(
            self.service.generate.call_args.args,
            (self.plan, self.group, self.group.source_id),
        )
        self.assertEqual(
            self.service.generate.call_args.kwargs["text"], COMPARISON_PREVIEW_TEXT
        )
        self.player.play_bytes.assert_called_once()
        self.assertEqual(self.dialog.voice_library.bindings(), bindings)
        self.assertIsNone(
            self.decisions.choice_for(
                self.group.group_id, self.group.decision_context_sha256
            )
        )

    def test_automatic_voices_prepare_before_manual_choices_and_narrator(self):
        fresh = replace(
            self.group, character="Fresh", resolution="known-character-voice"
        )
        manual = replace(
            self.group,
            group_id="manual",
            character="Manual",
            resolution="saved-player-decision",
        )
        saved_auto = replace(
            self.group,
            group_id="saved-auto",
            character="Automatic",
            resolution="saved-voice-assignment",
        )
        self.dialog.voice_library.select(
            saved_auto.character,
            route="voice",
            source_id=saved_auto.source_id,
            method="automatic",
        )
        self.plan = replace(self.plan, groups=(manual, fresh, saved_auto, self.other))
        self.dialog._voice_plan = self.plan
        session = Mock(supports_prefetch=True)
        self.service.supports_prefetch = True
        self.service.new_session.return_value = session

        self.dialog._prepare_summary_previews(self.plan)
        self.run_tasks()

        calls = session.generate.call_args_list
        self.assertEqual(
            [call.args[1].character for call in calls[:3]],
            ["Automatic", "Fresh", "Manual"],
        )
        self.assertTrue(all(call.args[2] == "preset:marius" for call in calls[3:]))
        self.assertTrue(all(call.kwargs["automatic"] for call in calls))
        self.assertTrue(
            all(call.kwargs["text"] == COMPARISON_PREVIEW_TEXT for call in calls)
        )
        self.player.play_bytes.assert_not_called()

    def test_background_ready_preview_plays_without_another_worker(self):
        preparing = Mock(supports_prefetch=True)
        preparing.peek.return_value = self.preview
        foreground = Mock(supports_prefetch=True)
        foreground.peek.return_value = None
        self.service.supports_prefetch = True
        self.service.peek.return_value = None
        self.service.new_session.side_effect = [preparing, foreground]
        self.dialog._prepare_summary_previews(self.plan)
        self.run_tasks()
        self.select(self.group)

        self.dialog.play_selected_voice.click()

        self.assertEqual(self.pool.tasks, [])
        foreground.generate.assert_not_called()
        self.player.play_bytes.assert_called_once()

    def test_leaving_summary_cancels_background_work_without_autoplay(self):
        session = Mock(supports_prefetch=True)
        self.service.supports_prefetch = True
        self.service.new_session.return_value = session
        self.dialog._prepare_summary_previews(self.plan)
        cancellation = self.dialog.summary_voice_preview.preparation_cancellation

        self.application.sendEvent(
            self.dialog.voice_confirmation, QEvent(QEvent.Type.Hide)
        )
        self.run_tasks()

        self.assertTrue(cancellation.is_set())
        session.close.assert_called_once()
        session.generate.assert_not_called()
        self.player.play_bytes.assert_not_called()

    def test_background_failure_does_not_skip_later_roles_or_block_navigation(self):
        session = Mock(supports_prefetch=True)
        session.generate.side_effect = [
            VoiceAuditionError("invalid reference"),
            self.preview,
            self.preview,
        ]
        self.service.supports_prefetch = True
        self.service.new_session.return_value = session
        self.dialog._prepare_summary_previews(self.plan)
        cancellation = self.dialog.summary_voice_preview.preparation_cancellation

        self.select(self.other)
        self.assertFalse(cancellation.is_set())
        self.run_tasks()

        self.assertGreaterEqual(session.generate.call_count, 2)
        self.player.play_bytes.assert_not_called()

    def test_destroying_owner_cancels_queued_preparation(self):
        parent = QObject()
        session = Mock(supports_prefetch=True)
        self.service.supports_prefetch = True
        self.service.new_session.return_value = session
        preview = StoryVoicePreview(self.service, parent, thread_pool=self.pool)
        preview.prepare(((self.plan, self.group, self.group.source_id, "Hello."),))
        cancellation = preview.preparation_cancellation

        shiboken6.delete(parent)
        self.run_tasks()

        self.assertTrue(cancellation.is_set())
        session.close.assert_called_once()
        session.generate.assert_not_called()

    def test_unreviewed_role_previews_actual_narrator_fallback(self):
        self.select(self.other)

        self.dialog.play_selected_voice.click()
        self.run_tasks()

        self.assertEqual(self.service.generate.call_args.args[2], "preset:marius")
        self.assertNotEqual(
            self.service.generate.call_args.args[2], self.other.candidates[0].source_id
        )

    def test_narrator_button_plays_inline_even_with_voices_editor_available(self):
        self.assertTrue(self.dialog.narrator_controls.isHidden())
        self.assertTrue(self.dialog.play_summary_narrator.isEnabled())

        self.dialog.play_summary_narrator.click()
        self.run_tasks()

        self.assertEqual(self.service.generate.call_args.args[2], "preset:marius")
        self.dialog.game_narrator_chooser.assert_not_called()
        self.assertEqual(self.dialog.play_summary_narrator.text(), "Narrator")
        self.assertEqual(
            self.dialog.play_summary_narrator.accessibleName(), "Stop narrator preview"
        )
        self.assertEqual(
            self.dialog.play_summary_narrator.toolTip(),
            "Stop the narrator preview.",
        )
        self.dialog.play_summary_narrator.click()
        self.assertFalse(self.dialog.summary_voice_preview.active)
        self.assertEqual(self.dialog.play_summary_narrator.text(), "Narrator")
        self.assertEqual(
            self.dialog.play_summary_narrator.accessibleName(), "Play narrator preview"
        )
        self.assertEqual(
            self.dialog.play_summary_narrator.toolTip(), "Play the narrator preview."
        )

    def test_switching_roles_discards_old_result_and_cancels_old_request(self):
        self.select(self.group)
        self.dialog.play_selected_voice.click()
        cancelled = self.dialog.summary_voice_preview.cancellation

        self.select(self.other)
        self.assertTrue(cancelled.is_set())
        self.run_tasks()
        self.player.play_bytes.assert_not_called()

        self.dialog.play_selected_voice.click()
        self.run_tasks()
        self.player.play_bytes.assert_called_once()
        self.assertEqual(self.service.generate.call_args.args[2], "preset:marius")

    def test_ready_sample_plays_without_waiting_for_cancelled_worker(self):
        self.service.supports_prefetch = True
        session = Mock(supports_prefetch=True)
        session.peek.return_value = None
        self.service.peek.return_value = None
        self.service.new_session.return_value = session
        self.select(self.group)
        self.dialog.play_selected_voice.click()
        old_request = self.dialog.summary_voice_preview.cancellation
        self.assertEqual(len(self.pool.tasks), 1)

        self.select(self.other)
        session.peek.return_value = self.preview
        self.dialog.play_selected_voice.click()

        self.assertTrue(old_request.is_set())
        self.assertEqual(len(self.pool.tasks), 1)
        self.player.play_bytes.assert_called_once()
        self.run_tasks()
        self.player.play_bytes.assert_called_once()

    def test_leaving_summary_cancels_and_suppresses_autoplay(self):
        self.select(self.group)
        self.dialog.play_selected_voice.click()
        cancellation = self.dialog.summary_voice_preview.cancellation

        self.application.sendEvent(
            self.dialog.voice_confirmation, QEvent(QEvent.Type.Hide)
        )
        self.run_tasks()

        self.assertTrue(cancellation.is_set())
        self.player.play_bytes.assert_not_called()
        self.assertFalse(self.dialog.summary_voice_preview.active)

    def test_changed_preview_is_rejected_before_playback(self):
        self.select(self.group)
        self.dialog.play_selected_voice.click()
        self.preview.path.write_bytes(b"changed")

        with self.assertLogs("vntts.story_voice_preview_ui", level="ERROR") as logged:
            self.run_tasks()

        self.player.play_bytes.assert_not_called()
        self.assertIn(
            "changed after validation", self.dialog.voice_confirmation_status.text()
        )
        self.assertIn(
            "Unable to validate or start selected voice preview", logged.output[0]
        )

    def test_missing_narrator_explains_fallback_instead_of_using_candidate(self):
        self.select(self.other)
        self.dialog.narrator_choice.setCurrentIndex(0)
        self.assertFalse(self.dialog.play_summary_narrator.isEnabled())
        self.assertIn(
            "Choose a supported narrator", self.dialog.play_summary_narrator.toolTip()
        )

        self.dialog.play_selected_voice.click()

        self.assertIn("Choose a narrator", self.dialog.voice_confirmation_status.text())
        self.service.generate.assert_not_called()
        self.assertEqual(self.pool.tasks, [])

    def test_summary_preview_actions_fit_compact_width_with_larger_font(self):
        font = self.dialog.font()
        font.setPointSizeF(font.pointSizeF() * 2)
        self.dialog.setFont(font)
        self.dialog.resize(620, 440)
        self.dialog.show()
        self.application.processEvents()

        viewport = self.dialog.content_scroll.viewport()
        for button in (
            self.dialog.play_selected_voice,
            self.dialog.inspect_character_voice,
            self.dialog.back_to_story_selection,
            self.dialog.play_summary_narrator,
            self.dialog.edit_confirmed_narrator,
        ):
            with self.subTest(button=button.text()):
                left = button.mapTo(viewport, QPoint()).x()
                self.assertGreaterEqual(left, 0)
                self.assertLessEqual(left + button.width(), viewport.width())

    def test_confirmation_shows_its_scroll_container_from_story_selection(self):
        self.dialog.content_scroll.hide()

        self.dialog._show_voice_confirmation(self.plan)

        self.assertFalse(self.dialog.content_scroll.isHidden())
        self.assertFalse(self.dialog.voice_confirmation.isHidden())

    def test_summary_keeps_model_identity_in_preparation_details(self):
        model = "/models/huggingface/openmoss/MOSS-TTS-Local-Transformer-v1.5-MLX-int8"
        self.dialog._voice_plan = replace(
            self.plan, synthesis_backend="moss-tts", synthesis_model=model
        )

        self.dialog._show_voice_confirmation(self.dialog._voice_plan)

        self.assertIn(model, self.dialog.voice_engine_details.text())
        self.assertNotIn(model, self.dialog.confirmed_narrator.text())
        self.assertIn(model, self.dialog._full_narrator_configuration())
        self.dialog._narrator_choice_changed()
        self.assertIn(model, self.dialog._full_narrator_configuration())


if __name__ == "__main__":
    unittest.main()

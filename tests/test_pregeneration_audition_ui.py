import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from vntts_artifacts.file_integrity import sha256_file  # noqa: E402

from tests.test_pregeneration_audition import ambiguous_fixture  # noqa: E402
from tests.test_pregeneration_setup import (  # noqa: E402
    ManualThreadPool,
    write_story_index,
)
from vntts.pregeneration_audition import VoiceAuditionCancelled  # noqa: E402
from vntts.pregeneration_audition_ui import VoiceAuditionPanel  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import (  # noqa: E402
    VoiceCandidate,
    VoiceDecisionStore,
)
from vntts.settings import AppSettings  # noqa: E402
from vntts.voices import default_voice_choice_id  # noqa: E402


def with_second_candidate(plan, group):
    reference = Path(plan.voice_manifest).parent / "references" / "centurion.wav"
    second = VoiceCandidate(
        source_id="character:centurion",
        source_character="Centurion",
        source_speaker="centurion-v1",
        reference_sha256s=(sha256_file(reference),),
    )
    group = replace(group, candidates=(*group.candidates, second))
    plan = replace(
        plan,
        groups=tuple(
            group if value.group_id == group.group_id else value
            for value in plan.groups
        ),
    )
    return plan, group


class VoiceAuditionPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_multimedia_output_is_lazy_when_no_audition_is_started(self):
        with (
            TemporaryDirectory() as temporary_directory,
            patch("vntts.pregeneration_audition_ui.QMediaPlayer") as media_player,
            patch("vntts.pregeneration_audition_ui.QAudioOutput") as audio_output,
        ):
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(Path(temporary_directory) / "decisions.json"),
                preview_service=Mock(),
            )

            media_player.assert_not_called()
            audio_output.assert_not_called()
            panel.shutdown()
            panel.deleteLater()

    def test_auto_preview_replay_candidate_cycle_and_persisted_choice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            decisions = VoiceDecisionStore(root / "decisions.json")
            preview_service = Mock()
            preview_service.generate.side_effect = lambda _plan, _group, source: Mock(
                path=root / f"{source.removeprefix('character:')}.wav"
            )
            player = Mock()
            pool = ManualThreadPool()
            completed = Mock()
            panel = VoiceAuditionPanel(
                decisions,
                preview_service=preview_service,
                thread_pool=pool,
                player=player,
            )
            panel.completed.connect(completed)

            panel.start(plan)
            self.assertFalse(panel.a_use.isEnabled())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(panel.a_use.isEnabled())
            self.assertTrue(panel.b_use.isEnabled())
            self.assertIn("Recommended", panel.a_title.text())
            panel.a_play.click()
            panel.b_play.click()
            self.assertEqual(player.play.call_count, 2)
            panel.b_use.click()
            self.assertIn("Saving", panel.status.text())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(completed.call_count, 1)
            self.assertEqual(
                decisions.choice_for(group.group_id, group.decision_context_sha256),
                "character:centurion",
            )
            self.assertEqual(preview_service.generate.call_count, 2)
            preview_service.close.assert_not_called()
            panel.shutdown()
            preview_service.close.assert_called_once_with()
            panel.deleteLater()

    def test_neither_without_narrator_uses_safe_choice_without_authoring_controls(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            decisions = VoiceDecisionStore(root / "decisions.json")
            preview_service = Mock()
            preview_service.generate.return_value = Mock(path=root / "preview.wav")
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions,
                preview_service=preview_service,
                thread_pool=pool,
                player=Mock(),
            )
            panel.start(plan)
            pool.tasks.pop().run()
            self.application.processEvents()

            panel.neither_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(
                decisions.choice_for(group.group_id, group.decision_context_sha256),
                group.candidates[0].source_id,
            )
            visible_text = " ".join(
                (
                    panel.character.text(),
                    panel.sample.text(),
                    panel.a_reason.text(),
                    panel.status.text(),
                )
            ).casefold()
            for authoring_word in ("manifest", "checksum", "backend", "model", "seed"):
                self.assertNotIn(authoring_word, visible_text)
            panel.deleteLater()

    def test_second_phrase_is_generated_only_when_requested(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            preview_service = Mock()
            preview_service.generate.return_value = Mock(path=root / "preview.wav")
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(root / "decisions.json"),
                preview_service=preview_service,
                thread_pool=pool,
                player=Mock(),
            )

            panel.start(plan)
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(panel.another_sample_button.isEnabled())
            self.assertTrue(
                all("text" not in call.kwargs for call in preview_service.generate.call_args_list)
            )

            panel.another_sample_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertIn(group.alternate_sample_text, panel.sample.text())
            self.assertTrue(panel.a_use.isEnabled())
            alternate_calls = preview_service.generate.call_args_list[-2:]
            self.assertEqual(
                [call.kwargs["text"] for call in alternate_calls],
                [group.alternate_sample_text, group.alternate_sample_text],
            )
            panel.shutdown()
            panel.deleteLater()

    def test_save_failure_keeps_the_same_decision_available(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, _group = with_second_candidate(plan, group)
            decisions = Mock()
            decisions.remember_many.side_effect = OSError("disk unavailable")
            preview_service = Mock()
            preview_service.generate.return_value = Mock(path=root / "preview.wav")
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions,
                preview_service=preview_service,
                thread_pool=pool,
                player=Mock(),
            )
            panel.start(plan)
            pool.tasks.pop().run()
            self.application.processEvents()

            panel.a_use.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertIn("Unable to save", panel.status.text())
            self.assertTrue(panel.retry_save_button.isEnabled())
            self.assertTrue(panel.isVisible())
            panel.shutdown()
            panel.deleteLater()

    def test_failed_candidate_is_not_shown_as_a_decision(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            decisions = VoiceDecisionStore(root / "decisions.json")
            preview_service = Mock()

            def generate(_plan, _group, source_id):
                if source_id == group.candidates[1].source_id:
                    raise RuntimeError("preview failed")
                return Mock(path=root / "preview.wav")

            preview_service.generate.side_effect = generate
            pool = ManualThreadPool()
            completed = Mock()
            panel = VoiceAuditionPanel(
                decisions,
                preview_service=preview_service,
                thread_pool=pool,
                player=Mock(),
            )
            panel.completed.connect(completed)

            panel.start(plan)
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertFalse(panel.a_box.isVisible())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(completed.call_count, 1)
            self.assertEqual(
                decisions.choice_for(group.group_id, group.decision_context_sha256),
                group.candidates[0].source_id,
            )
            panel.deleteLater()

    def test_neither_previews_and_selects_configured_narrator(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            narrator = VoiceCandidate(
                "preset:alba",
                "alba",
                "alba",
                (),
                120,
                "Configured narrator voice",
            )
            group = replace(group, narrator_candidate=narrator)
            plan = replace(
                plan,
                synthesis_backend="pocket-tts",
                synthesis_profile="default",
                groups=tuple(
                    group if value.group_id == group.group_id else value
                    for value in plan.groups
                ),
            )
            decisions = VoiceDecisionStore(root / "decisions.json")
            preview_service = Mock()
            preview_service.generate.return_value = Mock(path=root / "preview.wav")
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions,
                preview_service=preview_service,
                thread_pool=pool,
                player=Mock(),
            )

            panel.start(plan)
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.neither_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(panel.a_title.text(), "Narrator fallback")
            self.assertTrue(panel.a_use.isEnabled())
            panel.a_use.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(
                decisions.choice_for(group.group_id, group.decision_context_sha256),
                default_voice_choice_id,
            )
            panel.deleteLater()

    def test_choose_all_automatically_can_cancel_active_preview(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            decisions = VoiceDecisionStore(root / "decisions.json")
            preview_service = Mock()
            preview_service.generate.side_effect = VoiceAuditionCancelled("cancelled")
            pool = ManualThreadPool()
            completed = Mock()
            panel = VoiceAuditionPanel(
                decisions,
                preview_service=preview_service,
                thread_pool=pool,
                player=Mock(),
            )
            panel.completed.connect(completed)

            panel.start(plan)
            panel.choose_all_button.click()
            preview_service.cancel.assert_called_once_with()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(completed.call_count, 0)
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(completed.call_count, 1)
            self.assertEqual(
                decisions.choice_for(group.group_id, group.decision_context_sha256),
                group.candidates[0].source_id,
            )
            panel.deleteLater()

    def test_cancel_waits_for_preview_worker_terminal_result(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, _group = with_second_candidate(plan, group)
            preview_service = Mock()
            preview_service.generate.side_effect = VoiceAuditionCancelled("cancelled")
            pool = ManualThreadPool()
            cancelled = Mock()
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(root / "decisions.json"),
                preview_service=preview_service,
                thread_pool=pool,
                player=Mock(),
            )
            panel.cancelled.connect(cancelled)
            panel.start(plan)

            panel.cancel()
            self.assertEqual(cancelled.call_count, 0)
            preview_service.cancel.assert_called_once_with()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(cancelled.call_count, 1)
            preview_service.close.assert_called_once_with()
            panel.deleteLater()


class OfflineAudioPreparationAuditionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_saved_audition_replans_before_generation_input(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            plan, group, _manifest = ambiguous_fixture(root / "voice-fixture")
            plan, group = with_second_candidate(plan, group)
            resolved_group = replace(
                group,
                route="voice",
                resolution="saved-player-decision",
            )
            resolved = replace(
                plan,
                groups=tuple(
                    resolved_group if value.group_id == group.group_id else value
                    for value in plan.groups
                ),
            )
            voice_plan_store = Mock()
            voice_plan_store.create.side_effect = (plan, resolved)
            decisions = VoiceDecisionStore(root / "decisions.json")
            preview_service = Mock()
            preview_service.generate.return_value = Mock(path=root / "preview.wav")
            input_store = Mock()
            input_store.materialize.return_value = Mock(ready_items=1)
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                voice_plan_store=voice_plan_store,
                voice_decisions=decisions,
                audition_service=preview_service,
                preview_player=Mock(),
                input_store=input_store,
                thread_pool=pool,
            )

            dialog.continue_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(dialog.auditioning_voices)
            self.assertFalse(dialog.preparing_inputs)

            pool.tasks.pop().run()
            self.application.processEvents()
            dialog.voice_panel.a_use.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(dialog.planning_voices)
            self.assertEqual(voice_plan_store.create.call_count, 1)

            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertFalse(dialog.auditioning_voices)
            self.assertTrue(dialog.preparing_inputs)
            self.assertEqual(voice_plan_store.create.call_count, 2)
            self.assertEqual(
                decisions.choice_for(group.group_id, group.decision_context_sha256),
                group.candidates[0].source_id,
            )
            self.assertEqual(len(pool.tasks), 1)
            dialog.voice_panel.shutdown()
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()

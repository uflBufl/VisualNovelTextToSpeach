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
            self.assertFalse(panel.use_button.isEnabled())
            self.assertNotIn("character:", panel.candidate.text())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(panel.use_button.isEnabled())
            self.assertEqual(player.play.call_count, 1)
            panel.replay_button.click()
            self.assertEqual(player.play.call_count, 2)

            panel.next_button.click()
            self.assertIn("Candidate 2 of 2", panel.candidate.text())
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.use_button.click()
            self.assertIn("Saving", panel.status.text())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(completed.call_count, 1)
            self.assertEqual(
                decisions.choice_for(group.group_id, group.decision_context_sha256),
                "character:centurion",
            )
            self.assertEqual(preview_service.generate.call_count, 2)
            preview_service.close.assert_called_once_with()
            panel.deleteLater()

    def test_narrator_choice_is_saved_without_exposing_authoring_controls(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
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

            panel.narrator_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(
                decisions.choice_for(group.group_id, group.decision_context_sha256),
                default_voice_choice_id,
            )
            visible_text = " ".join(
                (
                    panel.character.text(),
                    panel.sample.text(),
                    panel.candidate.text(),
                    panel.status.text(),
                )
            ).casefold()
            for authoring_word in ("manifest", "checksum", "backend", "model", "seed"):
                self.assertNotIn(authoring_word, visible_text)
            panel.deleteLater()

    def test_save_failure_keeps_the_same_decision_available(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, _group, _manifest = ambiguous_fixture(root)
            decisions = Mock()
            decisions.remember.side_effect = OSError("disk unavailable")
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

            panel.use_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertIn("Unable to save", panel.status.text())
            self.assertTrue(panel.use_button.isEnabled())
            self.assertTrue(panel.isVisible())
            panel.shutdown()
            panel.deleteLater()

    def test_cancel_waits_for_preview_worker_terminal_result(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, _group, _manifest = ambiguous_fixture(root)
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
            dialog.voice_panel.use_button.click()
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

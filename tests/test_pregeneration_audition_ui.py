import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from vntts_artifacts.file_integrity import sha256_file  # noqa: E402

from tests.test_pregeneration_audition import (  # noqa: E402
    ambiguous_fixture,
    clean_wav_bytes,
)
from tests.test_pregeneration_setup import (  # noqa: E402
    ManualThreadPool,
    write_story_index,
)
from vntts.pregeneration_audition import (  # noqa: E402
    VoiceAuditionCancelled,
    VoiceAuditionPreviewService,
)
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


def with_second_group(plan, group):
    second = replace(
        group,
        group_id="1" * 64,
        character="Second character",
        line_ids=("second-line",),
        decision_context_sha256="2" * 64,
        control_sha256="3" * 64,
    )
    return replace(plan, groups=(*plan.groups, second)), second


class VoiceAuditionPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_retry_after_shutdown_gets_a_usable_preview_service(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, _group = with_second_candidate(plan, group)
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(root / "decisions.json"), thread_pool=pool
            )
            previous = panel.preview_service
            self.assertFalse(panel.runtime_timer.isActive())
            panel.shutdown()
            panel.start(plan)
            panel.a_play.click()
            self.assertTrue(panel.runtime_timer.isActive())
            self.assertIsNot(panel.preview_service, previous)
            self.assertFalse(panel.preview_service._closed)
            self.assertTrue(pool.tasks)
            panel.preview_runner.cancel()
            panel.shutdown()
            self.assertFalse(panel.runtime_timer.isActive())

    def test_multimedia_output_is_lazy_when_no_audition_is_started(self):
        with (
            TemporaryDirectory() as temporary_directory,
            patch("vntts.pregeneration_audition_ui.QMediaPlayer") as media_player,
        ):
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(Path(temporary_directory) / "decisions.json"),
                preview_service=Mock(),
            )

            media_player.assert_not_called()
            panel.shutdown()
            panel.deleteLater()

    def test_auto_preview_replay_candidate_cycle_and_persisted_choice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            decisions = VoiceDecisionStore(root / "decisions.json")
            preview_service = Mock()
            preview_service.generate.side_effect = (
                lambda _plan, _group, source, **_options: Mock(
                    path=root / f"{source.removeprefix('character:')}.wav"
                )
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
            panel.a_play.click()
            self.assertFalse(panel.a_use.isEnabled())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(panel.a_use.isEnabled())
            self.assertIn(group.candidates[0].source_character, panel.a_title.text())
            self.assertEqual(preview_service.generate.call_count, 1)
            self.assertEqual(len(panel._displayed), 1)
            panel.a_play.click()
            panel.neither_button.click()
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.a_play.click()
            self.assertEqual(player.play.call_count, 4)
            panel.a_use.click()
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

    def test_single_sample_waits_for_acceptance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            self.assertEqual(len(group.candidates), 1)
            decisions = Mock()
            previews = Mock()
            previews.generate.return_value = Mock(path=root / "preview.wav")
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=previews, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(len(panel._displayed), 1)
            decisions.remember_many.assert_not_called()
            self.assertEqual(pool.tasks, [])
            panel.a_use.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            decisions.remember_many.assert_called_once_with(
                ((group, group.candidates[0].source_id),)
            )

    def test_original_reference_can_be_accepted_without_generating_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            decisions = Mock()
            service = Mock(spec=VoiceAuditionPreviewService)
            service.reference_audio.side_effect = (
                VoiceAuditionPreviewService().reference_audio
            )
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=service, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan)
            self.assertFalse(panel.a_use.isEnabled())
            panel.a_original.click()
            self.assertTrue(panel.a_use.isEnabled())
            panel.a_use.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            service.generate.assert_not_called()
            decisions.remember_many.assert_called_once_with(
                ((group, group.candidates[0].source_id),)
            )

    def test_original_buttons_play_exact_references_without_synthesis_and_recheck_files(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, manifest = ambiguous_fixture(root)
            reference_a = manifest.parent / "references" / "rhiannon.wav"
            reference_b = manifest.parent / "references" / "centurion.wav"
            reference_b.write_bytes(clean_wav_bytes(amplitude=0.2))
            plan, group = with_second_candidate(plan, group)
            group = replace(group, narrator_candidate=group.candidates[1])
            plan = replace(plan, groups=(group,))
            service = Mock(spec=VoiceAuditionPreviewService)
            service.reference_audio.side_effect = (
                VoiceAuditionPreviewService().reference_audio
            )
            service.generate.return_value = Mock(path=root / "generated.wav")
            player = Mock()
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(root / "decisions.json"),
                preview_service=service,
                thread_pool=pool,
                player=player,
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            for index, reference in enumerate((reference_a, reference_b)):
                if index:
                    panel.neither_button.click()
                    panel.a_play.click()
                    pool.tasks.pop().run()
                    self.application.processEvents()
                calls_before = service.generate.call_count
                self.assertTrue(panel.a_original.isEnabled())
                panel.a_original.click()
                self.assertEqual(
                    Path(player.setSource.call_args.args[0].toLocalFile()),
                    reference.resolve(),
                )
                self.assertEqual(service.generate.call_count, calls_before)
            self.assertEqual(service.generate.call_count, 2)
            self.assertTrue(panel.a_use.isEnabled())
            panel.a_play.click()
            self.assertEqual(
                Path(player.setSource.call_args.args[0].toLocalFile()),
                root / "generated.wav",
            )

            # Narrator selection stores "default", but its original uses its actual source.
            panel.neither_button.click()
            panel.a_play.click()
            self.application.processEvents()
            panel.a_original.click()
            self.assertEqual(
                Path(player.setSource.call_args.args[0].toLocalFile()),
                reference_b.resolve(),
            )
            self.assertEqual(
                service.reference_audio.call_args.args[2], "character:centurion"
            )
            reference_b.write_bytes(b"changed")
            player.reset_mock()
            panel.a_original.click()
            player.play.assert_not_called()
            self.assertIn("changed", panel.status.text())
            self.assertEqual(service.generate.call_count, 2)
            panel.cancel()
            player.reset_mock()
            panel._play_original()
            player.play.assert_not_called()

    def test_rejecting_all_candidates_does_not_save_a_rejected_voice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            decisions = Mock()
            service = Mock()
            service.generate.return_value = Mock(path=root / "preview.wav")
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=service, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            for _ in group.candidates:
                panel.neither_button.click()
                while pool.tasks:
                    pool.tasks.pop(0).run()
                    self.application.processEvents()
            decisions.remember_many.assert_not_called()
            self.assertTrue(panel.isVisible())

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
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(panel.another_sample_button.isEnabled())
            self.assertTrue(
                all(
                    call.kwargs.get("text", group.sample_text) == group.sample_text
                    for call in preview_service.generate.call_args_list
                )
            )

            panel.another_sample_button.click()
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertIn(group.alternate_sample_text, panel.sample.text())
            self.assertTrue(panel.a_use.isEnabled())
            alternate_calls = preview_service.generate.call_args_list[-1:]
            self.assertEqual(
                [call.kwargs["text"] for call in alternate_calls],
                [group.alternate_sample_text],
            )
            panel.shutdown()
            panel.deleteLater()

    def test_only_requested_samples_generate_and_cached_candidate_can_be_accepted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            plan, second = with_second_group(plan, group)
            decisions = Mock()
            previews = Mock()
            previews.generate.return_value = Mock(path=root / "preview.wav")
            previews.backend.runtime_status = "GPU: RTX 2070 SUPER <8 GB>"
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=previews, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan)
            previews.generate.assert_not_called()
            self.assertEqual(pool.tasks, [])
            panel.a_original.click()
            previews.generate.assert_not_called()
            panel.a_play.click()
            self.assertEqual(panel.runtime.textFormat(), Qt.TextFormat.PlainText)
            self.assertIn("GPU: RTX 2070 SUPER", panel.runtime.text())
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(pool.tasks, [])
            panel.neither_button.click()
            self.assertFalse(panel.a_use.isEnabled())
            panel.neither_button.click()
            panel.a_play.click()
            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(previews.generate.call_count, 1)
            panel.a_use.click()
            self.assertEqual(panel.current_group(), second)
            self.assertEqual(pool.tasks, [])
            self.assertFalse(panel.a_use.isEnabled())
            decisions.remember_many.assert_not_called()

    def test_failed_alternate_phrase_keeps_selected_voice_unaccepted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            previews = Mock()

            def generate(_plan, _group, source_id, **options):
                if options.get("text") == group.alternate_sample_text:
                    raise RuntimeError("alternate preview failed")
                return Mock(path=root / "preview.wav")

            previews.generate.side_effect = generate
            decisions = Mock()
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=previews, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.neither_button.click()
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.another_sample_button.click()
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(
                previews.generate.call_args.args[2], group.candidates[1].source_id
            )
            self.assertFalse(panel.a_use.isEnabled())
            self.assertIn("failed", panel.status.text())
            decisions.remember_many.assert_not_called()

    def test_failed_narrator_can_be_retried_and_explicitly_accepted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            narrator = replace(
                group.candidates[0], source_id="preset:alba", reference_sha256s=()
            )
            group = replace(group, narrator_candidate=narrator)
            plan = replace(plan, groups=(group,))
            previews = Mock()
            previews.generate.side_effect = [
                RuntimeError("narrator preview failed"),
                Mock(path=root / "preview.wav"),
            ]
            decisions = Mock()
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=previews, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan)
            panel.neither_button.click()
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertFalse(panel.a_use.isEnabled())
            decisions.remember_many.assert_not_called()
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(panel.a_use.isEnabled())
            panel.a_use.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            decisions.remember_many.assert_called_once_with(
                ((group, default_voice_choice_id),)
            )

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
            panel.a_play.click()
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

    def test_failed_preview_requires_an_explicit_choice_and_next_sample_can_be_accepted(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            decisions = Mock()
            service = Mock()
            service.generate.side_effect = [
                RuntimeError("preview failed"),
                Mock(path=root / "preview.wav"),
            ]
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=service, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertFalse(panel.a_use.isEnabled())
            self.assertIn("failed", panel.status.text())
            decisions.remember_many.assert_not_called()
            panel.neither_button.click()
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(panel.a_use.isEnabled())
            decisions.remember_many.assert_not_called()
            panel.a_use.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            decisions.remember_many.assert_called_once_with(
                ((group, group.candidates[1].source_id),)
            )

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
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            for _ in group.candidates:
                panel.neither_button.click()
                panel.a_play.click()
                pool.tasks.pop().run()
                self.application.processEvents()

            self.assertEqual(panel.a_title.text(), "Narrator fallback")
            self.assertTrue(panel.a_use.isEnabled())
            self.assertFalse(panel.a_original.isEnabled())
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
            panel.a_play.click()
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
            panel.a_play.click()

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

            dialog.voice_panel.a_play.click()
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
            self.assertFalse(dialog._awaiting_voice_confirmation)
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

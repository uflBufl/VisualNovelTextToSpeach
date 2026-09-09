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
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertIsNone(group.anchor_source_id)
            self.assertFalse(panel.anchor_button.isVisible())
            for button, reference in (
                (panel.a_original, reference_a),
                (panel.b_original, reference_b),
            ):
                self.assertTrue(button.isEnabled())
                button.click()
                self.assertEqual(
                    Path(player.setSource.call_args.args[0].toLocalFile()),
                    reference.resolve(),
                )
            self.assertEqual(service.generate.call_count, 2)
            self.assertTrue(panel.a_use.isEnabled())
            self.assertTrue(panel.b_use.isEnabled())
            panel.a_play.click()
            self.assertEqual(
                Path(player.setSource.call_args.args[0].toLocalFile()),
                root / "generated.wav",
            )

            # Narrator selection stores "default", but its original uses its actual source.
            panel.neither_button.click()
            pool.tasks.pop().run()
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
            self.assertEqual(service.generate.call_count, 3)
            panel.cancel()
            player.reset_mock()
            panel._play_original_slot(0)
            player.play.assert_not_called()

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
                all(
                    "text" not in call.kwargs
                    for call in preview_service.generate.call_args_list
                )
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

    def test_next_comparison_is_prefetched_and_used_without_a_second_render(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            plan, second = with_second_group(plan, group)
            decisions = VoiceDecisionStore(root / "decisions.json")
            preview_service = Mock()
            preview_service.backend.runtime_status = "GPU: RTX 2070 SUPER <8 GB>"
            preview_service.generate.side_effect = lambda _plan, value, source: Mock(
                path=root / f"{value.group_id}-{source.removeprefix('character:')}.wav"
            )
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions,
                preview_service=preview_service,
                thread_pool=pool,
                player=Mock(),
            )

            panel.start(plan)
            self.assertTrue(panel.runtime_timer.isActive())
            self.assertTrue(panel.runtime.isVisibleTo(panel))
            self.assertEqual(panel.runtime.textFormat(), Qt.TextFormat.PlainText)
            self.assertIn("GPU: RTX 2070 SUPER", panel.runtime.text())
            pool.tasks.pop(0).run()
            self.application.processEvents()

            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(len(pool.tasks), 1)
            panel.play_a()
            self.assertIn("background", panel.runtime.text())
            self.assertIn("GPU: RTX 2070 SUPER", panel.runtime.text())
            self.assertNotIn("No preview generation", panel.runtime.text())
            preview_service.backend.runtime_status = None
            panel.runtime_timer.timeout.emit()
            self.assertIn("not running", panel.runtime.text())
            self.assertNotIn("GPU", panel.runtime.text())
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertEqual(preview_service.generate.call_count, 4)
            self.assertIn("No preview generation", panel.runtime.text())

            panel.a_use.click()

            self.assertEqual(panel.current_group(), second)
            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(pool.tasks, [])
            self.assertEqual(preview_service.generate.call_count, 4)

            panel.b_use.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertEqual(
                decisions.choice_for(second.group_id, second.decision_context_sha256),
                second.candidates[1].source_id,
            )
            self.assertFalse(panel.runtime_timer.isActive())
            panel.shutdown()
            self.assertFalse(panel.runtime_timer.isActive())
            panel.deleteLater()

    def test_alternate_phrase_with_failed_candidates_resets_pair_position(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            candidates = tuple(
                replace(group.candidates[0], source_id=f"character:candidate-{index}")
                for index in range(4)
            )
            group = replace(group, candidates=candidates)
            plan = replace(plan, groups=(group,))
            previews = Mock()

            def generate(_plan, _group, source_id, **options):
                if "text" in options and source_id in {
                    candidate.source_id for candidate in candidates[2:]
                }:
                    raise RuntimeError("alternate preview failed")
                return Mock(path=root / "preview.wav")

            previews.generate.side_effect = generate
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                Mock(), preview_service=previews, thread_pool=pool, player=Mock()
            )
            panel.start(plan)
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.neither_button.click()
            self.assertEqual(panel._displayed[0][2], candidates[2].source_id)
            panel.another_sample_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(
                tuple(value[2] for value in panel._displayed),
                tuple(candidate.source_id for candidate in candidates[:2]),
            )
            self.assertTrue(panel.a_use.isEnabled())
            self.assertTrue(panel.b_use.isEnabled())
            panel.shutdown()
            panel.deleteLater()

    def test_use_narrator_without_preview_saves_instead_of_retrying_generation(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            candidates = tuple(
                replace(group.candidates[0], source_id=f"character:candidate-{index}")
                for index in range(3)
            )
            narrator = replace(group.candidates[0], source_id="preset:alba")
            group = replace(group, candidates=candidates, narrator_candidate=narrator)
            plan = replace(plan, groups=(group,))
            previews = Mock()

            def generate(_plan, _group, source_id, **_options):
                if source_id == narrator.source_id:
                    raise RuntimeError("narrator preview failed")
                return Mock(path=root / "preview.wav")

            previews.generate.side_effect = generate
            decisions = Mock()
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=previews, thread_pool=pool, player=Mock()
            )
            panel.start(plan)
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.neither_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(
                panel.neither_button.text(), "Use narrator without preview"
            )
            calls_before = previews.generate.call_count
            panel.neither_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(previews.generate.call_count, calls_before)
            decisions.remember_many.assert_called_once_with(
                ((group, default_voice_choice_id),)
            )
            panel.shutdown()
            panel.deleteLater()

    def test_cancelling_does_not_wait_for_speculative_preview(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            plan, _second = with_second_group(plan, group)
            preview_service = Mock()
            preview_service.generate.return_value = Mock(path=root / "preview.wav")
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
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertEqual(len(pool.tasks), 1)

            panel.cancel()

            self.assertEqual(cancelled.call_count, 1)
            self.assertFalse(panel.runtime_timer.isActive())
            self.assertIn("Stopping", panel.runtime.text())
            preview_service.close.assert_not_called()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            preview_service.close.assert_called_once_with()
            self.assertIn("No preview generation", panel.runtime.text())
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

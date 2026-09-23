import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402
from vntts_artifacts.file_integrity import sha256_file  # noqa: E402

from tests.test_pregeneration_audition import (  # noqa: E402
    ambiguous_fixture,
    clean_wav_bytes,
)
from tests.test_pregeneration_setup import (  # noqa: E402
    ManualThreadPool,
    write_story_index,
)
from vntts.game_content_importer import Reverse1999GameImporter  # noqa: E402
from vntts.person_link_suggestions import PersonLinkSuggestion  # noqa: E402
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
from vntts.voice_library import VoiceLibrary  # noqa: E402
from vntts.voices import default_voice_choice_id  # noqa: E402


def with_second_candidate(plan, group):
    reference = Path(plan.voice_manifest).parent / "references" / "centurion.wav"
    second = VoiceCandidate(
        source_id="character:centurion",
        source_character="Centurion",
        source_speaker="centurion-v1",
        reference_sha256s=(sha256_file(reference),),
    )
    group = replace(
        group,
        candidates=(*group.candidates, second),
        candidate_inventory=(*group.candidate_inventory, second),
    )
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


def generated_preview(root: Path):
    path = root / "preview.wav"
    path.write_bytes(clean_wav_bytes())
    return Mock(path=path)


class VoiceAuditionPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_sparse_portraits_are_hidden_consistently(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            portrait = root / "portrait.png"
            self.assertTrue(QPixmap(2, 2).save(str(portrait)))
            group = replace(
                group,
                portrait_image=str(portrait),
                portrait_image_sha256=sha256_file(portrait),
            )
            plan = replace(plan, groups=(group,))
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(root / "decisions.json"),
                preview_service=Mock(),
            )
            self.addCleanup(panel.deleteLater)
            panel.start(plan, group_id=group.group_id)
            self.assertTrue(panel.portrait_image.isVisible())
            panel.cancel()

            sparse, other = with_second_group(plan, group)
            sparse = replace(
                sparse,
                groups=(
                    group,
                    replace(other, portrait_image=None, portrait_image_sha256=None),
                ),
            )
            panel.start(sparse, group_id=group.group_id)
            self.assertFalse(panel.portrait_image.isVisible())
            panel.cancel()

    def test_original_reference_transcript_is_visible_without_source_id(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            candidate = replace(
                group.candidates[0],
                source_character="Player candidate Rhiannon abcdef123456",
                source_excerpts=("Greeting: Good morning, traveller.",),
            )
            group = replace(
                group,
                candidates=(candidate,),
                candidate_inventory=(candidate,),
            )
            plan = replace(plan, groups=(group,))
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(root / "decisions.json"),
                preview_service=Mock(),
            )
            self.addCleanup(panel.deleteLater)
            panel.start(plan, group_id=group.group_id)

            self.assertIn("Good morning", panel.voice_reference.currentText())
            self.assertNotIn("abcdef123456", panel.voice_reference.currentText())
            self.assertIn("Greeting: Good morning", panel.a_reason.text())
            panel.cancel()

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

    def test_automatic_voice_inspector_explains_exact_reference_set(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, unresolved, _manifest = ambiguous_fixture(root)
            plan, unresolved = with_second_candidate(plan, unresolved)
            candidate = unresolved.candidates[0]
            group = replace(
                unresolved,
                route="voice",
                source_id=candidate.source_id,
                source_character=candidate.source_character,
                source_speaker=candidate.source_speaker,
                reference_sha256s=candidate.reference_sha256s,
                resolution="known-character-voice",
                candidates=(candidate,),
            )
            plan = replace(
                plan,
                groups=tuple(
                    group if value.group_id == group.group_id else value
                    for value in plan.groups
                ),
            )
            decisions = VoiceDecisionStore(root / "decisions.json")
            preview_service = Mock()
            reference = root / "reference.wav"
            reference.write_bytes(clean_wav_bytes())
            preview_service.reference_audio.return_value = reference
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions,
                preview_service=preview_service,
                player=Mock(),
                thread_pool=pool,
            )

            panel.start(plan, group_id=group.group_id)

            self.assertTrue(panel.choose_all_button.isHidden())
            self.assertEqual(panel.a_play.text(), "Generate preview")
            self.assertEqual(panel.a_use.text(), "Use this voice")
            self.assertEqual(panel.voice_reference.count(), 2)
            self.assertIn("Reference 1 - 1.2 s", panel.voice_reference.currentText())
            self.assertIn("text unavailable", panel.voice_reference.toolTip())
            self.assertTrue(panel.auto_button.isHidden())
            self.assertIn("1 original reference · 1.2 s total", panel.a_reason.text())
            self.assertIn("future speech and preparation", panel.scope.text())
            panel.reference_details_toggle.setChecked(True)
            self.assertIn(
                candidate.reference_sha256s[0], panel.reference_details.text()
            )
            alternative = unresolved.candidate_inventory[1]
            panel.voice_reference.setCurrentIndex(1)
            self.assertIn("Reference 2", panel.voice_reference.currentText())
            self.assertEqual(panel.a_box.title(), "Original game reference")
            panel.a_original.click()
            preview_service.reference_audio.assert_called_once_with(
                plan, group, alternative.source_id
            )
            panel.a_use.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(
                decisions.choice_for(group.group_id, group.decision_context_sha256),
                alternative.source_id,
            )
            panel.shutdown()
            panel.deleteLater()

    def test_auto_preview_replay_candidate_cycle_and_persisted_choice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            decisions = VoiceDecisionStore(root / "decisions.json")
            preview_service = Mock()

            def generated_preview(_plan, _group, source, **_options):
                path = root / f"{source.removeprefix('character:')}.wav"
                path.write_bytes(clean_wav_bytes())
                return Mock(path=path)

            preview_service.generate.side_effect = generated_preview
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
            self.assertFalse(panel.voice_reference.isEnabled())
            self.assertFalse(panel.preview_phrase.isEnabled())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(panel.a_use.isEnabled())
            self.assertIn("Reference 1", panel.voice_reference.currentText())
            self.assertEqual(preview_service.generate.call_count, 1)
            self.assertEqual(len(panel._displayed), 1)
            panel.a_play.click()
            self.assertEqual(panel.a_play.text(), "Play generated preview")
            panel.a_play.click()
            self.assertIn("Playing", panel.status.text())
            panel.voice_reference.setCurrentIndex(1)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.a_play.click()
            panel.a_play.click()
            self.assertEqual(player.play_bytes.call_count, 4)
            panel.voice_reference.setCurrentIndex(0)
            self.assertTrue(panel.a_use.isEnabled())
            panel.voice_reference.setCurrentIndex(1)
            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(preview_service.generate.call_count, 2)
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
            previews.generate.return_value = generated_preview(root)
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
            generated = root / "generated.wav"
            generated.write_bytes(clean_wav_bytes())
            service.generate.return_value = Mock(path=generated)
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
                    panel.voice_reference.setCurrentIndex(index)
                    panel.a_play.click()
                    pool.tasks.pop().run()
                    self.application.processEvents()
                calls_before = service.generate.call_count
                self.assertTrue(panel.a_original.isEnabled())
                with patch("vntts.support.record_game_import") as report:
                    panel.a_original.click()
                report.assert_any_call(
                    "voice-original-playback",
                    outcome="requested",
                    reference_sha256=sha256_file(reference),
                )
                self.assertEqual(
                    player.play_bytes.call_args.args[0], reference.read_bytes()
                )
                self.assertEqual(
                    Path(player.play_bytes.call_args.kwargs["source"]).resolve(),
                    reference.resolve(),
                )
                self.assertEqual(service.generate.call_count, calls_before)
            self.assertEqual(service.generate.call_count, 2)
            self.assertTrue(panel.a_use.isEnabled())
            panel.a_play.click()
            self.assertEqual(
                player.play_bytes.call_args.args[0], generated.read_bytes()
            )
            self.assertEqual(
                Path(player.play_bytes.call_args.kwargs["source"]).resolve(),
                generated.resolve(),
            )

            # Narrator selection stores "default", but its original uses its actual source.
            panel.voice_reference.setCurrentIndex(2)
            panel.a_play.click()
            self.application.processEvents()
            panel.a_original.click()
            self.assertEqual(
                player.play_bytes.call_args.args[0], reference_b.read_bytes()
            )
            self.assertEqual(
                Path(player.play_bytes.call_args.kwargs["source"]).resolve(),
                reference_b.resolve(),
            )
            self.assertEqual(
                service.reference_audio.call_args.args[2], "character:centurion"
            )
            reference_b.write_bytes(b"changed")
            player.reset_mock()
            panel.a_original.click()
            panel.a_original.click()
            player.play_bytes.assert_not_called()
            self.assertIn("changed", panel.status.text())
            self.assertEqual(service.generate.call_count, 2)
            panel.cancel()
            player.reset_mock()
            panel._play_original()
            player.play_bytes.assert_not_called()

    def test_rejecting_all_candidates_does_not_save_a_rejected_voice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            decisions = Mock()
            service = Mock()
            service.generate.return_value = generated_preview(root)
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
            for index in range(panel.voice_reference.count()):
                panel.voice_reference.setCurrentIndex(index)
            decisions.remember_many.assert_not_called()
            self.assertTrue(panel.isVisible())

    def test_second_phrase_is_generated_only_when_requested(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            preview_service = Mock()
            preview_service.generate.return_value = generated_preview(root)
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
            self.assertEqual(panel.preview_phrase.count(), 2)
            self.assertTrue(
                all(
                    call.kwargs.get("text", group.sample_text) == group.sample_text
                    for call in preview_service.generate.call_args_list
                )
            )

            panel.preview_phrase.setCurrentIndex(1)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(
                panel.preview_phrase.currentText(), group.alternate_sample_text
            )
            self.assertEqual(
                panel.preview_phrase.toolTip(), group.alternate_sample_text
            )
            self.assertTrue(panel.a_use.isEnabled())
            alternate_calls = preview_service.generate.call_args_list[-1:]
            self.assertEqual(
                [call.kwargs["text"] for call in alternate_calls],
                [group.alternate_sample_text],
            )
            panel.preview_phrase.setCurrentIndex(0)
            self.assertEqual(panel.preview_phrase.currentText(), group.sample_text)
            self.assertTrue(panel.a_use.isEnabled())
            panel.preview_phrase.setCurrentIndex(1)
            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(preview_service.generate.call_count, 2)
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
            previews.generate.return_value = generated_preview(root)
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
            panel.voice_reference.setCurrentIndex(1)
            self.assertFalse(panel.a_use.isEnabled())
            panel.voice_reference.setCurrentIndex(0)
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
                return generated_preview(root)

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
            panel.voice_reference.setCurrentIndex(1)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.preview_phrase.setCurrentIndex(1)
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
                generated_preview(root),
            ]
            decisions = Mock()
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=previews, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan)
            panel.voice_reference.setCurrentIndex(1)
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
            preview_service.generate.return_value = generated_preview(root)
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

    def test_back_during_save_finishes_the_choice_before_returning(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            decisions = Mock()
            preview_service = Mock()
            preview_service.generate.return_value = generated_preview(root)
            pool = ManualThreadPool()
            completed = Mock()
            cancelled = Mock()
            panel = VoiceAuditionPanel(
                decisions,
                preview_service=preview_service,
                thread_pool=pool,
                player=Mock(),
            )
            panel.completed.connect(completed)
            panel.cancelled.connect(cancelled)
            panel.start(plan, group_id=group.group_id)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.a_use.click()

            panel.cancel()

            self.assertFalse(panel._cancel_requested)
            self.assertIn("Finishing", panel.status.text())
            pool.tasks.pop().run()
            self.application.processEvents()
            decisions.remember_many.assert_called_once_with(
                ((group, group.candidates[0].source_id),)
            )
            self.assertEqual(completed.call_count, 1)
            self.assertEqual(cancelled.call_count, 0)
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
                generated_preview(root),
            ]
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=service, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan)
            with patch("vntts.support.record_game_import") as support_event:
                panel.a_play.click()
                pool.tasks.pop().run()
                self.application.processEvents()
            support_event.assert_called_once_with(
                "voice-preview",
                outcome="failed",
                command_kind="preview",
                exception_type="RuntimeError",
                reason="preview failed",
                traceback_tail=ANY,
            )
            self.assertFalse(panel.a_use.isEnabled())
            self.assertIn("failed", panel.status.text())
            decisions.remember_many.assert_not_called()
            panel.voice_reference.setCurrentIndex(1)
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

    def test_choose_for_me_skips_a_candidate_with_a_failed_preview(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            decisions = Mock()
            service = Mock()
            service.generate.side_effect = RuntimeError("preview failed")
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
            panel.auto_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            decisions.remember_many.assert_called_once_with(
                ((group, group.candidates[1].source_id),)
            )

    def test_choose_all_uses_the_narrator_after_all_candidates_failed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            narrator = VoiceCandidate(
                "preset:alba", "alba", "alba", (), 120, "Configured narrator voice"
            )
            group = replace(group, narrator_candidate=narrator)
            plan = replace(plan, groups=(group,))
            decisions = Mock()
            service = Mock()
            service.generate.side_effect = RuntimeError("preview failed")
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=service, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)

            panel.start(plan)
            for index, _candidate in enumerate(group.candidates):
                panel.voice_reference.setCurrentIndex(index)
                panel.a_play.click()
                pool.tasks.pop().run()
                self.application.processEvents()
            panel.voice_reference.setCurrentIndex(panel.voice_reference.count() - 1)
            self.assertIn("No original reference", panel.a_reason.text())
            panel.choose_all_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            decisions.remember_many.assert_called_once_with(
                ((group, default_voice_choice_id),)
            )

    def test_no_automatic_voice_explains_the_available_recovery_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            group = replace(group, narrator_candidate=None)
            plan = replace(plan, groups=(group,))
            service = Mock()
            service.generate.side_effect = RuntimeError("preview failed")
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                Mock(), preview_service=service, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)

            panel.start(plan)
            for index, _candidate in enumerate(group.candidates):
                panel.voice_reference.setCurrentIndex(index)
                panel.a_play.click()
                pool.tasks.pop().run()
                self.application.processEvents()
            panel.auto_button.click()

            self.assertIn("Retry", panel.status.text())
            self.assertIn("Edit selected role in Voices", panel.status.text())

    def test_failed_bulk_choice_stays_atomic_and_successful_retry_restores_candidate(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            first = replace(group, group_id="first", narrator_candidate=None)
            second = replace(
                group,
                group_id="second",
                candidates=(replace(group.candidates[0], source_id="broken"),),
                narrator_candidate=None,
            )
            decisions = Mock()
            pool = ManualThreadPool()
            service = Mock()
            service.generate.side_effect = (
                RuntimeError("preview failed"),
                generated_preview(root),
            )
            panel = VoiceAuditionPanel(
                decisions, preview_service=service, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(replace(plan, groups=(first, second)))
            panel._failed_candidate_source_ids.add("broken")
            for _attempt in range(2):
                panel.choose_all_button.click()
                self.assertEqual(panel._pending_decisions, [])
                decisions.remember_many.assert_not_called()
            for _attempt in range(2):
                panel.a_play.click()
                pool.tasks.pop().run()
                self.application.processEvents()
            self.assertEqual(
                panel._automatic_source_id(first), first.candidates[0].source_id
            )

    def test_voice_selector_previews_and_selects_configured_narrator(self):
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
            preview_service.generate.return_value = generated_preview(root)
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
            panel.voice_reference.setCurrentIndex(panel.voice_reference.count() - 1)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(
                panel.voice_reference.currentText().startswith("Narrator fallback")
            )
            self.assertEqual(panel.a_box.title(), "Original reference for narrator")
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
            preview_service.close.assert_not_called()
            panel.start(plan)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertIn("Could not prepare", panel.status.text())
            self.assertNotIn("service is closed", panel.status.text())
            panel.shutdown()
            preview_service.close.assert_called_once_with()
            panel.deleteLater()


class OfflineAudioPreparationAuditionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self._voice_library_directory = TemporaryDirectory()
        self._voice_library_patch = patch(
            "vntts.pregeneration_ui.application_voice_library",
            return_value=VoiceLibrary(
                Path(self._voice_library_directory.name) / "library"
            ),
        )
        self._importer_patch = patch(
            "vntts.pregeneration_ui.Reverse1999GameImporter",
            side_effect=lambda: Reverse1999GameImporter(
                output_root=Path(self._voice_library_directory.name) / "game-content",
                installation_file=Path(self._voice_library_directory.name)
                / "installation.json",
            ),
        )
        self._voice_library_patch.start()
        self._importer_patch.start()

    def tearDown(self):
        self._importer_patch.stop()
        self._voice_library_patch.stop()
        self._voice_library_directory.cleanup()

    def _inspected_voice_dialog(self, root):
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
        library = VoiceLibrary(root / "voice-library")
        for name in ("rhiannon.wav", "centurion.wav"):
            library.discover(
                group.character,
                Path(plan.voice_manifest).parent / "references" / name,
            )
        library.select("Narrator", route="voice", source_id="preset:marius")
        decisions = VoiceDecisionStore(root / "decisions.json", voice_library=library)
        preview_service = Mock()
        preview_service.generate.return_value = generated_preview(root)
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
            voice_library=library,
        )
        return dialog, pool, plan, group, decisions, voice_plan_store

    def test_voice_confirmation_can_return_to_story_selection(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            plan, _group, _manifest = ambiguous_fixture(root / "voice-fixture")
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
            )
            self.addCleanup(dialog.deleteLater)
            dialog.stories.item(0).setCheckState(Qt.CheckState.Checked)
            dialog._voice_plan = plan
            dialog._prepared_voice_manifest = root / "prepared-voices.json"
            dialog._prepared_voice_job = "prepared-job"
            dialog._generation_input = object()
            dialog._show_voice_confirmation(plan)
            dialog.content_scroll.show()

            dialog.back_to_story_selection.click()

            self.assertFalse(dialog._awaiting_voice_confirmation)
            self.assertFalse(dialog.selection_panel.isHidden())
            self.assertTrue(dialog.voice_confirmation.isHidden())
            self.assertTrue(dialog.voice_panel.isHidden())
            self.assertTrue(dialog.content_scroll.isHidden())
            self.assertEqual(dialog.step.text(), "Step 1 of 4 - Choose stories")
            self.assertEqual(dialog.cancel_button.text(), "Cancel")
            self.assertEqual(dialog.stories.item(0).checkState(), Qt.CheckState.Checked)
            self.assertIsNone(dialog.voice_plan())
            self.assertIsNone(dialog._prepared_voice_manifest)
            self.assertIsNone(dialog._prepared_voice_job)
            self.assertIsNone(dialog.generation_input())

    def test_inspector_back_without_voice_choice_remains_enabled(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dialog, _pool, plan, group, decisions, _store = (
                self._inspected_voice_dialog(root)
            )
            self.addCleanup(dialog.deleteLater)
            dialog._voice_plan = plan
            dialog._show_voice_confirmation(plan)
            for row in range(dialog.voice_routes.count()):
                if (
                    dialog.voice_routes.item(row).data(Qt.ItemDataRole.UserRole)
                    == group.character
                ):
                    dialog.voice_routes.setCurrentRow(row)
                    break
            dialog._inspect_character_voice()
            self.assertTrue(dialog.inspecting_voice_plan)

            dialog.cancel_button.click()

            self.assertFalse(dialog.inspecting_voice_plan)
            self.assertFalse(dialog.auditioning_voices)
            self.assertTrue(dialog.cancel_button.isEnabled())
            self.assertFalse(dialog.voice_confirmation.isHidden())
            self.assertEqual(dialog.voice_plan(), plan)
            self.assertIsNone(
                decisions.choice_for(group.group_id, group.decision_context_sha256)
            )

    def test_inspector_back_during_preview_returns_after_worker_stops(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dialog, pool, plan, group, decisions, _store = self._inspected_voice_dialog(
                root
            )
            self.addCleanup(dialog.deleteLater)
            dialog._voice_plan = plan
            dialog._show_voice_confirmation(plan)
            for row in range(dialog.voice_routes.count()):
                if (
                    dialog.voice_routes.item(row).data(Qt.ItemDataRole.UserRole)
                    == group.character
                ):
                    dialog.voice_routes.setCurrentRow(row)
                    break
            dialog._inspect_character_voice()
            dialog.voice_panel.a_play.click()
            self.assertTrue(pool.tasks)

            dialog.cancel_button.click()
            self.assertFalse(dialog.cancel_button.isEnabled())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(dialog.cancel_button.isEnabled())
            self.assertFalse(dialog.inspecting_voice_plan)
            self.assertIsNone(
                decisions.choice_for(group.group_id, group.decision_context_sha256)
            )

    def test_voice_plan_can_link_character_names_without_choosing_a_voice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dialog, pool, plan, group, _decisions, _store = (
                self._inspected_voice_dialog(root)
            )
            self.addCleanup(dialog.deleteLater)
            aderyn = replace(
                group,
                group_id="a" * 64,
                character="Aderyn",
                routing_role="Aderyn",
                line_ids=("line:aderyn",),
            )
            plan = replace(plan, groups=(aderyn,))
            dialog._voice_plan = plan
            dialog._job = Mock(job_id="job", selected_story_ids=("story",))
            dialog._show_voice_confirmation(plan)
            first_item = dialog.voice_routes.item(0)
            original_role = first_item.data(Qt.ItemDataRole.UserRole)
            first_item.setData(Qt.ItemDataRole.UserRole, "Narrator")
            dialog.voice_routes.setCurrentRow(0)
            dialog._update_identity_actions()
            self.assertFalse(dialog.link_identity.isEnabled())
            first_item.setData(Qt.ItemDataRole.UserRole, original_role)
            dialog._update_identity_actions()
            for row in range(dialog.voice_routes.count()):
                item = dialog.voice_routes.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == "Aderyn":
                    dialog.voice_routes.setCurrentRow(row)
                    break
            with (
                patch(
                    "vntts.pregeneration_ui.QInputDialog.getItem",
                    return_value=("Rhiannon", True),
                ),
                patch(
                    "vntts.pregeneration_ui.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
            ):
                dialog.link_identity.click()

            self.assertEqual(dialog.voice_library.canonical_role("Aderyn"), "Rhiannon")
            self.assertTrue(dialog.planning_voices)
            self.assertEqual(len(pool.tasks), 1)

    def test_voice_plan_suggests_but_does_not_link_matching_names(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dialog, _pool, plan, rhiannon, _decisions, _store = (
                self._inspected_voice_dialog(root)
            )
            self.addCleanup(dialog.deleteLater)
            aderyn = replace(
                rhiannon,
                group_id="a" * 64,
                character="Aderyn",
                routing_role="Aderyn",
                line_ids=("line:aderyn",),
            )
            suggestion = PersonLinkSuggestion(
                left_role="Aderyn",
                right_role="Rhiannon",
                shared_portraits=("314612.png", "314623.png"),
                shared_source_banks=("activityvoc_story_hero3146.bnk",),
            )
            plan = replace(
                plan,
                groups=(aderyn, rhiannon),
                person_link_suggestions=(suggestion,),
            )
            dialog._voice_plan = plan
            dialog._job = Mock(
                job_id=plan.job_id,
                story_index_sha256=plan.story_index_sha256,
                selected_story_ids=(),
            )
            dialog._show_voice_confirmation(plan)
            dialog.show_all_voice_routes.setChecked(True)
            self.assertEqual(dialog.voice_routes.count(), 2)
            for row in range(dialog.voice_routes.count()):
                item = dialog.voice_routes.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == "Aderyn":
                    dialog.voice_routes.setCurrentRow(row)
                    break

            self.assertFalse(dialog.identity_suggestion.isHidden())
            self.assertIn("Rhiannon", dialog.identity_suggestion.text())
            self.assertIn("2 matching portraits", dialog.identity_suggestion.text())
            self.assertEqual(dialog.voice_library.canonical_role("Aderyn"), "Aderyn")
            with patch(
                "vntts.pregeneration_ui.QInputDialog.getItem",
                return_value=("", False),
            ) as choose:
                dialog.link_identity.click()
            self.assertEqual(choose.call_args.args[3][0], "Rhiannon")
            self.assertEqual(choose.call_args.args[4], 0)
            self.assertEqual(dialog.voice_library.canonical_role("Aderyn"), "Aderyn")

            second = replace(suggestion, right_role="Gwyndolyn")
            dialog._voice_plan = replace(
                plan, person_link_suggestions=(suggestion, second)
            )
            dialog._update_identity_actions()
            with patch(
                "vntts.pregeneration_ui.QInputDialog.getItem",
                return_value=("", False),
            ) as choose:
                dialog.link_identity.click()
            self.assertEqual(choose.call_args.args[3][0], "")

            dialog.voice_library.link_person("Rhiannon", "Gwyndolyn")
            dialog._update_identity_actions()
            with patch(
                "vntts.pregeneration_ui.QInputDialog.getItem",
                return_value=("", False),
            ) as choose:
                dialog.link_identity.click()
            self.assertEqual(choose.call_args.args[3].count("Rhiannon"), 1)
            self.assertEqual(choose.call_args.args[3][0], "Rhiannon")

            dialog._job.story_index_sha256 = "stale"
            dialog._update_identity_actions()
            self.assertTrue(dialog.identity_suggestion.isHidden())

    def test_voice_plan_hides_icons_when_portrait_coverage_is_sparse(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dialog, _pool, plan, group, _decisions, _store = (
                self._inspected_voice_dialog(root)
            )
            self.addCleanup(dialog.deleteLater)
            portrait = root / "portrait.png"
            self.assertTrue(QPixmap(2, 2).save(str(portrait)))
            group = replace(
                group,
                portrait_image=str(portrait),
                portrait_image_sha256=sha256_file(portrait),
            )
            plan = replace(plan, groups=(group,))
            dialog._show_voice_confirmation(plan)
            self.assertFalse(dialog.voice_routes.item(0).icon().isNull())

            other = replace(
                group,
                group_id="b" * 64,
                character="Other character",
                routing_role="Other character",
                portrait_image=None,
                portrait_image_sha256=None,
            )
            dialog._show_voice_confirmation(replace(plan, groups=(group, other)))
            self.assertTrue(
                all(
                    dialog.voice_routes.item(row).icon().isNull()
                    for row in range(dialog.voice_routes.count())
                )
            )

    def test_inspected_automatic_voice_can_be_saved_and_replanned(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dialog, pool, plan, group, decisions, voice_plan_store = (
                self._inspected_voice_dialog(root)
            )

            dialog.stories.item(0).setCheckState(Qt.CheckState.Checked)
            dialog.continue_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertFalse(dialog.auditioning_voices)
            self.assertTrue(dialog.preparing_inputs)

            pool.tasks.clear()
            dialog.preparing_inputs = False
            dialog._show_voice_confirmation(plan)
            dialog.show_all_voice_routes.setChecked(True)
            for row in range(dialog.voice_routes.count()):
                if (
                    dialog.voice_routes.item(row).data(Qt.ItemDataRole.UserRole)
                    == group.character
                ):
                    dialog.voice_routes.setCurrentRow(row)
                    break
            dialog.inspect_character_voice.click()
            self.assertTrue(dialog.auditioning_voices)
            self.assertTrue(dialog.inspecting_voice_plan)

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

"""Shared editor contract independent of the Voices/Stories entry point."""

import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QInputMethodEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.pregeneration_fixtures import (  # noqa: E402
    ambiguous_fixture,
    clean_wav_bytes,
)
from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.document_identity import sha256_file  # noqa: E402
from vntts.pregeneration_audition import VoiceAuditionReference  # noqa: E402
from vntts.pregeneration_audition_ui import VoiceAuditionPanel  # noqa: E402


class CharacterVoicePanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.plan, self.group, _ = ambiguous_fixture(Path(directory.name))
        self.pool = ManualThreadPool()
        self.service = Mock(supports_prefetch=False)
        self.decisions = Mock()
        self.panel = VoiceAuditionPanel(
            self.decisions,
            preview_service=self.service,
            player=Mock(),
            thread_pool=self.pool,
        )
        self.addCleanup(self.panel.deleteLater)
        self.addCleanup(self.panel.shutdown)

    def run_tasks(self):
        while self.pool.tasks:
            self.pool.run_next()
            self.app.processEvents()

    def configure_current(self):
        candidate = replace(self.group.candidates[0], source_id="saved:voice")
        group = replace(
            self.group,
            source_id=candidate.source_id,
            candidates=(candidate,),
            candidate_inventory=(candidate,),
            narrator_candidate=None,
        )
        plan = replace(self.plan, groups=(group,))
        self.panel.start(self.plan, group_id=self.group.group_id)
        self.panel.set_current_voice(plan, "Saved character voice")
        path = Path(self.plan.voice_manifest).parent / "saved-preview.wav"
        path.write_bytes(clean_wav_bytes())
        self.service.generate.return_value = Mock(
            path=path, audio_sha256=sha256_file(path)
        )
        return plan, group, candidate

    def test_current_preview_uses_saved_plan_and_shared_phrase_without_approving_candidate(
        self,
    ):
        plan, group, candidate = self.configure_current()
        selection = self.panel.current_selection
        self.panel.preview_text.setPlainText("One phrase for both voices.")
        self.panel.current_play.click()
        self.run_tasks()
        self.service.generate.assert_called_once_with(
            plan,
            group,
            candidate.source_id,
            text="One phrase for both voices.",
            cancel_event=self.service.generate.call_args.kwargs["cancel_event"],
        )
        self.assertEqual(self.panel.current_selection, selection)
        self.assertFalse(self.panel.a_use.isEnabled())
        self.assertIsNone(self.panel._accepted_choice)
        self.assertEqual(self.panel._auditioned, {})
        self.assertEqual(self.panel.current_play.text(), "Preview")
        self.assertEqual(self.panel.a_play.text(), "Preview")
        self.assertFalse(self.panel.current_play.icon().isNull())
        self.decisions.remember_many.assert_not_called()

    def test_explicit_playback_waits_for_host_before_generating_or_playing(self):
        for current_voice in (False, True):
            for original in (False, True):
                with self.subTest(current_voice=current_voice, original=original):
                    self.configure_current()
                    preview = self.service.generate.return_value
                    self.service.reference_audio.return_value = VoiceAuditionReference(
                        preview.path, preview.path.read_bytes()
                    )
                    continuations = []
                    self.panel.before_playback = continuations.append
                    self.service.reset_mock()
                    self.panel.player.reset_mock()
                    if original:
                        self.panel._play_original(current_voice=current_voice)
                    elif current_voice:
                        self.panel.play_current()
                    else:
                        self.panel.play_a()
                    self.run_tasks()
                    self.assertEqual(len(continuations), 1)
                    self.service.generate.assert_not_called()
                    self.service.reference_audio.assert_not_called()
                    self.panel.player.play_bytes.assert_not_called()
                    continuations[0]()
                    self.run_tasks()
                    self.panel.player.play_bytes.assert_called_once()
                    if original:
                        self.service.reference_audio.assert_called_once()
                    else:
                        self.service.generate.assert_called_once()
                    self.panel._stop_playback()

    def test_cached_previews_also_wait_for_host(self):
        self.configure_current()
        for current_voice in (False, True):
            with self.subTest(current_voice=current_voice):
                play = self.panel.play_current if current_voice else self.panel.play_a
                play()
                self.run_tasks()
                self.panel._stop_playback()
                continuations = []
                self.panel.before_playback = continuations.append
                self.service.reset_mock()
                self.panel.player.reset_mock()
                play()
                self.assertEqual(len(continuations), 1)
                self.panel.player.play_bytes.assert_not_called()
                continuations[0]()
                self.panel.player.play_bytes.assert_called_once()
                self.service.generate.assert_not_called()
                self.panel._stop_playback()
                self.panel.before_playback = None

    def test_pending_playback_is_invalidated_by_selection_change_or_cancel(self):
        for interruption in ("phrase", "current-voice", "cancel", "shutdown"):
            with self.subTest(interruption=interruption):
                self.configure_current()
                continuations = []
                self.panel.before_playback = continuations.append
                self.panel.play_a()
                if interruption == "phrase":
                    self.panel.preview_text.setPlainText("Changed comparison phrase")
                elif interruption == "current-voice":
                    self.panel.set_current_voice(None, "Unavailable")
                else:
                    getattr(self.panel, interruption)()
                self.service.reset_mock()
                self.panel.player.reset_mock()
                continuations[0]()
                self.run_tasks()
                self.service.generate.assert_not_called()
                self.panel.player.play_bytes.assert_not_called()

    def test_new_playback_request_supersedes_pending_request(self):
        self.configure_current()
        continuations = []
        self.panel.before_playback = continuations.append
        self.panel.play_a()
        self.panel.play_current()
        continuations[0]()
        self.run_tasks()
        self.service.generate.assert_not_called()
        continuations[1]()
        self.run_tasks()
        self.assertEqual(self.panel._playing_source, "current-preview")
        self.assertEqual(self.service.generate.call_count, 1)

    def test_playback_toggle_stops_immediately_without_host_gate(self):
        for current_voice in (False, True):
            for original in (False, True):
                with self.subTest(current_voice=current_voice, original=original):
                    self.configure_current()
                    preview = self.service.generate.return_value
                    self.service.reference_audio.return_value = VoiceAuditionReference(
                        preview.path, preview.path.read_bytes()
                    )
                    if original:
                        button = (
                            self.panel.current_original
                            if current_voice
                            else self.panel.a_original
                        )
                    else:
                        button = (
                            self.panel.current_play
                            if current_voice
                            else self.panel.a_play
                        )
                    self.panel.before_playback = None
                    button.click()
                    self.run_tasks()
                    gate = Mock()
                    self.panel.before_playback = gate
                    self.panel.player.stop.reset_mock()
                    button.click()
                    gate.assert_not_called()
                    self.panel.player.stop.assert_called_once()
                    self.assertIsNone(self.panel._playing_source)

    def test_current_audition_preserves_candidate_acceptance_and_uses_one_player(self):
        self.configure_current()
        self.panel.play_a()
        self.run_tasks()
        accepted = self.panel._accepted_choice
        heard = dict(self.panel._auditioned)
        self.panel.current_play.click()
        self.run_tasks()
        self.assertEqual(self.panel._accepted_choice, accepted)
        self.assertTrue(self.panel.a_use.isEnabled())
        self.assertEqual(self.panel._auditioned, heard)
        self.assertEqual(self.panel.player.play_bytes.call_count, 2)
        self.assertEqual(self.panel.a_play.text(), "Preview")
        self.panel.current_play.click()
        self.assertEqual(self.panel.current_play.text(), "Preview")
        self.assertGreaterEqual(self.panel.player.stop.call_count, 2)

    def test_switching_preview_direction_waits_for_cancelled_generation(self):
        for first_current in (False, True):
            with self.subTest(first_current=first_current):
                self.configure_current()
                first = self.panel.play_current if first_current else self.panel.play_a
                second = self.panel.play_a if first_current else self.panel.play_current
                first()
                request = self.panel._preview_request
                second()
                self.assertTrue(request.cancellation.is_set())
                self.assertEqual(len(self.pool.tasks), 1)
                self.pool.run_next()
                self.app.processEvents()
                self.assertEqual(len(self.pool.tasks), 1)
                self.assertEqual(
                    self.panel._preview_request.current_voice, not first_current
                )
                self.run_tasks()
                self.assertEqual(
                    self.panel._playing_source,
                    "preview" if first_current else "current-preview",
                )
                self.panel._stop_playback()
                self.panel._previews.clear()
                self.panel.player.reset_mock()

    def test_current_original_uses_saved_guard_and_cannot_approve_candidate(self):
        plan, group, candidate = self.configure_current()
        guard = Mock()
        self.panel.set_current_voice(plan, "Saved", playback_guard=guard)
        preview = self.service.generate.return_value
        self.service.reference_audio.return_value = VoiceAuditionReference(
            preview.path, preview.path.read_bytes()
        )
        self.panel.current_original.click()
        self.assertEqual(self.panel.current_original.text(), "Original")
        self.assertEqual(self.panel.a_original.text(), "Original")
        self.run_tasks()
        self.service.reference_audio.assert_called_once_with(
            plan, group, candidate.source_id
        )
        guard.assert_called_once_with()
        self.assertEqual(self.panel._playing_source, "current-original")
        self.assertFalse(self.panel.a_use.isEnabled())

    def test_current_original_late_completion_cannot_play_after_switch_cancel_or_shutdown(
        self,
    ):
        for action in ("candidate", "cancel", "shutdown", "phrase", "role"):
            with self.subTest(action=action):
                _plan, _group, _candidate = self.configure_current()
                preview = self.service.generate.return_value
                self.service.reference_audio.return_value = VoiceAuditionReference(
                    preview.path, preview.path.read_bytes()
                )
                self.panel.current_original.click()
                if action == "candidate":
                    self.panel.play_a()
                elif action == "phrase":
                    self.panel.preview_text.setPlainText(
                        "Changed while reading reference."
                    )
                elif action == "role":
                    self.panel.set_current_voice(
                        None, "Unavailable", error="No saved voice"
                    )
                else:
                    getattr(self.panel, action)()
                self.pool.run_next()
                self.app.processEvents()
                self.panel.player.play_bytes.assert_not_called()
                self.run_tasks()
                self.panel.player.reset_mock()

    def test_current_checksum_and_guard_failures_preserve_candidate_acceptance(self):
        plan, _group, _candidate = self.configure_current()
        self.panel.play_a()
        self.run_tasks()
        accepted = self.panel._accepted_choice
        guard = Mock(side_effect=ValueError("Saved voice changed"))
        self.panel.set_current_voice(plan, "Saved", playback_guard=guard)
        self.panel.play_current()
        self.run_tasks()
        self.assertIn("Saved voice changed", self.panel.current_status.text())
        self.assertEqual(self.panel._accepted_choice, accepted)
        self.assertTrue(self.panel.a_use.isEnabled())
        self.panel.set_current_voice(plan, "Saved")
        self.service.generate.return_value.audio_sha256 = "0" * 64
        self.panel.play_current()
        self.run_tasks()
        self.assertIn("changed after validation", self.panel.current_status.text())
        self.assertEqual(self.panel._accepted_choice, accepted)
        self.assertEqual(self.panel.player.play_bytes.call_count, 1)

    def test_shared_phrase_survives_candidate_restart_and_saved_error_is_visible(self):
        self.configure_current()
        self.panel.preview_text.setPlainText("Keep this comparison phrase.")
        self.panel.start(self.plan, group_id=self.group.group_id)
        self.assertEqual(
            self.panel.preview_text.toPlainText(), "Keep this comparison phrase."
        )
        self.panel.set_current_voice(
            None, "Saved voice", error="Reference is unavailable."
        )
        self.assertEqual(self.panel.current_reason.text(), "Reference is unavailable.")
        self.assertFalse(self.panel.current_voice_box.isHidden())
        self.assertFalse(self.panel.current_play.isEnabled())
        self.assertFalse(self.panel.current_original.isEnabled())

    def test_current_voice_can_preview_before_a_new_voice_is_available(self):
        candidate = self.group.candidates[0]
        group = replace(self.group, source_id=candidate.source_id)
        plan = replace(self.plan, groups=(group,))
        self.panel.set_current_voice(plan, "Current voice")
        self.assertEqual(self.panel.preview_text.toPlainText(), group.sample_text)
        self.assertTrue(self.panel.current_play.isEnabled())
        path = Path(self.plan.voice_manifest).parent / "current-preview.wav"
        path.write_bytes(clean_wav_bytes())
        self.service.generate.return_value = Mock(
            path=path, audio_sha256=sha256_file(path)
        )
        self.panel.preview_text.setPlainText("Current voice only.")
        self.panel.play_current()
        self.run_tasks()
        self.assertEqual(
            self.service.generate.call_args.kwargs["text"], "Current voice only."
        )
        self.assertEqual(self.panel._playing_source, "current-preview")
        self.assertIsNone(self.panel.current_selection)

    def test_policy_uses_same_save_and_persists_only_target_without_audio(self):
        group = replace(
            self.group, candidates=(), candidate_inventory=(), narrator_candidate=None
        )
        plan = replace(self.plan, groups=(group,))
        busy, completed = [], []
        self.panel.activeChanged.connect(busy.append)
        self.panel.completed.connect(lambda: completed.append(True))
        self.panel.start_policy(
            plan,
            group_id=group.group_id,
            choice_id="automatic",
            label="Use automatic matching",
        )
        self.assertEqual(self.panel.current_selection, (plan, group, "automatic"))
        self.assertFalse(self.panel.a_box.isVisibleTo(self.panel))
        self.assertTrue(self.panel.a_use.isEnabled())
        self.assertFalse(self.panel.a_use.autoDefault())
        snapshots = []
        self.panel.beforeSave.connect(
            lambda selections: snapshots.append(
                (selections, self.panel.decision_runner.active)
            )
        )
        self.panel.a_use.click()
        self.assertEqual(snapshots, [(((group, "automatic"),), False)])
        self.run_tasks()
        self.decisions.remember_many.assert_called_once_with(((group, "automatic"),))
        self.service.generate.assert_not_called()
        self.assertEqual(completed, [True])
        self.assertEqual(busy, [True, False])

    def test_policy_cancel_does_not_save_and_regular_reference_can_reopen(self):
        self.panel.start_policy(
            self.plan,
            group_id=self.group.group_id,
            choice_id="default",
            label="Use narrator",
        )
        self.panel.cancel()
        self.decisions.remember_many.assert_not_called()
        self.panel.start(self.plan, group_id=self.group.group_id)
        self.assertTrue(self.panel.a_box.isVisibleTo(self.panel))
        self.assertTrue(self.panel.reference_browser.isVisibleTo(self.panel))
        self.assertGreater(self.panel.reference_model.rowCount(), 0)
        self.assertTrue(self.panel.a_original.isEnabled())
        self.assertNotEqual(self.panel.current_selection[2], "default")

    def test_reference_browser_shows_single_reference_and_full_inventory(self):
        candidate = self.group.candidates[0]
        group = replace(
            self.group,
            candidates=(candidate,),
            candidate_inventory=(candidate,),
            narrator_candidate=None,
        )
        self.panel.start(replace(self.plan, groups=(group,)), group_id=group.group_id)
        self.assertFalse(self.panel.reference_browser.isHidden())
        self.assertEqual(self.panel.reference_model.rowCount(), 1)
        self.assertTrue(self.panel.a_original.isEnabled())
        self.assertTrue(self.panel.a_play.isEnabled())
        second = replace(candidate, source_id="character:second")
        group = replace(group, candidate_inventory=(candidate, second))
        self.panel.start(replace(self.plan, groups=(group,)), group_id=group.group_id)
        self.assertFalse(self.panel.reference_browser.isHidden())
        self.assertEqual(self.panel.reference_model.rowCount(), 2)
        self.assertTrue(self.panel.reference_table.isEnabled())

    def test_inspection_hides_repeated_target_scope_and_idle_instructions(self):
        self.panel.start(self.plan, group_id=self.group.group_id)
        self.assertTrue(self.panel.status.isHidden())
        self.assertEqual(self.panel.status.toPlainText(), "")
        self.assertTrue(self.panel.recommendation.isHidden())
        self.assertEqual(self.panel.a_box.title(), "")
        self.assertNotIn("Voice:", self.panel.a_reason.text())
        self.assertNotIn("Original spoken text:", self.panel.a_reason.text())
        for row in range(self.panel.reference_model.rowCount()):
            label = self.panel.reference_model.item(row, 0).text()
            self.assertNotIn("Not prepared", label)
            self.assertNotIn("Content not checked", label)
        self.panel._set_status("Unable to play original reference: file missing")
        self.assertFalse(self.panel.status.isHidden())
        self.panel.start(self.plan, group_id=self.group.group_id)
        self.assertTrue(self.panel.status.isHidden())
        self.panel.start(self.plan, group_id=self.plan.groups[0].group_id)

    def test_original_transcript_is_selectable_without_repeated_metadata(self):
        candidate = replace(
            self.group.candidates[0],
            source_excerpts=("The full original line.",),
            reference_transcript="The verified original line.",
        )
        group = replace(
            self.group, candidates=(candidate,), candidate_inventory=(candidate,)
        )
        self.panel.start(replace(self.plan, groups=(group,)), group_id=group.group_id)
        self.assertEqual(self.panel.a_reason.text(), "The verified original line.")
        self.panel.a_reason.setSelection(0, len(self.panel.a_reason.text()))
        self.assertEqual(self.panel.a_reason.selectedText(), self.panel.a_reason.text())

    def test_custom_text_invalidates_heard_choice_and_empty_text_cannot_generate(self):
        self.panel.start(self.plan, group_id=self.group.group_id)
        self.panel._accepted_choice = self.panel.current_selection[2]
        self.panel.preview_text.setPlainText("A new comparison phrase.")
        self.assertEqual(self.panel._sample_text, "A new comparison phrase.")
        self.assertIsNone(self.panel._accepted_choice)
        self.assertFalse(self.panel.a_use.isEnabled())
        self.panel.preview_text.clear()
        self.assertFalse(self.panel.a_play.isEnabled())
        self.panel.play_a()
        self.service.generate.assert_not_called()
        self.panel.game_phrase_button.click()
        self.assertTrue(self.panel.a_play.isEnabled())

    def test_failed_custom_preview_keeps_custom_text_for_retry(self):
        group = replace(self.group, alternate_sample_text="Another story phrase.")
        plan = replace(self.plan, groups=(group,))
        self.service.generate.side_effect = RuntimeError("model unavailable")
        self.panel.start(plan, group_id=group.group_id)
        self.assertGreater(len(self.panel._game_phrases), 1)
        text = "Only this custom phrase should be spoken."
        self.panel.preview_text.setPlainText(text)
        self.panel.a_play.click()
        self.run_tasks()
        self.assertIn("Preview failed", self.panel.status.toPlainText())
        self.assertTrue(self.panel.preview_text.isEnabled())
        self.assertEqual(self.panel.preview_text.toPlainText(), text)
        self.assertTrue(self.panel.a_play.isEnabled())
        self.assertEqual(self.panel._sample_text, text)
        self.panel.a_play.click()
        self.run_tasks()
        self.assertEqual(self.service.generate.call_count, 2)
        self.assertEqual(self.service.generate.call_args.kwargs["text"], text)
        self.decisions.remember_many.assert_not_called()

    def test_game_phrase_button_cycles_all_available_text_without_duplicates(self):
        phrases = (
            "The first full game phrase.",
            "Why would you do that?!",
            "Тихо, они могут нас услышать...",
            "Je suis désolée.",
            "One last line.",
        )
        candidate = replace(
            self.group.candidates[0],
            source_excerpts=(*phrases[2:], phrases[0], "  "),
            reference_transcript=phrases[1],
        )
        group = replace(
            self.group,
            sample_text=phrases[0],
            alternate_sample_text=phrases[1],
            candidates=(candidate,),
            candidate_inventory=(candidate,),
        )
        self.panel.start(replace(self.plan, groups=(group,)), group_id=group.group_id)
        self.assertEqual(self.panel._game_phrases, phrases)
        self.assertEqual(self.panel.preview_text.toPlainText(), phrases[0])
        for text in (*phrases[1:], phrases[0]):
            self.panel.game_phrase_button.click()
            self.assertEqual(self.panel.preview_text.toPlainText(), text)
            self.assertEqual(self.panel._sample_text, text)
        self.panel.preview_text.setPlainText("My edited phrase")
        self.panel.game_phrase_button.click()
        self.assertEqual(self.panel.preview_text.toPlainText(), phrases[0])
        self.service.generate.assert_not_called()
        self.decisions.remember_many.assert_not_called()

    def test_unicode_input_preserves_full_text_and_cursor(self):
        self.panel.start(self.plan, group_id=self.group.group_id)
        self.panel.preview_text.selectAll()
        text = "Не уходи! Je suis désolée."
        event = QInputMethodEvent()
        event.setCommitString(text)
        QCoreApplication.sendEvent(self.panel.preview_text, event)
        self.assertEqual(self.panel.preview_text.toPlainText(), text)
        self.assertEqual(self.panel._sample_text, text)
        self.assertEqual(self.panel.preview_text.textCursor().position(), len(text))
        self.assertTrue(self.panel.a_play.isEnabled())
        self.decisions.remember_many.assert_not_called()

    def test_missing_game_text_keeps_manual_editing_available(self):
        candidate = replace(
            self.group.candidates[0], source_excerpts=(), reference_transcript=None
        )
        group = replace(
            self.group,
            sample_text="",
            alternate_sample_text=None,
            candidates=(candidate,),
            candidate_inventory=(candidate,),
        )
        self.panel.start(
            replace(self.plan, synthesis_language="ru", groups=(group,)),
            group_id=group.group_id,
        )
        self.assertFalse(self.panel.game_phrase_button.isEnabled())
        self.assertTrue(self.panel.preview_text.isEnabled())
        self.assertEqual(self.panel.preview_text.toPlainText(), "")
        self.assertFalse(self.panel.a_play.isEnabled())
        self.panel.preview_text.setPlainText("Теперь я могу проверить голос.")
        self.assertTrue(self.panel.a_play.isEnabled())
        self.assertEqual(self.panel._sample_text, "Теперь я могу проверить голос.")

    def test_qwen_transcript_updates_selection_but_not_original_plan(self):
        plan = replace(self.plan, synthesis_backend="qwen-tts")
        self.panel.start(plan, group_id=self.group.group_id)
        old_candidate = self.panel._current_entry()[0]
        changes = []
        self.panel.selectionChanged.connect(lambda: changes.append(True))
        self.panel.reference_transcript.setText("Verified exact spoken words.")
        self.panel.reference_transcript.editingFinished.emit()
        selected_plan, selected_group, selected_id = self.panel.current_selection
        candidate = next(
            value
            for value in selected_group.candidate_inventory or selected_group.candidates
            if value.source_id == selected_id
        )
        self.assertEqual(candidate.reference_transcript, "Verified exact spoken words.")
        self.assertIsNot(selected_plan, plan)
        self.assertNotEqual(
            old_candidate.reference_transcript, candidate.reference_transcript
        )
        self.assertFalse(self.panel.a_use.isEnabled())
        self.assertTrue(changes)
        self.decisions.remember_many.assert_not_called()

"""The public role/source host delegates audio editing to the shared panel."""

import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid
from vntts_artifacts.file_integrity import sha256_file

from tests.pregeneration_fixtures import FakeBackend, ambiguous_fixture, clean_wav_bytes
from tests.qt_task_fixtures import ManualThreadPool
from tests.voice_manifest_fixtures import (
    write_manifest,
    write_player_candidate_manifest,
)
from vntts.game_narrator import (
    bind_voice_library_selection,
    load_original_reference,
    narrator_preview_plan,
)
from vntts.game_narrator_ui import GameNarratorDialog
from vntts.pregeneration_audition import VoiceAuditionPreviewService
from vntts.pregeneration_audition_ui import VoiceAuditionPanel
from vntts.preview_content_advisor import PreviewContentAdvisor
from vntts.runtime_config import initialize_voice_registry
from vntts.settings import AppSettings
from vntts.voice_library import VoiceLibrary
from vntts.voices import CharacterVoiceRegistry


class GameNarratorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = VoiceLibrary(self.root / "library")
        self.pool = ManualThreadPool()
        self.backend = FakeBackend("moss-tts")
        self.service = VoiceAuditionPreviewService(
            self.root / "previews",
            backend_factory=lambda *_args, **_kwargs: self.backend,
        )
        self.addCleanup(self.service.close)
        self.player = Mock()
        self.manifest = self.narrator_manifest(self.root / "voices")
        self.settings = AppSettings(
            speech_backend="moss-tts", voice_manifest=str(self.manifest)
        )
        self.importer = Mock()
        self.importer.selected_installation_root.return_value = None
        self.importer.narrator_characters.return_value = ()
        self.library_patch = patch(
            "vntts.game_narrator.application_voice_library", return_value=self.library
        )
        self.library_patch.start()
        self.addCleanup(self.library_patch.stop)
        advisor = Mock(spec=PreviewContentAdvisor)
        advisor.root = self.root / "advice"
        advisor.check.return_value = ()
        advisor.lookup_saved.return_value = None
        self.advisor_patch = patch(
            "vntts.pregeneration_audition_ui.PreviewContentAdvisor",
            return_value=advisor,
        )
        self.advisor_patch.start()
        self.addCleanup(self.advisor_patch.stop)

    def drain(self):
        for _ in range(100):
            self.application.processEvents()
            if not self.pool.tasks:
                return
            self.pool.run_next()
        self.fail("Voice editor did not become idle")

    def editor(
        self, *, settings=None, rollback=None, binder=bind_voice_library_selection
    ):
        self.backend.name = (settings or self.settings).speech_backend
        dialog = GameNarratorDialog(
            settings or self.settings,
            importer=self.importer,
            preview_service=self.service,
            thread_pool=self.pool,
            player=self.player,
            voice_library=self.library,
            voice_binding_rollback=rollback,
            use_offline_engine=False,
            binder=binder,
        )
        self.addCleanup(self.close_editor, dialog)
        self.application.processEvents()
        self.drain()
        return dialog

    def close_editor(self, dialog):
        dialog.reject()
        self.drain()
        self.assertFalse(dialog.active)
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(dialog))

    def select(self, dialog, mode, *, role="Narrator", source=None):
        dialog.select_role(role)
        dialog.source.setCurrentIndex(dialog.source.findData(mode))
        if source is not None:
            combo = dialog.presets if mode == "preset" else dialog.catalog_choice
            combo.setCurrentIndex(combo.findData(source))
        self.drain()

    def listen(self, dialog, *, original=False):
        (
            dialog.voice_panel.a_original if original else dialog.voice_panel.a_play
        ).click()
        self.drain()
        self.assertTrue(
            dialog.voice_panel.a_use.isEnabled(),
            dialog.voice_panel.status.toPlainText(),
        )

    def save(self, dialog):
        dialog.voice_panel.a_use.click()
        self.drain()
        self.assertEqual(
            dialog.result(),
            QDialog.DialogCode.Accepted,
            dialog.voice_panel.status.toPlainText(),
        )

    @staticmethod
    def narrator_manifest(root):
        manifest = write_manifest(root, rhiannon=clean_wav_bytes())
        (root / "references" / "centurion.wav").write_bytes(
            clean_wav_bytes(amplitude=0.2)
        )
        return manifest

    def test_host_contains_one_audio_editor_and_no_duplicate_reference_widgets(self):
        dialog = self.editor()
        self.assertEqual(dialog.findChildren(VoiceAuditionPanel), [dialog.voice_panel])
        for old_widget in (
            "references",
            "original_button",
            "preview_button",
            "text",
            "save_button",
            "exact_transcript",
        ):
            self.assertFalse(hasattr(dialog, old_widget), old_widget)

    def test_footer_transfer_retires_original_layout_items(self):
        build_actions = GameNarratorDialog._build_editor_actions
        valid_after_transfer = []

        def capture_items(dialog, content):
            panel = dialog.voice_panel
            old_items = tuple(
                panel.outcome_actions.itemAt(panel.outcome_actions.indexOf(widget))
                for widget in (panel.retry_save_button, panel.a_use)
            )
            self.assertTrue(all(item is not None for item in old_items))
            build_actions(dialog, content)
            valid_after_transfer.extend(isValid(item) for item in old_items)

        with patch.object(GameNarratorDialog, "_build_editor_actions", capture_items):
            self.editor()

        self.assertEqual(valid_after_transfer, [False, False])

    def test_saved_label_stays_separate_from_story_plan_and_current_audition(self):
        plan, group, _manifest = ambiguous_fixture(self.root / "saved-story")
        self.library.select(group.character, route="voice", source_id="preset:alba")
        dialog = self.editor(
            settings=self.settings.updated(speech_backend="pocket-tts")
        )
        dialog.set_voice_context(
            plan, character=group.character, story_titles=("Story one", "Story two")
        )
        self.drain()
        self.assertEqual(dialog.role_summary.text(), "Saved: Alba")
        self.assertIn("Story one", dialog.role_summary.toolTip())
        self.assertNotIn("Story one", dialog.role_summary.text())
        self.select(dialog, "preset", role=group.character, source="preset:marius")
        self.assertEqual(dialog.role_summary.text(), "Saved: Alba")
        self.assertEqual(self.library.binding(group.character).source_id, "preset:alba")
        self.assertTrue(dialog.status.isHidden())

    def test_current_audition_preserves_candidate_and_does_not_approve_it(self):
        self.bind_narrator()
        before = self.library.bindings()
        dialog = self.editor()
        self.select(dialog, "catalog", source="character:centurion")
        selected = dialog.voice_panel.current_selection
        self.assertFalse(dialog.voice_panel.a_use.isEnabled())
        dialog.voice_panel.current_original.click()
        self.drain()
        self.assertEqual(
            self.player.play_bytes.call_args.args[0],
            (self.manifest.parent / "references" / "rhiannon.wav").read_bytes(),
        )
        self.assertEqual(dialog.voice_panel.current_selection, selected)
        self.assertFalse(dialog.voice_panel.a_use.isEnabled())
        self.assertEqual(self.library.bindings(), before)

    def test_current_audition_rejects_binding_change_during_generation(self):
        self.bind_narrator()
        dialog = self.editor()
        self.select(dialog, "catalog", source="character:centurion")
        dialog.voice_panel.current_play.click()
        self.library.select("Narrator", route="voice", source_id="preset:alba")
        self.drain()
        self.player.play_bytes.assert_not_called()
        self.assertIn("Current voice changed", dialog.voice_panel.current_status.text())
        self.assertFalse(dialog.voice_panel.a_use.isEnabled())

    def test_current_narrator_fallback_uses_saved_bytes_without_assignment(self):
        self.bind_narrator()
        self.library.select("Bob", route="narrator")
        before = self.library.bindings()
        dialog = self.editor()
        self.select(dialog, "catalog", role="Bob", source="character:centurion")
        self.assertIn("Narrator", dialog.voice_panel.current_voice_label.text())
        dialog.voice_panel.current_original.click()
        self.drain()
        self.assertEqual(
            self.player.play_bytes.call_args.args[0],
            (self.manifest.parent / "references" / "rhiannon.wav").read_bytes(),
        )
        self.assertEqual(self.library.bindings(), before)

    def test_current_original_unavailable_does_not_block_new_voice(self):
        self.bind_narrator()
        for reference in self.library.resolve_source_paths("Narrator"):
            reference.unlink()
        dialog = self.editor()
        self.select(dialog, "catalog", source="character:centurion")
        self.assertFalse(dialog.voice_panel.current_original.isEnabled())
        self.assertFalse(dialog.voice_panel.current_voice_box.isHidden())
        self.assertTrue(dialog.voice_panel.current_reason.text())
        self.listen(dialog, original=True)

    def test_approved_layout_has_shared_phrase_and_two_sections(self):
        dialog = self.editor()
        self.assertEqual(dialog.new_voice_box.title(), "New voice")
        self.assertEqual(dialog.voice_panel.current_voice_box.title(), "Current voice")
        self.assertIs(dialog.voice_panel.phrase_box.parentWidget(), dialog.controls)
        self.assertIs(
            dialog.voice_panel.current_voice_box.parentWidget(), dialog.controls
        )
        self.assertIs(dialog.voice_panel.parentWidget(), dialog.new_voice_box)
        self.assertEqual(dialog.voice_panel.a_use.text(), "Save")
        phrase = "A shared comparison phrase."
        dialog.voice_panel.preview_text.setPlainText(phrase)
        self.select(dialog, "catalog", source="character:centurion")
        self.assertEqual(dialog.voice_panel.preview_text.toPlainText(), phrase)

    def test_saved_game_voice_label_uses_library_without_the_catalog(self):
        self.bind_narrator()
        dialog = self.editor()
        dialog._catalog_registry = CharacterVoiceRegistry()
        dialog._role_changed()
        self.drain()
        self.assertEqual(dialog.role_summary.text(), "Saved: Rhiannon")
        self.assertEqual(dialog.saved_assignment_summary(), "Narrator: Rhiannon")

    def test_saved_reference_missing_from_catalog_opens_without_a_new_donor(self):
        for role in ("Narrator", "Aderyn"):
            bind_voice_library_selection(
                self.settings,
                self.manifest,
                "character:rhiannon",
                "Rhiannon",
                root=self.library.root,
                target_character=role,
            )
        expected = self.library.binding("Narrator").source_sha256s
        document = json.loads(self.manifest.read_text())
        document["voices"] = [
            voice for voice in document["voices"] if voice["character"] != "Rhiannon"
        ]
        self.manifest.write_text(json.dumps(document))
        self.importer.narrator_characters.return_value = ("Unrelated",)
        before = self.library.bindings()
        dialog = self.editor()
        for role in ("Narrator", "Aderyn"):
            with self.subTest(role=role):
                dialog.select_role(role)
                self.drain()
                self.assertEqual(dialog.source.currentData(), "saved")
                self.assertEqual(dialog.role_summary.text(), "Saved: Rhiannon")
                self.assertFalse(dialog.voice_panel.isHidden())
                self.assertEqual(
                    dialog.voice_panel.current_group().candidates[0].reference_sha256s,
                    expected,
                )
                self.assertFalse(dialog._settings_dirty)
                self.assertTrue(dialog.status.isHidden(), dialog.status.text())
        self.importer.narrator_characters.assert_not_called()
        self.importer.prepare_voice_roles.assert_not_called()
        self.assertEqual(self.library.bindings(), before)

    def test_generated_catalog_donors_are_not_assignment_targets(self):
        document = json.loads(self.manifest.read_text())
        donor = dict(document["voices"][0])
        donor["character"] = "Player candidate Centurion bd3540bbf881"
        donor["aliases"] = []
        document["voices"].append(donor)
        self.manifest.write_text(json.dumps(document))
        dialog = self.editor()
        targets = [dialog.role.itemText(index) for index in range(dialog.role.count())]
        sources = [
            dialog.catalog_choice.itemText(index)
            for index in range(dialog.catalog_choice.count())
        ]
        self.assertNotIn(donor["character"], targets)
        self.assertIn("Centurion", sources)
        self.assertIn("Rhiannon", targets)

    def test_missing_saved_reference_shows_an_error_without_selecting_a_donor(self):
        self.bind_narrator()
        for reference in self.library.resolve_source_paths("Narrator"):
            reference.unlink()
        document = json.loads(self.manifest.read_text())
        document["voices"] = []
        self.manifest.write_text(json.dumps(document))
        dialog = self.editor()
        self.assertEqual(dialog.source.currentData(), "saved")
        self.assertFalse(dialog.status.isHidden())
        self.assertIn("Nothing was assigned", dialog.status.text())
        self.assertTrue(dialog.voice_panel.isHidden())
        self.importer.narrator_characters.assert_not_called()

    def test_host_status_only_shows_progress_and_actionable_messages(self):
        dialog = self.editor(
            settings=self.settings.updated(speech_backend="pocket-tts")
        )
        self.select(dialog, "preset", source="preset:alba")
        self.assertTrue(dialog.status.isHidden())
        dialog.discover()
        self.assertFalse(dialog.status.isHidden())
        self.assertIn("Finding", dialog.status.text())
        self.drain()
        self.assertFalse(dialog.status.isHidden())
        self.assertIn("No voiced characters", dialog.status.text())
        self.select(dialog, "preset", source="preset:marius")
        self.assertTrue(dialog.status.isHidden())

    def test_builtin_save_uses_one_target_without_game_or_cloning_access(self):
        dialog = self.editor(settings=AppSettings(speech_backend="pocket-tts"))
        self.select(dialog, "preset", role="Aderyn", source="preset:marius")
        self.listen(dialog)
        self.save(dialog)
        self.assertEqual(self.library.binding("Aderyn").source_id, "preset:marius")
        self.assertIsNone(self.library.binding("Narrator"))
        self.assertFalse(dialog.result_settings.pocket_gated_model_accepted)
        self.importer.prepare_voice_roles.assert_not_called()

    def test_clearing_qwen_transcript_persists_voice_only_binding(self):
        document = json.loads(self.manifest.read_text())
        for voice in document["voices"]:
            voice["vntts.reference_transcript"] = "Existing reference words."
        self.manifest.write_text(json.dumps(document))
        settings = self.settings.updated(speech_backend="qwen-tts")
        dialog = self.editor(settings=settings)
        self.select(dialog, "catalog", role="Aderyn", source="character:rhiannon")
        panel = dialog.voice_panel
        self.assertEqual(panel.reference_transcript.text(), "Existing reference words.")
        panel.reference_transcript.clear()
        panel.reference_transcript.editingFinished.emit()
        self.assertEqual(
            panel.current_selection[1].candidates[0].reference_transcript, ""
        )
        self.listen(dialog, original=True)
        self.save(dialog)
        binding = self.library.binding("Aderyn")
        self.assertEqual(binding.provenance["evidence"]["reference_transcript"], "")
        saved_voice = initialize_voice_registry(
            dialog.result_settings, voice_library=self.library
        ).resolve("Aderyn")
        self.assertIsNone(saved_voice.reference_transcript)
        self.assertIsNone(self.library.binding("Narrator"))

    def test_imported_donor_saves_target_and_keeps_narrator(self):
        self.library.select("Narrator", route="voice", source_id="preset:alba")
        dialog = self.editor()
        self.select(dialog, "catalog", role="Aderyn", source="character:rhiannon")
        self.listen(dialog, original=True)
        self.save(dialog)
        binding = self.library.binding("Aderyn")
        self.assertEqual(len(binding.source_sha256s), 1)
        self.assertEqual(self.library.binding("Narrator").source_id, "preset:alba")
        self.assertEqual(
            initialize_voice_registry(self.settings, voice_library=self.library)
            .resolve("Aderyn")
            .source_character,
            "Rhiannon",
        )
        self.assertEqual(self.backend.requests, [])

    def test_automatic_and_narrator_policy_use_shared_save_without_audio(self):
        for mode in ("automatic", "narrator"):
            with self.subTest(mode=mode):
                self.library.select("Aderyn", route="voice", source_id="preset:marius")
                dialog = self.editor()
                self.select(dialog, mode, role="Aderyn")
                self.save(dialog)
                binding = self.library.binding("Aderyn")
                self.assertIsNone(binding) if mode == "automatic" else self.assertEqual(
                    binding.route, "narrator"
                )
                self.assertEqual(self.backend.requests, [])
                # Each editor receives a separate service ownership lifetime.
                self.service = VoiceAuditionPreviewService(
                    self.root / f"previews-{mode}"
                )
                self.addCleanup(self.service.close)

    def test_browsing_and_cancel_leave_saved_binding_unchanged(self):
        self.library.select("Aderyn", route="voice", source_id="preset:marius")
        before = self.library.binding("Aderyn")
        dialog = self.editor()
        self.select(dialog, "catalog", role="Aderyn", source="character:rhiannon")
        self.listen(dialog, original=True)
        dialog.reject()
        self.drain()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(self.library.binding("Aderyn"), before)

    def test_external_transaction_can_rollback_accepted_nested_save(self):
        self.library.select("Aderyn", route="voice", source_id="preset:marius")
        rollback = self.library.binding_rollback()
        dialog = self.editor(rollback=rollback)
        self.select(dialog, "catalog", role="Aderyn", source="character:rhiannon")
        self.listen(dialog, original=True)
        self.save(dialog)
        dialog.restore_initial_voice_bindings()
        self.assertEqual(self.library.binding("Aderyn").source_id, "preset:marius")

    def test_library_write_failure_keeps_choice_available_for_retry(self):
        binder = Mock(side_effect=[ValueError("disk full"), self.settings])
        dialog = self.editor(binder=binder)
        self.select(dialog, "catalog", role="Aderyn", source="character:rhiannon")
        self.listen(dialog, original=True)
        dialog.voice_panel.a_use.click()
        self.drain()
        self.assertIn("disk full", dialog.voice_panel.status.toPlainText())
        self.assertFalse(dialog._closed)
        dialog.voice_panel.retry_save_button.click()
        self.drain()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(binder.call_args.kwargs["target_character"], "Aderyn")

    def test_cancel_discards_pending_original_and_emits_aggregate_busy(self):
        dialog = self.editor()
        self.select(dialog, "catalog", source="character:rhiannon")
        changes = []
        dialog.busyChanged.connect(changes.append)
        dialog.voice_panel.a_original.click()
        self.assertTrue(dialog.active)
        dialog.reject()
        self.drain()
        self.assertTrue(dialog._closed)
        self.assertEqual(changes, [True, False])
        self.player.play_bytes.assert_not_called()

    def test_cancel_during_save_finishes_exact_selected_target(self):
        dialog = self.editor()
        self.select(dialog, "catalog", role="Aderyn", source="character:rhiannon")
        self.listen(dialog, original=True)
        dialog.voice_panel.a_use.click()
        self.assertTrue(dialog.active)
        dialog.reject()
        self.assertFalse(dialog._closed)
        self.drain()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertIsNotNone(self.library.binding("Aderyn"))
        self.assertIsNone(self.library.binding("Narrator"))

    def test_changed_reference_cannot_be_saved_after_listening(self):
        dialog = self.editor()
        self.select(dialog, "catalog", role="Aderyn", source="character:rhiannon")
        self.listen(dialog, original=True)
        (self.manifest.parent / "references" / "rhiannon.wav").write_bytes(
            clean_wav_bytes(amplitude=0.31)
        )
        dialog.voice_panel.a_use.click()
        self.drain()
        self.assertIn("reference changed", dialog.voice_panel.status.toPlainText())
        self.assertIsNone(self.library.binding("Aderyn"))

    def test_custom_preview_and_qwen_transcript_belong_to_shared_panel(self):
        dialog = self.editor(settings=self.settings.updated(speech_backend="qwen-tts"))
        self.select(dialog, "catalog", source="character:rhiannon")
        panel = dialog.voice_panel
        self.assertTrue(hasattr(panel, "preview_text"))
        self.assertTrue(hasattr(panel, "reference_transcript"))
        self.assertFalse(hasattr(dialog, "exact_transcript"))

    def test_missing_manifest_cannot_be_saved_after_listening(self):
        dialog = self.editor()
        self.select(dialog, "catalog", role="Aderyn", source="character:rhiannon")
        self.listen(dialog, original=True)
        dialog._candidate_manifest = None

        dialog.voice_panel.a_use.click()
        self.drain()

        self.assertIn("checked voice manifest", dialog.voice_panel.status.toPlainText())
        self.assertIsNone(self.library.binding("Aderyn"))
        self.assertIsNone(dialog.result_settings)

    def test_inherited_variant_without_optional_metadata_keeps_defaults(self):
        dialog = self.editor()
        dialog._character = "Rhiannon"
        registry = CharacterVoiceRegistry.from_file(self.manifest)
        variants = ({"character": "Rhiannon", "voice_character": "rhiannon"},)
        with patch(
            "vntts.game_narrator_ui.validated_player_voice_candidates",
            return_value=(registry, variants),
        ):
            dialog._show_character_candidates(self.manifest)
        self.drain()

        candidate = dialog.voice_panel.current_group().candidate_inventory[0]
        self.assertIsNone(candidate.reference_duration_seconds)
        self.assertIsNone(candidate.source_bank)
        self.assertIsNone(candidate.candidate_origin)
        self.assertEqual(candidate.source_line_ids, ())
        self.assertEqual(candidate.source_voice_ids, ())
        self.assertEqual(candidate.source_excerpts, ())

    def test_story_context_reuses_all_candidates_without_import(self):
        plan, group, _manifest = ambiguous_fixture(self.root / "story")
        dialog = self.editor()
        self.importer.reset_mock()
        dialog.set_voice_context(plan, character=group.character)
        self.drain()
        self.assertEqual(dialog.voice_panel.current_group(), group)
        self.assertEqual(dialog.voice_panel.current_selection[0], plan)
        self.importer.prepare_voice_roles.assert_not_called()
        self.assertEqual(dialog._candidate_manifest, plan.voice_manifest)

    def test_existing_binding_story_hydration_does_not_start_fresh_import(self):
        plan, group, _manifest = ambiguous_fixture(self.root / "bound-story")
        bind_voice_library_selection(
            self.settings,
            plan.voice_manifest,
            group.candidates[0].source_id,
            group.character,
            root=self.library.root,
            target_character=group.character,
        )
        dialog = GameNarratorDialog(
            self.settings,
            importer=self.importer,
            preview_service=self.service,
            thread_pool=self.pool,
            player=self.player,
            voice_library=self.library,
            use_offline_engine=False,
        )
        self.addCleanup(self.close_editor, dialog)
        dialog.set_voice_context(plan, character=group.character)
        self.drain()
        self.assertEqual(dialog.source.currentData(), "game")
        self.assertEqual(dialog.voice_panel.current_group(), group)
        self.assertFalse(dialog.voice_panel.isHidden())
        self.importer.prepare_voice_roles.assert_not_called()

    def test_consent_change_keeps_the_selected_game_donor(self):
        plan, group, _manifest = ambiguous_fixture(self.root / "story-donor")
        dialog = self.editor()
        dialog.set_voice_context(plan, character=group.character)
        self.drain()
        with patch.object(dialog, "_prepare") as prepare:
            dialog.characters.addItem("Another donor")
            dialog.characters.setCurrentText("Another donor")
            prepare.reset_mock()
            dialog.consent.setChecked(not dialog.consent.isChecked())
            prepare.assert_called_once_with()
            self.assertEqual(dialog.characters.currentText(), "Another donor")

    def test_empty_story_candidates_keep_source_choices_available(self):
        plan, group, _manifest = ambiguous_fixture(self.root / "story")
        empty = replace(
            group, candidates=(), candidate_inventory=(), narrator_candidate=None
        )
        dialog = self.editor()
        dialog.set_voice_context(
            replace(plan, groups=(empty,)), character=group.character
        )
        self.drain()
        self.assertTrue(dialog.voice_panel.isHidden())
        self.assertTrue(dialog.source.isEnabled())

    def test_discovery_uses_donor_speaker_identity_and_cancellation(self):
        manifest = write_player_candidate_manifest(
            self.root / "game", "a" * 64, admission_policy="audio-only"
        )
        document = json.loads(manifest.read_text())
        for voice in document["voices"]:
            if voice.get("references"):
                (manifest.parent / voice["references"][0]).write_bytes(
                    clean_wav_bytes()
                )
        # Keep evidence checksums tied to the actual usable bytes.
        for variant in document["vntts.player.voice_candidates"]["variants"]:
            source = next(
                voice
                for voice in document["voices"]
                if voice["character"] == variant["voice_character"]
            )
            variant["reference_sha256"] = sha256_file(
                manifest.parent / source["references"][0]
            )
        manifest.write_text(json.dumps(document))
        self.importer.narrator_characters.return_value = ("Rhiannon",)
        self.importer.prepare_voice_roles.return_value = manifest
        dialog = self.editor()
        self.select(dialog, "game")
        group = dialog.voice_panel.current_group()
        candidate = group.candidate_inventory[0]
        registry = CharacterVoiceRegistry.from_file(manifest)
        voice = registry.resolve_source(candidate.source_id)
        self.assertEqual(candidate.source_speaker, voice.speaker)
        self.assertEqual(
            candidate.source_character, voice.source_character or voice.character
        )

    def test_recovery_context_and_quoted_role_identity_are_preserved(self):
        dialog = self.editor()
        dialog.set_voice_context(roles=('"Aderyn"', "Aderyn"), character="Aderyn")
        dialog.set_recovery_context("Aderyn", resume_live=True)
        self.assertEqual(
            sum(
                dialog.role.itemText(index) == "Aderyn"
                for index in range(dialog.role.count())
            ),
            1,
        )
        self.assertIn("paused", dialog.context_note.text())
        self.assertEqual(dialog.cancel_button.text(), "Back to recovery choices")

    def new_service(self):
        self.service = VoiceAuditionPreviewService(
            self.root / "previews",
            backend_factory=lambda *_args, **_kwargs: self.backend,
        )
        self.addCleanup(self.service.close)

    def bind_narrator(self):
        return bind_voice_library_selection(
            self.settings,
            self.manifest,
            "character:rhiannon",
            "Rhiannon",
            root=self.library.root,
        )

    def test_narrator_uses_library_bytes_when_original_reference_was_removed(self):
        self.bind_narrator()
        expected = (self.manifest.parent / "references" / "rhiannon.wav").read_bytes()
        (self.manifest.parent / "references" / "rhiannon.wav").unlink()
        dialog = self.editor()
        self.select(dialog, "narrator", role="Aderyn")
        self.listen(dialog, original=True)
        self.assertEqual(self.player.play_bytes.call_args.args[0], expected)
        self.assertIsNone(self.library.binding("Aderyn"))
        self.save(dialog)
        self.assertEqual(self.library.binding("Aderyn").route, "narrator")

    def test_narrator_binding_changed_during_render_is_not_played(self):
        self.bind_narrator()
        dialog = self.editor()
        self.select(dialog, "narrator", role="Aderyn")
        dialog.voice_panel.a_play.click()
        self.library.select("Narrator", route="voice", source_id="preset:alba")
        self.drain()
        self.player.play_bytes.assert_not_called()
        self.assertIn("Narrator voice changed", dialog.voice_panel.status.toPlainText())
        self.assertFalse(dialog.voice_panel.a_use.isEnabled())

    def test_narrator_library_reference_changed_during_render_is_not_played(self):
        self.bind_narrator()
        dialog = self.editor()
        self.select(dialog, "narrator", role="Aderyn")
        dialog.voice_panel.a_play.click()
        reference = self.library.resolve_source_paths("Narrator")[0]
        reference.write_bytes(clean_wav_bytes(amplitude=0.37))
        self.drain()
        self.player.play_bytes.assert_not_called()
        self.assertFalse(dialog.voice_panel.a_use.isEnabled())

    def test_pocket_saved_narrator_requires_consent_for_audio_and_preserves_binding(
        self,
    ):
        self.bind_narrator()
        before = self.library.binding("Narrator")
        dialog = self.editor(
            settings=self.settings.updated(
                speech_backend="pocket-tts", pocket_gated_model_accepted=False
            )
        )
        self.select(dialog, "narrator", role="Aderyn")
        self.assertIn("terms", dialog.status.text())
        self.assertTrue(dialog.voice_panel.preview_box.isHidden())
        self.assertEqual(self.library.binding("Narrator"), before)
        self.assertEqual(self.backend.requests, [])

    def test_unaccepted_xtts_and_unsupported_builtin_show_engine_guidance(self):
        dialog = self.editor(
            settings=self.settings.updated(
                speech_backend="coqui-xtts", xtts_terms_accepted=False
            )
        )
        self.select(dialog, "catalog", source="character:rhiannon")
        self.assertIn("XTTS", dialog.status.text())
        self.assertFalse(dialog.voice_panel.a_play.isEnabled())
        self.select(dialog, "preset")
        self.assertIn("Pocket", dialog.status.text())
        self.assertEqual(self.backend.requests, [])

    def test_shared_outer_rollback_covers_multiple_accepted_editor_saves(self):
        self.library.select("Aderyn", route="voice", source_id="preset:marius")
        self.library.select("Narrator", route="voice", source_id="preset:alba")
        rollback = self.library.binding_rollback()
        first = self.editor(rollback=rollback)
        self.select(first, "catalog", role="Aderyn", source="character:rhiannon")
        self.listen(first, original=True)
        self.save(first)
        self.new_service()
        second = self.editor(rollback=rollback)
        self.select(second, "catalog", source="character:centurion")
        self.listen(second, original=True)
        self.save(second)
        second.restore_initial_voice_bindings()
        self.assertEqual(self.library.binding("Aderyn").source_id, "preset:marius")
        self.assertEqual(self.library.binding("Narrator").source_id, "preset:alba")

    def test_cancel_discards_late_discovery_and_preserves_current_bindings(self):
        dialog = self.editor()
        dialog.discover(Path("/temporary-new-game-folder"))
        cancel = dialog.cancellation
        dialog.reject()
        self.assertTrue(cancel.is_set())
        self.assertFalse(dialog._closed)
        self.drain()
        self.assertTrue(dialog._closed)
        self.assertEqual(self.library.bindings(), ())

    def test_offline_engine_selection_is_scoped_to_the_editor(self):
        settings = self.settings.updated(
            tts_model="live-model",
            offline_speech_backend="qwen-tts",
            offline_tts_model="offline-model",
        )
        dialog = GameNarratorDialog(
            settings,
            importer=self.importer,
            preview_service=self.service,
            thread_pool=self.pool,
            player=self.player,
            voice_library=self.library,
        )
        self.addCleanup(self.close_editor, dialog)
        self.assertEqual(dialog.settings_value.speech_backend, "qwen-tts")
        self.assertIsNone(dialog.settings_value.tts_model)
        self.assertEqual(dialog.settings_value.tts_profile, "stable")
        self.assertEqual(settings.speech_backend, "moss-tts")
        self.assertEqual(settings.tts_model, "live-model")

    def test_imported_alias_preview_identity_uses_authoritative_source_character(self):
        document = json.loads(self.manifest.read_text())
        document["voices"][0]["vntts.source_character"] = "Original donor"
        self.manifest.write_text(json.dumps(document))
        plan = narrator_preview_plan(
            self.settings, self.manifest, "character:rhiannon", "Hello."
        )
        candidate = plan.groups[0].candidates[0]
        self.assertEqual(candidate.source_character, "Original donor")
        self.service.reference_audio(plan, plan.groups[0], candidate.source_id)

    def test_voice_picker_saves_one_library_binding_without_a_manifest_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root / "voices")
            library = VoiceLibrary(root / "library")

            saved = bind_voice_library_selection(
                AppSettings(voice_manifest=str(manifest)),
                manifest,
                "character:centurion",
                "Centurion",
                root=library.root,
            )

            self.assertEqual(saved.voice_manifest, str(manifest))
            self.assertEqual(len(library.resolve_source_paths("Narrator")), 1)
            registry = initialize_voice_registry(saved, voice_library=library)
            self.assertEqual(registry.resolve("Narrator").source_character, "Centurion")

    def test_original_snapshot_keeps_inspected_bytes_if_file_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root)
            with patch("vntts.support.record_game_import") as record:
                original = load_original_reference(manifest, "character:centurion")
            self.assertEqual(record.call_args.args, ("original-reference",))
            self.assertEqual(record.call_args.kwargs["duration_seconds"], 1.2)
            self.assertEqual(
                record.call_args.kwargs["reference_bytes"], len(original.payload)
            )
            self.assertEqual(
                record.call_args.kwargs["reference_sha256"], original.sha256
            )
            expected = original.path.read_bytes()
            original.path.write_bytes(clean_wav_bytes(seconds=0.06))
            self.assertEqual(original.payload, expected)
            self.assertNotEqual(original.sha256, sha256_file(original.path))
            self.assertEqual(original.duration_seconds, 1.2)
            self.assertEqual(original.rejection_reasons, ())

    def test_preview_checksum_disappearance_uses_value_error_boundary(self):
        with TemporaryDirectory() as directory:
            manifest = self.narrator_manifest(Path(directory))
            original_loader = CharacterVoiceRegistry.from_file

            def load_then_disappear(path):
                registry = original_loader(path)
                manifest.unlink()
                return registry

            with patch(
                "vntts.game_narrator.CharacterVoiceRegistry.from_file",
                side_effect=load_then_disappear,
            ):
                with self.assertRaises(ValueError) as raised:
                    narrator_preview_plan(
                        AppSettings(), manifest, "character:centurion", "Line."
                    )
            self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)

    def test_preview_plan_binds_exact_reference_and_engine(self):
        with TemporaryDirectory() as directory:
            manifest = write_manifest(Path(directory))
            settings = AppSettings(speech_backend="moss-tts", tts_profile="natural")
            plan = narrator_preview_plan(
                settings, manifest, "character:centurion", "Hello."
            )
            self.assertEqual(plan.synthesis_backend, "moss-tts")
            self.assertEqual(plan.synthesis_profile, "natural")
            self.assertEqual(plan.groups[0].candidates[0].source_character, "Centurion")

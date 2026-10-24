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
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QLabel,
    QScrollArea,
)
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

    def test_voice_replacement_keeps_current_above_settings_and_candidate(self):
        dialog = self.editor()
        self.assertEqual(dialog.voice_panel.title(), "New voice")
        self.assertEqual(dialog.voice_panel.current_voice_box.title(), "Current voice")
        self.assertIs(dialog.voice_panel.phrase_box.parentWidget(), dialog.controls)
        self.assertIs(
            dialog.voice_panel.current_voice_box.parentWidget(), dialog.controls
        )
        self.assertIs(dialog.voice_panel.parentWidget(), dialog.controls)
        self.assertEqual(dialog.voice_panel.a_use.text(), "Save")
        phrase = "A shared comparison phrase."
        dialog.voice_panel.preview_text.setPlainText(phrase)
        self.select(dialog, "catalog", source="character:centurion")
        self.assertEqual(dialog.voice_panel.preview_text.toPlainText(), phrase)
        self.assertEqual(dialog.voice_panel.candidate_voice_label.text(), "Centurion")
        dialog.resize(1060, 720)
        dialog.show()
        self.application.processEvents()
        current = dialog.voice_panel.current_voice_box.geometry()
        candidate = dialog.voice_panel.geometry()
        self.assertLess(current.bottom(), dialog.source_controls.geometry().top())
        self.assertLess(dialog.source_controls.geometry().bottom(), candidate.top())
        self.assertLess(
            candidate.bottom(), dialog.voice_panel.phrase_box.geometry().top()
        )
        separators = [
            frame
            for frame in dialog.controls.findChildren(QFrame)
            if frame.frameShape() == QFrame.Shape.HLine
        ]
        self.assertEqual(len(separators), 2)
        separators.sort(key=lambda frame: frame.y())
        self.assertLess(current.bottom(), separators[0].y())
        self.assertLess(separators[0].geometry().bottom(), dialog.source_controls.y())
        self.assertLess(candidate.bottom(), separators[1].y())
        self.assertLess(
            separators[1].geometry().bottom(), dialog.voice_panel.phrase_box.y()
        )

    def test_voice_replacement_remains_vertical_with_reachable_playback_controls(self):
        dialog = self.editor()
        self.select(dialog, "catalog", source="character:centurion")
        font = dialog.font()
        font.setPointSizeF(font.pointSizeF() * 2)
        dialog.setFont(font)
        dialog.resize(480, 540)
        dialog.show()
        self.application.processEvents()
        scroll = dialog.findChild(QScrollArea)
        self.assertIsNotNone(scroll)
        for width, height in ((480, 540), (1060, 720), (480, 540)):
            dialog.resize(width, height)
            self.application.processEvents()
            self.assertLess(
                dialog.voice_panel.current_voice_box.geometry().bottom(),
                dialog.source_controls.geometry().top(),
            )
            self.assertLess(
                dialog.voice_panel.geometry().bottom(),
                dialog.voice_panel.phrase_box.geometry().top(),
            )
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        for box, buttons in (
            (
                dialog.voice_panel.current_voice_box,
                (dialog.voice_panel.current_original, dialog.voice_panel.current_play),
            ),
            (
                dialog.voice_panel,
                (dialog.voice_panel.a_original, dialog.voice_panel.a_play),
            ),
        ):
            for button in buttons:
                self.assertTrue(
                    box.rect().contains(button.mapTo(box, button.rect().bottomRight()))
                )

    def test_donor_name_is_shown_once_and_different_candidate_names_remain_visible(
        self,
    ):
        plan, group, _manifest = ambiguous_fixture(self.root / "donor-label")
        dialog = self.editor()
        dialog.set_voice_context(plan, character=group.character)
        self.drain()
        panel = dialog.voice_panel
        self.assertEqual(dialog.characters.currentText(), "Rhiannon")
        self.assertEqual(panel.candidate_voice_label.text(), "Rhiannon")
        self.assertTrue(panel.candidate_voice_label.isHidden())
        for index in range(panel.voice_reference.count()):
            panel.voice_reference.setCurrentIndex(index)
            self.drain()
            if panel.candidate_voice_label.text() == "Rhiannon":
                self.assertTrue(panel.candidate_voice_label.isHidden())
            else:
                self.assertFalse(panel.candidate_voice_label.isHidden())
        self.select(dialog, "catalog", source="character:centurion")
        self.assertEqual(panel.candidate_voice_label.text(), "Centurion")
        self.assertTrue(panel.candidate_voice_label.isHidden())
        self.select(dialog, "automatic")
        self.assertFalse(panel.candidate_voice_label.isHidden())

    def test_same_character_replacement_shows_both_reference_texts_and_keeps_saved_audio(
        self,
    ):
        old_text = "Good morning, traveller."
        new_text = "The storm has passed."
        original = (self.manifest.parent / "references" / "rhiannon.wav").read_bytes()
        self.settings = bind_voice_library_selection(
            self.settings,
            self.manifest,
            "character:rhiannon",
            "Rhiannon",
            reference_transcript=old_text,
        )
        (self.manifest.parent / "references" / "rhiannon.wav").write_bytes(
            clean_wav_bytes(amplitude=0.2)
        )
        document = json.loads(self.manifest.read_text())
        document["voices"][0]["vntts.reference_transcript"] = new_text
        self.manifest.write_text(json.dumps(document))
        before = self.library.bindings()
        dialog = self.editor()
        self.select(dialog, "catalog", source="character:rhiannon")
        dialog.show()
        self.application.processEvents()
        panel = dialog.voice_panel
        self.assertIn("Rhiannon", panel.current_voice_label.text())
        self.assertEqual(panel.candidate_voice_label.text(), "Rhiannon")
        self.assertEqual(panel.current_reason.text(), old_text)
        self.assertEqual(panel.a_reason.text(), new_text)
        self.assertTrue(panel.current_reason.isVisible())
        self.assertTrue(panel.a_reason.isVisible())
        self.assertNotEqual(
            panel._current_voice[2].reference_sha256s,
            panel.current_selection[1].candidates[0].reference_sha256s,
        )
        panel.current_original.click()
        self.drain()
        self.assertEqual(self.player.play_bytes.call_args.args[0], original)
        self.assertFalse(panel.a_use.isEnabled())
        self.assertEqual(self.library.bindings(), before)

    def test_game_reference_excerpt_survives_save_and_reopen_without_becoming_transcript(
        self,
    ):
        old_text = "Good morning, traveller."
        new_text = "The storm has passed."
        plan, group, _manifest = ambiguous_fixture(self.root / "game-reference")
        candidate = replace(
            group.candidate_inventory[0],
            source_excerpts=(old_text,),
            reference_transcript=None,
        )
        group = replace(
            group, candidates=(candidate,), candidate_inventory=(candidate,)
        )
        plan = replace(plan, groups=(group,))
        dialog = self.editor()
        dialog.set_voice_context(plan, character=group.character)
        self.drain()
        self.assertEqual(dialog.voice_panel.a_reason.text(), old_text)
        self.listen(dialog, original=True)
        self.save(dialog)
        binding = self.library.binding(group.character)
        self.assertEqual(binding.provenance["evidence"]["source_excerpts"], [old_text])
        self.assertNotIn("reference_transcript", binding.provenance["evidence"])
        document = json.loads(self.manifest.read_text())
        document["voices"][0]["vntts.reference_transcript"] = new_text
        self.manifest.write_text(json.dumps(document))
        reopened = self.editor()
        self.select(
            reopened, "catalog", role=group.character, source="character:rhiannon"
        )
        panel = reopened.voice_panel
        self.assertIn("Rhiannon", panel.current_voice_label.text())
        self.assertEqual(panel.candidate_voice_label.text(), "Rhiannon")
        self.assertEqual(panel.current_reason.text(), old_text)
        self.assertEqual(panel.a_reason.text(), new_text)
        self.assertIsNone(panel._current_voice[2].reference_transcript)

    def test_current_reference_stays_visible_while_candidate_loads_or_is_unavailable(
        self,
    ):
        self.bind_narrator()
        dialog = self.editor()
        self.select(dialog, "catalog", source="character:centurion")
        dialog.show()
        self.application.processEvents()
        panel = dialog.voice_panel
        dialog.source.setCurrentIndex(dialog.source.findData("saved"))
        self.assertTrue(dialog.runner.active)
        self.assertTrue(panel.isHidden())
        self.assertTrue(panel.current_voice_box.isVisible())
        self.assertTrue(panel.current_reason.isVisible())
        self.assertTrue(panel.current_original.isEnabled())
        self.assertTrue(panel.current_play.isEnabled())
        self.drain()
        dialog.source.setCurrentIndex(dialog.source.findData("catalog"))
        dialog.catalog_choice.clear()
        self.application.processEvents()
        self.assertTrue(panel.isHidden())
        self.assertTrue(panel.current_voice_box.isVisible())
        self.assertTrue(panel.current_reason.isVisible())
        self.assertTrue(panel.current_original.isEnabled())
        self.assertTrue(panel.current_play.isEnabled())
        self.assertFalse(panel.a_use.isEnabled())

    def test_saved_game_voice_label_uses_library_without_the_catalog(self):
        self.bind_narrator()
        dialog = self.editor()
        dialog._catalog_registry = CharacterVoiceRegistry()
        dialog._role_changed()
        self.drain()
        self.assertEqual(dialog.role_summary.text(), "Saved: Rhiannon")
        self.assertEqual(dialog.saved_assignment_summary(), "Narrator: Rhiannon")

    def test_imported_voice_catalog_failure_is_logged(self):
        dialog = self.editor()
        with (
            patch(
                "vntts.game_narrator_ui.CharacterVoiceRegistry.from_file",
                side_effect=OSError("catalog unreadable"),
            ),
            self.assertLogs("vntts.game_narrator_ui", level="ERROR") as logged,
        ):
            dialog.set_voice_context(character="Narrator")

        self.assertIn("Unable to load imported voice catalog", logged.output[0])

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
                self.assertFalse(dialog.voice_panel.candidate_voice_label.isHidden())
                self.assertTrue(dialog.voice_panel.candidate_voice_label.text())
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

    def test_pocket_acceptance_is_owned_by_settings_without_editor_terms_widgets(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                settings = self.settings.updated(
                    speech_backend="pocket-tts", pocket_gated_model_accepted=accepted
                )
                dialog = self.editor(settings=settings)
                self.select(dialog, "catalog", source="character:rhiannon")
                self.assertIs(dialog.settings_value, settings)
                self.assertFalse(hasattr(dialog, "consent"))
                self.assertFalse(hasattr(dialog, "terms"))
                self.assertFalse(
                    any(
                        "Pocket" in widget.text()
                        for widget in dialog.findChildren(QCheckBox)
                    )
                )
                self.assertFalse(
                    any(
                        "model terms" in widget.text()
                        for widget in dialog.findChildren(QLabel)
                    )
                )
                self.assertEqual(self.backend.requests, [])

    def test_pocket_save_uses_settings_acceptance_and_preserves_it(self):
        before = self.library.bindings()
        missing = self.editor(
            settings=self.settings.updated(speech_backend="pocket-tts")
        )
        self.select(missing, "catalog", source="character:rhiannon")
        group = missing.voice_panel.current_group()
        missing._capture_save(None)
        with self.assertRaisesRegex(ValueError, "Accept Pocket's terms"):
            missing.remember_many(((group, "character:rhiannon"),))
        self.assertEqual(self.library.bindings(), before)
        self.assertIsNone(missing.result_settings)
        self.assertFalse(missing.settings_value.pocket_gated_model_accepted)
        accepted = self.editor(
            settings=self.settings.updated(
                speech_backend="pocket-tts", pocket_gated_model_accepted=True
            )
        )
        self.select(accepted, "catalog", source="character:rhiannon")
        self.listen(accepted, original=True)
        self.save(accepted)
        self.assertTrue(accepted.result_settings.pocket_gated_model_accepted)
        self.assertIsNotNone(self.library.binding("Narrator"))

    def test_editor_has_no_maintenance_actions_and_keeps_model_visible_for_every_source(
        self,
    ):
        self.bind_narrator()
        dialog = self.editor(
            settings=self.settings.updated(
                speech_backend="pocket-tts", pocket_gated_model_accepted=True
            )
        )
        for name in (
            "source_setup",
            "source_setup_controls",
            "game_installation",
            "folder_button",
            "discover_button",
            "copy_details",
        ):
            self.assertFalse(hasattr(dialog, name), name)
        dialog.show()
        panel = dialog.voice_panel
        for mode in ("catalog", "saved", "game", "preset", "automatic"):
            with self.subTest(mode=mode):
                self.select(dialog, mode)
                self.assertTrue(dialog.tts_model_label.isVisible())
                for name in (
                    "reference_details_toggle",
                    "reference_details",
                    "advice_details",
                    "runtime",
                ):
                    self.assertFalse(hasattr(panel, name), name)
                self.assertTrue(dialog.settings_value.pocket_gated_model_accepted)
        self.assertEqual(self.backend.requests, [])

    def test_model_matches_actual_preview_plan_without_reloading_or_changing_current_voice(
        self,
    ):
        plan, group, _manifest = ambiguous_fixture(self.root / "model-preview")
        plan = replace(plan, synthesis_model="selected-preview-model")
        dialog = self.editor(
            settings=self.settings.updated(speech_backend="pocket-tts")
        )
        self.service.backend_factory = Mock(return_value=self.backend)
        dialog.set_voice_context(plan, character=group.character)
        self.drain()
        dialog.show()
        self.application.processEvents()
        panel = dialog.voice_panel
        self.assertEqual(panel.current_selection[0], plan)
        self.assertEqual(
            dialog.tts_model_label.text(), "TTS model: selected-preview-model"
        )
        self.assertIn("Model: selected-preview-model", dialog.tts_model_label.toolTip())
        self.assertLess(
            dialog.role.mapTo(dialog.controls, dialog.role.rect().bottomLeft()).y(),
            dialog.tts_model_label.y(),
        )
        self.assertLess(
            dialog.tts_model_label.geometry().bottom(), panel.current_voice_box.y()
        )
        self.backend.name = plan.synthesis_backend
        self.listen(dialog)
        self.assertEqual(
            self.service.backend_factory.call_args.args[0], plan.synthesis_backend
        )
        self.assertEqual(
            self.service.backend_factory.call_args.kwargs["model_name"],
            plan.synthesis_model,
        )
        panel.preview_text.setPlainText("Keep my comparison phrase.")
        self.drain()
        selected, donor = panel.current_selection, dialog.characters.currentText()
        current, dirty, bindings = (
            panel._current_voice,
            dialog._settings_dirty,
            self.library.bindings(),
        )
        requests = len(self.backend.requests)
        self.importer.reset_mock()
        dialog._update()
        dialog._update()
        self.assertEqual(panel.current_selection, selected)
        self.assertEqual(panel._current_voice, current)
        self.assertEqual(dialog.characters.currentText(), donor)
        self.assertEqual(panel.preview_text.toPlainText(), "Keep my comparison phrase.")
        self.assertEqual(dialog._settings_dirty, dirty)
        self.assertEqual(self.library.bindings(), bindings)
        self.assertEqual(len(self.backend.requests), requests)
        self.importer.prepare_voice_roles.assert_not_called()
        self.importer.narrator_characters.assert_not_called()

    def test_model_follows_new_source_context_and_pocket_cloning_configuration(self):
        plan, group, _manifest = ambiguous_fixture(self.root / "model-context")
        dialog = self.editor(
            settings=self.settings.updated(
                speech_backend="pocket-tts", pocket_gated_model_accepted=True
            )
        )
        dialog.set_voice_context(
            replace(plan, synthesis_model="story-model"), character=group.character
        )
        self.drain()
        self.assertIn("story-model", dialog.tts_model_label.text())
        self.select(dialog, "preset", role=group.character, source="preset:alba")
        selected_plan = dialog.voice_panel.current_selection[0]
        self.assertEqual(selected_plan.synthesis_backend, "pocket-tts")
        self.assertFalse(selected_plan.pocket_voice_cloning)
        self.assertIn("Pocket TTS preset-only", dialog.tts_model_label.text())
        self.assertNotIn("story-model", dialog.tts_model_label.toolTip())
        self.select(dialog, "automatic", role=group.character)
        self.assertTrue(dialog.voice_panel.a_use.isEnabled())
        self.assertIn("Pocket TTS with voice cloning", dialog.tts_model_label.text())
        self.assertEqual(self.backend.requests, [])

    def test_loading_uses_resolved_model_and_preserves_known_installation(self):
        installation = self.root / "installed-game"
        self.importer.selected_installation_root.return_value = installation
        dialog = self.editor(
            settings=self.settings.updated(tts_model="configured-model")
        )
        plan, group, _manifest = ambiguous_fixture(self.root / "loading-model")
        dialog.set_voice_context(
            replace(plan, synthesis_model="story-model"), character=group.character
        )
        self.drain()
        dialog.show()
        dialog.discover()
        self.assertTrue(dialog.runner.active)
        self.assertEqual(dialog._installation_root, installation)
        self.assertTrue(dialog.voice_panel.isHidden())
        self.assertTrue(dialog.tts_model_label.isVisible())
        self.assertIn("configured-model", dialog.tts_model_label.text())
        self.assertNotIn("story-model", dialog.tts_model_label.toolTip())
        self.assertFalse(dialog.characters.isEnabled())
        self.drain()
        self.assertEqual(dialog._installation_root, installation)
        self.importer.narrator_characters.assert_called_with(dialog.cancellation, None)
        self.assertIn("No voiced characters", dialog.status.text())

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
        self.assertEqual(dialog.cancel_button.text(), "Back")

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
        dialog.show()
        self.application.processEvents()
        self.assertTrue(dialog.tts_model_label.isVisible())
        self.assertIn("Qwen", dialog.tts_model_label.text())
        self.assertNotIn("live-model", dialog.tts_model_label.toolTip())
        self.assertNotIn("offline-model", dialog.tts_model_label.toolTip())
        self.assertEqual(self.backend.requests, [])

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

import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from tests.pregeneration_fixtures import ambiguous_fixture  # noqa: E402
from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.dashboard_ui import ControlDashboard  # noqa: E402
from vntts.game_content_importer import Reverse1999GameImporter  # noqa: E402
from vntts.pregeneration_pack import OfflinePreparationChanges  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import VoiceDecisionStore  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.voice_library import VoiceLibrary  # noqa: E402


class VoicePlanOverviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        plan, group, _manifest = ambiguous_fixture(self.root / "fixture")
        self.group = group
        self.plan = replace(
            plan,
            synthesis_backend="pocket-tts",
            pocket_voice_cloning=True,
            groups=(group,),
        )
        self.library = VoiceLibrary(self.root / "library")
        self.library.select("Narrator", route="voice", source_id="preset:alba")
        self.dialog = OfflineAudioPreparationDialog(
            AppSettings(pocket_gated_model_accepted=True),
            discovery=lambda: ContentDiscovery(()),
            job_store=PregenerationJobStore(self.root / "jobs"),
            voice_library=self.library,
            voice_decisions=VoiceDecisionStore(
                self.root / "decisions.json", voice_library=self.library
            ),
            importer=Reverse1999GameImporter(
                output_root=self.root / "imported",
                installation_file=self.root / "installation.json",
            ),
            audition_service=Mock(),
            preview_player=Mock(),
            thread_pool=ManualThreadPool(),
        )
        self.addCleanup(self.dialog.deleteLater)
        self.addCleanup(self.dialog.summary_voice_preview.close)

    def show_plan(self, groups=None):
        plan = replace(self.plan, groups=tuple(groups)) if groups else self.plan
        self.dialog._voice_plan = plan
        self.dialog._show_voice_confirmation(plan)
        return plan

    def row_values(self, row=0):
        return self.dialog.voice_routes.item(row).data(
            int(Qt.ItemDataRole.UserRole) + 2
        )

    def saved_group(self, **changes):
        candidate = self.group.candidates[0]
        return replace(
            self.group,
            route="voice",
            resolution="saved-voice-assignment",
            source_id=candidate.source_id,
            source_character=candidate.source_character,
            reference_sha256s=candidate.reference_sha256s,
            **changes,
        )

    def test_imported_voice_names_remain_distinct_in_confirmation(self):
        for name in ("Zuzanna", "Everecho"):
            with self.subTest(name=name):
                group = replace(
                    self.saved_group(),
                    source_character=f"Player candidate {name} abcdef123456",
                )
                self.show_plan((group,))
                self.assertIn(name, self.row_values()[1])
                self.assertNotIn("abcdef123456", self.row_values()[1])
                self.assertNotEqual(self.row_values()[1], "Game voice")

    def test_saved_voice_immediately_rechecks_ready_recordings(self):
        self.dialog._job = Mock(job_id=self.plan.job_id)
        self.dialog._changes_rows = (
            ("Reuse", "120 recordings"),
            ("To generate", "200 lines"),
        )
        self.dialog._fast_voice_choice_pending = True
        with patch.object(self.dialog, "_start_generation_input") as start:
            self.dialog._voice_plan_finished(self.plan, None)
        start.assert_called_once_with(self.plan)
        self.assertIn(("Reuse", "120 recordings"), self.dialog._changes_rows)

    def test_routine_routes_do_not_repeat_summary_or_assignment_explanation(self):
        group = self.saved_group()
        self.show_plan((group,))
        self.assertTrue(self.dialog.voice_route_summary.isHidden())
        details = self.dialog.voice_route_details.text()
        self.assertIn(group.character, details)
        self.assertIn("reference recording", details)
        self.assertNotIn("Saved character voice assignment", details)
        self.show_plan((replace(group, resolution="automatic-character-match"),))
        self.assertNotIn(
            "Automatic character match", self.dialog.voice_route_details.text()
        )

    def test_empty_filter_explains_hidden_characters_without_dead_actions(self):
        group = replace(
            self.saved_group(), character=self.group.candidates[0].source_character
        )
        self.show_plan((group,))
        self.dialog.more_voice_actions_toggle.setChecked(True)
        self.dialog.show_all_voice_routes.setChecked(True)
        self.assertEqual(self.dialog.voice_routes.count(), 0)
        self.assertFalse(self.dialog.voice_routes_empty.isHidden())
        self.assertIn(
            "Turn off Substitutions only", self.dialog.voice_routes_empty.text()
        )
        self.assertTrue(self.dialog.inspect_character_voice.isHidden())
        self.assertTrue(self.dialog.more_voice_actions_toggle.isHidden())
        self.assertTrue(self.dialog.more_voice_actions.isHidden())
        self.dialog.show_all_voice_routes.setChecked(False)
        self.assertEqual(self.dialog.voice_routes.count(), 1)
        self.assertTrue(self.dialog.voice_routes_empty.isHidden())
        self.assertFalse(self.dialog.inspect_character_voice.isHidden())
        self.assertFalse(self.dialog.more_voice_actions.isHidden())

    def test_narrator_only_plan_explains_absent_character_rows(self):
        plan = replace(self.plan, groups=(replace(self.group, character="Narrator"),))
        self.dialog._voice_plan = plan
        self.dialog._show_voice_confirmation(plan)
        self.assertEqual(self.dialog.voice_routes.count(), 0)
        self.assertIn("No character voices", self.dialog.voice_routes_empty.text())
        self.assertTrue(self.dialog.show_all_voice_routes.isHidden())
        self.assertTrue(self.dialog.voice_route_summary.isHidden())
        self.assertTrue(self.dialog.more_voice_actions_toggle.isHidden())

    def test_narrator_reference_count_uses_the_narrator_not_previous_character(self):
        candidate = replace(self.group.candidates[0], reference_duration_seconds=12.5)
        self.library.select("Narrator", route="voice", source_id=candidate.source_id)
        group = replace(
            self.group,
            route="narrator",
            resolution="automatic-narrator-fallback",
            reference_sha256s=("a" * 64,) * 3,
            narrator_candidate=candidate,
        )
        self.show_plan((group,))
        details = self.dialog.voice_route_details.text()
        self.assertIn("1 reference recording, 12.5 s", details)
        self.assertNotIn("3 reference", details)

    def test_pending_voice_routes_block_stale_editing_and_reenable_after_revert(self):
        group = self.saved_group()
        self.library.link_person(group.character, "Another name")
        self.show_plan(
            (
                group,
                replace(
                    group, group_id="other", character="Other", routing_role="Other"
                ),
            )
        )
        self.dialog.game_narrator_chooser = Mock()
        for control in ("narrator", "cloning"):
            with self.subTest(control=control):
                if control == "narrator":
                    self.dialog.narrator_choice.setCurrentIndex(
                        self.dialog.narrator_choice.findData("preset:marius")
                    )
                else:
                    self.dialog.pocket_voice_cloning.setChecked(False)
                self.assertEqual(
                    self.dialog.continue_button.text(), "Apply voice changes"
                )
                self.assertIn(
                    "review character voices", self.dialog.change_summary.text()
                )
                self.dialog.voice_routes.setCurrentRow(1)
                self.dialog.voice_routes.setCurrentRow(0)
                self.dialog.show_all_voice_routes.setChecked(True)
                self.dialog.show_all_voice_routes.setChecked(False)
                self.dialog.voice_routes.setCurrentRow(
                    next(
                        row
                        for row in range(self.dialog.voice_routes.count())
                        if self.dialog.voice_routes.item(row).data(
                            Qt.ItemDataRole.UserRole
                        )
                        == group.character
                    )
                )
                for action in (
                    self.dialog.inspect_character_voice,
                    self.dialog.link_identity,
                    self.dialog.unlink_identity,
                ):
                    self.assertFalse(action.isEnabled())
                aliases = self.library.person_aliases()
                with patch("vntts.pregeneration_ui.QInputDialog.getItem") as picker:
                    self.dialog._choose_character_voice()
                    self.dialog.voice_routes.itemDoubleClicked.emit(
                        self.dialog.voice_routes.currentItem()
                    )
                    self.dialog._choose_character_voice()
                    self.dialog._link_selected_person()
                    self.dialog._unlink_selected_person()
                    picker.assert_not_called()
                self.assertEqual(self.library.person_aliases(), aliases)
                self.dialog.game_narrator_chooser.assert_not_called()
                if control == "narrator":
                    self.dialog.narrator_choice.setCurrentIndex(
                        self.dialog.narrator_choice.findData("preset:alba")
                    )
                else:
                    self.dialog.pocket_voice_cloning.setChecked(True)
                self.assertEqual(
                    self.dialog.continue_button.text(), "Generate with these voices"
                )
                self.assertTrue(self.dialog.inspect_character_voice.isEnabled())
                self.assertTrue(self.dialog.unlink_identity.isEnabled())

    def test_character_without_candidates_opens_the_shared_editor(self):
        group = replace(
            self.saved_group(),
            candidate_inventory=(),
            candidates=(),
            narrator_candidate=None,
        )
        self.show_plan((group,))
        self.dialog.game_narrator_chooser = Mock(return_value=None)
        self.dialog.inspect_character_voice.click()
        self.dialog.game_narrator_chooser.assert_called_once_with(
            self.dialog.settings, self.dialog, character=group.character
        )
        self.assertTrue(self.dialog._awaiting_voice_confirmation)
        self.assertTrue(self.dialog.continue_button.isEnabled())
        self.assertFalse(hasattr(self.dialog, "voice_panel"))
        self.assertFalse(hasattr(self.dialog, "choose_character_voice"))

    def test_primary_action_and_double_click_open_the_same_editor(self):
        self.show_plan()
        self.dialog.game_narrator_chooser = Mock(return_value=None)
        self.dialog.inspect_character_voice.click()
        self.dialog.voice_routes.itemDoubleClicked.emit(
            self.dialog.voice_routes.currentItem()
        )
        self.assertEqual(self.dialog.game_narrator_chooser.call_count, 2)
        for call in self.dialog.game_narrator_chooser.call_args_list:
            self.assertEqual(call.args, (self.dialog.settings, self.dialog))
            self.assertEqual(call.kwargs, {"character": self.group.character})

    def test_cancelled_or_async_editor_keeps_the_story_plan_unchanged(self):
        plan = self.show_plan()
        self.dialog.game_narrator_chooser = Mock(return_value=None)
        self.dialog.apply_narrator_settings = Mock()
        self.dialog.inspect_character_voice.click()
        self.assertIs(self.dialog.voice_plan(), plan)
        self.assertTrue(self.dialog._awaiting_voice_confirmation)
        self.dialog.apply_narrator_settings.assert_not_called()

    def test_accepted_editor_replans_even_when_narrator_settings_are_unchanged(self):
        self.show_plan()
        self.dialog.game_narrator_chooser = Mock(return_value=self.dialog.settings)
        self.dialog.apply_narrator_settings = Mock()
        self.dialog.inspect_character_voice.click()
        self.dialog.apply_narrator_settings.assert_called_once_with(
            self.dialog.settings, voice_changed=True
        )

    def test_standalone_editor_receives_selected_role_and_story_plan(self):
        from PySide6.QtWidgets import QDialog

        plan = self.show_plan()
        with patch("vntts.game_narrator_ui.GameNarratorDialog") as editor:
            editor.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.dialog.inspect_character_voice.click()
        editor.return_value.set_voice_context.assert_called_once_with(
            plan, character=self.group.character
        )
        self.assertIs(editor.call_args.kwargs["voice_library"], self.library)
        self.assertIs(self.dialog.voice_plan(), plan)

    def test_editor_context_survives_deferred_initialization(self):
        from vntts.game_narrator_ui import GameNarratorDialog

        group = self.saved_group()
        plan = self.show_plan((group,))
        self.library.select(group.character, route="voice", source_id=group.source_id)
        importer = Mock()
        importer.selected_installation_root.return_value = None
        editor = GameNarratorDialog(
            self.dialog.settings,
            importer=importer,
            preview_service=Mock(),
            player=Mock(),
            thread_pool=ManualThreadPool(),
            voice_library=self.library,
        )
        self.addCleanup(editor.deleteLater)
        self.addCleanup(editor.voice_panel.shutdown)
        editor.set_voice_context(plan, character=group.character)
        self.application.processEvents()
        self.assertIs(editor._voice_context, plan)
        self.assertEqual(editor.selected_role, group.character)
        self.assertFalse(editor.voice_panel.isHidden())
        selection = editor.voice_panel.current_selection
        self.assertIsNotNone(selection)
        self.assertEqual(selection[1].character, group.character)
        self.assertEqual(selection[2], group.source_id)
        importer.prepare_voice_roles.assert_not_called()
        importer.narrator_characters.assert_not_called()

    def test_choice_uses_saved_binding_provenance(self):
        group = self.saved_group()
        for method, expected in (
            ("automatic", "automatic"),
            ("manual", "chosen by you"),
        ):
            with self.subTest(method=method):
                self.library.select(
                    group.routing_role or group.character,
                    route="voice",
                    source_id=group.source_id,
                    method=method,
                )
                self.show_plan((group,))
                self.assertEqual(self.row_values()[2], expected)

    def test_unknown_or_unrelated_binding_does_not_imply_manual_choice(self):
        group = self.saved_group()
        self.show_plan((group,))
        self.assertEqual(self.row_values()[2], "saved choice")

        self.library.select(
            group.routing_role or group.character,
            route="voice",
            source_id="preset:marius",
            method="manual",
        )
        self.show_plan((group,))
        self.assertEqual(self.row_values()[2], "saved choice")

        self.show_plan((replace(group, resolution="saved-player-decision"),))
        self.assertEqual(self.row_values()[2], "chosen by you")

    def test_alias_choice_uses_canonical_provenance(self):
        group = self.saved_group(
            character="Canonical person", routing_role="Story alias", variant_key="v1"
        )
        self.library.link_person(group.character, group.routing_role)
        self.library.select(
            group.character,
            route="voice",
            source_id=group.source_id,
            method="manual",
        )
        self.library.select(
            group.routing_role,
            route="voice",
            source_id=group.source_id,
            method="automatic",
        )
        self.show_plan((group,))
        self.assertEqual(self.row_values()[2], "automatic")

        self.library.select(
            group.routing_role,
            variant_key=group.variant_key,
            route="voice",
            source_id=group.source_id,
            method="manual",
        )
        self.show_plan((group,))
        self.assertEqual(self.row_values()[2], "chosen by you")

    def test_one_character_voice_applies_to_all_selected_story_lines(self):
        group = replace(
            self.saved_group(), line_ids=("story-one:line", "story-two:line")
        )
        self.show_plan((group,))
        self.assertEqual(self.dialog.voice_routes.count(), 1)
        self.assertEqual(self.row_values()[3], "2")
        self.assertFalse(hasattr(self.dialog, "voice_assignment_choice"))
        self.assertTrue(
            self.dialog.voice_route_details.text().startswith(f"{group.character}: ")
        )
        self.dialog.game_narrator_chooser = Mock(return_value=None)
        self.dialog._choose_character_voice()
        self.dialog.game_narrator_chooser.assert_called_once_with(
            self.dialog.settings, self.dialog, character=group.character
        )

    def test_canonical_row_can_unlink_its_other_name(self):
        group = self.saved_group()
        self.library.link_person(group.character, "Other name")
        self.show_plan((group,))
        self.dialog._replan_voice_choices = Mock()
        self.assertTrue(self.dialog.unlink_identity.isEnabled())
        self.assertFalse(self.dialog.link_identity.isEnabled())
        self.assertEqual(
            self.dialog.identity_suggestion.text(), "Linked names: othername"
        )
        self.dialog.unlink_identity.click()
        self.assertEqual(self.library.person_aliases(), {})
        self.dialog._replan_voice_choices.assert_called_once()

    def test_canonical_row_can_choose_which_name_to_unlink(self):
        group = self.saved_group()
        self.library.link_person(group.character, "Other name")
        self.library.link_person(group.character, "Second name")
        self.show_plan((group,))
        self.dialog._replan_voice_choices = Mock()
        with patch(
            "vntts.pregeneration_ui.QInputDialog.getItem",
            return_value=("secondname", True),
        ):
            self.dialog.unlink_identity.click()
        self.assertEqual(set(self.library.person_aliases()), {"othername"})
        self.dialog._replan_voice_choices.assert_called_once()

    def test_substitution_filter_keeps_valid_selection_and_restores_all_rows(self):
        direct = self.saved_group()
        direct = replace(
            direct,
            character=direct.source_character,
            routing_role=direct.source_character,
            line_ids=("direct-1", "direct-2"),
        )
        fallback = replace(
            direct,
            group_id="f" * 64,
            character="Unknown character",
            routing_role="Unknown character",
            route="narrator",
            resolution="automatic-narrator-fallback",
            line_ids=("fallback",),
        )
        self.show_plan((direct, fallback))
        routes = self.dialog.voice_routes
        self.assertEqual(routes.count(), 2)
        routes.setCurrentRow(0)
        self.assertEqual(
            routes.currentItem().data(int(Qt.ItemDataRole.UserRole) + 1),
            direct.group_id,
        )

        self.dialog.show_all_voice_routes.setChecked(True)
        self.assertEqual(routes.count(), 1)
        self.assertEqual(
            routes.currentItem().data(int(Qt.ItemDataRole.UserRole) + 1),
            fallback.group_id,
        )
        self.assertTrue(self.dialog.inspect_character_voice.isEnabled())
        self.assertEqual(
            self.dialog.voice_route_details.text(),
            routes.currentItem().data(int(Qt.ItemDataRole.UserRole) + 3),
        )

        self.dialog.show_all_voice_routes.setChecked(False)
        self.assertEqual(
            {
                routes.item(row).data(int(Qt.ItemDataRole.UserRole) + 1)
                for row in range(routes.count())
            },
            {direct.group_id, fallback.group_id},
        )
        self.assertEqual(
            routes.currentItem().data(int(Qt.ItemDataRole.UserRole) + 1),
            fallback.group_id,
        )

    def test_keyboard_selection_updates_reference_details(self):
        first = self.saved_group()
        second = replace(
            first,
            group_id="f" * 64,
            character="Other role",
            routing_role="Other role",
            line_ids=("other-line",),
            resolution="known-character-voice",
            reference_sha256s=(),
        )
        self.show_plan((first, second))
        self.dialog.show()
        self.dialog.activateWindow()
        self.application.processEvents()
        self.dialog.voice_routes.setCurrentRow(0)
        self.dialog.voice_routes.setFocus()
        before = self.dialog.voice_route_details.text()
        QTest.keyClick(self.dialog.voice_routes, Qt.Key.Key_Down)
        self.assertEqual(self.dialog.voice_routes.currentRow(), 1)
        self.assertEqual(
            self.dialog.voice_route_details.text(),
            self.dialog.voice_routes.currentItem().data(
                int(Qt.ItemDataRole.UserRole) + 3
            ),
        )
        self.assertNotEqual(self.dialog.voice_route_details.text(), before)
        details = self.dialog.voice_route_details
        self.dialog.preparation_details_toggle.click()
        self.application.processEvents()
        self.assertTrue(details.isVisible())
        self.dialog.voice_routes.setFocus()
        for _ in range(30):
            QTest.keyClick(self.application.focusWidget(), Qt.Key.Key_Tab)
            if self.application.focusWidget() is details:
                break
        self.assertIs(self.application.focusWidget(), details)
        details.setSelection(0, len(details.text()))
        self.application.clipboard().clear()
        QTest.keySequence(details, QKeySequence(QKeySequence.StandardKey.Copy))
        self.assertEqual(self.application.clipboard().text(), details.text())
        QTest.keyClick(details, Qt.Key.Key_Tab)
        self.assertIsNot(self.application.focusWidget(), details)

    def test_card_mouse_selection_double_click_and_keyboard_preview(self):
        first = self.saved_group()
        second = replace(
            first, group_id="second", character="Other", routing_role="Other"
        )
        self.show_plan((first, second))
        self.dialog.show()
        self.application.processEvents()
        routes = self.dialog.voice_routes
        second_item = routes.item(1)
        card = routes.itemWidget(second_item)
        label = card.findChildren(QLabel)[0]
        QTest.mouseClick(label, Qt.MouseButton.LeftButton)
        self.assertIs(routes.currentItem(), second_item)
        with patch.object(self.dialog, "_choose_character_voice") as open_voice:
            QTest.mouseDClick(label, Qt.MouseButton.LeftButton)
            open_voice.assert_called_once()
        button = self.dialog._summary_preview_rows[second.group_id][0]
        button.setFocus()
        with patch.object(self.dialog, "_play_summary_voice") as preview:
            QTest.keyClick(button, Qt.Key.Key_Space)
            preview.assert_called_once_with(second.group_id)

    def test_compact_confirmation_keeps_list_and_actions_reachable_with_tools(self):
        groups = tuple(
            self.saved_group(
                group_id=str(index),
                character=f"Role {index}",
                routing_role=f"Role {index}",
            )
            for index in range(12)
        )
        self.show_plan(groups)
        dashboard = ControlDashboard(AppSettings())
        self.addCleanup(dashboard.deleteLater)
        dashboard.embed_preparation(self.dialog)
        dashboard.resize(860, 560)
        dashboard.show()
        self.addCleanup(dashboard.close)
        for scale in (1, 2):
            font = self.dialog.font()
            font.setPointSizeF(font.pointSizeF() * scale)
            self.dialog.setFont(font)
            dashboard.resize(1800, 1200)
            self.application.processEvents()
            routes = self.dialog.voice_routes
            ordinary_height = routes.visualItemRect(routes.item(0)).height()
            for tools_open in (False, True):
                with self.subTest(scale=scale, tools_open=tools_open):
                    dashboard.setup_more_button.setChecked(tools_open)
                    dashboard.resize(620, 440)
                    for _ in range(3):
                        self.application.processEvents()
                    self.assertGreaterEqual(
                        routes.viewport().height(), 4 * ordinary_height
                    )
                    self.assertTrue(self.dialog.content_scroll.isHidden())
                    for widget in (
                        routes,
                        self.dialog.inspect_character_voice,
                        self.dialog.continue_button,
                        self.dialog.cancel_button,
                    ):
                        self.assertTrue(widget.isVisible())
                        position = widget.mapTo(dashboard, widget.rect().topLeft())
                        self.assertTrue(dashboard.rect().contains(position))
                        self.assertTrue(
                            dashboard.rect().contains(
                                widget.mapTo(dashboard, widget.rect().bottomRight())
                            )
                        )

    def test_confirmation_is_hidden_when_another_phase_takes_over(self):
        transitions = (
            lambda: self.dialog._show_waiting_phase(
                "Planning", "Checking voices", "Cancel keeps the saved selection."
            ),
            lambda: self.dialog._show_voice_plan_error(
                ValueError("Voice is unavailable")
            ),
            lambda: self.dialog._set_discovery_loading(True),
            lambda: self.dialog._apply_discovery(ContentDiscovery(())),
        )
        for transition in transitions:
            self.show_plan((self.saved_group(),))
            transition()
            self.assertTrue(self.dialog.voice_confirmation.isHidden())

    def test_compact_counts_keep_replacement_pack_and_voice_warnings_visible(self):
        self.dialog._preparation_changes = OfflinePreparationChanges(
            reused=85,
            new=118,
            failed=2,
            live_fallbacks=3,
            omissions=1,
            original=11,
            replacement_candidates=5,
            preserved=12,
            switches_pack=True,
        )
        self.dialog._changes_rows = (
            ("Voice changes", "Two voices require review"),
            ("Reuse", "85 saved recordings; 11 original game lines"),
            ("To generate", "120 lines (2 previously failed)"),
            ("Existing live fallback", "3 lines; 1 omissions"),
            (
                "On activation",
                "5 recordings may be replaced; 12 in other stories stay.",
            ),
        )
        self.show_plan((self.saved_group(),))
        summary = self.dialog.change_summary.text()
        for expected in (
            "85",
            "120",
            "5 recordings may be replaced",
            "different story pack",
            "Two voices",
        ):
            self.assertIn(expected, summary)
        self.assertNotIn("previously failed", summary)
        self.dialog.preparation_details_toggle.click()
        self.assertIn(
            "2 previously failed", self.dialog.preparation_change_details.text()
        )
        self.assertIn(
            "11 original game lines", self.dialog.preparation_change_details.text()
        )

    def test_long_voice_rows_fit_compact_and_large_font_then_shrink_when_widened(self):
        first = self.saved_group(
            character="A character with a very long name in the selected story " * 3,
            routing_role="A long story role",
        )
        first = replace(
            first,
            source_character="A game reference with a long descriptive name " * 3,
        )
        second = replace(
            first,
            group_id="f" * 64,
            character="Another character with an equally long display name " * 3,
            routing_role="Another long story role",
            line_ids=("other-line",),
        )
        self.show_plan((first, second))
        self.dialog.resize(620, 440)
        self.dialog.show()
        self.application.processEvents()

        def fitted_row_heights():
            routes = self.dialog.voice_routes
            heights = []
            for row in range(routes.count()):
                item = routes.item(row)
                card = routes.itemWidget(item)
                required = card.layout().totalHeightForWidth(routes.viewport().width())
                allocated = routes.visualItemRect(item).height()
                self.assertGreaterEqual(allocated, required)
                self.assertGreaterEqual(card.height(), required)
                heights.append(allocated)
            return heights

        fitted_row_heights()
        font = self.dialog.font()
        font.setPointSizeF(font.pointSizeF() * 2)
        self.dialog.setFont(font)
        self.application.processEvents()
        compact_heights = fitted_row_heights()
        compact_width = self.dialog.voice_routes.viewport().width()

        self.dialog.resize(1040, 760)
        self.application.processEvents()
        wide_heights = fitted_row_heights()
        self.assertTrue(
            all(
                wide <= compact
                for wide, compact in zip(wide_heights, compact_heights, strict=True)
            )
        )
        self.assertTrue(
            any(
                wide < compact
                for wide, compact in zip(wide_heights, compact_heights, strict=True)
            ),
            f"Widths {compact_width} -> {self.dialog.voice_routes.viewport().width()}; "
            f"row heights {compact_heights} -> {wide_heights}",
        )

    def test_narrator_draft_does_not_claim_old_references(self):
        group = replace(
            self.group,
            route="narrator",
            resolution="automatic-narrator-fallback",
            reference_sha256s=("a" * 64,) * 3,
        )
        self.show_plan((group,))
        self.dialog.narrator_choice.setCurrentIndex(
            self.dialog.narrator_choice.findData("preset:marius")
        )
        self.assertIn("Marius", self.row_values()[1])
        details = self.dialog.voice_routes.item(0).data(
            int(Qt.ItemDataRole.UserRole) + 3
        )
        self.assertEqual(details, group.character)
        self.assertIn("Apply these changes", self.dialog.change_summary.text())
        self.assertNotIn("3 reference", details)
        self.assertEqual(self.dialog.continue_button.text(), "Apply voice changes")

    def test_review_is_optional_and_names_current_narrator(self):
        self.show_plan()
        self.assertIn("Alba", self.row_values()[1])
        self.assertIn("automatic", self.row_values()[2])
        self.assertIn("optional", self.row_values()[2])
        self.assertTrue(self.dialog.continue_button.isEnabled())
        self.assertEqual(self.row_values()[3], str(len(self.group.line_ids)))

    def test_missing_narrator_blocks_both_fallback_routes(self):
        for route in ("narrator", "needs-audition"):
            with self.subTest(route=route):
                self.show_plan((replace(self.group, route=route),))
                self.dialog.narrator_choice.setCurrentIndex(0)
                self.assertFalse(self.dialog.continue_button.isEnabled())
                self.assertIn("required", self.row_values()[1].casefold())

    def test_technical_and_advanced_actions_are_available_on_disclosure(self):
        self.show_plan()
        self.assertEqual(self.dialog.confirmed_narrator.text(), "Narrator: Alba")
        self.assertTrue(self.dialog.preparation_details.isHidden())
        self.assertFalse(
            self.dialog.work_summary.isVisibleTo(self.dialog.voice_confirmation)
        )
        self.assertTrue(self.dialog.more_voice_actions.isHidden())
        self.assertFalse(
            self.dialog.link_identity.isVisibleTo(self.dialog.voice_confirmation)
        )
        self.dialog.preparation_details_toggle.click()
        self.assertFalse(self.dialog.preparation_details.isHidden())
        self.assertTrue(
            self.dialog.work_summary.isVisibleTo(self.dialog.voice_confirmation)
        )
        self.dialog.more_voice_actions_toggle.click()
        self.assertFalse(self.dialog.more_voice_actions.isHidden())
        self.assertTrue(
            self.dialog.link_identity.isVisibleTo(self.dialog.voice_confirmation)
        )
        self.assertTrue(self.dialog.inspect_character_voice.isEnabled())
        headers = [
            label.text()
            for label in self.dialog.voice_routes_header.findChildren(QLabel)
        ]
        self.assertEqual(headers, ["Character", "Voice", "Selected by", "Lines"])


if __name__ == "__main__":
    unittest.main()

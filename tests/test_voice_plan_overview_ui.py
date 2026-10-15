import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from tests.pregeneration_fixtures import ambiguous_fixture  # noqa: E402
from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.game_content_importer import Reverse1999GameImporter  # noqa: E402
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
        self.plan = replace(plan, synthesis_backend="pocket-tts", groups=(group,))
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
        self.addCleanup(self.dialog.voice_panel.shutdown)

    def show_plan(self, groups=None):
        plan = replace(self.plan, groups=tuple(groups)) if groups else self.plan
        self.dialog._voice_plan = plan
        self.dialog._show_voice_confirmation(plan)
        self.dialog.content_scroll.show()
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

    def test_choice_uses_routing_role_and_variant_then_role_fallback(self):
        group = self.saved_group(
            character="Canonical person", routing_role="Story alias", variant_key="v1"
        )
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

    def test_character_appears_once_across_stories_with_unique_line_count(self):
        first = self.saved_group(character="Rhiannon")
        first = replace(first, line_ids=("story-one:line", "shared-line"))
        second = replace(
            first,
            character="RHIANNON",
            group_id="f" * 64,
            line_ids=("story-two:line", "shared-line"),
        )
        plan = self.show_plan((first, second))
        self.assertEqual(self.dialog.voice_routes.count(), 1)
        self.assertEqual(self.row_values()[0], "Rhiannon")
        self.assertEqual(self.row_values()[3], "3")
        self.assertEqual(
            set(
                self.dialog.voice_routes.item(0).data(int(Qt.ItemDataRole.UserRole) + 4)
            ),
            {first.group_id, second.group_id},
        )
        self.assertTrue(self.dialog.voice_assignment_controls.isHidden())
        self.assertEqual(plan.groups, (first, second))

    def test_character_assignments_remain_visible_and_inspect_exact_variant(self):
        first = self.saved_group()
        other_voice = replace(
            self.group.candidates[0],
            source_id="character:other-reference",
            source_character="Another game reference",
            reference_sha256s=("b" * 64,),
        )
        second = replace(
            first,
            group_id="f" * 64,
            routing_role="Story alias",
            variant_key="child",
            source_id=other_voice.source_id,
            source_character=other_voice.source_character,
            reference_sha256s=other_voice.reference_sha256s,
            candidates=(other_voice,),
            candidate_inventory=(other_voice,),
            line_ids=("other-story:line",),
        )
        self.library.select(
            first.routing_role or first.character,
            route="voice",
            source_id=first.source_id,
            method="automatic",
        )
        self.library.select(
            second.routing_role,
            variant_key=second.variant_key,
            route="voice",
            source_id=second.source_id,
            method="manual",
        )
        plan = self.show_plan((first, second))
        self.assertEqual(self.dialog.voice_routes.count(), 1)
        self.assertIn(first.source_character, self.row_values()[1])
        self.assertIn(second.source_character, self.row_values()[1])
        self.assertIn("automatic", self.row_values()[2])
        self.assertIn("chosen by you", self.row_values()[2])
        self.assertFalse(self.dialog.voice_assignment_controls.isHidden())
        choice = self.dialog.voice_assignment_choice
        self.assertEqual(choice.count(), 2)
        choice.setCurrentIndex(choice.findData(second.group_id))
        item = self.dialog.voice_routes.currentItem()
        self.assertEqual(item.data(int(Qt.ItemDataRole.UserRole)), second.routing_role)
        self.assertEqual(item.data(int(Qt.ItemDataRole.UserRole) + 1), second.group_id)
        self.assertEqual(
            set(item.data(int(Qt.ItemDataRole.UserRole) + 4)),
            {first.group_id, second.group_id},
        )
        self.dialog.voice_routes.setCurrentRow(0)
        self.dialog._render_voice_routes(plan)
        self.assertEqual(choice.currentData(), second.group_id)
        self.assertEqual(
            self.dialog.voice_routes.currentItem().data(
                int(Qt.ItemDataRole.UserRole) + 1
            ),
            second.group_id,
        )

        self.dialog.voice_panel.start = Mock()
        self.dialog._inspect_character_voice()
        self.dialog.voice_panel.start.assert_called_once_with(
            plan, group_id=second.group_id
        )
        self.assertEqual(plan.groups, (first, second))

    def test_substitution_filter_retains_every_assignment_of_character(self):
        direct = self.saved_group()
        direct = replace(
            direct,
            character=direct.source_character,
            routing_role=direct.source_character,
            line_ids=("direct-line",),
        )
        fallback = replace(
            direct,
            group_id="f" * 64,
            routing_role="Story alias",
            variant_key="child",
            route="narrator",
            resolution="automatic-narrator-fallback",
            line_ids=("fallback-line",),
        )
        other = replace(
            direct,
            group_id="e" * 64,
            character="Other character",
            routing_role="Other character",
            source_character="Other character",
            line_ids=("other-line",),
        )
        self.show_plan((direct, fallback, other))
        self.assertEqual(self.dialog.voice_routes.count(), 2)
        self.dialog.show_all_voice_routes.setChecked(True)
        self.assertEqual(self.dialog.voice_routes.count(), 1)
        self.assertEqual(self.row_values()[3], "2")
        self.assertIn(direct.source_character, self.row_values()[1])
        self.assertIn("Alba", self.row_values()[1])
        self.assertEqual(
            set(
                self.dialog.voice_routes.item(0).data(int(Qt.ItemDataRole.UserRole) + 4)
            ),
            {direct.group_id, fallback.group_id},
        )
        self.assertEqual(self.dialog.voice_assignment_choice.count(), 2)

    def test_same_voice_name_distinguishes_reference_sets_for_inspection(self):
        first = self.saved_group()
        second = replace(
            first,
            group_id="f" * 64,
            source_id="character:another-reference",
            reference_sha256s=("b" * 64,),
            line_ids=("other-story:line",),
        )
        plan = self.show_plan((first, second))
        self.assertEqual(self.dialog.voice_routes.count(), 1)
        self.assertIn("reference set 1", self.row_values()[1])
        self.assertIn("reference set 2", self.row_values()[1])
        choice = self.dialog.voice_assignment_choice
        self.assertEqual(choice.count(), 2)
        first_index = choice.findData(first.group_id)
        second_index = choice.findData(second.group_id)
        self.assertGreaterEqual(first_index, 0)
        self.assertGreaterEqual(second_index, 0)
        self.assertIn("reference set 1", choice.itemText(first_index))
        self.assertIn("reference set 2", choice.itemText(second_index))
        choice.setCurrentIndex(second_index)
        self.dialog.voice_panel.start = Mock()
        self.dialog._inspect_character_voice()
        self.dialog.voice_panel.start.assert_called_once_with(
            plan, group_id=second.group_id
        )
        self.assertEqual(plan.groups, (first, second))

    def test_assignment_change_clears_previous_accessible_action_hint(self):
        review = replace(self.group, line_ids=("review-story:line",))
        saved = self.saved_group(routing_role="Story alias", variant_key="child")
        saved = replace(saved, group_id="f" * 64, line_ids=("saved-story:line",))
        self.show_plan((review, saved))
        item = self.dialog.voice_routes.currentItem()
        self.assertEqual(
            self.dialog.voice_assignment_choice.currentData(), review.group_id
        )
        self.assertIn(
            "Continuing without a choice uses Narrator",
            item.data(Qt.ItemDataRole.AccessibleDescriptionRole),
        )
        summary = item.data(Qt.ItemDataRole.AccessibleTextRole)
        self.assertIn("2 lines", summary)
        self.assertIn("Alba", summary)
        self.assertIn(saved.source_character, summary)

        choice = self.dialog.voice_assignment_choice
        choice.setCurrentIndex(choice.findData(saved.group_id))
        self.assertEqual(item.data(Qt.ItemDataRole.AccessibleDescriptionRole), "")
        self.assertEqual(item.data(Qt.ItemDataRole.AccessibleTextRole), summary)

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
        QTest.keyClick(self.dialog.voice_routes, Qt.Key.Key_Tab)
        focused = self.application.focusWidget()
        self.assertIs(
            focused,
            details,
            f"Tab focused {focused.text() if hasattr(focused, 'text') else focused}",
        )
        details.setSelection(0, len(details.text()))
        self.application.clipboard().clear()
        QTest.keySequence(details, QKeySequence(QKeySequence.StandardKey.Copy))
        self.assertEqual(self.application.clipboard().text(), details.text())
        QTest.keyClick(details, Qt.Key.Key_Tab)
        self.assertIsNot(self.application.focusWidget(), details)

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
        self.assertIn("update", details.casefold())
        self.assertIn("route", details.casefold())
        self.assertNotIn("3 reference", details)
        self.assertEqual(self.dialog.continue_button.text(), "Update voice routes")

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
            self.dialog.choose_character_voice.isVisibleTo(
                self.dialog.voice_confirmation
            )
        )
        self.dialog.preparation_details_toggle.click()
        self.assertFalse(self.dialog.preparation_details.isHidden())
        self.assertTrue(
            self.dialog.work_summary.isVisibleTo(self.dialog.voice_confirmation)
        )
        self.dialog.more_voice_actions_toggle.click()
        self.assertFalse(self.dialog.more_voice_actions.isHidden())
        self.assertTrue(
            self.dialog.choose_character_voice.isVisibleTo(
                self.dialog.voice_confirmation
            )
        )
        self.assertTrue(self.dialog.choose_character_voice.isEnabled())
        headers = [
            label.text()
            for label in self.dialog.voice_routes_header.findChildren(QLabel)
        ]
        self.assertEqual(headers, ["Character", "Voice", "Selected by", "Lines"])


if __name__ == "__main__":
    unittest.main()

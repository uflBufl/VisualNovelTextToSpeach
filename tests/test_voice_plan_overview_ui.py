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
        plan = self.show_plan((group,))
        self.assertEqual(self.dialog.voice_routes.count(), 1)
        self.assertEqual(self.row_values()[3], "2")
        self.assertFalse(hasattr(self.dialog, "voice_assignment_choice"))
        self.assertTrue(
            self.dialog.voice_route_details.text().startswith(f"{group.character}: ")
        )
        self.dialog.voice_panel.start = Mock()
        self.dialog._inspect_character_voice()
        self.dialog.voice_panel.start.assert_called_once_with(
            plan, group_id=group.group_id
        )

    def test_canonical_row_can_unlink_its_other_name(self):
        group = self.saved_group()
        self.library.link_person(group.character, "Other name")
        self.show_plan((group,))
        self.dialog._voice_auditions_completed = Mock()
        self.assertTrue(self.dialog.unlink_identity.isEnabled())
        self.assertFalse(self.dialog.link_identity.isEnabled())
        self.assertEqual(
            self.dialog.identity_suggestion.text(), "Linked names: othername"
        )
        self.dialog.unlink_identity.click()
        self.assertEqual(self.library.person_aliases(), {})
        self.dialog._voice_auditions_completed.assert_called_once()

    def test_canonical_row_can_choose_which_name_to_unlink(self):
        group = self.saved_group()
        self.library.link_person(group.character, "Other name")
        self.library.link_person(group.character, "Second name")
        self.show_plan((group,))
        self.dialog._voice_auditions_completed = Mock()
        with patch(
            "vntts.pregeneration_ui.QInputDialog.getItem",
            return_value=("secondname", True),
        ):
            self.dialog.unlink_identity.click()
        self.assertEqual(set(self.library.person_aliases()), {"othername"})
        self.dialog._voice_auditions_completed.assert_called_once()

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

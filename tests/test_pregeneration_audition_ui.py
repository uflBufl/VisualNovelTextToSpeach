import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMessageBox,
)
from vntts_artifacts.file_integrity import sha256_file  # noqa: E402

from tests.pregeneration_fixtures import (  # noqa: E402
    ambiguous_fixture,
    clean_wav_bytes,
)
from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from tests.story_fixtures import write_story_index  # noqa: E402
from vntts.action_icons import action_icon  # noqa: E402
from vntts.game_content_importer import Reverse1999GameImporter  # noqa: E402
from vntts.person_link_suggestions import PersonLinkSuggestion  # noqa: E402
from vntts.pregeneration_audition import (  # noqa: E402
    COMPARISON_PREVIEW_TEXT,
    VoiceAuditionCancelled,
    VoiceAuditionPreviewService,
    VoiceAuditionReference,
)
from vntts.pregeneration_audition_queue import VoiceAuditionCoordinator  # noqa: E402
from vntts.pregeneration_audition_ui import (  # noqa: E402
    VoiceAuditionPanel,
    VoiceAuditionUIError,
)
from vntts.pregeneration_queue import PregenerationQueueError  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    StoryContentChanged,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import (  # noqa: E402
    VoiceCandidate,
    VoiceDecisionStore,
)
from vntts.qt_audio import QtPcmPlayer  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.voice_library import VoiceLibrary  # noqa: E402
from vntts.voices import default_voice_choice_id  # noqa: E402


def select_reference(panel, offset):
    panel.reference_table.setCurrentIndex(
        panel.reference_proxy.mapFromSource(panel.reference_model.index(offset, 0))
    )


def reference_text(panel):
    return panel.reference_model.index(panel._candidate_offset, 0).data()


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
    return Mock(path=path, audio_sha256=sha256_file(path))


class VoiceAuditionPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def finish_task(self, task):
        task.run()
        self.application.processEvents()

    def test_same_owner_references_use_only_numbers(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            base = replace(
                group.candidate_inventory[0],
                source_character="Player candidate Rhiannon 012345abcdef",
            )
            candidates = tuple(
                replace(base, source_id=f"reference:{index}") for index in range(3)
            )
            group = replace(
                group,
                source_id=candidates[0].source_id,
                candidates=candidates,
                candidate_inventory=candidates,
                narrator_candidate=None,
            )
            panel = VoiceAuditionPanel(
                Mock(), preview_service=Mock(), thread_pool=ManualThreadPool()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(replace(plan, groups=(group,)), group_id=group.group_id)
            self.assertEqual(panel.reference_source_names, ("Rhiannon",))
            self.assertEqual(
                [panel.reference_model.item(index, 0).text() for index in range(3)],
                ["1", "2", "3"],
            )
            self.assertLess(panel.reference_table.columnWidth(0), 150)
            self.assertIn(
                "Source: Rhiannon", panel.reference_model.item(0, 0).toolTip()
            )

    def test_mixed_reference_owners_include_speaker_unknown_and_narrator(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            base = group.candidate_inventory[0]
            candidates = (
                replace(
                    base, source_id="reference:rhiannon", source_character="Rhiannon"
                ),
                replace(
                    base,
                    source_id="reference:speaker",
                    source_character="",
                    source_speaker="Speaker name",
                ),
                replace(
                    base,
                    source_id="reference:unknown",
                    source_character="",
                    source_speaker="",
                ),
            )
            narrator = replace(
                base, source_id="reference:narrator", source_character="Centurion"
            )
            group = replace(
                group,
                source_id=candidates[0].source_id,
                candidates=candidates,
                candidate_inventory=candidates,
                narrator_candidate=narrator,
            )
            panel = VoiceAuditionPanel(
                Mock(), preview_service=Mock(), thread_pool=ManualThreadPool()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(replace(plan, groups=(group,)), group_id=group.group_id)
            owners = ("Rhiannon", "Speaker name", "Unknown source", "Centurion")
            self.assertEqual(panel.reference_source_names, owners)
            self.assertEqual(
                [panel.reference_model.item(index, 0).text() for index in range(4)],
                [
                    "1 - Rhiannon",
                    "2 - Speaker name",
                    "3 - Unknown source",
                    "Narrator fallback - Centurion",
                ],
            )
            for index, owner in enumerate(owners):
                self.assertIn(
                    f"Source: {owner}", panel.reference_model.item(index, 0).toolTip()
                )
            panel.reference_search.setText("Speaker name")
            self.assertEqual(panel.reference_proxy.rowCount(), 1)
            panel.reference_table.setCurrentIndex(panel.reference_proxy.index(0, 0))
            self.assertEqual(panel._current_entry()[0].source_id, "reference:speaker")
            self.assertEqual(panel.reference_source_names, owners)

    def test_reference_browser_keeps_full_inventory_and_selection_through_search(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            base = group.candidate_inventory[0]
            candidates = tuple(
                replace(
                    base,
                    source_id=f"reference:{index}",
                    source_excerpts=(f"Full reference sentence {index:03d}",),
                    source_titles=(f"Title {index:03d}",),
                    reference_duration_seconds=index / 10,
                )
                for index in range(500)
            )
            group = replace(
                group,
                source_id=candidates[0].source_id,
                candidates=candidates,
                candidate_inventory=candidates,
                narrator_candidate=None,
            )
            panel = VoiceAuditionPanel(
                Mock(), preview_service=Mock(), thread_pool=ManualThreadPool()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(replace(plan, groups=(group,)), group_id=group.group_id)
            self.assertFalse(hasattr(panel, "voice_reference"))
            self.assertEqual(panel.reference_model.rowCount(), 500)
            self.assertGreaterEqual(panel.reference_table.minimumHeight(), 200)
            selected = panel._current_entry()[0].source_id
            panel.reference_search.setText("Title 499")
            self.assertEqual(panel.reference_proxy.rowCount(), 1)
            self.assertEqual(panel._current_entry()[0].source_id, selected)
            self.assertFalse(panel.reference_table.currentIndex().isValid())
            panel.reference_table.setCurrentIndex(panel.reference_proxy.index(0, 0))
            self.assertEqual(panel._current_entry()[0].source_id, "reference:499")
            self.assertEqual(panel.a_reason.text(), "Full reference sentence 499")
            panel.reference_search.setText("not present")
            self.assertEqual(panel.reference_proxy.rowCount(), 0)
            self.assertEqual(panel._current_entry()[0].source_id, "reference:499")
            panel.reference_search.clear()
            self.assertEqual(
                panel.reference_proxy.mapToSource(
                    panel.reference_table.currentIndex()
                ).row(),
                499,
            )
            self.assertEqual(panel.reference_model.index(499, 2).data(), "49.9 s")

    def test_sorted_filtered_hover_and_preview_keep_source_identity(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            base = group.candidate_inventory[0]
            candidates = tuple(
                replace(
                    base,
                    source_id=f"reference:{index}",
                    source_excerpts=(f"Match {index}",),
                    reference_duration_seconds=duration,
                )
                for index, duration in enumerate((2.0, 11.0, 3.0))
            )
            group = replace(
                group,
                source_id=candidates[0].source_id,
                candidates=candidates,
                candidate_inventory=candidates,
                narrator_candidate=None,
            )
            service = Mock()
            service.generate.return_value = generated_preview(Path(directory))
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                Mock(), preview_service=service, player=Mock(), thread_pool=pool
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(replace(plan, groups=(group,)), group_id=group.group_id)
            panel.reference_table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
            first = panel.reference_proxy.index(0, 0)
            self.assertEqual(first.data(Qt.ItemDataRole.UserRole), "reference:1")
            panel.reference_table.entered.emit(first)
            panel._hover_timer.stop()
            panel._prepare_highlighted_reference()
            self.assertEqual(panel._focus_index, 1)
            self.assertEqual(panel._current_entry()[0].source_id, "reference:0")
            panel.reference_search.setText("Match 2")
            panel.reference_table.setCurrentIndex(panel.reference_proxy.index(0, 0))
            self.assertEqual(
                [value.source_id for value in panel._focus_candidates()],
                ["reference:2"],
            )
            panel.play_a()
            self.finish_task(pool.tasks.pop())
            self.assertEqual(service.generate.call_args.args[2], "reference:2")

    def test_saved_alias_hides_duplicate_comparison_until_reference_changes(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            plan, group = with_second_candidate(plan, group)
            original = group.candidate_inventory[0]
            saved = replace(original, source_id="saved-alias")
            saved_group = replace(
                group,
                source_id=saved.source_id,
                candidates=(saved,),
                candidate_inventory=(saved,),
                narrator_candidate=None,
            )
            panel = VoiceAuditionPanel(
                Mock(), preview_service=Mock(), thread_pool=ManualThreadPool()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.set_current_voice(replace(plan, groups=(saved_group,)), "Saved voice")
            panel.start(plan, group_id=group.group_id)
            self.assertTrue(panel.current_voice_box.isHidden())
            self.assertTrue(panel.a_original.isEnabled())
            self.assertTrue(panel.a_play.isEnabled())
            self.assertFalse(panel.reference_model.item(0, 0).icon().isNull())
            self.assertTrue(panel.reference_model.item(0, 0).font().bold())
            select_reference(panel, 1)
            self.assertFalse(panel.current_voice_box.isHidden())
            select_reference(panel, 0)
            self.assertEqual(panel._current_entry()[0], original)
            self.assertTrue(panel.current_voice_box.isHidden())
            panel.reference_transcript.setText("Different exact words")
            panel._reference_transcript_edited()
            self.assertFalse(panel.current_voice_box.isHidden())
            self.assertTrue(panel.reference_model.item(0, 0).icon().isNull())
            self.assertFalse(panel.reference_model.item(0, 0).font().bold())

    def test_saved_reference_stays_marked_when_preview_configuration_differs(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            candidate = group.candidate_inventory[0]
            saved = replace(candidate, source_id="saved-alias")
            saved_group = replace(
                group,
                source_id=saved.source_id,
                candidates=(saved,),
                candidate_inventory=(saved,),
                narrator_candidate=None,
            )
            panel = VoiceAuditionPanel(
                Mock(), preview_service=Mock(), thread_pool=ManualThreadPool()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.set_current_voice(replace(plan, groups=(saved_group,)), "Saved voice")
            panel.start(plan, group_id=group.group_id)
            self.assertTrue(panel.current_voice_box.isHidden())
            self.assertEqual(panel.title(), "Selected reference")
            for field, value in (
                ("synthesis_backend", "other-backend"),
                ("synthesis_model", "other-model"),
                ("synthesis_language", "other-language"),
                ("synthesis_profile", "other-profile"),
                ("pocket_voice_cloning", not plan.pocket_voice_cloning),
            ):
                with self.subTest(field=field):
                    panel.start(
                        replace(plan, **{field: value}), group_id=group.group_id
                    )
                    self.assertFalse(panel.current_voice_box.isHidden())
                    self.assertEqual(panel.title(), "New voice")
                    self.assertFalse(panel.reference_model.item(0, 0).icon().isNull())
            panel.start(plan, group_id=group.group_id)
            self.assertTrue(panel.current_voice_box.isHidden())
            self.assertEqual(panel.title(), "Selected reference")

    def test_icons_identify_the_active_playback_control(self):
        panel = VoiceAuditionPanel(Mock(), preview_service=Mock(), player=Mock())
        self.addCleanup(panel.deleteLater)
        self.addCleanup(panel.shutdown)
        controls = (
            ("original", panel.a_original),
            ("preview", panel.a_play),
            ("current-original", panel.current_original),
            ("current-preview", panel.current_play),
        )
        for source in (
            None,
            "original",
            "preview",
            "current-original",
            "current-preview",
            None,
        ):
            panel._set_playing_source(source)
            for target, button in controls:
                with self.subTest(source=source, target=target):
                    expected = action_icon("square" if source == target else "play")
                    self.assertEqual(
                        button.icon().pixmap(24).toImage(),
                        expected.pixmap(24).toImage(),
                    )

    def test_one_play_click_generates_if_needed_and_starts_playback(self):
        for cached in (False, True):
            with self.subTest(cached=cached), TemporaryDirectory() as directory:
                root = Path(directory)
                plan, group, _manifest = ambiguous_fixture(root)
                preview = generated_preview(root)
                service, player, decisions = Mock(), Mock(), Mock()
                service.supports_prefetch = cached
                service.peek.return_value = preview if cached else None
                service.generate.return_value = preview
                pool = ManualThreadPool()
                panel = VoiceAuditionPanel(
                    decisions, preview_service=service, player=player, thread_pool=pool
                )
                self.addCleanup(panel.deleteLater)
                self.addCleanup(panel.shutdown)
                panel.start(plan, group_id=group.group_id)
                self.assertEqual(panel.a_play.text(), "Preview")
                panel.a_play.click()
                if not cached:
                    player.play_bytes.assert_not_called()
                    self.assertIn("will play automatically", panel.status.toPlainText())
                    pool.run_next()
                    self.application.processEvents()
                    service.generate.assert_called_once()
                else:
                    service.generate.assert_not_called()
                player.play_bytes.assert_called_once()
                self.assertEqual(panel.a_play.text(), "Preview")
                self.assertTrue(panel.a_use.isEnabled())
                decisions.remember_many.assert_not_called()

    def test_reference_selection_then_play_uses_ready_audio_without_saving(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            service, player, decisions = Mock(), Mock(), Mock()
            service.supports_prefetch = True
            service.peek.return_value = generated_preview(root)
            panel = VoiceAuditionPanel(
                decisions, preview_service=service, player=player
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=plan.groups[0].group_id)
            select_reference(panel, 1)
            player.play_bytes.assert_not_called()
            panel.a_play.click()
            player.play_bytes.assert_called_once()
            select_reference(panel, 0)
            self.assertEqual(player.play_bytes.call_count, 1)
            panel.a_play.click()
            self.assertEqual(player.play_bytes.call_count, 2)
            panel._stop_playback()
            self.assertEqual(panel.a_play.text(), "Preview")
            service.generate.assert_not_called()
            decisions.remember_many.assert_not_called()
            panel._set_decision_actions(False)
            self.assertFalse(panel.a_play.isEnabled())

    def test_preview_actions_fit_compact_width_with_larger_font(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            panel = VoiceAuditionPanel(Mock(), preview_service=Mock())
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            font = panel.font()
            font.setPointSizeF(font.pointSizeF() * 2)
            panel.setFont(font)
            panel.start(plan, group_id=group.group_id)
            panel.resize(620, 440)
            panel.show()
            self.application.processEvents()

            self.assertLessEqual(panel.phrase_box.minimumSizeHint().width(), 600)
            self.assertLess(
                panel.preview_text.geometry().right(),
                panel.game_phrase_button.geometry().left(),
            )

    def test_browsing_during_render_cancels_request_without_stale_playback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            service, player, pool = Mock(), Mock(), ManualThreadPool()
            service.generate.return_value = generated_preview(root)
            panel = VoiceAuditionPanel(
                Mock(), preview_service=service, player=player, thread_pool=pool
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.play_a()
            request = panel._preview_request
            self.assertTrue(panel.reference_table.isEnabled())
            select_reference(panel, 1)
            self.assertTrue(request.cancellation.is_set())
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertIs(
                service.generate.call_args.kwargs["cancel_event"], request.cancellation
            )
            player.play_bytes.assert_not_called()
            self.assertFalse(panel.a_use.isEnabled())

    def test_prefetched_sample_is_playable_during_render_but_needs_audition(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            service, player, pool = Mock(), Mock(), ManualThreadPool()
            service.supports_prefetch = True
            service.peek.return_value = None
            panel = VoiceAuditionPanel(
                Mock(), preview_service=service, player=player, thread_pool=pool
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=plan.groups[0].group_id)
            self.assertLessEqual(len(service.prefetch.call_args.args[1]), 3)
            panel.play_a()
            select_reference(panel, 1)
            service.peek.return_value = generated_preview(root)
            panel._refresh_runtime()
            self.assertEqual(panel.a_play.text(), "Preview")
            self.assertEqual(panel.a_play.accessibleName(), "Play voice preview")
            self.assertFalse(panel.a_use.isEnabled())
            panel.play_a()
            player.play_bytes.assert_called_once()
            self.assertTrue(panel.a_use.isEnabled())
            self.assertTrue(panel.preview_runner.active)
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(player.play_bytes.call_count, 1)
            panel.use_a()
            service.cancel.assert_called_once_with()
            pool.tasks.pop().run()
            self.application.processEvents()

    def test_replacing_panel_session_ignores_previous_worker(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _group, _manifest = ambiguous_fixture(root)
            service, player, pool = Mock(), Mock(), ManualThreadPool()
            service.generate.return_value = generated_preview(root)
            panel = VoiceAuditionPanel(
                Mock(), preview_service=service, player=player, thread_pool=pool
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.play_a()
            old_request = panel._preview_request
            panel.start(plan, group_id=plan.groups[0].group_id)
            self.assertTrue(old_request.cancellation.is_set())
            pool.tasks.pop().run()
            self.application.processEvents()
            player.play_bytes.assert_not_called()
            self.assertFalse(panel.a_use.isEnabled())

    def test_shutdown_panel_replaces_injected_shared_session_before_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _group, _manifest = ambiguous_fixture(root)
            old, replacement = Mock(), Mock()
            old.supports_prefetch = replacement.supports_prefetch = True
            old.peek.return_value = replacement.peek.return_value = None
            old.new_session.return_value = replacement
            panel = VoiceAuditionPanel(Mock(), preview_service=old, player=Mock())
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.shutdown()
            old.close.assert_called_once_with()
            panel.start(plan, group_id=plan.groups[0].group_id)
            self.assertIs(panel.preview_service, replacement)
            old.new_session.assert_called_once_with()
            replacement.prefetch.assert_called_once()

    def test_latest_play_request_starts_after_canceled_worker_finishes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            service, player, pool = Mock(), Mock(), ManualThreadPool()
            service.generate.return_value = generated_preview(root)
            panel = VoiceAuditionPanel(
                Mock(), preview_service=service, player=player, thread_pool=pool
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.play_a()
            first = panel._preview_request
            select_reference(panel, 1)
            panel.play_a()
            superseded = panel._queued_preview_request
            select_reference(panel, 0)
            panel.play_a()
            latest = panel._queued_preview_request
            self.assertTrue(first.cancellation.is_set())
            self.assertTrue(superseded.cancellation.is_set())
            pool.tasks.pop().run()
            self.application.processEvents()
            player.play_bytes.assert_not_called()
            self.assertIs(panel._preview_request, latest)
            self.assertEqual(len(pool.tasks), 1)
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(service.generate.call_count, 2)
            self.assertIs(
                service.generate.call_args.kwargs["cancel_event"], latest.cancellation
            )
            self.assertEqual(
                service.generate.call_args.args[2], group.candidates[0].source_id
            )
            player.play_bytes.assert_called_once()

    def test_cancel_and_reopen_releases_shared_session_ready_pins(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _group, _manifest = ambiguous_fixture(root)
            owner = VoiceAuditionCoordinator(
                Mock(root=root), background_available=lambda: False
            )
            self.addCleanup(owner.close)
            panel = VoiceAuditionPanel(
                Mock(), preview_service=owner.session(), player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            for _ in range(3):
                panel.start(plan, group_id=plan.groups[0].group_id)
                session = panel.preview_service
                session._ready["unused"] = (generated_preview(root), ())
                self.assertEqual(len(owner._sessions), 1)
                panel.cancel()
                self.assertEqual(len(owner._sessions), 0)
                self.assertEqual(session._ready, {})
                self.assertTrue(session._closed)

    def test_selected_group_uses_its_game_sample_without_other_groups(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, second = with_second_group(plan, group)
            second = replace(second, sample_text="A different line from another story.")
            plan = replace(plan, groups=(group, second))
            service, pool = Mock(), ManualThreadPool()
            service.generate.return_value = generated_preview(root)
            service.supports_prefetch = True
            service.peek.return_value = None
            panel = VoiceAuditionPanel(
                Mock(), preview_service=service, player=Mock(), thread_pool=pool
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=second.group_id)
            self.assertEqual(
                service.prefetch.call_args.args[1][0],
                (second, second.candidates[0].source_id, second.sample_text),
            )
            self.assertTrue(
                all(
                    request[0] == second
                    for request in service.prefetch.call_args.args[1]
                )
            )
            self.assertEqual(panel.preview_text.toPlainText(), second.sample_text)
            panel.play_a()
            self.finish_task(pool.tasks.pop())
            self.assertEqual(
                service.generate.call_args.kwargs["text"], second.sample_text
            )

    def test_unknown_language_keeps_story_phrase_as_default(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            panel = VoiceAuditionPanel(Mock(), preview_service=Mock(), player=Mock())
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(
                replace(plan, synthesis_language="fr"), group_id=plan.groups[0].group_id
            )
            self.assertEqual(panel.preview_text.toPlainText(), group.sample_text)
            self.assertTrue(panel.game_phrase_button.isEnabled())

    def test_missing_game_text_uses_editable_comparison_fallback(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            candidates = tuple(
                replace(candidate, source_excerpts=(), reference_transcript=None)
                for candidate in group.candidates
            )
            group = replace(
                group,
                sample_text="",
                alternate_sample_text=None,
                candidates=candidates,
                candidate_inventory=candidates,
            )
            panel = VoiceAuditionPanel(Mock(), preview_service=Mock(), player=Mock())
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(
                replace(plan, groups=(group,)), group_id=plan.groups[0].group_id
            )
            self.assertEqual(panel.preview_text.toPlainText(), COMPARISON_PREVIEW_TEXT)
            self.assertFalse(panel.game_phrase_button.isEnabled())
            panel.preview_text.setPlainText("My own phrase.")
            self.assertEqual(panel.preview_text.toPlainText(), "My own phrase.")

    def test_game_phrase_button_cycles_all_unique_game_excerpts_without_playing(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            plan, group = with_second_candidate(plan, group)
            candidates = (
                replace(
                    group.candidates[0],
                    source_excerpts=(group.sample_text, "Third game phrase.", ""),
                    reference_transcript=None,
                ),
                replace(
                    group.candidates[1],
                    source_excerpts=("Third game phrase.", "Fourth game phrase."),
                    reference_transcript=None,
                ),
            )
            group = replace(
                group, candidates=candidates, candidate_inventory=candidates
            )
            service, player = Mock(), Mock()
            panel = VoiceAuditionPanel(Mock(), preview_service=service, player=player)
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(
                replace(plan, groups=(group,)), group_id=plan.groups[0].group_id
            )
            self.assertEqual(panel.preview_text.toPlainText(), group.sample_text)
            for text in (
                group.alternate_sample_text,
                "Third game phrase.",
                "Fourth game phrase.",
                group.sample_text,
            ):
                panel.game_phrase_button.click()
                self.assertEqual(panel.preview_text.toPlainText(), text)
            panel.preview_text.setPlainText("My own phrase.")
            panel.game_phrase_button.click()
            self.assertEqual(panel.preview_text.toPlainText(), group.sample_text)
            service.generate.assert_not_called()
            player.play_bytes.assert_not_called()

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
                source_excerpts=("Good morning, traveller.",),
                source_titles=("Morning",),
                source_bank="hero3146_mainvoc.bnk",
                source_line_ids=("playable-voice:3146:main:1",),
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

            tooltip = panel.reference_model.index(panel._candidate_offset, 0).data(
                Qt.ItemDataRole.ToolTipRole
            )
            self.assertIn("Good morning", tooltip)
            self.assertIn("Playable line", tooltip)
            self.assertIn("Morning", tooltip)
            self.assertNotIn("abcdef123456", reference_text(panel))
            self.assertNotIn("abcdef123456", panel.a_reason.text())
            self.assertEqual(panel.a_reason.text(), "Good morning, traveller.")
            self.assertNotIn("Greeting:", panel.a_reason.text())
            panel.cancel()

    def test_full_preview_text_is_literal_copyable_and_updates_with_phrase(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            text = (
                "<b>This is spoken literally</b>. "
                + "A long sentence for the traveller. " * 8
            ).strip()
            group = replace(group, sample_text=text)
            plan = replace(plan, groups=(group,))
            panel = VoiceAuditionPanel(Mock(), preview_service=Mock())
            self.addCleanup(panel.deleteLater)
            panel.start(plan, group_id=group.group_id)
            self.assertEqual(panel.preview_text.toPlainText(), text)
            self.assertFalse(panel.preview_text.isReadOnly())
            panel.preview_text.setFocus()
            panel.preview_text.selectAll()
            QTest.keySequence(
                panel.preview_text, QKeySequence(QKeySequence.StandardKey.Copy)
            )
            self.assertEqual(self.application.clipboard().text(), text)
            panel.preview_text.setPlainText(group.alternate_sample_text)
            self.assertEqual(
                panel.preview_text.toPlainText(), group.alternate_sample_text
            )
            panel.cancel()

    def test_editor_has_no_technical_reference_or_speech_check_panes(self):
        panel = VoiceAuditionPanel(Mock(), preview_service=Mock(), player=Mock())
        self.addCleanup(panel.deleteLater)
        self.addCleanup(panel.shutdown)
        for name in (
            "reference_details_toggle",
            "reference_details",
            "advice_details",
            "runtime",
            "summary",
            "character",
            "scope",
            "auto_button",
            "choose_all_button",
            "preview_box",
            "_comparison_mode",
            "_inspection_mode",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(panel, name))

    def test_start_requires_one_matching_character(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            panel = VoiceAuditionPanel(Mock(), preview_service=Mock(), player=Mock())
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            for groups in ((), (group, group)):
                with self.subTest(groups=groups):
                    with self.assertRaisesRegex(
                        VoiceAuditionUIError, "one matching character"
                    ):
                        panel.start(
                            replace(plan, groups=groups), group_id=group.group_id
                        )
            self.assertTrue(panel.isHidden())

    def test_multiple_references_name_the_first_playback_sample(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            candidate = replace(
                group.candidates[0],
                reference_sha256s=(*group.candidates[0].reference_sha256s, "f" * 64),
            )
            group = replace(
                group, candidates=(candidate,), candidate_inventory=(candidate,)
            )
            plan = replace(plan, groups=(group,))
            panel = VoiceAuditionPanel(Mock(), preview_service=Mock(), player=Mock())
            self.addCleanup(panel.deleteLater)
            panel.start(plan, group_id=group.group_id)
            self.assertEqual(panel.a_original.text(), "Original")
            self.assertIn("first original reference", panel.a_original.toolTip())
            panel._set_playing_source("original")
            self.assertEqual(panel.a_original.text(), "Original")
            panel._set_playing_source(None)
            self.assertEqual(panel.a_original.text(), "Original")
            panel.cancel()

    def test_inspection_keeps_selected_story_voice_then_shows_menu_references(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            story = replace(
                group.candidates[0],
                source_id="character:story",
                source_bank="story.bnk",
                source_line_ids=("story:1",),
            )
            other_story = replace(
                story,
                source_id="character:other-story",
                source_excerpts=("Other story line.",),
            )
            menu = replace(
                story,
                source_id="character:menu",
                source_bank="hero3145_mainvoc.bnk",
                source_line_ids=("playable-voice:3145:1314513:22",),
                source_titles=("Hobby",),
                source_excerpts=("Original hobby line.",),
            )
            group = replace(
                group,
                source_id=story.source_id,
                candidates=(story,),
                candidate_inventory=(story, other_story, menu),
            )
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(root / "decisions.json"),
                preview_service=Mock(),
            )
            self.addCleanup(panel.deleteLater)
            panel.start(replace(plan, groups=(group,)), group_id=group.group_id)

            self.assertIn(
                "Hobby",
                panel.reference_model.index(1, 0).data(Qt.ItemDataRole.ToolTipRole),
            )
            self.assertIn(
                "Other story line",
                panel.reference_model.index(2, 0).data(Qt.ItemDataRole.ToolTipRole),
            )
            panel.cancel()

    def test_unlinked_game_bank_reference_is_labelled_without_a_transcript(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            candidate = replace(
                group.candidates[0],
                source_excerpts=(),
                source_line_ids=(),
                candidate_origin="exact_bank_unrouted_media",
            )
            group = replace(
                group,
                candidates=(),
                candidate_inventory=(candidate,),
            )
            plan = replace(plan, groups=(group,))
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(root / "decisions.json"),
                preview_service=Mock(),
            )
            self.addCleanup(panel.deleteLater)
            panel.start(plan, group_id=group.group_id)

            self.assertIn(
                "Unlinked game-bank audio",
                panel.reference_model.index(panel._candidate_offset, 0).data(
                    Qt.ItemDataRole.ToolTipRole
                ),
            )
            self.assertIn("Transcript unavailable", panel.a_reason.text())
            panel.cancel()

    def test_shutdown_waits_for_save_without_emitting_terminal_signals(self):
        for save_error, preview_active, save_first in (
            (None, False, False),
            (OSError("save failed"), False, False),
            (None, True, False),
            (None, True, True),
        ):
            with self.subTest(
                error=save_error, preview=preview_active, save_first=save_first
            ):
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    plan, _group, _manifest = ambiguous_fixture(root)
                    decisions = Mock()
                    decisions.remember_many.side_effect = save_error
                    service = Mock()
                    service.generate.side_effect = VoiceAuditionCancelled("cancelled")
                    pool = ManualThreadPool()
                    panel = VoiceAuditionPanel(
                        decisions,
                        preview_service=service,
                        thread_pool=pool,
                        player=Mock(),
                    )
                    self.addCleanup(panel.deleteLater)
                    completed, failed = Mock(), Mock()
                    panel.completed.connect(completed)
                    panel.saveFailed.connect(failed)
                    panel.start(plan, group_id=plan.groups[0].group_id)
                    if preview_active:
                        panel.a_play.click()
                    panel._record_choice(plan.groups[0].candidates[0].source_id)
                    panel.shutdown()
                    service.close.assert_not_called()
                    tasks = list(reversed(pool.tasks)) if save_first else pool.tasks
                    for index, task in enumerate(tasks):
                        task.run()
                        self.application.processEvents()
                        if index < len(tasks) - 1:
                            service.close.assert_not_called()
                    service.close.assert_called_once_with()
                    completed.assert_not_called()
                    failed.assert_not_called()
                    self.assertFalse(panel.retry_save_button.isVisible())

    def test_cached_preview_playback_failure_cannot_accept_the_voice(self):
        for failure in ("changed", "missing", "unplayable"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                root = Path(directory)
                plan, group, _manifest = ambiguous_fixture(root)
                preview = generated_preview(root)
                service, decisions, player = Mock(), Mock(), Mock()
                service.generate.return_value = preview
                pool = ManualThreadPool()
                panel = VoiceAuditionPanel(
                    decisions, preview_service=service, thread_pool=pool, player=player
                )
                self.addCleanup(panel.deleteLater)
                self.addCleanup(panel.shutdown)
                panel.start(plan, group_id=plan.groups[0].group_id)
                panel.a_play.click()
                pool.tasks.pop().run()
                self.application.processEvents()
                self.assertTrue(panel.a_use.isEnabled())
                panel._stop_player()
                if failure == "changed":
                    preview.path.write_bytes(clean_wav_bytes(amplitude=0.2))
                elif failure == "missing":
                    preview.path.unlink()
                else:
                    player.play_bytes.return_value = None
                player.reset_mock()
                panel.a_play.click()
                self.assertFalse(panel.a_use.isEnabled())
                panel.preview_text.setPlainText(group.alternate_sample_text)
                panel.preview_text.setPlainText(group.sample_text)
                self.assertFalse(panel.a_use.isEnabled())
                panel.a_use.click()
                decisions.remember_many.assert_not_called()
                self.assertEqual(pool.tasks, [])
                if failure != "unplayable":
                    player.play_bytes.assert_not_called()
                    self.assertEqual(panel.a_play.text(), "Preview")
                else:
                    player.play_bytes.return_value = object()
                    panel.a_play.click()
                    self.assertFalse(panel.a_use.isEnabled())
                    pool.run_next()
                    self.application.processEvents()
                    self.assertTrue(panel.a_use.isEnabled())
                    self.assertEqual(service.generate.call_count, 2)

    def test_original_plays_verified_snapshot_even_if_file_changes_after_preparation(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            service = Mock()
            verifier = VoiceAuditionPreviewService()
            self.addCleanup(verifier.close)

            def replace_reference(*arguments):
                reference = verifier.reference_audio(*arguments)
                reference.path.write_bytes(clean_wav_bytes(amplitude=0.2))
                return reference

            service.reference_audio.side_effect = replace_reference
            player, decisions = Mock(), Mock()
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=service, player=player, thread_pool=pool
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=group.group_id)
            panel.a_original.click()
            player.play_bytes.assert_not_called()
            self.assertFalse(panel.a_use.isEnabled())
            service.reference_audio.assert_not_called()
            pool.run_next()
            self.application.processEvents()
            player.play_bytes.assert_called_once()
            self.assertEqual(player.play_bytes.call_args.args[0], clean_wav_bytes())
            self.assertTrue(panel.a_use.isEnabled())
            service.generate.assert_not_called()
            decisions.remember_many.assert_not_called()

    def test_cancelled_preview_completion_preserves_pending_original_status(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, _group, manifest = ambiguous_fixture(root)
            service = Mock(spec=VoiceAuditionPreviewService)
            service.generate.return_value = generated_preview(root)
            reference = manifest.parent / "references" / "rhiannon.wav"
            service.reference_audio.return_value = VoiceAuditionReference(
                reference, reference.read_bytes()
            )
            pool, player = ManualThreadPool(), Mock()
            panel = VoiceAuditionPanel(
                Mock(), preview_service=service, player=player, thread_pool=pool
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()
            panel.a_original.click()
            pool.run_next()
            self.application.processEvents()
            player.play_bytes.assert_not_called()
            self.assertEqual(
                panel.status.toPlainText(), "Preparing original reference..."
            )
            self.assertEqual(panel.a_original.text(), "Original")
            pool.run_next()
            self.application.processEvents()
            player.play_bytes.assert_called_once_with(
                reference.read_bytes(), source=str(reference)
            )

    def test_pending_original_can_be_stopped_or_superseded_without_late_playback(self):
        for action in (
            "stop",
            "select",
            "phrase",
            "preview",
            "cancel",
            "shutdown",
            "restart",
            "save",
        ):
            with self.subTest(action=action), TemporaryDirectory() as directory:
                root = Path(directory)
                plan, group, manifest = ambiguous_fixture(root)
                plan, group = with_second_candidate(plan, group)
                reference = manifest.parent / "references" / "rhiannon.wav"
                service = Mock(spec=VoiceAuditionPreviewService)
                service.reference_audio.return_value = VoiceAuditionReference(
                    reference, reference.read_bytes()
                )
                service.generate.return_value = generated_preview(root)
                pool, player = ManualThreadPool(), Mock()
                panel = VoiceAuditionPanel(
                    Mock(), preview_service=service, player=player, thread_pool=pool
                )
                self.addCleanup(panel.deleteLater)
                self.addCleanup(panel.shutdown)
                panel.start(plan, group_id=plan.groups[0].group_id)
                panel.a_original.click()
                self.assertTrue(panel.original_runner.active)
                self.assertEqual(panel.a_original.text(), "Original")
                self.assertFalse(panel.a_use.isEnabled())
                service.reference_audio.assert_not_called()
                # UI actions remain usable while the disk task has not finished.
                if action == "stop":
                    panel.a_original.click()
                elif action == "select":
                    select_reference(panel, 1)
                elif action == "phrase":
                    panel.preview_text.setPlainText(group.alternate_sample_text)
                elif action == "preview":
                    panel.a_play.click()
                elif action == "restart":
                    panel.start(plan, group_id=plan.groups[0].group_id)
                elif action == "save":
                    panel._record_choice(group.candidates[0].source_id)
                else:
                    getattr(panel, action)()
                self.assertFalse(panel.original_runner.active)
                pool.run_next()
                self.application.processEvents()
                player.play_bytes.assert_not_called()
                self.assertIsNone(panel._accepted_choice)
                while pool.tasks:
                    pool.run_next()
                    self.application.processEvents()
                if action == "preview":
                    player.play_bytes.assert_called_once()
                    self.assertEqual(panel.a_play.text(), "Preview")
                else:
                    player.play_bytes.assert_not_called()

    def test_retry_after_shutdown_gets_a_usable_preview_service(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan, _group = with_second_candidate(plan, group)
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                VoiceDecisionStore(root / "decisions.json"),
                player=Mock(),
                thread_pool=pool,
            )
            previous = panel.preview_service
            self.assertFalse(panel.runtime_timer.isActive())
            panel.shutdown()
            panel.start(plan, group_id=plan.groups[0].group_id)
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
            verifier = VoiceAuditionPreviewService()
            self.addCleanup(verifier.close)
            preview_service.reference_audio.side_effect = verifier.reference_audio
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions,
                preview_service=preview_service,
                player=Mock(),
                thread_pool=pool,
            )

            panel.start(plan, group_id=group.group_id)

            self.assertEqual(panel.a_play.text(), "Preview")
            self.assertEqual(panel.a_use.text(), "Save")
            self.assertEqual(panel.reference_model.rowCount(), 2)
            self.assertTrue(reference_text(panel).startswith("1"))
            self.assertIn(
                "No transcript",
                panel.reference_model.index(panel._candidate_offset, 0).data(
                    Qt.ItemDataRole.ToolTipRole
                ),
            )
            alternative = unresolved.candidate_inventory[1]
            select_reference(panel, 1)
            self.assertTrue(reference_text(panel).startswith("2"))
            self.assertEqual(panel.a_box.title(), "")
            panel.a_original.click()
            self.finish_task(pool.tasks.pop())
            preview_service.reference_audio.assert_called_once_with(
                plan, group, alternative.source_id
            )
            panel.a_use.click()
            self.finish_task(pool.tasks.pop())
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
                return Mock(path=path, audio_sha256=sha256_file(path))

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

            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()
            self.assertFalse(panel.a_use.isEnabled())
            self.assertTrue(panel.reference_table.isEnabled())
            self.assertTrue(panel.preview_text.isEnabled())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(panel.a_use.isEnabled())
            self.assertTrue(reference_text(panel).startswith("1"))
            self.assertEqual(preview_service.generate.call_count, 1)
            panel.a_play.click()
            self.assertEqual(panel.a_play.text(), "Preview")
            panel.a_play.click()
            self.assertIn("Starting", panel.status.toPlainText())
            panel._playback_state_changed(QtPcmPlayer.PlaybackState.PlayingState)
            self.assertIn("Playing", panel.status.toPlainText())
            select_reference(panel, 1)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.a_play.click()
            panel.a_play.click()
            self.assertEqual(player.play_bytes.call_count, 4)
            select_reference(panel, 0)
            self.assertTrue(panel.a_use.isEnabled())
            select_reference(panel, 1)
            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(preview_service.generate.call_count, 2)
            panel.a_use.click()
            self.assertIn("Saving", panel.status.toPlainText())
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
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(panel.a_use.isEnabled())
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
            panel.start(plan, group_id=plan.groups[0].group_id)
            self.assertFalse(panel.a_use.isEnabled())
            panel.a_original.click()
            self.finish_task(pool.tasks.pop())
            self.assertTrue(panel.a_use.isEnabled())
            panel.a_use.click()
            self.finish_task(pool.tasks.pop())
            service.generate.assert_not_called()
            decisions.remember_many.assert_called_once_with(
                ((group, group.candidates[0].source_id),)
            )

    def test_unavailable_audio_runtime_blocks_preview_and_original_before_preparation(
        self,
    ):
        for original in (False, True):
            with self.subTest(original=original), TemporaryDirectory() as directory:
                root = Path(directory)
                plan, group, _manifest = ambiguous_fixture(root)
                service = Mock(spec=VoiceAuditionPreviewService)
                pool = ManualThreadPool()
                panel = VoiceAuditionPanel(
                    Mock(), preview_service=service, thread_pool=pool
                )
                self.addCleanup(panel.deleteLater)
                self.addCleanup(panel.shutdown)
                panel.start(plan, group_id=group.group_id)
                message = (
                    "Rust audio worker is missing; run cargo build --release --locked"
                )
                with patch(
                    "vntts.rust_audio.check_audio_runtime",
                    side_effect=RuntimeError(message),
                ):
                    (panel.a_original if original else panel.a_play).click()
                self.assertFalse(panel.active)
                self.assertIn(message, panel.status.toPlainText())
                self.assertFalse(panel.a_use.isEnabled())
                service.generate.assert_not_called()
                service.reference_audio.assert_not_called()
                self.assertIsInstance(panel.player, QtPcmPlayer)
                panel.player = Mock()
                (panel.a_original if original else panel.a_play).click()
                self.assertTrue(panel.active)
                self.assertTrue(pool.tasks)

    def test_worker_start_failure_clears_preview_and_original_playback_state(self):
        for original in (False, True):
            with self.subTest(original=original), TemporaryDirectory() as directory:
                root = Path(directory)
                plan, group, _manifest = ambiguous_fixture(root)
                service = Mock(spec=VoiceAuditionPreviewService)
                service.generate.return_value = generated_preview(root)
                service.reference_audio.side_effect = (
                    VoiceAuditionPreviewService().reference_audio
                )
                pool = ManualThreadPool()
                player = QtPcmPlayer(
                    player_factory=Mock(
                        side_effect=RuntimeError("worker protocol mismatch")
                    )
                )
                panel = VoiceAuditionPanel(
                    Mock(), preview_service=service, thread_pool=pool, player=player
                )
                self.addCleanup(player.deleteLater)
                self.addCleanup(panel.deleteLater)
                self.addCleanup(panel.shutdown)
                panel.start(plan, group_id=group.group_id)
                (panel.a_original if original else panel.a_play).click()
                self.finish_task(pool.tasks.pop())
                self.assertFalse(panel.active)
                self.assertIsNone(panel._playing_source)
                self.assertFalse(panel.a_use.isEnabled())
                self.assertIn("worker protocol mismatch", panel.status.toPlainText())
                self.assertEqual(panel.a_play.text(), "Preview")
                self.assertNotIn("Stop", panel.a_original.text())

    def test_failed_preview_can_recover_through_original_or_preview_retry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            service = Mock(spec=VoiceAuditionPreviewService)
            service.reference_audio.side_effect = (
                VoiceAuditionPreviewService().reference_audio
            )
            service.generate.side_effect = [
                RuntimeError("engine unavailable"),
                generated_preview(root),
            ]
            player = Mock()
            player.play_bytes.return_value = None
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                Mock(), preview_service=service, thread_pool=pool, player=player
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=group.group_id)
            panel.a_original.click()
            self.finish_task(pool.tasks.pop())
            self.assertFalse(panel.a_use.isEnabled())
            player.play_bytes.return_value = object()
            panel.a_original.click()
            self.finish_task(pool.tasks.pop())
            self.assertTrue(panel.a_use.isEnabled())
            panel.a_play.click()
            with patch("vntts.support.record_game_import"):
                self.finish_task(pool.tasks.pop())
            self.assertIn(
                "Listen to the original reference", panel.status.toPlainText()
            )
            self.assertIn("Preparation failed", reference_text(panel))
            self.assertNotIn("Preparing", reference_text(panel))
            self.assertIn("engine unavailable", panel.status.toPlainText())
            self.assertTrue(panel.a_original.isEnabled())
            panel.a_original.click()
            self.finish_task(pool.tasks.pop())
            self.assertTrue(panel.a_use.isEnabled())
            panel.a_play.click()
            self.finish_task(pool.tasks.pop())
            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(panel.a_play.text(), "Preview")
            panel.a_play.click()
            self.assertEqual(panel.a_play.text(), "Preview")
            self.assertTrue(panel.a_use.isEnabled())

    def test_keyboard_can_move_from_preview_text_to_game_phrase(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            panel = VoiceAuditionPanel(Mock(), preview_service=Mock())
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=group.group_id)
            panel.show()
            panel.activateWindow()
            self.application.processEvents()
            panel.preview_text.setFocus()
            self.assertIs(self.application.focusWidget(), panel.preview_text)
            QTest.keyClick(panel.preview_text, Qt.Key.Key_Tab)
            self.assertIs(self.application.focusWidget(), panel.game_phrase_button)

    def test_long_status_is_copyable_scrollable_and_scales_with_font(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            panel = VoiceAuditionPanel(Mock(), preview_service=Mock())
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=group.group_id)
            panel.show()
            message = "Unable to prepare preview: " + "diagnostic detail " * 200
            height = panel.status.height()
            panel._set_status(message)
            self.application.processEvents()
            self.assertEqual(panel.status.height(), height)
            self.assertTrue(panel.status.isReadOnly())
            self.assertGreater(panel.status.verticalScrollBar().maximum(), 0)
            panel.status.selectAll()
            panel.status.copy()
            self.assertEqual(self.application.clipboard().text(), message)
            font = panel.font()
            font.setPointSizeF(font.pointSizeF() * 2)
            panel.setFont(font)
            self.application.processEvents()
            self.assertGreater(panel.status.height(), height)
            self.assertEqual(panel.status.toPlainText(), message)

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
            service.generate.return_value = Mock(
                path=generated, audio_sha256=sha256_file(generated)
            )
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
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()
            self.finish_task(pool.tasks.pop())
            for index, reference in enumerate((reference_a, reference_b)):
                if index:
                    select_reference(panel, index)
                    panel.a_play.click()
                    self.finish_task(pool.tasks.pop())
                calls_before = service.generate.call_count
                self.assertTrue(panel.a_original.isEnabled())
                with patch("vntts.support.record_game_import") as report:
                    panel.a_original.click()
                    self.finish_task(pool.tasks.pop())
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
            select_reference(panel, 2)
            panel.a_play.click()
            self.application.processEvents()
            panel.a_original.click()
            self.finish_task(pool.tasks.pop())
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
            self.finish_task(pool.tasks.pop())
            player.play_bytes.assert_not_called()
            self.assertIn("changed", panel.status.toPlainText())
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
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            for index in range(panel.reference_model.rowCount()):
                select_reference(panel, index)
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

            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(
                all(
                    call.kwargs["text"] == group.sample_text
                    for call in preview_service.generate.call_args_list
                )
            )

            panel.preview_text.setPlainText(group.alternate_sample_text)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(
                panel.preview_text.toPlainText(), group.alternate_sample_text
            )
            self.assertTrue(panel.a_use.isEnabled())
            alternate_calls = preview_service.generate.call_args_list[-1:]
            self.assertEqual(
                [call.kwargs["text"] for call in alternate_calls],
                [group.alternate_sample_text],
            )
            panel.preview_text.setPlainText(group.sample_text)
            self.assertEqual(panel.preview_text.toPlainText(), group.sample_text)
            self.assertTrue(panel.a_use.isEnabled())
            panel.preview_text.setPlainText(group.alternate_sample_text)
            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(preview_service.generate.call_count, 2)
            panel.shutdown()
            panel.deleteLater()

    def test_only_requested_samples_generate_and_cached_candidate_can_be_accepted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, manifest = ambiguous_fixture(root)
            plan, group = with_second_candidate(plan, group)
            plan, _second = with_second_group(plan, group)
            decisions = Mock()
            previews = Mock()
            previews.generate.return_value = generated_preview(root)
            reference = manifest.parent / "references" / "rhiannon.wav"
            previews.reference_audio.return_value = VoiceAuditionReference(
                reference, reference.read_bytes()
            )
            pool = ManualThreadPool()
            panel = VoiceAuditionPanel(
                decisions, preview_service=previews, thread_pool=pool, player=Mock()
            )
            self.addCleanup(panel.deleteLater)
            self.addCleanup(panel.shutdown)
            panel.start(plan, group_id=plan.groups[0].group_id)
            previews.generate.assert_not_called()
            self.assertEqual(pool.tasks, [])
            panel.a_original.click()
            self.finish_task(pool.tasks.pop())
            previews.generate.assert_not_called()
            previews.reference_audio.assert_called_once_with(
                plan, group, group.candidates[0].source_id
            )
            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(pool.tasks, [])
            panel.a_play.click()
            self.finish_task(pool.tasks.pop())
            self.assertEqual(pool.tasks, [])
            select_reference(panel, 1)
            self.assertFalse(panel.a_use.isEnabled())
            select_reference(panel, 0)
            panel.a_play.click()
            self.assertTrue(panel.a_use.isEnabled())
            self.assertEqual(previews.generate.call_count, 1)
            panel.a_use.click()
            self.assertFalse(panel.a_use.isEnabled())
            self.finish_task(pool.tasks.pop())
            decisions.remember_many.assert_called_once_with(
                ((group, group.candidates[0].source_id),)
            )
            self.assertTrue(panel.isHidden())

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
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            select_reference(panel, 1)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            panel.preview_text.setPlainText(group.alternate_sample_text)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(
                previews.generate.call_args.args[2], group.candidates[1].source_id
            )
            self.assertFalse(panel.a_use.isEnabled())
            self.assertIn("failed", panel.status.toPlainText())
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
            panel.start(plan, group_id=plan.groups[0].group_id)
            select_reference(panel, 1)
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
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            panel.a_use.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertIn("Unable to save", panel.status.toPlainText())
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
            self.assertIn("Finishing", panel.status.toPlainText())
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
            panel.start(plan, group_id=plan.groups[0].group_id)
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
            self.assertIn("failed", panel.status.toPlainText())
            decisions.remember_many.assert_not_called()
            select_reference(panel, 1)
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

            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            select_reference(panel, panel.reference_model.rowCount() - 1)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(reference_text(panel).startswith("Narrator fallback"))
            self.assertEqual(panel.a_box.title(), "")
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
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()

            panel.cancel()
            self.assertEqual(cancelled.call_count, 0)
            preview_service.cancel.assert_not_called()
            self.assertTrue(panel._preview_request.cancellation.is_set())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(cancelled.call_count, 1)
            preview_service.close.assert_not_called()
            panel.start(plan, group_id=plan.groups[0].group_id)
            panel.a_play.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertIn("Preview preparation stopped", panel.status.toPlainText())
            self.assertNotIn("Preparing", panel.status.toPlainText())
            self.assertNotIn("service is closed", panel.status.toPlainText())
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

    def _story_voice_dialog(self, root):
        content = inspect_story_index(write_story_index(root / "content"))
        plan, group, _manifest = ambiguous_fixture(root / "voice-fixture")
        plan, group = with_second_candidate(plan, group)
        voice_plan_store = Mock()
        voice_plan_store.create.return_value = plan
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
        input_store.materialize.return_value = Mock(ready_items=1, progress_items=1)
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
            dialog.stories.item(0).setCheckState(0, Qt.CheckState.Checked)
            dialog._voice_plan = plan
            dialog._prepared_voice_manifest = root / "prepared-voices.json"
            dialog._prepared_voice_job = "prepared-job"
            dialog._generation_input = object()
            dialog.voice_library.select(
                "Narrator", route="voice", source_id="preset:marius"
            )
            dialog._show_voice_confirmation(plan)
            dialog.narrator_choice.setCurrentIndex(1)
            self.assertNotIn("Marius", dialog.narrator_status.text())
            dialog.content_scroll.show()

            dialog.back_to_story_selection.click()

            self.assertFalse(dialog._awaiting_voice_confirmation)
            self.assertFalse(dialog.selection_panel.isHidden())
            self.assertTrue(dialog.voice_confirmation.isHidden())
            self.assertTrue(dialog.content_scroll.isHidden())
            self.assertEqual(dialog.step.text(), "Step 1 of 4 - Choose stories")
            self.assertEqual(dialog.cancel_button.text(), "Cancel")
            self.assertEqual(
                dialog.stories.item(0).checkState(0), Qt.CheckState.Checked
            )
            self.assertIsNone(dialog.voice_plan())
            self.assertIsNone(dialog._prepared_voice_manifest)
            self.assertIsNone(dialog._prepared_voice_job)
            self.assertIsNone(dialog.generation_input())
            self.assertIn("Marius", dialog.narrator_status.text())

    def test_cancelled_shared_editor_keeps_voice_confirmation_and_choices(self):
        with TemporaryDirectory() as temporary_directory:
            dialog, _pool, plan, group, decisions, _store = self._story_voice_dialog(
                Path(temporary_directory)
            )
            self.addCleanup(dialog.deleteLater)
            dialog._voice_plan = plan
            dialog._show_voice_confirmation(plan)
            dialog.game_narrator_chooser = Mock(return_value=None)
            bindings = dialog.voice_library.bindings()
            dialog.inspect_character_voice.click()
            self.assertTrue(dialog.cancel_button.isEnabled())
            self.assertFalse(dialog.voice_confirmation.isHidden())
            self.assertEqual(dialog.voice_plan(), plan)
            self.assertTrue(dialog.continue_button.isDefault())
            self.assertTrue(dialog.cancel_button.autoDefault())
            self.assertEqual(dialog.voice_library.bindings(), bindings)
            self.assertIsNone(
                decisions.choice_for(group.group_id, group.decision_context_sha256)
            )

    def test_voice_plan_separates_role_voice_status_and_coverage(self):
        with TemporaryDirectory() as temporary_directory:
            dialog, _pool, plan, group, _decisions, _store = self._story_voice_dialog(
                Path(temporary_directory)
            )
            self.addCleanup(dialog.deleteLater)
            dialog._voice_plan = plan
            dialog._show_voice_confirmation(plan)
            item = dialog.voice_routes.item(0)
            card = dialog.voice_routes.itemWidget(item)
            assert card is not None
            labels = [
                label.text()
                for label in card.findChildren(QLabel)
                if label.objectName() != "voice_preview_status"
            ]
            self.assertEqual(labels[0], group.character)
            self.assertEqual(labels[1], "Narrator required")
            self.assertEqual(labels[2], "automatic (review optional)")
            self.assertEqual(labels[3], str(len(group.line_ids)))
            self.assertEqual(item.text(), "")
            self.assertIn(
                group.character,
                item.data(Qt.ItemDataRole.AccessibleTextRole),
            )

            narrator_fallback = replace(
                group,
                route="narrator",
                resolution="automatic-narrator-fallback",
            )
            saved_fallback = replace(
                narrator_fallback,
                resolution="saved-player-decision",
            )
            self.assertEqual(
                dialog._voice_route_item(
                    narrator_fallback, show_portraits=False, suggested_link=False
                ).data(int(Qt.ItemDataRole.UserRole) + 2)[2],
                "automatic",
            )
            self.assertEqual(
                dialog._voice_route_item(
                    saved_fallback, show_portraits=False, suggested_link=False
                ).data(int(Qt.ItemDataRole.UserRole) + 2)[2],
                "chosen by you",
            )

    def test_voice_route_card_supports_mouse_keyboard_and_double_click(self):
        with TemporaryDirectory() as temporary_directory:
            dialog, _pool, plan, group, _decisions, _store = self._story_voice_dialog(
                Path(temporary_directory)
            )
            self.addCleanup(dialog.deleteLater)
            plan, _second = with_second_group(plan, group)
            dialog._voice_plan = plan
            dialog._show_voice_confirmation(plan)
            dialog.resize(1000, 760)
            dialog.show()
            self.application.processEvents()

            routes = dialog.voice_routes
            routes.scrollToItem(routes.item(1))
            second = routes.visualItemRect(routes.item(1)).center()
            QTest.mouseClick(routes.viewport(), Qt.MouseButton.LeftButton, pos=second)
            self.assertEqual(routes.currentRow(), 1)
            self.assertTrue(dialog.inspect_character_voice.isEnabled())

            routes.setCurrentRow(0)
            routes.setFocus()
            QTest.keyClick(routes, Qt.Key.Key_Down)
            self.assertEqual(routes.currentRow(), 1)
            second = routes.visualItemRect(routes.item(1)).center()
            self.assertTrue(routes.viewport().rect().contains(second))
            self.assertIs(routes.itemAt(second), routes.item(1))
            double_clicked = Mock()
            dialog.game_narrator_chooser = Mock(return_value=None)
            routes.itemDoubleClicked.connect(double_clicked)
            QTest.mouseClick(routes.viewport(), Qt.MouseButton.LeftButton, pos=second)
            QTest.mouseDClick(routes.viewport(), Qt.MouseButton.LeftButton, pos=second)
            self.assertTrue(double_clicked.called)
            role = routes.currentItem().data(Qt.ItemDataRole.UserRole)
            dialog.game_narrator_chooser.assert_called_once_with(
                dialog.settings, dialog, character=role
            )
            self.assertFalse(dialog.voice_confirmation.isHidden())

    def test_voice_plan_can_link_character_names_without_choosing_a_voice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dialog, pool, plan, group, _decisions, _store = self._story_voice_dialog(
                root
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
            dialog._update_voice_route_actions()
            self.assertFalse(dialog.link_identity.isEnabled())
            first_item.setData(Qt.ItemDataRole.UserRole, original_role)
            dialog._update_voice_route_actions()
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
                self._story_voice_dialog(root)
            )
            self.addCleanup(dialog.deleteLater)
            aderyn = replace(
                rhiannon,
                group_id="a" * 64,
                character="Aderyn",
                routing_role="Aderyn",
                line_ids=("line:aderyn",),
                route="voice",
                source_id=rhiannon.candidates[0].source_id,
                source_character=rhiannon.candidates[0].source_character,
                resolution="known-character-voice",
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
            self.assertIn(
                "Optional voice review: 1 assignment",
                dialog.voice_route_summary.text(),
            )
            self.assertTrue(dialog.voice_route_summary.font().bold())
            for row in range(dialog.voice_routes.count()):
                item = dialog.voice_routes.item(row)
                accessible_text = item.data(Qt.ItemDataRole.AccessibleTextRole)
                if item.data(Qt.ItemDataRole.UserRole) == "Rhiannon":
                    self.assertTrue(accessible_text.startswith("REVIEW VOICE: "))
                    self.assertIn("Narrator required", accessible_text)
                    self.assertTrue(item.font().bold())
                    self.assertIn(
                        "Listen or change voice",
                        item.data(Qt.ItemDataRole.AccessibleDescriptionRole),
                    )
                if item.data(Qt.ItemDataRole.UserRole) == "Aderyn":
                    self.assertTrue(
                        accessible_text.startswith("POSSIBLE SAME PERSON: ")
                    )
                    self.assertIn(
                        "Link same person",
                        item.data(Qt.ItemDataRole.AccessibleDescriptionRole),
                    )
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
            dialog._update_voice_route_actions()
            with patch(
                "vntts.pregeneration_ui.QInputDialog.getItem",
                return_value=("", False),
            ) as choose:
                dialog.link_identity.click()
            self.assertEqual(choose.call_args.args[3][0], "")

            dialog.voice_library.link_person("Rhiannon", "Gwyndolyn")
            dialog._update_voice_route_actions()
            with patch(
                "vntts.pregeneration_ui.QInputDialog.getItem",
                return_value=("", False),
            ) as choose:
                dialog.link_identity.click()
            self.assertEqual(choose.call_args.args[3].count("Rhiannon"), 1)
            self.assertEqual(choose.call_args.args[3][0], "Rhiannon")

            dialog._job.story_index_sha256 = "stale"
            dialog._update_voice_route_actions()
            self.assertTrue(dialog.identity_suggestion.isHidden())

    def test_voice_plan_hides_icons_when_portrait_coverage_is_sparse(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dialog, _pool, plan, group, _decisions, _store = self._story_voice_dialog(
                root
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

    def test_saved_voice_replan_failure_restores_continue_for_enter(self):
        for focus_target in ("dialog", "stories"):
            with self.subTest(focus=focus_target), TemporaryDirectory() as directory:
                dialog, pool, plan, _group, _decisions, store = (
                    self._story_voice_dialog(Path(directory))
                )
                self.addCleanup(dialog.deleteLater)
                dialog.show()
                dialog.activateWindow()
                dialog.stories.item(0).setCheckState(0, Qt.CheckState.Checked)
                dialog.continue_button.click()
                pool.tasks.pop().run()
                self.application.processEvents()
                pool.tasks.clear()
                dialog.preparing_inputs = False
                dialog._show_voice_confirmation(plan)
                dialog.game_narrator_chooser = Mock(return_value=dialog.settings)
                store.create.side_effect = OSError("voice reference changed")
                dialog.inspect_character_voice.click()
                pool.tasks.pop().run()
                self.application.processEvents()
                self.assertFalse(dialog.selection_panel.isHidden())
                self.assertFalse(dialog._awaiting_voice_confirmation)
                self.assertTrue(dialog.continue_button.isEnabled())
                self.assertTrue(dialog.continue_button.isDefault())
                self.assertTrue(dialog.cancel_button.autoDefault())
                clicked = Mock()
                dialog.continue_button.clicked.connect(clicked)
                target = dialog if focus_target == "dialog" else dialog.stories
                target.setFocus()
                QTest.keyClick(target, Qt.Key.Key_Return)
                self.assertEqual(clicked.call_count, 1)

    def test_shared_editor_save_runs_general_planning_and_audio_check(self):
        with TemporaryDirectory() as temporary_directory:
            dialog, pool, plan, _group, _decisions, voice_plan_store = (
                self._story_voice_dialog(Path(temporary_directory))
            )
            self.addCleanup(dialog.deleteLater)
            dialog.stories.item(0).setCheckState(0, Qt.CheckState.Checked)
            dialog.continue_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(dialog.preparing_inputs)
            pool.tasks.clear()
            dialog.preparing_inputs = False
            dialog._show_voice_confirmation(plan)
            dialog.game_narrator_chooser = Mock(return_value=dialog.settings)
            dialog.inspect_character_voice.click()
            self.assertTrue(dialog.planning_voices)
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(voice_plan_store.create.call_count, 2)
            self.assertFalse(dialog._awaiting_voice_confirmation)
            self.assertTrue(dialog.preparing_inputs)
            self.assertEqual(len(pool.tasks), 1)
            self.assertTrue(dialog.input_runner.active)
            self.assertIn("Checking", dialog.resume_status.text())

    def test_deferred_input_check_recovers_a_changed_story(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dialog, pool, _plan, _group, _decisions, _store = self._story_voice_dialog(
                root
            )
            self.addCleanup(dialog.deleteLater)
            dialog.stories.item(0).setCheckState(0, Qt.CheckState.Checked)
            dialog.continue_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            pool.tasks.clear()
            cause = StoryContentChanged("Story content checksum changed")
            error = PregenerationQueueError("Unable to read selected dialogue")
            error.__cause__ = cause

            dialog._generation_input_finished(None, error)

            self.assertTrue(dialog._recovering_stale_job)
            self.assertTrue(dialog.planning_voices)
            self.assertEqual(len(pool.tasks), 1)


if __name__ == "__main__":
    unittest.main()

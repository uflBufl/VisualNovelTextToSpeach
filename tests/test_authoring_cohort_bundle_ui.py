import json
import os
import threading
import time
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from tests import test_authoring_cohort_review
    from vntts.authoring.cohort_bundle import (
        build_cohort_review_bundle,
        execute_cohort_bundle_decision,
        load_cohort_review_bundle_samples,
        write_cohort_review_bundle,
    )
    from vntts.authoring.cohort_bundle_ui import CohortReviewBundleDialog
    from vntts.authoring.cohort_bundle_ui import main as review_bundle_main
    from vntts.authoring.cohort_review import (
        build_cohort_review_decision,
        build_cohort_review_plan,
    )
    from vntts.authoring.voice_quality_gate import (
        build_voice_quality_gate,
        write_voice_quality_gate,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QCloseEvent = None
    QMediaPlayer = None
    QTest = None
    CohortReviewBundleDialog = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class AuthoringCohortBundleUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_player_patcher = patch(
            "vntts.authoring.cohort_bundle_ui.QMediaPlayer"
        )
        cls.audio_output_patcher = patch(
            "vntts.authoring.cohort_bundle_ui.QAudioOutput"
        )
        media_player = cls.media_player_patcher.start()
        media_player.MediaStatus = QMediaPlayer.MediaStatus
        cls.audio_output_patcher.start()
        cls.application = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.audio_output_patcher.stop()
        cls.media_player_patcher.stop()

    def tearDown(self):
        for widget in self.application.topLevelWidgets():
            if isinstance(widget, CohortReviewBundleDialog):
                widget._load_active = False
                widget._playback_prepare_active = False
                widget._decision_active = False
                widget.close()
                widget.deleteLater()
        self.application.processEvents()

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Timed out waiting for Qt bundle work")

    def create_bundle(self, root):
        fixture = test_authoring_cohort_review.AuthoringCohortReviewTest()
        first = fixture.create_pending_workspace(root / "first")[0]
        second = fixture.create_pending_workspace(root / "second")[0]
        return build_cohort_review_bundle((first, second))

    def create_quality_gated_bundle(self, root, *, mismatch=False):
        fixture = test_authoring_cohort_review.AuthoringCohortReviewTest()
        first, first_state, queue_id = fixture.create_pending_workspace(root / "first")
        second, second_state, _second_queue = fixture.create_pending_workspace(
            root / "second"
        )
        for state_path in (first_state, second_state):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            result = next(iter(state["items"].values()))
            result.update(
                {
                    "provider": "moss-tts",
                    "model": "model with spaces",
                    "generation_profile": "stable",
                }
            )
            state_path.write_text(
                json.dumps(state, sort_keys=True),
                encoding="utf-8",
            )
        if mismatch:
            state = json.loads(second_state.read_text(encoding="utf-8"))
            next(iter(state["items"].values()))["prompt_sha256"] = "c" * 64
            second_state.write_text(
                json.dumps(state, sort_keys=True),
                encoding="utf-8",
            )
        plan = build_cohort_review_plan(first)
        decision = build_cohort_review_decision(
            plan,
            plan.document["cohorts"][0]["cohort_id"],
            "accepted",
            reviewed_queue_ids=[queue_id],
            sample_assessments={queue_id: "acceptable"},
        )
        gate = build_voice_quality_gate(first, plan, decision)
        bundle_path = root / "bundle.json"
        gate_path = root / "quality-gate.json"
        write_cohort_review_bundle(
            build_cohort_review_bundle((first, second)),
            bundle_path,
        )
        write_voice_quality_gate(gate, gate_path)
        return bundle_path, gate_path, gate

    def test_replay_keeps_controls_and_marks_only_finished_audio_heard(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)

            self.assertTrue(dialog.cohort_choice.itemText(0).startswith("Required 1/2"))
            self.assertIn("Review every listed cohort", dialog.operation.text())
            self.assertEqual(
                dialog.overall_progress.format(),
                "0 of 2 cohorts completed in this review session",
            )
            self.assertIn("Play 1 remaining sample", dialog.decision_help.text())
            self.assertTrue(dialog.table.isColumnHidden(5))
            self.assertTrue(dialog.table.isColumnHidden(6))
            self.assertIn("Generated role", dialog.sample_identity.text())
            self.assertEqual(
                dialog.decision_context.values["game_speaker"].text(),
                dialog._selected_sample().item.speaker,
            )
            self.assertEqual(
                dialog.decision_context.values["synthesis_voice"].text(),
                dialog._current_cohort()["identity"]["voice_character"],
            )
            self.assertIn(
                "checksum-bound WAV",
                dialog.decision_context.values["effect"].text(),
            )
            self.assertIn("listening decides", dialog.sample_identity.text())
            self.assertIn("not a rejection verdict", dialog.guide.text())
            self.assertNotIn("technical-attention", dialog.table.item(0, 4).text())
            self.assertEqual(
                dialog.sample_text.text(), dialog._selected_sample().item.text
            )

            dialog.technical_details.setChecked(True)
            self.assertFalse(dialog.table.isColumnHidden(5))
            self.assertFalse(dialog.table.isColumnHidden(6))
            self.assertTrue(dialog.cohort_audit.isVisible())
            self.assertIn("Provider:", dialog.cohort_audit.text())

            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            self.assertTrue(dialog.replay.isEnabled())
            self.assertFalse(dialog.accept.isEnabled())

            stop_calls = []
            original_stop = dialog.stop_playback

            def tracked_stop():
                stop_calls.append(True)
                original_stop()

            dialog.stop_playback = tracked_stop
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertEqual(stop_calls, [])
            self.wait_for(lambda: bool(stop_calls))

            self.assertTrue(dialog.replay.isEnabled())
            self.assertTrue(dialog.accept.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
            self.assertTrue(dialog.mark_bad.isEnabled())
            self.assertTrue(dialog.leave_undecided.isVisible())
            self.assertEqual(dialog.table.item(0, 0).text(), "Heard")
            self.assertIn("All 1 required samples", dialog.decision_help.text())

    def test_navigation_during_playback_credits_target_and_keeps_new_selection(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            first = dialog._selected_sample()
            second_item = replace(
                first.item,
                queue_id=f"{first.item.queue_id}-second",
                line_id=f"{first.item.line_id}-second",
                text="A second immutable review sample.",
            )
            second = replace(first, item=second_item)
            key = dialog._current_key()
            dialog.samples_by_cohort[key] = (first, second)
            dialog._show_current_cohort()

            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            dialog.table.selectRow(1)
            self.assertEqual(
                dialog._selected_sample().item.queue_id, second.item.queue_id
            )
            self.assertIsNotNone(dialog._playback_target)

            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.wait_for(lambda: dialog.table.item(0, 0).text() == "Heard")

            self.assertEqual(
                dialog._selected_sample().item.queue_id, second.item.queue_id
            )
            self.assertIn(first.item.queue_id, dialog.heard[key])
            self.assertNotIn(second.item.queue_id, dialog.heard[key])
            self.assertEqual(dialog.table.item(0, 0).text(), "Heard")
            self.assertEqual(dialog.table.item(1, 0).text(), "Not heard")

    def test_bad_marker_blocks_accept_but_keeps_reject(self):
        calls = []

        def execute(*arguments):
            calls.append(arguments)
            return SimpleNamespace(next_bundle=arguments[0])

        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(
                bundle,
                confirmer=lambda *_args: True,
                decision_executor=execute,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(sample.item.queue_id)
            dialog._show_current_cohort()

            dialog.defect_checks["pause_or_pacing"].setChecked(True)
            dialog.defect_checks["repetition"].setChecked(True)

            self.assertFalse(dialog.accept.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
            self.assertFalse(dialog.need_another.isEnabled())
            self.assertIn("Pause or pacing", dialog.table.item(0, 1).text())
            self.assertEqual(
                dialog.bad_reasons[key][sample.item.queue_id],
                {"pause_or_pacing", "repetition"},
            )
            self.assertIn("marked bad", dialog.decision_help.text())
            dialog.apply_decision("rejected")
            self.wait_for(lambda: bool(calls) and not dialog._decision_active)

        self.assertEqual(
            calls[0][5][sample.item.queue_id],
            {
                "assessment": "bad",
                "defect_reasons": ["pause_or_pacing", "repetition"],
            },
        )

    def test_mixed_action_projects_only_individually_heard_marked_scope(self):
        calls = []

        def execute(*arguments):
            calls.append(arguments)
            return SimpleNamespace(next_bundle=arguments[0])

        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(
                bundle,
                confirmer=lambda *_args: True,
                decision_executor=execute,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            first = dialog._selected_sample()
            second_item = replace(
                first.item,
                queue_id=f"{first.item.queue_id}-second",
                line_id=f"{first.item.line_id}-second",
                text="Second individually heard generated WAV.",
            )
            second = replace(first, item=second_item)
            key = dialog._current_key()
            dialog.samples_by_cohort[key] = (first, second)
            bundle_cohort = next(
                value
                for value in dialog.bundle.document["cohorts"]
                if (value["workspace_id"], value["cohort_id"]) == key
            )
            second_sample = deepcopy(bundle_cohort["samples"][0])
            second_sample["queue_id"] = second_item.queue_id
            second_sample["line_id"] = second_item.line_id
            second_sample["text"] = second_item.text
            bundle_cohort["samples"].append(second_sample)
            bundle_cohort["item_count"] = 2
            source = next(
                value
                for value in dialog.bundle.document["sources"]
                if value["workspace_id"] == key[0]
            )
            plan_cohort = next(
                value
                for value in source["plan"]["cohorts"]
                if value["cohort_id"] == key[1]
            )
            second_target = deepcopy(plan_cohort["items"][0])
            second_target["queue_id"] = second_item.queue_id
            second_target["line_id"] = second_item.line_id
            second_target["sampled"] = True
            plan_cohort["items"].append(second_target)
            plan_cohort["sample_queue_ids"].append(second_item.queue_id)
            plan_cohort["item_count"] = 2
            dialog.heard[key].update({first.item.queue_id, second_item.queue_id})
            dialog.bad[key].add(first.item.queue_id)
            dialog.bad_reasons[key][first.item.queue_id] = {"pause_or_pacing"}
            dialog._show_current_cohort()

            self.assertTrue(dialog.repair_marked.isEnabled())
            self.assertEqual(
                dialog.repair_marked.text(), "Repair 1 marked; accept 1 heard"
            )
            self.assertIn("individually heard", dialog.decision_help.text())
            dialog.apply_decision("split")
            self.wait_for(lambda: bool(calls) and not dialog._decision_active)

        self.assertEqual(calls[0][3], "split")
        self.assertEqual(calls[0][4], [first.item.queue_id, second_item.queue_id])
        self.assertEqual(
            calls[0][5],
            {
                first.item.queue_id: {
                    "assessment": "bad",
                    "defect_reasons": ["pause_or_pacing"],
                },
                second_item.queue_id: {
                    "assessment": "acceptable",
                    "defect_reasons": [],
                },
            },
        )

    def test_mixed_action_keeps_an_unsampled_target_pending(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            source = next(
                value
                for value in dialog.bundle.document["sources"]
                if value["workspace_id"] == key[0]
            )
            plan_cohort = next(
                value
                for value in source["plan"]["cohorts"]
                if value["cohort_id"] == key[1]
            )
            unsampled = deepcopy(plan_cohort["items"][0])
            unsampled["queue_id"] = f"{sample.item.queue_id}-unsampled"
            unsampled["sampled"] = False
            plan_cohort["items"].append(unsampled)
            plan_cohort["item_count"] = 2
            bundle_cohort = next(
                value
                for value in dialog.bundle.document["cohorts"]
                if (value["workspace_id"], value["cohort_id"]) == key
            )
            bundle_cohort["item_count"] = 2
            dialog.heard[key].add(sample.item.queue_id)
            dialog.bad[key].add(sample.item.queue_id)
            dialog.bad_reasons[key][sample.item.queue_id] = {"pause_or_pacing"}
            dialog._show_current_cohort()

        self.assertTrue(dialog.repair_marked.isEnabled())
        self.assertEqual(
            dialog.repair_marked.text(),
            "Repair 1 marked; accept 0 heard; leave 1 pending",
        )
        self.assertIn("leave 1 unsampled WAVs pending", dialog.decision_help.text())

    def test_space_and_bad_shortcuts_work_from_table_at_compact_size(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            dialog.resize(900, 820)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            dialog.table.setFocus()

            QTest.keyClick(dialog.table, Qt.Key.Key_Space)
            self.wait_for(lambda: dialog._playback_target is not None)
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.wait_for(lambda: dialog.mark_bad.isEnabled())
            QTest.keyClick(dialog.table, Qt.Key.Key_B)
            dialog.table.doubleClicked.emit(dialog.table.model().index(0, 0))
            self.wait_for(lambda: dialog._playback_target is not None)
            dialog.stop_playback()

            self.assertIn("Other or unclear defect", dialog.table.item(0, 1).text())
            self.assertTrue(dialog.heading.isVisible())
            self.assertTrue(dialog.sample_text.isVisible())
            self.assertTrue(dialog.reject.isVisible())
            self.assertIn("press Space to play/replay", dialog.shortcuts_help.text())
            review_bottom = dialog.review_scroll.mapTo(
                dialog, QPoint(0, dialog.review_scroll.height())
            ).y()
            decision_top = dialog.decision_help.mapTo(dialog, QPoint(0, 0)).y()
            self.assertLessEqual(review_bottom, decision_top)

    def test_compact_decision_rows_and_shortcuts_never_overlap(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            dialog.resize(900, 820)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(sample.item.queue_id)
            dialog._show_current_cohort()
            self.application.processEvents()

            first_row_bottom = max(
                widget.mapTo(dialog, QPoint(0, widget.height())).y()
                for widget in (
                    dialog.mark_bad,
                    dialog.need_another,
                    dialog.leave_undecided,
                )
            )
            second_row_top = min(
                widget.mapTo(dialog, QPoint(0, 0)).y()
                for widget in (
                    dialog.repair_marked,
                    dialog.accept,
                    dialog.reject,
                )
            )
            second_row_bottom = max(
                widget.mapTo(dialog, QPoint(0, widget.height())).y()
                for widget in (
                    dialog.repair_marked,
                    dialog.accept,
                    dialog.reject,
                )
            )
            shortcut_top = dialog.shortcuts_help.mapTo(dialog, QPoint(0, 0)).y()

            self.assertLessEqual(first_row_bottom, second_row_top)
            self.assertLessEqual(second_row_bottom, shortcut_top)
            self.assertTrue(dialog.review_scroll.isVisible())
            self.assertTrue(dialog.accept.isVisible())
            self.assertTrue(dialog.reject.isVisible())

    def test_decision_is_source_local_and_navigation_remains_live_while_saving(self):
        calls = []
        started = threading.Event()
        release = threading.Event()

        def execute(*arguments):
            calls.append(arguments)
            started.set()
            release.wait(2)
            return SimpleNamespace(next_bundle=arguments[0])

        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(
                bundle,
                decision_executor=execute,
                confirmer=lambda *_args: True,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(sample.item.queue_id)
            dialog._update_actions()

            dialog.table.setFocus()
            QTest.keyClick(
                dialog.table,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.wait_for(started.is_set)
            self.assertTrue(dialog._decision_active)
            self.assertTrue(dialog.replay.isEnabled())
            self.assertFalse(dialog.accept.isEnabled())
            self.assertTrue(dialog.progress.isVisible())
            self.assertIn("Saving in background", dialog.operation.text())
            self.assertIn("approving all 1 cohort WAVs", dialog.operation.text())
            release.set()
            self.wait_for(lambda: bool(calls) and not dialog._decision_active)

        self.assertEqual(calls[0][1], sample.workspace_id)
        self.assertEqual(calls[0][2], sample.cohort_id)
        self.assertEqual(calls[0][4], [sample.item.queue_id])

    def test_close_is_deferred_during_authority_work(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle)
            dialog._load_active = True
            event = QCloseEvent()

            dialog.closeEvent(event)

        self.assertFalse(event.isAccepted())
        self.assertIn("Close deferred", dialog.status.text())
        self.assertFalse(dialog.progress.isHidden())
        self.assertIn("Refreshing checksum authority", dialog.operation.text())

    def test_real_decision_removes_completed_cohort_and_selects_next(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            first = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(first.item.queue_id)
            dialog._update_actions()

            dialog.apply_decision("accepted")

            self.wait_for(
                lambda: (
                    not dialog._decision_active
                    and not dialog._load_active
                    and dialog.cohort_choice.count() == 1
                ),
                timeout=5,
            )
            self.assertEqual(dialog.cohort_choice.currentIndex(), 0)
            self.assertTrue(
                dialog.cohort_choice.currentText().startswith("Required 1/1")
            )
            self.assertNotEqual(
                dialog._selected_sample().workspace_id, first.workspace_id
            )
            self.assertIn("Current required cohort: 1/1", dialog.operation.text())
            self.assertEqual(
                dialog.overall_progress.format(),
                "1 of 2 cohorts completed in this review session",
            )

    def test_published_bundle_reopens_from_persisted_successor(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self.create_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            dialog = CohortReviewBundleDialog(
                publication,
                confirmer=lambda *_args: True,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            first = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(first.item.queue_id)
            dialog._update_actions()

            dialog.apply_decision("accepted")
            self.wait_for(
                lambda: (
                    not dialog._decision_active
                    and not dialog._load_active
                    and dialog.cohort_choice.count() == 1
                ),
                timeout=5,
            )
            dialog.close()
            self.application.processEvents()

            reopened = CohortReviewBundleDialog(publication)
            reopened.show()
            self.wait_for(lambda: reopened.table.rowCount() == 1)
            self.assertNotEqual(
                reopened._selected_sample().workspace_id,
                first.workspace_id,
            )
            self.assertEqual(reopened.bundle.document["cohort_count"], 1)
            self.assertTrue((root / "bundle.progress.json").is_file())

    def test_unfinished_listening_checkpoint_survives_close_and_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self.create_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            dialog = CohortReviewBundleDialog(publication)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()

            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.wait_for(lambda: dialog.mark_bad.isEnabled())
            dialog.toggle_bad()
            dialog.close()
            self.wait_for(lambda: not dialog.isVisible())

            reopened = CohortReviewBundleDialog(publication)
            reopened.show()
            self.wait_for(lambda: reopened.table.rowCount() == 1)
            key = (sample.workspace_id, sample.cohort_id)
            self.assertIn(sample.item.queue_id, reopened.heard[key])
            self.assertIn(sample.item.queue_id, reopened.bad[key])
            self.assertEqual(reopened.table.item(0, 0).text(), "Heard")
            self.assertIn("Other or unclear defect", reopened.table.item(0, 1).text())
            self.assertTrue((root / "bundle.observations.json").is_file())

    def test_observation_checkpoint_is_background_coalesced_and_close_safe(self):
        started = threading.Event()
        release = threading.Event()
        snapshots = []

        def slow_writer(*arguments):
            snapshots.append(arguments)
            started.set()
            release.wait(2)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self.create_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            dialog = CohortReviewBundleDialog(
                publication,
                observation_writer=slow_writer,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(sample.item.queue_id)
            heartbeat = []

            QTimer.singleShot(0, lambda: heartbeat.append(True))
            dialog._checkpoint_observations()
            self.assertTrue(started.wait(1))
            dialog.bad[key].add(sample.item.queue_id)
            dialog._checkpoint_observations()
            close_event = QCloseEvent()
            dialog.closeEvent(close_event)
            self.application.processEvents()

            self.assertEqual(heartbeat, [True])
            self.assertFalse(close_event.isAccepted())
            self.assertTrue(dialog.replay.isEnabled())
            self.assertTrue(dialog._close_after_observation)
            release.set()
            self.wait_for(
                lambda: len(snapshots) == 2 and not dialog._observation_active
            )
            self.wait_for(lambda: not dialog.isVisible())

            final_bad = snapshots[-1][-2]
            self.assertIn(sample.item.queue_id, final_bad[key])

    def test_published_expand_reopens_with_exact_prior_assessments(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self.create_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            selected = bundle.document["cohorts"][0]
            queue_id = selected["samples"][0]["queue_id"]
            execute_cohort_bundle_decision(
                bundle,
                selected["workspace_id"],
                selected["cohort_id"],
                "expand",
                reviewed_queue_ids=[queue_id],
                sample_assessments={queue_id: "bad"},
                next_clean_samples_per_bucket=2,
            )

            dialog = CohortReviewBundleDialog(publication)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)

            self.assertEqual(dialog.table.item(0, 0).text(), "Heard")
            self.assertIn("Unspecified", dialog.table.item(0, 1).text())
            self.assertIn("1 heard", dialog.sample_position.text())

    def test_published_decision_preloads_next_cohort_without_second_reload(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self.create_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            dialog = CohortReviewBundleDialog(
                publication,
                confirmer=lambda *_args: True,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            first = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(first.item.queue_id)
            dialog._update_actions()
            reloads = []
            dialog.reload_bundle = lambda: reloads.append(None)

            dialog.apply_decision("accepted")
            self.wait_for(
                lambda: not dialog._decision_active,
                timeout=5,
            )

            self.assertEqual(reloads, [])
            self.assertFalse(dialog._load_active)
            self.assertEqual(dialog.cohort_choice.count(), 1)
            self.assertIn("commit", dialog.status.text())
            self.assertIn("checkpoint", dialog.status.text())
            self.assertIn("next cohort", dialog.status.text())

    def test_retry_recovers_a_transient_load_error(self):
        calls = []

        def load(bundle):
            calls.append(None)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return load_cohort_review_bundle_samples(bundle)

        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, sample_loader=load)
            dialog.show()
            self.wait_for(lambda: dialog.retry_load.isEnabled())
            self.assertIn("BLOCKED", dialog.status.text())

            dialog.retry_load.click()
            self.wait_for(lambda: dialog.table.rowCount() == 1)

        self.assertEqual(len(calls), 2)
        self.assertIn("READY", dialog.status.text())

    def test_status_reports_progress_without_opening_a_window(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self.create_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = review_bundle_main([str(publication), "--status"])

        self.assertEqual(exit_code, 0)
        self.assertIn('"remaining_cohorts": 2', stdout.getvalue())
        self.assertEqual(
            [
                widget
                for widget in self.application.topLevelWidgets()
                if isinstance(widget, CohortReviewBundleDialog) and widget.isVisible()
            ],
            [],
        )

    def test_quality_gate_explains_baseline_without_projecting_a_decision(self):
        with TemporaryDirectory() as directory:
            bundle_path, gate_path, gate = self.create_quality_gated_bundle(
                Path(directory)
            )
            dialog = CohortReviewBundleDialog(
                bundle_path,
                quality_gate=gate_path,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)

            self.assertTrue(dialog.quality_baseline.isVisible())
            self.assertIn(
                "VOICE BASELINE ALREADY ACCEPTED",
                dialog.quality_baseline.text(),
            )
            self.assertIn(
                "not choosing the narrator again",
                dialog.quality_baseline.text(),
            )
            self.assertIn(gate.gate_id, dialog.quality_baseline.toolTip())
            self.assertIn(
                "matched 2 remaining cohorts", dialog.quality_baseline.toolTip()
            )
            self.assertFalse(dialog.accept.isEnabled())
            self.assertEqual(dialog.bundle.document["pending_item_count"], 2)
            self.assertTrue((Path(directory) / "bundle.progress.json").is_file())

    def test_missing_quality_gate_fails_closed_and_status_validates_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_path, gate_path, _gate = self.create_quality_gated_bundle(root)
            stdout = StringIO()
            with redirect_stdout(stdout):
                status_exit = review_bundle_main(
                    [
                        str(bundle_path),
                        "--quality-gate",
                        str(gate_path),
                        "--status",
                    ]
                )
            self.assertEqual(status_exit, 0)
            self.assertIn('"remaining_cohorts": 2', stdout.getvalue())

            dialog = CohortReviewBundleDialog(
                bundle_path,
                quality_gate=root / "missing-gate.json",
            )
            dialog.show()
            self.wait_for(lambda: dialog.retry_load.isEnabled())

            self.assertIn("BLOCKED", dialog.status.text())
            self.assertFalse(dialog.quality_baseline.isVisible())
            self.assertFalse(dialog.accept.isEnabled())
            self.assertFalse((root / "bundle.progress.json").exists())

    def test_mismatched_quality_gate_blocks_every_cohort_decision(self):
        with TemporaryDirectory() as directory:
            bundle_path, gate_path, _gate = self.create_quality_gated_bundle(
                Path(directory),
                mismatch=True,
            )
            dialog = CohortReviewBundleDialog(
                bundle_path,
                quality_gate=gate_path,
            )
            dialog.show()
            self.wait_for(lambda: dialog.retry_load.isEnabled())

            self.assertIn("does not match every remaining cohort", dialog.status.text())
            self.assertIn("prompt_sha256", dialog.status.text())
            self.assertEqual(dialog.table.rowCount(), 0)
            self.assertFalse(dialog.accept.isEnabled())
            self.assertFalse(dialog.reject.isEnabled())
            self.assertFalse((Path(directory) / "bundle.progress.json").exists())


if __name__ == "__main__":
    unittest.main()

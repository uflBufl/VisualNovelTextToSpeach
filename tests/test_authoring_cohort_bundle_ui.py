import hashlib
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
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtGui import QCloseEvent, QKeySequence
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from shiboken6 import isValid

    from tests.authoring_fixtures import (
        create_pending_cohort_workspace,
        write_legacy_fixture,
    )
    from tests.qt_task_fixtures import ManualThreadPool, release_and_wait_for_pool
    from vntts.authoring.cohort_bundle import (
        build_cohort_review_bundle,
        execute_cohort_bundle_decision,
        load_cohort_review_bundle_samples,
        write_cohort_review_bundle,
        write_cohort_review_observations,
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
    isValid = None


def create_multisample_bundle(root):
    def write_multisample_fixture(*arguments, **options):
        fixture = write_legacy_fixture(*arguments, **options)
        metadata, template = (
            json.loads(line) for line in fixture["queue"].read_text().splitlines()
        )
        state = json.loads(fixture["state"].read_text())
        result = next(iter(state["items"].values()))
        manifest = json.loads(fixture["manifest"].read_text())
        entry = manifest["entries"][0]
        items, entries, results = [], [], {}
        for index, word_count in enumerate((3, 10, 18, 4)):
            text = " ".join(["word"] * word_count) + "."
            digest = hashlib.sha256(text.encode()).hexdigest()
            line_id = f"reverse1999:cohort:{index}"
            queue_id = f"{line_id}:{digest[:16]}"
            identity = {"queue_id": queue_id, "line_id": line_id, "text_sha256": digest}
            items.append({**template, **identity, "text": text})
            entries.append({**entry, **identity})
            results[queue_id] = {
                **result,
                "line_id": line_id,
                "text_sha256": digest,
                "status": "generated",
                "review_status": "pending_review",
                "generation_profile": "stable",
                "voice_character": "Rhiannon",
                "prompt_applied": False,
                "synthesis_provenance_sha256": "b" * 64,
            }
        metadata["item_count"] = len(items)
        fixture["queue"].write_text(
            "\n".join(json.dumps(row) for row in [metadata, *items]) + "\n"
        )
        queue_sha256 = hashlib.sha256(fixture["queue"].read_bytes()).hexdigest()
        state.update(items=results, queue_sha256=queue_sha256)
        fixture["state"].write_text(json.dumps(state))
        manifest.update(
            entries=entries, entry_count=len(entries), source_queue_sha256=queue_sha256
        )
        fixture["manifest"].write_text(json.dumps(manifest))
        return fixture

    with patch(
        "tests.authoring_fixtures.write_legacy_fixture",
        side_effect=write_multisample_fixture,
    ):
        workspace, state_path, _queue_id = create_pending_cohort_workspace(root)
    return build_cohort_review_bundle((workspace,)), state_path


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class AuthoringCohortBundleUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_player_patcher = patch(
            "vntts.authoring.cohort_bundle_ui.QMediaPlayer"
        )
        media_player = cls.media_player_patcher.start()
        media_player.MediaStatus = QMediaPlayer.MediaStatus
        cls.application = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.media_player_patcher.stop()

    def tearDown(self):
        for widget in self.application.topLevelWidgets():
            if isinstance(widget, CohortReviewBundleDialog):
                widget._load_active = False
                widget._playback_prepare_active = False
                widget._decision_active = False
                widget._observation_error = None
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
        first = create_pending_cohort_workspace(root / "first")[0]
        second = create_pending_cohort_workspace(root / "second")[0]
        return build_cohort_review_bundle((first, second))

    def create_quality_gated_bundle(self, root, *, mismatch=False):
        first, first_state, queue_id = create_pending_cohort_workspace(root / "first")
        second, second_state, _second_queue = create_pending_cohort_workspace(
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

            self.assertTrue(
                dialog.cohort_choice.itemText(0).startswith("Cohort 1 of 2")
            )
            self.assertIn("Cohort 1 of 2", dialog.cohort_scope.text())
            self.assertEqual(
                dialog.overall_progress.format(),
                "0 of 2 cohorts completed in this review session",
            )
            self.assertIn("Listen to 1 remaining sample", dialog.decision_help.text())
            self.assertTrue(dialog.table.isColumnHidden(5))
            self.assertTrue(dialog.table.isColumnHidden(6))
            self.assertIn("Voice used", dialog.sample_identity.text())
            self.assertEqual(
                dialog.decision_context.values["game_speaker"],
                dialog._selected_sample().item.speaker,
            )
            self.assertEqual(
                dialog.decision_context.values["synthesis_voice"],
                dialog._current_cohort()["identity"]["voice_character"],
            )
            self.assertIn(
                "checksum-bound WAV",
                dialog.decision_context.values["effect"],
            )
            self.assertIn("listening decides", dialog.sample_identity.toolTip())
            self.assertIn("not a rejection verdict", dialog.guide.toolTip())
            self.assertNotIn("technical-attention", dialog.table.item(0, 4).text())
            self.assertEqual(
                dialog.sample_text.text(), dialog._selected_sample().item.text
            )
            self.assertTrue(dialog.summary.accessibleName())
            self.assertTrue(dialog.sample_text.accessibleName())
            self.assertIs(
                dialog.cohort_choice.nextInFocusChain(),
                dialog.technical_details,
            )

            dialog.technical_details.setChecked(True)
            self.assertFalse(dialog.table.isColumnHidden(5))
            self.assertFalse(dialog.table.isColumnHidden(6))
            self.assertTrue(dialog.decision_context.isVisible())
            self.assertIn("Synthesis:", dialog.decision_context.synthesis.text())

            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            self.assertTrue(dialog.replay.isEnabled())
            self.assertFalse(dialog.accept_button.isEnabled())

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
            self.assertTrue(dialog.accept_button.isEnabled())
            self.assertTrue(dialog.reject_button.isEnabled())
            self.assertTrue(dialog.mark_bad.isEnabled())
            self.assertTrue(dialog.leave_undecided.isVisible())
            self.assertEqual(dialog.table.item(0, 0).text(), "Heard")
            self.assertEqual(dialog.status.text(), "Ready to decide this cohort")
            self.assertIn("All 1 required samples", dialog.decision_help.text())

    def test_navigation_stops_old_sample_without_heard_credit(self):
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
            self.assertIsNone(dialog._playback_target)

            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertFalse(dialog.heard[key])

            self.assertEqual(
                dialog._selected_sample().item.queue_id, second.item.queue_id
            )
            self.assertNotIn(first.item.queue_id, dialog.heard[key])
            self.assertNotIn(second.item.queue_id, dialog.heard[key])
            self.assertEqual(dialog.table.item(0, 0).text(), "Not heard")
            self.assertEqual(dialog.table.item(1, 0).text(), "Not heard")

    def test_cohort_switch_restores_exact_heard_and_defect_state(self):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            first = dialog._selected_sample()
            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.wait_for(lambda: dialog._playback_buffer is None)
            dialog.defect_checks["repetition"].setChecked(True)
            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)

            dialog.cohort_choice.setCurrentIndex(1)

            self.assertNotEqual(
                dialog._selected_sample().workspace_id, first.workspace_id
            )
            self.assertIsNone(dialog._playback_target)
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertFalse(dialog.heard[dialog._current_key()])
            self.assertFalse(dialog.defect_checks["repetition"].isChecked())
            self.assertFalse(dialog.mark_bad.isEnabled())
            dialog.cohort_choice.setCurrentIndex(0)
            self.assertEqual(dialog._selected_sample(), first)
            self.assertEqual(dialog.table.item(0, 0).text(), "Heard")
            self.assertTrue(dialog.defect_checks["repetition"].isChecked())
            self.assertIn("Repeated words", dialog.table.item(0, 1).text())

    def test_stop_cancels_preparation_before_audio_can_start(self):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            pool = ManualThreadPool()
            dialog._playback_runner.thread_pool = pool
            dialog.player.play_bytes.reset_mock()

            dialog.play_selected()
            self.assertTrue(dialog._playback_prepare_active)
            self.assertTrue(dialog.stop.isEnabled())
            dialog.stop.click()
            pool.run_next()
            self.application.processEvents()

            dialog.player.play_bytes.assert_not_called()
            self.assertFalse(dialog._playback_prepare_active)
            self.assertIsNone(dialog._playback_target)
            self.assertFalse(dialog.heard[dialog._current_key()])

    def test_replay_stops_existing_audio_before_preparing_replacement(self):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            target = dialog._playback_target
            pool = ManualThreadPool()
            dialog._playback_runner.thread_pool = pool
            dialog.player.stop.reset_mock()
            dialog.player.play_bytes.reset_mock()

            dialog.play_selected()

            dialog.player.stop.assert_called_once()
            self.assertIsNone(dialog._playback_target)
            self.assertTrue(dialog._playback_prepare_active)
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertFalse(dialog.heard[dialog._current_key()])
            dialog.player.play_bytes.assert_not_called()
            pool.run_next()
            self.wait_for(lambda: dialog._playback_target is not None)
            self.assertEqual(dialog._playback_target, target)
            dialog.player.play_bytes.assert_called_once()

    def test_decision_shortcuts_wait_for_loading_and_playback_preparation(self):
        with TemporaryDirectory() as directory:
            confirmer = Mock(return_value=True)
            dialog = CohortReviewBundleDialog(
                self.create_bundle(Path(directory)), confirmer=confirmer
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            dialog.heard[dialog._current_key()].add(sample.item.queue_id)
            for active in ("_load_active", "_playback_prepare_active"):
                with self.subTest(active=active):
                    setattr(dialog, active, True)
                    dialog._update_actions()
                    self.assertFalse(dialog.accept_button.isEnabled())
                    dialog.table.setFocus()
                    with patch.object(dialog._decision_runner, "start") as start:
                        try:
                            QTest.keyClick(
                                dialog.table,
                                Qt.Key.Key_Return,
                                Qt.KeyboardModifier.ControlModifier,
                            )
                            start.assert_not_called()
                            confirmer.assert_not_called()
                        finally:
                            setattr(dialog, active, False)
                            dialog._decision_active = False
                            dialog._operation_timer.stop()
                            confirmer.reset_mock()

    def test_bad_shortcut_cannot_change_evidence_while_decision_saves(self):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(sample.item.queue_id)
            dialog._decision_active = True
            dialog._update_actions()
            self.assertFalse(dialog.mark_bad.isEnabled())
            dialog.table.setFocus()
            try:
                QTest.keyClick(dialog.table, Qt.Key.Key_B)

                self.assertFalse(dialog.bad[key])
                self.assertFalse(dialog.bad_reasons[key])
            finally:
                dialog._decision_active = False

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
            self.assertEqual(dialog.status.text(), "Ready to decide this cohort")

            dialog.defect_checks["pause_or_pacing"].setChecked(True)
            dialog.defect_checks["repetition"].setChecked(True)

            self.assertFalse(dialog.accept_button.isEnabled())
            self.assertTrue(dialog.reject_button.isEnabled())
            self.assertFalse(dialog.need_another.isEnabled())
            self.assertIn(
                "No additional sample is available", dialog.decision_help.text()
            )
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

    def test_specific_defect_replaces_unclear_quick_mark(self):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(sample.item.queue_id)
            dialog._show_current_cohort()

            dialog.toggle_bad()
            self.assertEqual(
                dialog.bad_reasons[key][sample.item.queue_id], {"other_or_unclear"}
            )
            dialog.defect_checks["pause_or_pacing"].setChecked(True)
            self.assertEqual(
                dialog.bad_reasons[key][sample.item.queue_id], {"pause_or_pacing"}
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
            self.assertIn("reject 1", dialog.decision_help.text())
            self.assertIn("accept 1 individually heard", dialog.decision_help.text())
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
        self.assertIn("reject 1", dialog.decision_help.text())
        self.assertIn("accept 0 individually heard", dialog.decision_help.text())
        self.assertIn("leave 1 unsampled WAVs pending", dialog.decision_help.text())

    def test_real_mixed_review_saves_one_bad_two_heard_good_and_one_pending(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, state_path = create_multisample_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            dialog = CohortReviewBundleDialog(
                publication, confirmer=lambda *_args: True
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 3)
            samples = dialog._current_samples()
            heard_ids = {sample.item.queue_id for sample in samples}
            target_ids = {
                item["queue_id"]
                for item in bundle.document["sources"][0]["plan"]["cohorts"][0]["items"]
            }
            pending_id = (target_ids - heard_ids).pop()
            for row in range(3):
                dialog.table.selectRow(row)
                dialog.play_selected()
                self.wait_for(lambda: dialog._playback_target is not None)
                dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
                self.wait_for(
                    lambda: (
                        not dialog._observation_active
                        and dialog._playback_buffer is None
                    )
                )
            dialog.table.selectRow(0)
            bad_id = dialog._selected_sample().item.queue_id
            dialog.toggle_bad()
            self.wait_for(lambda: not dialog._observation_active)

            dialog.repair_marked.click()
            self.wait_for(
                lambda: not dialog._decision_active and not dialog._load_active
            )

            state = json.loads(state_path.read_text())["items"]
            self.assertEqual(state[bad_id]["review_status"], "rejected")
            for queue_id in heard_ids - {bad_id}:
                self.assertEqual(state[queue_id]["review_status"], "approved")
            self.assertEqual(state[pending_id]["review_status"], "pending_review")
            self.assertEqual(dialog.bundle.document["pending_item_count"], 1)
            self.assertEqual(dialog._selected_sample().item.queue_id, pending_id)
            dialog.close()

            reopened = CohortReviewBundleDialog(publication)
            self.wait_for(lambda: reopened.table.rowCount() == 1)
            self.assertEqual(reopened._selected_sample().item.queue_id, pending_id)

    def test_one_heard_sample_allows_whole_cohort_rejection_but_not_acceptance(self):
        with TemporaryDirectory() as directory:
            bundle, state_path = create_multisample_bundle(Path(directory))
            confirmer = Mock(return_value=True)
            dialog = CohortReviewBundleDialog(bundle, confirmer=confirmer)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 3)
            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.wait_for(lambda: dialog._playback_buffer is None)

            self.assertEqual(len(dialog.heard[dialog._current_key()]), 1)
            self.assertFalse(dialog.accept_button.isEnabled())
            self.assertTrue(dialog.reject_button.isEnabled())
            self.assertIn("every WAV", dialog.reject_button.toolTip())
            dialog.reject_button.click()
            self.wait_for(
                lambda: not dialog._decision_active and not dialog._load_active
            )

            decision, cohort, reviewed_count, bad_count = confirmer.call_args.args
            self.assertEqual((decision, reviewed_count, bad_count), ("rejected", 1, 0))
            self.assertEqual(cohort["item_count"], 4)
            state = json.loads(state_path.read_text())["items"]
            self.assertEqual(len(state), 4)
            self.assertEqual(
                {item["review_status"] for item in state.values()}, {"rejected"}
            )
            self.assertEqual(dialog.bundle.document["cohort_count"], 0)

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
            self.assertTrue(dialog.reject_button.isVisible())
            self.assertIn("press Space to play/replay", dialog.guide.toolTip())
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
                    dialog.need_another,
                    dialog.repair_marked,
                    dialog.leave_undecided,
                )
            )
            second_row_top = min(
                widget.mapTo(dialog, QPoint(0, 0)).y()
                for widget in (
                    dialog.accept_button,
                    dialog.reject_button,
                )
            )
            self.assertLessEqual(first_row_bottom, second_row_top)
            self.assertTrue(dialog.review_scroll.isVisible())
            self.assertTrue(dialog.accept_button.isVisible())
            self.assertTrue(dialog.reject_button.isVisible())

    def test_scaled_font_and_accessibility_keep_journey_reachable(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            self.assertEqual(
                dialog.review_scroll.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
            base_point_size = dialog.font().pointSizeF()
            for scale in (1.5, 2.0, None):
                font = dialog.font()
                if scale is None:
                    font.setPixelSize(48)  # Exercise wide metrics on every host.
                else:
                    font.setPointSizeF(base_point_size * scale)
                dialog.setFont(font)
                dialog.resize(dialog.minimumSize())
                dialog.show()
                self.wait_for(lambda: dialog.table.rowCount() == 1)
                self.assertEqual(
                    dialog.review_scroll.horizontalScrollBar().maximum(), 0
                )

            self.assertLessEqual(dialog.minimumSizeHint().width(), dialog.width())
            self.assertLessEqual(dialog.minimumSizeHint().height(), dialog.height())
            self.assertIs(dialog.cohort_choice_label.buddy(), dialog.cohort_choice)
            self.assertIs(
                dialog.decision_context.technical_toggle.nextInFocusChain(),
                dialog.need_another,
            )
            self.assertIs(
                dialog.cohort_choice.nextInFocusChain(), dialog.technical_details
            )
            for button in (
                dialog.previous,
                dialog.replay,
                dialog.stop,
                dialog.next,
                dialog.mark_bad,
                dialog.need_another,
                dialog.leave_undecided,
                dialog.repair_marked,
                dialog.accept_button,
                dialog.reject_button,
                dialog.retry_load,
            ):
                self.assertTrue(button.accessibleName(), button.text())
                self.assertTrue(button.accessibleDescription(), button.text())

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
            with release_and_wait_for_pool(
                dialog.thread_pool,
                release,
                finish=lambda: self.wait_for(
                    lambda: not dialog._decision_active and not dialog._load_active
                ),
            ):
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
                self.assertFalse(dialog.accept_button.isEnabled())
                self.assertTrue(dialog.progress.isVisible())
                self.assertIn("Saving in background", dialog.operation.text())
                self.assertIn("approving all 1 cohort WAVs", dialog.operation.text())
                release.set()
                self.wait_for(lambda: bool(calls) and not dialog._decision_active)

        self.assertEqual(calls[0][1], sample.workspace_id)
        self.assertEqual(calls[0][2], sample.cohort_id)
        self.assertEqual(calls[0][4], [sample.item.queue_id])

    def test_close_during_in_memory_decision_finishes_without_restarting_playback(self):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(
                self.create_bundle(Path(directory)), confirmer=lambda *_args: True
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            dialog.heard[dialog._current_key()].add(sample.item.queue_id)
            pool = ManualThreadPool()
            dialog._decision_runner.thread_pool = pool
            dialog.player.play_bytes.reset_mock()
            dialog.apply_decision("accepted")
            self.assertTrue(dialog._decision_active)

            dialog.close()
            self.assertTrue(dialog.isVisible())
            dialog.play_selected()
            self.assertFalse(dialog._playback_prepare_active)
            pool.run_next()
            self.wait_for(lambda: not dialog.isVisible())

            self.assertFalse(dialog._decision_active)
            self.assertFalse(dialog._load_active)
            dialog.player.play_bytes.assert_not_called()

    def test_repeated_reflow_invalidates_replaced_sample_action_items(self):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            for _iteration in range(4):
                previous_items = [
                    dialog.sample_actions.itemAt(index)
                    for index in range(dialog.sample_actions.count())
                ]
                self.assertEqual(len(previous_items), 5)
                self.assertTrue(all(isValid(item) for item in previous_items))

                dialog._reflow_controls()

                self.assertFalse(any(isValid(item) for item in previous_items))
                self.assertEqual(dialog.sample_actions.count(), 5)

    def test_close_is_deferred_during_authority_work(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            with patch.object(CohortReviewBundleDialog, "reload_bundle"):
                dialog = CohortReviewBundleDialog(bundle)
            dialog._load_active = True
            dialog._update_actions()
            event = QCloseEvent()

            dialog.closeEvent(event)

        self.assertFalse(event.isAccepted())
        self.assertIn("Close deferred", dialog.status.text())
        self.assertFalse(dialog.progress.isHidden())
        self.assertIn("Closing after the current task", dialog.decision_help.text())

    def test_reject_does_not_finish_during_authority_work(self):
        with TemporaryDirectory() as directory:
            with patch.object(CohortReviewBundleDialog, "reload_bundle"):
                dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            finished = Mock()
            rejected = Mock()
            dialog.finished.connect(finished)
            dialog.rejected.connect(rejected)
            dialog.show()
            dialog._load_active = True

            dialog.reject()
            self.application.processEvents()

            finished.assert_not_called()
            rejected.assert_not_called()
            self.assertTrue(dialog.isVisible())
            self.assertIn("Close deferred", dialog.status.text())
            dialog._load_active = False
            dialog.close()
            finished.assert_called_once_with(0)
            rejected.assert_called_once_with()
            dialog.deleteLater()

    def test_worker_success_results_are_validated_at_qt_boundaries(self):
        with TemporaryDirectory() as directory:
            with patch.object(CohortReviewBundleDialog, "reload_bundle"):
                dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            dialog._load_active = True
            dialog._load_finished(object(), None)
            self.assertIn("BLOCKED", dialog.status.text())
            self.assertTrue(dialog.retry_load.isEnabled())
            self.assertFalse(dialog._load_active)

            dialog._playback_prepare_active = True
            dialog._playback_finished(object(), None)
            self.assertIn("REPLAY BLOCKED", dialog.status.text())
            self.assertFalse(dialog._playback_prepare_active)

            dialog._decision_active = True
            dialog._checkpoint_decisions = False
            dialog._decision_finished(object(), None)
            self.assertIn("SAVE FAILED", dialog.status.text())
            self.assertFalse(dialog._decision_active)

    def test_incomplete_checkpoint_result_fails_closed_without_attribute_errors(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            with patch.object(CohortReviewBundleDialog, "reload_bundle"):
                dialog = CohortReviewBundleDialog(bundle)
            complete = {
                "projection": SimpleNamespace(next_bundle=bundle),
                "bundle": bundle,
                "samples": (),
                "checkpoint_error": None,
                "refresh_error": None,
                "commit_seconds": 0,
                "checkpoint_seconds": 0,
                "refresh_seconds": 0,
            }
            for missing in ("bundle", "checkpoint_error", "refresh_error"):
                with self.subTest(missing=missing):
                    dialog._decision_active = True
                    dialog._checkpoint_decisions = True
                    dialog._decision_finished(
                        SimpleNamespace(
                            **{
                                key: value
                                for key, value in complete.items()
                                if key != missing
                            }
                        ),
                        None,
                    )
                    self.assertIn("SAVE FAILED", dialog.status.text())
                    self.assertIn("reload review", dialog.status.text())
                    self.assertTrue(dialog._load_failed)
                    self.assertFalse(dialog.accept_button.isEnabled())
                    self.assertFalse(dialog._decision_active)
                    self.assertIs(dialog.bundle, bundle)

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
                dialog.cohort_choice.currentText().startswith("Cohort 1 of 1")
            )
            self.assertNotEqual(
                dialog._selected_sample().workspace_id, first.workspace_id
            )
            self.assertIn("Cohort 1 of 1", dialog.cohort_scope.text())
            self.assertEqual(
                dialog.overall_progress.format(),
                "1 of 2 cohorts completed in this review session",
            )

    def test_final_checkpoint_is_complete_even_when_refresh_has_nothing_to_load(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            document = deepcopy(bundle.document)
            document.update(
                cohort_count=0,
                cohorts=[],
                sample_item_count=0,
                pending_item_count=0,
            )
            completed = replace(bundle, document=document)
            dialog._checkpoint_decisions = True
            dialog._decision_active = True

            dialog._decision_finished(
                SimpleNamespace(
                    projection=SimpleNamespace(next_bundle=completed),
                    checkpoint_error=None,
                    bundle=None,
                    samples=(),
                    refresh_error=RuntimeError("no remaining cohort"),
                    commit_seconds=0,
                    checkpoint_seconds=0,
                    refresh_seconds=0,
                ),
                None,
            )

            self.assertEqual(
                dialog.status.text(),
                "COMPLETE: all required cohorts are saved",
            )
            self.assertEqual(dialog.summary.text(), "")
            self.assertTrue(dialog.sample_group.isHidden())
            self.assertTrue(dialog.cohort_group.isHidden())
            self.assertTrue(dialog.table.isHidden())
            self.assertTrue(dialog.accept_button.isHidden())
            self.assertEqual(dialog.leave_undecided.text(), "Close")
            self.assertEqual(dialog.sample_position.text(), "Review complete")
            self.assertFalse(dialog.retry_load.isVisible())

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

    def test_close_before_end_of_media_cleanup_preserves_heard_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            publication = root / "bundle.json"
            write_cohort_review_bundle(self.create_bundle(root), publication)
            dialog = CohortReviewBundleDialog(publication)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            dialog.player.stop.reset_mock()
            dialog.player.setSource.reset_mock()

            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            dialog.player.stop.assert_not_called()
            dialog.player.setSource.assert_not_called()
            dialog.close()

            self.assertTrue(dialog.isVisible())
            self.wait_for(lambda: not dialog.isVisible())
            self.assertTrue((root / "bundle.observations.json").is_file())
            reopened = CohortReviewBundleDialog(publication)
            reopened.show()
            self.wait_for(lambda: reopened.table.rowCount() == 1)
            key = (sample.workspace_id, sample.cohort_id)
            self.assertIn(sample.item.queue_id, reopened.heard[key])

    def test_checkpoint_error_remains_visible_after_deferred_playback_cleanup(self):
        def failing_writer(*_arguments):
            raise OSError("checkpoint disk failure")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            publication = root / "bundle.json"
            write_cohort_review_bundle(self.create_bundle(root), publication)
            dialog = CohortReviewBundleDialog(
                publication, observation_writer=failing_writer
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            callbacks = []
            with patch(
                "vntts.authoring.cohort_bundle_ui.QTimer.singleShot",
                side_effect=lambda _delay, *args: callbacks.append(args[-1]),
            ):
                dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
                dialog.close()
                self.assertTrue(dialog.isVisible())
                self.wait_for(lambda: not dialog._observation_active)
                self.assertIn(
                    "checkpoint disk failure", dialog.error_details.toPlainText()
                )
                self.assertFalse(dialog._close_after_observation)
                self.assertEqual(len(callbacks), 1)
                callbacks.pop()()
            self.assertIn("LISTENING CHECKPOINT FAILED", dialog.status.text())
            self.assertIn("checkpoint disk failure", dialog.error_details.toPlainText())
            self.assertTrue(dialog.isVisible())

    def test_retry_progress_persists_failed_listening_and_defect_observations(self):
        attempts = []

        def fail_once(*arguments):
            attempts.append(arguments)
            if len(attempts) == 1:
                raise OSError("checkpoint disk failure")
            return write_cohort_review_observations(*arguments)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            publication = root / "bundle.json"
            write_cohort_review_bundle(self.create_bundle(root), publication)
            dialog = CohortReviewBundleDialog(publication, observation_writer=fail_once)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            queue_id = sample.item.queue_id
            dialog.heard[key].add(queue_id)
            dialog.bad[key].add(queue_id)
            dialog.bad_reasons[key][queue_id] = {"repetition"}
            dialog._checkpoint_observations()
            self.wait_for(lambda: not dialog._observation_active)

            self.assertIn("checkpoint disk failure", dialog.error_details.toPlainText())
            self.assertTrue(dialog.retry_progress.isVisible())
            self.assertTrue(dialog.retry_progress.isEnabled())
            dialog.retry_progress.click()
            self.wait_for(lambda: not dialog._observation_active)
            self.assertEqual(len(attempts), 2)
            self.assertFalse(dialog.retry_progress.isVisible())
            self.assertNotIn("FAILED", dialog.status.text())
            dialog.close()

            reopened = CohortReviewBundleDialog(publication)
            self.wait_for(lambda: reopened.table.rowCount() == 1)
            self.assertIn(queue_id, reopened.heard[key])
            self.assertIn(queue_id, reopened.bad[key])
            self.assertEqual(reopened.bad_reasons[key][queue_id], {"repetition"})

    def test_observation_completion_preserves_blocking_decision_failure(self):
        for observation_error in (None, OSError("checkpoint write failure")):
            with (
                self.subTest(error=observation_error),
                TemporaryDirectory() as directory,
            ):
                dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
                dialog.show()
                self.wait_for(lambda: dialog.table.rowCount() == 1)
                dialog._observation_active = True
                dialog._decision_active = True

                dialog._decision_finished(
                    None, OSError("postcommit projection failure")
                )
                dialog._observation_finished(None, observation_error)

                self.assertIn("SAVE FAILED", dialog.status.text())
                self.assertIn(
                    "postcommit projection failure", dialog.error_details.toPlainText()
                )
                self.assertTrue(dialog._load_failed)
                self.assertTrue(dialog.retry_load.isVisible())
                self.assertTrue(dialog.retry_load.isEnabled())
                self.assertFalse(dialog.accept_button.isEnabled())
                self.assertFalse(dialog.reject_button.isEnabled())
                if observation_error is not None:
                    self.assertEqual(dialog._observation_error, str(observation_error))
                    self.assertTrue(dialog.retry_progress.isVisible())
                    self.assertTrue(dialog.retry_progress.isEnabled())
                dialog._observation_error = None
                dialog.close()

    def test_bad_mark_immediately_exposes_pending_checkpoint_and_close_guard(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            publication = root / "bundle.json"
            write_cohort_review_bundle(self.create_bundle(root), publication)
            dialog = CohortReviewBundleDialog(publication)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            dialog.heard[dialog._current_key()].add(sample.item.queue_id)
            pool = ManualThreadPool()
            dialog._observation_runner.thread_pool = pool

            dialog.toggle_bad()

            self.assertTrue(dialog._observation_active)
            self.assertTrue(dialog.operation.isVisible())
            self.assertIn("Saving listening progress", dialog.operation.text())
            self.assertTrue(dialog.progress.isVisible())
            self.assertFalse(dialog.leave_undecided.isEnabled())
            pool.run_next()
            self.wait_for(lambda: not dialog._observation_active)
            self.assertTrue(dialog.leave_undecided.isEnabled())
            self.assertTrue((root / "bundle.observations.json").is_file())

    def test_successful_checkpoint_preserves_current_playback_and_save_headlines(self):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            playing = dialog.status.text()
            self.assertTrue(playing.startswith("PLAYING:"))
            dialog._observation_active = True

            dialog._observation_finished(None, None)

            self.assertEqual(dialog.status.text(), playing)
            dialog.stop_playback()
            dialog._decision_active = True
            saving = "SAVING accepted: the current cohort"
            dialog._set_status(saving)
            dialog._observation_active = True
            try:
                dialog._observation_finished(None, None)

                self.assertEqual(dialog.status.text(), saving)
            finally:
                dialog._decision_active = False

    def test_accepted_decision_preserves_exact_heard_evidence_after_checkpoint_failure(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            publication = root / "bundle.json"
            write_cohort_review_bundle(self.create_bundle(root), publication)
            dialog = CohortReviewBundleDialog(
                publication,
                observation_writer=Mock(side_effect=OSError("listening disk failure")),
                confirmer=lambda *_args: True,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.wait_for(
                lambda: (
                    not dialog._observation_active and dialog._playback_buffer is None
                )
            )
            self.assertIsNotNone(dialog._observation_error)
            self.assertTrue(dialog.accept_button.isEnabled())

            dialog.accept_button.click()
            self.wait_for(lambda: not dialog._decision_active)
            workspace = Path(
                next(
                    source["workspace"]
                    for source in dialog.original_bundle.document["sources"]
                    if source["workspace_id"] == sample.workspace_id
                )
            )
            decision_path = next((workspace / "cohort-reviews").glob("decision-*.json"))
            decision = json.loads(decision_path.read_text())
            self.assertEqual(decision["decision"], "accepted")
            self.assertEqual(
                decision["reviewed_samples"][0]["queue_id"], sample.item.queue_id
            )
            self.assertEqual(
                decision["reviewed_samples"][0]["audio_sha256"],
                sample.item.authority.audio_sha256,
            )
            self.assertEqual(
                decision["sample_assessments"][0]["assessment"], "acceptable"
            )
            with patch(
                "vntts.authoring.cohort_bundle_ui.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Discard,
            ):
                dialog.close()

            reopened = CohortReviewBundleDialog(publication)
            self.wait_for(lambda: reopened.table.rowCount() == 1)
            self.assertEqual(reopened.bundle.document["cohort_count"], 1)
            self.assertNotEqual(
                reopened._selected_sample().workspace_id, sample.workspace_id
            )
            state = json.loads(
                (workspace / "generated-audio/generation-state.json").read_text()
            )
            self.assertEqual(
                state["items"][sample.item.queue_id]["review_status"], "approved"
            )

    def test_long_error_copies_completely_and_keyboard_navigation_recovers(self):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            diagnostic = (
                "\n".join(
                    f"{index}: <b>literal error</b> & unavailable source"
                    for index in range(200)
                )
                + "\nFINAL-DIAGNOSTIC-MARKER"
            )
            dialog._mark_load_failed(diagnostic)
            clipboard = self.application.clipboard()
            previous_clipboard = clipboard.text()
            try:
                dialog.error_details.setFocus()
                QTest.keySequence(
                    dialog.error_details,
                    QKeySequence(QKeySequence.StandardKey.SelectAll),
                )
                QTest.keySequence(
                    dialog.error_details, QKeySequence(QKeySequence.StandardKey.Copy)
                )
                self.assertEqual(clipboard.text(), diagnostic)
                self.assertTrue(clipboard.text().endswith("FINAL-DIAGNOSTIC-MARKER"))
            finally:
                clipboard.setText(previous_clipboard)

            for _step in range(20):
                if self.application.focusWidget() is dialog.retry_load:
                    break
                QTest.keyClick(self.application.focusWidget(), Qt.Key.Key_Tab)
            self.assertIs(self.application.focusWidget(), dialog.retry_load)
            QTest.keyClick(dialog.retry_load, Qt.Key.Key_Space)
            self.wait_for(lambda: not dialog._load_active)
            self.assertFalse(dialog._load_failed)
            self.assertTrue(dialog.error_details.isHidden())
            for _step in range(20):
                if self.application.focusWidget() is dialog.replay:
                    break
                QTest.keyClick(self.application.focusWidget(), Qt.Key.Key_Tab)
            self.assertIs(self.application.focusWidget(), dialog.replay)
            QTest.keyClick(dialog.replay, Qt.Key.Key_Space)
            self.wait_for(lambda: dialog._playback_target is not None)

    def test_enlarged_provenance_wraps_and_copies_literal_identifiers_without_trapping_focus(
        self,
    ):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            font = dialog.font()
            font.setPointSize(22)
            dialog.setFont(font)
            dialog.resize(900, 640)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            dialog.technical_details.setChecked(True)
            dialog.decision_context.technical_toggle.setChecked(True)
            provenance = (
                "Exact model: C:\\models\\<literal>&configuration\\"
                + "long-model-" * 60
                + "\nWorkspace: "
                + "abcdef0123456789" * 40
                + "\nFINAL-PROVENANCE-MARKER"
            )
            dialog.decision_context.set_context(
                dict(dialog.decision_context.values), technical=provenance
            )
            details = dialog.decision_context.technical
            dialog.review_scroll.ensureWidgetVisible(details)
            self.application.processEvents()
            self.assertTrue(details.isVisible())
            self.assertEqual(details.horizontalScrollBar().maximum(), 0)
            self.assertEqual(dialog.review_scroll.horizontalScrollBar().maximum(), 0)
            clipboard = self.application.clipboard()
            previous_clipboard = clipboard.text()
            try:
                details.setFocus()
                QTest.keySequence(
                    details, QKeySequence(QKeySequence.StandardKey.SelectAll)
                )
                QTest.keySequence(details, QKeySequence(QKeySequence.StandardKey.Copy))
                self.assertEqual(clipboard.text(), provenance)
            finally:
                clipboard.setText(previous_clipboard)
            QTest.keyClick(details, Qt.Key.Key_Tab)
            self.assertIsNot(self.application.focusWidget(), details)
            self.assertIsNotNone(self.application.focusWidget())

    def test_failed_progress_requires_explicit_discard_before_close(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            publication = root / "bundle.json"
            write_cohort_review_bundle(self.create_bundle(root), publication)
            dialog = CohortReviewBundleDialog(
                publication, observation_writer=Mock(side_effect=OSError("disk full"))
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(sample.item.queue_id)
            dialog._checkpoint_observations()
            self.wait_for(lambda: not dialog._observation_active)

            with patch(
                "vntts.authoring.cohort_bundle_ui.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Cancel,
            ) as confirm:
                dialog.close()
                self.assertTrue(dialog.isVisible())
                self.assertIn(sample.item.queue_id, dialog.heard[key])
                confirm.assert_called_once()
                self.assertEqual(
                    confirm.call_args.args[-1], QMessageBox.StandardButton.Cancel
                )

            with patch(
                "vntts.authoring.cohort_bundle_ui.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Discard,
            ):
                dialog.close()
                self.assertFalse(dialog.isVisible())

    def test_close_during_loading_finishes_after_successful_load(self):
        with TemporaryDirectory() as directory:
            dialog = CohortReviewBundleDialog(self.create_bundle(Path(directory)))
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            pool = ManualThreadPool()
            dialog._load_runner.thread_pool = pool
            dialog.reload_bundle()

            dialog.close()
            self.assertTrue(dialog.isVisible())
            pool.run_next()
            self.wait_for(lambda: not dialog.isVisible())

    def test_postcommit_failure_blocks_stale_decision_and_retry_recovers_saved_scope(
        self,
    ):
        for published in (True, False):
            with self.subTest(published=published), TemporaryDirectory() as directory:
                root = Path(directory)
                bundle = self.create_bundle(root)
                publication = root / "bundle.json"
                write_cohort_review_bundle(bundle, publication)
                confirmer = Mock(return_value=True)
                dialog = CohortReviewBundleDialog(
                    publication if published else bundle, confirmer=confirmer
                )
                dialog.show()
                self.wait_for(lambda: dialog.table.rowCount() == 1)
                sample = dialog._selected_sample()
                dialog.heard[dialog._current_key()].add(sample.item.queue_id)
                dialog._update_actions()

                with patch(
                    "vntts.authoring.cohort_bundle._reconcile_cohort_source",
                    side_effect=OSError("successor temporarily unavailable"),
                ):
                    dialog.apply_decision("accepted")
                    self.wait_for(lambda: not dialog._decision_active)

                self.assertTrue(dialog.retry_load.isVisible())
                self.assertFalse(dialog.accept_button.isEnabled())
                self.assertEqual(dialog.table.rowCount(), 0)
                self.assertIn(
                    "successor temporarily unavailable",
                    dialog.error_details.toPlainText(),
                )
                dialog.apply_decision("accepted")
                confirmer.assert_called_once()

                dialog.retry_load.click()
                self.wait_for(lambda: not dialog._load_active)
                self.assertFalse(dialog._load_failed)
                self.assertEqual(dialog.cohort_choice.count(), 1)
                self.assertNotEqual(
                    dialog._selected_sample().workspace_id, sample.workspace_id
                )
                dialog.close()

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
            with release_and_wait_for_pool(
                dialog.thread_pool,
                release,
                finish=lambda: self.wait_for(lambda: not dialog._observation_active),
            ):
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
                dialog.reject()
                self.application.processEvents()

                self.assertEqual(heartbeat, [True])
                self.assertFalse(close_event.isAccepted())
                self.assertFalse(dialog.replay.isEnabled())
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

    def test_real_more_evidence_adds_one_unheard_sample_and_restores_prior_defect(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, _state_path = create_multisample_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            dialog = CohortReviewBundleDialog(
                publication, confirmer=lambda *_args: True
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 3)
            key = dialog._current_key()
            previous_ids = {
                sample.item.queue_id for sample in dialog._current_samples()
            }
            dialog.heard[key].update(previous_ids)
            bad_id = dialog._selected_sample().item.queue_id
            dialog._update_actions()
            dialog.defect_checks["repetition"].setChecked(True)
            self.wait_for(lambda: not dialog._observation_active)
            self.assertTrue(dialog.need_another.isEnabled())

            dialog.need_another.click()
            self.wait_for(
                lambda: not dialog._decision_active and dialog.table.rowCount() == 4
            )

            current_ids = {sample.item.queue_id for sample in dialog._current_samples()}
            self.assertEqual(len(current_ids - previous_ids), 1)
            self.assertEqual(dialog.heard[key], previous_ids)
            self.assertEqual(dialog.bad_reasons[key][bad_id], {"repetition"})
            self.assertFalse(dialog.accept_button.isEnabled())
            dialog.close()

            reopened = CohortReviewBundleDialog(publication)
            self.wait_for(lambda: reopened.table.rowCount() == 4)
            self.assertEqual(reopened.heard[key], previous_ids)
            self.assertEqual(reopened.bad_reasons[key][bad_id], {"repetition"})
            self.assertFalse(reopened.accept_button.isEnabled())

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
            self.assertIn("SAVED", dialog.status.text())
            self.assertIn("next cohort", dialog.status.text())
            self.assertIn("commit", dialog.status.toolTip())
            self.assertIn("checkpoint", dialog.status.toolTip())

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
            self.assertEqual(
                dialog.sample_text.text(), "Retry loading the samples to begin."
            )

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
            self.assertFalse(dialog.accept_button.isEnabled())
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
            self.assertFalse(dialog.accept_button.isEnabled())
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

            self.assertIn(
                "does not match every remaining cohort",
                dialog.error_details.toPlainText(),
            )
            self.assertIn("prompt_sha256", dialog.error_details.toPlainText())
            self.assertEqual(dialog.table.rowCount(), 0)
            self.assertFalse(dialog.accept_button.isEnabled())
            self.assertFalse(dialog.reject_button.isEnabled())
            self.assertFalse((Path(directory) / "bundle.progress.json").exists())


if __name__ == "__main__":
    unittest.main()

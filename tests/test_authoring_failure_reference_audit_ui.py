import io
import json
import os
import time
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    from tests import test_authoring_failure_reference_audit
    from vntts.authoring.failure_reference_audit import (
        load_failure_reference_decisions,
        prepare_failure_reference_audio,
        publish_failure_reference_audit,
    )
    from vntts.authoring.failure_reference_audit_ui import FailureReferenceAuditDialog
    from vntts.authoring.failure_reference_audit_ui import main as reference_audit_main
    from vntts.authoring.failure_reference_preview import FailureReferencePreview
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QCloseEvent = None
    QMediaPlayer = None
    FailureReferenceAuditDialog = None


def _wav_payload():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 800)
    return output.getvalue()


class _PreviewService:
    def __init__(self, _audit):
        self.generate_calls = []
        self.cancel_calls = 0
        self.close_calls = 0

    def generate(self, group_id, candidate_id, text):
        self.generate_calls.append((group_id, candidate_id, text))
        return FailureReferencePreview(
            group_id=group_id,
            candidate_id=candidate_id,
            text=text,
            synthesis_text=text,
            text_sha256="1" * 64,
            backend="moss-tts",
            model="model",
            generation_profile="stable",
            seed=0,
            sample_rate=16_000,
            audio_sha256="2" * 64,
            payload=_wav_payload(),
        )

    def cancel(self):
        self.cancel_calls += 1

    def close(self):
        self.close_calls += 1


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class FailureReferenceAuditUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_player_patcher = patch(
            "vntts.authoring.failure_reference_audit_ui.QMediaPlayer"
        )
        cls.audio_output_patcher = patch(
            "vntts.authoring.failure_reference_audit_ui.QAudioOutput"
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
            if isinstance(widget, FailureReferenceAuditDialog):
                widget._playback_active = False
                widget._save_active = False
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
        self.fail("Timed out waiting for failed-reference audit work")

    def create_audit(self, root):
        fixture = test_authoring_failure_reference_audit.FailureReferenceAuditTest()
        workspace, _queue_id = fixture.create_failed_workspace(root)
        output = root / "audit"
        publish_failure_reference_audit(workspace, output)
        return output

    def hear_all_candidates(self, dialog):
        group = dialog._current_group()
        for index, candidate in enumerate(group["candidates"]):
            dialog.candidate_choice.setCurrentIndex(index)
            dialog._playback_target = (
                group["group_id"],
                candidate["candidate_id"],
                candidate["sha256"],
            )
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

    def test_playback_uses_immutable_bytes_and_unlocks_after_every_candidate(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            dialog.show()
            group = dialog._current_group()
            candidate_id = dialog.candidate_choice.currentData()
            prepared = prepare_failure_reference_audio(
                audit, group["group_id"], candidate_id
            )

            dialog._playback_active = True
            dialog._playback_finished(prepared, None)

            self.assertEqual(dialog._playback_buffer.data().data(), prepared.payload)
            self.assertFalse(dialog.choose.isEnabled())
            self.assertFalse(dialog.neither.isEnabled())
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.hear_all_candidates(dialog)
            self.assertTrue(dialog.choose.isEnabled())
            self.assertTrue(dialog.neither.isEnabled())
            total = len(group["candidates"])
            self.assertIn(f"{total}/{total}", dialog.candidate_heard.text())
            self.assertNotIn("source_reference", dialog.summary.text())

    def test_initial_layout_prioritizes_candidate_and_collapses_line_ids(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            dialog.show()

            self.assertFalse(dialog.cases.isVisibleTo(dialog))
            self.assertIn("Candidate 1 of", dialog.candidate_heading.text())
            self.assertIn(
                f"0/{len(dialog._current_group()['candidates'])}",
                dialog.candidate_heard.text(),
            )
            self.assertFalse(dialog.choose.isEnabled())
            self.assertFalse(dialog.neither.isEnabled())
            self.assertIn("listen through every candidate", dialog.action_reason.text())
            self.assertTrue(dialog.progress.accessibleName())
            self.assertTrue(dialog.action_reason.accessibleName())
            self.assertIn(
                "selects voice-cloning source audio", dialog.explanation.text()
            )
            self.assertIn("Voice target: Rhiannon", dialog.summary.text())
            self.assertEqual(
                dialog.decision_context.values["synthesis_voice"].text(), "Rhiannon"
            )
            self.assertIn("blinded", dialog.decision_context.values["reference"].text())
            self.assertIn(
                "does not approve",
                dialog.decision_context.values["effect"].text(),
            )
            self.assertTrue(dialog.preview_text_choice.accessibleName())
            self.assertEqual(
                dialog.decision_context.technical_toggle.text(),
                "Decision provenance",
            )
            self.assertFalse(dialog.preview_panel.isVisibleTo(dialog))
            dialog.preview_toggle.setChecked(True)
            self.assertTrue(dialog.preview_panel.isVisibleTo(dialog))
            self.assertIn(
                "without saving authoring state",
                dialog.generate_preview.accessibleDescription(),
            )
            self.assertIn(
                "does not reject the character",
                dialog.neither.accessibleDescription(),
            )

            dialog.technical_details.setChecked(True)
            self.assertTrue(dialog.cases.isVisibleTo(dialog))

    def test_scaled_font_keeps_keyboard_journey_scroll_reachable(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            dialog.preview_toggle.setChecked(True)
            dialog.technical_details.setChecked(True)
            base_point_size = dialog.font().pointSizeF()
            for scale in (1.5, 2.0):
                font = dialog.font()
                font.setPointSizeF(base_point_size * scale)
                dialog.setFont(font)
                dialog.resize(dialog.minimumSize())
                dialog.show()
                self.application.processEvents()
                self.assertEqual(
                    dialog.review_scroll.horizontalScrollBar().maximum(), 0
                )

            self.assertGreater(dialog.review_scroll.verticalScrollBar().maximum(), 0)
            self.assertTrue(dialog.close_button.isVisible())
            self.assertIs(dialog.group_label.buddy(), dialog.group_choice)
            self.assertIs(dialog.candidate_label.buddy(), dialog.candidate_choice)
            self.assertIs(dialog.preview_text_label.buddy(), dialog.preview_text_choice)
            self.assertIs(
                dialog.decision_context.technical_toggle.nextInFocusChain(),
                dialog.group_choice,
            )
            self.assertIs(
                dialog.technical_details.nextInFocusChain(), dialog.close_button
            )
            for button in (
                dialog.play,
                dialog.stop,
                dialog.generate_preview,
                dialog.replay_preview,
                dialog.cancel_preview,
                dialog.choose,
                dialog.neither,
                dialog.previous,
                dialog.next,
                dialog.close_button,
            ):
                self.assertTrue(button.accessibleName(), button.text())
                self.assertTrue(button.accessibleDescription(), button.text())
            dialog.close()

    def test_direct_decision_is_blocked_until_all_candidates_are_heard(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)

            dialog.save_decision(dialog.candidate_choice.currentData())

            self.assertFalse(dialog._save_active)
            self.assertIn("listen through every candidate", dialog.status.text())
            self.assertEqual(load_failure_reference_decisions(audit)["decisions"], [])

    def test_save_runs_in_background_and_advances_progress(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            dialog.show()

            self.hear_all_candidates(dialog)
            dialog.choose_selected()
            self.assertTrue(dialog._save_active)
            self.assertTrue(dialog.play.isEnabled())
            self.wait_for(lambda: not dialog._save_active)

            decisions = load_failure_reference_decisions(audit)
            self.assertEqual(len(decisions["decisions"]), 1)
            self.assertIn("SAVED", dialog.status.text())

    def test_generated_preview_is_optional_immutable_evidence(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            service = _PreviewService(audit)
            dialog = FailureReferenceAuditDialog(
                audit, preview_service_factory=lambda _audit: service
            )
            dialog.show()
            group = dialog._current_group()
            candidate_id = dialog.candidate_choice.currentData()
            text = dialog.preview_text_choice.currentData()

            dialog.generate_selected_preview()
            self.assertTrue(dialog._preview_active)
            self.assertTrue(dialog.play.isEnabled())
            self.wait_for(lambda: not dialog._preview_active)

            self.assertEqual(
                service.generate_calls,
                [(group["group_id"], candidate_id, text)],
            )
            self.assertEqual(dialog._playback_kind, "generated")
            self.assertEqual(dialog._playback_buffer.data().data(), _wav_payload())
            self.assertIn("does not select the reference", dialog.status.text())
            self.assertFalse(dialog.choose.isEnabled())
            self.assertFalse(dialog.neither.isEnabled())
            self.assertEqual(load_failure_reference_decisions(audit)["decisions"], [])

            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertIn("GENERATED SAMPLE HEARD", dialog.status.text())
            self.assertFalse(dialog.choose.isEnabled())
            dialog.replay_generated_preview()
            self.assertEqual(dialog._playback_kind, "generated")

    def test_single_candidate_group_uses_binary_reference_wording(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            group = dialog._current_group()
            group["candidates"] = group["candidates"][:1]
            dialog.candidate_choice.blockSignals(True)
            dialog.candidate_choice.clear()
            dialog.candidate_choice.addItem("Candidate 1 of 1", "candidate-01")
            dialog.candidate_choice.blockSignals(False)

            dialog._update_candidate_card()

            self.assertEqual(dialog.choose.text(), "Use this reference")
            self.assertEqual(dialog.neither.text(), "This reference is unsuitable")

    def test_preview_generation_does_not_block_reference_decision_controls(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            dialog.show()
            self.hear_all_candidates(dialog)
            dialog._preview_active = True

            dialog._update_actions()

            self.assertTrue(dialog.play.isEnabled())
            self.assertTrue(dialog.choose.isEnabled())
            self.assertTrue(dialog.neither.isEnabled())
            self.assertTrue(dialog.cancel_preview.isEnabled())
            self.assertIn("remain available", dialog.action_reason.text())

    def test_close_is_deferred_during_checksum_work(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            dialog._save_active = True
            event = QCloseEvent()

            dialog.closeEvent(event)

            self.assertFalse(event.isAccepted())
            self.assertIn("Close deferred", dialog.status.text())

    def test_close_is_deferred_during_preview_generation(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            service = _PreviewService(audit)
            dialog = FailureReferenceAuditDialog(
                audit, preview_service_factory=lambda _audit: service
            )
            dialog._preview_active = True
            event = QCloseEvent()

            dialog.closeEvent(event)

            self.assertFalse(event.isAccepted())
            self.assertEqual(service.close_calls, 0)
            self.assertIn("Close deferred", dialog.status.text())

    def test_status_is_read_only_and_does_not_create_qt_state(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            output = io.StringIO()

            with redirect_stdout(output):
                self.assertEqual(
                    reference_audit_main([str(audit), "--status"]),
                    0,
                )

            progress = json.loads(output.getvalue())
            self.assertEqual(progress["completed_groups"], 0)
            self.assertEqual(progress["remaining_groups"], 1)
            self.assertEqual(progress["total_groups"], 1)
            self.assertIsNone(progress["decision_set_id"])
            self.assertFalse((audit / "decisions.json").exists())


if __name__ == "__main__":
    unittest.main()

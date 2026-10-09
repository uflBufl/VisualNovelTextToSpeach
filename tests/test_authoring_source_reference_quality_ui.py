import hashlib
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from tests.source_reference_fixtures import write_quality_session
from vntts.authoring.source_reference_quality import (
    load_source_reference_quality_review,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from vntts.authoring.source_reference_quality_ui import (
        SourceReferenceQualityDialog,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QCloseEvent = None
    QMediaPlayer = None
    QTest = None
    QTimer = None
    SourceReferenceQualityDialog = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class SourceReferenceQualityDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_player_patcher = patch(
            "vntts.authoring.source_reference_quality_ui.QMediaPlayer"
        )
        media_player = cls.media_player_patcher.start()
        media_player.MediaStatus = QMediaPlayer.MediaStatus
        cls.application = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.media_player_patcher.stop()

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("Timed out waiting for the Qt worker")

    @staticmethod
    def finish_audio(dialog, token):
        dialog._playing_token = token
        dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

    def authorize_accept(self, dialog):
        self.finish_audio(dialog, "reference")
        for sample in dialog.current["generated_samples"]:
            self.finish_audio(dialog, sample["queue_id"])

    def test_decisions_require_exact_completed_audio_evidence(self):
        with TemporaryDirectory() as directory:
            session = write_quality_session(Path(directory))
            dialog = SourceReferenceQualityDialog(
                session, confirmer=lambda _decision: True
            )

            self.assertFalse(dialog.accept_button.isEnabled())
            self.assertFalse(dialog.reject_reference.isEnabled())
            self.assertFalse(dialog.needs_sample.isEnabled())
            self.assertFalse(dialog.stop.isEnabled())
            self.assertIn("original 0/1", dialog.evidence_progress.text())
            self.assertFalse(dialog.portrait_image.pixmap().isNull())
            self.assertNotIn("534704", dialog.identity.text())
            self.assertIn("hero.bnk", dialog.reference_details.text())
            self.assertNotIn("Original media: 123", dialog.reference_details.text())
            self.assertEqual(dialog.progress.text(), "Decisions: 0/1")
            self.assertIn("Text: Generated sample 1.", dialog.generated_details.text())
            self.assertIn("not heard", dialog.generated.item(0).text())
            self.assertTrue(
                dialog.generated_details.textInteractionFlags()
                & Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.assertEqual(
                dialog.decision_context.values["game_speaker"], "Dobharchu"
            )
            self.assertIn(
                "legacy review format",
                dialog.decision_context.values["model"],
            )
            self.assertIn(
                "later voice binding",
                dialog.decision_context.values["effect"],
            )

            self.finish_audio(dialog, "reference")
            self.assertFalse(dialog.stop.isEnabled())
            self.assertFalse(dialog.accept_button.isEnabled())
            self.assertTrue(dialog.reject_reference.isEnabled())
            self.assertTrue(dialog.needs_sample.isEnabled())
            self.finish_audio(dialog, "queue-1")
            self.assertFalse(dialog.accept_button.isEnabled())
            self.assertTrue(dialog.generated.item(0).text().startswith("1. heard"))
            self.assertTrue(dialog.generated.item(1).text().startswith("2. not heard"))
            self.finish_audio(dialog, "queue-2")
            self.assertTrue(dialog.accept_button.isEnabled())
            self.assertTrue(dialog.reject_reference.isEnabled())
            self.assertTrue(dialog.needs_sample.isEnabled())
            dialog._decide("reject")
            self.wait_for(lambda: not dialog._decision_active)
            result = load_source_reference_quality_review(session)
            self.assertIsNone(dialog.current)
            self.assertTrue(dialog.play_reference.isHidden())
            self.assertTrue(dialog.accept_button.isHidden())
            self.assertTrue(dialog.decision_context.isHidden())
            self.assertLessEqual(dialog.height(), 220)
            dialog.close()

        self.assertEqual(result["variants"][0]["decision"]["decision"], "reject")

    def test_checksum_change_blocks_playback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = write_quality_session(root)
            dialog = SourceReferenceQualityDialog(session)
            (root / "reference.wav").write_bytes(b"changed")

            dialog._play_reference()
            self.wait_for(lambda: not dialog.playback_runner.active)
            message = dialog.status.text()
            dialog.close()

        self.assertIn("checksum changed", message)

    def test_irreversible_decision_can_be_cancelled(self):
        with TemporaryDirectory() as directory:
            session = write_quality_session(Path(directory))
            dialog = SourceReferenceQualityDialog(
                session, confirmer=lambda _decision: False
            )
            self.authorize_accept(dialog)

            dialog._decide("accept")

            self.assertFalse(dialog._decision_active)
            self.assertIn("cancelled", dialog.status.text())
            dialog.close()

    def test_generated_playback_is_cancelled_when_row_changes_during_prepare(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = write_quality_session(root)
            dialog = SourceReferenceQualityDialog(session)
            first = dialog.current["generated_samples"][0]
            payload = (root / first["audio"]).read_bytes()

            dialog.generated.setCurrentRow(1)
            dialog._playback_prepared(
                (
                    dialog.current["variant_id"],
                    first["queue_id"],
                    first["audio_sha256"],
                    payload,
                ),
                None,
            )

            self.assertIsNone(dialog._playing_token)
            self.assertIn("audio selection changed", dialog.status.text())
            self.assertIn("Sample 2", dialog.generated_details.text())
            dialog.close()

    def test_changing_sample_stops_old_audio_without_listening_credit(self):
        with TemporaryDirectory() as directory:
            session = write_quality_session(Path(directory))
            dialog = SourceReferenceQualityDialog(session)
            dialog._playing_token = dialog.current["generated_samples"][0]["queue_id"]

            dialog.generated.setCurrentRow(1)
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

            self.assertIsNone(dialog._playing_token)
            self.assertEqual(dialog.completed_audio, set())
            self.assertIn("sample selection changed", dialog.status.text())
            self.assertIn("Sample 2", dialog.generated_details.text())
            dialog._playing_token = "reference"
            dialog._stop()
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertEqual(dialog.completed_audio, set())
            dialog.close()

    def test_missing_exact_portrait_uses_truthful_placeholder(self):
        with TemporaryDirectory() as directory:
            session = write_quality_session(Path(directory))
            document = json.loads(session.read_text(encoding="utf-8"))
            document["variants"][0]["portrait_image"] = None
            session.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            dialog = SourceReferenceQualityDialog(session)

            message = dialog.portrait_image.text()
            maximum_height = dialog.portrait_image.maximumHeight()
            dialog.close()

        self.assertEqual(message, "Exact game portrait is not installed")
        self.assertLessEqual(maximum_height, 48)

    def test_decision_context_normalizes_strings_and_preserves_summary_widgets(self):
        from vntts.authoring.review_context_ui import ReviewDecisionContext

        card = ReviewDecisionContext()
        self.addCleanup(card.deleteLater)
        card.set_context(
            {"purpose": "  Compare  ", "reference": None, "controls": 0, "effect": " "},
            technical="  provenance  ",
        )
        self.assertTrue(all(isinstance(value, str) for value in card.values.values()))
        self.assertEqual(card.values["reference"], "Unknown")
        self.assertEqual(card.purpose.text(), "You are deciding: Compare")
        self.assertEqual(
            card.identity.text(),
            "Speaker in game: Unknown | Voice used: Unknown | Reference: Unknown",
        )
        self.assertEqual(
            card.synthesis.text(), "Synthesis: Unknown | Unknown | Unknown | 0"
        )
        self.assertEqual(card.effect.text(), "Your decision will: Unknown")
        self.assertEqual(card.identity.accessibleName(), "Speaker voice and reference")
        self.assertTrue(card.identity.wordWrap())
        self.assertTrue(
            card.identity.textInteractionFlags()
            & Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.assertEqual(card.technical.text(), "provenance")
        card.technical_toggle.setChecked(True)
        card.set_context({})
        self.assertEqual(card.purpose.text(), "You are deciding: Unknown")
        self.assertEqual(card.values["controls"], "Unknown")
        self.assertTrue(card.technical_toggle.isHidden())
        self.assertFalse(card.technical_toggle.isChecked())
        self.assertTrue(card.technical.isHidden())

    def test_model_label_shortens_both_native_path_formats(self):
        from vntts.authoring.review_context_ui import review_model_label

        for model in (
            r"C:\Users\runner\AppData\Local\models\moss-test",
            r"\\server\models\moss-test",
            "/var/cache/models/moss-test",
            "OpenMOSS/moss-test",
            "moss-test",
        ):
            with self.subTest(model=model):
                self.assertEqual(review_model_label(model), "moss-test")

    def test_offscreen_font_has_real_proportional_glyphs(self):
        from PySide6.QtGui import QFont, QFontMetrics

        metrics = QFontMetrics(QFont("Arial", 12))
        self.assertLess(
            metrics.horizontalAdvance("i"),
            metrics.horizontalAdvance("W"),
            "Qt is measuring missing-glyph boxes; check QT_QPA_FONTDIR",
        )

    def test_empty_generated_evidence_and_technical_failures_stay_compact(self):
        with TemporaryDirectory() as directory:
            session = write_quality_session(Path(directory))
            document = json.loads(session.read_text(encoding="utf-8"))
            document["variants"][0]["generated_samples"] = []
            text = "Generation failed."
            document["variants"][0]["excluded_results"] = [
                {
                    "queue_id": "queue-failed",
                    "evaluation_kind": "fixed-1",
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "status": "failed",
                    "attempts": 1,
                    "failure_kind": "limited",
                    "error": "No WAV was published",
                }
            ]
            session.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            dialog = SourceReferenceQualityDialog(session)

            self.assertTrue(dialog.generated.isHidden())
            self.assertIn("No published generated", dialog.generated_details.text())
            self.assertEqual(dialog.technical_toggle.text(), "Diagnostics (1)")
            self.assertTrue(dialog.failures.isHidden())
            dialog.technical_toggle.setChecked(True)
            self.assertFalse(dialog.failures.isHidden())
            self.assertIn("No WAV was published", dialog.failures.text())
            self.finish_audio(dialog, "reference")
            self.assertFalse(dialog.accept_button.isEnabled())
            self.assertTrue(dialog.reject_reference.isEnabled())
            self.assertTrue(dialog.needs_sample.isEnabled())
            dialog.close()

    def test_keyboard_accessibility_and_compact_layout(self):
        with TemporaryDirectory() as directory:
            session = write_quality_session(Path(directory))
            dialog = SourceReferenceQualityDialog(session)
            dialog.resize(700, 500)
            dialog.layout().activate()

            for widget in (
                dialog.progress,
                dialog.portrait_image,
                dialog.identity,
                dialog.reference_details,
                dialog.generated,
                dialog.generated_details,
                dialog.play_reference,
                dialog.play_generated,
                dialog.stop,
                dialog.evidence_progress,
                dialog.technical_toggle,
                dialog.failures,
                dialog.status,
                dialog.accept_button,
                dialog.reject_reference,
                dialog.needs_sample,
            ):
                self.assertTrue(widget.accessibleName(), type(widget).__name__)
            self.assertEqual(dialog.play_reference.shortcut().toString(), "Ctrl+O")
            self.assertEqual(dialog.play_generated.shortcut().toString(), "Ctrl+G")
            self.assertEqual(dialog.accept_button.shortcut().toString(), "Ctrl+Return")
            self.assertEqual(dialog.size().width(), 700)
            self.assertEqual(dialog.size().height(), 500)
            self.assertIs(dialog.play_reference.nextInFocusChain(), dialog.generated)
            dialog.close()

    def test_scaled_font_keeps_keyboard_journey_scroll_reachable(self):
        with TemporaryDirectory() as directory:
            session = write_quality_session(Path(directory))
            dialog = SourceReferenceQualityDialog(session)
            base_point_size = max(12.0, dialog.font().pointSizeF())
            for scale in (1.5, 2.0, None):
                font = dialog.font()
                if scale is None:
                    font.setPixelSize(48)  # Exercise wide metrics on every host.
                else:
                    font.setPointSizeF(base_point_size * scale)
                dialog.setFont(font)
                dialog.resize(dialog.minimumSize())
                dialog.show()
                self.application.processEvents()
                self.assertEqual(
                    dialog.review_scroll.horizontalScrollBar().maximum(),
                    0,
                    (
                        font.toString(),
                        dialog.review_scroll.viewport().size(),
                        sorted(
                            (
                                widget.minimumSizeHint().width(),
                                widget.metaObject().className(),
                                widget.accessibleName(),
                            )
                            for widget in dialog.findChildren(
                                type(dialog.review_scroll.widget())
                            )
                            if widget.isVisible()
                        )[-10:],
                    ),
                )

            self.assertGreater(dialog.review_scroll.verticalScrollBar().maximum(), 0)
            self.assertTrue(dialog.close_button.isVisible())
            self.assertIs(dialog.generated_label.buddy(), dialog.generated)
            self.assertIs(
                dialog.decision_context.technical_toggle.nextInFocusChain(),
                dialog.accept_button,
            )
            self.assertIs(dialog.needs_sample.nextInFocusChain(), dialog.close_button)
            for button in (
                dialog.play_reference,
                dialog.play_generated,
                dialog.stop,
                dialog.technical_toggle,
                dialog.accept_button,
                dialog.reject_reference,
                dialog.needs_sample,
                dialog.close_button,
            ):
                self.assertTrue(button.accessibleName(), button.text())
                self.assertTrue(button.accessibleDescription(), button.text())
            dialog.close_button.click()
            self.assertFalse(dialog.isVisible())

    def test_catalog_completion_persists_a_valid_accepted_decision(self):
        from contextlib import ExitStack

        from scripts.render_ui_catalog import (
            _close_catalog_widget,
            _source_reference_review,
        )

        with ExitStack() as resources:
            dialog = _source_reference_review("complete", resources)
            try:
                self.assertIsNone(dialog.current)
                persisted = load_source_reference_quality_review(dialog.session_path)
                self.assertEqual(persisted["completed_count"], 1)
                self.assertEqual(
                    persisted["variants"][0]["decision"]["decision"], "accept"
                )
            finally:
                _close_catalog_widget(dialog)

    def test_complete_message_remains_visible_with_large_text(self):
        with TemporaryDirectory() as directory:
            session = write_quality_session(Path(directory))
            dialog = SourceReferenceQualityDialog(session)
            font = dialog.font()
            font.setPixelSize(48)
            dialog.setFont(font)
            current = dialog.current
            self.assertIsNotNone(current)
            dialog._load_next(
                dialog.decision_recorder(session, current["variant_id"], "accept")
            )
            persisted = load_source_reference_quality_review(session)
            self.assertEqual(persisted["completed_count"], 1)
            self.assertEqual(persisted["variants"][0]["decision"]["decision"], "accept")
            self.assertIsNone(dialog.current)
            dialog.show()
            self.application.processEvents()

            status_bottom = (
                dialog.status.mapTo(dialog.review_scroll.viewport(), QPoint(0, 0)).y()
                + dialog.status.height()
            )
            self.assertLessEqual(
                status_bottom, dialog.review_scroll.viewport().height()
            )
            self.assertIn("Only accepted references", dialog.status.text())
            dialog.close()

    def test_slow_decision_keeps_qt_responsive_and_defers_close(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = write_quality_session(root)
            started = Event()
            release = Event()

            def slow_recorder(*args):
                started.set()
                release.wait(3)
                from vntts.authoring.source_reference_quality import (
                    record_source_reference_quality_decision,
                )

                return record_source_reference_quality_decision(*args)

            dialog = SourceReferenceQualityDialog(
                session,
                decision_recorder=slow_recorder,
                confirmer=lambda _decision: True,
            )
            self.authorize_accept(dialog)
            heartbeat = []
            QTimer.singleShot(0, lambda: heartbeat.append("painted"))

            before = time.monotonic()
            dialog._decide("accept")
            elapsed = time.monotonic() - before
            self.wait_for(lambda: started.is_set() and bool(heartbeat))

            try:
                self.assertLess(elapsed, 0.1)
                self.assertTrue(dialog._decision_active)
                self.assertFalse(dialog.accept_button.isEnabled())
                self.assertTrue(dialog.play_reference.isEnabled())
                self.assertIn("Saving the exact", dialog.status.text())
                self.finish_audio(dialog, "reference")
                for button in (
                    dialog.accept_button,
                    dialog.reject_reference,
                    dialog.needs_sample,
                ):
                    self.assertFalse(button.isEnabled())
                    self.assertIn("Unavailable", button.accessibleDescription())
                    self.assertIn("Saving the exact", button.accessibleDescription())
                close_event = QCloseEvent()
                dialog.closeEvent(close_event)
                self.assertFalse(close_event.isAccepted())
                dialog.reject()
                self.assertTrue(dialog._close_pending)
                self.assertIn("Close is deferred", dialog.status.text())

            finally:
                release.set()
                self.wait_for(lambda: not dialog._decision_active)
            result = load_source_reference_quality_review(session)
            self.assertEqual(result["completed_count"], 1)

    def test_transient_decision_failure_can_retry_in_place(self):
        with TemporaryDirectory() as directory:
            session = write_quality_session(Path(directory))
            attempts = 0

            def flaky_recorder(*args):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("temporary disk failure")
                from vntts.authoring.source_reference_quality import (
                    record_source_reference_quality_decision,
                )

                return record_source_reference_quality_decision(*args)

            dialog = SourceReferenceQualityDialog(
                session,
                decision_recorder=flaky_recorder,
                confirmer=lambda _decision: True,
            )
            self.authorize_accept(dialog)
            dialog._decide("accept")
            self.wait_for(lambda: not dialog._decision_active)
            self.assertIn("Choose again to retry", dialog.status.text())
            self.assertTrue(dialog.accept_button.isEnabled())
            self.assertEqual(
                load_source_reference_quality_review(session)["completed_count"], 0
            )

            dialog._decide("accept")
            self.wait_for(lambda: not dialog._decision_active)
            self.assertEqual(
                load_source_reference_quality_review(session)["completed_count"], 1
            )
            self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()

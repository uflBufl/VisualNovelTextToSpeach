import hashlib
import json
import os
import struct
import time
import unittest
import zlib
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav

from vntts.authoring.source_reference_quality import (
    QUALITY_REVIEW_SCHEMA,
    QUALITY_REVIEW_VERSION,
    load_source_reference_quality_review,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QTimer
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


def _write_audio(root, name, value):
    path = root / name
    write_pcm16_wav(path, np.full(800, value, dtype=np.float32), 16_000)
    info = probe_pcm16_mono_wav(path)
    return {
        "audio": name,
        "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sample_rate": info.sample_rate,
        "sample_count": info.sample_count,
        "duration_seconds": round(info.duration_seconds, 6),
    }


def _write_png(root, name):
    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
    rows = b"\x00" + b"\x7f\x30\x10\xff" * 2
    payload += chunk(b"IDAT", zlib.compress(rows * 2))
    payload += chunk(b"IEND", b"")
    path = root / name
    path.write_bytes(payload)
    return {
        "image": name,
        "image_sha256": hashlib.sha256(payload).hexdigest(),
        "width": 2,
        "height": 2,
    }


def write_quality_session(root):
    reference = _write_audio(root, "reference.wav", 0.1)
    generated_one = _write_audio(root, "generated-one.wav", 0.2)
    generated_two = _write_audio(root, "generated-two.wav", 0.3)
    portrait = _write_png(root, "portrait.png")
    now = datetime.now(timezone.utc).isoformat()
    samples = []
    for index, audio in enumerate((generated_one, generated_two), start=1):
        text = f"Generated sample {index}."
        samples.append(
            {
                "queue_id": f"queue-{index}",
                "evaluation_kind": ("source-match" if index == 1 else "fixed-1"),
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                **audio,
            }
        )
    session = root / "review.json"
    session.write_text(
        json.dumps(
            {
                "schema": QUALITY_REVIEW_SCHEMA,
                "schema_version": QUALITY_REVIEW_VERSION,
                "created_at": now,
                "updated_at": now,
                "source_reference_plan_sha256": "1" * 64,
                "source_reference_evaluation_sha256": "2" * 64,
                "generation_state_sha256": "3" * 64,
                "variant_count": 1,
                "completed_count": 0,
                "variants": [
                    {
                        "variant_id": "cluster-a-anchor-1",
                        "cluster_id": "cluster-a",
                        "character": "Dobharchu",
                        "portrait": "534704",
                        "portrait_image": portrait,
                        "source_bank": "hero.bnk",
                        "media_id": 123,
                        "affected_queue_item_count": 37,
                        "reference": reference,
                        "generated_samples": samples,
                        "excluded_results": [],
                        "decision": None,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return session


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class SourceReferenceQualityDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_player_patcher = patch(
            "vntts.authoring.source_reference_quality_ui.QMediaPlayer"
        )
        cls.audio_output_patcher = patch(
            "vntts.authoring.source_reference_quality_ui.QAudioOutput"
        )
        media_player = cls.media_player_patcher.start()
        media_player.MediaStatus = QMediaPlayer.MediaStatus
        cls.audio_output_patcher.start()
        cls.application = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.audio_output_patcher.stop()
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
            dialog = SourceReferenceQualityDialog(session)

            self.assertFalse(dialog.accept.isEnabled())
            self.assertFalse(dialog.reject.isEnabled())
            self.assertFalse(dialog.needs_sample.isEnabled())
            self.assertIn("original 0/1", dialog.evidence_progress.text())
            self.assertFalse(dialog.portrait_image.pixmap().isNull())
            self.assertNotIn("534704", dialog.identity.text())
            self.assertIn("Text: Generated sample 1.", dialog.generated_details.text())
            self.assertEqual(
                dialog.decision_context.values["game_speaker"].text(), "Dobharchu"
            )
            self.assertIn(
                "legacy review format",
                dialog.decision_context.values["model"].text(),
            )
            self.assertIn(
                "later voice binding",
                dialog.decision_context.values["effect"].text(),
            )

            self.finish_audio(dialog, "reference")
            self.assertFalse(dialog.accept.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
            self.assertTrue(dialog.needs_sample.isEnabled())
            self.finish_audio(dialog, "queue-1")
            self.assertFalse(dialog.accept.isEnabled())
            self.finish_audio(dialog, "queue-2")
            self.assertTrue(dialog.accept.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
            self.assertTrue(dialog.needs_sample.isEnabled())
            dialog._decide("reject")
            self.wait_for(lambda: not dialog._decision_active)
            result = load_source_reference_quality_review(session)
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
            self.assertEqual(dialog.technical_toggle.text(), "Technical exclusions (1)")
            self.assertTrue(dialog.failures.isHidden())
            dialog.technical_toggle.setChecked(True)
            self.assertFalse(dialog.failures.isHidden())
            self.assertIn("No WAV was published", dialog.failures.text())
            self.finish_audio(dialog, "reference")
            self.assertFalse(dialog.accept.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
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
                dialog.accept,
                dialog.reject,
                dialog.needs_sample,
            ):
                self.assertTrue(widget.accessibleName(), type(widget).__name__)
            self.assertEqual(dialog.play_reference.shortcut().toString(), "Ctrl+O")
            self.assertEqual(dialog.play_generated.shortcut().toString(), "Ctrl+G")
            self.assertEqual(dialog.accept.shortcut().toString(), "Ctrl+Return")
            self.assertEqual(dialog.size().width(), 700)
            self.assertEqual(dialog.size().height(), 500)
            self.assertIs(
                dialog.play_reference.nextInFocusChain(), dialog.play_generated
            )
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
                session, decision_recorder=slow_recorder
            )
            self.authorize_accept(dialog)
            heartbeat = []
            QTimer.singleShot(0, lambda: heartbeat.append("painted"))

            before = time.monotonic()
            dialog._decide("accept")
            elapsed = time.monotonic() - before
            self.wait_for(lambda: started.is_set() and bool(heartbeat))

            self.assertLess(elapsed, 0.1)
            self.assertTrue(dialog._decision_active)
            self.assertFalse(dialog.accept.isEnabled())
            self.assertTrue(dialog.play_reference.isEnabled())
            self.assertIn("Saving the exact", dialog.status.text())
            close_event = QCloseEvent()
            dialog.closeEvent(close_event)
            self.assertFalse(close_event.isAccepted())
            self.assertIn("Close is deferred", dialog.status.text())

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
                session, decision_recorder=flaky_recorder
            )
            self.authorize_accept(dialog)
            dialog._decide("accept")
            self.wait_for(lambda: not dialog._decision_active)
            self.assertIn("Choose again to retry", dialog.status.text())
            self.assertTrue(dialog.accept.isEnabled())
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

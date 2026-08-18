import hashlib
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav

from vntts.authoring.source_reference_quality import (
    QUALITY_REVIEW_SCHEMA,
    QUALITY_REVIEW_VERSION,
    load_source_reference_quality_review,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    from vntts.authoring.source_reference_quality_ui import (
        SourceReferenceQualityDialog,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QMediaPlayer = None
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


def write_quality_session(root):
    reference = _write_audio(root, "reference.wav", 0.1)
    generated_one = _write_audio(root, "generated-one.wav", 0.2)
    generated_two = _write_audio(root, "generated-two.wav", 0.3)
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
        cls.application = QApplication.instance() or QApplication([])

    def test_decision_unlocks_only_after_original_and_every_generated_sample(self):
        with TemporaryDirectory() as directory:
            session = write_quality_session(Path(directory))
            dialog = SourceReferenceQualityDialog(session)

            self.assertFalse(dialog.accept.isEnabled())
            dialog._playing_token = "reference"
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertFalse(dialog.accept.isEnabled())
            for sample in dialog.current["generated_samples"]:
                dialog._playing_token = sample["queue_id"]
                dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertTrue(dialog.accept.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
            self.assertTrue(dialog.needs_sample.isEnabled())
            dialog._decide("reject")
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
            message = dialog.status.text()
            dialog.close()

        self.assertIn("checksum changed", message)


if __name__ == "__main__":
    unittest.main()

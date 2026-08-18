"""Qt review for one source-reference character cluster at a time."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from vntts.authoring.source_reference_quality import (
    SourceReferenceQualityError,
    load_source_reference_quality_review,
    next_pending_quality_variant,
    quality_review_progress,
    record_source_reference_quality_decision,
)


class SourceReferenceQualityDialog(QDialog):
    """Present exact original and generated evidence without cross-character A/B."""

    def __init__(self, session_path, parent=None):
        super().__init__(parent)
        self.session_path = Path(session_path).expanduser().resolve()
        self.session = load_source_reference_quality_review(self.session_path)
        self.current = None
        self.completed_audio = set()
        self._audio_buffer = None
        self._playing_token = None

        self.setWindowTitle("Source-reference quality review")
        self.setMinimumSize(780, 540)
        self.progress = QLabel()
        self.portrait_image = QLabel()
        self.portrait_image.setAccessibleName("Exact game portrait")
        self.portrait_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.portrait_image.setMinimumHeight(150)
        self.identity = QLabel()
        self.identity.setWordWrap(True)
        self.reference_details = QLabel()
        self.play_reference = QPushButton("Play original reference")
        self.play_reference.setAccessibleName("Play original source reference")
        self.play_reference.clicked.connect(self._play_reference)

        self.generated = QListWidget()
        self.generated.setAccessibleName("Generated samples for this reference")
        self.generated.currentRowChanged.connect(self._update_play_enabled)
        self.play_generated = QPushButton("Play selected generated sample")
        self.play_generated.clicked.connect(self._play_generated)
        self.stop = QPushButton("Stop audio")
        self.stop.clicked.connect(self._stop)
        playback = QHBoxLayout()
        playback.addWidget(self.play_reference)
        playback.addWidget(self.play_generated)
        playback.addWidget(self.stop)

        self.failures = QLabel()
        self.failures.setWordWrap(True)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.accept = QPushButton("Accept reference")
        self.reject = QPushButton("Reject reference")
        self.needs_sample = QPushButton("Need another sample")
        self.accept.clicked.connect(lambda: self._decide("accept"))
        self.reject.clicked.connect(lambda: self._decide("reject"))
        self.needs_sample.clicked.connect(lambda: self._decide("needs_sample"))
        decisions = QHBoxLayout()
        decisions.addWidget(self.accept)
        decisions.addWidget(self.reject)
        decisions.addWidget(self.needs_sample)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.progress)
        layout.addWidget(self.portrait_image)
        layout.addWidget(self.identity)
        layout.addWidget(self.reference_details)
        layout.addWidget(self.generated, 1)
        layout.addLayout(playback)
        layout.addWidget(self.failures)
        layout.addWidget(self.status)
        layout.addLayout(decisions)
        layout.addWidget(buttons)

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(self._playback_error)
        self._load_next()

    def _load_next(self):
        self._stop()
        self.session = load_source_reference_quality_review(self.session_path)
        completed, total = quality_review_progress(self.session)
        self.progress.setText(f"Progress: {completed}/{total}")
        self.current = next_pending_quality_variant(self.session)
        self.completed_audio.clear()
        self.generated.clear()
        if self.current is None:
            self.portrait_image.clear()
            self.identity.setText("Review complete")
            self.reference_details.clear()
            self.failures.clear()
            self.status.setText(
                "All cluster decisions are saved. Only accepted references may be "
                "published into queue bindings."
            )
            self._set_actions_enabled(False)
            self.play_reference.setEnabled(False)
            self.play_generated.setEnabled(False)
            return

        self.identity.setText(
            f"Character: {self.current['character']} | "
            f"Original media: {self.current['media_id']} | "
            f"Affected story lines: {self.current['affected_queue_item_count']}"
        )
        self._load_portrait()
        reference = self.current["reference"]
        self.reference_details.setText(
            "Original reference: "
            f"{reference['duration_seconds']:.2f}s, {reference['sample_rate']} Hz"
        )
        for sample in self.current["generated_samples"]:
            self.generated.addItem(
                f"{sample['evaluation_kind']} | {sample['text']} | "
                f"{sample['duration_seconds']:.2f}s"
            )
        if self.generated.count():
            self.generated.setCurrentRow(0)
        failure_lines = []
        for sample in self.current["excluded_results"]:
            reason = sample.get("failure_kind") or sample.get("completion") or "failed"
            failure_lines.append(
                f"Excluded {sample['evaluation_kind']}: {reason}; "
                f"{sample.get('error') or 'no WAV was published'}"
            )
        self.failures.setText(
            "\n".join(failure_lines)
            if failure_lines
            else "No excluded generation results for this cluster."
        )
        if self.generated.count():
            self.status.setText(
                "Decisions are available now. Listen to the original and generated "
                "samples before choosing; playback progress is advisory."
            )
        else:
            self.status.setText(
                "No generated sample is available. Listen to the original, then request "
                "another sample."
            )
        self.play_reference.setEnabled(True)
        self._update_play_enabled()
        self._update_decision_enabled()

    def _load_portrait(self):
        record = self.current.get("portrait_image")
        if record is None:
            self.portrait_image.setPixmap(QPixmap())
            self.portrait_image.setText("Exact game portrait is not installed")
            return
        path = (self.session_path.parent / record["image"]).resolve()
        try:
            path.relative_to(self.session_path.parent)
            payload = path.read_bytes()
        except (OSError, ValueError) as error:
            self.portrait_image.setPixmap(QPixmap())
            self.portrait_image.setText(f"Portrait unavailable: {error}")
            return
        if hashlib.sha256(payload).hexdigest() != record["image_sha256"]:
            self.portrait_image.setPixmap(QPixmap())
            self.portrait_image.setText("Portrait blocked: checksum changed")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(payload, "PNG"):
            self.portrait_image.setPixmap(QPixmap())
            self.portrait_image.setText("Portrait blocked: invalid PNG")
            return
        self.portrait_image.setText("")
        self.portrait_image.setPixmap(
            pixmap.scaled(
                204,
                204,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _update_play_enabled(self):
        self.play_generated.setEnabled(
            self.current is not None
            and self.generated.currentRow() >= 0
            and bool(self.current["generated_samples"])
        )

    def _set_actions_enabled(self, enabled):
        self.accept.setEnabled(enabled)
        self.reject.setEnabled(enabled)
        self.needs_sample.setEnabled(enabled)

    def _update_decision_enabled(self):
        if self.current is None:
            self._set_actions_enabled(False)
            return
        self.accept.setEnabled(bool(self.current["generated_samples"]))
        self.reject.setEnabled(True)
        self.needs_sample.setEnabled(True)

    def _play_reference(self):
        if self.current is not None:
            self._play_record(self.current["reference"], "reference")

    def _play_generated(self):
        if self.current is None:
            return
        row = self.generated.currentRow()
        if row < 0 or row >= len(self.current["generated_samples"]):
            return
        sample = self.current["generated_samples"][row]
        self._play_record(sample, sample["queue_id"])

    def _play_record(self, record, token):
        path = (self.session_path.parent / record["audio"]).resolve()
        try:
            path.relative_to(self.session_path.parent)
            payload = path.read_bytes()
        except (OSError, ValueError) as error:
            self.status.setText(f"Playback blocked: {error}")
            return
        if hashlib.sha256(payload).hexdigest() != record["audio_sha256"]:
            self.status.setText("Playback blocked: audio checksum changed")
            return
        self.player.stop()
        self._audio_buffer = QBuffer(self)
        self._audio_buffer.setData(QByteArray(payload))
        if not self._audio_buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            self.status.setText("Playback blocked: audio buffer could not be opened")
            return
        self._playing_token = token
        self.player.setSourceDevice(self._audio_buffer, QUrl("buffer:review.wav"))
        self.player.play()
        self.status.setText("Starting checksum-verified audio.")

    def _playback_state_changed(self, state):
        if (
            state == QMediaPlayer.PlaybackState.PlayingState
            and self._playing_token is not None
        ):
            self.status.setText("Playing checksum-verified audio.")

    def _media_status_changed(self, status):
        if (
            status == QMediaPlayer.MediaStatus.EndOfMedia
            and self._playing_token is not None
        ):
            self.completed_audio.add(self._playing_token)
            generated_tokens = {
                sample["queue_id"] for sample in self.current["generated_samples"]
            }
            generated_finished = len(generated_tokens & self.completed_audio)
            original = "yes" if "reference" in self.completed_audio else "no"
            self.status.setText(
                "Finished checksum-verified audio. "
                f"Listened: original {original}; generated "
                f"{generated_finished}/{len(generated_tokens)}."
            )
            self._update_decision_enabled()

    def _playback_error(self, _error, message):
        self.status.setText(f"Playback failed: {message or self.player.errorString()}")

    def _stop(self):
        if hasattr(self, "player"):
            self.player.stop()
        self._playing_token = None
        if self._audio_buffer is not None:
            self._audio_buffer.close()
            self._audio_buffer = None

    def _decide(self, decision):
        if self.current is None:
            return
        if not {
            "accept": self.accept,
            "reject": self.reject,
            "needs_sample": self.needs_sample,
        }[decision].isEnabled():
            self.status.setText("This decision is unavailable for the current card.")
            return
        try:
            record_source_reference_quality_decision(
                self.session_path, self.current["variant_id"], decision
            )
        except (SourceReferenceQualityError, OSError) as error:
            self.status.setText(f"Decision was not saved: {error}")
            return
        self._load_next()

    def closeEvent(self, event):
        self._stop()
        super().closeEvent(event)


def launch_source_reference_quality_review(session_path):
    _application = QApplication.instance() or QApplication(sys.argv)
    try:
        dialog = SourceReferenceQualityDialog(session_path)
    except Exception as error:
        QMessageBox.critical(None, "Unable to open source-reference review", str(error))
        return 1
    dialog.exec()
    return 0


__all__ = [
    "SourceReferenceQualityDialog",
    "launch_source_reference_quality_review",
]

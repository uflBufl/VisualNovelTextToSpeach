"""Qt review for one source-reference character cluster at a time."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QUrl
from PySide6.QtGui import QKeySequence, QPixmap
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
    QToolButton,
    QVBoxLayout,
)

from vntts.async_ui import LatestTaskRunner
from vntts.authoring.review_context_ui import ReviewDecisionContext
from vntts.authoring.source_reference_quality_records import (
    load_source_reference_quality_review,
    next_pending_quality_variant,
    quality_review_progress,
    record_source_reference_quality_decision,
)


class SourceReferenceQualityDialog(QDialog):
    """Present exact original and generated evidence without cross-character A/B."""

    def __init__(
        self,
        session_path,
        parent=None,
        *,
        decision_recorder=record_source_reference_quality_decision,
        thread_pool=None,
    ):
        super().__init__(parent)
        self.session_path = Path(session_path).expanduser().resolve()
        self.session = load_source_reference_quality_review(self.session_path)
        self.decision_recorder = decision_recorder
        self.decision_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.decision_runner.finished.connect(self._decision_finished)
        self.playback_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.playback_runner.finished.connect(self._playback_prepared)
        self._decision_active = False
        self._close_pending = False
        self.current = None
        self.completed_audio = set()
        self._audio_buffer = None
        self._playing_token = None

        self.setWindowTitle("Source-reference quality review")
        self.setMinimumSize(700, 500)
        self.progress = QLabel()
        self.progress.setAccessibleName("Source reference review progress")
        self.decision_context = ReviewDecisionContext()
        self.portrait_image = QLabel()
        self.portrait_image.setAccessibleName("Exact game portrait")
        self.portrait_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.portrait_image.setMinimumHeight(150)
        self.identity = QLabel()
        self.identity.setWordWrap(True)
        self.identity.setAccessibleName("Current source reference identity")
        self.reference_details = QLabel()
        self.reference_details.setAccessibleName("Original source reference details")
        self.play_reference = QPushButton("Play original reference")
        self.play_reference.setAccessibleName("Play original source reference")
        self.play_reference.setAccessibleDescription(
            "Play the checksum-verified original until its end"
        )
        self.play_reference.setShortcut(QKeySequence("Ctrl+O"))
        self.play_reference.clicked.connect(self._play_reference)

        self.generated = QListWidget()
        self.generated.setAccessibleName("Generated samples for this reference")
        self.generated.currentRowChanged.connect(self._generated_selection_changed)
        self.generated_details = QLabel()
        self.generated_details.setWordWrap(True)
        self.generated_details.setAccessibleName("Selected generated sample details")
        self.play_generated = QPushButton("Play selected generated sample")
        self.play_generated.setAccessibleName("Play selected generated sample")
        self.play_generated.setAccessibleDescription(
            "Play the selected checksum-verified generated sample until its end"
        )
        self.play_generated.setShortcut(QKeySequence("Ctrl+G"))
        self.play_generated.clicked.connect(self._play_generated)
        self.stop = QPushButton("Stop audio")
        self.stop.setAccessibleName("Stop source reference review audio")
        self.stop.setShortcut(QKeySequence("Ctrl+Space"))
        self.stop.clicked.connect(self._stop)
        playback = QHBoxLayout()
        playback.addWidget(self.play_reference)
        playback.addWidget(self.play_generated)
        playback.addWidget(self.stop)

        self.failures = QLabel()
        self.failures.setWordWrap(True)
        self.failures.setAccessibleName("Excluded generation diagnostics")
        self.technical_toggle = QToolButton()
        self.technical_toggle.setCheckable(True)
        self.technical_toggle.setChecked(False)
        self.technical_toggle.setAccessibleName("Show excluded technical diagnostics")
        self.technical_toggle.toggled.connect(self.failures.setVisible)
        self.failures.setVisible(False)
        self.evidence_progress = QLabel()
        self.evidence_progress.setWordWrap(True)
        self.evidence_progress.setAccessibleName("Required listening evidence")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Source reference review status")
        self.accept = QPushButton("Accept reference")
        self.reject = QPushButton("Reject reference")
        self.needs_sample = QPushButton("Need another sample")
        self.accept.setAccessibleName("Accept exact source reference")
        self.reject.setAccessibleName("Reject exact source reference")
        self.needs_sample.setAccessibleName("Request another source reference sample")
        self.accept.setShortcut(QKeySequence("Ctrl+Return"))
        self.reject.setShortcut(QKeySequence("Ctrl+Backspace"))
        self.needs_sample.setShortcut(QKeySequence("Ctrl+N"))
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
        layout.addWidget(self.decision_context)
        layout.addWidget(self.portrait_image)
        layout.addWidget(self.identity)
        layout.addWidget(self.reference_details)
        layout.addWidget(self.generated, 1)
        layout.addWidget(self.generated_details)
        layout.addLayout(playback)
        layout.addWidget(self.evidence_progress)
        layout.addWidget(self.technical_toggle)
        layout.addWidget(self.failures)
        layout.addWidget(self.status)
        layout.addLayout(decisions)
        layout.addWidget(buttons)
        self.setTabOrder(self.generated, self.play_reference)
        self.setTabOrder(self.play_reference, self.play_generated)
        self.setTabOrder(self.play_generated, self.stop)
        self.setTabOrder(self.stop, self.technical_toggle)
        self.setTabOrder(self.technical_toggle, self.accept)
        self.setTabOrder(self.accept, self.reject)
        self.setTabOrder(self.reject, self.needs_sample)

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
            self.decision_context.set_context(
                {
                    "purpose": "Judge whether source audio is safe for voice cloning",
                    "effect": "Review complete; no further decision is required",
                }
            )
            self.portrait_image.clear()
            self.portrait_image.setVisible(False)
            self.identity.setText("Review complete")
            self.reference_details.clear()
            self.generated_details.clear()
            self.failures.clear()
            self.technical_toggle.setVisible(False)
            self.evidence_progress.setText("All required decisions are saved.")
            self.status.setText(
                "All cluster decisions are saved. Only accepted references may be "
                "published into queue bindings."
            )
            self._set_actions_enabled(False)
            self.play_reference.setEnabled(False)
            self.play_generated.setEnabled(False)
            return

        if self.current.get("reference_kind") == "exact_bank_composite":
            media = "Exact-bank composite media: " + ", ".join(
                str(value) for value in self.current["media_ids"]
            )
        else:
            media = f"Original media: {self.current['media_id']}"
        self.identity.setText(
            f"Character: {self.current['character']} | {media} | "
            f"Affected story lines: {self.current['affected_queue_item_count']}"
        )
        synthesis = self.current.get("decision_context") or {}
        model = str(synthesis.get("model") or "Unknown (legacy review format)")
        seed = synthesis.get("seed", "Unknown")
        self.decision_context.set_context(
            {
                "purpose": "Accept, reject, or replace a voice-cloning reference",
                "game_speaker": self.current["character"],
                "synthesis_voice": self.current["character"],
                "reference": media,
                "backend": synthesis.get("backend") or "Unknown (legacy review format)",
                "model": Path(model).name if "/" in model else model,
                "generation_profile": synthesis.get("generation_profile")
                or "Unknown (legacy review format)",
                "controls": (
                    f"Original plus published generated evidence | Seed: {seed}"
                ),
                "effect": (
                    "allow this exact reference for later voice binding, reject it, "
                    "or request another source sample"
                ),
            },
            technical=(
                f"Exact model: {model}\n"
                f"Variant: {self.current['variant_id']}\n"
                f"Cluster: {self.current['cluster_id']}\n"
                f"Affected queue items: {self.current['affected_queue_item_count']}"
            ),
        )
        self.portrait_image.setVisible(True)
        self._load_portrait()
        reference = self.current["reference"]
        self.reference_details.setText(
            "Original reference: "
            f"{reference['duration_seconds']:.2f}s, {reference['sample_rate']} Hz"
        )
        for sample in self.current["generated_samples"]:
            self.generated.addItem(
                f"{sample['evaluation_kind']} | {sample['duration_seconds']:.2f}s"
            )
        self.generated.setVisible(bool(self.generated.count()))
        if self.generated.count():
            self.generated.setCurrentRow(0)
        else:
            self.generated_details.setText(
                "No published generated sample is available for this reference."
            )
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
        self.failures.setVisible(False)
        self.technical_toggle.setChecked(False)
        self.technical_toggle.setVisible(bool(failure_lines))
        self.technical_toggle.setText(f"Technical exclusions ({len(failure_lines)})")
        if self.generated.count():
            self.status.setText(
                "Listen through the original and generated evidence. Playback must "
                "reach the end before it authorizes a decision."
            )
        else:
            self.status.setText(
                "No generated sample is available. Finish the original before rejecting "
                "it or requesting another sample."
            )
        self.play_reference.setEnabled(True)
        self._update_play_enabled()
        self._update_decision_enabled()

    def _load_portrait(self):
        record = self.current.get("portrait_image")
        if record is None:
            self._set_portrait_message("Exact game portrait is not installed")
            return
        path = (self.session_path.parent / record["image"]).resolve()
        try:
            path.relative_to(self.session_path.parent)
            payload = path.read_bytes()
        except (OSError, ValueError) as error:
            self._set_portrait_message(f"Portrait unavailable: {error}")
            return
        if hashlib.sha256(payload).hexdigest() != record["image_sha256"]:
            self._set_portrait_message("Portrait blocked: checksum changed")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(payload, "PNG"):
            self._set_portrait_message("Portrait blocked: invalid PNG")
            return
        self.portrait_image.setMinimumHeight(150)
        self.portrait_image.setMaximumHeight(220)
        self.portrait_image.setText("")
        self.portrait_image.setPixmap(
            pixmap.scaled(
                204,
                204,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _set_portrait_message(self, message):
        self.portrait_image.setPixmap(QPixmap())
        self.portrait_image.setText(message)
        self.portrait_image.setMinimumHeight(36)
        self.portrait_image.setMaximumHeight(48)

    def _generated_selection_changed(self, row):
        self._update_play_enabled()
        if self.current is None or not 0 <= row < len(
            self.current["generated_samples"]
        ):
            return
        sample = self.current["generated_samples"][row]
        self.generated_details.setText(
            f"Sample {row + 1} of {len(self.current['generated_samples'])}\n"
            f"Kind: {sample['evaluation_kind']}\n"
            f"Duration: {sample['duration_seconds']:.2f}s\n"
            f"Text: {sample['text']}"
        )

    def _update_play_enabled(self):
        self.play_generated.setEnabled(
            self.current is not None
            and self.generated.currentRow() >= 0
            and bool(self.current["generated_samples"])
        )

    def _set_actions_enabled(self, enabled, reason=None):
        self.accept.setEnabled(enabled)
        self.reject.setEnabled(enabled)
        self.needs_sample.setEnabled(enabled)
        if reason is not None:
            self.evidence_progress.setText(reason)
        for button, action in (
            (self.accept, "accept this exact reference"),
            (self.reject, "reject this exact reference"),
            (self.needs_sample, "request another sample"),
        ):
            button.setAccessibleDescription(
                f"Ready to {action}"
                if button.isEnabled()
                else f"Unavailable: {self.evidence_progress.text()}"
            )

    def _update_decision_enabled(self):
        if self.current is None:
            self._set_actions_enabled(False, "Review complete.")
            return
        generated_tokens = {
            sample["queue_id"] for sample in self.current["generated_samples"]
        }
        reference_finished = "reference" in self.completed_audio
        generated_finished = len(generated_tokens & self.completed_audio)
        all_generated_finished = bool(generated_tokens) and generated_tokens.issubset(
            self.completed_audio
        )
        self.accept.setEnabled(reference_finished and all_generated_finished)
        self.reject.setEnabled(reference_finished)
        self.needs_sample.setEnabled(reference_finished)
        self.evidence_progress.setText(
            "Required evidence: original "
            f"{'1/1' if reference_finished else '0/1'} | generated "
            f"{generated_finished}/{len(generated_tokens)}. "
            "Reject and Need another require the original; Accept requires all "
            "published generated samples too."
        )
        for button, action in (
            (self.accept, "accept this exact reference"),
            (self.reject, "reject this exact reference"),
            (self.needs_sample, "request another sample"),
        ):
            button.setAccessibleDescription(
                f"Ready to {action}"
                if button.isEnabled()
                else f"Unavailable: {self.evidence_progress.text()}"
            )

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
        self._stop()
        self.status.setText("Preparing checksum-verified audio in background...")
        self.playback_runner.start(
            self._load_audio_payload,
            self.session_path.parent,
            dict(record),
            token,
            self.current["variant_id"],
        )

    @staticmethod
    def _load_audio_payload(root, record, token, variant_id):
        root = Path(root).resolve()
        path = root / record["audio"]
        if path.is_symlink():
            raise ValueError("audio path is a symlink")
        path = path.resolve()
        path.relative_to(root)
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != record["audio_sha256"]:
            raise ValueError("audio checksum changed")
        return variant_id, token, record["audio_sha256"], payload

    def _playback_prepared(self, result, error):
        if error is not None:
            self.status.setText(f"Playback blocked: {error}")
            return
        variant_id, token, digest, payload = result
        if self.current is None or self.current["variant_id"] != variant_id:
            self.status.setText("Playback cancelled: review card changed")
            return
        if token == "reference":
            selected = self.current["reference"]
        else:
            row = self.generated.currentRow()
            selected = (
                self.current["generated_samples"][row]
                if 0 <= row < len(self.current["generated_samples"])
                else None
            )
        if (
            selected is None
            or selected.get("audio_sha256") != digest
            or (token != "reference" and selected.get("queue_id") != token)
        ):
            self.status.setText("Playback cancelled: audio selection changed")
            return
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
        if hasattr(self, "playback_runner"):
            self.playback_runner.cancel()
        if hasattr(self, "player"):
            self.player.stop()
        self._playing_token = None
        if self._audio_buffer is not None:
            self._audio_buffer.close()
            self._audio_buffer = None

    def _decide(self, decision):
        if self.current is None:
            return
        if self._decision_active:
            self.status.setText("Wait for the current decision to finish saving.")
            return
        if not {
            "accept": self.accept,
            "reject": self.reject,
            "needs_sample": self.needs_sample,
        }[decision].isEnabled():
            self.status.setText("This decision is unavailable for the current card.")
            return
        self._decision_active = True
        self._set_actions_enabled(
            False,
            "Saving the exact variant-local decision; playback remains available.",
        )
        self.status.setText(
            "Saving the exact reference decision... Playback remains available."
        )
        self.decision_runner.start(
            self.decision_recorder,
            self.session_path,
            self.current["variant_id"],
            decision,
        )

    def _decision_finished(self, _result, error):
        self._decision_active = False
        if error is None:
            self._load_next()
        else:
            self._update_decision_enabled()
            self.status.setText(
                f"Decision was not saved: {error}. Choose again to retry."
            )
        if self._close_pending:
            self._close_pending = False
            self.close()

    def closeEvent(self, event):
        if self._decision_active:
            self._close_pending = True
            self.status.setText(
                "Saving the exact reference decision. Close is deferred until "
                "the authoritative write finishes."
            )
            event.ignore()
            return
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

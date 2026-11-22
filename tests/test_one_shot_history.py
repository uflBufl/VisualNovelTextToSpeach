"""History follows actual manual playback, independently of live capture."""

import unittest
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from tests.test_live import QueuedExecutor
from vntts.controller import AppController, create_dialog_read_scheduler
from vntts.generated_audio import (
    AudioRouteTrace,
    GeneratedAudioRoute,
    PreparedGeneratedAudio,
)
from vntts.live import LiveDialogReader
from vntts.ocr import OCRResult
from vntts.playback import PlaybackOutcome, PlaybackStatus, PreparedPlayback
from vntts.settings import AppSettings


class ManualExecutor(QueuedExecutor):
    def submit(self, function, *args, **kwargs):
        return super().submit(partial(function, *args, **kwargs))


class ManualPlaybackBackend:
    name = "manual-test"

    def __init__(self, *, generated=False, starts=True, interrupted=False):
        self.generated = generated
        self.starts = starts
        self.interrupted = interrupted
        self.on_started = lambda: None

    def prepare_route(self, character, text):
        if self.generated:
            return GeneratedAudioRoute(
                PreparedGeneratedAudio(
                    "story:1", "a" * 64, np.zeros(100, dtype=np.float32), 24000
                ),
                AudioRouteTrace(
                    None, "generated", "exact", None, None, "story:1", "verified"
                ),
            )
        return PreparedPlayback((character, text), 0, None, None, "live:manual-test")

    def play_route(self, _route, *, playback_guard=None):
        return self.play_prepared(None, playback_guard=playback_guard)

    def play_prepared(self, _prepared, *, playback_guard=None):
        from vntts.playback_observer import playback_started_context

        if playback_guard is not None and not playback_guard():
            return PlaybackOutcome(PlaybackStatus.INTERRUPTED, None)
        if self.starts:
            started = playback_started_context.get()
            if started is not None:
                started()
            self.on_started()
        return PlaybackOutcome(
            PlaybackStatus.INTERRUPTED
            if self.interrupted
            else PlaybackStatus.COMPLETED,
            1,
            first_audio_ms=0 if self.starts else None,
        )


class OneShotHistoryTests(unittest.TestCase):
    def controller(self, backend):
        capture = ManualExecutor()
        speech = ManualExecutor()
        playback = ManualExecutor()
        errors = []
        dialogs = []
        controller = AppController(
            AppSettings(speaker_announcement_mode="off"),
            tts_factory=Mock(),
            error_handler=errors.append,
            dialog_handler=lambda character, text: dialogs.append((character, text)),
        )
        controller.speech_backend = backend
        controller.live_reader = LiveDialogReader(
            capture_executor=capture,
            ocr_executor=ManualExecutor(),
            speech_executor=speech,
            playback_executor=playback,
            capture_frame=Mock(),
            recognize_frame=Mock(),
            prepare_chunk=controller._prepare_live_chunk,
            play_prepared=controller._play_live_chunk,
            report_error=errors.append,
            first_pcm_on_prepare=False,
        )
        controller.schedule_dialog_read = create_dialog_read_scheduler(
            capture,
            Mock(),
            Path("captures"),
            live_reader=controller.live_reader,
            speech_handler=controller._enqueue_dialog,
            shutdown_requested=controller.shutdown_requested,
            error_handler=errors.append,
        )
        return controller, capture, speech, playback, errors, dialogs

    def capture_read(self, controller, capture, text):
        self.assertTrue(controller.read_once())
        with patch(
            "vntts.dialog_capture.analyze_dialog_snapshot",
            return_value=(
                Image.new("RGB", (1, 1)),
                None,
                OCRResult("Marcus", text, 99, "test", 1),
            ),
        ):
            capture.run_next()

    def test_manual_live_and_generated_reads_enter_history_only_at_playback_onset(self):
        text = "The complete dialogue line is ready."
        for generated in (False, True):
            with self.subTest(generated=generated):
                backend = ManualPlaybackBackend(generated=generated)
                controller, capture, speech, playback, errors, dialogs = (
                    self.controller(backend)
                )
                self.capture_read(controller, capture, text)
                self.assertEqual(dialogs, [("Marcus", text)])
                self.assertEqual(controller.history.snapshot(), [])
                speech.run_next()
                self.assertEqual(controller.history.snapshot(), [])
                observed_at_start = []
                backend.on_started = lambda: observed_at_start.extend(
                    controller.history.snapshot()
                )
                playback.run_next()
                self.assertEqual(
                    [(row.character, row.text) for row in observed_at_start],
                    [("Marcus", text)],
                )
                self.assertEqual(controller.history.snapshot(), observed_at_start)
                self.assertEqual(errors, [])
                self.assertFalse(controller.is_live_running)

    def test_canceling_manual_read_before_playback_leaves_no_history(self):
        controller, capture, speech, playback, errors, _dialogs = self.controller(
            ManualPlaybackBackend()
        )
        self.capture_read(controller, capture, "Queued but never spoken.")
        speech.run_next()
        controller.live_reader.clear_queue(reason="cancel-manual-read")
        playback.run_next()
        self.assertEqual(controller.history.snapshot(), [])
        self.assertEqual(errors, [])

    def test_manual_read_interruption_records_only_audio_that_started(self):
        for starts in (False, True):
            with self.subTest(starts=starts):
                backend = ManualPlaybackBackend(starts=starts, interrupted=True)
                controller, capture, speech, playback, errors, _dialogs = (
                    self.controller(backend)
                )
                self.capture_read(controller, capture, "An interrupted dialogue.")
                speech.run_next()
                playback.run_next()
                self.assertEqual(
                    [row.text for row in controller.history.snapshot()],
                    ["An interrupted dialogue."] if starts else [],
                )
                self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()

import queue
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from unittest.mock import MagicMock, patch

import numpy as np

from tests.speech_worker_fixtures import FakeProcess
from vntts.speech_worker import IsolatedSpeechBackend, _result_document
from vntts.synthesis import (
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class WorkerPlaybackLifetimeTests(unittest.TestCase):
    @contextmanager
    def worker(self, *, complete_requests=(True, True)):
        with (
            patch.object(IsolatedSpeechBackend, "_prepare_runtime"),
            patch.object(IsolatedSpeechBackend, "_start_worker"),
        ):
            backend = IsolatedSpeechBackend(
                "pocket-tts",
                CharacterVoiceRegistry([CharacterVoice("Hedone", "marius")]),
                audio_output=MagicMock(),
            )
        process = backend.process = FakeProcess(None)
        process.pid = 42
        backend.sample_rate = 24_000
        frames = queue.Queue()
        received_result = Event()
        sent = []
        completions = iter(complete_requests)
        original_send = backend._send
        previous_frame = None

        def read(_stream):
            nonlocal previous_frame
            if previous_frame is not None and previous_frame[0]["type"] == "result":
                received_result.set()
            previous_frame = frames.get(timeout=3)
            return previous_frame

        def send(owner, document):
            original_send(owner, document)
            if document["type"] != "render":
                return
            sent.append((owner.pid, document["voice"], document["pocket_voice_source"]))
            request_id = document["request_id"]
            pcm = np.array([[0.25], [-0.25]], dtype=np.float32)
            result = SynthesisResult(
                pcm=pcm,
                sample_rate=24_000,
                completion=SynthesisCompletion.COMPLETE,
                limits=SynthesisLimits(None, None),
                timing=SynthesisTiming(0.0, 1.0),
                diagnostics=SynthesisDiagnostics(
                    "pocket-tts", "fresh-generation", "default", None, 1, len(pcm)
                ),
            )
            received_result.clear()
            frames.put(
                (
                    {
                        "type": "chunk",
                        "request_id": request_id,
                        "shape": list(pcm.shape),
                        "sample_rate": 24_000,
                        "index": 0,
                        "elapsed_ms": 0.0,
                    },
                    pcm.tobytes(),
                )
            )
            if next(completions):
                frames.put(
                    (
                        {
                            "type": "result",
                            "request_id": request_id,
                            "result": _result_document(result),
                        },
                        b"",
                    )
                )
                self.assertTrue(received_result.wait(3))

        with (
            patch("vntts.speech_worker._read_frame", side_effect=read),
            patch.object(backend, "_send", side_effect=send),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            reader = executor.submit(backend._read_messages, process)
            try:
                yield backend, process, sent
            finally:
                frames.put(None)
                reader.result(3)
                backend.shutdown()

    def test_stop_after_render_finishes_reuses_worker_for_different_voice(self):
        with self.worker() as (backend, process, sent):
            output = (
                backend.audio_output.OutputStream.return_value.__enter__.return_value
            )
            output.write.side_effect = lambda _pcm: backend.stop()

            stopped = backend.play_prepared(
                backend.prepare_playback("Narrator", "Interrupted audio.")
            )

            self.assertEqual(stopped.status.value, "interrupted")
            self.assertIs(backend.process, process)
            self.assertIsNone(process.poll())
            self.assertTrue(backend._messages.empty())
            output.write.side_effect = None
            output.write.return_value = False
            retried = backend.play_prepared(
                backend.prepare_playback("Hedone", "A different voice.")
            )

            self.assertTrue(retried.successful)
            self.assertEqual(
                [request[:2] for request in sent], [(42, "Narrator"), (42, "Hedone")]
            )
            self.assertEqual(sent[0][2]["source"], "alba")
            self.assertEqual(sent[1][2]["source"], "marius")
            self.assertIs(backend.process, process)

    def test_expired_playback_guard_preserves_finished_worker(self):
        with self.worker() as (backend, process, _sent):
            active = Event()
            active.set()
            output = backend.audio_output.OutputStream

            def enter():
                active.clear()
                return MagicMock()

            output.return_value.__enter__.side_effect = enter
            stopped = backend.play_prepared(
                backend.prepare_playback("Narrator", "Outdated audio."),
                playback_guard=active.is_set,
            )

            self.assertEqual(stopped.status.value, "interrupted")
            self.assertIs(backend.process, process)
            self.assertIsNone(process.poll())
            self.assertTrue(backend._messages.empty())

    def test_abandoned_render_discards_only_its_unread_frames(self):
        for document in (
            {"type": "eof"},
            {"type": "fatal", "error": "Worker failed"},
            {"type": "chunk", "request_id": "another-request"},
        ):
            with (
                self.subTest(document=document),
                self.worker() as (
                    backend,
                    process,
                    _sent,
                ),
            ):
                rendered = backend.render(SynthesisRequest("Narrator", "Finished."))
                next(rendered)
                retained_frame = (process, document, b"")
                backend._messages.put(retained_frame)
                backend.stop()
                rendered.close()

                self.assertEqual(backend._messages.get_nowait(), retained_frame)
                self.assertTrue(backend._messages.empty())

    def test_completed_request_does_not_preserve_replacement_process(self):
        with self.worker() as (backend, _process, _sent):
            backend.render(SynthesisRequest("Narrator", "Finished.")).collect()
            replacement = backend.process = FakeProcess(None)

            with backend._request_lock:
                backend.stop()

            self.assertIsNone(backend.process)
            self.assertEqual(replacement.poll(), -15)

    def test_previous_result_does_not_preserve_inflight_request(self):
        with self.worker(complete_requests=(True, False)) as (backend, process, _sent):
            backend.render(SynthesisRequest("Narrator", "Finished.")).collect()
            output = (
                backend.audio_output.OutputStream.return_value.__enter__.return_value
            )
            output.write.side_effect = lambda _pcm: backend.stop()

            stopped = backend.play_prepared(
                backend.prepare_playback("Hedone", "Still generating.")
            )

            self.assertEqual(stopped.status.value, "interrupted")
            self.assertIsNone(backend.process)
            self.assertEqual(process.poll(), -15)


if __name__ == "__main__":
    unittest.main()

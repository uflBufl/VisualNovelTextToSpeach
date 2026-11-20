"""Cancellation while an isolated model loader cannot return a health frame."""

import subprocess
import sys
import unittest
from io import BytesIO
from pathlib import Path
from threading import Event
from time import monotonic
from unittest.mock import patch

from vntts.speech_backend import TTSSynthesisError
from vntts.speech_worker import IsolatedSpeechBackend, _write_frame
from vntts.voices import CharacterVoiceRegistry


class ModelDownloadCancellationTests(unittest.TestCase):
    def test_blocked_download_is_terminated_and_same_backend_can_retry(self):
        for cancel_through in ("stop", "startup-event"):
            with self.subTest(cancel_through=cancel_through):
                cancellation, entered = Event(), Event()

                def progress(message):
                    if message == "Downloading MOSS model fixture/blocked...":
                        entered.set()

                backend = IsolatedSpeechBackend(
                    "moss-tts",
                    CharacterVoiceRegistry(),
                    defer_startup=True,
                    startup_cancellation=cancellation,
                    startup_progress=progress,
                )
                backend.runtime_root = Path(sys.prefix).resolve()
                backend.interpreter = Path(sys.executable)
                backend.runtime_site = Path(__file__).resolve().parent
                health = BytesIO()
                _write_frame(
                    health,
                    {
                        "type": "health",
                        "interpreter": str(backend.interpreter),
                        "prefix": str(backend.runtime_root),
                        "sample_rate": 24_000,
                    },
                )
                downloading = BytesIO()
                _write_frame(
                    downloading,
                    {
                        "type": "asset_progress",
                        "message": "Downloading MOSS model fixture/blocked...",
                    },
                )
                processes = []

                def launch(_command, **options):
                    # Block inside the child like an unresponsive asset request.
                    code = (
                        "import sys, time; "
                        "sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); "
                        "sys.stdout.buffer.flush(); "
                        "time.sleep(60)"
                    )
                    frame = (health if processes else downloading).getvalue().hex()
                    process = subprocess.Popen(
                        [sys.executable, "-u", "-c", code, frame], **options
                    )
                    processes.append(process)
                    return process

                backend.process_factory = launch
                try:
                    with (
                        patch.object(backend, "_prepare_runtime"),
                        patch(
                            "vntts.runtime_ownership.claim_runtime", return_value=None
                        ),
                    ):
                        startup = backend.load_in_background()
                        self.assertTrue(entered.wait(5), "download did not start")
                        self.assertFalse(startup.done())
                        started = monotonic()
                        if cancel_through == "stop":
                            self.assertTrue(backend.stop())
                        else:
                            cancellation.set()
                        with self.assertRaisesRegex(TTSSynthesisError, "cancelled"):
                            startup.result(timeout=5)
                        self.assertLess(monotonic() - started, 5)
                        self.assertIsNotNone(processes[0].poll())
                        self.assertIsNone(backend.process)

                        cancellation.clear()
                        backend.load_in_background().result(timeout=5)
                        self.assertIs(backend.process, processes[-1])
                        self.assertIsNone(backend.process.poll())
                        self.assertEqual(backend.health["sample_rate"], 24_000)
                finally:
                    backend.clear_runtime_cache()
                    backend.shutdown()
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)
                        for stream in (process.stdin, process.stdout, process.stderr):
                            stream.close()


if __name__ == "__main__":
    unittest.main()

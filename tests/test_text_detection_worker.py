import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
from unittest.mock import Mock, patch

from PIL import Image

from vntts import text_detection_worker as worker
from vntts.ocr import DialogRegion, OCRPreviewCancelled


def successful_worker(connection, directory, *_paths):
    connection.send_bytes(b"ready")
    while connection.recv_bytes(16) == b"run":
        root = Path(directory)
        request = json.loads((root / "request.json").read_text())
        width, height = request["size"]
        if len((root / "image.raw").read_bytes()) != width * height * 3:
            raise ValueError("Invalid RGB payload")
        (root / "result.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "lines": [
                        {
                            "box": {
                                "left": 0.1,
                                "top": 0.2,
                                "width": 0.7,
                                "height": 0.1,
                            },
                            "text": "Hello world",
                            "confidence": 99,
                        }
                    ],
                }
            )
        )
        connection.send_bytes(b"done")


def hanging_worker(connection, directory, *_paths):
    connection.send_bytes(b"ready")
    connection.recv_bytes(16)
    sleep(60)


def startup_hanging_worker(connection, directory, *_paths):
    sleep(60)


def crashing_worker(connection, directory, *_paths):
    connection.send_bytes(b"ready")
    connection.recv_bytes(16)
    os._exit(17)


def partial_worker(connection, directory, *_paths):
    connection.send_bytes(b"ready")
    connection.recv_bytes(16)
    (Path(directory) / "result.json").write_text('{"ok":')
    os._exit(18)


class TextDetectionWorkerTest(unittest.TestCase):
    def setUp(self):
        self.owner = worker.TextDetectionWorker(
            "detector.onnx",
            "recognizer.onnx",
            timeout_seconds=1,
            startup_timeout_seconds=3,
        )
        self.addCleanup(self.owner.close)
        self.image = Image.new("RGBA", (200, 100), "white")

    def test_reuses_process_and_returns_immutable_normalized_lines(self):
        with patch.object(worker, "_worker_main", successful_worker):
            first = self.owner.detect(self.image)
            pid = self.owner.pid
            self.assertEqual(self.owner.detect(self.image), first)
        self.assertEqual(first[0].text, "Hello world")
        self.assertEqual(first[0].box.left, 0.1)
        self.assertEqual(self.owner.pid, pid)
        self.assertTrue(self.owner._process.is_alive())

    def test_serializes_allowed_region_in_request(self):
        region = DialogRegion(0.1, 0.2, 0.7, 0.5)
        with patch.object(worker, "_worker_main", successful_worker):
            self.owner.detect(self.image, allowed_region=region)
            request = json.loads(
                (Path(self.owner._directory.name) / "request.json").read_text()
            )
        self.assertEqual(request["allowed_region"], region.to_json())

    def test_child_validates_and_passes_allowed_region(self):
        region = DialogRegion(0.1, 0.2, 0.7, 0.5)
        for value in (region.to_json(), None, {"left": -1}):
            with self.subTest(value=value), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "request.json").write_text(
                    json.dumps({"size": [1, 1], "allowed_region": value})
                )
                (root / "image.raw").write_bytes(b"\0\0\0")
                connection = Mock()
                connection.recv_bytes.side_effect = [b"run", b"stop"]
                with patch("vntts.text_detection.ScreenTextDetector") as detector:
                    detector.return_value.detect.return_value = ()
                    worker._worker_main(connection, directory, "det", "rec")
                result = json.loads((root / "result.json").read_text())
                if value == {"left": -1}:
                    self.assertFalse(result["ok"])
                    detector.return_value.detect.assert_not_called()
                else:
                    self.assertTrue(result["ok"])
                    self.assertEqual(
                        detector.return_value.detect.call_args.kwargs["allowed_region"],
                        region if value is not None else None,
                    )

    def test_inference_timeout_reaps_process_and_next_request_recovers(self):
        with patch.object(worker, "_worker_main", hanging_worker):
            with self.assertRaisesRegex(TimeoutError, "detection timed out"):
                self.owner.detect(self.image)
        self.assertIsNone(self.owner.pid)
        self.assertIsNone(self.owner._directory)
        with patch.object(worker, "_worker_main", successful_worker):
            self.assertEqual(self.owner.detect(self.image)[0].text, "Hello world")

    def test_startup_timeout_reaps_process(self):
        self.owner.startup_timeout_seconds = 0.1
        with patch.object(worker, "_worker_main", startup_hanging_worker):
            with self.assertRaisesRegex(TimeoutError, "startup timed out"):
                self.owner.detect(self.image)
        self.assertIsNone(self.owner.pid)
        self.assertIsNone(self.owner._directory)

    def test_crash_and_partial_result_never_reuse_stale_output(self):
        for target in (crashing_worker, partial_worker):
            with (
                self.subTest(target=target.__name__),
                patch.object(worker, "_worker_main", target),
            ):
                with self.assertRaises((RuntimeError, EOFError)):
                    self.owner.detect(self.image)
                self.assertIsNone(self.owner.pid)

    def test_cancel_interrupts_active_and_queued_requests(self):
        errors = []

        def detect():
            try:
                self.owner.detect(self.image)
            except Exception as error:
                errors.append(error)

        with patch.object(worker, "_worker_main", hanging_worker):
            threads = [Thread(target=detect) for _ in range(2)]
            for thread in threads:
                thread.start()
            deadline = monotonic() + 3
            while self.owner.pid is None and monotonic() < deadline:
                sleep(0.01)
            self.owner.cancel_pending()
            for thread in threads:
                thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(error, OCRPreviewCancelled) for error in errors))
        self.assertIsNone(self.owner.pid)

    def test_close_interrupts_hung_inference_and_removes_temporary_files(self):
        errors = []

        def detect():
            try:
                self.owner.detect(self.image)
            except Exception as error:
                errors.append(error)

        with patch.object(worker, "_worker_main", hanging_worker):
            thread = Thread(target=detect)
            thread.start()
            deadline = monotonic() + 3
            while self.owner.pid is None and monotonic() < deadline:
                sleep(0.01)
            directory = self.owner._directory.name
            started = monotonic()
            self.owner.close()
            thread.join(3)
        self.assertLess(monotonic() - started, 3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(Path(directory).exists())
        self.assertIsInstance(errors[0], OCRPreviewCancelled)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.owner.detect(self.image)

    def test_precancelled_request_and_invalid_dimensions_do_not_spawn(self):
        cancelled = Event()
        cancelled.set()
        self.owner.cancel_event = cancelled
        with self.assertRaises(OCRPreviewCancelled):
            self.owner.detect(self.image)
        self.assertIsNone(self.owner.pid)
        with self.assertRaises(ValueError):
            self.owner.detect(Image.new("RGB", (0, 0)))

    def test_invalid_timeouts_are_rejected(self):
        for timeout in (0, -1, True, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                worker.TextDetectionWorker("det", "rec", timeout_seconds=timeout)

    def test_per_call_cancel_event_does_not_poison_a_new_reading_run(self):
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(OCRPreviewCancelled):
            self.owner.detect(self.image, cancel_event=cancelled)
        self.assertIsNone(self.owner.pid)
        with patch.object(worker, "_worker_main", successful_worker):
            lines = self.owner.detect(self.image, cancel_event=Event())
        self.assertEqual(lines[0].text, "Hello world")


if __name__ == "__main__":
    unittest.main()

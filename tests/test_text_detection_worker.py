import json
import os
import unittest
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
from unittest.mock import Mock, call, patch

from PIL import Image

from vntts import text_detection_worker as worker
from vntts.ocr import DialogRegion, OCRPreviewCancelled
from vntts.text_detection import ScreenTextDetection


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
                    "dialogue_text_present": False,
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


def result_worker(connection, directory, *_paths, result):
    connection.send_bytes(b"ready")
    connection.recv_bytes(16)
    (Path(directory) / "result.json").write_text(json.dumps(result))
    connection.send_bytes(b"done")
    connection.recv_bytes(16)


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
        self.assertEqual(first.lines[0].text, "Hello world")
        self.assertEqual(first.lines[0].box.left, 0.1)
        self.assertEqual(self.owner.pid, pid)
        self.assertTrue(self.owner._process.is_alive())

    def test_identical_pixels_reuse_result_without_another_worker_request(self):
        with patch.object(worker, "_worker_main", successful_worker):
            first = self.owner.detect(self.image)
            with patch.object(
                self.owner._connection,
                "send_bytes",
                wraps=self.owner._connection.send_bytes,
            ) as send:
                self.assertIs(self.owner.detect(self.image.copy()), first)
                self.assertIs(self.owner.detect(self.image.convert("RGB")), first)
                send.assert_not_called()

    def test_pixel_dimension_and_region_changes_require_new_detection(self):
        with patch.object(worker, "_worker_main", successful_worker):
            self.owner.detect(self.image)
            changed = self.image.copy()
            changed.putpixel((0, 0), (254, 255, 255, 255))
            resized = Image.frombytes(
                "RGB", (100, 200), changed.convert("RGB").tobytes()
            )
            region = DialogRegion(0.1, 0.2, 0.7, 0.5)
            with patch.object(
                self.owner._connection,
                "send_bytes",
                wraps=self.owner._connection.send_bytes,
            ) as send:
                self.owner.detect(changed)
                self.owner.detect(resized)
                self.owner.detect(resized, allowed_region=region)
                self.owner.detect(resized, allowed_region=region, covered_region=region)
                self.owner.detect(
                    resized,
                    allowed_region=region,
                    covered_region=region,
                    dialogue_region=region,
                )
                self.assertEqual(send.call_args_list, [call(b"run")] * 5)

    def test_cached_result_cannot_bypass_cancellation_or_close(self):
        with patch.object(worker, "_worker_main", successful_worker):
            self.owner.detect(self.image)
            cancelled = Event()
            cancelled.set()
            with self.assertRaises(OCRPreviewCancelled):
                self.owner.detect(self.image, cancel_event=cancelled)
            self.owner.close()
            self.assertIsNone(self.owner._last_detection)
            with self.assertRaisesRegex(RuntimeError, "closed"):
                self.owner.detect(self.image)

    def test_cancel_pending_during_cached_query_discards_worker_and_result(self):
        digest = worker.sha256
        with patch.object(worker, "_worker_main", successful_worker):
            self.owner.detect(self.image)

            def cancel_after_hash(pixels):
                value = digest(pixels)
                self.owner.cancel_pending()
                return value

            with patch.object(worker, "sha256", side_effect=cancel_after_hash):
                with self.assertRaises(OCRPreviewCancelled):
                    self.owner.detect(self.image)
            self.assertIsNone(self.owner._last_detection)
            self.assertIsNone(self.owner.pid)
            self.assertEqual(self.owner.detect(self.image).lines[0].text, "Hello world")

    def test_timeout_during_cached_query_does_not_return_previous_result(self):
        digest = worker.sha256
        with patch.object(worker, "_worker_main", successful_worker):
            self.owner.detect(self.image)
            elapsed = [0.0]

            def slow_hash(pixels):
                value = digest(pixels)
                elapsed[0] += self.owner.timeout_seconds + 1
                return value

            with (
                patch.object(worker, "monotonic", side_effect=lambda: elapsed[0]),
                patch.object(worker, "sha256", side_effect=slow_hash),
                self.assertRaises(TimeoutError),
            ):
                self.owner.detect(self.image)
            self.assertIsNone(self.owner._last_detection)
            self.assertIsNone(self.owner.pid)

    def test_dead_worker_restarts_even_when_pixels_are_identical(self):
        with patch.object(worker, "_worker_main", successful_worker):
            expected = self.owner.detect(self.image)
            old_pid = self.owner.pid
            self.owner._process.terminate()
            self.owner._process.join(2)
            self.assertEqual(self.owner.detect(self.image), expected)
            self.assertNotEqual(self.owner.pid, old_pid)

    def test_failed_request_discards_cached_detection(self):
        with patch.object(worker, "_worker_main", successful_worker):
            self.owner.detect(self.image)
            old_pid = self.owner.pid
            with patch.object(
                worker,
                "_read_detection_result",
                side_effect=RuntimeError("Invalid response"),
            ):
                with self.assertRaisesRegex(RuntimeError, "Invalid response"):
                    self.owner.detect(Image.new("RGB", self.image.size, "black"))
            self.assertIsNone(self.owner._last_detection)
            self.assertIsNone(self.owner.pid)
            self.owner.detect(self.image)
            self.assertNotEqual(self.owner.pid, old_pid)

    def test_serializes_allowed_and_covered_regions_separately(self):
        region = DialogRegion(0.1, 0.2, 0.7, 0.5)
        covered_region = DialogRegion(0.2, 0.3, 0.5, 0.2)
        dialogue_region = DialogRegion(0.2, 0.4, 0.5, 0.1)
        with patch.object(worker, "_worker_main", successful_worker):
            self.owner.detect(
                self.image,
                allowed_region=region,
                covered_region=covered_region,
                dialogue_region=dialogue_region,
            )
            request = json.loads(
                (Path(self.owner._directory.name) / "request.json").read_text()
            )
        self.assertEqual(request["allowed_region"], region.to_json())
        self.assertEqual(request["covered_region"], covered_region.to_json())
        self.assertEqual(request["dialogue_region"], dialogue_region.to_json())

    def test_serializes_absent_regions_as_null(self):
        with patch.object(worker, "_worker_main", successful_worker):
            self.owner.detect(self.image)
            request = json.loads(
                (Path(self.owner._directory.name) / "request.json").read_text()
            )
        self.assertIsNone(request["allowed_region"])
        self.assertIsNone(request["covered_region"])
        self.assertIsNone(request["dialogue_region"])

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
                    detector.return_value.detect.return_value = ScreenTextDetection(())
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
                    self.assertIsNone(
                        detector.return_value.detect.call_args.kwargs["covered_region"]
                    )

    def test_child_validates_and_passes_covered_region(self):
        allowed_region = DialogRegion(0.1, 0.2, 0.7, 0.5)
        covered_region = DialogRegion(0.2, 0.3, 0.5, 0.2)
        for value in (covered_region.to_json(), None, {"left": -1}):
            with self.subTest(value=value), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "request.json").write_text(
                    json.dumps(
                        {
                            "size": [1, 1],
                            "allowed_region": allowed_region.to_json(),
                            "covered_region": value,
                        }
                    )
                )
                (root / "image.raw").write_bytes(bytes(3))
                connection = Mock()
                connection.recv_bytes.side_effect = [b"run", b"stop"]
                with patch("vntts.text_detection.ScreenTextDetector") as detector:
                    detector.return_value.detect.return_value = ScreenTextDetection(())
                    worker._worker_main(connection, directory, "det", "rec")
                result = json.loads((root / "result.json").read_text())
                if value == {"left": -1}:
                    self.assertFalse(result["ok"])
                    detector.return_value.detect.assert_not_called()
                else:
                    self.assertTrue(result["ok"])
                    self.assertEqual(
                        detector.return_value.detect.call_args.kwargs,
                        {
                            "allowed_region": allowed_region,
                            "covered_region": covered_region
                            if value is not None
                            else None,
                            "dialogue_region": None,
                        },
                    )

    def test_child_validates_dialogue_region_and_preserves_detected_presence(self):
        region = DialogRegion(0.1, 0.7, 0.8, 0.3)
        for value in (region.to_json(), None, {"left": -1}, True):
            with self.subTest(value=value), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "request.json").write_text(
                    json.dumps(
                        {
                            "size": [1, 1],
                            "allowed_region": None,
                            "dialogue_region": value,
                        }
                    )
                )
                (root / "image.raw").write_bytes(bytes(3))
                connection = Mock()
                connection.recv_bytes.side_effect = [b"run", b"stop"]
                with patch("vntts.text_detection.ScreenTextDetector") as detector:
                    detector.return_value.detect.return_value = ScreenTextDetection(
                        (), True
                    )
                    worker._worker_main(connection, directory, "det", "rec")
                result = json.loads((root / "result.json").read_text())
                if value in ({"left": -1}, True):
                    self.assertFalse(result["ok"])
                    detector.return_value.detect.assert_not_called()
                else:
                    self.assertTrue(result["ok"])
                    self.assertIs(result["dialogue_text_present"], True)
                    self.assertEqual(result["lines"], [])
                    self.assertEqual(
                        detector.return_value.detect.call_args.kwargs[
                            "dialogue_region"
                        ],
                        region if value is not None else None,
                    )

    def test_worker_response_requires_strict_boolean_presence(self):
        responses = [
            {"ok": True, "lines": [], "dialogue_text_present": presence}
            for presence in (True, False, None, 0, 1, "false", [], {})
        ]
        responses.append({"ok": True, "lines": []})
        for result in responses:
            with self.subTest(result=result):
                self.owner.close()
                self.owner = worker.TextDetectionWorker("det", "rec")
                self.addCleanup(self.owner.close)
                target = partial(result_worker, result=result)
                with patch.object(worker, "_worker_main", target):
                    if type(result.get("dialogue_text_present")) is bool:
                        self.assertEqual(
                            self.owner.detect(self.image),
                            ScreenTextDetection((), result["dialogue_text_present"]),
                        )
                    else:
                        with self.assertRaisesRegex(
                            RuntimeError, "Invalid dialogue text"
                        ):
                            self.owner.detect(self.image)
                        self.assertIsNone(self.owner.pid)
                        self.assertIsNone(self.owner._directory)

    def test_inference_timeout_reaps_process_and_next_request_recovers(self):
        with patch.object(worker, "_worker_main", hanging_worker):
            with self.assertRaisesRegex(TimeoutError, "detection timed out"):
                self.owner.detect(self.image)
        self.assertIsNone(self.owner.pid)
        self.assertIsNone(self.owner._directory)
        with patch.object(worker, "_worker_main", successful_worker):
            self.assertEqual(self.owner.detect(self.image).lines[0].text, "Hello world")

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
        self.assertEqual(lines.lines[0].text, "Hello world")


if __name__ == "__main__":
    unittest.main()

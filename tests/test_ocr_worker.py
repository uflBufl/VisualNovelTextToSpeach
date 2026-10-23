import json
import os
import unittest
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from unittest.mock import patch

from PIL import Image

from vntts import ocr_worker
from vntts.ocr import DialogRegion, OCRRegions


def successful_worker(connection, directory, *configuration):
    root = Path(directory)
    connection.send_bytes(b"ready")
    while connection.recv_bytes(16) == b"run":
        request = json.loads((root / "request.json").read_text())
        text = "Speaker" if request["psm"] == 7 else "Hello world"
        data = dict(
            level=[5],
            page_num=[1],
            block_num=[1],
            par_num=[1],
            line_num=[1],
            word_num=[1],
            left=[0],
            top=[0],
            width=[100],
            height=[20],
            conf=[95],
            text=[text],
        )
        (root / "result.json").write_text(
            json.dumps({"ok": True, "text": text, "data": data})
        )
        connection.send_bytes(b"done")


def hanging_worker(connection, directory, *configuration):
    connection.send_bytes(b"ready")
    connection.recv_bytes(16)
    sleep(60)


def crashing_worker(connection, directory, *configuration):
    connection.send_bytes(b"ready")
    connection.recv_bytes(16)
    os._exit(17)


def partial_result_worker(connection, directory, *configuration):
    connection.send_bytes(b"ready")
    connection.recv_bytes(16)
    (Path(directory) / "result.json").write_text('{"ok":')
    os._exit(18)


class PersistentOCRTest(unittest.TestCase):
    def setUp(self):
        self.owner = ocr_worker.PersistentOCR()
        self.addCleanup(self.owner.close)
        self.image = Image.new("L", (200, 100), "white")
        self.regions = OCRRegions(
            DialogRegion(0, 0, 1, 0.3), DialogRegion(0, 0.4, 1, 0.6)
        )

    def recognize(self, **options):
        return self.owner.recognize(self.image, ocr_regions=self.regions, **options)

    def test_reuses_pid_and_preserves_parent_registry_pipeline(self):
        with patch.object(ocr_worker, "_worker_main", successful_worker):
            first = self.recognize()
            pid = self.owner.pid
            second = self.recognize(language="eng+rus")
        self.assertEqual(first.text, "Hello world")
        self.assertEqual(second.text, first.text)
        self.assertEqual(self.owner.pid, pid)
        self.assertTrue(self.owner._process.is_alive())

    def test_timeout_kills_worker_and_next_request_recovers(self):
        with patch.object(ocr_worker, "_worker_main", hanging_worker):
            with self.assertRaises(TimeoutError):
                self.recognize(timeout=1)
        self.assertIsNone(self.owner.pid)
        with patch.object(ocr_worker, "_worker_main", successful_worker):
            self.assertEqual(self.recognize().text, "Hello world")

    def test_crash_and_partial_result_do_not_hang_or_return_stale_text(self):
        for worker in (crashing_worker, partial_result_worker):
            with self.subTest(worker=worker.__name__):
                with patch.object(ocr_worker, "_worker_main", worker):
                    started = monotonic()
                    with self.assertRaises((EOFError, RuntimeError)):
                        self.recognize(timeout=3)
                    self.assertLess(monotonic() - started, 4)
                    self.assertIsNone(self.owner.pid)

    def test_cancellation_interrupts_active_and_queued_requests(self):
        errors = []

        def read():
            try:
                self.recognize()
            except Exception as error:
                errors.append(error)

        with patch.object(ocr_worker, "_worker_main", hanging_worker):
            threads = [Thread(target=read) for _ in range(2)]
            for thread in threads:
                thread.start()
            try:
                deadline = monotonic() + 3
                while self.owner.pid is None and monotonic() < deadline:
                    sleep(0.01)
                self.owner.cancel_pending()
            finally:
                for thread in threads:
                    thread.join(4)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(errors), 2)
            self.assertTrue(all("cancelled" in str(error) for error in errors))
            self.assertIsNone(self.owner.pid)

    def test_precancelled_request_never_starts_worker(self):
        cancelled = Event()
        cancelled.set()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.recognize(cancelled=cancelled.is_set)
        self.assertIsNone(self.owner.pid)

    def test_close_reaps_idle_worker_and_rejects_later_use(self):
        with patch.object(ocr_worker, "_worker_main", successful_worker):
            self.recognize()
        process = self.owner._process
        self.owner.close()
        self.assertTrue(process._closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.recognize()

    def test_executable_or_tessdata_change_replaces_process(self):
        with patch.object(ocr_worker, "_worker_main", successful_worker):
            self.recognize()
            pid = self.owner.pid
            with patch.dict(os.environ, {"TESSDATA_PREFIX": "/different-models"}):
                self.recognize()
                self.assertNotEqual(pid, self.owner.pid)

    def test_invalid_budget_fails_before_startup(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.recognize(timeout=timeout)
        self.assertIsNone(self.owner.pid)

    def test_shutdown_seals_admission_until_explicit_startup(self):
        with (
            patch.object(ocr_worker, "_service", self.owner),
            patch.object(ocr_worker, "_accepting", True),
        ):
            ocr_worker.close_ocr_worker()
            with self.assertRaisesRegex(RuntimeError, "shutting down"):
                ocr_worker.recognize_dialog_image_result(self.image)
            self.assertIs(ocr_worker._service, self.owner)
            ocr_worker.open_ocr_worker()
            self.assertIsNone(ocr_worker._service)
            self.assertTrue(ocr_worker._accepting)

    def test_runtime_start_serializes_ocr_admission_with_shutdown(self):
        from types import SimpleNamespace

        from vntts.controller_components import RuntimeLifecycleComponent

        controller = SimpleNamespace(shutdown_requested=Event(), is_ready=True)
        lifecycle = RuntimeLifecycleComponent(controller)

        def opened():
            acquired = lifecycle._shutdown_lock.acquire(blocking=False)
            if acquired:
                lifecycle._shutdown_lock.release()
            self.assertFalse(acquired, "OCR admission must share the shutdown lock")

        with patch(
            "vntts.controller_components.open_ocr_worker", side_effect=opened
        ) as open_owner:
            self.assertTrue(lifecycle.start())
            open_owner.assert_called_once()
            controller.shutdown_requested.set()
            self.assertFalse(lifecycle.start())
            open_owner.assert_called_once()


if __name__ == "__main__":
    unittest.main()

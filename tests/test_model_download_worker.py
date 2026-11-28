import unittest
from multiprocessing import active_children
from pathlib import Path
from threading import Event
from time import sleep
from unittest.mock import patch

from vntts.huggingface_assets import AssetProgress
from vntts.model_assets import ModelDownloadCancelled
from vntts.model_download_worker import download_model_snapshot


def _transfer_fixture(sender, source, _revision, _cache_dir):
    with sender:
        progress = AssetProgress("downloading", "Model", completed_bytes=1)
        sender.send(progress)
        if source == "retry":
            sender.send(None)
            return
        sender.send(progress)
        sleep(60)


class ModelDownloadWorkerTest(unittest.TestCase):
    def test_stalled_worker_is_reaped_before_retry(self):
        elapsed = 0.0
        updates = []

        def report(progress):
            nonlocal elapsed
            updates.append(progress)
            elapsed += 31

        with patch("vntts.model_download_worker._download", _transfer_fixture):
            with patch("vntts.model_download_worker.monotonic", lambda: elapsed):
                with self.assertRaisesRegex(TimeoutError, "stopped making progress"):
                    self._download("stalled", Event(), report)
            self.assertEqual(len(updates), 2)
            self._assert_worker_stopped()
            self._download("retry", Event(), updates.append)
            self._assert_worker_stopped()

    def test_stop_reaps_worker_without_waiting_for_a_new_callback(self):
        cancelled = Event()
        with patch("vntts.model_download_worker._download", _transfer_fixture):
            with self.assertRaises(ModelDownloadCancelled):
                self._download("stalled", cancelled, lambda _: cancelled.set())
        self._assert_worker_stopped()

    @staticmethod
    def _download(source, cancelled, report):
        download_model_snapshot(
            source,
            revision="a" * 40,
            cache_dir=Path("."),
            cancelled=cancelled,
            progress=report,
        )

    def _assert_worker_stopped(self):
        self.assertFalse(
            any(child.name == "model-download" for child in active_children())
        )

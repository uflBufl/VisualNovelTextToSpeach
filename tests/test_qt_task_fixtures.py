import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from PySide6.QtCore import QRunnable, QThreadPool

from tests.qt_task_fixtures import release_and_wait_for_pool


class QtTaskFixturesTest(unittest.TestCase):
    def test_failure_drains_workers_before_removing_temporary_directory(self) -> None:
        pool = QThreadPool()
        started = Event()
        release = Event()
        directory_exists = []
        failure = AssertionError("injected failure after worker acquisition")

        with self.assertRaises(AssertionError) as raised:
            with TemporaryDirectory() as directory:
                root = Path(directory)

                class Worker(QRunnable):
                    def run(self) -> None:
                        started.set()
                        release.wait(3)
                        directory_exists.append(root.exists())

                with release_and_wait_for_pool(
                    pool, release, finish=lambda: pool.start(Worker())
                ):
                    pool.start(Worker())
                    self.assertTrue(started.wait(1))
                    raise failure

        self.assertIs(raised.exception, failure)
        self.assertTrue(release.is_set())
        self.assertEqual(directory_exists, [True, True])
        self.assertFalse(root.exists())
        self.assertEqual(pool.activeThreadCount(), 0)
        self.assertTrue(pool.waitForDone(0))


if __name__ == "__main__":
    unittest.main()

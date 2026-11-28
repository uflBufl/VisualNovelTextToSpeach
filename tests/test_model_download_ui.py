import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.huggingface_assets import AssetProgress, SnapshotSize  # noqa: E402
from vntts.model_activity import begin_model_activity  # noqa: E402
from vntts.model_download_ui import (  # noqa: E402
    QWEN_BACKEND,
    ModelDownloadPanel,
    QwenModelSetup,
)


class ModelDownloadUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_one_dialog_for_first_use_settings_and_progress(self):
        setup = QwenModelSetup()
        self.addCleanup(setup.deleteLater)
        self.addCleanup(setup.shutdown)
        pool = ManualThreadPool()
        setup.runner.thread_pool = pool
        with patch(
            "vntts.model_download_ui.inspect_snapshot",
            return_value=SnapshotSize(
                setup.revision, 3 * 1024**3, 1024**3, 2 * 1024**3, 9, 3
            ),
        ) as inspect:
            self.assertFalse(setup.ready_for_use(QWEN_BACKEND))
            dialog = setup._dialog
            setup.open_dialog()
            self.assertIs(setup._dialog, dialog)
            self.assertEqual(len(pool.tasks), 1)
            pool.tasks.pop().run()
            self.app.processEvents()
            inspect.assert_called_once_with(
                setup.source,
                revision=setup.revision,
                cache_dir=setup.cache_dir,
                token=False,
            )

        panel = dialog.findChild(ModelDownloadPanel)
        self.assertEqual(
            panel.detail.text(), "3.0 GiB total · 1.0 GiB on disk · 2.0 GiB to download"
        )
        self.assertEqual(panel.button.text(), "Download model")
        activity = begin_model_activity(
            QWEN_BACKEND,
            AssetProgress("downloading", "Model", completed_files=6, total_files=9),
        )
        self.addCleanup(activity.close)
        setup._refresh_activity()
        self.assertEqual(panel.progress.maximum(), 0)
        self.assertIn("6 of 9 files", panel.detail.text())
        activity.report(
            AssetProgress(
                "downloading", "Model", completed_bytes=1024, total_bytes=4096
            )
        )
        setup._refresh_activity()
        self.assertEqual(panel.progress.value(), 25)
        self.assertIn("1 KiB of 4 KiB", panel.detail.text())
        activity.report(AssetProgress("loading", "Model"))
        setup._refresh_activity()
        self.assertEqual(panel.progress.maximum(), 0)
        self.assertIn("memory", panel.phase_label.text())

    def test_stalled_download_refreshes_saved_files_and_allows_retry(self):
        setup = QwenModelSetup()
        self.addCleanup(setup.deleteLater)
        self.addCleanup(setup.shutdown)
        pool = ManualThreadPool()
        setup.runner.thread_pool = pool

        def finish_task():
            pool.tasks.pop().run()
            self.app.processEvents()

        with (
            patch(
                "vntts.model_download_ui.inspect_snapshot",
                side_effect=[
                    SnapshotSize(setup.revision, 4096, 0, 4096, 2, 0),
                    SnapshotSize(setup.revision, 4096, 1024, 3072, 2, 1),
                    SnapshotSize(setup.revision, 4096, 4096, 0, 2, 2),
                ],
            ),
            patch(
                "vntts.model_download_ui.download_model_snapshot",
                side_effect=[TimeoutError(), None],
            ) as download,
        ):
            setup.open_dialog()
            finish_task()
            setup.action()
            finish_task()
            finish_task()
            state = setup.state()
            self.assertIn("Download stalled", state.detail)
            self.assertIn("1 KiB on disk", state.detail)
            self.assertEqual(state.action, "Download model")

            setup.action()
            finish_task()
            self.assertTrue(setup.is_ready_for_use(QWEN_BACKEND))
            self.assertEqual(setup.state().detail, "4 KiB on disk.")
            for call in download.call_args_list:
                self.assertEqual(call.kwargs["cache_dir"], setup.cache_dir)

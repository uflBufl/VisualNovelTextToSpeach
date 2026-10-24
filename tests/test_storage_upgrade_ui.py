"""Saved preparation upgrades are available without a manual command."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from tests.story_fixtures import write_story_index  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    inspect_story_index,
    job_schema_version,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.storage_upgrade_ui import (  # noqa: E402
    StorageUpgradeDialog,
    offer_storage_upgrade,
)
from vntts.versioned_json import write_versioned_json  # noqa: E402


class StorageUpgradeDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_update_preserves_job_selection_and_backup_and_unblocks_continue(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "data"
            content = inspect_story_index(write_story_index(root / "source"))
            store = PregenerationJobStore(root / "pregeneration" / "jobs")
            selected = (content.selections[0].selection_id,)
            job = store.create_or_resume(content, selected)
            store.save_selection(content, selected)
            legacy_job = store.directory_for(job.job_id) / "job.json"
            legacy_selection = (
                store.root / "selections" / f"{content.catalog_story_index_sha256}.json"
            )
            write_versioned_json(legacy_job, job_schema_version, job.to_document())
            write_versioned_json(
                legacy_selection,
                1,
                {
                    "story_index_sha256": content.catalog_story_index_sha256,
                    "selected_story_ids": list(selected),
                },
            )
            original_job = legacy_job.read_bytes()
            store.catalog.path.unlink()
            store.catalog.required_marker.unlink()
            pool = ManualThreadPool()
            update = StorageUpgradeDialog(root, thread_pool=pool)
            self.addCleanup(update.deleteLater)
            update.show()
            update.update_button.click()
            self.assertTrue(update.runner.active)
            update.reject()
            self.assertTrue(update.isVisible())
            self.assertFalse(update.later_button.isEnabled())
            pool.run_next()
            self.application.processEvents()
            self.assertTrue(update.completed, update.status.text())
            self.assertEqual(store.load(job.job_id), job)
            self.assertEqual(store.selection_for_content(content), selected)
            backups = list(update.backup.rglob("job.json"))
            self.assertTrue(any(path.read_bytes() == original_job for path in backups))
            update.update_button.click()
            self.assertFalse(update.isVisible())
            stories = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=store,
                thread_pool=ManualThreadPool(),
            )
            self.addCleanup(stories.deleteLater)
            self.assertEqual(stories.selected_story_ids(), selected)
            self.assertTrue(stories.continue_button.isEnabled())
            self.assertIsNone(stories._preparation_catalog_error)
            stories.close()

    def test_failure_stays_actionable_and_is_logged_and_can_be_retried(self):
        with TemporaryDirectory() as directory:
            pool = ManualThreadPool()
            dialog = StorageUpgradeDialog(Path(directory), thread_pool=pool)
            self.addCleanup(dialog.deleteLater)
            dialog.show()
            with patch(
                "vntts.storage_upgrade_ui.upgrade_storage",
                side_effect=PermissionError("Backup directory is read only"),
            ):
                dialog.update_button.click()
                with self.assertLogs("vntts.async_ui", level="ERROR") as logged:
                    pool.run_next()
                self.application.processEvents()
            self.assertFalse(dialog.completed)
            self.assertIn("Backup directory is read only", dialog.status.text())
            self.assertIn("Backup directory is read only", logged.output[0])
            self.assertTrue(dialog.update_button.isEnabled())
            self.assertTrue(dialog.later_button.isEnabled())
            dialog.reject()

    def test_later_does_not_start_or_change_storage(self):
        with TemporaryDirectory() as directory:
            pool = ManualThreadPool()
            root = Path(directory)
            dialog = StorageUpgradeDialog(root, thread_pool=pool)
            self.addCleanup(dialog.deleteLater)
            dialog.show()
            dialog.later_button.click()
            self.assertEqual(pool.tasks, [])
            self.assertEqual(list(root.iterdir()), [])
            self.assertFalse(dialog.backup.exists())

    def test_ordinary_current_storage_does_not_show_an_upgrade(self):
        with (
            patch(
                "vntts.storage_upgrade_ui.storage_upgrade_required", return_value=False
            ),
            patch("vntts.storage_upgrade_ui.StorageUpgradeDialog") as dialog,
        ):
            offer_storage_upgrade(Path("unused"))
        dialog.assert_not_called()

    def test_startup_offers_upgrade_before_device_and_storage_consumers(self):
        from vntts.app import main

        events = []
        with (
            TemporaryDirectory() as directory,
            patch("vntts.app.get_local_data_directory", return_value=Path(directory)),
            patch("vntts.app.configure_application_error_log"),
            patch("vntts.app.configure_bundled_dependencies"),
            patch("vntts.app.enable_windows_dpi_awareness"),
            patch(
                "vntts.storage_upgrade_ui.offer_storage_upgrade",
                side_effect=lambda _root: events.append("upgrade"),
            ),
            patch(
                "vntts.system_audio.initialize_audio_device_monitor",
                side_effect=lambda: events.append("devices"),
            ),
            patch(
                "vntts.app.TrayApplication",
                side_effect=lambda _app: events.append("consumers") or Mock(),
            ),
            patch.object(self.application, "exec", return_value=0),
        ):
            self.assertEqual(main([]), 0)
        self.assertEqual(events, ["upgrade", "devices", "consumers"])


if __name__ == "__main__":
    unittest.main()

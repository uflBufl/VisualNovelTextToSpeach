"""Durability, current-schema boundaries, and atomic preparation updates."""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.preparation_catalog as catalog_module
from tests.story_fixtures import write_story_index
from vntts.pregeneration_setup import (
    PregenerationJobStore,
    PregenerationSetupError,
    inspect_story_index,
)
from vntts.preparation_catalog import (
    PreparationCatalog,
    PreparationCatalogError,
)
from vntts.versioned_json import write_versioned_json


class PreparationCatalogTest(unittest.TestCase):
    def test_nonempty_jobs_without_catalog_require_manual_recreation(self):
        with TemporaryDirectory() as temporary:
            catalog = PreparationCatalog(Path(temporary) / "jobs", owned_root=True)
            legacy = catalog.path.parent / ("a" * 24) / "job.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"legacy job authority")
            with self.assertRaisesRegex(PreparationCatalogError, "back up"):
                catalog.jobs()
            self.assertFalse(catalog.path.exists())
            catalog.required_marker.touch()
            with self.assertRaises(PreparationCatalogError) as caught:
                catalog.jobs()
            self.assertIn("restore", str(caught.exception))
            self.assertFalse(catalog.path.exists())
            self.assertEqual(legacy.read_bytes(), b"legacy job authority")

    def test_interrupted_schema_creation_never_publishes_empty_database(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "legacy.json"
            original.write_bytes(b"preserved legacy metadata")
            catalog = PreparationCatalog(root)
            first_statement = catalog_module._SCHEMA[0]

            def interrupted_schema():
                yield first_statement
                raise KeyboardInterrupt()

            with (
                patch.object(catalog_module, "_SCHEMA", interrupted_schema()),
                self.assertRaises(KeyboardInterrupt),
            ):
                catalog.save_selection("a" * 64, ("story",))
            self.assertFalse(catalog.path.exists())
            self.assertEqual(list(root.iterdir()), [original])
            self.assertEqual(original.read_bytes(), b"preserved legacy metadata")
            catalog.save_selection("a" * 64, ("story",))
            self.assertEqual(catalog.selection("a" * 64), ("story",))

    def test_absent_reads_do_not_create_catalog(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "absent"
            catalog = PreparationCatalog(root)
            self.assertIsNone(catalog.load_job("a" * 24))
            self.assertEqual(catalog.jobs(), ())
            self.assertIsNone(catalog.selection("b" * 64))
            self.assertEqual(catalog.input_directories(), ())
            self.assertIsNone(catalog.input_document(root / "input"))
            self.assertFalse(root.exists())

    def test_jobs_and_empty_selection_survive_restart_without_json_writes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = inspect_story_index(write_story_index(root))
            store = PregenerationJobStore(root / "jobs")
            selected = (content.selections[0].selection_id,)
            job = store.create_or_resume(content, selected)
            store.save_selection(content, ())
            restarted = PregenerationJobStore(store.root)
            self.assertEqual(restarted.load(job.job_id), job)
            self.assertEqual(restarted.jobs_for_content(content), (job,))
            self.assertEqual(restarted.selection_for_content(content), ())
            self.assertFalse((store.directory_for(job.job_id) / "job.json").exists())
            self.assertFalse(
                (
                    store.root
                    / "selections"
                    / f"{content.catalog_story_index_sha256}.json"
                ).exists()
            )
            self.assertEqual(store.directory_for(job.job_id), store.root / job.job_id)
            prepared = store.mark_prepared(job)
            self.assertEqual(restarted.load(job.job_id), prepared)
            self.assertEqual(restarted.story_statuses(content), {selected[0]: "ready"})
            with closing(sqlite3.connect(store.catalog.path)) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(jobs)")
                }
                self.assertNotIn("document", columns)
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone(), (3,)
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT line_id FROM job_lines ORDER BY ordinal"
                    ).fetchall(),
                    [(line,) for line in job.selected_line_ids],
                )

    def test_existing_json_is_refused_without_reading_or_changing_it(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = inspect_story_index(write_story_index(root))
            store = PregenerationJobStore(root / "source")
            selected = (content.selections[0].selection_id,)
            job = store.create_or_resume(content, selected)
            legacy = PregenerationJobStore(root / "legacy")
            path = legacy.directory_for(job.job_id) / "job.json"
            path.parent.mkdir(parents=True)
            write_versioned_json(path, 1, job.to_document())
            selection_path = (
                legacy.root
                / "selections"
                / f"{content.catalog_story_index_sha256}.json"
            )
            selection_path.parent.mkdir()
            write_versioned_json(
                selection_path,
                1,
                {
                    "story_index_sha256": content.catalog_story_index_sha256,
                    "selected_story_ids": list(selected),
                },
            )
            original = path.read_bytes(), selection_path.read_bytes()
            with (
                patch.object(
                    catalog_module.json,
                    "loads",
                    side_effect=AssertionError("old JSON was read"),
                ),
                self.assertRaisesRegex(PregenerationSetupError, "back up"),
            ):
                legacy.jobs_for_content(content)
            self.assertFalse(legacy.catalog.path.exists())
            self.assertEqual((path.read_bytes(), selection_path.read_bytes()), original)

    def test_invalid_legacy_job_is_preserved_and_not_imported(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = PregenerationJobStore(root / "jobs")
            path = store.directory_for("a" * 24) / "job.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"schema_version":99}')
            original = path.read_bytes()
            with self.assertRaises(PregenerationSetupError):
                store.load("a" * 24)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(store.catalog.path.exists())

    def test_failed_replace_rolls_back_job_and_ordered_children(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = inspect_story_index(write_story_index(root))
            store = PregenerationJobStore(root / "jobs")
            job = store.create_or_resume(content, (content.selections[0].selection_id,))
            invalid = job.to_document()
            invalid["status"] = "prepared"
            invalid["selected_line_ids"] = [None]
            with self.assertRaises(PreparationCatalogError):
                store.catalog.save_job(invalid)
            self.assertEqual(store.load(job.job_id), job)

    def test_concurrent_insert_only_save_does_not_replace_existing_job(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = inspect_story_index(write_story_index(root))
            store = PregenerationJobStore(root / "jobs")
            job = store.create_or_resume(content, (content.selections[0].selection_id,))
            prepared = replace(job, status="prepared")

            def insert_existing():
                PreparationCatalog(store.root).save_job(
                    job.to_document(), replace=False
                )

            def save_new():
                PreparationCatalog(store.root).save_job(prepared.to_document())

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(insert_existing) for _ in range(8)]
                futures.append(executor.submit(save_new))
                for future in futures:
                    future.result()
            self.assertEqual(store.load(job.job_id), prepared)

    def test_concurrent_fresh_jobs_share_one_initialized_catalog(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = inspect_story_index(write_story_index(root / "content"))
            selected = (content.selections[0].selection_id,)

            def create():
                return PregenerationJobStore(root / "jobs").create_or_resume(
                    content, selected
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(create) for _ in range(2)]
                first, second = (future.result() for future in futures)
            self.assertEqual(first, second)
            self.assertEqual(
                PregenerationJobStore(root / "jobs").jobs_for_content(content), (first,)
            )

    def test_missing_expected_catalog_cannot_recreate_completed_or_planned_work(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = inspect_story_index(write_story_index(root / "content"))
            store = PregenerationJobStore(root / "jobs")
            job = store.create_or_resume(content, (content.selections[0].selection_id,))
            store.catalog.path.unlink()
            self.assertTrue(store.catalog.required_marker.is_file())
            with self.assertRaisesRegex(PregenerationSetupError, "catalog is missing"):
                PregenerationJobStore(store.root).create_or_resume(
                    content, job.selected_story_ids
                )
            self.assertFalse(store.catalog.path.exists())

    def test_unsupported_versions_are_refused_without_mutating_original_bytes(self):
        for version in (1, 2, 99):
            with self.subTest(version=version), TemporaryDirectory() as temporary:
                catalog = PreparationCatalog(Path(temporary), owned_root=True)
                catalog.initialize()
                with closing(sqlite3.connect(catalog.path)) as connection:
                    connection.execute(f"PRAGMA user_version = {version}")
                    connection.commit()
                original = catalog.path.read_bytes()
                for operation in (
                    catalog.jobs,
                    lambda: catalog.input_staging(catalog.path.parent / "input"),
                    lambda: catalog.save_selection("a" * 64, ()),
                ):
                    with self.assertRaisesRegex(
                        PreparationCatalogError,
                        "Unsupported preparation catalog schema",
                    ) as caught:
                        operation()
                    self.assertIn(f"{version} (expected 3)", str(caught.exception))
                    self.assertIn(str(catalog.path), str(caught.exception))
                    self.assertIn("including generated audio", str(caught.exception))
                    self.assertEqual(catalog.path.read_bytes(), original)
                self.assertTrue(catalog.required_marker.is_file())

    def test_catalog_rejects_corrupt_future_and_linked_database(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = PreparationCatalog(root)
            catalog.path.write_bytes(b"not sqlite")
            with self.assertRaises(PreparationCatalogError):
                catalog.jobs()
            catalog.path.unlink()
            catalog.path.write_bytes(b"")
            with self.assertRaises(PreparationCatalogError):
                catalog.save_selection("a" * 64, ())
            self.assertEqual(catalog.path.read_bytes(), b"")
            catalog.path.unlink()
            catalog.save_selection("a" * 64, ())
            with closing(sqlite3.connect(catalog.path)) as connection:
                connection.execute("PRAGMA user_version = 99")
            with self.assertRaises(PreparationCatalogError):
                catalog.jobs()
            catalog.path.rename(root / "original.sqlite3")
            catalog.path.symlink_to(root / "original.sqlite3")
            with self.assertRaises(PreparationCatalogError):
                catalog.jobs()

    def test_input_registry_is_indexed_immutable_and_survives_restart(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = inspect_story_index(write_story_index(root))
            store = PregenerationJobStore(root / "jobs")
            job = store.create_or_resume(content, (content.selections[0].selection_id,))
            directory = store.directory_for(job.job_id) / "generation-input-example"
            document = {
                "identity": "a" * 64,
                "job_id": job.job_id,
                "queue_sha256": "b" * 64,
                "source_story_index_sha256": job.story_index_sha256,
                "ready_items": 2,
            }
            staging = directory.with_name(".generation-input-staging")
            store.catalog.save_input(job.job_id, document, directory, staging=staging)
            restarted = PreparationCatalog(store.root)
            self.assertEqual(restarted.input_staging(directory), staging.resolve())
            restarted.save_input(job.job_id, dict(document), directory)
            self.assertIsNone(store.catalog.input_staging(directory))
            self.assertEqual(restarted.input_document(directory), document)
            self.assertEqual(
                restarted.input_directories(content.catalog_story_index_sha256),
                (directory.resolve(),),
            )
            self.assertEqual(restarted.input_directories("c" * 64), ())
            with self.assertRaises(PreparationCatalogError):
                restarted.save_input(
                    job.job_id, {**document, "ready_items": 3}, directory
                )
            with self.assertRaises(PreparationCatalogError):
                restarted.save_input(
                    job.job_id, {**document, "identity": "d" * 64}, directory
                )
            self.assertEqual(restarted.input_document(directory), document)


if __name__ == "__main__":
    unittest.main()

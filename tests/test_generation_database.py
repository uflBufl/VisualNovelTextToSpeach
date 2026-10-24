import copy
import json
import os
import sqlite3
import subprocess
import sys
import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.symlink_support import symlink_or_skip
from vntts import generation_database as database


def item(line="one", *, attempts=1):
    return {
        "line_id": f"line:{line}",
        "text_sha256": "b" * 64,
        "status": "generated",
        "review_status": "pending_review",
        "voice_character": "Hero",
        "path": f"audio/{line}.wav",
        "file_sha256": "c" * 64,
        "attempts": attempts,
        "attempts_by_provider": {"synthetic": attempts},
        "provenance": {"seed": 3, "configured": [True, None]},
    }


def state():
    return {
        "schema": "vntts.authoring-generation-state",
        "schema_version": 1,
        "queue_sha256": "a" * 64,
        "game": "Synthetic Game",
        "language": "en",
        "active": None,
        "items": {"one": item(), "two": item("two", attempts=2)},
        "synthesis_controls": {
            "d" * 64: [
                {
                    "role": "voice",
                    "kind": "file",
                    "path": "voice.wav",
                    "sha256": "e" * 64,
                }
            ]
        },
    }


class GenerationDatabaseTest(unittest.TestCase):
    def test_unregistered_saved_output_requires_explicit_upgrade_without_legacy_reads(
        self,
    ):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            database.require_registered_output(output)
            (output / ".generation-lease.json").write_text("lease")
            database.require_registered_output(output)
            legacy = output / "unregistered-recording.wav"
            legacy.write_bytes(b"saved work")
            with patch.object(
                database,
                "read_regular_bytes",
                side_effect=AssertionError("opened saved work"),
            ):
                with self.assertRaisesRegex(ValueError, "explicit storage upgrade"):
                    database.require_registered_output(output)
            database.write_state(database.state_path(output), state())
            database.require_registered_output(output)

    def test_progress_refuses_old_and_inexact_working_state_schema_versions(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            for schema, version in (
                ("r1999.bulk-generation-state", 1),
                ("vntts.authoring-generation-state", True),
                ("vntts.authoring-generation-state", 1.0),
            ):
                value = state()
                value.update(schema=schema, schema_version=version)
                database.write_state(path, value)
                with self.assertRaisesRegex(ValueError, "working-state schema"):
                    database.read_progress(path)
                with self.assertRaisesRegex(ValueError, "working-state schema"):
                    database.active_attempt(path)

    def test_all_success_paths_are_available_without_decoding_item_evidence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            database.write_state(path, state())
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE items SET payload='invalid evidence' WHERE queue_id='two'"
                )
                connection.commit()
            with patch.object(
                database, "_object", side_effect=AssertionError("decoded evidence")
            ):
                self.assertEqual(
                    set(database.read_audio_paths(path)),
                    {"audio/one.wav", "audio/two.wav"},
                )
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE items SET audio_path=NULL WHERE queue_id='two'"
                )
                connection.commit()
            with self.assertRaisesRegex(ValueError, "audio path"):
                database.read_audio_paths(path)

    def test_progress_reads_counts_and_line_identity_without_completed_evidence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            value = state()
            value["items"]["cancelled"] = {
                "status": "failed",
                "failure": {"kind": "cancelled", "attempt_counted": False},
            }
            value["items"]["failed"] = {
                "status": "failed",
                "failure": {"kind": "backend_error"},
            }
            for status in ("live_fallback", "omitted"):
                value["items"][status] = {"status": status, "review_status": status}
            value["active"] = {"phase": "rendering", "generation_owner_pid": 42}
            database.write_state(path, value)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE items SET payload = 'not JSON' WHERE status = 'generated'"
                )
                connection.execute("UPDATE controls SET payload = 'not JSON'")
                connection.commit()
            before = path.read_bytes()
            with patch.object(
                database, "decode_json", wraps=database.decode_json
            ) as decode:
                progress = database.read_progress(path)
            self.assertEqual(
                (progress.generated, progress.failed, progress.other_terminal),
                (2, 1, 2),
            )
            self.assertEqual(progress.queue_sha256, value["queue_sha256"])
            self.assertEqual(progress.active, value["active"])
            self.assertEqual(
                progress.ready_lines, (("line:one", "b" * 64), ("line:two", "b" * 64))
            )
            self.assertEqual(decode.call_count, 1)
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(ValueError):
                database.read_state(path)

    def test_progress_rejects_counted_cancellation_before_renderer_creation(self):
        for marker in ({}, {"attempt_counted": True}, {"attempt_counted": 0}):
            with self.subTest(marker=marker), TemporaryDirectory() as directory:
                path = Path(directory) / "generation-state.sqlite3"
                value = state()
                value["items"] = {
                    "cancelled": {
                        "status": "failed",
                        "failure": {"kind": "cancelled", **marker},
                    }
                }
                database.write_state(path, value)
                with self.assertRaisesRegex(ValueError, "explicit storage upgrade"):
                    database.read_progress(path)

    def test_progress_rejects_malformed_failed_evidence(self):
        for payload in ("not JSON", "[]", '{"status":"approved"}'):
            with self.subTest(payload=payload), TemporaryDirectory() as directory:
                path = Path(directory) / "generation-state.sqlite3"
                value = state()
                value["items"]["failed"] = {"status": "failed"}
                database.write_state(path, value)
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute(
                        "UPDATE items SET payload = ? WHERE status = 'failed'",
                        (payload,),
                    )
                    connection.commit()
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    database.read_progress(path)
                self.assertEqual(path.read_bytes(), before)

    def test_progress_is_one_snapshot_during_concurrent_update(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            original = state()
            original["active"] = {"phase": "initial"}
            database.write_state(path, original)
            changed = copy.deepcopy(original)
            changed["active"] = {"phase": "updated"}
            changed["items"]["one"].update(status="failed", review_status=None)
            updated = threading.Event()
            finished = threading.Event()
            errors = []
            write = database._write
            counts = database._progress_counts

            def notified(*args, **kwargs):
                write(*args, **kwargs)
                updated.set()

            def writer():
                try:
                    database.write_state(path, changed, changed_items=("one",))
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()

            thread = threading.Thread(target=writer)

            def read_counts(connection):
                thread.start()
                self.assertTrue(updated.wait(10))
                self.assertFalse(finished.wait(0.1))
                return counts(connection)

            with (
                patch.object(database, "_write", side_effect=notified),
                patch.object(database, "_progress_counts", side_effect=read_counts),
            ):
                progress = database.read_progress(path)
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(progress.active, original["active"])
            self.assertEqual((progress.generated, progress.failed), (2, 0))
            self.assertEqual(database.read_progress(path).active, changed["active"])

    def test_complete_initial_state_is_published_atomically_and_round_trips(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "generation-state.sqlite3"
            legacy = root / "generation-state.json"
            original = json.dumps(state())
            legacy.write_text(original)
            link = os.link
            inspected = []

            def inspect_publication(source, destination):
                self.assertFalse(path.exists())
                self.assertEqual(database.read_state(source), state())
                inspected.append(source)
                return link(source, destination)

            with patch(
                "vntts.generation_database.os.link", side_effect=inspect_publication
            ):
                database.write_state(path, state(), changed_items=())
            self.assertEqual(len(inspected), 1)
            self.assertEqual(database.state_path(root), path)
            self.assertEqual(database.read_state(path), state())
            self.assertEqual(database.read_snapshot(path.read_bytes()), state())
            self.assertEqual(legacy.read_text(), original)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT revision FROM run").fetchone(), (1,)
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT attempts FROM items WHERE queue_id='two'"
                    ).fetchone(),
                    (2,),
                )
                metadata = json.loads(
                    connection.execute("SELECT metadata FROM run").fetchone()[0]
                )
                self.assertNotIn("items", metadata)
                self.assertNotIn("synthesis_controls", metadata)

    def test_optional_legacy_controls_shape_is_preserved(self):
        for kind in ("absent", "null", "object"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                path = Path(directory) / "generation-state.sqlite3"
                value = state()
                if kind == "absent":
                    del value["synthesis_controls"]
                elif kind == "null":
                    value["synthesis_controls"] = None
                database.write_state(path, value)
                self.assertEqual(database.read_state(path), value)
                self.assertEqual(database.read_snapshot(path.read_bytes()), value)
                value["synthesis_controls"] = {}
                database.write_state(path, value, changed_items=())
                self.assertEqual(database.read_state(path), value)

    def test_invalid_json_values_fail_without_publishing_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            value = state()
            value["items"]["one"]["provenance"]["invalid"] = object()
            with self.assertRaises(ValueError):
                database.write_state(path, value)
            self.assertFalse(path.exists())

    def test_missing_reads_do_not_create_directories_or_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "missing"
            path = root / "generation-state.sqlite3"
            self.assertEqual(database.state_path(root), path)
            for operation in (
                lambda: database.read_state(path),
                lambda: database.read_progress(path),
                lambda: database.active_attempt(path),
            ):
                with self.assertRaises(ValueError):
                    operation()
            self.assertFalse(root.exists())

    def test_failed_first_write_never_masks_legacy_json_with_empty_database(self):
        for failure in ("write", "sync"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "generation-state.sqlite3"
                legacy = root / "generation-state.json"
                legacy.write_text(json.dumps(state()))
                original = legacy.read_bytes()
                target = "_write" if failure == "write" else "_sync_regular_file"
                with patch.object(
                    database, target, side_effect=OSError("interrupted publication")
                ):
                    with self.assertRaises(OSError):
                        database.write_state(path, state())
                self.assertFalse(path.exists())
                self.assertEqual(database.state_path(root), path)
                self.assertEqual(legacy.read_bytes(), original)
                self.assertEqual(list(root.iterdir()), [legacy])
                database.write_state(path, state())
                self.assertEqual(database.read_state(path), state())

    def test_failed_transaction_rolls_back_metadata_items_and_controls(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            original = state()
            database.write_state(path, original)
            changed = copy.deepcopy(original)
            changed["active"] = {"phase": "publishing"}
            changed["items"]["one"]["attempts"] = 4
            changed["items"]["two"]["status"] = "invalid"
            changed["synthesis_controls"] = {}
            with self.assertRaises(ValueError):
                database.write_state(path, changed)
            self.assertEqual(database.read_state(path), original)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT revision FROM run").fetchone(), (1,)
                )

    def test_full_and_delta_updates_delete_only_the_requested_items(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            original = state()
            database.write_state(path, original)
            changed = copy.deepcopy(original)
            changed["items"]["one"]["attempts"] = 5
            changed["items"]["one"]["attempts_by_provider"]["synthetic"] = 5
            changed["items"]["two"]["unserializable"] = object()
            database.write_state(path, changed, changed_items=("one",))
            expected = copy.deepcopy(original)
            expected["items"]["one"] = changed["items"]["one"]
            self.assertEqual(database.read_state(path), expected)
            changed = copy.deepcopy(expected)
            del changed["items"]["one"]
            database.write_state(path, changed, changed_items=("one",))
            self.assertEqual(database.read_state(path), changed)
            changed["items"] = {"three": item("three")}
            changed["synthesis_controls"] = {}
            database.write_state(path, changed)
            self.assertEqual(database.read_state(path), changed)

    def test_active_only_update_does_not_encode_or_write_any_item(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            original = state()
            database.write_state(path, original)
            changed = copy.deepcopy(original)
            changed["active"] = {
                "queue_id": "one",
                "phase": "generating",
                "total_attempts": 3,
            }
            changed["items"]["one"]["unserializable"] = object()
            queries = []
            connect = sqlite3.connect

            def traced(*args, **kwargs):
                connection = connect(*args, **kwargs)
                connection.set_trace_callback(queries.append)
                return connection

            with patch("vntts.generation_database.sqlite3.connect", side_effect=traced):
                database.write_state(path, changed, changed_items=())
            self.assertFalse(
                any(
                    query.startswith(
                        ("INSERT INTO items", "UPDATE items", "DELETE FROM items")
                    )
                    for query in queries
                )
            )
            self.assertEqual(database.active_attempt(path), changed["active"])
            expected = copy.deepcopy(original)
            expected["active"] = changed["active"]
            self.assertEqual(database.read_state(path), expected)

    def test_failed_and_interrupted_items_without_line_identity_round_trip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            value = state()
            value["items"]["failed"] = {
                "status": "failed",
                "attempts": 0,
                "last_error": "interrupted",
                "attempts_by_provider": {"synthetic": 0},
            }
            database.write_state(path, value)
            self.assertEqual(database.read_state(path), value)
            selected = database.read_state(
                path, selected_line_ids=frozenset({"line:one"})
            )
            self.assertEqual(set(selected["items"]), {"one", "failed"})

    def test_selected_read_decodes_only_selected_items_and_checks_all_ids_and_statuses(
        self,
    ):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            value = state()
            database.write_state(path, value)
            with patch.object(database, "_object", wraps=database._object) as decoded:
                selected = database.read_state(
                    path,
                    selected_line_ids=frozenset({"line:one"}),
                    allowed_queue_ids=frozenset(value["items"]),
                )
            self.assertEqual(set(selected["items"]), {"one"})
            self.assertEqual(decoded.call_count, 2)
            self.assertEqual(
                selected["synthesis_controls"], value["synthesis_controls"]
            )
            with self.assertRaisesRegex(ValueError, "unknown queue IDs"):
                database.read_state(
                    path,
                    selected_line_ids=frozenset({"line:one"}),
                    allowed_queue_ids=frozenset({"one"}),
                )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "UPDATE items SET review_status='invalid' WHERE queue_id='two'"
                )
            with self.assertRaisesRegex(ValueError, "invalid item status"):
                database.read_state(path, selected_line_ids=frozenset({"line:one"}))

    def test_waveform_publication_requires_full_item_evidence_and_large_selection_is_supported(
        self,
    ):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            value = state()
            value["reviewed_waveform_publication"] = {"authority": "all-items"}
            database.write_state(path, value)
            self.assertEqual(
                database.read_state(path, selected_line_ids=frozenset()), value
            )
            del value["reviewed_waveform_publication"]
            database.write_state(path, value)
            selected = frozenset(
                {f"line:{index}" for index in range(1900)} | {"line:one"}
            )
            self.assertEqual(
                set(database.read_state(path, selected_line_ids=selected)["items"]),
                {"one"},
            )

    def test_payload_and_index_disagreement_is_rejected(self):
        for field, value in (
            ("line_id", "other"),
            ("attempts", 99),
            ("audio_sha256", "other"),
        ):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                path = Path(directory) / "generation-state.sqlite3"
                database.write_state(path, state())
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(
                        f"UPDATE items SET {field}=? WHERE queue_id='one'", (value,)
                    )
                with self.assertRaisesRegex(ValueError, "index differs"):
                    database.read_state(path)
                with self.assertRaisesRegex(ValueError, "index differs"):
                    database.read_snapshot(path.read_bytes())

    def test_future_corrupt_and_unrelated_databases_are_never_overwritten(self):
        for damage in ("future", "application", "corrupt", "empty"):
            with self.subTest(damage=damage), TemporaryDirectory() as directory:
                path = Path(directory) / "generation-state.sqlite3"
                if damage in {"future", "application"}:
                    database.write_state(path, state())
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute(
                            "PRAGMA user_version=999"
                            if damage == "future"
                            else "PRAGMA application_id=123"
                        )
                else:
                    path.write_bytes(
                        b"corrupt saved database" if damage == "corrupt" else b""
                    )
                before = path.read_bytes()
                for operation in (
                    lambda: database.read_state(path),
                    lambda: database.write_state(path, state()),
                    lambda: database.active_attempt(path),
                    lambda: database.read_snapshot(before),
                ):
                    with self.assertRaises(ValueError):
                        operation()
                    self.assertEqual(path.read_bytes(), before)

    def test_schema_without_committed_state_is_not_silently_initialized(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            database.write_state(path, state())
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DELETE FROM run")
            before = path.read_bytes()
            for operation in (
                lambda: database.read_state(path),
                lambda: database.write_state(path, state()),
            ):
                with self.assertRaisesRegex(ValueError, "no committed state"):
                    operation()
                self.assertEqual(path.read_bytes(), before)

    def test_changed_schema_views_triggers_and_indexes_are_rejected_before_writes(self):
        changes = (
            "CREATE VIEW fake AS SELECT * FROM items",
            "CREATE TRIGGER destructive AFTER UPDATE ON run BEGIN DELETE FROM items; END",
            "ALTER TABLE items ADD COLUMN extra TEXT",
            "DROP INDEX items_line",
            "CREATE INDEX unexpected ON items(attempts)",
        )
        for statement in changes:
            with self.subTest(statement=statement), TemporaryDirectory() as directory:
                path = Path(directory) / "generation-state.sqlite3"
                database.write_state(path, state())
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute(statement)
                before = path.read_bytes()
                for operation in (
                    lambda: database.read_state(path),
                    lambda: database.write_state(path, state()),
                    lambda: database.read_snapshot(before),
                ):
                    with self.assertRaisesRegex(ValueError, "schema objects"):
                        operation()
                    self.assertEqual(path.read_bytes(), before)

    def test_symlink_database_journals_and_parent_are_rejected(self):
        for suffix in ("", "-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "generation-state.sqlite3"
                outside = root / "outside"
                outside.write_bytes(b"saved work")
                symlink_or_skip(Path(str(path) + suffix), outside)
                for operation in (
                    lambda: database.read_state(path),
                    lambda: database.write_state(path, state()),
                ):
                    with self.assertRaises(ValueError):
                        operation()
                self.assertEqual(outside.read_bytes(), b"saved work")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            alias = root / "alias"
            symlink_or_skip(alias, outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                database.write_state(alias / "generation-state.sqlite3", state())
            self.assertEqual(list(outside.iterdir()), [])

    def test_stable_read_snapshot_survives_concurrent_writer(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            original = state()
            database.write_state(path, original)
            changed = copy.deepcopy(original)
            changed["items"]["one"]["attempts"] = 8
            updated = threading.Event()
            finished = threading.Event()
            errors = []
            write = database._write

            def notified(*args, **kwargs):
                write(*args, **kwargs)
                updated.set()

            def writer():
                try:
                    database.write_state(path, changed, changed_items=("one",))
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()

            with patch.object(database, "_write", side_effect=notified):
                with database._connection(path) as connection:
                    self.assertEqual(
                        connection.execute("SELECT revision FROM run").fetchone(), (1,)
                    )
                    thread = threading.Thread(target=writer)
                    thread.start()
                    self.assertTrue(updated.wait(10))
                    self.assertFalse(finished.is_set())
                    self.assertEqual(database._read(connection), original)
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(database.read_state(path), changed)

    def test_concurrent_initial_delta_publication_keeps_all_item_counters(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "generation-state.sqlite3"
            program = "from pathlib import Path; import sys; from vntts import generation_database as d; key=sys.argv[2]; d.write_state(Path(sys.argv[1]), {'queue_sha256':'a'*64,'active':None,'items':{key:{'status':'failed','attempts':int(key)}},'synthesis_controls':{}}, changed_items=(key,))"
            children = [
                subprocess.Popen(
                    [sys.executable, "-c", program, str(path), str(index)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for index in range(6)
            ]
            try:
                for child in children:
                    _stdout, stderr = child.communicate(timeout=30)
                    self.assertEqual(child.returncode, 0, stderr.decode())
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate()
            result = database.read_state(path)
            self.assertEqual(set(result["items"]), {str(index) for index in range(6)})
            for index in range(6):
                self.assertEqual(result["items"][str(index)]["attempts"], index)
            self.assertEqual(list(root.iterdir()), [path])

    def test_file_snapshot_blocks_commit_until_committed_bytes_are_captured(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            original = state()
            database.write_state(path, original)
            changed = copy.deepcopy(original)
            changed["items"]["one"]["attempts"] = 8
            updated = threading.Event()
            finished = threading.Event()
            errors = []
            write = database._write
            read = database.read_regular_bytes

            def notified(*args, **kwargs):
                write(*args, **kwargs)
                updated.set()

            def writer():
                try:
                    database.write_state(path, changed, changed_items=("one",))
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()

            thread = threading.Thread(target=writer)

            def capture(snapshot_path):
                thread.start()
                self.assertTrue(updated.wait(10))
                self.assertFalse(finished.wait(0.1))
                return read(snapshot_path)

            with (
                patch.object(database, "_write", side_effect=notified),
                patch.object(database, "read_regular_bytes", side_effect=capture),
            ):
                payload = database.snapshot_file(path)
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(database.read_snapshot(payload), original)
            self.assertEqual(database.read_state(path), changed)

    def test_another_queue_cannot_replace_saved_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "generation-state.sqlite3"
            database.write_state(path, state())
            changed = state()
            changed["queue_sha256"] = "f" * 64
            with self.assertRaisesRegex(ValueError, "another queue"):
                database.write_state(path, changed)
            self.assertEqual(database.read_state(path), state())


if __name__ == "__main__":
    unittest.main()

import hashlib
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread, current_thread
from unittest.mock import patch

from tests.symlink_support import symlink_or_skip
from vntts.recording_index import (
    _APPLICATION_ID,
    _OBSOLETE_SCHEMA,
    RecordingIndex,
    RecordingIndexError,
    _bucket,
    _receipt,
    _validate_row,
)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


SOURCE = sha("queue"), sha("state"), sha("input")
LINES = {line: sha(line) for line in ("ready", "pending", "missing", "other")}


class RecordingIndexTest(unittest.TestCase):
    def test_missing_read_does_not_create_storage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "absent")
            self.assertIsNone(index.load(root / "input", SOURCE, LINES))
            self.assertFalse(index.root.exists())

    def test_membership_distinguishes_unchecked_notready_and_absent_lines(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            recording = root / "saved.wav", sha("wav")
            index.save(
                root / "input", SOURCE, LINES, {"ready": recording, "missing": None}
            )
            requested = dict(LINES, absent=sha("absent"), other=sha("changed text"))
            self.assertEqual(
                index.load(root / "input", SOURCE, requested),
                ({"ready": recording}, frozenset({"pending"})),
            )
            self.assertEqual(
                index.load(root / "input", SOURCE, {"missing": LINES["missing"]}),
                ({}, frozenset()),
            )

    def test_same_source_merges_checks_after_restart_and_separates_directories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            first = root / "first.wav", sha("first")
            second = root / "second.wav", sha("second")
            index.save(root / "input", SOURCE, LINES, {"ready": first})
            RecordingIndex(index.root).save(
                root / "input", SOURCE, LINES, {"pending": second}
            )
            index.save(root / "different", SOURCE, LINES, {"missing": None})
            self.assertEqual(
                index.load(root / "input", SOURCE, LINES),
                ({"ready": first, "pending": second}, frozenset({"missing", "other"})),
            )
            self.assertEqual(
                index.load(root / "different", SOURCE, LINES),
                ({}, frozenset({"ready", "pending", "other"})),
            )

    def test_changed_source_resets_all_old_readiness(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            index.save(
                root / "input", SOURCE, LINES, {"ready": (root / "old.wav", sha("old"))}
            )
            changed = SOURCE[0], sha("new state"), SOURCE[2]
            self.assertIsNone(index.load(root / "input", changed, LINES))
            index.save(root / "input", changed, LINES, {"pending": None})
            self.assertEqual(
                index.load(root / "input", changed, LINES),
                ({}, frozenset({"ready", "missing", "other"})),
            )
            self.assertIsNone(index.load(root / "input", SOURCE, LINES))

    def test_receipt_detects_changed_deleted_or_forged_ready_rows_and_save_rebuilds(
        self,
    ):
        mutations = (
            "DELETE FROM lines WHERE line_id = 'pending'",
            "DELETE FROM directories",
            "UPDATE lines SET checked = 1 WHERE line_id = 'pending'",
            "UPDATE lines SET audio_path = '/forged.wav' WHERE line_id = 'ready'",
            "UPDATE lines SET text_sha256 = '"
            + sha("changed")
            + "' WHERE line_id = 'pending'",
            "UPDATE directories SET receipt_sha256 = '" + sha("forged") + "'",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                index = RecordingIndex(root / "index")
                index.save(
                    root / "input",
                    SOURCE,
                    LINES,
                    {"ready": (root / "old.wav", sha("old"))},
                )
                with closing(sqlite3.connect(index.path)) as connection, connection:
                    connection.execute(mutation)
                self.assertIsNone(index.load(root / "input", SOURCE, LINES))
                index.save(root / "input", SOURCE, LINES, {"missing": None})
                self.assertEqual(
                    index.load(root / "input", SOURCE, LINES),
                    ({}, frozenset({"ready", "pending", "other"})),
                )

    def test_selected_lookup_validates_only_requested_buckets(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            lines = {f"line-{number}": sha(str(number)) for number in range(10_000)}
            index.save(root / "input", SOURCE, lines, {"line-10": None})
            selected = {"line-10": lines["line-10"], "absent": sha("absent")}
            expected = sum(
                _bucket(line) in {_bucket(key) for key in selected} for line in lines
            )
            with patch(
                "vntts.recording_index._validate_row", wraps=_validate_row
            ) as validate:
                self.assertEqual(
                    index.load(root / "input", SOURCE, selected), ({}, frozenset())
                )
            self.assertEqual(validate.call_count, expected)
            self.assertLess(expected, 150)
            with closing(sqlite3.connect(index.path)) as connection, connection:
                connection.execute("DELETE FROM lines WHERE line_id='line-10'")
            self.assertIsNone(index.load(root / "input", SOURCE, selected))

    def test_missing_or_moved_bucket_cannot_hide_a_requested_line(self):
        mutations = (
            "DELETE FROM buckets WHERE bucket=?",
            "UPDATE lines SET bucket=(bucket+1)%256 WHERE bucket=?",
            "UPDATE buckets SET receipt_sha256='" + sha("forged") + "' WHERE bucket=?",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                index = RecordingIndex(root / "index")
                index.save(
                    root / "input",
                    SOURCE,
                    LINES,
                    {"ready": (root / "saved.wav", sha("audio"))},
                )
                with closing(sqlite3.connect(index.path)) as connection, connection:
                    connection.execute(mutation, (_bucket("ready"),))
                self.assertIsNone(
                    index.load(root / "input", SOURCE, {"ready": LINES["ready"]})
                )

    def test_obsolete_derived_index_rebuilds_without_touching_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            recording = root / "saved.wav"
            recording.write_bytes(b"consumer-owned audio")
            index.root.mkdir()
            with closing(sqlite3.connect(index.path)) as connection, connection:
                for statement in _OBSOLETE_SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 1")
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            self.assertIsNone(index.load(root / "input", SOURCE, LINES))
            index.save(
                root / "input", SOURCE, LINES, {"ready": (recording, sha("audio"))}
            )
            self.assertEqual(
                index.load(root / "input", SOURCE, {"ready": LINES["ready"]}),
                ({"ready": (recording, sha("audio"))}, frozenset()),
            )
            with closing(sqlite3.connect(index.path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone(), (2,)
                )
            self.assertEqual(recording.read_bytes(), b"consumer-owned audio")

    def test_obsolete_version_with_unknown_schema_is_never_discarded(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            index.root.mkdir()
            with closing(sqlite3.connect(index.path)) as connection, connection:
                for statement in _OBSOLETE_SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 1")
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                connection.execute("CREATE VIEW unknown AS SELECT 1")
            before = index.path.read_bytes()
            with self.assertRaises(RecordingIndexError):
                index.load(root / "input", SOURCE, LINES)
            with self.assertRaises(RecordingIndexError):
                index.save(root / "input", SOURCE, LINES, {})
            self.assertEqual(index.path.read_bytes(), before)

    def test_corrupt_file_rebuild_does_not_touch_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            recording = root / "saved.wav"
            recording.write_bytes(b"audio owned by the consumer")
            before = recording.stat().st_ino
            index.save(
                root / "input", SOURCE, LINES, {"ready": (recording, sha("audio"))}
            )
            index.path.write_bytes(b"corrupt derived database")
            self.assertIsNone(index.load(root / "input", SOURCE, LINES))
            index.save(
                root / "input", SOURCE, LINES, {"ready": (recording, sha("audio"))}
            )
            self.assertEqual(
                index.load(root / "input", SOURCE, LINES)[0],
                {"ready": (recording, sha("audio"))},
            )
            self.assertEqual(recording.read_bytes(), b"audio owned by the consumer")
            self.assertEqual(recording.stat().st_ino, before)

    def test_unsupported_version_application_and_schema_are_never_overwritten(self):
        for mutation in (
            "PRAGMA user_version = 3",
            "PRAGMA application_id = 1",
            "CREATE VIEW future AS SELECT 1",
            "CREATE TRIGGER changed AFTER INSERT ON lines BEGIN DELETE FROM directories; END",
        ):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                index = RecordingIndex(root / "index")
                index.save(root / "input", SOURCE, LINES, {})
                with closing(sqlite3.connect(index.path)) as connection, connection:
                    connection.execute(mutation)
                before = index.path.read_bytes()
                with self.assertRaises(RecordingIndexError):
                    index.load(root / "input", SOURCE, LINES)
                with self.assertRaises(RecordingIndexError):
                    index.save(root / "input", SOURCE, LINES, {})
                self.assertEqual(index.path.read_bytes(), before)

    def test_failed_commit_rolls_back_and_does_not_replace_valid_database(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            recording = root / "first.wav", sha("first")
            index.save(root / "input", SOURCE, LINES, {"ready": recording})
            inode = index.path.stat().st_ino
            connect = sqlite3.connect

            class FailingCommit(sqlite3.Connection):
                def commit(self):
                    raise sqlite3.OperationalError("simulated I/O failure")

            def fail_commit(*args, **kwargs):
                return connect(*args, **kwargs, factory=FailingCommit)

            with patch(
                "vntts.recording_index.sqlite3.connect", side_effect=fail_commit
            ):
                with self.assertRaises(RecordingIndexError):
                    index.save(root / "input", SOURCE, LINES, {"pending": None})
            self.assertEqual(index.path.stat().st_ino, inode)
            self.assertEqual(
                index.load(root / "input", SOURCE, LINES),
                ({"ready": recording}, frozenset({"pending", "missing", "other"})),
            )

    def test_concurrent_initial_writers_merge_checked_subsets(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            script = """
from pathlib import Path
import sys
from tests.test_recording_index import SOURCE, LINES
from vntts.recording_index import RecordingIndex
root, line = Path(sys.argv[1]), sys.argv[2]
RecordingIndex(root / "index").save(root / "input", SOURCE, LINES, {line: (root / (line + ".wav"), LINES[line])})
"""
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(root), line],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for line in LINES
            ]
            for process in processes:
                _, errors = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, errors.decode())
            index = RecordingIndex(root / "index")
            self.assertEqual(
                index.load(root / "input", SOURCE, LINES),
                (
                    {
                        line: (root / (line + ".wav"), checksum)
                        for line, checksum in LINES.items()
                    },
                    frozenset(),
                ),
            )
            self.assertEqual(list(index.root.glob(".recordings-*.sqlite3")), [])

    def test_load_uses_one_stable_snapshot_while_another_writer_commits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            first = root / "first.wav", sha("first")
            second = root / "second.wav", sha("second")
            index.save(root / "input", SOURCE, LINES, {"ready": first})
            started, finished = Event(), Event()
            errors = []

            def update():
                started.set()
                try:
                    index.save(root / "input", SOURCE, LINES, {"pending": second})
                except Exception as error:
                    errors.append(error)
                finally:
                    finished.set()

            writer = Thread(target=update)
            reader = current_thread()

            def start_writer(*args):
                if current_thread() is reader and not started.is_set():
                    writer.start()
                    self.assertTrue(started.wait(2))
                return _receipt(*args)

            with patch("vntts.recording_index._receipt", side_effect=start_writer):
                loaded = index.load(root / "input", SOURCE, LINES)
            self.assertEqual(
                loaded, ({"ready": first}, frozenset({"pending", "missing", "other"}))
            )
            writer.join(5)
            self.assertTrue(finished.is_set())
            self.assertEqual(errors, [])
            self.assertEqual(
                index.load(root / "input", SOURCE, LINES)[0],
                {"ready": first, "pending": second},
            )

    def test_database_replacement_while_opening_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            index.save(root / "input", SOURCE, LINES, {})
            replacement = root / "future.sqlite3"
            replacement.write_bytes(index.path.read_bytes())
            with closing(sqlite3.connect(replacement)) as connection, connection:
                connection.execute("PRAGMA user_version = 3")
            before = replacement.read_bytes()
            connect = sqlite3.connect

            def replace_then_connect(*args, **kwargs):
                replacement.replace(index.path)
                return connect(*args, **kwargs)

            with patch(
                "vntts.recording_index.sqlite3.connect",
                side_effect=replace_then_connect,
            ):
                with self.assertRaises(RecordingIndexError):
                    index.load(root / "input", SOURCE, LINES)
            self.assertEqual(index.path.read_bytes(), before)

    def test_database_journal_and_guard_aliases_are_rejected(self):
        for suffix in ("", "-journal", "-wal", "-shm", "guard"):
            with self.subTest(suffix=suffix), TemporaryDirectory() as directory:
                root = Path(directory)
                index = RecordingIndex(root / "index")
                index.save(root / "input", SOURCE, LINES, {})
                outside = root / "outside"
                outside.write_bytes(b"unrelated")
                target = (
                    index.root / ".recordings.guard"
                    if suffix == "guard"
                    else Path(str(index.path) + suffix)
                )
                target.unlink(missing_ok=True)
                symlink_or_skip(target, outside)
                with self.assertRaises(RecordingIndexError):
                    index.load(root / "input", SOURCE, LINES)
                with self.assertRaises(RecordingIndexError):
                    index.save(root / "input", SOURCE, LINES, {})
                self.assertEqual(outside.read_bytes(), b"unrelated")

    def test_invalid_checked_membership_and_relative_audio_paths_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            index = RecordingIndex(root / "index")
            with self.assertRaises(ValueError):
                index.save(root / "input", SOURCE, LINES, {"unknown": None})
            with self.assertRaises(ValueError):
                index.save(
                    root / "input",
                    SOURCE,
                    LINES,
                    {"ready": (Path("relative.wav"), sha("audio"))},
                )
            self.assertFalse(index.root.exists())

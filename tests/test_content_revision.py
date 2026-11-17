import hashlib
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.story_index import StoryIndexError, write_story_index_document

from tests.story_fixtures import write_content
from vntts.content_revision import ContentRevision, source_signature
from vntts.path_safety import open_regular_binary
from vntts.story_index_snapshot import load_story_index_snapshot


class ContentRevisionTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = write_content(Path(temporary.name))
        document = load_story_index_snapshot(self.source, self.source.read_bytes())
        records = [row.to_record() for row in document.records]
        records.append({**records[-1], "line_id": "other", "chapter": "other"})
        write_story_index_document(self.source, document.metadata, records)
        self.signature = source_signature(self.source)
        self.payload = self.source.read_bytes()
        self.document = load_story_index_snapshot(self.source, self.payload)
        self.revision = ContentRevision.capture(
            self.source, self.payload, self.document, signature=self.signature
        )

    def test_reads_only_requested_chapter_rows(self):
        self.assertEqual(self.revision.source_path, self.source.absolute())
        reads = []

        class TrackedReader:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, length):
                reads.append(length)
                return self.stream.read(length)

        @contextmanager
        def tracked_open(path):
            with open_regular_binary(path) as stream:
                yield TrackedReader(stream)

        with patch("vntts.content_revision.open_regular_binary", tracked_open):
            chapter = self.revision.read_chapter("1")
        self.assertEqual({row.chapter for row in chapter.records}, {"1"})
        self.assertEqual(len(reads), len(chapter.records))
        self.assertLess(sum(reads), len(self.payload))
        self.assertEqual(chapter.metadata["line_count"], len(chapter.records))

    def test_catalog_revision_uses_bounded_sql_chapter_locations(self):
        from vntts.story_catalog import StoryCatalog

        storage = StoryCatalog(self.source.with_suffix(".sqlite3"))
        evidence_sha256 = hashlib.sha256(
            self.source.with_name("source-audio-semantic-evidence.json").read_bytes()
        ).hexdigest()
        storage.save(
            {
                "schema_version": 1,
                "source_story_index_sha256": self.revision.source_sha256,
                "source_audio_semantic_evidence_sha256": evidence_sha256,
                "game": self.document.game,
                "selections": [],
            },
            story=self.document,
        )
        with storage.open_dialogue(
            self.source, self.revision.source_sha256, evidence_sha256, cache_version=1
        ) as owner:
            revision = ContentRevision.from_catalog(owner)
            self.assertEqual(dict(revision._chapters), {})
            self.assertEqual(revision.source_sha256, self.revision.source_sha256)
            self.assertEqual(
                revision.read_chapter("other").records,
                self.revision.read_chapter("other").records,
            )
            self.assertEqual(revision.read_chapter("absent").records, ())

    def test_rejects_source_changed_after_payload_capture(self):
        self.source.write_bytes(self.payload + b"\n")
        with self.assertRaisesRegex(StoryIndexError, "changed during admission"):
            ContentRevision.capture(
                self.source, self.payload, self.document, signature=self.signature
            )

    def test_admitted_chapter_reads_do_not_poll_or_hash_source(self):
        self.source.write_bytes(self.payload + b"\n")
        with (
            patch(
                "vntts.content_revision.source_signature",
                side_effect=AssertionError("source polled"),
            ),
            patch("vntts.content_revision.hashlib") as hashing,
        ):
            hashing.sha256.side_effect = AssertionError("source rehashed")
            chapter = self.revision.read_chapter("1")
        self.assertEqual(
            chapter.records,
            tuple(row for row in self.document.records if row.chapter == "1"),
        )

    def test_missing_source_reports_actual_read_failure(self):
        self.source.unlink()
        with self.assertRaises(OSError):
            self.revision.read_chapter("1")


if __name__ == "__main__":
    unittest.main()

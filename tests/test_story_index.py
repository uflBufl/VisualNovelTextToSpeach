import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.story_index import StoryIndexError, load_story_index


class StoryIndexTest(unittest.TestCase):
    def write_records(self, records):
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        path = Path(temporary_directory.name) / "story.jsonl"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )
        return path

    def test_loads_versioned_line_records(self):
        path = self.write_records(
            [
                {
                    "record_type": "metadata",
                    "schema": "vntts.story-index",
                    "schema_version": 1,
                    "line_count": 1,
                },
                {
                    "record_type": "line",
                    "line_id": "game:1:2",
                    "chapter": "1",
                    "sequence": 2,
                    "speaker": "Ada",
                    "text": "Hello",
                    "kind": "dialogue",
                },
            ]
        )
        metadata, lines = load_story_index(path)
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(lines[0].speaker, "Ada")

    def test_rejects_unknown_versions_and_count_mismatches(self):
        path = self.write_records(
            [
                {
                    "record_type": "metadata",
                    "schema": "vntts.story-index",
                    "schema_version": 2,
                    "line_count": 0,
                }
            ]
        )
        with self.assertRaisesRegex(StoryIndexError, "schema version"):
            load_story_index(path)

        path = self.write_records(
            [
                {
                    "record_type": "metadata",
                    "schema": "vntts.story-index",
                    "schema_version": 1,
                    "line_count": 1,
                }
            ]
        )
        with self.assertRaisesRegex(StoryIndexError, "count mismatch"):
            load_story_index(path)


if __name__ == "__main__":
    unittest.main()

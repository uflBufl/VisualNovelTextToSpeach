import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.source_audio_progress import SourceAudioProgress, read_source_audio_progress


class SourceAudioProgressTest(unittest.TestCase):
    def test_accepts_valid_counts_and_rejects_invalid_or_partial_snapshots(self):
        document = {
            "schema": "r1999.source-audio-progress",
            "schema_version": 1,
            "stage": "semantics",
            "processed": 3,
            "total": 8,
            "state": "analyzing",
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            self.assertIsNone(read_source_audio_progress(path))
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(
                read_source_audio_progress(path),
                SourceAudioProgress("semantics", 3, 8, "analyzing"),
            )
            path.write_text(
                json.dumps(document | {"stage": "portraits", "state": "publishing"}),
                encoding="utf-8",
            )
            self.assertEqual(
                read_source_audio_progress(path),
                SourceAudioProgress("portraits", 3, 8, "publishing"),
            )
            for changed in (
                {"schema_version": True},
                {"schema_version": 2},
                {"processed": True},
                {"processed": -1},
                {"processed": 9},
                {"total": 2**31},
                {"stage": "unknown"},
                {"state": "publishing"},
            ):
                with self.subTest(changed=changed):
                    path.write_text(json.dumps(document | changed), encoding="utf-8")
                    self.assertIsNone(read_source_audio_progress(path))
            for payload in ('{"processed":', "[]", "\ud800", " " * 4097):
                with self.subTest(payload=repr(payload)):
                    path.write_bytes(payload.encode("utf-8", errors="surrogatepass"))
                    self.assertIsNone(read_source_audio_progress(path))

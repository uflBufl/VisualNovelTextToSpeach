import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.symlink_support import symlink_or_skip
from vntts.live_speaker_corpus import LiveSpeakerCorpus


class LiveSpeakerCorpusTest(unittest.TestCase):
    def test_loads_a_versioned_unique_explicit_scope(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "speakers.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "Rhiannon session",
                        "speakers": [
                            "Rhiannon",
                            "Hotelier",
                            "Adar Llwch Gwin Fledgling",
                            "Narrator",
                            "???",
                        ],
                    }
                ),
                encoding="utf-8",
            )

            corpus = LiveSpeakerCorpus.load(path)

            self.assertEqual(corpus.path, path.resolve())
            self.assertEqual(corpus.sha256, sha256(path.read_bytes()).hexdigest())
            self.assertIs(corpus.revalidate(), corpus)

        self.assertEqual(corpus.name, "Rhiannon session")
        self.assertEqual(corpus.speakers[0], "Rhiannon")
        self.assertEqual(len(corpus.speakers), 5)

    def test_rejects_duplicate_or_empty_speaker_entries(self):
        for speakers in (["Rhiannon", "rhiannon"], ["Rhiannon", ""]):
            with self.subTest(speakers=speakers), TemporaryDirectory() as directory:
                path = Path(directory) / "speakers.json"
                path.write_text(
                    json.dumps({"schema_version": 1, "speakers": speakers}),
                    encoding="utf-8",
                )

                with self.assertRaises(ValueError):
                    LiveSpeakerCorpus.load(path)

    def test_revalidation_rejects_changed_bytes_and_symlinked_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "speakers.json"
            path.write_text(
                json.dumps({"schema_version": 1, "speakers": ["Rhiannon"]}),
                encoding="utf-8",
            )
            corpus = LiveSpeakerCorpus.load(path)
            path.write_text(
                json.dumps({"schema_version": 1, "speakers": ["Hotelier"]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "changed after settings"):
                corpus.revalidate()

            link = root / "linked.json"
            symlink_or_skip(link, path)
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                LiveSpeakerCorpus.load(link)


if __name__ == "__main__":
    unittest.main()

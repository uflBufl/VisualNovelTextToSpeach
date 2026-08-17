import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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


if __name__ == "__main__":
    unittest.main()

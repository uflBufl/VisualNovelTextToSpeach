import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.game_pack import (
    GamePackError,
    create_game_pack_artifact_bindings,
)

from vntts.game_pack import preflight_game_pack_checksums


class GamePackChecksumPreflightTest(unittest.TestCase):
    def test_preflight_validates_required_and_optional_artifacts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            voices = root / "voices" / "manifest.json"
            generated = root / "generated" / "manifest.json"
            voices.parent.mkdir()
            generated.parent.mkdir()
            story.write_bytes(b"story")
            voices.write_bytes(b"voices")
            generated.write_bytes(b"generated")
            bindings = create_game_pack_artifact_bindings(
                root,
                {
                    "story_index": story,
                    "voice_manifest": voices,
                    "generated_audio": generated,
                },
            )

            validated = preflight_game_pack_checksums(root, bindings)

        self.assertEqual(
            [binding.name for binding in validated],
            ["generated_audio", "story_index", "voice_manifest"],
        )

    def test_preflight_rejects_missing_required_artifact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            story.write_bytes(b"story")
            bindings = create_game_pack_artifact_bindings(
                root,
                {"story_index": story},
            )

            with self.assertRaisesRegex(GamePackError, "voice_manifest"):
                preflight_game_pack_checksums(root, bindings)

    def test_preflight_rejects_modified_artifact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            voices = root / "manifest.json"
            story.write_bytes(b"story")
            voices.write_bytes(b"voices")
            bindings = create_game_pack_artifact_bindings(
                root,
                {"story_index": story, "voice_manifest": voices},
            )
            voices.write_bytes(b"tampered")

            with self.assertRaisesRegex(GamePackError, "checksum does not match"):
                preflight_game_pack_checksums(root, bindings)


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts_artifacts.voice_manifest import load_voice_manifest

from tests.symlink_support import symlink_or_skip
from vntts.authoring.cli import create_parser
from vntts.authoring.cli_generation import (
    _load_stable_voice_registry,
    _run_bulk_generation,
)
from vntts.voices import CharacterVoiceRegistry, VoiceManifestError


class AuthoringGenerationCleanupTest(unittest.TestCase):
    def test_generation_cleanup_keeps_primary_failure_and_rejects_failed_release(self):
        arguments = create_parser().parse_args(
            [
                "generate",
                "--queue",
                "unused-queue.jsonl",
                "--output",
                "unused-generated",
                "--voice-manifest",
                "unused-voices.json",
                "--backend",
                "pocket-tts",
            ]
        )
        for primary in (None, ValueError("generation failed"), KeyboardInterrupt()):
            with self.subTest(primary=primary):
                cleanup_error = RuntimeError("shutdown failed")
                backend = SimpleNamespace(
                    generation_profile="stable",
                    model_name="fake",
                    shutdown=Mock(side_effect=cleanup_error),
                )
                expected = primary if primary is not None else cleanup_error
                policy = Mock()
                with (
                    patch(
                        "vntts.authoring.cli_generation.run_bulk_generation",
                        side_effect=primary,
                    ),
                    self.assertRaises(type(expected)) as raised,
                ):
                    _run_bulk_generation(
                        arguments,
                        Mock(return_value=backend),
                        None,
                        None,
                        Mock(),
                        SimpleNamespace(items=()),
                        None,
                        {},
                        None,
                        (),
                        {},
                        {},
                        policy,
                        policy,
                        None,
                    )
                self.assertIs(raised.exception, expected)
                backend.shutdown.assert_called_once_with()


class AuthoringGenerationVoiceInputTest(unittest.TestCase):
    def test_captured_manifest_uses_original_root_and_retains_metadata(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                path = root / "manifest.json"
                document = {
                    "voices": [
                        {
                            "character": "Voice",
                            "speaker": "original",
                            "aliases": ["Alias"],
                            "reference": "references/source.wav",
                            "vntts.source_character": " Display identity ",
                            "vntts.reference_transcript": " Exact original line. ",
                        }
                    ]
                }
                if not legacy:
                    document["version"] = 2
                payload = json.dumps(document).encode()
                path.write_bytes(payload)
                expected = CharacterVoiceRegistry.from_file(path)

                def load_captured(snapshot):
                    path.write_text("{}", encoding="utf-8")
                    return load_voice_manifest(snapshot)

                with patch(
                    "vntts.authoring.cli_generation.load_voice_manifest",
                    side_effect=load_captured,
                ):
                    registry, digest, captured, entries = _load_stable_voice_registry(
                        path
                    )
                self.assertEqual(registry.unique_voices(), expected.unique_voices())
                self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
                self.assertEqual(captured, document)
                self.assertEqual(entries[0].character, "Voice")
                voice = registry.resolve("Alias")
                self.assertEqual(voice.reference_root, root)
                self.assertEqual(voice.source_character, "Display identity")
                self.assertEqual(voice.reference_transcript, "Exact original line.")

    def test_captured_manifest_rejects_symlinked_references(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.wav"
            outside.write_bytes(b"reference")
            manifest_root = root / "manifest"
            manifest_root.mkdir()
            path = manifest_root / "manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Voice",
                                "speaker": "original",
                                "reference": "linked.wav",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            symlink_or_skip(manifest_root / "linked.wav", outside)
            with self.assertRaisesRegex(VoiceManifestError, "symlink"):
                _load_stable_voice_registry(path)

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.audio import PCM16_MONO_WAV_FORMAT, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import GamePackError, write_game_pack
from vntts_artifacts.generated_audio import write_generated_audio_manifest
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.live_sequence import write_live_sequence_plan
from vntts_artifacts.story_index import write_story_index
from vntts_artifacts.voice_manifest import write_voice_manifest

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.game_pack import apply_game_pack, import_game_pack, main
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.settings import AppSettings, load_app_settings
from vntts.voices import CharacterVoiceRegistry


def write_synthetic_game_pack(root, *, include_generated=True, include_sequence=False):
    line_id = "synthetic:chapter-1:line-7"
    text = "Keep this exact line intact."
    text_hash = text_sha256(text)
    story = root / "story-index.jsonl"
    write_story_index(
        story,
        {"game": "Synthetic Game", "language": "en"},
        [
            {
                "record_type": "line",
                "line_id": line_id,
                "chapter": "chapter-1",
                "sequence": 7,
                "speaker": "Ada",
                "text": text,
                "kind": "dialogue",
                "source_audio_status": "absent",
            }
        ],
    )

    voice_wav = root / "voices" / "ada.wav"
    write_pcm16_wav(voice_wav, np.zeros(240, dtype=np.float32), 24_000)
    voices = root / "voice-manifest.json"
    write_voice_manifest(
        voices,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Ada",
                    "speaker": "ada-v1",
                    "references": ["voices/ada.wav"],
                }
            ],
        },
    )

    generated = None
    generated_wav = None
    if include_generated:
        generated_wav = root / "generated" / "line-7.wav"
        write_pcm16_wav(
            generated_wav,
            np.linspace(-0.1, 0.1, 240, dtype=np.float32),
            24_000,
        )
        generated = root / "generated-audio.json"
        write_generated_audio_manifest(
            generated,
            {"game": "Synthetic Game", "language": "en"},
            [
                {
                    "line_id": line_id,
                    "text_sha256": text_hash,
                    "audio": "generated/line-7.wav",
                    "audio_format": PCM16_MONO_WAV_FORMAT,
                    "audio_sha256": sha256_file(generated_wav),
                    "sample_rate": 24_000,
                    "sample_count": 240,
                }
            ],
        )

    pack_path = root / "game-pack.json"
    components = {"story_index": story, "voice_manifest": voices}
    if generated is not None:
        components["generated_audio"] = generated
    if include_sequence:
        sequence = root / "live-sequence.json"
        write_live_sequence_plan(
            sequence,
            {
                "game_id": "synthetic-game",
                "producer": {"name": "synthetic-extractor", "version": "0.7.0"},
                "source_extract_sha256": "1" * 64,
                "chapters": [
                    {
                        "chapter": "chapter-1",
                        "entry_event_ids": ["event-7"],
                        "events": [
                            {
                                "event_id": "event-7",
                                "sequence": 7,
                                "kind": "speech",
                                "line_id": line_id,
                                "control": "terminal",
                                "successors": [],
                            }
                        ],
                    }
                ],
            },
            story,
        )
        components["live_sequence_plan"] = sequence
    write_game_pack(
        pack_path,
        {
            "game": {"id": "synthetic-game", "version": "1.0"},
            "producers": [{"name": "synthetic-extractor", "version": "0.6.0"}],
            "created_at": "2026-08-16T12:05:00Z",
        },
        components,
    )
    return pack_path, line_id, text, text_hash, generated_wav


class GamePackImportTest(unittest.TestCase):
    def test_public_producer_pack_reaches_all_public_vntts_consumers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app_data = root / "app-data"
            app_data.mkdir()
            sentinel = app_data / "keep.txt"
            sentinel.write_text("preserve me", encoding="utf-8")
            pack_path, line_id, text, text_hash, _generated_wav = (
                write_synthetic_game_pack(root)
            )

            imported = import_game_pack(pack_path)
            settings = imported.apply_to(
                AppSettings(
                    screenshot_directory=str(app_data),
                    generated_audio_manifest="stale-generated.json",
                )
            )
            line = ChapterVoicePreloader.load_optional(
                settings.story_index
            ).resolve_exact("Ada", text)
            voice = CharacterVoiceRegistry.from_file(settings.voice_manifest).resolve(
                "Ada"
            )
            generated = GeneratedAudioLibrary.load_optional(
                settings.generated_audio_manifest
            ).find(line_id, text_hash)

            self.assertEqual(imported.pack.game_id, "synthetic-game")
            self.assertEqual(line.line_id, line_id)
            self.assertEqual(voice.speaker, "ada-v1")
            self.assertEqual(generated.sample_rate, 24_000)
            self.assertEqual(settings.game_pack, str(pack_path.resolve()))
            self.assertEqual(settings.screenshot_directory, str(app_data))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me")

    def test_loading_settings_preflights_configured_pack_and_applies_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root)

            settings = load_app_settings(
                root / "missing-settings.json",
                environment={"VNTTS_GAME_PACK": str(pack_path)},
            )

        self.assertEqual(settings.game_pack, str(pack_path.resolve()))
        self.assertEqual(Path(settings.story_index).name, "story-index.jsonl")
        self.assertEqual(Path(settings.voice_manifest).name, "voice-manifest.json")
        self.assertEqual(
            Path(settings.generated_audio_manifest).name,
            "generated-audio.json",
        )

    def test_pack_without_generated_audio_clears_stale_generated_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(
                root, include_generated=False
            )

            settings = apply_game_pack(
                AppSettings(generated_audio_manifest="stale-generated.json"),
                pack_path,
            )

        self.assertIsNone(settings.generated_audio_manifest)

    def test_pack_import_clears_a_sequence_plan_bound_to_another_pack(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root)

            settings = apply_game_pack(
                AppSettings(
                    live_sequence_plan="stale-plan.json",
                    live_sequence_mode="shadow",
                ),
                pack_path,
            )

        self.assertIsNone(settings.live_sequence_plan)
        self.assertEqual(settings.live_sequence_mode, "off")

    def test_reapplying_same_pack_preserves_explicit_external_sequence_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root)

            settings = apply_game_pack(
                AppSettings(
                    game_pack=str(pack_path),
                    live_sequence_plan="external-live-sequence.json",
                    live_sequence_mode="audio-auto",
                )
            )

        self.assertEqual(
            settings.live_sequence_plan,
            "external-live-sequence.json",
        )
        self.assertEqual(settings.live_sequence_mode, "audio-auto")

    def test_pack_import_applies_its_checksum_bound_sequence_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root, include_sequence=True)

            settings = apply_game_pack(
                AppSettings(
                    live_sequence_plan="stale-plan.json",
                    live_sequence_mode="audio-manual",
                ),
                pack_path,
            )

        self.assertEqual(Path(settings.live_sequence_plan).name, "live-sequence.json")
        self.assertEqual(settings.live_sequence_mode, "audio-manual")

    def test_preflight_rejects_modified_referenced_wav(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_identity, generated_wav = write_synthetic_game_pack(root)
            generated_wav.write_bytes(b"tampered")

            with self.assertRaisesRegex(GamePackError, "checksum does not match"):
                import_game_pack(pack_path)

    def test_cli_preflight_reports_resolved_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_path, *_unused = write_synthetic_game_pack(root)
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main([str(pack_path)])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["game_id"], "synthetic-game")
        self.assertEqual(payload["game_pack"], str(pack_path.resolve()))


if __name__ == "__main__":
    unittest.main()

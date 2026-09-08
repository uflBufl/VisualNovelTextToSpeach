import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.audio import PCM16_MONO_WAV_FORMAT, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import write_game_pack
from vntts_artifacts.generated_audio import write_generated_audio_manifest
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.story_index import write_story_index
from vntts_artifacts.voice_manifest import write_voice_manifest

from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import PregenerationVoiceError, VoiceDecisionStore
from vntts.settings import AppSettings
from vntts.voice_default_impact import inspect_voice_default_impact


def voice_impact_fixture(root):
    story = root / "story-index.jsonl"
    roles = (
        ("changed", "1", "Rhiannon", "alba"),
        ("matching", "1", "Rhiannon", "marius"),
        ("legacy", "1", "Rhiannon", None),
        ("original", "1", "Rhiannon", None),
        ("other", "2", "Centurion", "alba"),
        ("fallback", "2", "Hotelier", "alba"),
        ("unknown-speaker", "2", "???", "alba"),
    )
    write_story_index(
        story,
        {"game": "Synthetic Game", "language": "en"},
        [
            {
                "record_type": "line",
                "line_id": name,
                "chapter": chapter,
                "sequence": index,
                "speaker": role,
                "voice_character": role,
                "text": f"Line {name}.",
                "kind": "dialogue",
                "speakable": True,
                "source_audio_status": "available" if name == "original" else "absent",
            }
            for index, (name, chapter, role, _voice) in enumerate(roles, 1)
        ],
    )
    content = inspect_story_index(story)
    jobs = PregenerationJobStore(root / "jobs")
    job = jobs.create_or_resume(
        content, tuple(value.selection_id for value in content.selections)
    )
    pack_root = jobs.path_for(job.job_id).parent / "game-packs" / f"pack-{'a' * 24}"
    pack_root.mkdir(parents=True)
    voices = root / "voices.json"
    write_voice_manifest(voices, {"version": 2, "voices": []})
    entries = []
    for name, _chapter, role, voice in roles:
        if name == "original":
            continue
        audio = pack_root / f"{name}.wav"
        write_pcm16_wav(audio, np.linspace(-0.1, 0.1, 2400, dtype=np.float32), 24000)
        entry = {
            "line_id": name,
            "text_sha256": text_sha256(f"Line {name}."),
            "audio": audio.name,
            "audio_format": PCM16_MONO_WAV_FORMAT,
            "audio_sha256": sha256_file(audio),
            "sample_rate": 24000,
            "sample_count": 2400,
            "provider": "pocket-tts",
            "model": "pocket-tts",
            "voice_character": role,
            "synthesis_provenance_sha256": "b" * 64,
        }
        if voice:
            entry["vntts.recorded_voice"] = {
                "schema_version": 1,
                "source_character": voice,
                "speaker": voice,
                "reference_sha256s": [],
                **{
                    field: entry[field]
                    for field in (
                        "audio_sha256",
                        "synthesis_provenance_sha256",
                        "provider",
                        "model",
                        "voice_character",
                    )
                },
            }
        entries.append(entry)
    generated = pack_root / "generated-audio.json"
    write_generated_audio_manifest(
        generated, {"game": "Synthetic Game", "language": "en"}, entries
    )
    # Pack publication requires all component paths inside its own directory.
    pack_story = pack_root / story.name
    pack_story.write_bytes(story.read_bytes())
    pack_voices = pack_root / voices.name
    pack_voices.write_bytes(voices.read_bytes())
    pack = pack_root / "game-pack.json"
    write_game_pack(
        pack,
        {
            "game": {"id": "synthetic-game", "version": "1"},
            "producers": [{"name": "fixture", "version": "1"}],
            "created_at": "2026-09-08T00:00:00Z",
        },
        {
            "story_index": pack_story,
            "voice_manifest": pack_voices,
            "generated_audio": generated,
        },
    )
    settings = AppSettings(
        voice_manifest=str(voices),
        voice_assignments={"Narrator": "preset:alba"},
        character_voice_defaults={
            "Rhiannon": "preset:alba",
            "Centurion": "preset:alba",
        },
    )
    return (
        content,
        jobs,
        VoiceDecisionStore(root / "decisions.json"),
        settings,
        pack_root,
    )


class VoiceDefaultImpactTest(unittest.TestCase):
    def test_changed_default_compares_saved_voices_and_keeps_original_and_unrelated_stories(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content, jobs, decisions, settings, _pack = voice_impact_fixture(root)
            before = {
                path: path.read_bytes() for path in root.rglob("*") if path.is_file()
            }
            result = inspect_voice_default_impact(
                content,
                jobs,
                decisions,
                settings,
                settings.updated(
                    character_voice_defaults={
                        **settings.character_voice_defaults,
                        "Rhiannon": "preset:marius",
                    }
                ),
                "Rhiannon",
            )
            self.assertEqual(result[0].changed_line_ids, ("changed",))
            self.assertEqual(
                (result[0].matching, result[0].unknown, result[0].original), (1, 1, 1)
            )
            self.assertEqual(result[1].changed_line_ids, ())
            self.assertEqual(
                before,
                {path: path.read_bytes() for path in root.rglob("*") if path.is_file()},
            )

    def test_narrator_change_includes_fallback_and_unknown_roles_only(self):
        with TemporaryDirectory() as directory:
            content, jobs, decisions, settings, _pack = voice_impact_fixture(
                Path(directory)
            )
            result = inspect_voice_default_impact(
                content,
                jobs,
                decisions,
                settings,
                settings.updated(voice_assignments={"Narrator": "preset:marius"}),
                "Narrator",
            )
            self.assertEqual(result[0].changed_line_ids, ())
            self.assertEqual(
                result[1].changed_line_ids, ("fallback", "unknown-speaker")
            )

    def test_damaged_recording_cannot_be_reported_as_a_reusable_voice(self):
        with TemporaryDirectory() as directory:
            content, jobs, decisions, settings, pack = voice_impact_fixture(
                Path(directory)
            )
            (pack / "changed.wav").write_bytes(b"damaged")
            with self.assertRaisesRegex(
                (ValueError, RuntimeError), "(checksum|missing|damaged|integrity)"
            ):
                inspect_voice_default_impact(
                    content, jobs, decisions, settings, settings, "Rhiannon"
                )

    def test_changed_source_requires_refresh_before_comparison(self):
        with TemporaryDirectory() as directory:
            content, jobs, decisions, settings, _pack = voice_impact_fixture(
                Path(directory)
            )
            content.story_index.write_text(content.story_index.read_text() + "\n")
            with self.assertRaisesRegex(
                PregenerationVoiceError, "Story content changed"
            ):
                inspect_voice_default_impact(
                    content, jobs, decisions, settings, settings, "Rhiannon"
                )

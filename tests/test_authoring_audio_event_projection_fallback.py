import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    write_generated_audio_manifest,
)
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    write_voice_generation_queue,
)

from tests.test_authoring_legacy_import import write_legacy_fixture
from tests.test_generated_audio import FakeAudioOutput
from vntts.authoring.audio_event_projection_fallback import (
    create_audio_event_projection_fallback_workspace,
)
from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.game_pack import _decision_records
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.legacy_import import import_legacy_job
from vntts.authoring.missing_voice_policy import NARRATOR_ROLES, MissingVoicePolicy
from vntts.authoring.workbench import AuthoringWorkbenchError, create_resume_workspace
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
    LiveFallbackRoute,
)
from vntts.playback import PlaybackStatus, PreparedPlayback, outcome_for_prepared
from vntts.speech_backend import SpeechBackendCapabilities


class AudioEventProjectionFallbackTests(unittest.TestCase):
    def _base(self, root, text="No! *gasp*"):
        fixture = write_legacy_fixture(root / "legacy", text=text)
        old_queue = VoiceGenerationQueue.load(fixture["queue"])
        document = dict(old_queue.items[0].document)
        document.update({"speaker": "Poacher I", "voice_character": "Poacher I"})
        write_voice_generation_queue(fixture["queue"], old_queue.metadata, [document])
        queue = VoiceGenerationQueue.load(fixture["queue"])
        queue_item = queue.items[0]
        policy = MissingVoicePolicy(NARRATOR_ROLES, ("Poacher I",))
        state_path = fixture["state"]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["active"] = None
        item = state["items"][fixture["queue_id"]]
        item.update(
            {
                "status": "generated",
                "review_status": "rejected",
                "speaker": "Poacher I",
                "requested_voice_character": "Poacher I",
                "voice_character": "Narrator",
                "narrator_character": "Rhiannon",
                "synthesis_configuration": {
                    "missing_voice_policy": policy.to_document(),
                    "synthesis_character_overrides": {"poacheri": "Narrator"},
                },
                "synthesis_fallback": {
                    "schema_version": 1,
                    "kind": "missing_voice_to_narrator",
                    "policy": policy.to_document(),
                    "source_voice_character": "Poacher I",
                    "synthesis_voice_character": "Narrator",
                    "narrator_character": "Rhiannon",
                },
            }
        )
        state["queue_sha256"] = sha256_file(fixture["queue"])
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        write_generated_manifest_from_state(
            state,
            state_path.parent,
            state_path.parent / "manifest.json",
        )
        write_story_index_document(
            fixture["job"]["story_index"],
            {
                "game": "Reverse: 1999",
                "language": "en",
                "generated_at": "2026-08-28T00:00:00+00:00",
            },
            [
                {
                    "record_type": "line",
                    "line_id": queue_item.line_id,
                    "text_sha256": queue_item.text_sha256,
                    "text": queue_item.text,
                    "speaker": queue_item.speaker,
                    "voice_character": queue_item.voice_character,
                    "kind": "dialogue",
                    "chapter": "315401",
                    "sequence": 7,
                    "source_audio_status": "absent",
                    "source_audio_reason": "fixture_absent",
                    "source_kind": "story",
                    "speakable": True,
                }
            ],
        )
        voice_manifest = Path(fixture["job"]["voice_manifest"])
        (voice_manifest.parent / "rhiannon.wav").write_bytes(b"voice-reference")
        voice_manifest.write_text(
            json.dumps(
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Rhiannon",
                            "speaker": "Rhiannon",
                            "references": ["rhiannon.wav"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        imported = import_legacy_job(
            fixture["job_directory"], root / "imports"
        ).destination
        workspace = create_resume_workspace(
            imported,
            root / "base-workspaces",
            story_index=fixture["job"]["story_index"],
            voice_manifest=voice_manifest,
            narrator_character="Rhiannon",
            backend="moss-tts",
            model="model",
            generation_profile="stable",
            missing_voice_policy=policy,
        )
        return workspace.directory, queue_item

    def test_exact_projection_is_idempotent_checksum_bound_and_used_at_runtime(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, queue_item = self._base(root / "source")
            base_state = (base / "generated-audio/generation-state.json").read_bytes()
            first = create_audio_event_projection_fallback_workspace(
                base, (queue_item.queue_id,), root / "workspaces"
            )
            second = create_audio_event_projection_fallback_workspace(
                base, (queue_item.queue_id,), root / "workspaces"
            )
            state_path = first.directory / "generated-audio/generation-state.json"
            state = load_generation_state(state_path, first.directory / "queue.jsonl")
            queue = VoiceGenerationQueue.load(first.directory / "queue.jsonl")
            records = _decision_records(
                state,
                queue,
                "live_fallback",
                "Live fallback item",
            )
            manifest = root / "generated-audio.json"
            write_generated_audio_manifest(
                manifest,
                {
                    "vntts.authoring.live_fallback": {
                        "schema_version": 1,
                        "mode": "explicit",
                        "entries": records,
                    }
                },
                [],
            )
            library = GeneratedAudioLibrary(GeneratedAudioIndex.load(manifest))
            live = Mock()
            live.name = "pocket-tts"
            live.model_name = "pocket-tts"
            live.model_identity = None
            live.generation_profile = "default"
            live.capabilities = SpeechBackendCapabilities(True, False, True)
            live.prepare_playback.return_value = PreparedPlayback(
                "live-audio", None, None, "fresh-generation", "live:pocket-tts"
            )
            live.play_prepared.side_effect = lambda prepared, **_kwargs: (
                outcome_for_prepared(prepared, PlaybackStatus.COMPLETED, 0.0)
            )
            live.stop.return_value = False
            resolver = ChapterVoicePreloader(
                [
                    ChapterDialogue(
                        queue_item.line_id,
                        "315401",
                        7,
                        queue_item.speaker,
                        queue_item.text,
                        queue_item.text_sha256,
                    )
                ]
            )
            backend = GeneratedAudioFallbackBackend(
                live, library, resolver, audio_output=FakeAudioOutput()
            )
            route = backend.prepare_route(queue_item.speaker, queue_item.text)
            base_state_after = (
                base / "generated-audio/generation-state.json"
            ).read_bytes()

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.directory, second.directory)
        self.assertEqual(base_state, base_state_after)
        self.assertIsInstance(route, LiveFallbackRoute)
        self.assertEqual(route.decision.schema_version, 6)
        live.prepare_playback.assert_called_once_with("Narrator", "No!")

    def test_rejects_pure_or_unmarked_text_and_detects_projection_tampering(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index, text in enumerate(("*gasp*", "No!")):
                base, queue_item = self._base(root / f"invalid-{index}", text)
                with self.assertRaisesRegex(
                    AuthoringWorkbenchError, "mixed speech and events"
                ):
                    create_audio_event_projection_fallback_workspace(
                        base, (queue_item.queue_id,), root / f"workspaces-{index}"
                    )

            base, queue_item = self._base(root / "mixed")
            result = create_audio_event_projection_fallback_workspace(
                base, (queue_item.queue_id,), root / "workspaces"
            )
            state_path = result.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_item.queue_id]["live_fallback"]["evidence"][
                "spoken_text"
            ] = "Read the marker *gasp*"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with self.assertRaises(BulkGenerationError):
                load_generation_state(state_path, result.directory / "queue.jsonl")


if __name__ == "__main__":
    unittest.main()

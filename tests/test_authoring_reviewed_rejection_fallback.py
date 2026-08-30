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
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from tests import test_authoring_audio_event_projection_fallback
from tests.test_authoring_workbench import create_test_workspace
from tests.test_generated_audio import FakeAudioOutput
from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.game_pack import _live_fallback_records
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.reviewed_rejection_fallback import (
    _downstream_overlay_queue_ids,
    create_reviewed_rejection_fallback_workspace,
)
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
    LiveFallbackRoute,
)
from vntts.playback import PlaybackStatus, PreparedPlayback, outcome_for_prepared
from vntts.speech_backend import SpeechBackendCapabilities


class ReviewedRejectionFallbackTests(unittest.TestCase):
    def _base(self, root):
        base, queue_item = (
            test_authoring_audio_event_projection_fallback.AudioEventProjectionFallbackTests()._base(
                root, text="A rejected line."
            )
        )
        workspace = json.loads((base / "workspace.json").read_text(encoding="utf-8"))
        voice_manifest = base / workspace["voice_manifest"]["path"]
        reference = voice_manifest.parent / "rhiannon.wav"
        state_path = base / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        item = state["items"][queue_item.queue_id]
        item["config_rebase"] = {
            "status": "generated",
            "review_status": "rejected",
            "audio_sha256": item["file_sha256"],
            "source_item_sha256": "1" * 64,
            "projected_item_sha256": "2" * 64,
            "source_effective_character": "Narrator",
            "target_effective_character": "Rhiannon",
            "source_reference_sha256s": [sha256_file(reference)],
            "target_reference_sha256s": [sha256_file(reference)],
            "target_route_status": "active",
            "successor_state": "carried_terminal",
        }
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        write_generated_manifest_from_state(
            state, state_path.parent, state_path.parent / "manifest.json"
        )
        return base, queue_item

    def test_exact_rejection_is_idempotent_and_routes_current_character(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, queue_item = self._base(root / "source")
            base_state = (base / "generated-audio/generation-state.json").read_bytes()
            first = create_reviewed_rejection_fallback_workspace(
                base, root / "workspaces"
            )
            second = create_reviewed_rejection_fallback_workspace(
                base, root / "workspaces"
            )
            state_path = first.directory / "generated-audio/generation-state.json"
            state = load_generation_state(state_path, first.directory / "queue.jsonl")
            queue = VoiceGenerationQueue.load(first.directory / "queue.jsonl")
            records = _live_fallback_records(state, queue)
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
        self.assertEqual(route.decision.schema_version, 7)
        live.prepare_playback.assert_called_once_with("Rhiannon", queue_item.text)

    def test_tampering_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, queue_item = self._base(root / "source")
            result = create_reviewed_rejection_fallback_workspace(
                base, root / "workspaces"
            )
            state_path = result.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_item.queue_id]["live_fallback"]["evidence"][
                "synthesis_character"
            ] = "Centurion"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with self.assertRaises(BulkGenerationError):
                load_generation_state(state_path, result.directory / "queue.jsonl")

    def test_new_identity_uses_ordinary_manifest_character_route(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, workspace = create_test_workspace(root / "source")
            base = workspace.directory
            queue_item = VoiceGenerationQueue.load(base / "queue.jsonl").items[0]
            state_path = base / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active"] = None
            state["items"][queue_item.queue_id].update(
                {
                    "status": "generated",
                    "review_status": "rejected",
                    "speaker": "Rhiannon",
                    "requested_voice_character": "Rhiannon",
                    "voice_character": "Rhiannon",
                }
            )
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            write_generated_manifest_from_state(
                state, state_path.parent, state_path.parent / "manifest.json"
            )

            result = create_reviewed_rejection_fallback_workspace(
                base, root / "workspaces"
            )
            workspace = json.loads(
                (result.directory / "workspace.json").read_text(encoding="utf-8")
            )
            ledger = workspace["reviewed_rejection_live_fallback"]["items"][0]

        self.assertEqual(ledger["queue_id"], queue_item.queue_id)
        self.assertEqual(ledger["route_source"], "voice_manifest")
        self.assertEqual(ledger["synthesis_character"], "Rhiannon")
        self.assertEqual(len(ledger["route_reference_sha256s"]), 2)

    def test_only_declared_downstream_overlay_ids_are_exempt(self):
        workspace = {
            "explicit_fallback_merge": {"items": [{"queue_id": "fallback-id"}]},
            "audio_event_omission": {"items": [{"queue_id": "omission-id"}]},
        }

        self.assertEqual(
            _downstream_overlay_queue_ids(workspace),
            {"fallback-id", "omission-id"},
        )


if __name__ == "__main__":
    unittest.main()

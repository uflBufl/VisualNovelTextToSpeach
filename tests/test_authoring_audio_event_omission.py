import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    write_generated_audio_manifest,
)
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from tests.test_authoring_workbench import create_test_workspace
from tests.test_generated_audio import FakeAudioOutput
from vntts.authoring.audio_event_omission import (
    create_audio_event_omission_workspace,
)
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
)
from vntts.authoring.game_pack import (
    _decision_records,
    _review_counts,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.workbench import AuthoringWorkbenchError, inspect_workspace
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.generated_audio import (
    AudioEventOmissionRoute,
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
)
from vntts.playback import PlaybackStatus, PreparedPlayback, outcome_for_prepared
from vntts.speech_backend import SpeechBackendCapabilities


class AudioEventOmissionTests(unittest.TestCase):
    def _base(self, root, text="*whimper*"):
        _fixture, _imported, created = create_test_workspace(root, text=text)
        base = created.directory
        queue = VoiceGenerationQueue.load(base / "queue.jsonl")
        queue_id = queue.items[0].queue_id
        state_path = base / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["active"] = None
        state["items"].pop(queue_id)
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        write_generated_manifest_from_state(
            state, base / "generated-audio", base / "generated-audio/manifest.json"
        )
        return base, queue.items[0]

    def test_exact_pure_event_omission_is_terminal_idempotent_and_runtime_safe(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, queue_item = self._base(root / "source")
            base_state = (base / "generated-audio/generation-state.json").read_bytes()

            first = create_audio_event_omission_workspace(
                base, (queue_item.queue_id,), root / "workspaces"
            )
            second = create_audio_event_omission_workspace(
                base, (queue_item.queue_id,), root / "workspaces"
            )
            state_path = first.directory / "generated-audio/generation-state.json"
            state = load_generation_state(state_path, first.directory / "queue.jsonl")
            summary = inspect_workspace(first.directory)
            queue = VoiceGenerationQueue.load(first.directory / "queue.jsonl")
            omission_records = _decision_records(
                state,
                queue,
                "audio_event_omission",
                "Audio-event omission",
            )
            runtime_manifest = root / "generated-audio.json"
            write_generated_audio_manifest(
                runtime_manifest,
                {
                    "vntts.authoring.audio_event_omission": {
                        "schema_version": 1,
                        "mode": "explicit",
                        "entries": omission_records,
                    }
                },
                [],
            )
            generated = GeneratedAudioIndex.load(runtime_manifest)
            library = GeneratedAudioLibrary(generated)
            live = Mock()
            live.name = "live-test"
            live.capabilities = SpeechBackendCapabilities(True, False, True)
            live.prepare_playback.return_value = PreparedPlayback(
                "live-audio", None, None, "fresh-generation", "live:live-test"
            )
            live.play_prepared.side_effect = lambda prepared, **_kwargs: (
                outcome_for_prepared(prepared, PlaybackStatus.COMPLETED, 0.0)
            )
            live.stop.return_value = False
            resolver = ChapterVoicePreloader(
                [
                    ChapterDialogue(
                        queue_item.line_id,
                        "1",
                        1,
                        queue_item.speaker,
                        queue_item.text,
                        queue_item.text_sha256,
                    )
                ]
            )
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                resolver,
                audio_output=FakeAudioOutput(),
            )
            backend.voice_override = Mock(return_value=True)
            route = backend.prepare_route(queue_item.speaker, queue_item.text)
            outcome = backend.play_route(route)
            base_state_after = (
                base / "generated-audio/generation-state.json"
            ).read_bytes()

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.directory, second.directory)
        self.assertEqual(summary.omitted, 1)
        self.assertEqual(state["items"][queue_item.queue_id]["status"], "omitted")
        self.assertEqual(_review_counts(state)["omitted_count"], 1)
        self.assertEqual(
            len(generated.metadata["vntts.authoring.audio_event_omission"]["entries"]),
            1,
        )
        self.assertIsInstance(route, AudioEventOmissionRoute)
        self.assertTrue(outcome.successful)
        self.assertEqual(outcome.audio_source, "audio-event-omission")
        live.prepare_playback.assert_not_called()
        self.assertEqual(base_state_after, base_state)

    def test_rejects_mixed_event_and_detects_state_tampering(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mixed, mixed_item = self._base(root / "mixed", "No! *gasp*")
            with self.assertRaisesRegex(AuthoringWorkbenchError, "pure event"):
                create_audio_event_omission_workspace(
                    mixed, (mixed_item.queue_id,), root / "workspaces"
                )

            base, queue_item = self._base(root / "pure")
            result = create_audio_event_omission_workspace(
                base, (queue_item.queue_id,), root / "workspaces"
            )
            state_path = result.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_item.queue_id]["audio_event_omission"][
                "plan_sha256"
            ] = "0" * 64
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with self.assertRaises(BulkGenerationError):
                load_generation_state(state_path, result.directory / "queue.jsonl")


if __name__ == "__main__":
    unittest.main()

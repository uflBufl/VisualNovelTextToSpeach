import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    write_generated_audio_manifest,
)
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
)

import vntts.authoring.audio_event_projection_fallback as successor_module
from tests.audio_output_fixtures import FakeAudioOutput
from tests.authoring_fixtures import (
    create_audio_event_projection_fixture,
)
from tests.symlink_support import symlink_or_skip
from vntts.authoring.audio_event_projection_fallback import (
    create_audio_event_projection_fallback_workspace,
    validate_audio_event_projection_fallback_workspace,
)
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.game_pack import _decision_records
from vntts.authoring.workbench import AuthoringWorkbenchError
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
    LiveFallbackRoute,
)
from vntts.playback import PlaybackStatus, PreparedPlayback, outcome_for_prepared
from vntts.speech_backend import SpeechBackendCapabilities


class AudioEventProjectionFallbackTests(unittest.TestCase):
    def _rebind_batch(self, directory, workspace, queue_id):
        batch = workspace["audio_event_projection_fallback"]
        batch["batch_id"] = canonical_document_sha256(
            {key: value for key, value in batch.items() if key != "batch_id"}
        )
        path = directory / "generated-audio/generation-state.json"
        state = json.loads(path.read_text())
        result = state["items"][queue_id]
        authority = result["live_fallback"]["evidence"]
        for field in (
            "batch_id",
            "base_workspace_id",
            "base_workspace_sha256",
            "base_state_sha256",
            "queue_sha256",
        ):
            authority[field] = batch[field]
        path.write_text(json.dumps(state, sort_keys=True))

    def test_batch_versions_and_invalid_json_values_raise_workbench_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, item = create_audio_event_projection_fixture(root / "source")
            created = create_audio_event_projection_fallback_workspace(
                base, [item.queue_id], root / "successors"
            )
            original = (created.directory / "workspace.json").read_text()
            for version in (True, 1.0):
                with self.subTest(version=version):
                    workspace = json.loads(original)
                    workspace["audio_event_projection_fallback"]["schema_version"] = (
                        version
                    )
                    self._rebind_batch(created.directory, workspace, item.queue_id)
                    with self.assertRaisesRegex(AuthoringWorkbenchError, "malformed"):
                        validate_audio_event_projection_fallback_workspace(
                            created.directory, workspace
                        )
            for value in (float("nan"), object()):
                with self.subTest(value=type(value).__name__):
                    workspace = json.loads(original)
                    workspace["audio_event_projection_fallback"]["items"] = value
                    with self.assertRaisesRegex(AuthoringWorkbenchError, "malformed"):
                        validate_audio_event_projection_fallback_workspace(
                            created.directory, workspace
                        )

    def test_bound_base_files_are_rechecked_after_state_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, item = create_audio_event_projection_fixture(root / "source")
            created = create_audio_event_projection_fallback_workspace(
                base, [item.queue_id], root / "successors"
            )
            workspace = json.loads((created.directory / "workspace.json").read_text())
            original = successor_module.load_stable_workspace_generation_state
            for field in ("base_workspace_path", "base_state_path"):
                target = (
                    created.directory
                    / workspace["audio_event_projection_fallback"][field]
                )
                payload = target.read_bytes()
                for removed in (False, True):
                    with self.subTest(field=field, removed=removed):

                        def load_then_mutate(*args, **kwargs):
                            value = original(*args, **kwargs)
                            if removed:
                                target.unlink()
                            else:
                                target.write_bytes(payload + b" ")
                            return value

                        try:
                            with patch.object(
                                successor_module,
                                "load_stable_workspace_generation_state",
                                load_then_mutate,
                            ):
                                with self.assertRaisesRegex(
                                    AuthoringWorkbenchError, "changed"
                                ):
                                    validate_audio_event_projection_fallback_workspace(
                                        created.directory, workspace
                                    )
                        finally:
                            target.write_bytes(payload)

    def test_base_authority_identity_and_symlinks_are_not_accepted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, item = create_audio_event_projection_fixture(root / "source")
            created = create_audio_event_projection_fallback_workspace(
                base, [item.queue_id], root / "successors"
            )
            original = (created.directory / "workspace.json").read_text()
            with self.subTest(field="base_workspace_id"):
                workspace = json.loads(original)
                workspace["audio_event_projection_fallback"]["base_workspace_id"] = (
                    "wrong-workspace"
                )
                self._rebind_batch(created.directory, workspace, item.queue_id)
                with self.assertRaisesRegex(AuthoringWorkbenchError, "base.*authority"):
                    validate_audio_event_projection_fallback_workspace(
                        created.directory, workspace
                    )
            for field, value in (
                ("queue_sha256", "0" * 64),
                ("active", {"unexpected": True}),
            ):
                with self.subTest(field=field):
                    workspace = json.loads(original)
                    batch = workspace["audio_event_projection_fallback"]
                    target = created.directory / batch["base_state_path"]
                    payload = target.read_bytes()
                    document = json.loads(payload)
                    document[field] = value
                    target.write_text(json.dumps(document, sort_keys=True))
                    batch["base_state_sha256"] = successor_module.sha256_file(target)
                    self._rebind_batch(created.directory, workspace, item.queue_id)
                    try:
                        with self.assertRaisesRegex(
                            AuthoringWorkbenchError, "base.*authority"
                        ):
                            validate_audio_event_projection_fallback_workspace(
                                created.directory, workspace
                            )
                    finally:
                        target.write_bytes(payload)
            workspace = json.loads(original)
            self._rebind_batch(created.directory, workspace, item.queue_id)
            target = (
                created.directory
                / workspace["audio_event_projection_fallback"]["base_workspace_path"]
            )
            alias = target.with_name("alias.json")
            alias.write_bytes(target.read_bytes())
            target.unlink()
            symlink_or_skip(target, alias)
            with self.assertRaisesRegex(AuthoringWorkbenchError, "unsafe|symlink"):
                validate_audio_event_projection_fallback_workspace(
                    created.directory, workspace
                )

    def test_projection_ledger_metadata_is_bound_to_the_queue(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, item = create_audio_event_projection_fixture(root / "source")
            created = create_audio_event_projection_fallback_workspace(
                base, [item.queue_id], root / "successors"
            )
            original = (created.directory / "workspace.json").read_text()
            for field, value in (
                ("line_id", "wrong-line"),
                ("text_sha256", "0" * 64),
                ("speaker", "Wrong speaker"),
            ):
                with self.subTest(field=field):
                    workspace = json.loads(original)
                    workspace["audio_event_projection_fallback"]["items"][0][field] = (
                        value
                    )
                    self._rebind_batch(created.directory, workspace, item.queue_id)
                    with self.assertRaisesRegex(
                        AuthoringWorkbenchError, "result changed"
                    ):
                        validate_audio_event_projection_fallback_workspace(
                            created.directory, workspace
                        )

    def test_exact_projection_is_idempotent_checksum_bound_and_used_at_runtime(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, queue_item = create_audio_event_projection_fixture(root / "source")
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
                base, queue_item = create_audio_event_projection_fixture(
                    root / f"invalid-{index}", text
                )
                with self.assertRaisesRegex(
                    AuthoringWorkbenchError, "mixed speech and events"
                ):
                    create_audio_event_projection_fallback_workspace(
                        base, (queue_item.queue_id,), root / f"workspaces-{index}"
                    )

            base, queue_item = create_audio_event_projection_fixture(root / "mixed")
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

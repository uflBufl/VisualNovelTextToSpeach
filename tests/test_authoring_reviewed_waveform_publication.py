import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import soundfile as sf
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import load_game_pack
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    write_voice_generation_queue,
)

from tests.test_authoring_legacy_import import write_legacy_fixture
from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.game_pack import publish_final_game_pack
from vntts.authoring.legacy_import import import_legacy_job
from vntts.authoring.reviewed_waveform_publication import (
    create_reviewed_waveform_publication_workspace,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    create_resume_workspace,
    load_workspace_authority,
)


class ReviewedWaveformPublicationTests(unittest.TestCase):
    def _base(self, root):
        fixture = write_legacy_fixture(root / "legacy", text="An approved line.")
        old_queue = VoiceGenerationQueue.load(fixture["queue"])
        document = dict(old_queue.items[0].document)
        document.update({"speaker": "Narrator", "voice_character": "Narrator"})
        write_voice_generation_queue(fixture["queue"], old_queue.metadata, [document])
        queue_item = VoiceGenerationQueue.load(fixture["queue"]).items[0]
        state_path = fixture["state"]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        item = state["items"][fixture["queue_id"]]
        item.update({"status": "approved", "review_status": "approved"})
        state["active"] = None
        state["queue_sha256"] = sha256_file(fixture["queue"])
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        write_story_index_document(
            fixture["job"]["story_index"],
            {
                "game": "Reverse: 1999",
                "language": "en",
                "generated_at": "2026-08-29T00:00:00+00:00",
            },
            [
                {
                    "record_type": "line",
                    "line_id": queue_item.line_id,
                    "text_sha256": queue_item.text_sha256,
                    "text": queue_item.text,
                    "speaker": queue_item.speaker,
                    "voice_character": queue_item.voice_character,
                    "kind": "narration",
                    "chapter": "315401",
                    "sequence": 1,
                    "source_audio_status": "absent",
                    "source_audio_reason": "fixture_absent",
                    "source_kind": "story",
                    "speakable": True,
                }
            ],
        )
        voice_manifest = Path(fixture["job"]["voice_manifest"])
        reference = voice_manifest.parent / "centurion.ogg"
        sf.write(
            reference,
            np.sin(np.linspace(0, 8 * np.pi, 8_000, dtype=np.float32)) * 0.2,
            16_000,
            format="OGG",
            subtype="VORBIS",
        )
        voice_manifest.write_text(
            json.dumps(
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Centurion",
                            "speaker": "centurion",
                            "references": ["centurion.ogg"],
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
            narrator_character="Centurion",
            backend="moss-tts",
            model="model",
            generation_profile="stable",
        )
        return workspace.directory, queue_item

    def test_migration_is_idempotent_exact_and_pack_round_trips(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, queue_item = self._base(root / "source")
            base_state = (base / "generated-audio/generation-state.json").read_bytes()
            first = create_reviewed_waveform_publication_workspace(
                base, root / "workspaces"
            )
            second = create_reviewed_waveform_publication_workspace(
                base, root / "workspaces"
            )
            state_path = first.directory / "generated-audio/generation-state.json"
            state = load_generation_state(state_path, first.directory / "queue.jsonl")
            publication = state["reviewed_waveform_publication"]
            workspace = json.loads(
                (first.directory / "workspace.json").read_text(encoding="utf-8")
            )
            pack_result = publish_final_game_pack(
                root / "pack",
                state_path=state_path,
                queue_path=first.directory / "queue.jsonl",
                story_index_path=first.directory / workspace["story_index"]["path"],
                voice_manifest_path=first.directory
                / workspace["voice_manifest"]["path"],
                game_id="reverse-1999",
                game_version="test",
                producers=[{"name": "test", "version": "1"}],
                created_at="2026-08-29T00:00:00+00:00",
            )
            pack = load_game_pack(pack_result.manifest)
            packed_voice = json.loads(
                (pack_result.directory / "voices/voice-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            base_state_after = (
                base / "generated-audio/generation-state.json"
            ).read_bytes()

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.directory, second.directory)
        self.assertEqual(base_state, base_state_after)
        self.assertEqual(len(publication["items"]), 1)
        ledger = publication["items"][0]
        self.assertEqual(ledger["queue_id"], queue_item.queue_id)
        self.assertEqual(ledger["route"]["status"], "not_reproducible")
        self.assertFalse(publication["synthesis_reproducibility"])
        authoring = pack.extensions["vntts.authoring"]
        self.assertEqual(
            authoring["reviewed_waveform_publication"]["approved_count"], 1
        )
        self.assertEqual(authoring["narrator_selection"]["character"], "Centurion")
        projection = authoring["voice_reference_projection"]
        self.assertEqual(projection["schema_version"], 1)
        self.assertEqual(len(projection["entries"]), 1)
        self.assertEqual(
            packed_voice["voices"][0]["references"],
            ["centurion.vntts-pcm16.wav"],
        )

    def test_tampering_and_partial_coverage_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, _queue_item = self._base(root / "source")
            result = create_reviewed_waveform_publication_workspace(
                base, root / "workspaces"
            )
            state_path = result.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["reviewed_waveform_publication"]["items"] = []
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with self.assertRaises(BulkGenerationError):
                load_generation_state(state_path, result.directory / "queue.jsonl")

            workspace_path = result.directory / "workspace.json"
            workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
            workspace["reviewed_waveform_publication"]["items"][0]["file_sha256"] = (
                "0" * 64
            )
            workspace_path.write_text(
                json.dumps(workspace, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaises(AuthoringWorkbenchError):
                load_workspace_authority(result.directory)


if __name__ == "__main__":
    unittest.main()

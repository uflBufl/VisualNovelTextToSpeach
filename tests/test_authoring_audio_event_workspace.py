import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

import vntts.authoring.workbench as workbench_module
from tests.test_authoring_audio_event_review import write_source_story
from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.audio_event_composition import (
    publish_audio_event_composition,
    record_audio_event_composition_decision,
)
from vntts.authoring.audio_event_review import (
    publish_source_audio_event_review,
    record_audio_event_review_decision,
)
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    publish_generated_manifest,
    review_generation_item,
)
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.config_rebase import rebase_workspace_config
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    create_audio_event_composition_workspace,
    create_resume_workspace,
    inspect_workspace,
    list_review_items,
)


class AudioEventWorkspaceTest(unittest.TestCase):
    def _base_and_composition(self, root, *, approve=True, outcome_merge=False):
        _fixture, _imported, created = create_test_workspace(root, text="Tsk!")
        base = created.directory
        queue_path = base / "queue.jsonl"
        queue_item = VoiceGenerationQueue.load(queue_path).items[0]
        state_path = base / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        result = state["items"][queue_item.queue_id]
        state["active"] = None
        result.update(
            {
                "status": "generated",
                "review_status": "rejected",
                "attempts_by_provider": {"moss-tts": result["attempts"]},
            }
        )
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        publish_generated_manifest(state_path)
        if outcome_merge:
            workspace_path = base / "workspace.json"
            workspace = json.loads(workspace_path.read_text())
            source_workspace_id = "resume-" + "a" * 24 + "-" + "b" * 16
            source_state_sha256 = "d" * 64
            source_item_sha256 = canonical_document_sha256(result)
            outcome_item = {
                "queue_id": queue_item.queue_id,
                "source_workspace_id": source_workspace_id,
                "source_state_sha256": source_state_sha256,
                "source_item_sha256": source_item_sha256,
                "audio_sha256": result["file_sha256"],
                "status": "generated",
                "review_status": "rejected",
            }
            outcome = {
                "schema": "vntts.authoring-workspace-outcome-merge",
                "schema_version": 1,
                "base_workspace_id": workspace["workspace_id"],
                "base_state_sha256": "e" * 64,
                "sources": [
                    {
                        "workspace_id": source_workspace_id,
                        "config_fingerprint": "c" * 64,
                        "state_sha256": source_state_sha256,
                        "terminal_item_count": 1,
                    }
                ],
                "items": [outcome_item],
            }
            result["outcome_merge"] = {
                key: value for key, value in outcome_item.items() if key != "queue_id"
            }
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            publish_generated_manifest(state_path)
            fingerprint = workbench_module._workspace_config_fingerprint(
                workspace["source"]["import_id"],
                workspace.get("story_index"),
                workspace.get("voice_manifest"),
                workspace["narrator_character"],
                workspace["run_config"],
                workspace.get("carry_forward"),
                outcome,
                workspace.get("failure_reference_binding"),
                workspace.get("terminal_conflict_merge"),
                workspace.get("config_rebase"),
                workspace.get("audio_event_composition"),
            )
            workspace["outcome_merge"] = outcome
            workspace["config_fingerprint"] = fingerprint
            workspace["workspace_id"] = (
                "resume-"
                + workspace["source"]["import_id"].removeprefix("legacy-")
                + "-"
                + fingerprint[:16]
            )
            workspace_path.write_text(json.dumps(workspace, sort_keys=True))
            renamed = base.with_name(workspace["workspace_id"])
            base.rename(renamed)
            base = renamed
            queue_path = base / "queue.jsonl"

        source_audio = root / "source-event.wav"
        samples = np.zeros(18_024, dtype=np.float32)
        samples[6_000:7_000] = 0.4
        write_pcm16_wav(source_audio, samples, 24_000)
        review = publish_source_audio_event_review(
            queue_path,
            queue_item.queue_id,
            write_source_story(root / "source-story.jsonl"),
            source_audio,
            root / "review",
            source_line_id="reverse1999:200308:6",
            source_speaker="Kanjira",
            source_event="play_activityvoc_hero3071_660",
            source_bank="activityvoc_hero3071molu1_3_part02.bnk",
            source_media_id=410389900,
            source_audio_id="610008734",
        )
        record_audio_event_review_decision(review.directory, "accept")
        composition = publish_audio_event_composition(
            review.directory, root / "composition"
        )
        if approve:
            composition = record_audio_event_composition_decision(
                composition.directory, "approved"
            )
        return base, queue_item, composition

    def test_preserves_overridden_outcome_merge_as_base_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, queue_item, composition = self._base_and_composition(
                root, outcome_merge=True
            )
            created = create_audio_event_composition_workspace(
                base, composition.directory, root / "successors"
            )
            summary = inspect_workspace(created.directory)
            workspace = json.loads((created.directory / "workspace.json").read_text())
            base_state = json.loads(
                (
                    created.directory / "inputs/audio-event-base/generation-state.json"
                ).read_text()
            )

            self.assertEqual(summary.generated, 1)
            self.assertEqual(workspace["audio_event_composition"]["schema_version"], 2)
            self.assertIn("outcome_merge", base_state["items"][queue_item.queue_id])

    def test_creates_exact_reviewable_successor_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, queue_item, composition = self._base_and_composition(root)
            state_path = base / "generated-audio/generation-state.json"
            base_state_before = state_path.read_bytes()
            base_audio = next((base / "generated-audio/audio").rglob("*.wav"))
            base_audio_before = base_audio.read_bytes()

            created = create_audio_event_composition_workspace(
                base, composition.directory, root / "successors"
            )
            repeated = create_audio_event_composition_workspace(
                base, composition.directory, root / "successors"
            )
            successor_state_path = (
                created.directory / "generated-audio/generation-state.json"
            )
            state = load_generation_state(
                successor_state_path, created.directory / "queue.jsonl"
            )
            result = state["items"][queue_item.queue_id]
            target = created.directory / "generated-audio" / result["path"]
            manifest = json.loads(
                (created.directory / "generated-audio/manifest.json").read_text()
            )

            self.assertTrue(created.created)
            self.assertFalse(repeated.created)
            self.assertEqual(created.directory, repeated.directory)
            self.assertEqual(state_path.read_bytes(), base_state_before)
            self.assertEqual(base_audio.read_bytes(), base_audio_before)
            self.assertEqual(result["status"], "generated")
            self.assertEqual(result["review_status"], "pending_review")
            self.assertEqual(state["schema"], "r1999.bulk-generation-state")
            self.assertEqual(result["provider"], "original-game-audio-event")
            self.assertFalse(
                result["audio_event_composition"]["speaker_identity_claim"]
            )
            self.assertEqual(target.read_bytes(), composition.audio.read_bytes())
            self.assertEqual(sha256_file(target), composition.audio_sha256)
            self.assertEqual(manifest["entry_count"], 0)
            self.assertEqual(inspect_workspace(created.directory).generated, 1)
            review_item = next(
                item
                for item in list_review_items(created.directory)
                if item.queue_id == queue_item.queue_id
            )
            self.assertEqual(review_item.voice_character, "Audio Event")
            self.assertIsNone(review_item.words_per_minute)
            self.assertEqual(review_item.technical_flags, ())

            review_generation_item(
                successor_state_path,
                queue_item.queue_id,
                "approved",
                queue_path=created.directory / "queue.jsonl",
            )
            approved_manifest = json.loads(
                (created.directory / "generated-audio/manifest.json").read_text()
            )
            self.assertEqual(approved_manifest["entry_count"], 1)
            self.assertEqual(
                approved_manifest["entries"][0]["audio_event_composition"],
                result["audio_event_composition"],
            )
            tampered_state = json.loads(successor_state_path.read_text())
            tampered_state["items"][queue_item.queue_id].pop("audio_event_composition")
            successor_state_path.write_text(json.dumps(tampered_state, sort_keys=True))
            with self.assertRaisesRegex(
                BulkGenerationError, "composition ledger changed"
            ):
                load_generation_state(
                    successor_state_path, created.directory / "queue.jsonl"
                )

    def test_config_rebase_carries_complete_composition_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, queue_item, composition = self._base_and_composition(root)
            source = create_audio_event_composition_workspace(
                base, composition.directory, root / "composition-workspaces"
            )
            review_generation_item(
                source.directory / "generated-audio/generation-state.json",
                queue_item.queue_id,
                "approved",
                queue_path=source.directory / "queue.jsonl",
            )
            source_workspace = json.loads(
                (source.directory / "workspace.json").read_text(encoding="utf-8")
            )
            imported = root / "imports" / source_workspace["source"]["import_id"]
            target = create_resume_workspace(
                imported,
                root / "target-workspaces",
                story_index=source.directory / "inputs/story-index.jsonl",
                voice_manifest=source.directory / "inputs/voice/manifest.json",
                backend=source_workspace["run_config"]["backend"],
                model=source_workspace["run_config"]["model"],
                generation_profile=source_workspace["run_config"]["generation_profile"],
                narrator_character=source_workspace["narrator_character"],
            )
            target_state_path = (
                target.directory / "generated-audio/generation-state.json"
            )
            target_state = json.loads(target_state_path.read_text(encoding="utf-8"))
            target_state["active"] = None
            target_state_path.write_text(
                json.dumps(target_state, sort_keys=True), encoding="utf-8"
            )

            rebased = rebase_workspace_config(
                source.directory, target.directory, root / "rebased-workspaces"
            )
            summary = inspect_workspace(rebased.directory)
            rebased_workspace = json.loads(
                (rebased.directory / "workspace.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary.approved, 1)
        self.assertEqual(
            rebased_workspace["audio_event_composition"],
            source_workspace["audio_event_composition"],
        )

    def test_rejects_unapproved_or_tampered_composition_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, _queue_item, composition = self._base_and_composition(
                root, approve=False
            )
            with self.assertRaisesRegex(AuthoringWorkbenchError, "approved"):
                create_audio_event_composition_workspace(
                    base, composition.directory, root / "unapproved"
                )
            record_audio_event_composition_decision(composition.directory, "approved")
            created = create_audio_event_composition_workspace(
                base, composition.directory, root / "successors"
            )
            decision = (
                created.directory
                / "inputs/audio-event-composition/composition-decision.json"
            )
            decision.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                AuthoringWorkbenchError,
                "changed|invalid|missing|required|Unsupported",
            ):
                inspect_workspace(created.directory)

    def test_rejects_base_mutation_before_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, _queue_item, composition = self._base_and_composition(root)
            state_path = base / "generated-audio/generation-state.json"
            original_copy = workbench_module._copy_workspace_tree_snapshot

            def copy_then_mutate(source, target, snapshots):
                original_copy(source, target, snapshots)
                if Path(source).resolve() == (base / "generated-audio").resolve():
                    document = json.loads(state_path.read_text())
                    document["test_only_race"] = True
                    state_path.write_text(json.dumps(document, sort_keys=True))

            with patch.object(
                workbench_module,
                "_copy_workspace_tree_snapshot",
                copy_then_mutate,
            ):
                with self.assertRaisesRegex(
                    AuthoringWorkbenchError,
                    "source changed before workspace publication",
                ):
                    create_audio_event_composition_workspace(
                        base, composition.directory, root / "successors"
                    )
            self.assertFalse(any((root / "successors").glob("resume-*")))

    def test_cli_creates_successor(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, _queue_item, composition = self._base_and_composition(root)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = authoring_main(
                    [
                        "audio-event-composition-workspace",
                        str(base),
                        str(composition.directory),
                        "--workspaces-root",
                        str(root / "successors"),
                    ]
                )
            result = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(result["created"])
            self.assertTrue(Path(result["workspace"]).is_dir())


if __name__ == "__main__":
    unittest.main()

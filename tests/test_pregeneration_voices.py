import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts import write_story_index_document

from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import (
    PregenerationVoiceError,
    VoiceDecisionStore,
    VoicePlanStore,
)
from vntts.settings import AppSettings


def write_content(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "story-index.jsonl"
    write_story_index_document(
        path,
        {
            "game": "Reverse: 1999",
            "language": "en",
            "collections": [
                {
                    "collection_id": "story",
                    "title": "Story",
                    "kind": "character-story",
                    "order": 1,
                }
            ],
        },
        [
            {
                "record_type": "line",
                "line_id": "line:original",
                "chapter": "1",
                "sequence": 1,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": "Already voiced.",
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "available",
                "speakable": True,
                "portrait": 10,
                "source_bank": "rhiannon.bnk",
            },
            {
                "record_type": "line",
                "line_id": "line:rhiannon:1",
                "chapter": "1",
                "sequence": 2,
                "speaker": "Aderyn",
                "voice_character": "Rhiannon",
                "text": "This is the most useful preview sentence for my voice.",
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "absent",
                "speakable": True,
                "portrait": 10,
                "source_bank": "rhiannon.bnk",
            },
            {
                "record_type": "line",
                "line_id": "line:rhiannon:2",
                "chapter": "1",
                "sequence": 3,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": "Short.",
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "absent",
                "speakable": True,
                "portrait": 10,
                "source_bank": "rhiannon.bnk",
            },
            {
                "record_type": "line",
                "line_id": "line:unknown",
                "chapter": "1",
                "sequence": 4,
                "speaker": "Hotelier",
                "voice_character": "Hotelier",
                "text": "A one-off role.",
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "absent",
                "speakable": True,
                "portrait": 20,
                "source_bank": "hotel.bnk",
            },
            {
                "record_type": "line",
                "line_id": "line:unattributed",
                "chapter": "1",
                "sequence": 5,
                "speaker": "???",
                "voice_character": "Someone",
                "text": "Who am I?",
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "absent",
                "speakable": True,
            },
        ],
    )
    return path


def write_manifest(root, *, rhiannon=b"rhiannon", unrelated=b"unrelated"):
    references = root / "references"
    references.mkdir(parents=True, exist_ok=True)
    (references / "rhiannon.wav").write_bytes(rhiannon)
    (references / "centurion.wav").write_bytes(b"centurion")
    (references / "unrelated.wav").write_bytes(unrelated)
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "voices": [
                    {
                        "character": "Rhiannon",
                        "speaker": "rhiannon-v1",
                        "aliases": ["Aderyn"],
                        "references": ["references/rhiannon.wav"],
                    },
                    {
                        "character": "Centurion",
                        "speaker": "centurion-v1",
                        "aliases": [],
                        "references": ["references/centurion.wav"],
                    },
                    {
                        "character": "Unrelated",
                        "speaker": "unrelated-v1",
                        "aliases": [],
                        "references": ["references/unrelated.wav"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class VoicePlanStoreTest(unittest.TestCase):
    def create_fixture(self, root):
        content = inspect_story_index(write_content(root / "content"))
        jobs = PregenerationJobStore(root / "jobs")
        job = jobs.create_or_resume(content, ("story",))
        return job, jobs

    def test_source_audio_is_excluded_and_lines_are_grouped_by_voice_variant(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices")

            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(),
                manifest_path=manifest,
            )

            self.assertEqual(plan.generation_line_count, 4)
            self.assertEqual(len(plan.groups), 3)
            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(
                rhiannon.line_ids,
                ("line:rhiannon:1", "line:rhiannon:2"),
            )
            self.assertEqual(rhiannon.route, "voice")
            self.assertEqual(rhiannon.resolution, "known-character-voice")
            self.assertEqual(rhiannon.source_character, "Rhiannon")
            self.assertEqual(rhiannon.portrait, "10")
            self.assertNotIn("line:original", rhiannon.line_ids)
            self.assertTrue(VoicePlanStore(jobs).path_for(job).is_file())

    def test_missing_named_role_and_unattributed_role_use_narrator_without_review(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(),
                manifest_path=write_manifest(root / "voices"),
            )

            hotelier = next(
                group for group in plan.groups if group.character == "Hotelier"
            )
            unknown = next(
                group for group in plan.groups if group.character == "Narrator"
            )
            self.assertEqual(hotelier.route, "narrator")
            self.assertEqual(hotelier.resolution, "automatic-narrator-fallback")
            self.assertEqual(unknown.resolution, "narrator-dialogue")
            self.assertEqual(plan.audition_count, 0)
            self.assertEqual(plan.synthesis_profile, "default")

    def test_saved_alias_assignment_is_reused_automatically(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(voice_assignments={"Rhiannon": "character:centurion"}),
                manifest_path=write_manifest(root / "voices"),
            )

            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(rhiannon.source_character, "Centurion")
            self.assertEqual(rhiannon.resolution, "saved-voice-assignment")

    def test_unrelated_reference_change_does_not_invalidate_other_group_controls(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices", unrelated=b"first")
            store = VoicePlanStore(jobs)
            first = store.create(job, AppSettings(), manifest_path=manifest)
            first_group = next(
                group for group in first.groups if group.character == "Rhiannon"
            )

            manifest = write_manifest(root / "voices", unrelated=b"changed")
            second = store.create(job, AppSettings(), manifest_path=manifest)
            second_group = next(
                group for group in second.groups if group.character == "Rhiannon"
            )

            self.assertEqual(first_group.control_sha256, second_group.control_sha256)
            self.assertEqual(
                first_group.decision_context_sha256,
                second_group.decision_context_sha256,
            )

    def test_selected_reference_or_backend_change_invalidates_only_affected_control(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices", rhiannon=b"first")
            store = VoicePlanStore(jobs)
            first = store.create(job, AppSettings(), manifest_path=manifest)
            first_groups = {group.character: group for group in first.groups}

            manifest = write_manifest(root / "voices", rhiannon=b"changed")
            second = store.create(job, AppSettings(), manifest_path=manifest)
            second_groups = {group.character: group for group in second.groups}

            self.assertNotEqual(
                first_groups["Rhiannon"].control_sha256,
                second_groups["Rhiannon"].control_sha256,
            )
            self.assertEqual(
                first_groups["Hotelier"].control_sha256,
                second_groups["Hotelier"].control_sha256,
            )
            third = store.create(
                job,
                AppSettings(speech_backend="moss-tts"),
                manifest_path=manifest,
            )
            self.assertNotEqual(
                second.synthesis_controls_sha256,
                third.synthesis_controls_sha256,
            )

    def test_story_change_is_rejected_before_plan_publication(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            Path(job.story_index).write_text("changed", encoding="utf-8")

            with self.assertRaisesRegex(PregenerationVoiceError, "changed"):
                VoicePlanStore(jobs).create(job, AppSettings())

            self.assertFalse(VoicePlanStore(jobs).path_for(job).exists())

    def test_decision_store_rejects_non_audition_routes(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            plan = VoicePlanStore(jobs).create(job, AppSettings())
            decisions = VoiceDecisionStore(root / "decisions.json")

            with self.assertRaisesRegex(PregenerationVoiceError, "unresolved"):
                decisions.remember(plan.groups[0], "default")


if __name__ == "__main__":
    unittest.main()

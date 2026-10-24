import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import (
    expected_voice_generation_queue_id,
    text_sha256,
)

from tests.voice_manifest_fixtures import write_conflicting_manifest
from vntts.authoring.source_reference_bindings import (
    SOURCE_REFERENCE_BINDINGS_FIELD,
    queue_voice_overrides_sha256,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import (
    VoiceDecisionStore,
    VoicePlanStore,
)
from vntts.settings import AppSettings
from vntts.voice_library import VoiceLibrary
from vntts.voices import VoiceManifestError


class CanonicalVoicePlanTest(unittest.TestCase):
    def fixture(self, root):
        story = root / "story-index.jsonl"
        records = [
            {
                "record_type": "line",
                "line_id": line_id,
                "chapter": collection,
                "collection_id": collection,
                "sequence": index,
                "speaker": role,
                "voice_character": role,
                "text": text,
                "kind": "dialogue",
                "speakable": True,
                "source_audio_status": "absent",
                "portrait": index + 10,
                "source_bank": f"rhiannon-{index}.bnk",
            }
            for index, (line_id, collection, role, text) in enumerate(
                (
                    (
                        "line:rhiannon:1",
                        "first",
                        "Rhiannon",
                        "This is the most useful preview sentence for my voice.",
                    ),
                    ("line:rhiannon:2", "second", "Aderyn", "Short."),
                    ("line:rhiannon:3", "second", "Rhiannon", "A third line."),
                    ("line:other", "second", "Hotelier", "Another character."),
                ),
                1,
            )
        ]
        write_story_index_document(
            story,
            {
                "game": "Synthetic Game",
                "language": "en",
                "collections": [
                    {
                        "collection_id": name,
                        "title": name.title(),
                        "kind": "character-story",
                        "order": index,
                    }
                    for index, name in enumerate(("first", "second"), 1)
                ],
            },
            records,
        )
        manifest = write_conflicting_manifest(root / "voices")
        document = json.loads(manifest.read_text())
        bindings = document[SOURCE_REFERENCE_BINDINGS_FIELD]
        overrides = {}
        for record, variant in zip(
            records[:2], bindings["selected_variants"], strict=True
        ):
            queue_id = expected_voice_generation_queue_id(
                record["line_id"], text_sha256(record["text"])
            )
            variant["queue_ids"] = [queue_id]
            overrides[queue_id] = variant["voice_character"]
        bindings["queue_voice_overrides"] = dict(sorted(overrides.items()))
        bindings["queue_voice_overrides_sha256"] = queue_voice_overrides_sha256(
            overrides
        )
        manifest.write_text(json.dumps(document))
        library = VoiceLibrary(root / "library")
        library.link_person("Rhiannon", "Aderyn")
        decisions = VoiceDecisionStore(root / "decisions.json", voice_library=library)
        jobs = PregenerationJobStore(root / "jobs")
        store = VoicePlanStore(jobs, decisions=decisions, voice_library=library)
        settings = AppSettings(pocket_gated_model_accepted=True)
        return (
            inspect_story_index(story),
            jobs,
            manifest,
            library,
            decisions,
            store,
            settings,
        )

    def test_two_stories_and_conflicting_line_bindings_have_one_character_group(self):
        with TemporaryDirectory() as directory:
            content, jobs, manifest, _library, _decisions, store, settings = (
                self.fixture(Path(directory))
            )
            job = jobs.create_or_resume(content, ("first", "second"))
            plan = store.create(job, settings, manifest_path=manifest)
            groups = [group for group in plan.groups if group.character == "Rhiannon"]

            self.assertEqual(len(groups), 1)
            group = groups[0]
            self.assertEqual(group.routing_role, "Rhiannon")
            self.assertIsNone(group.variant_key)
            self.assertEqual(
                set(group.line_ids),
                {"line:rhiannon:1", "line:rhiannon:2", "line:rhiannon:3"},
            )
            self.assertEqual(set(group.speakers), {"Rhiannon", "Aderyn"})
            self.assertIsNone(group.source_bank)
            self.assertEqual(
                {candidate.source_speaker for candidate in group.candidate_inventory},
                {"rhiannon-v1", "adult-v1", "child-v1"},
            )

    def test_saved_canonical_or_alias_choice_is_reused_in_another_story_job(self):
        for role in ("Rhiannon", "Aderyn"):
            with self.subTest(role=role), TemporaryDirectory() as directory:
                root = Path(directory)
                content, jobs, manifest, library, decisions, store, settings = (
                    self.fixture(root)
                )
                first_job = jobs.create_or_resume(content, ("first",))
                first = store.create(first_job, settings, manifest_path=manifest)
                group = first.groups[0]
                candidate = next(
                    value
                    for value in group.candidate_inventory
                    if value.source_speaker == "child-v1"
                )
                selected = replace(group, character=role, routing_role=role)
                decisions.remember(selected, candidate.source_id)

                reloaded = VoiceLibrary(library.root)
                fresh_store = VoicePlanStore(
                    jobs,
                    decisions=VoiceDecisionStore(
                        decisions.path, voice_library=reloaded
                    ),
                    voice_library=reloaded,
                )
                second_job = jobs.create_or_resume(content, ("second",))
                second = fresh_store.create(
                    second_job, settings, manifest_path=manifest
                )
                future = next(
                    group for group in second.groups if group.character == "Rhiannon"
                )

                self.assertEqual(future.source_speaker, candidate.source_speaker)
                self.assertEqual(future.reference_sha256s, candidate.reference_sha256s)
                self.assertEqual(
                    set(future.line_ids), {"line:rhiannon:2", "line:rhiannon:3"}
                )
                self.assertEqual(
                    reloaded.binding("Aderyn"), reloaded.binding("Rhiannon")
                )
                self.assertEqual(reloaded.binding(role).provenance["method"], "manual")

    def test_unavailable_archived_source_is_optional_but_selected_source_is_strict(
        self,
    ):
        with TemporaryDirectory() as directory:
            content, jobs, manifest, library, _decisions, store, settings = (
                self.fixture(Path(directory))
            )
            library.unlink_person("Aderyn")
            library.select("Aderyn", route="voice", source_id="character:missing")
            library.select("Rhiannon", route="voice", source_id="preset:alba")
            library.link_person("Rhiannon", "Aderyn")
            job = jobs.create_or_resume(content, ("first", "second"))

            plan = store.create(job, settings, manifest_path=manifest)

            group = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(group.source_id, "preset:alba")
            self.assertNotIn(
                "character:missing",
                {candidate.source_id for candidate in group.candidate_inventory},
            )
            self.assertEqual(library.binding("Aderyn").source_id, "preset:alba")
            self.assertEqual(len(library.archived_bindings("Rhiannon")), 1)

            library.select("Rhiannon", route="voice", source_id="character:missing")
            with self.assertRaisesRegex(VoiceManifestError, "missing"):
                store.create(job, settings, manifest_path=manifest)

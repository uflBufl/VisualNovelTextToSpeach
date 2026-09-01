import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from vntts_artifacts import write_story_index_document
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    expected_voice_generation_queue_id,
    text_sha256,
)

from vntts.authoring.source_reference_bindings import (
    SOURCE_REFERENCE_BINDINGS_FIELD,
    SOURCE_REFERENCE_BINDINGS_SCHEMA,
    SOURCE_REFERENCE_BINDINGS_VERSION,
    queue_voice_overrides_sha256,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import (
    PLAYER_VOICE_CANDIDATES_FIELD,
    PregenerationVoiceError,
    VoiceDecisionStore,
    VoicePlanStore,
    resolve_pregeneration_settings,
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


def write_conflicting_manifest(root, *, bind_selected_lines=False):
    references = root / "references"
    references.mkdir(parents=True, exist_ok=True)
    payloads = {
        "rhiannon.wav": b"rhiannon",
        "adult.wav": b"adult",
        "child.wav": b"child",
    }
    for name, payload in payloads.items():
        (references / name).write_bytes(payload)
    adult_voice = "Source reference Rhiannon adult"
    child_voice = "Source reference Rhiannon child"
    adult_queue_ids = ["historical:adult"]
    if bind_selected_lines:
        adult_queue_ids = [
            expected_voice_generation_queue_id(
                line_id,
                text_sha256(text),
            )
            for line_id, text in (
                (
                    "line:rhiannon:1",
                    "This is the most useful preview sentence for my voice.",
                ),
                ("line:rhiannon:2", "Short."),
            )
        ]
    overrides = {
        **{queue_id: adult_voice for queue_id in adult_queue_ids},
        "historical:child": child_voice,
    }
    variants = [
        {
            "variant_id": "a" * 64,
            "cluster_id": "adult-cluster",
            "character": "Rhiannon",
            "portrait": "10",
            "source_bank": "rhiannon.bnk",
            "voice_character": adult_voice,
            "reference_sha256": "b" * 64,
            "queue_ids": adult_queue_ids,
        },
        {
            "variant_id": "c" * 64,
            "cluster_id": "child-cluster",
            "character": "Rhiannon",
            "portrait": "11",
            "source_bank": "rhiannon-child.bnk",
            "voice_character": child_voice,
            "reference_sha256": "d" * 64,
            "queue_ids": ["historical:child"],
        },
    ]
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
                        "character": adult_voice,
                        "speaker": "adult-v1",
                        "aliases": [],
                        "references": ["references/adult.wav"],
                    },
                    {
                        "character": child_voice,
                        "speaker": "child-v1",
                        "aliases": [],
                        "references": ["references/child.wav"],
                    },
                ],
                SOURCE_REFERENCE_BINDINGS_FIELD: {
                    "schema": SOURCE_REFERENCE_BINDINGS_SCHEMA,
                    "schema_version": SOURCE_REFERENCE_BINDINGS_VERSION,
                    "source_reference_plan_sha256": "e" * 64,
                    "selected_variants": variants,
                    "queue_voice_overrides": dict(sorted(overrides.items())),
                    "queue_voice_overrides_sha256": queue_voice_overrides_sha256(
                        overrides
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def write_player_candidate_manifest(
    root,
    story_index_sha256,
    *,
    portrait_image_sha256=None,
    quality_scores=(99, 98),
):
    references = root / "references"
    references.mkdir(parents=True, exist_ok=True)
    report = root / "report.json"
    report.write_text('{"candidate_count":2}', encoding="utf-8")
    voices = []
    variants = []
    for index, quality_score in enumerate(quality_scores, start=1):
        reference = references / f"rhiannon-{index}.wav"
        reference.write_bytes(f"rhiannon-{index}".encode())
        variant_id = str(index) * 64
        voice_character = f"Player candidate Rhiannon {index}"
        voices.append(
            {
                "character": voice_character,
                "speaker": f"player-candidate:{variant_id}",
                "references": [f"references/rhiannon-{index}.wav"],
            }
        )
        variants.append(
            {
                "variant_id": variant_id,
                "character": "Rhiannon",
                "portrait": "10",
                "portrait_image_sha256": portrait_image_sha256,
                "source_bank": "rhiannon.bnk",
                "source_voice_ids": [f"play_rhiannon_{index}"],
                "voice_character": voice_character,
                "reference_sha256": sha256_file(reference),
                "source_line_ids": [f"line:source:{index}"],
                "source_event_ids": [index],
                "duration_seconds": 3.0 + index,
                "quality_score": quality_score,
            }
        )
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "voices": voices,
                PLAYER_VOICE_CANDIDATES_FIELD: {
                    "schema": "vntts.player-voice-candidates",
                    "schema_version": 2,
                    "story_index_sha256": story_index_sha256,
                    "candidate_report": report.name,
                    "candidate_report_sha256": sha256_file(report),
                    "variants": variants,
                },
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

    def test_self_service_uses_a_supported_backend_and_profile(self):
        unsupported_moss = resolve_pregeneration_settings(
            AppSettings(
                speech_backend="moss-tts",
                tts_model="local-moss",
                tts_profile="stable",
            ),
            platform_name="win32",
            machine="AMD64",
        )
        invalid_profile = resolve_pregeneration_settings(
            AppSettings(speech_backend="coqui-xtts", tts_profile="obsolete"),
            platform_name="linux",
            machine="x86_64",
        )

        self.assertEqual(unsupported_moss.speech_backend, "pocket-tts")
        self.assertIsNone(unsupported_moss.tts_model)
        self.assertEqual(unsupported_moss.tts_profile, "default")
        self.assertEqual(invalid_profile.speech_backend, "coqui-xtts")
        self.assertEqual(invalid_profile.tts_profile, "stable")

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
            self.assertEqual(len(rhiannon.candidates), 1)
            self.assertEqual(
                rhiannon.candidates[0].source_id,
                "character:rhiannon",
            )
            self.assertEqual(
                rhiannon.candidates[0].reference_sha256s,
                rhiannon.reference_sha256s,
            )
            self.assertEqual(rhiannon.portrait, "10")
            self.assertEqual(
                rhiannon.sample_text,
                "This is the most useful preview sentence for my voice.",
            )
            self.assertEqual(rhiannon.alternate_sample_text, "Short.")
            self.assertNotIn("line:original", rhiannon.line_ids)
            self.assertTrue(VoicePlanStore(jobs).path_for(job).is_file())

    def test_missing_named_role_and_unattributed_role_use_narrator_without_review(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices")
            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(),
                manifest_path=manifest,
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

    def test_close_variant_evidence_creates_one_informed_audition(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)

            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(),
                manifest_path=write_conflicting_manifest(root / "voices"),
            )

            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(rhiannon.route, "needs-audition")
            self.assertEqual(rhiannon.resolution, "ambiguous-voice-evidence")
            self.assertEqual(plan.audition_count, 1)
            self.assertEqual(len(rhiannon.candidate_inventory), 3)
            self.assertEqual(len(rhiannon.candidates), 2)
            self.assertEqual(rhiannon.candidates[0].match_score, 100)
            self.assertEqual(
                rhiannon.candidates[0].recommendation,
                "Same character portrait and original voice bank",
            )
            self.assertEqual(rhiannon.candidates[1].source_character, "Rhiannon")
            self.assertEqual(
                rhiannon.anchor_source_id,
                rhiannon.candidates[0].source_id,
            )
            self.assertNotIn(
                "Source reference Rhiannon child",
                {candidate.source_character for candidate in rhiannon.candidates},
            )

    def test_player_import_candidates_reach_the_same_bounded_audition(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_player_candidate_manifest(
                root / "player-voices",
                job.story_index_sha256,
            )

            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(),
                manifest_path=manifest,
            )

            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(rhiannon.route, "needs-audition")
            self.assertEqual(len(rhiannon.candidates), 2)
            self.assertEqual(rhiannon.candidates[0].source_bank, "rhiannon.bnk")
            self.assertEqual(
                rhiannon.candidates[0].source_line_ids,
                ("line:source:1",),
            )
            self.assertEqual(
                rhiannon.candidates[0].source_voice_ids,
                ("play_rhiannon_1",),
            )

    def test_player_audition_keeps_only_three_best_equal_evidence_clips(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_player_candidate_manifest(
                root / "player-voices",
                job.story_index_sha256,
                quality_scores=(60, 100, 80, 70, 90),
            )

            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(),
                manifest_path=manifest,
            )

            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(len(rhiannon.candidate_inventory), 5)
            self.assertEqual(
                [candidate.source_character for candidate in rhiannon.candidates],
                [
                    "Player candidate Rhiannon 2",
                    "Player candidate Rhiannon 5",
                    "Player candidate Rhiannon 3",
                ],
            )

    def test_player_import_candidates_reject_another_story_index(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_player_candidate_manifest(
                root / "player-voices",
                "0" * 64,
            )

            with self.assertRaisesRegex(
                PregenerationVoiceError,
                "Player voice candidate evidence is invalid",
            ):
                VoicePlanStore(jobs).create(
                    job,
                    AppSettings(),
                    manifest_path=manifest,
                )

    def test_player_import_candidate_portrait_is_checksum_bound(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            portraits = Path(job.story_index).parent / "portraits"
            portraits.mkdir()
            portrait = portraits / "10.png"
            Image.new("RGB", (32, 32), "purple").save(portrait)
            manifest = write_player_candidate_manifest(
                root / "player-voices",
                job.story_index_sha256,
                portrait_image_sha256=sha256_file(portrait),
            )

            VoicePlanStore(jobs).create(
                job,
                AppSettings(),
                manifest_path=manifest,
            )
            portrait.write_bytes(b"changed")

            with self.assertRaisesRegex(
                PregenerationVoiceError,
                "portrait changed",
            ):
                VoicePlanStore(jobs).create(
                    job,
                    AppSettings(),
                    manifest_path=manifest,
                )

    def test_exact_queue_voice_binding_wins_without_prompt(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)

            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(),
                manifest_path=write_conflicting_manifest(
                    root / "voices",
                    bind_selected_lines=True,
                ),
            )

            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(rhiannon.route, "voice")
            self.assertEqual(rhiannon.resolution, "exact-source-voice-binding")
            self.assertEqual(
                rhiannon.source_character,
                "Source reference Rhiannon adult",
            )
            self.assertEqual(plan.audition_count, 0)

    def test_saved_ambiguous_choice_is_reused_without_another_prompt(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_conflicting_manifest(root / "voices")
            decisions = VoiceDecisionStore(root / "decisions.json")
            store = VoicePlanStore(jobs, decisions=decisions)
            first = store.create(job, AppSettings(), manifest_path=manifest)
            group = next(
                value for value in first.groups if value.character == "Rhiannon"
            )

            decisions.remember(group, group.candidates[1].source_id)
            second = store.create(job, AppSettings(), manifest_path=manifest)
            resolved = next(
                value for value in second.groups if value.character == "Rhiannon"
            )

            self.assertEqual(resolved.route, "voice")
            self.assertEqual(resolved.resolution, "saved-player-decision")
            self.assertEqual(resolved.source_id, group.candidates[1].source_id)
            self.assertEqual(second.audition_count, 0)

            reconsidered = store.create(
                job,
                AppSettings(),
                manifest_path=manifest,
                ignore_decisions=True,
            )
            reopened = next(
                value for value in reconsidered.groups if value.character == "Rhiannon"
            )
            self.assertEqual(reopened.route, "needs-audition")
            self.assertEqual(reconsidered.audition_count, 1)

    def test_changed_dominated_candidate_does_not_repeat_a_saved_choice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_conflicting_manifest(root / "voices")
            decisions = VoiceDecisionStore(root / "decisions.json")
            store = VoicePlanStore(jobs, decisions=decisions)
            first = store.create(job, AppSettings(), manifest_path=manifest)
            group = next(
                value for value in first.groups if value.character == "Rhiannon"
            )
            dominated_before = group.candidate_inventory[-1].reference_sha256s
            decisions.remember(group, group.candidates[0].source_id)

            child_reference = manifest.parent / "references" / "child.wav"
            child_reference.write_bytes(b"changed dominated child reference")
            second = store.create(job, AppSettings(), manifest_path=manifest)
            resolved = next(
                value for value in second.groups if value.character == "Rhiannon"
            )

            self.assertNotEqual(
                dominated_before,
                resolved.candidate_inventory[-1].reference_sha256s,
            )
            self.assertEqual(resolved.route, "voice")
            self.assertEqual(resolved.resolution, "saved-player-decision")
            self.assertEqual(resolved.source_id, group.candidates[0].source_id)
            self.assertEqual(second.audition_count, 0)

    def test_exact_installed_portrait_is_checksum_bound_for_the_comparison(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            portraits = root / "content" / "portraits"
            portraits.mkdir()
            portrait = portraits / "10.png"
            Image.new("RGB", (32, 32), "purple").save(portrait)

            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(),
                manifest_path=write_conflicting_manifest(root / "voices"),
            )
            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )

            self.assertEqual(rhiannon.portrait_image, str(portrait.resolve()))
            self.assertEqual(rhiannon.portrait_image_sha256, sha256_file(portrait))

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

    def test_decision_store_accepts_only_bound_candidate_or_narrator(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices")
            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(),
                manifest_path=manifest,
            )
            selected = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            group = replace(selected, route="needs-audition")
            decisions = VoiceDecisionStore(root / "decisions.json")

            with self.assertRaisesRegex(PregenerationVoiceError, "not a candidate"):
                decisions.remember(group, "character:unrelated")

            decisions.remember(group, group.candidates[0].source_id)
            reloaded = VoiceDecisionStore(root / "decisions.json")
            self.assertEqual(
                reloaded.choice_for(group.group_id, group.decision_context_sha256),
                group.candidates[0].source_id,
            )
            reused = VoicePlanStore(jobs, decisions=reloaded).create(
                job,
                AppSettings(),
                manifest_path=manifest,
            )
            reused_group = next(
                value for value in reused.groups if value.character == "Rhiannon"
            )
            self.assertEqual(reused_group.resolution, "saved-player-decision")
            self.assertEqual(reused_group.source_id, group.candidates[0].source_id)

            decisions.remember(group, "default")
            self.assertEqual(
                reloaded.choice_for(group.group_id, group.decision_context_sha256),
                "default",
            )


if __name__ == "__main__":
    unittest.main()

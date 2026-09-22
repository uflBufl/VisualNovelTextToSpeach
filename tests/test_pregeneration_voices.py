import hashlib
import json
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
from vntts_artifacts import write_story_index_document
from vntts_artifacts.atomic_io import atomic_write_json
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
from vntts.document_identity import canonical_document_sha256
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import (
    PLAYER_VOICE_CANDIDATES_FIELD,
    PregenerationVoiceError,
    VoiceDecisionStore,
    VoicePlanStore,
    resolve_pregeneration_settings,
)
from vntts.settings import AppSettings
from vntts.source_audio_semantics import (
    SEMANTIC_EVIDENCE_METHOD,
    semantic_text_sha256,
)
from vntts.voice_library import VoiceLibrary
from vntts.voices import (
    CharacterVoiceRegistry,
    remember_voice_binding,
    voice_binding_source_id,
)


def write_reference(path, payload):
    if payload.startswith(b"RIFF"):
        path.write_bytes(payload)
        return
    frames = (payload * ((32_000 // len(payload)) + 1))[:32_000]
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        audio.writeframes(frames)


def write_content(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "story-index.jsonl"
    line_id = "line:original"
    text = "Already voiced."
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    media_hash = "a" * 64
    entry = {
        "locale": "en",
        "media_id": 7,
        "media_sha256": media_hash,
        "displayed_text_sha256": text_hash,
        "normalized_displayed_text_sha256": semantic_text_sha256(text),
        "observed_transcript": text,
        "normalized_observed_text_sha256": semantic_text_sha256(text),
        "verdict": "full",
        "reason": "exact-normalized-asr-transcript",
        "method": SEMANTIC_EVIDENCE_METHOD,
        "model_sha256": "b" * 64,
        "source_line_ids": [line_id],
    }
    entry["entry_id"] = canonical_document_sha256(
        {key: value for key, value in entry.items() if key != "source_line_ids"}
    )
    evidence = {
        "schema": "r1999.source-audio-semantic-evidence",
        "schema_version": 1,
        "locale": "en",
        "source_story_index_sha256": "c" * 64,
        "model": {
            "kind": "whisper",
            "snapshot": "synthetic",
            "sha256": "b" * 64,
            "device": "cpu",
            "decoding": "deterministic_greedy_default",
        },
        "entries": [entry],
    }
    evidence["evidence_id"] = canonical_document_sha256(evidence)
    evidence["generated_at"] = "2026-09-14T00:00:00+00:00"
    evidence_path = root / "source-audio-semantic-evidence.json"
    atomic_write_json(evidence_path, evidence, sort_keys=True)
    write_story_index_document(
        path,
        {
            "game": "Reverse: 1999",
            "language": "en",
            "source_audio_completion": "verified-media-duration-seconds",
            "source_audio_semantics": {
                "evidence_id": evidence["evidence_id"],
                "evidence_sha256": sha256_file(evidence_path),
                "method": SEMANTIC_EVIDENCE_METHOD,
                "selected_chapters": ["1"],
                "applied_count": 1,
            },
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
                "line_id": line_id,
                "chapter": "1",
                "sequence": 1,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": text,
                "text_sha256": text_hash,
                "kind": "dialogue",
                "collection_id": "story",
                "source_audio_status": "available",
                "source_audio_duration_seconds": 1.0,
                "source_audio_duration_media_id": 7,
                "source_audio_duration_media_sha256": media_hash,
                "source_audio_duration_sample_rate": 24000,
                "source_audio_duration_sample_count": 24000,
                "source_audio_duration_decoder": "synthetic",
                "source_media_ids": [7],
                "available_media_ids": [7],
                "source_audio_completeness": "full",
                "source_audio_completeness_reason": ("exact-normalized-asr-transcript"),
                "source_audio_semantic_evidence_id": evidence["evidence_id"],
                "source_audio_semantic_evidence_entry_id": entry["entry_id"],
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
    write_reference(references / "rhiannon.wav", rhiannon)
    write_reference(references / "centurion.wav", b"centurion")
    write_reference(references / "unrelated.wav", unrelated)
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
        write_reference(references / name, payload)
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
        with wave.open(str(reference), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes((index * 100).to_bytes(2, "little", signed=True) * 1_600)
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
    def test_planning_persists_one_binding_and_rediscovery_keeps_it(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices")
            for reference in ("rhiannon.wav", "centurion.wav"):
                with wave.open(
                    str(manifest.parent / "references" / reference), "wb"
                ) as audio:
                    audio.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
                    audio.writeframes(b"\x00\x00" * 24_000)
            library = VoiceLibrary(root / "library")
            planner = VoicePlanStore(jobs, voice_library=library)

            first = planner.create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
                manifest_path=manifest,
            )
            first_group = next(
                group for group in first.groups if group.character == "Rhiannon"
            )
            binding = library.binding("Rhiannon")
            second = planner.create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
                manifest_path=manifest,
            )
            second_group = next(
                group for group in second.groups if group.character == "Rhiannon"
            )

            self.assertEqual(binding.route, "voice")
            self.assertEqual(library.binding("Rhiannon"), binding)
            self.assertEqual(second_group.group_id, first_group.group_id)

    def create_fixture(self, root):
        content = inspect_story_index(write_content(root / "content"))
        jobs = PregenerationJobStore(root / "jobs")
        job = jobs.create_or_resume(content, ("story",))
        return job, jobs

    def test_voice_plan_records_bounded_phase_timings(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            with patch(
                "vntts.pregeneration_voices.record_background_operation"
            ) as record:
                VoicePlanStore(jobs).create(
                    job,
                    AppSettings(pocket_gated_model_accepted=True),
                    manifest_path=write_manifest(root / "voices"),
                )

            self.assertEqual(
                [call.args[0] for call in record.call_args_list],
                [
                    "pregeneration-voice-plan-story",
                    "pregeneration-voice-plan-voice-inventory",
                    "pregeneration-voice-plan-routing",
                    "pregeneration-voice-plan-write",
                ],
            )
            self.assertTrue(
                all("cpu_ms" in call.kwargs for call in record.call_args_list)
            )

    def test_self_service_preserves_selected_backend_and_normalizes_profile(self):
        with patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=False):
            unsupported_moss = resolve_pregeneration_settings(
                AppSettings(
                    speech_backend="moss-tts",
                    tts_model="local-moss",
                    tts_profile="stable",
                ),
            )
        invalid_profile = resolve_pregeneration_settings(
            AppSettings(speech_backend="coqui-xtts", tts_profile="obsolete"),
        )

        self.assertEqual(unsupported_moss.speech_backend, "moss-tts")
        self.assertEqual(unsupported_moss.tts_model, "local-moss")
        self.assertEqual(unsupported_moss.tts_profile, "stable")
        self.assertEqual(invalid_profile.speech_backend, "coqui-xtts")
        self.assertEqual(invalid_profile.tts_profile, "stable")

    def test_self_service_drops_mlx_model_when_openmoss_is_required(self):
        with patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True):
            settings = resolve_pregeneration_settings(
                AppSettings(
                    speech_backend="moss-tts",
                    tts_model="shraey/MOSS-TTS-Local-Transformer-v1.5-MLX-int8",
                )
            )

        self.assertEqual(settings.speech_backend, "moss-tts")
        self.assertIsNone(settings.tts_model)

    def test_metadata_does_not_change_voice_groups_or_selection(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_conflicting_manifest(root / "voices")
            library = VoiceLibrary(root / "library")
            planner = VoicePlanStore(jobs, voice_library=library)
            settings = AppSettings(pocket_gated_model_accepted=True)
            original = planner.create(job, settings, manifest_path=manifest)
            original_group = next(
                group for group in original.groups if group.character == "Rhiannon"
            )
            self.assertNotIn("age", original_group.to_document())
            path = Path(job.story_index)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            first = next(row for row in rows if row.get("line_id") == "line:rhiannon:1")
            second = next(
                row for row in rows if row.get("line_id") == "line:rhiannon:2"
            )
            first.update(source_bank="other.bnk", source_voice_id="audio-1")
            second["source_voice_id"] = "audio-2"
            for different_portrait in (False, True):
                if different_portrait:
                    second["portrait"] = 11
                path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                changed_job = replace(job, story_index_sha256=sha256_file(path))
                plan = planner.create(changed_job, settings, manifest_path=manifest)
                groups = [
                    group for group in plan.groups if group.character == "Rhiannon"
                ]
                self.assertEqual(len(groups), 1)
                self.assertEqual(
                    plan.generation_line_count, original.generation_line_count
                )
                group = groups[0]
                self.assertEqual(group.group_id, original_group.group_id)
                self.assertEqual(group.line_ids, original_group.line_ids)
                self.assertIsNone(group.source_bank)
                self.assertIsNone(group.source_voice_id)
                self.assertEqual(group.candidates, original_group.candidates)
                self.assertEqual(group.source_id, original_group.source_id)
                self.assertEqual(
                    group.decision_context_sha256,
                    original_group.decision_context_sha256,
                )
            decisions = VoiceDecisionStore(
                root / "decisions.json",
                voice_library=library,
            )
            chosen = original_group.candidates[-1]
            decisions.remember(original_group, chosen.source_id)
            saved = VoicePlanStore(
                jobs,
                decisions=decisions,
                voice_library=library,
            ).create(
                changed_job, settings, manifest_path=manifest
            )
            saved_group = next(
                group for group in saved.groups if group.character == "Rhiannon"
            )
            self.assertEqual(saved_group.route, "voice")
            self.assertEqual(saved_group.resolution, "saved-voice-assignment")
            self.assertEqual(
                library.binding("Rhiannon").source_sha256s,
                chosen.reference_sha256s,
            )

    def test_source_audio_is_excluded_and_lines_are_grouped_by_character(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices")

            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
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

    def test_public_pocket_uses_selected_preset_instead_of_reference_cloning(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            library = VoiceLibrary(root / "library")
            library.select("Narrator", route="voice", source_id="preset:marius")
            plan = VoicePlanStore(jobs, voice_library=library).create(
                job,
                AppSettings(),
                manifest_path=write_manifest(root / "voices"),
            )

            self.assertEqual(plan.audition_count, 0)
            for group in plan.groups:
                self.assertEqual(group.route, "narrator")
                self.assertEqual(group.source_character, "marius")
                self.assertEqual(group.source_speaker, "marius")
                self.assertEqual(group.reference_sha256s, ())

    def test_saved_alias_assignment_is_reused_automatically(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices")
            library = VoiceLibrary(root / "library")
            remember_voice_binding(
                library,
                CharacterVoiceRegistry.from_file(manifest),
                "Rhiannon",
                "character:centurion",
            )
            plan = VoicePlanStore(jobs, voice_library=library).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
                manifest_path=manifest,
            )

            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(rhiannon.source_character, "Centurion")
            self.assertEqual(rhiannon.resolution, "saved-voice-assignment")

    def test_character_defaults_apply_to_future_audio_with_manual_override_priority(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices")
            settings = AppSettings(pocket_gated_model_accepted=True)
            for source, expected_route, expected_voice in (
                ("character:centurion", "voice", "Centurion"),
                ("default", "narrator", "marius"),
                ("character:rhiannon", "voice", "Rhiannon"),
            ):
                with self.subTest(source=source):
                    library = VoiceLibrary(root / f"library-{expected_voice}")
                    library.select("Narrator", route="voice", source_id="preset:marius")
                    if source == "default":
                        library.select("Rhiannon", route="narrator")
                    else:
                        remember_voice_binding(
                            library,
                            CharacterVoiceRegistry.from_file(manifest),
                            "Rhiannon",
                            source,
                        )
                    plan = VoicePlanStore(jobs, voice_library=library).create(
                        job, settings, manifest_path=manifest
                    )
                    role = next(g for g in plan.groups if g.character == "Rhiannon")
                    self.assertEqual(role.route, expected_route)
                    self.assertEqual(role.source_character, expected_voice)
                    self.assertNotIn("line:original", role.line_ids)

    def test_game_voice_default_requires_pocket_cloning_access(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices")
            library = VoiceLibrary(root / "library")
            remember_voice_binding(
                library,
                CharacterVoiceRegistry.from_file(manifest),
                "Rhiannon",
                "character:centurion",
            )
            with self.assertRaisesRegex(
                PregenerationVoiceError, "requires Pocket voice cloning access"
            ):
                VoicePlanStore(jobs, voice_library=library).create(
                    job, AppSettings(), manifest_path=manifest
                )

    def test_close_variant_evidence_creates_one_informed_audition(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)

            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
                manifest_path=write_conflicting_manifest(root / "voices"),
            )

            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(rhiannon.route, "needs-audition")
            self.assertEqual(rhiannon.resolution, "ambiguous-voice-evidence")
            self.assertEqual(rhiannon.source_id, "preset:alba")
            self.assertEqual(rhiannon.source_character, "alba")
            self.assertEqual(plan.audition_count, 1)
            self.assertEqual(len(rhiannon.candidate_inventory), 3)
            self.assertEqual(len(rhiannon.candidates), 3)
            self.assertEqual(
                {candidate.match_score for candidate in rhiannon.candidates}, {90}
            )
            self.assertIsNone(rhiannon.anchor_source_id)
            self.assertEqual(
                {candidate.source_character for candidate in rhiannon.candidates},
                {
                    "Rhiannon",
                    "Source reference Rhiannon adult",
                    "Source reference Rhiannon child",
                },
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
                AppSettings(pocket_gated_model_accepted=True),
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
                AppSettings(pocket_gated_model_accepted=True),
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

    def test_saved_player_voice_keeps_other_references_available_for_inspection(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_player_candidate_manifest(
                root / "player-voices",
                job.story_index_sha256,
            )
            saved_source = "character:playercandidaterhiannon1"

            library = VoiceLibrary(root / "library")
            remember_voice_binding(
                library,
                CharacterVoiceRegistry.from_file(manifest),
                "Rhiannon",
                saved_source,
            )
            plan = VoicePlanStore(jobs, voice_library=library).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
                manifest_path=manifest,
            )

            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            binding = library.binding("Rhiannon")
            self.assertEqual(rhiannon.source_id, voice_binding_source_id(binding))
            self.assertEqual(binding.provenance["evidence"]["source_id"], saved_source)
            self.assertEqual(rhiannon.resolution, "saved-voice-assignment")
            self.assertEqual(len(rhiannon.candidates), 1)
            self.assertEqual(len(rhiannon.candidate_inventory), 2)

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

    def test_player_import_skips_one_invalid_optional_candidate(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_player_candidate_manifest(
                root / "player-voices",
                job.story_index_sha256,
            )
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document[PLAYER_VOICE_CANDIDATES_FIELD]["variants"][1][
                "source_event_ids"
            ] = [2, "invalid"]
            manifest.write_text(json.dumps(document), encoding="utf-8")

            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
                manifest_path=manifest,
            )

            rhiannon = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(
                [candidate.source_character for candidate in rhiannon.candidate_inventory],
                ["Player candidate Rhiannon 1"],
            )

    def test_player_import_still_rejects_changed_candidate_report(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_player_candidate_manifest(
                root / "player-voices",
                job.story_index_sha256,
            )
            (manifest.parent / "report.json").write_text("changed", encoding="utf-8")

            with self.assertRaisesRegex(
                PregenerationVoiceError,
                "Player voice candidate report changed",
            ):
                VoicePlanStore(jobs).create(
                    job,
                    AppSettings(),
                    manifest_path=manifest,
                )

    def test_player_import_candidate_ignores_changed_portrait(self):
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

            settings = AppSettings(pocket_gated_model_accepted=True)
            before = VoicePlanStore(jobs).create(job, settings, manifest_path=manifest)
            portrait.write_bytes(b"changed")
            after = VoicePlanStore(jobs).create(job, settings, manifest_path=manifest)
            self.assertEqual(
                [
                    (group.group_id, group.source_id, group.decision_context_sha256)
                    for group in before.groups
                ],
                [
                    (group.group_id, group.source_id, group.decision_context_sha256)
                    for group in after.groups
                ],
            )

    def test_exact_queue_voice_binding_wins_without_prompt(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)

            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
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
            library = VoiceLibrary(root / "library")
            decisions = VoiceDecisionStore(
                root / "decisions.json", voice_library=library
            )
            store = VoicePlanStore(jobs, decisions=decisions, voice_library=library)
            settings = AppSettings(pocket_gated_model_accepted=True)
            first = store.create(job, settings, manifest_path=manifest)
            group = next(
                value for value in first.groups if value.character == "Rhiannon"
            )

            decisions.remember(group, group.candidates[1].source_id)
            second = store.create(job, settings, manifest_path=manifest)
            resolved = next(
                value for value in second.groups if value.character == "Rhiannon"
            )

            self.assertEqual(resolved.route, "voice")
            self.assertEqual(resolved.resolution, "saved-voice-assignment")
            self.assertEqual(
                resolved.source_character,
                group.candidates[1].source_character,
            )
            self.assertEqual(second.audition_count, 0)

            library.select("Narrator", route="voice", source_id="preset:marius")
            changed_narrator = store.create(job, settings, manifest_path=manifest)
            preserved = next(
                value
                for value in changed_narrator.groups
                if value.character == "Rhiannon"
            )
            self.assertEqual(preserved.reference_sha256s, resolved.reference_sha256s)
            self.assertEqual(preserved.source_character, resolved.source_character)
            self.assertEqual(preserved.resolution, "saved-voice-assignment")
            self.assertEqual(changed_narrator.audition_count, 0)

            reconsidered = store.create(
                job,
                settings,
                manifest_path=manifest,
                ignore_decisions=True,
            )
            reopened = next(
                value for value in reconsidered.groups if value.character == "Rhiannon"
            )
            self.assertEqual(reopened.route, "voice")
            self.assertEqual(reopened.reference_sha256s, resolved.reference_sha256s)
            self.assertEqual(reconsidered.audition_count, 0)

    def test_changed_eligible_reference_requires_a_new_choice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_conflicting_manifest(root / "voices")
            decisions = VoiceDecisionStore(root / "decisions.json")
            store = VoicePlanStore(jobs, decisions=decisions)
            settings = AppSettings(pocket_gated_model_accepted=True)
            first = store.create(job, settings, manifest_path=manifest)
            group = next(
                value for value in first.groups if value.character == "Rhiannon"
            )
            reference_before = group.candidate_inventory[-1].reference_sha256s
            decisions.remember(group, group.candidates[0].source_id)

            child_reference = manifest.parent / "references" / "child.wav"
            child_reference.write_bytes(b"changed child reference")
            second = store.create(job, settings, manifest_path=manifest)
            resolved = next(
                value for value in second.groups if value.character == "Rhiannon"
            )

            self.assertNotEqual(
                reference_before,
                resolved.candidate_inventory[-1].reference_sha256s,
            )
            self.assertEqual(resolved.route, "needs-audition")
            self.assertEqual(resolved.resolution, "ambiguous-voice-evidence")
            self.assertEqual(resolved.source_id, "preset:alba")
            self.assertEqual(second.audition_count, 1)

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
            settings = AppSettings(pocket_gated_model_accepted=True)
            first = store.create(job, settings, manifest_path=manifest)
            first_group = next(
                group for group in first.groups if group.character == "Rhiannon"
            )

            manifest = write_manifest(root / "voices", unrelated=b"changed")
            second = store.create(job, settings, manifest_path=manifest)
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
            settings = AppSettings(pocket_gated_model_accepted=True)
            first = store.create(job, settings, manifest_path=manifest)
            first_groups = {group.character: group for group in first.groups}

            manifest = write_manifest(root / "voices", rhiannon=b"changed")
            second = store.create(job, settings, manifest_path=manifest)
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

    def test_decision_store_accepts_explicit_confirmation_of_automatic_route(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
                manifest_path=write_manifest(root / "voices"),
            )
            decisions = VoiceDecisionStore(root / "decisions.json")

            group = next(value for value in plan.groups if value.route == "voice")
            decisions.remember(group, group.source_id)

            self.assertEqual(
                decisions.choice_for(group.group_id, group.decision_context_sha256),
                group.source_id,
            )

    def test_decision_store_accepts_only_bound_candidate_or_narrator(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs = self.create_fixture(root)
            manifest = write_manifest(root / "voices")
            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
                manifest_path=manifest,
            )
            selected = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            group = replace(selected, route="needs-audition")
            decisions = VoiceDecisionStore(root / "decisions.json")

            with self.assertRaisesRegex(PregenerationVoiceError, "not part"):
                decisions.remember(group, "character:unrelated")

            decisions.remember(group, group.candidates[0].source_id)
            reloaded = VoiceDecisionStore(root / "decisions.json")
            self.assertEqual(
                reloaded.choice_for(group.group_id, group.decision_context_sha256),
                group.candidates[0].source_id,
            )
            reused = VoicePlanStore(jobs, decisions=reloaded).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
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
